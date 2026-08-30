"""Real Hermes runtime probe used by test_hermes_e2e."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "hermes"
        shutil.copytree(REPO, home / "plugins" / "codex-web")
        (home / "logs").mkdir()
        (home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - codex-web\n",
            encoding="utf-8",
        )
        bundled = root / "bundled"
        bundled.mkdir()
        os.environ.update({
            "HERMES_HOME": str(home),
            "HERMES_BUNDLED_PLUGINS": str(bundled),
            "CODEX_WEB_BASE_URL": "https://gateway.example/v1",
            "CODEX_WEB_API_KEY": "test-only-key",
        })

        from hermes_cli.plugins import PluginManager

        manager = PluginManager()
        manager.discover_and_load()
        loaded = manager._plugins["codex-web"]
        assert loaded.enabled and loaded.error is None
        assert manager.find_plugin_skill("codex-web:codex-web-research") is not None

        class Response:
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit=None):
                return json.dumps(self.payload).encode("utf-8")

        responses = iter([
            {"output": "search", "results": [{"ref_id": "turn0search0", "url": "https://example.com"}]},
            {"output": "open", "results": [{"ref_id": "turn0view0", "url": "https://example.com"}]},
            {"output": "find", "results": []},
        ])
        request_ids: list[str] = []

        def fake_urlopen(request, **kwargs):
            request_ids.append(json.loads(request.data)["id"])
            return Response(next(responses))

        provider_module = None
        original_open_request = None
        try:
            from run_agent import AIAgent

            agent = AIAgent(
                base_url="https://model.example/v1",
                api_key="test-model-key",
                provider="openai",
                model="gpt-5.4-mini",
                quiet_mode=True,
                skip_memory=True,
                skip_background_review=True,
                skip_context_files=True,
                platform="cli",
            )
            provider_module = next(
                module for module in sys.modules.values()
                if getattr(module, "handle_codex_web", None) is not None
                and hasattr(module, "_open_request")
            )
            original_open_request = provider_module._open_request
            setattr(provider_module, "_open_request", fake_urlopen)
            from tools.registry import registry

            entry = registry.get_entry("codex_web")
            assert entry is not None
            original_handler = entry.handler
            received_session_ids: list[str] = []

            def recording_handler(params, **kwargs):
                received_session_ids.append(kwargs.get("session_id", ""))
                return original_handler(params, **kwargs)

            entry.handler = recording_handler
            messages: list[dict] = []

            def call(number: int, args: dict) -> dict:
                function = SimpleNamespace(name="codex_web", arguments=json.dumps(args))
                tool_call = SimpleNamespace(id=f"call-{number}", function=function)
                agent._execute_tool_calls_sequential(
                    SimpleNamespace(tool_calls=[tool_call]), messages, "e2e"
                )
                return json.loads(messages[-1]["content"])

            assert call(1, {"search_query": [{"q": "x"}]})["success"]
            assert call(2, {"open": [{"ref_id": "turn0search0"}]})["success"]
            assert call(3, {"find": [{"ref_id": "turn0view0", "pattern": "x"}]})["success"]
            assert len(set(request_ids)) == 1, request_ids
            assert received_session_ids == [agent.session_id] * 3
            assert "e2e" not in received_session_ids
        finally:
            if "entry" in locals() and "original_handler" in locals():
                entry.handler = original_handler
            if provider_module is not None and original_open_request is not None:
                setattr(provider_module, "_open_request", original_open_request)


if __name__ == "__main__":
    main()
