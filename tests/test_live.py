import json
import os
import re
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import provider  # noqa: E402


LIVE = os.getenv("CODEX_WEB_LIVE_TESTS") == "1" and provider.is_available()


@unittest.skipUnless(LIVE, "set CODEX_WEB_LIVE_TESTS=1 with Codex Web credentials")
class CodexWebLiveTests(unittest.TestCase):
    def setUp(self):
        provider.clear_session_state()

    def _request(self, params, session):
        result = {}
        for attempt in range(2):
            result = json.loads(provider.handle_codex_web(params, session_id=session))
            if result.get("success"):
                return result
            error = result.get("error") or {}
            error_type = error.get("type") if isinstance(error, dict) else None
            if error_type not in {"upstream", "timeout"} or attempt == 1:
                return result
        return result

    def _call(self, params, session):
        result = self._request(params, session)
        if not result.get("success"):
            error = result.get("error") or {}
            error_type = error.get("type") if isinstance(error, dict) else None
            self.fail(error_type or "live request failed")
        return result

    def test_search_open_find_click(self):
        search = self._call({"search_query": [{"q": "Python official release notes"}]}, "live-chain")
        ref = next(item["ref_id"] for item in search["results"] if item.get("ref_id"))
        opened = self._call({"open": [{"ref_id": ref}]}, "live-chain")
        view_ref = next(item["ref_id"] for item in opened["results"] if item.get("ref_id"))
        self._call({"find": [{"ref_id": view_ref, "pattern": "Python"}]}, "live-chain")
        opened_for_click = opened
        output = opened_for_click.get("output", "")
        candidates = sorted({int(value) for value in re.findall(r"(?:^|\s)(\d+)[.)]\s", output)})
        if not candidates:
            opened_for_click = self._call({"open": [{"ref_id": "https://www.python.org/downloads/"}]}, "live-chain")
            view_ref = next(item["ref_id"] for item in opened_for_click["results"] if item.get("ref_id"))
            output = opened_for_click.get("output", "")
            candidates = sorted({int(value) for value in re.findall(r"(?:^|\s)(\d+)[.)]\s", output)})
        if not candidates:
            self.skipTest("opened endpoint output exposed no numbered links")
        failures = []
        for link_id in candidates:
            result = self._request({"click": [{"ref_id": view_ref, "id": link_id}]}, "live-chain")
            if result.get("success"):
                return
            error = result.get("error") or {}
            error_type = error.get("type") if isinstance(error, dict) else None
            failures.append(f"{link_id}:{error_type or 'unknown'}")
        self.fail(f"no numbered link exposed by the opened page was clickable ({', '.join(failures)})")

    def test_pdf_open_screenshot_zero_based(self):
        opened = self._call({"open": [{"ref_id": "https://arxiv.org/pdf/1706.03762.pdf"}]}, "live-pdf")
        ref = next(item["ref_id"] for item in opened["results"] if item.get("ref_id"))
        self._call({"screenshot": [{"ref_id": ref, "pageno": 0}]}, "live-pdf")

    def test_image_finance_weather_sports_time(self):
        for name, params in (
            ("image", {"image_query": [{"q": "Python logo"}]}),
            ("finance", {"finance": [{"ticker": "NVDA", "type": "equity", "market": "USA"}]}),
            ("weather", {"weather": [{"location": "Ho Chi Minh City", "duration": 1}]}),
            ("sports", {"sports": [{"fn": "schedule", "league": "nba"}]}),
            ("time", {"time": [{"utc_offset": "+07:00"}]}),
        ):
            with self.subTest(name=name):
                self._call(params, f"live-{name}")

    def test_encrypted_output_is_retained_but_not_needed_on_follow_up(self):
        first = self._call({"search_query": [{"q": "Python official website"}]}, "live-encrypted")
        ref = next(item["ref_id"] for item in first["results"] if item.get("ref_id"))
        self.assertNotIn("encrypted_output", first)
        self.assertIsInstance(provider._sessions["live-encrypted"].encrypted_output, str)
        self._call({"open": [{"ref_id": ref}]}, "live-encrypted")

    def test_concurrent_sessions_do_not_cross_references(self):
        def search():
            result = self._call({"search_query": [{"q": "official Python site"}]}, "live-a")
            return next(item["ref_id"] for item in result["results"] if item.get("ref_id"))

        def specialized():
            self._call({"time": [{"utc_offset": "+07:00"}]}, "live-b")

        with ThreadPoolExecutor(max_workers=2) as executor:
            ref_a, _ = executor.map(lambda fn: fn(), (search, specialized))
        crossed = json.loads(provider.handle_codex_web({"open": [{"ref_id": ref_a}]}, session_id="live-b"))
        self.assertEqual(crossed["error"]["type"], "reference_expired")


if __name__ == "__main__":
    unittest.main()
