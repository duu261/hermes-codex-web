import http.client
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
hermes_cli = types.ModuleType("hermes_cli")
hermes_config = types.ModuleType("hermes_cli.config")
setattr(hermes_config, "get_env_value", lambda name: os.getenv(name, ""))
setattr(hermes_config, "load_config", lambda: {})
setattr(hermes_config, "load_config_readonly", lambda: {})
sys.modules["hermes_cli"] = hermes_cli
sys.modules["hermes_cli.config"] = hermes_config
spec = importlib.util.spec_from_file_location("codex_web_provider", ROOT / "provider.py")
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)


class FakeResponse:
    def __init__(self, payload=None, *, body=None, headers=None):
        self.payload = payload
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=None):
        data = self.body if self.body is not None else json.dumps(self.payload).encode()
        return data if limit is None else data[:limit]


class CodexWebTests(unittest.TestCase):
    def setUp(self):
        provider.clear_session_state()

    def _env(self):
        return patch.dict(os.environ, {
            "CODEX_WEB_BASE_URL": "https://gateway.example/v1",
            "CODEX_WEB_API_KEY": "secret",
        }, clear=False)

    def test_invalid_configuration_is_rejected_without_exposing_values(self):
        cases = [
            ("CODEX_WEB_API_KEY", "fixture\nAUDIT_MARKER"),
            ("CODEX_WEB_API_KEY", "fixture\rAUDIT_MARKER"),
            ("CODEX_WEB_API_KEY", "fixture\x7fAUDIT_MARKER"),
            ("CODEX_WEB_BASE_URL", "https://user:AUDIT_MARKER@gateway.example/v1"),
            ("CODEX_WEB_BASE_URL", "https://gateway.example:AUDIT_MARKER/v1"),
            ("CODEX_WEB_BASE_URL", "https://[AUDIT_MARKER/v1"),
            ("CODEX_WEB_BASE_URL", "https://gateway.example/bad path/AUDIT_MARKER"),
            ("CODEX_WEB_BASE_URL", "https://gateway.example/\nAUDIT_MARKER"),
        ]
        for field, value in cases:
            with self.subTest(field=field, case=cases.index((field, value))):
                env = {"CODEX_WEB_BASE_URL": "https://gateway.example/v1", "CODEX_WEB_API_KEY": "fixture"}
                env[field] = value
                with patch.dict(os.environ, env, clear=True), patch.object(provider, "_open_request", return_value=FakeResponse({"output": "fixture", "results": []})) as transport:
                    try:
                        result = json.loads(provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]}))
                    except (ValueError, http.client.InvalidURL) as exc:
                        self.fail(f"configuration escaped as {type(exc).__name__}")
                self.assertFalse(result["success"])
                self.assertEqual(result["error"]["type"], "configuration")
                self.assertNotIn("AUDIT_MARKER", json.dumps(result))
                transport.assert_not_called()

    def test_request_errors_do_not_expose_configuration_values(self):
        errors = [ValueError("AUDIT_MARKER"), http.client.InvalidURL("AUDIT_MARKER"),
                  UnicodeEncodeError("latin-1", "AUDIT_MARKER", 0, 1, "fixture")]
        for stage in ("Request", "_open_request"):
            owner = provider.urllib.request if stage == "Request" else provider
            for error in errors:
                with self.subTest(stage=stage, error=type(error).__name__):
                    with self._env(), patch.object(owner, stage, side_effect=error) as fault:
                        try:
                            result = json.loads(provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]}))
                        except (ValueError, http.client.InvalidURL) as exc:
                            self.fail(f"request error escaped as {type(exc).__name__}")
                    self.assertFalse(result["success"])
                    self.assertEqual(result["error"]["type"], "configuration")
                    self.assertNotIn("AUDIT_MARKER", json.dumps(result))
                    fault.assert_called_once()

    def test_schema_matches_public_surface_exactly(self):
        properties = provider.CODEX_WEB_SCHEMA["parameters"]["properties"]
        self.assertEqual(set(properties), {
            "context", "search_query", "image_query", "open", "click", "find",
            "screenshot", "finance", "weather", "sports", "time", "response_length",
            "search_context_size", "allowed_domains", "blocked_domains", "image_settings",
            "external_web_access", "user_location", "max_output_tokens",
        })
        self.assertFalse(provider.CODEX_WEB_SCHEMA["parameters"]["additionalProperties"])
        self.assertEqual(properties["search_query"]["maxItems"], 4)
        self.assertNotIn("tool", properties["sports"]["items"]["properties"])
        self.assertEqual(properties["search_context_size"]["enum"], sorted(provider.CONTEXT_SIZES))
        self.assertEqual(properties["external_web_access"]["oneOf"][1]["enum"], sorted(provider.ACCESS_MODES))
        self.assertEqual(properties["image_settings"]["minProperties"], 1)
        self.assertEqual(properties["max_output_tokens"]["minimum"], 1)
        self.assertNotIn("input", properties)
        self.assertNotIn("reasoning", properties)

    def test_schema_warns_agents_about_endpoint_dependent_controls(self):
        properties = provider.CODEX_WEB_SCHEMA["parameters"]["properties"]
        self.assertIn("advisory", properties["allowed_domains"]["description"].lower())
        self.assertIn("verify returned sources", properties["search_query"]["items"]["properties"]["domains"]["description"].lower())
        self.assertIn("not a security boundary", properties["blocked_domains"]["description"].lower())
        self.assertIn("not a network or privacy control", properties["external_web_access"]["description"].lower())
        self.assertIn("sent to", properties["user_location"]["description"].lower())
        self.assertIn("endpoint-dependent", properties["context"]["description"].lower())
        self.assertIn("endpoint-dependent", properties["search_context_size"]["description"].lower())
        self.assertIn("endpoint-dependent", properties["image_settings"]["description"].lower())

    def test_builds_all_command_payloads(self):
        payload = provider.build_codex_web_payload({
            "search_query": [{"q": "Hermes", "recency": 7, "domains": ["nousresearch.com"]}],
            "image_query": [{"q": "Hermes logo"}],
            "open": [{"ref_id": "https://example.com", "lineno": 12}],
            "click": [{"ref_id": "turn0search0", "id": 3}],
            "find": [{"ref_id": "turn0search0", "pattern": "plugin"}],
            "screenshot": [{"ref_id": "turn0view0", "pageno": 0}],
            "finance": [{"ticker": "NVDA", "type": "equity", "market": "USA"}],
            "weather": [{"location": "Ho Chi Minh City", "duration": 3}],
            "sports": [{"fn": "schedule", "league": "nba", "team": "LAL", "num_games": 2}],
            "time": [{"utc_offset": "+07:00"}],
            "response_length": "long",
        }, model="gpt-test", request_id="search-1")
        self.assertEqual(payload["id"], "search-1")
        self.assertEqual(payload["commands"]["sports"][0]["tool"], "sports")
        self.assertEqual(payload["commands"]["screenshot"][0]["pageno"], 0)
        self.assertEqual(payload["commands"]["open"][0]["ref_id"], "https://example.com")

    def test_advanced_fields_build_payload_and_internal_fields_stay_hidden(self):
        payload = provider.build_codex_web_payload({
            "search_query": [{"q": "Hermes"}],
            "context": "focused context",
            "reasoning": {"effort": "medium"},
            "search_context_size": "high",
            "allowed_domains": ["example.com"],
            "blocked_domains": ["spam.example"],
            "image_settings": {"caption": True},
            "external_web_access": "live",
            "user_location": {"city": "HCMC"},
            "max_output_tokens": 123,
        }, model="gpt-test")
        self.assertEqual(payload["input"], "focused context")
        self.assertEqual(payload["settings"]["search_context_size"], "high")
        self.assertEqual(payload["settings"]["filters"]["allowed_domains"], ["example.com"])
        self.assertEqual(payload["settings"]["filters"]["blocked_domains"], ["spam.example"])
        self.assertEqual(payload["settings"]["image_settings"], {"caption": True})
        self.assertEqual(payload["settings"]["external_web_access"], "live")
        self.assertEqual(payload["settings"]["user_location"], {"type": "approximate", "city": "HCMC"})
        self.assertEqual(payload["max_output_tokens"], 123)
        properties = provider.CODEX_WEB_SCHEMA["parameters"]["properties"]
        self.assertIn("context", properties)
        self.assertIn("max_output_tokens", properties)
        self.assertNotIn("input", properties)
        self.assertNotIn("reasoning", properties)

    def test_strict_differences_from_native_behavior_are_documented_by_tests(self):
        with self.assertRaisesRegex(ValueError, "unknown field"):
            provider.build_codex_web_payload({"search_query": [{"q": "x", "native_extra": True}]}, model="x")
        with self.assertRaisesRegex(ValueError, "unknown parameter"):
            provider.build_codex_web_payload({"search_query": [{"q": "x"}], "native_extra": True}, model="x")
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            provider.build_codex_web_payload({"search_query": [{"q": ""}]}, model="x")
        with self.assertRaisesRegex(ValueError, "case-sensitive"):
            provider.build_codex_web_payload({"time": [{"utc_offset": "+07:00"}], "response_length": "SHORT"}, model="x")
        self.assertEqual(
            provider.build_codex_web_payload({"search_query": [{"q": "x", "domains": []}]}, model="x")["commands"]["search_query"][0]["domains"],
            [],
        )
        for command, item in (
            ("image_query", {"q": "x", "extra": True}),
            ("open", {"ref_id": "https://example.com", "extra": True}),
            ("click", {"ref_id": "turn0view0", "id": 1, "extra": True}),
            ("find", {"ref_id": "turn0view0", "pattern": "x", "extra": True}),
            ("screenshot", {"ref_id": "turn0view0", "pageno": 0, "extra": True}),
            ("finance", {"ticker": "NVDA", "type": "equity", "extra": True}),
            ("weather", {"location": "HCMC", "extra": True}),
            ("sports", {"fn": "schedule", "league": "nba", "extra": True}),
            ("time", {"utc_offset": "+07:00", "extra": True}),
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "unknown field"):
                    provider.build_codex_web_payload({command: [item]}, model="x")

    def test_integer_fields_use_unsigned_64_bit_bounds(self):
        too_large = 2**64
        for command, item, field in (
            ("search_query", {"q": "x", "recency": too_large}, "recency"),
            ("open", {"ref_id": "https://example.com", "lineno": too_large}, "lineno"),
            ("click", {"ref_id": "https://example.com", "id": too_large}, "id"),
            ("screenshot", {"ref_id": "https://example.com/a.pdf", "pageno": too_large}, "pageno"),
            ("weather", {"location": "HCMC", "duration": too_large}, "duration"),
            ("sports", {"fn": "schedule", "league": "nba", "num_games": too_large}, "num_games"),
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "at most 18446744073709551615"):
                    provider.build_codex_web_payload({command: [item]}, model="x")

    def test_schema_declares_unsigned_64_bit_maximums(self):
        properties = provider.CODEX_WEB_SCHEMA["parameters"]["properties"]
        self.assertEqual(properties["search_query"]["items"]["properties"]["recency"]["maximum"], provider.UINT64_MAX)
        self.assertEqual(properties["open"]["items"]["properties"]["lineno"]["maximum"], provider.UINT64_MAX)
        self.assertEqual(properties["click"]["items"]["properties"]["id"]["maximum"], provider.UINT64_MAX)
        self.assertEqual(properties["screenshot"]["items"]["properties"]["pageno"]["maximum"], provider.UINT64_MAX)
        self.assertEqual(properties["weather"]["items"]["properties"]["duration"]["maximum"], provider.UINT64_MAX)
        self.assertEqual(properties["sports"]["items"]["properties"]["num_games"]["maximum"], provider.UINT64_MAX)

    def test_advanced_nested_controls_are_strict_too(self):
        for field, value in (
            ("image_settings", {"caption": True, "extra": True}),
            ("user_location", {"city": "HCMC", "extra": True}),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "unknown field"):
                    provider.build_codex_web_payload({"time": [{"utc_offset": "+07:00"}], field: value}, model="x")

    def test_enforces_four_query_and_response_length_rules(self):
        queries = [{"q": str(i)} for i in range(5)]
        with self.assertRaisesRegex(ValueError, "at most 4"):
            provider.build_codex_web_payload({"search_query": queries}, model="x")
        with self.assertRaisesRegex(ValueError, "medium or long"):
            provider.build_codex_web_payload({"search_query": queries[:4]}, model="x")
        with self.assertRaisesRegex(ValueError, "medium or long"):
            provider.build_codex_web_payload({"search_query": queries[:4], "response_length": "short"}, model="x")
        provider.build_codex_web_payload({"search_query": queries[:4], "response_length": "medium"}, model="x")

    def test_validates_sports_injection_and_zero_based_screenshot(self):
        with self.assertRaisesRegex(ValueError, "sports\[0\].tool"):
            provider.build_codex_web_payload({"sports": [{"fn": "schedule", "league": "nba", "tool": "other"}]}, model="x")
        payload = provider.build_codex_web_payload({"screenshot": [{"ref_id": "https://example.com/a.pdf", "pageno": 0}]}, model="x")
        self.assertEqual(payload["commands"]["screenshot"][0]["pageno"], 0)

    def test_state_is_isolated_thread_safe_bounded_and_ttl_evicted(self):
        with patch.object(provider, "_config", return_value={"max_sessions": 4}):
            with ThreadPoolExecutor(max_workers=8) as executor:
                ids = list(executor.map(provider._session_request_id, ["same"] * 32))
            self.assertEqual(len(set(ids)), 1)
            self.assertNotEqual(provider._session_request_id("other"), ids[0])
            for i in range(8):
                provider._session_request_id(f"session-{i}")
            self.assertLessEqual(len(provider._sessions), 4)

        clock = [100.0]
        with patch.object(provider.time, "monotonic", side_effect=lambda: clock[0]), patch.object(provider, "_config", return_value={"session_ttl_seconds": 2}):
            request_id = provider._session_request_id("ttl")
            clock[0] += 3
            self.assertIsNone(provider._session_state("ttl", create=False))
            self.assertNotEqual(provider._session_request_id("ttl"), request_id)

    def test_reference_store_is_bounded_by_count_and_length(self):
        state = provider._session_state("refs", create=True)
        result = {
            "success": True,
            "results": [{"ref_id": ref} for ref in ("one", "two", "three", "four", "too-long")],
        }
        with patch.object(provider, "_config", return_value={"max_refs_per_session": 3, "max_ref_length": 5}):
            provider._record_refs(state, result)
        self.assertEqual(list(state.refs), ["two", "three", "four"])

    def test_handler_reuses_request_id_and_records_refs_for_continuation(self):
        responses = [
            FakeResponse({"output": "Search", "results": [{"ref_id": "turn0search0", "url": "https://example.com"}]}),
            FakeResponse({"output": "Opened", "results": [{"ref_id": "turn0view0", "url": "https://example.com"}]}),
            FakeResponse({"output": "Found", "results": []}),
        ]
        with self._env(), patch.object(provider, "_open_request", side_effect=responses) as urlopen:
            self.assertTrue(json.loads(provider.handle_codex_web({"search_query": [{"q": "x"}]}, session_id="hermes"))["success"])
            self.assertTrue(json.loads(provider.handle_codex_web({"open": [{"ref_id": "turn0search0"}]}, session_id="hermes"))["success"])
            self.assertTrue(json.loads(provider.handle_codex_web({"find": [{"ref_id": "turn0view0", "pattern": "x"}]}, session_id="hermes"))["success"])
        request_ids = [json.loads(call.args[0].data)["id"] for call in urlopen.call_args_list]
        self.assertEqual(len(set(request_ids)), 1)

    def test_reference_without_session_or_after_expiry_is_clear_error(self):
        with self._env(), patch.object(provider, "_open_request") as urlopen:
            result = json.loads(provider.handle_codex_web({"find": [{"ref_id": "turn0search0", "pattern": "x"}]}))
        self.assertEqual(result["error"]["type"], "reference_expired")
        urlopen.assert_not_called()

    def test_screenshot_requires_an_opened_reference(self):
        with self._env(), patch.object(provider, "_open_request") as open_request:
            result = json.loads(provider.handle_codex_web({
                "screenshot": [{"ref_id": "https://example.com/file.pdf", "pageno": 0}],
            }, session_id="s"))
        self.assertEqual(result["error"]["type"], "validation")
        self.assertIn("open the PDF first", result["error"]["message"])
        open_request.assert_not_called()

    def test_screenshot_rejects_search_and_non_pdf_references(self):
        responses = [
            FakeResponse({"output": "Search", "results": [{"ref_id": "turn0search0", "url": "https://example.com"}]}),
            FakeResponse({"output": "Page", "results": [{"ref_id": "turn0view0", "url": "https://example.com"}]}),
            FakeResponse({"output": "Page", "results": [{"ref_id": "turn0view0", "url": "https://example.com"}]}),
        ]
        with self._env(), patch.object(provider, "_open_request", side_effect=responses):
            provider.handle_codex_web({"search_query": [{"q": "x"}]}, session_id="s")
            result = json.loads(provider.handle_codex_web({"screenshot": [{"ref_id": "turn0search0", "pageno": 0}]}, session_id="s"))
            provider.handle_codex_web({"open": [{"ref_id": "turn0search0"}]}, session_id="s")
            result_non_pdf = json.loads(provider.handle_codex_web({"screenshot": [{"ref_id": "turn0view0", "pageno": 0}]}, session_id="s"))
        self.assertEqual(result["error"]["type"], "validation")
        self.assertEqual(result_non_pdf["error"]["type"], "validation")

    def test_normalizes_sources_from_endpoint_results_and_keeps_unknown_fields(self):
        response = FakeResponse({"output": "Opened", "results": [{
            "type": "text_result", "ref_id": "turn0search0", "title": "Example",
            "url": "https://example.com", "future_field": {"keep": True},
        }]})
        with self._env(), patch.object(provider, "_open_request", return_value=response):
            result = json.loads(provider.handle_codex_web({"open": [{"ref_id": "https://example.com"}]}))
        self.assertEqual(result["sources"], [{"ref_id": "turn0search0", "title": "Example", "url": "https://example.com", "type": "web"}])
        self.assertEqual(result["results"][0]["future_field"], {"keep": True})

    def test_retries_retry_after_and_classifies_http_errors(self):
        from urllib.error import HTTPError
        retry = HTTPError("https://gateway.example/v1/alpha/search", 429, "busy", {"Retry-After": "7", "X-Request-ID": "req-1"}, io.BytesIO())
        response = FakeResponse({"output": "ok", "results": []})
        with self._env(), patch.object(provider, "_open_request", side_effect=[retry, response]), patch.object(provider.time, "sleep") as sleep, patch.object(provider.random, "uniform", return_value=0.0):
            result = json.loads(provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]}))
        self.assertTrue(result["success"])
        sleep.assert_called_once_with(7.0)

        unauthorized = HTTPError("https://gateway.example/v1/alpha/search", 401, "unauthorized", {"X-Request-ID": "req-2"}, io.BytesIO(json.dumps({"error": {"message": "Bearer sk-secret https://prod.example/private"}}).encode()))
        with self._env(), patch.object(provider, "_open_request", side_effect=unauthorized):
            result = json.loads(provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]}))
        self.assertEqual(result["error"]["type"], "authentication")
        self.assertEqual(result["error"]["request_id"], "req-2")
        self.assertNotIn("sk-secret", json.dumps(result))
        self.assertNotIn("prod.example", json.dumps(result))

        capped = HTTPError("https://gateway.example/v1/alpha/search", 503, "busy", {"Retry-After": "999999999"}, io.BytesIO())
        with self._env(), patch.object(provider, "_open_request", side_effect=[capped, response]), patch.object(provider.time, "sleep") as sleep, patch.object(provider.random, "uniform", return_value=0.0):
            provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]})
        self.assertEqual(sleep.call_args.args[0], provider.DEFAULT_MAX_RETRY_AFTER_SECONDS)
        self.assertEqual(provider._classify_status(402), "quota")
        self.assertEqual(provider._classify_status(403), "authentication")

    def test_redirects_are_refused_and_opaque_secrets_are_not_echoed(self):
        self.assertIsNone(provider._NoRedirectHandler().redirect_request(None, None, 302, "redirect", {}, "https://other.example"))

    def test_timeout_malformed_json_and_response_size_are_distinct(self):
        with self._env(), patch.object(provider, "_open_request", side_effect=TimeoutError()):
            result = json.loads(provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]}))
        self.assertEqual(result["error"]["type"], "timeout")

        calls = []
        with patch.object(provider, "_config", return_value={"request_timeout_seconds": 3}), self._env(), patch.object(provider, "_open_request", side_effect=lambda request, **kwargs: calls.append(kwargs) or FakeResponse({"output": "ok", "results": []})):
            result = json.loads(provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]}))
        self.assertTrue(result["success"])
        self.assertEqual(calls[0]["timeout"], 3.0)

        with self._env(), patch.object(provider, "_open_request", return_value=FakeResponse(body=b"not-json")):
            result = json.loads(provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]}))
        self.assertEqual(result["error"]["type"], "malformed_response")

        with patch.object(provider, "_config", return_value={"max_response_bytes": 4}), self._env(), patch.object(provider, "_open_request", return_value=FakeResponse(body=b"12345")):
            result = json.loads(provider.handle_codex_web({"time": [{"utc_offset": "+07:00"}]}))
        self.assertEqual(result["error"]["type"], "response_too_large")

    def test_encrypted_output_is_preserved_not_replayed(self):
        responses = [
            FakeResponse({"output": "Search", "encrypted_output": "opaque", "results": [{"ref_id": "turn0search0"}]}),
            FakeResponse({"output": "Open", "results": []}),
        ]
        with self._env(), patch.object(provider, "_open_request", side_effect=responses) as urlopen:
            first = json.loads(provider.handle_codex_web({"search_query": [{"q": "x"}]}, session_id="s"))
            json.loads(provider.handle_codex_web({"open": [{"ref_id": "turn0search0"}]}, session_id="s"))
        self.assertNotIn("encrypted_output", first)
        self.assertEqual(provider._sessions["s"].encrypted_output, "opaque")
        second_payload = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertNotIn("encrypted_output", second_payload)

    def test_manifest_declares_credentials_and_skill(self):
        manifest = (ROOT / "plugin.yaml").read_text()
        self.assertIn("CODEX_WEB_BASE_URL", manifest)
        self.assertIn("CODEX_WEB_API_KEY", manifest)
        self.assertTrue((ROOT / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
