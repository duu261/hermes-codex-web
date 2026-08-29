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


agent = types.ModuleType("agent")
base = types.ModuleType("agent.web_search_provider")


class WebSearchProvider:
    pass


base.WebSearchProvider = WebSearchProvider
setattr(base, "get_provider_env", lambda name: os.getenv(name, "").strip())
sys.modules.setdefault("agent", agent)
sys.modules["agent.web_search_provider"] = base
hermes_cli = types.ModuleType("hermes_cli")
hermes_config = types.ModuleType("hermes_cli.config")
setattr(hermes_config, "get_env_value", lambda name: os.getenv(name, ""))
setattr(hermes_config, "load_config", lambda: {})
sys.modules["hermes_cli"] = hermes_cli
sys.modules["hermes_cli.config"] = hermes_config
spec = importlib.util.spec_from_file_location("codex_web_provider", ROOT / "provider.py")
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class CodexWebTests(unittest.TestCase):
    def setUp(self):
        with provider._session_request_ids_lock:
            provider._session_request_ids.clear()

    def test_builds_full_public_command_surface(self):
        payload = provider.build_codex_web_payload(
            {
                "context": "Research this",
                "search_query": [{"q": "Hermes", "recency": 7, "domains": ["nousresearch.com"]}],
                "image_query": [{"q": "Hermes logo"}],
                "open": [{"ref_id": "turn0search0", "lineno": 12}],
                "click": [{"ref_id": "turn0search0", "id": 3}],
                "find": [{"ref_id": "turn0search0", "pattern": "plugin"}],
                "screenshot": [{"ref_id": "turn0search0", "pageno": 0}],
                "finance": [{"ticker": "NVDA", "type": "equity", "market": "USA"}],
                "weather": [{"location": "Vietnam, Ho Chi Minh City", "duration": 3}],
                "sports": [{"fn": "schedule", "league": "nba", "team": "LAL", "num_games": 2}],
                "time": [{"utc_offset": "+07:00"}],
                "response_length": "long",
                "search_context_size": "high",
                "allowed_domains": ["nousresearch.com"],
                "blocked_domains": ["spam.example"],
                "image_settings": {"max_results": 4, "caption": True},
                "external_web_access": "live",
                "max_output_tokens": 2500,
            },
            model="gpt-test",
            request_id="search-1",
        )

        self.assertEqual(payload["id"], "search-1")
        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(payload["input"], "Research this")
        self.assertEqual(payload["commands"]["search_query"][0]["domains"], ["nousresearch.com"])
        self.assertEqual(payload["commands"]["click"], [{"ref_id": "turn0search0", "id": 3}])
        self.assertEqual(payload["commands"]["finance"][0]["type"], "equity")
        self.assertEqual(payload["commands"]["sports"][0]["fn"], "schedule")
        self.assertEqual(payload["commands"]["sports"][0]["tool"], "sports")
        self.assertEqual(payload["commands"]["response_length"], "long")
        self.assertEqual(payload["settings"]["search_context_size"], "high")
        self.assertEqual(payload["settings"]["external_web_access"], "live")
        self.assertEqual(payload["settings"]["filters"], {
            "allowed_domains": ["nousresearch.com"],
            "blocked_domains": ["spam.example"],
        })
        self.assertEqual(payload["max_output_tokens"], 2500)

    def test_builds_codex_input_and_reasoning_fields(self):
        input_items = [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Inspect this page"}],
        }]
        payload = provider.build_codex_web_payload(
            {
                "input": input_items,
                "reasoning": {
                    "effort": "high",
                    "summary": "auto",
                    "context": "current_turn",
                },
                "open": [{"ref_id": "https://example.com"}],
            },
            model="gpt-test",
        )

        self.assertEqual(payload["input"], input_items)
        self.assertEqual(payload["reasoning"], {
            "effort": "high",
            "summary": "auto",
            "context": "current_turn",
        })

    def test_rejects_empty_command_request(self):
        with self.assertRaises(ValueError):
            provider.build_codex_web_payload({}, model="gpt-test")

    def test_requires_screenshot_page_and_valid_sports_tool(self):
        with self.assertRaisesRegex(ValueError, "screenshot\[0\].pageno"):
            provider.build_codex_web_payload({
                "screenshot": [{"ref_id": "turn0search0"}],
            }, model="gpt-test")
        with self.assertRaisesRegex(ValueError, "sports\[0\].tool"):
            provider.build_codex_web_payload({
                "sports": [{"fn": "schedule", "league": "nba", "tool": "other"}],
            }, model="gpt-test")

    def test_enforces_search_query_batch_rules(self):
        queries = [{"q": f"query {index}"} for index in range(5)]
        with self.assertRaisesRegex(ValueError, "at most 4"):
            provider.build_codex_web_payload({"search_query": queries}, model="gpt-test")
        four = queries[:4]
        with self.assertRaisesRegex(ValueError, "response_length"):
            provider.build_codex_web_payload({"search_query": four}, model="gpt-test")
        with self.assertRaisesRegex(ValueError, "response_length"):
            provider.build_codex_web_payload({"search_query": four, "response_length": "short"}, model="gpt-test")
        payload = provider.build_codex_web_payload({
            "search_query": four,
            "response_length": "medium",
        }, model="gpt-test")
        self.assertEqual(len(payload["commands"]["search_query"]), 4)

    def test_allows_empty_crypto_market(self):
        payload = provider.build_codex_web_payload({
            "finance": [{"ticker": "BTC", "type": "crypto", "market": ""}],
        }, model="gpt-test")
        self.assertEqual(payload["commands"]["finance"][0]["market"], "")

    def test_handler_reuses_request_id_for_hermes_session(self):
        responses = [
            FakeResponse({"output": "Opened PDF", "results": [{"ref_id": "turn0view0"}]}),
            FakeResponse({"output": "Screenshot result", "results": []}),
        ]
        with patch.dict("os.environ", {
            "CODEX_WEB_BASE_URL": "https://gateway.example/v1",
            "CODEX_WEB_API_KEY": "secret",
        }, clear=False), patch.object(
            provider.urllib.request,
            "urlopen",
            side_effect=responses,
        ) as open_url:
            provider.handle_codex_web(
                {"open": [{"ref_id": "https://example.com/document.pdf"}]},
                session_id="hermes-session",
            )
            provider.handle_codex_web(
                {"screenshot": [{"ref_id": "turn0view0", "pageno": 0}]},
                session_id="hermes-session",
            )

        request_ids = [json.loads(call.args[0].data)["id"] for call in open_url.call_args_list]
        self.assertEqual(request_ids[0], request_ids[1])


    def test_session_request_id_is_isolated_and_concurrent(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            request_ids = list(executor.map(
                provider._session_request_id,
                ["same-session"] * 32,
            ))

        self.assertEqual(len(set(request_ids)), 1)
        self.assertNotEqual(
            provider._session_request_id("other-session"),
            request_ids[0],
        )

    def test_session_request_id_cache_is_bounded(self):
        for index in range(provider._SESSION_REQUEST_ID_LIMIT + 1):
            provider._session_request_id(f"session-{index}")

        with provider._session_request_ids_lock:
            self.assertEqual(
                len(provider._session_request_ids),
                provider._SESSION_REQUEST_ID_LIMIT,
            )
            self.assertNotIn("session-0", provider._session_request_ids)

    def test_handler_defaults_missing_results_to_empty_list(self):
        with patch.dict("os.environ", {
            "CODEX_WEB_BASE_URL": "https://gateway.example/v1",
            "CODEX_WEB_API_KEY": "secret",
        }, clear=False), patch.object(
            provider.urllib.request,
            "urlopen",
            return_value=FakeResponse({"output": "text only"}),
        ):
            result = json.loads(provider.handle_codex_web({
                "time": [{"utc_offset": "+07:00"}],
            }))
        self.assertEqual(result["success"], True)
        self.assertEqual(result["results"], [])

    def test_handler_rejects_missing_or_non_string_output(self):
        with patch.dict("os.environ", {
            "CODEX_WEB_BASE_URL": "https://gateway.example/v1",
            "CODEX_WEB_API_KEY": "secret",
        }, clear=False):
            for response_payload in ({"results": []}, {"output": None, "results": []}):
                with self.subTest(response_payload=response_payload), patch.object(
                    provider.urllib.request,
                    "urlopen",
                    return_value=FakeResponse(response_payload),
                ):
                    result = json.loads(provider.handle_codex_web({
                        "search_query": [{"q": "test"}],
                    }))
                self.assertEqual(result, {
                    "success": False,
                    "error": "Codex Web returned invalid output",
                })

    def test_handler_rejects_embedded_endpoint_errors(self):
        with patch.dict("os.environ", {
            "CODEX_WEB_BASE_URL": "https://gateway.example/v1",
            "CODEX_WEB_API_KEY": "secret",
        }, clear=False), patch.object(
            provider.urllib.request,
            "urlopen",
            return_value=FakeResponse({
                "output": "Error parsing function call: invalid command",
                "results": [],
            }),
        ):
            result = json.loads(provider.handle_codex_web({
                "sports": [{"fn": "schedule", "league": "nba"}],
            }))
        self.assertEqual(result, {
            "success": False,
            "error": "Codex Web returned an endpoint error",
        })

    def test_schema_restricts_sports_tool_value_and_declares_url(self):
        sports_tool = provider.CODEX_WEB_SCHEMA["parameters"]["properties"]["sports"]["items"]["properties"]["tool"]
        self.assertEqual(sports_tool["enum"], ["sports"])
        manifest = (ROOT / "plugin.yaml").read_text()
        self.assertIn("CODEX_WEB_BASE_URL", manifest)
        self.assertIn("CODEX_WEB_API_KEY", manifest)

    def test_handler_preserves_structured_results_and_output(self):
        response = FakeResponse({
            "output": "Opened page content",
            "results": [{
                "type": "text_result",
                "ref_id": "turn0search0",
                "url": "https://example.com",
                "title": "Example",
            }],
        })
        with patch.dict("os.environ", {
            "CODEX_WEB_BASE_URL": "https://gateway.example/v1",
            "CODEX_WEB_API_KEY": "secret",
        }, clear=False), patch.object(provider.urllib.request, "urlopen", return_value=response) as open_url:
            result = json.loads(provider.handle_codex_web({
                "open": [{"ref_id": "turn0search0"}],
            }))

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.example/v1/alpha/search")
        self.assertEqual(request.get_header("Openai-beta"), "responses=experimental")
        self.assertEqual(request.get_header("Originator"), "codex_cli_rs")
        self.assertEqual(result["success"], True)
        self.assertEqual(result["output"], "Opened page content")
        self.assertEqual(result["results"][0]["ref_id"], "turn0search0")

    def test_handler_preserves_encrypted_output_and_optional_results(self):
        response = FakeResponse({
            "encrypted_output": "opaque",
            "output": "Specialized result",
        })
        with patch.dict("os.environ", {
            "CODEX_WEB_BASE_URL": "https://gateway.example/v1",
            "CODEX_WEB_API_KEY": "secret",
        }, clear=False), patch.object(provider.urllib.request, "urlopen", return_value=response):
            result = json.loads(provider.handle_codex_web({
                "time": [{"utc_offset": "+07:00"}],
            }))

        self.assertEqual(result["encrypted_output"], "opaque")
        self.assertEqual(result["results"], [])

    def test_schema_exposes_all_endpoint_commands(self):
        properties = provider.CODEX_WEB_SCHEMA["parameters"]["properties"]
        expected = {
            "context", "input", "reasoning", "search_query", "image_query", "open", "click", "find",
            "screenshot", "finance", "weather", "sports", "time", "response_length",
            "search_context_size", "allowed_domains", "blocked_domains", "image_settings",
            "external_web_access", "max_output_tokens", "user_location",
        }
        self.assertEqual(set(properties), expected)


if __name__ == "__main__":
    unittest.main()
