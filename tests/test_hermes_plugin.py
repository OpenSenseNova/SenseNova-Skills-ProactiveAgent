from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch


def load_plugin():
    path = (
        Path(__file__).parents[1]
        / "src"
        / "sn_proactive_agent_connectors"
        / "hermes"
        / "tui"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("sn_proactive_agent_hermes_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class HermesPluginTests(unittest.TestCase):
    def test_tui_turn_claims_source_and_posts_complete_qa(self) -> None:
        plugin = load_plugin()
        plugin._SOURCE_BY_TURN.clear()
        plugin._EVIDENCE_BY_TURN.clear()
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_urlopen(request, timeout: float):
            del timeout
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            calls.append((request.full_url, body))
            if request.full_url.endswith("/source/claim"):
                return _Response({"source_suggestion_id": "suggestion-1"})
            return _Response({"accepted": True})

        with patch.dict(os.environ, {"SN_PROACTIVE_AGENT_SERVICE_URL": "http://service.test"}, clear=False), patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            plugin.on_pre_llm_call(
                platform="tui",
                session_id="session-1",
                turn_id="turn-1",
                user_message="继续推进",
            )
            plugin.on_post_tool_call(
                platform="tui",
                session_id="session-1",
                turn_id="turn-1",
                tool_name="terminal",
                status="ok",
            )
            plugin.on_post_llm_call(
                platform="tui",
                session_id="session-1",
                turn_id="turn-1",
                user_message="继续推进",
                assistant_response="已继续完成。",
            )

        self.assertEqual(calls[0][0], "http://service.test/v1/bridge/source/claim")
        self.assertEqual(calls[1][0], "http://service.test/v1/events/turn.started")
        self.assertEqual(calls[2][0], "http://service.test/v1/events/turn.completed")
        self.assertEqual(calls[2][1]["source_suggestion_id"], "suggestion-1")
        self.assertEqual(calls[2][1]["user_question"], "继续推进")
        self.assertEqual(calls[2][1]["tool_execution_evidence"][0]["tool_name"], "terminal")

    def test_non_tui_turn_is_ignored(self) -> None:
        plugin = load_plugin()
        with patch("urllib.request.urlopen") as urlopen:
            plugin.on_pre_llm_call(platform="cli", session_id="s", turn_id="t")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
