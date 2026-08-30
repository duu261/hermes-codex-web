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

    def test_schema_matches_public_surface_exactly(self):
        properties = provider.CODEX_WEB_SCHEMA["parameters"]["properties"]
        self.assertEqual(set(properties), {
            "search_query", "image_query", "open", "click", "find", "screenshot",
            "finance", "weather", "sports", "time", "response_length",
        })
        self.assertFalse(provider.CODEX_WEB_SCHEMA["parameters"]["additionalProperties"])
        self.assertEqual(properties["search_query"]["maxItems"], 4)
        self.assertNotIn("tool", properties["sports"]["items"]["properties"])

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

    def test_advanced_fields_are_backward_compatible_but_hidden(self):
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
        self.assertNotIn("context", provider.CODEX_WEB_SCHEMA["parameters"]["properties"])
        self.assertNotIn("max_output_tokens", provider.CODEX_WEB_SCHEMA["parameters"]["properties"])

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

    def test_redirects_are_refused_and_opaque_secrets_are_not_echoed(self):
        self.assertIsNone(provider._NoRedirectHandler().redirect_request(None, None, 302, "redirect", {}, "https://other.example"))
        self.assertNotIn("opaque-production-secret", provider._safe_message("opaque-production-secret", "generic"))
        self.assertEqual(provider._safe_message("opaque-production-secret", "generic"), "generic")

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
        self.assertEqual(first["encrypted_output"], "opaque")
        second_payload = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertNotIn("encrypted_output", second_payload)

    def test_manifest_declares_credentials_and_skill(self):
        manifest = (ROOT / "plugin.yaml").read_text()
        self.assertIn("CODEX_WEB_BASE_URL", manifest)
        self.assertIn("CODEX_WEB_API_KEY", manifest)
        self.assertTrue((ROOT / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
