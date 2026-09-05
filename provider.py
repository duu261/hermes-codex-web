"""Standalone Hermes tool for the Codex web command surface."""

from __future__ import annotations

import email.utils
import http.client
import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timezone
from threading import RLock
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL = "gpt-5.4-mini"
COMMAND_KEYS = (
    "search_query", "image_query", "open", "click", "find", "screenshot",
    "finance", "weather", "sports", "time",
)

FINANCE_TYPES = {"equity", "fund", "crypto", "index"}
SPORTS_FUNCTIONS = {"schedule", "standings"}
SPORTS_LEAGUES = {"nba", "wnba", "nfl", "nhl", "mlb", "epl", "ncaamb", "ncaawb", "ipl"}
RESPONSE_LENGTHS = {"short", "medium", "long"}
CONTEXT_SIZES = {"low", "medium", "high"}
ACCESS_MODES = {"cached", "indexed", "live"}
REASONING_SUMMARIES = {"auto", "concise", "detailed", "none"}
REASONING_CONTEXTS = {"auto", "current_turn", "all_turns"}
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_SESSION_TTL_SECONDS = 30 * 60
DEFAULT_MAX_SESSIONS = 1024
DEFAULT_MAX_REFS_PER_SESSION = 256
DEFAULT_MAX_REF_LENGTH = 512
DEFAULT_MAX_RETRY_AFTER_SECONDS = 60.0
UINT64_MAX = 2**64 - 1
ADVANCED_FIELDS = frozenset({
    "context", "input", "reasoning", "search_context_size", "allowed_domains",
    "blocked_domains", "image_settings", "external_web_access", "user_location",
    "max_output_tokens",
})
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class _ValidationError(ValueError):
    pass


class _ReferenceExpiredError(ValueError):
    pass


@dataclass
class _ConversationState:
    request_id: str
    expires_at: float
    refs: OrderedDict[str, str] = field(default_factory=OrderedDict)
    encrypted_output: str | None = None


def _env(name: str) -> str:
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        return os.getenv(name, "").strip()
    return (get_env_value(name) or "").strip()


def _setting(name: str) -> str:
    """Read credentials, with compatibility for the basic search plugin."""
    suffix = name.removeprefix("CODEX_WEB_")
    return _env(name) or _env(f"CODEX_SEARCH_{suffix}")


def _config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        loaded = load_config_readonly()
    except (ImportError, AttributeError):
        try:
            from hermes_cli.config import load_config
            loaded = load_config()
        except Exception:
            return {}
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    web = loaded.get("web")
    codex = web.get("codex_web") if isinstance(web, dict) else None
    return codex if isinstance(codex, dict) else {}


def _numeric_setting(name: str, default: float, *, minimum: float, maximum: float) -> float:
    value = _config().get(name, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if value != value:
        return default
    return min(max(value, minimum), maximum)


def _integer_setting(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = _config().get(name, default)
    if isinstance(value, bool):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


_sessions: OrderedDict[str, _ConversationState] = OrderedDict()
_sessions_lock = RLock()


def _expire_sessions_locked(now: float) -> None:
    expired = [key for key, state in _sessions.items() if state.expires_at <= now]
    for key in expired:
        _sessions.pop(key, None)


def clear_session_state() -> None:
    """Clear process-local continuation state, primarily for tests and shutdown."""
    with _sessions_lock:
        _sessions.clear()


def _session_state(session_id: str, *, create: bool) -> _ConversationState | None:
    now = time.monotonic()
    with _sessions_lock:
        _expire_sessions_locked(now)
        state = _sessions.get(session_id)
        if state is not None:
            state.expires_at = now + _numeric_setting(
                "session_ttl_seconds", DEFAULT_SESSION_TTL_SECONDS,
                minimum=1.0, maximum=24 * 60 * 60,
            )
            _sessions.move_to_end(session_id)
            return state
        if not create:
            return None
        state = _ConversationState(
            request_id=str(uuid.uuid4()),
            expires_at=now + _numeric_setting(
                "session_ttl_seconds", DEFAULT_SESSION_TTL_SECONDS,
                minimum=1.0, maximum=24 * 60 * 60,
            ),
        )
        _sessions[session_id] = state
        limit = _integer_setting("max_sessions", DEFAULT_MAX_SESSIONS, minimum=1, maximum=10000)
        while len(_sessions) > limit:
            _sessions.popitem(last=False)
        return state


def _session_request_id(session_id: str) -> str:
    """Return the stable upstream request id for one Hermes conversation."""
    return _session_state(session_id, create=True).request_id


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _ValidationError(f"{field} must be a non-negative integer")
    if value > UINT64_MAX:
        raise _ValidationError(f"{field} must be at most {UINT64_MAX}")
    return value


def _reject_unknown(item: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise _ValidationError(f"{field} has unknown field(s): {', '.join(unknown)}")


def _string_list(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise _ValidationError(f"{field} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _input_value(value: Any) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        return _nonempty_string(value, "input")
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise _ValidationError("input must be a non-empty string or list of objects")
    return value


def _reasoning(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise _ValidationError("reasoning must be a non-empty object")
    result: dict[str, str] = {}
    for key in ("effort", "summary", "context"):
        item = value.get(key)
        if item is None:
            continue
        item = _nonempty_string(item, f"reasoning.{key}")
        if key != "effort":
            item = item.lower()
        if key == "summary" and item not in REASONING_SUMMARIES:
            raise _ValidationError(f"reasoning.summary must be one of {sorted(REASONING_SUMMARIES)}")
        if key == "context" and item not in REASONING_CONTEXTS:
            raise _ValidationError(f"reasoning.context must be one of {sorted(REASONING_CONTEXTS)}")
        result[key] = item
    if not result or any(key not in {"effort", "summary", "context"} for key in value):
        raise _ValidationError("reasoning supports only effort, summary, and context")
    return result


def _objects(value: Any, field: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise _ValidationError(f"{field} must be a non-empty list of objects")
    return value


def _query_list(value: Any, field: str) -> list[dict[str, Any]] | None:
    rows = _objects(value, field)
    if rows is None:
        return None
    if field == "search_query" and len(rows) > 4:
        raise _ValidationError("search_query supports at most 4 queries")
    result = []
    for index, item in enumerate(rows):
        _reject_unknown(item, {"q", "recency", "domains"}, f"{field}[{index}]")
        row = {"q": _nonempty_string(item.get("q"), f"{field}[{index}].q")}
        recency = _optional_nonnegative_int(item.get("recency"), f"{field}[{index}].recency")
        domains = _string_list(item.get("domains"), f"{field}[{index}].domains")
        if recency is not None:
            row["recency"] = recency
        if domains is not None:
            row["domains"] = domains
        result.append(row)
    return result


def _operation_list(value: Any, field: str, required: tuple[str, ...], optional_ints: tuple[str, ...] = ()) -> list[dict[str, Any]] | None:
    rows = _objects(value, field)
    if rows is None:
        return None
    result = []
    for index, item in enumerate(rows):
        _reject_unknown(item, set(required) | set(optional_ints), f"{field}[{index}]")
        row = {key: _nonempty_string(item.get(key), f"{field}[{index}].{key}") for key in required}
        for key in optional_ints:
            number = _optional_nonnegative_int(item.get(key), f"{field}[{index}].{key}")
            if number is not None:
                row[key] = number
        result.append(row)
    return result


def _validate_special_operations(params: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    commands: dict[str, list[dict[str, Any]]] = {}
    for key in ("search_query", "image_query"):
        rows = _query_list(params.get(key), key)
        if rows is not None:
            commands[key] = rows

    for key, required, optional_ints in (
        ("open", ("ref_id",), ("lineno",)),
        ("find", ("ref_id", "pattern"), ()),
        ("time", ("utc_offset",), ()),
    ):
        rows = _operation_list(params.get(key), key, required, optional_ints)
        if rows is not None:
            commands[key] = rows

    rows = _objects(params.get("screenshot"), "screenshot")
    if rows is not None:
        commands["screenshot"] = []
        for index, item in enumerate(rows):
            _reject_unknown(item, {"ref_id", "pageno"}, f"screenshot[{index}]")
            pageno = _optional_nonnegative_int(item.get("pageno"), f"screenshot[{index}].pageno")
            if pageno is None:
                raise _ValidationError(f"screenshot[{index}].pageno is required")
            commands["screenshot"].append({
                "ref_id": _nonempty_string(item.get("ref_id"), f"screenshot[{index}].ref_id"),
                "pageno": pageno,
            })

    rows = _objects(params.get("click"), "click")
    if rows is not None:
        commands["click"] = []
        for index, item in enumerate(rows):
            _reject_unknown(item, {"ref_id", "id"}, f"click[{index}]")
            link_id = _optional_nonnegative_int(item.get("id"), f"click[{index}].id")
            if link_id is None:
                raise _ValidationError(f"click[{index}].id is required")
            commands["click"].append({
                "ref_id": _nonempty_string(item.get("ref_id"), f"click[{index}].ref_id"),
                "id": link_id,
            })

    rows = _objects(params.get("finance"), "finance")
    if rows is not None:
        commands["finance"] = []
        for index, item in enumerate(rows):
            _reject_unknown(item, {"ticker", "type", "market"}, f"finance[{index}]")
            asset_type = _nonempty_string(item.get("type"), f"finance[{index}].type").lower()
            if asset_type not in FINANCE_TYPES:
                raise _ValidationError(f"finance[{index}].type must be one of {sorted(FINANCE_TYPES)}")
            row = {
                "ticker": _nonempty_string(item.get("ticker"), f"finance[{index}].ticker"),
                "type": asset_type,
            }
            if item.get("market") is not None:
                market = item["market"]
                if asset_type == "crypto" and market == "":
                    row["market"] = ""
                else:
                    row["market"] = _nonempty_string(market, f"finance[{index}].market")
            commands["finance"].append(row)

    rows = _objects(params.get("weather"), "weather")
    if rows is not None:
        commands["weather"] = []
        for index, item in enumerate(rows):
            _reject_unknown(item, {"location", "start", "duration"}, f"weather[{index}]")
            row = {"location": _nonempty_string(item.get("location"), f"weather[{index}].location")}
            if item.get("start") is not None:
                row["start"] = _nonempty_string(item["start"], f"weather[{index}].start")
            duration = _optional_nonnegative_int(item.get("duration"), f"weather[{index}].duration")
            if duration is not None:
                row["duration"] = duration
            commands["weather"] = commands.get("weather", []) + [row]

    rows = _objects(params.get("sports"), "sports")
    if rows is not None:
        commands["sports"] = []
        for index, item in enumerate(rows):
            _reject_unknown(item, {"tool", "fn", "league", "team", "opponent", "date_from", "date_to", "num_games", "locale"}, f"sports[{index}]")
            function = _nonempty_string(item.get("fn"), f"sports[{index}].fn").lower()
            league = _nonempty_string(item.get("league"), f"sports[{index}].league").lower()
            if function not in SPORTS_FUNCTIONS:
                raise _ValidationError(f"sports[{index}].fn must be one of {sorted(SPORTS_FUNCTIONS)}")
            if league not in SPORTS_LEAGUES:
                raise _ValidationError(f"sports[{index}].league must be one of {sorted(SPORTS_LEAGUES)}")
            row: dict[str, Any] = {"tool": "sports", "fn": function, "league": league}
            if item.get("tool") is not None:
                tool = _nonempty_string(item["tool"], f"sports[{index}].tool").lower()
                if tool != "sports":
                    raise _ValidationError(f"sports[{index}].tool must be 'sports'")
            for key in ("team", "opponent", "date_from", "date_to", "locale"):
                if item.get(key) is not None:
                    row[key] = _nonempty_string(item[key], f"sports[{index}].{key}")
            number = _optional_nonnegative_int(item.get("num_games"), f"sports[{index}].num_games")
            if number is not None:
                row["num_games"] = number
            commands["sports"].append(row)
    return commands


def build_codex_web_payload(params: dict[str, Any], *, model: str, request_id: str | None = None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise _ValidationError("parameters must be an object")
    unknown = sorted(set(params) - (set(COMMAND_KEYS) | {"response_length"} | ADVANCED_FIELDS))
    if unknown:
        raise _ValidationError(f"unknown parameter(s): {', '.join(unknown)}")
    commands = _validate_special_operations(params)
    if not any(key in commands for key in COMMAND_KEYS):
        raise _ValidationError("at least one web command is required")
    response_length = params.get("response_length")
    if response_length is not None:
        response_length = _nonempty_string(response_length, "response_length")
        if response_length not in RESPONSE_LENGTHS:
            raise _ValidationError(f"response_length must be one of {sorted(RESPONSE_LENGTHS)} (case-sensitive)")
    if len(commands.get("search_query", [])) > 3 and response_length not in {"medium", "long"}:
        raise _ValidationError("response_length must be medium or long for more than 3 search queries")
    if response_length is not None:
        commands["response_length"] = response_length

    settings: dict[str, Any] = {
        "allowed_callers": ["direct"],
        "external_web_access": True,
    }
    context_size = params.get("search_context_size")
    if context_size is not None:
        context_size = _nonempty_string(context_size, "search_context_size").lower()
        if context_size not in CONTEXT_SIZES:
            raise _ValidationError(f"search_context_size must be one of {sorted(CONTEXT_SIZES)}")
        settings["search_context_size"] = context_size

    for key in ("allowed_domains", "blocked_domains"):
        domains = _string_list(params.get(key), key)
        if domains is not None:
            settings.setdefault("filters", {})[key] = domains

    image_settings = params.get("image_settings")
    if image_settings is not None:
        if not isinstance(image_settings, dict):
            raise _ValidationError("image_settings must be an object")
        _reject_unknown(image_settings, {"max_results", "caption"}, "image_settings")
        image_payload: dict[str, Any] = {}
        max_results = _optional_nonnegative_int(image_settings.get("max_results"), "image_settings.max_results")
        if max_results is not None:
            image_payload["max_results"] = max_results
        if image_settings.get("caption") is not None:
            if not isinstance(image_settings["caption"], bool):
                raise _ValidationError("image_settings.caption must be boolean")
            image_payload["caption"] = image_settings["caption"]
        if not image_payload:
            raise _ValidationError("image_settings must contain max_results or caption")
        settings["image_settings"] = image_payload

    access = params.get("external_web_access")
    if access is not None:
        if isinstance(access, bool):
            settings["external_web_access"] = access
        else:
            access = _nonempty_string(access, "external_web_access").lower()
            if access not in ACCESS_MODES:
                raise _ValidationError(f"external_web_access must be boolean or one of {sorted(ACCESS_MODES)}")
            settings["external_web_access"] = access

    location = params.get("user_location")
    if location is not None:
        if not isinstance(location, dict):
            raise _ValidationError("user_location must be an object")
        _reject_unknown(location, {"country", "region", "city", "timezone"}, "user_location")
        location_payload = {"type": "approximate"}
        for key in ("country", "region", "city", "timezone"):
            if location.get(key) is not None:
                location_payload[key] = _nonempty_string(location[key], f"user_location.{key}")
        settings["user_location"] = location_payload

    payload: dict[str, Any] = {
        "id": request_id or str(uuid.uuid4()),
        "model": _nonempty_string(model, "model"),
        "commands": commands,
        "settings": settings,
    }
    if params.get("input") is not None and params.get("context") is not None:
        raise _ValidationError("input and context cannot both be set")
    if params.get("input") is not None:
        payload["input"] = _input_value(params["input"])
    elif params.get("context") is not None:
        payload["input"] = _nonempty_string(params["context"], "context")
    if params.get("reasoning") is not None:
        payload["reasoning"] = _reasoning(params["reasoning"])
    max_output_tokens = _optional_nonnegative_int(params.get("max_output_tokens"), "max_output_tokens")
    if max_output_tokens is not None:
        if max_output_tokens == 0:
            raise _ValidationError("max_output_tokens must be greater than zero")
        payload["max_output_tokens"] = max_output_tokens
    return payload


def _endpoint(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    return base_url if base_url.endswith("/alpha/search") else f"{base_url}/alpha/search"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer credential to a redirected origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_request(request: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(_NoRedirectHandler)
    return opener.open(request, timeout=timeout)


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        value = headers.get(name)
        if value is None:
            wanted = name.lower()
            for key, candidate in headers.items():
                if str(key).lower() == wanted:
                    value = candidate
                    break
    except AttributeError:
        return None
    return str(value).strip() if value else None


def _request_id(headers: Any, payload: Any = None) -> str | None:
    for name in ("x-request-id", "request-id", "x-openai-request-id"):
        value = _header(headers, name)
        if value:
            return value[:200]
    if isinstance(payload, dict):
        value = payload.get("request_id")
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return None


def _error(error_type: str, message: str, *, status: int | None = None, request_id: str | None = None) -> dict[str, Any]:
    details: dict[str, Any] = {"type": error_type, "message": str(message)[:500]}
    if status is not None:
        details["status"] = status
    if request_id:
        details["request_id"] = request_id
    return {"success": False, "error": details}


def _attach_request_id(result: dict[str, Any], request_id: str | None) -> dict[str, Any]:
    if request_id and isinstance(result.get("error"), dict):
        result["error"].setdefault("request_id", request_id)
    return result


def _read_limited(response: Any, limit: int) -> tuple[bytes | None, bool]:
    length = _header(getattr(response, "headers", None), "content-length")
    if length:
        try:
            if int(length) > limit:
                return None, True
        except ValueError:
            pass
    try:
        body = response.read(limit + 1)
    except TypeError:
        body = response.read()
    if not isinstance(body, (bytes, bytearray)):
        return None, False
    if len(body) > limit:
        return None, True
    return bytes(body), False


def _parse_response_body(response: Any, limit: int) -> tuple[Any, dict[str, Any] | None]:
    body, oversized = _read_limited(response, limit)
    if oversized:
        return None, _error("response_too_large", "Codex Web response exceeded the configured size limit")
    if body is None:
        return None, _error("malformed_response", "Codex Web returned a non-byte response body")
    try:
        return json.loads(body.decode("utf-8")), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _error("malformed_response", "Codex Web returned invalid JSON")


def _retry_after(headers: Any, now: float | None = None) -> float | None:
    value = _header(headers, "retry-after")
    if not value:
        return None
    maximum = _numeric_setting(
        "max_retry_after_seconds", DEFAULT_MAX_RETRY_AFTER_SECONDS,
        minimum=0.0, maximum=300.0,
    )
    try:
        return min(maximum, max(0.0, float(value)))
    except ValueError:
        try:
            target = email.utils.parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return min(maximum, max(0.0, target.timestamp() - (time.time() if now is None else now)))
        except (TypeError, ValueError, OverflowError):
            return None


def _retry_delay(headers: Any, attempt: int) -> float:
    retry_after = _retry_after(headers)
    backoff = min(10.0, _numeric_setting("retry_base_seconds", 0.25, minimum=0.0, maximum=10.0) * (2 ** attempt))
    jitter = random.uniform(0.0, min(1.0, max(backoff, 0.01)))
    return max(retry_after or 0.0, backoff) + jitter if retry_after is None else retry_after + jitter


def _http_failure(exc: urllib.error.HTTPError) -> tuple[str, int | None, str | None]:
    headers = getattr(exc, "headers", None)
    request_id = _request_id(headers)
    status = int(exc.code)
    return f"Codex Web returned HTTP {status}", status, request_id


def _classify_status(status: int) -> str:
    if status == 402:
        return "quota"
    if status in {401, 403}:
        return "authentication"
    if status == 429:
        return "rate_limit"
    if 400 <= status < 500:
        return "upstream"
    return "upstream"


def _normalize_success(data: Any, request_id: str | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _error("malformed_response", "Codex Web returned a JSON value instead of an object")
    if data.get("error") is not None:
        return _error("upstream", "Codex Web returned an upstream error", request_id=request_id or _request_id(None, data))
    output = data.get("output")
    results = data.get("results", [])
    if results is None:
        results = []
    if not isinstance(results, list):
        return _error("malformed_response", "Codex Web returned no result list")
    if not isinstance(output, str):
        return _error("malformed_response", "Codex Web returned invalid output")
    if output.lstrip().startswith(("Error ", "Internal Error")):
        return _error("upstream", "Codex Web returned an endpoint error")

    sources = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        source: dict[str, Any] = {"url": url}
        for key in ("ref_id", "title"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                source[key] = value
        item_type = item.get("type")
        source["type"] = "image" if isinstance(item_type, str) and "image" in item_type.lower() else "web"
        sources.append(source)

    result: dict[str, Any] = {
        "success": True,
        "output": output,
        "results": results,
        "sources": sources,
    }
    return result


def _post(payload: dict[str, Any], state: _ConversationState | None = None) -> dict[str, Any]:
    base_url = _setting("CODEX_WEB_BASE_URL")
    api_key = _setting("CODEX_WEB_API_KEY")
    if not base_url:
        return _error("configuration", "CODEX_WEB_BASE_URL is not set")
    if not api_key:
        return _error("authentication", "CODEX_WEB_API_KEY is not set")
    endpoint = _endpoint(base_url)
    try:
        parsed = urlparse(endpoint)
        hostname = parsed.hostname
        _ = parsed.port  # Validate the port without echoing parser exceptions.
        if (not hostname or parsed.username is not None or parsed.password is not None
                or any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)):
            return _error("configuration", "CODEX_WEB_BASE_URL must be a valid URL without embedded credentials")
    except ValueError:
        return _error("configuration", "CODEX_WEB_BASE_URL must be a valid URL without embedded credentials")
    if parsed.scheme != "https":
        return _error("configuration", "CODEX_WEB_BASE_URL must use HTTPS")
    if any(ord(char) < 32 or ord(char) == 127 for char in api_key):
        return _error("configuration", "CODEX_WEB_API_KEY must not contain control characters")

    try:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "responses=experimental",
                "Originator": "codex_cli_rs",
            },
            method="POST",
        )
    except (ValueError, http.client.InvalidURL):
        return _error("configuration", "Codex Web request configuration is invalid")
    max_retries = _integer_setting("max_retries", DEFAULT_MAX_RETRIES, minimum=0, maximum=5)
    timeout = _numeric_setting("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS, minimum=0.1, maximum=600.0)
    max_response_bytes = _integer_setting("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES, minimum=1, maximum=64 * 1024 * 1024)

    for attempt in range(max_retries + 1):
        try:
            with _open_request(request, timeout=timeout) as response:
                data, error = _parse_response_body(response, max_response_bytes)
                if error is not None:
                    return _attach_request_id(error, _request_id(getattr(response, "headers", None)))
                if isinstance(data, dict):
                    encrypted_output = data.get("encrypted_output")
                    if encrypted_output is not None and not isinstance(encrypted_output, str):
                        return _error("malformed_response", "Codex Web returned invalid encrypted output")
                result = _normalize_success(data, _request_id(getattr(response, "headers", None)))
                if state is not None and isinstance(data, dict) and isinstance(data.get("encrypted_output"), str):
                    with _sessions_lock:
                        state.encrypted_output = data["encrypted_output"]
                return result
        except urllib.error.HTTPError as exc:
            message, status, upstream_request_id = _http_failure(exc)
            if status in RETRYABLE_STATUS_CODES and attempt < max_retries:
                time.sleep(_retry_delay(getattr(exc, "headers", None), attempt))
                continue
            return _error(_classify_status(status or 0), message, status=status, request_id=upstream_request_id)
        except (socket.timeout, TimeoutError):
            return _error("timeout", "Codex Web request timed out")
        except urllib.error.URLError:
            return _error("upstream", "Could not reach Codex Web")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error("malformed_response", "Codex Web returned invalid JSON")
        except (ValueError, http.client.InvalidURL):
            return _error("configuration", "Codex Web request configuration is invalid")
        except OSError:
            return _error("upstream", "Could not reach Codex Web")
    return _error("upstream", "Codex Web request failed")


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_pdf_url(value: Any) -> bool:
    return isinstance(value, str) and urlparse(value).path.lower().endswith(".pdf")


def _is_pdf_result(item: dict[str, Any]) -> bool:
    if _is_pdf_url(item.get("url")):
        return True
    for key in ("content_type", "mime_type", "media_type"):
        value = item.get(key)
        if isinstance(value, str) and value.lower().split(";", 1)[0].strip() == "application/pdf":
            return True
    return False


def _reference_values(params: dict[str, Any]) -> list[str]:
    values = []
    for key in ("open", "find", "click", "screenshot"):
        for item in params.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("ref_id"), str):
                values.append(item["ref_id"].strip())
    return values


def _validate_continuation(params: dict[str, Any], session_id: str | None) -> _ConversationState | None:
    screenshot_urls: list[str] = []
    for item in params.get("screenshot") or []:
        if isinstance(item, dict) and isinstance(item.get("ref_id"), str):
            screenshot_urls.append(item["ref_id"])
    if any(_is_url(ref) for ref in screenshot_urls):
        raise _ValidationError("screenshot requires an opened PDF reference; open the PDF first")
    refs = [ref for ref in _reference_values(params) if not _is_url(ref)]
    if not refs:
        return _session_state(session_id, create=True) if session_id else None
    if not session_id:
        raise _ReferenceExpiredError("reference continuation requires a Hermes conversation")
    state = _session_state(session_id, create=False)
    if state is None:
        raise _ReferenceExpiredError("reference expired; repeat the search or open the URL again")
    screenshot_refs = {
        item["ref_id"]
        for item in params.get("screenshot") or []
        if isinstance(item, dict) and isinstance(item.get("ref_id"), str)
    }
    with _sessions_lock:
        missing = [ref for ref in refs if ref not in state.refs]
        non_pdf_screenshots = [ref for ref in refs if ref in screenshot_refs and state.refs.get(ref) != "pdf"]
    if missing:
        raise _ReferenceExpiredError("reference is unknown or expired; repeat the search or open the URL again")
    if non_pdf_screenshots:
        raise _ValidationError("screenshot requires an opened PDF reference; open the PDF first")
    return state


def _record_refs(state: _ConversationState | None, result: dict[str, Any], params: dict[str, Any] | None = None) -> None:
    if state is None or not result.get("success"):
        return
    refs: list[tuple[str, str]] = []
    params = params or {}
    is_search = "search_query" in params or "image_query" in params
    open_rows = params.get("open") or []
    with _sessions_lock:
        opened_pdf = any(
            isinstance(item, dict)
            and isinstance(item.get("ref_id"), str)
            and (
                _is_pdf_url(item["ref_id"])
                or state.refs.get(item["ref_id"]) in {"search_pdf", "pdf"}
            )
            for item in open_rows
        )
    for item in result.get("results", []):
        if isinstance(item, dict) and isinstance(item.get("ref_id"), str) and item["ref_id"].strip():
            ref = item["ref_id"].strip()
            if is_search:
                kind = "search_pdf" if _is_pdf_result(item) else "search"
            elif open_rows:
                kind = "pdf" if opened_pdf or _is_pdf_result(item) else "page"
            elif params.get("screenshot"):
                kind = "pdf"
            else:
                kind = "unknown"
            refs.append((ref, kind))
    if refs:
        with _sessions_lock:
            limit = _integer_setting(
                "max_refs_per_session", DEFAULT_MAX_REFS_PER_SESSION,
                minimum=1, maximum=4096,
            )
            max_length = _integer_setting(
                "max_ref_length", DEFAULT_MAX_REF_LENGTH,
                minimum=1, maximum=4096,
            )
            for ref, kind in refs:
                if len(ref) > max_length:
                    continue
                state.refs[ref] = kind
                state.refs.move_to_end(ref)
            while len(state.refs) > limit:
                state.refs.popitem(last=False)


def is_available() -> bool:
    return bool(_setting("CODEX_WEB_BASE_URL") and _setting("CODEX_WEB_API_KEY"))


def handle_codex_web(params: dict[str, Any], **kwargs: Any) -> str:
    try:
        if not isinstance(params, dict):
            raise _ValidationError("parameters must be an object")
        session_id = kwargs.get("session_id")
        session_id = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        state = _validate_continuation(params, session_id)
        request_id = state.request_id if state is not None else None
        payload = build_codex_web_payload(params, model=_model(), request_id=request_id)
        result = _post(payload, state)
        _record_refs(state, result, params)
        return json.dumps(result, ensure_ascii=False)
    except _ReferenceExpiredError as exc:
        return json.dumps(_error("reference_expired", str(exc)), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return json.dumps(_error("validation", str(exc)), ensure_ascii=False)


def _model() -> str:
    value = _config().get("model")
    return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_MODEL


_STRING = {"type": "string", "minLength": 1}
_REF = {"type": "string", "minLength": 1}
_UINT64 = {"type": "integer", "minimum": 0, "maximum": UINT64_MAX}
_QUERY = {
    "type": "array", "minItems": 1,
    "items": {"type": "object", "required": ["q"], "additionalProperties": False, "properties": {
        "q": _STRING, "recency": _UINT64,
        "domains": {"type": "array", "items": _STRING, "description": "Advisory domain filter. Verify returned sources; not a security boundary."},
    }},
}
_SEARCH_QUERY = {**_QUERY, "maxItems": 4}
CODEX_WEB_SCHEMA = {
    "name": "codex_web",
    "description": "Use codex_web for Codex-specific web operations: search, image search, open, find, click, PDF screenshots, finance, weather, sports, and time. Use the returned ref_id for follow-up open, find, click, or screenshot calls in the same conversation. Open a PDF before screenshot. Prefer web_search for ordinary provider-neutral search and web_extract for general extraction. Return evidence and never invent sources.",
    "parameters": {
        "type": "object",
        "properties": {
            "context": {**_STRING, "description": "Optional context hint. Advisory and endpoint-dependent."},
            "search_query": _SEARCH_QUERY,
            "image_query": _QUERY,
            "open": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["ref_id"], "additionalProperties": False, "properties": {"ref_id": _REF, "lineno": _UINT64}}},
            "click": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["ref_id", "id"], "additionalProperties": False, "properties": {"ref_id": _REF, "id": _UINT64}}},
            "find": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["ref_id", "pattern"], "additionalProperties": False, "properties": {"ref_id": _REF, "pattern": _STRING}}},
            "screenshot": {"type": "array", "minItems": 1, "description": "Open the PDF first, then reuse its returned ref_id. pageno is zero-based.", "items": {"type": "object", "required": ["ref_id", "pageno"], "additionalProperties": False, "properties": {"ref_id": _REF, "pageno": _UINT64}}},
            "finance": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["ticker", "type"], "additionalProperties": False, "properties": {"ticker": _STRING, "type": {"type": "string", "enum": sorted(FINANCE_TYPES)}, "market": {"type": "string"}}}},
            "weather": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["location"], "additionalProperties": False, "properties": {"location": _STRING, "start": _STRING, "duration": _UINT64}}},
            "sports": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["fn", "league"], "additionalProperties": False, "properties": {"fn": {"type": "string", "enum": sorted(SPORTS_FUNCTIONS)}, "league": {"type": "string", "enum": sorted(SPORTS_LEAGUES)}, "team": _STRING, "opponent": _STRING, "date_from": _STRING, "date_to": _STRING, "num_games": _UINT64, "locale": _STRING}}},
            "time": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["utc_offset"], "additionalProperties": False, "properties": {"utc_offset": _STRING}}},
            "response_length": {"type": "string", "enum": sorted(RESPONSE_LENGTHS)},
            "search_context_size": {"type": "string", "enum": sorted(CONTEXT_SIZES), "description": "Advisory retrieval breadth hint. Behavior is endpoint-dependent."},
            "allowed_domains": {"type": "array", "items": _STRING, "description": "Advisory domain allowlist. Verify returned sources; not a security boundary."},
            "blocked_domains": {"type": "array", "items": _STRING, "description": "Advisory domain blocklist. Verify returned sources; not a security boundary."},
            "image_settings": {"type": "object", "minProperties": 1, "additionalProperties": False, "description": "Advisory image result preferences. Behavior is endpoint-dependent.", "properties": {"max_results": _UINT64, "caption": {"type": "boolean"}}},
            "external_web_access": {"description": "Endpoint access hint. Not a network or privacy control.", "oneOf": [{"type": "boolean"}, {"type": "string", "enum": sorted(ACCESS_MODES)}]},
            "user_location": {"type": "object", "additionalProperties": False, "description": "Approximate location sent to the configured endpoint and its intermediaries; may affect results.", "properties": {"country": _STRING, "region": _STRING, "city": _STRING, "timezone": _STRING}},
            "max_output_tokens": {"type": "integer", "minimum": 1, "maximum": UINT64_MAX, "description": "Maximum endpoint output tokens."},
        },
        "additionalProperties": False,
    },
}
