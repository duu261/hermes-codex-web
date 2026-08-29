"""Standalone Hermes tool for the Codex alpha web-search command surface."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from threading import Lock
from typing import Any

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


def _model() -> str:
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        value = (config.get("web") or {}).get("codex_web", {}).get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass
    return DEFAULT_MODEL


_SESSION_REQUEST_ID_LIMIT = 1024
_session_request_ids: OrderedDict[str, str] = OrderedDict()
_session_request_ids_lock = Lock()


def _session_request_id(session_id: str) -> str:
    with _session_request_ids_lock:
        request_id = _session_request_ids.get(session_id)
        if request_id is None:
            request_id = str(uuid.uuid4())
            _session_request_ids[session_id] = request_id
            if len(_session_request_ids) > _SESSION_REQUEST_ID_LIMIT:
                _session_request_ids.popitem(last=False)
        else:
            _session_request_ids.move_to_end(session_id)
        return request_id


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _string_list(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _objects(value: Any, field: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must be a non-empty list of objects")
    return value


def _query_list(value: Any, field: str) -> list[dict[str, Any]] | None:
    rows = _objects(value, field)
    if rows is None:
        return None
    if field == "search_query" and len(rows) > 4:
        raise ValueError("search_query supports at most 4 queries")
    result = []
    for index, item in enumerate(rows):
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
            pageno = _optional_nonnegative_int(item.get("pageno"), f"screenshot[{index}].pageno")
            if pageno is None:
                raise ValueError(f"screenshot[{index}].pageno is required")
            commands["screenshot"].append({
                "ref_id": _nonempty_string(item.get("ref_id"), f"screenshot[{index}].ref_id"),
                "pageno": pageno,
            })

    rows = _objects(params.get("click"), "click")
    if rows is not None:
        commands["click"] = []
        for index, item in enumerate(rows):
            link_id = _optional_nonnegative_int(item.get("id"), f"click[{index}].id")
            if link_id is None:
                raise ValueError(f"click[{index}].id is required")
            commands["click"].append({
                "ref_id": _nonempty_string(item.get("ref_id"), f"click[{index}].ref_id"),
                "id": link_id,
            })

    rows = _objects(params.get("finance"), "finance")
    if rows is not None:
        commands["finance"] = []
        for index, item in enumerate(rows):
            asset_type = _nonempty_string(item.get("type"), f"finance[{index}].type").lower()
            if asset_type not in FINANCE_TYPES:
                raise ValueError(f"finance[{index}].type must be one of {sorted(FINANCE_TYPES)}")
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
            row = {"location": _nonempty_string(item.get("location"), f"weather[{index}].location")}
            if item.get("start") is not None:
                row["start"] = _nonempty_string(item["start"], f"weather[{index}].start")
            duration = _optional_nonnegative_int(item.get("duration"), f"weather[{index}].duration")
            if duration is not None:
                row["duration"] = duration
            commands["weather"].append(row)

    rows = _objects(params.get("sports"), "sports")
    if rows is not None:
        commands["sports"] = []
        for index, item in enumerate(rows):
            function = _nonempty_string(item.get("fn"), f"sports[{index}].fn").lower()
            league = _nonempty_string(item.get("league"), f"sports[{index}].league").lower()
            if function not in SPORTS_FUNCTIONS:
                raise ValueError(f"sports[{index}].fn must be one of {sorted(SPORTS_FUNCTIONS)}")
            if league not in SPORTS_LEAGUES:
                raise ValueError(f"sports[{index}].league must be one of {sorted(SPORTS_LEAGUES)}")
            row: dict[str, Any] = {"tool": "sports", "fn": function, "league": league}
            if item.get("tool") is not None:
                tool = _nonempty_string(item["tool"], f"sports[{index}].tool").lower()
                if tool != "sports":
                    raise ValueError(f"sports[{index}].tool must be 'sports'")
            for key in ("team", "opponent", "date_from", "date_to", "locale"):
                if item.get(key) is not None:
                    row[key] = _nonempty_string(item[key], f"sports[{index}].{key}")
            for key in ("num_games",):
                number = _optional_nonnegative_int(item.get(key), f"sports[{index}].{key}")
                if number is not None:
                    row[key] = number
            commands["sports"].append(row)
    return commands


def build_codex_web_payload(params: dict[str, Any], *, model: str, request_id: str | None = None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("parameters must be an object")
    commands = _validate_special_operations(params)
    if not any(key in commands for key in COMMAND_KEYS):
        raise ValueError("at least one web command is required")
    response_length = params.get("response_length")
    if response_length is not None:
        response_length = _nonempty_string(response_length, "response_length").lower()
        if response_length not in RESPONSE_LENGTHS:
            raise ValueError(f"response_length must be one of {sorted(RESPONSE_LENGTHS)}")
    if len(commands.get("search_query", [])) > 3 and response_length not in {"medium", "long"}:
        raise ValueError("response_length must be medium or long for more than 3 search queries")
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
            raise ValueError(f"search_context_size must be one of {sorted(CONTEXT_SIZES)}")
        settings["search_context_size"] = context_size

    for key in ("allowed_domains", "blocked_domains"):
        domains = _string_list(params.get(key), key)
        if domains is not None:
            settings.setdefault("filters", {})[key] = domains

    image_settings = params.get("image_settings")
    if image_settings is not None:
        if not isinstance(image_settings, dict):
            raise ValueError("image_settings must be an object")
        image_payload: dict[str, Any] = {}
        max_results = _optional_nonnegative_int(image_settings.get("max_results"), "image_settings.max_results")
        if max_results is not None:
            image_payload["max_results"] = max_results
        if image_settings.get("caption") is not None:
            if not isinstance(image_settings["caption"], bool):
                raise ValueError("image_settings.caption must be boolean")
            image_payload["caption"] = image_settings["caption"]
        if not image_payload:
            raise ValueError("image_settings must contain max_results or caption")
        settings["image_settings"] = image_payload

    access = params.get("external_web_access")
    if access is not None:
        if isinstance(access, bool):
            settings["external_web_access"] = access
        else:
            access = _nonempty_string(access, "external_web_access").lower()
            if access not in ACCESS_MODES:
                raise ValueError(f"external_web_access must be boolean or one of {sorted(ACCESS_MODES)}")
            settings["external_web_access"] = access

    location = params.get("user_location")
    if location is not None:
        if not isinstance(location, dict):
            raise ValueError("user_location must be an object")
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
    if params.get("context") is not None:
        payload["input"] = _nonempty_string(params["context"], "context")
    max_output_tokens = _optional_nonnegative_int(params.get("max_output_tokens"), "max_output_tokens")
    if max_output_tokens is not None:
        if max_output_tokens == 0:
            raise ValueError("max_output_tokens must be greater than zero")
        payload["max_output_tokens"] = max_output_tokens
    return payload


def _endpoint(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    return base_url if base_url.endswith("/alpha/search") else f"{base_url}/alpha/search"


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    base_url = _setting("CODEX_WEB_BASE_URL")
    api_key = _setting("CODEX_WEB_API_KEY")
    if not base_url:
        return {"success": False, "error": "CODEX_WEB_BASE_URL is not set"}
    if not api_key:
        return {"success": False, "error": "CODEX_WEB_API_KEY is not set"}
    request = urllib.request.Request(
        _endpoint(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": f"Codex Web returned HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError):
        return {"success": False, "error": "Could not reach Codex Web"}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"success": False, "error": "Codex Web returned invalid JSON"}

    if not isinstance(data, dict) or data.get("error") is not None:
        return {"success": False, "error": "Codex Web returned an error"}
    output = data.get("output")
    results = data.get("results")
    if not isinstance(results, list):
        return {"success": False, "error": "Codex Web returned no result list"}
    if not isinstance(output, str):
        return {"success": False, "error": "Codex Web returned invalid output"}
    if output.lstrip().startswith(("Error ", "Internal Error")):
        return {"success": False, "error": "Codex Web returned an endpoint error"}
    return {
        "success": True,
        "output": output,
        "results": results,
    }


def is_available() -> bool:
    return bool(_setting("CODEX_WEB_BASE_URL") and _setting("CODEX_WEB_API_KEY"))


def handle_codex_web(params: dict[str, Any], **kwargs: Any) -> str:
    try:
        session_id = kwargs.get("session_id")
        request_id = (
            _session_request_id(session_id.strip())
            if isinstance(session_id, str) and session_id.strip()
            else None
        )
        payload = build_codex_web_payload(
            params,
            model=_model(),
            request_id=request_id,
        )
        return json.dumps(_post(payload), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


_STRING = {"type": "string"}
_QUERY = {
    "type": "array", "items": {"type": "object", "required": ["q"], "properties": {
        "q": _STRING, "recency": {"type": "integer", "minimum": 0},
        "domains": {"type": "array", "items": _STRING},
    }},
}
_REF = {"type": "string", "minLength": 1}
CODEX_WEB_SCHEMA = {
    "name": "codex_web",
    "description": "Use codex_web for Codex-specific operations - image search, opening/finding/clicking within pages, PDF screenshots, finance, weather, sports, or time - or when the task explicitly requires the Codex endpoint. For PDF screenshots, open the PDF first and reuse its returned ref_id. Prefer web_search for ordinary provider-neutral search and web_extract for extracting page content. Return endpoint results without inventing facts.",
    "parameters": {
        "type": "object",
        "properties": {
            "context": {"type": "string", "description": "Optional focused context for this web request."},
            "search_query": _QUERY,
            "image_query": _QUERY,
            "open": {"type": "array", "items": {"type": "object", "required": ["ref_id"], "properties": {"ref_id": _REF, "lineno": {"type": "integer", "minimum": 0}}}},
            "click": {"type": "array", "items": {"type": "object", "required": ["ref_id", "id"], "properties": {"ref_id": _REF, "id": {"type": "integer", "minimum": 0}}}},
            "find": {"type": "array", "items": {"type": "object", "required": ["ref_id", "pattern"], "properties": {"ref_id": _REF, "pattern": _STRING}}},
            "screenshot": {"type": "array", "description": "Open the PDF first, then reuse its returned ref_id in a later call in the same conversation.", "items": {"type": "object", "required": ["ref_id", "pageno"], "properties": {"ref_id": _REF, "pageno": {"type": "integer", "minimum": 0}}}},
            "finance": {"type": "array", "items": {"type": "object", "required": ["ticker", "type"], "properties": {"ticker": _STRING, "type": {"type": "string", "enum": sorted(FINANCE_TYPES)}, "market": _STRING}}},
            "weather": {"type": "array", "items": {"type": "object", "required": ["location"], "properties": {"location": _STRING, "start": _STRING, "duration": {"type": "integer", "minimum": 0}}}},
            "sports": {"type": "array", "items": {"type": "object", "required": ["fn", "league"], "properties": {"fn": {"type": "string", "enum": sorted(SPORTS_FUNCTIONS)}, "league": {"type": "string", "enum": sorted(SPORTS_LEAGUES)}, "tool": {"type": "string", "enum": ["sports"]}, "team": _STRING, "opponent": _STRING, "date_from": _STRING, "date_to": _STRING, "num_games": {"type": "integer", "minimum": 0}, "locale": _STRING}}},
            "time": {"type": "array", "items": {"type": "object", "required": ["utc_offset"], "properties": {"utc_offset": _STRING}}},
            "response_length": {"type": "string", "enum": sorted(RESPONSE_LENGTHS)},
            "search_context_size": {"type": "string", "enum": sorted(CONTEXT_SIZES)},
            "allowed_domains": {"type": "array", "items": _STRING},
            "blocked_domains": {"type": "array", "items": _STRING},
            "image_settings": {"type": "object", "properties": {"max_results": {"type": "integer", "minimum": 0}, "caption": {"type": "boolean"}}},
            "external_web_access": {"oneOf": [{"type": "boolean"}, {"type": "string", "enum": sorted(ACCESS_MODES)}]},
            "user_location": {"type": "object", "properties": {"country": _STRING, "region": _STRING, "city": _STRING, "timezone": _STRING}},
            "max_output_tokens": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
}
