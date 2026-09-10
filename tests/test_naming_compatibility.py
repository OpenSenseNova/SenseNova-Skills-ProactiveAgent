from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sn_proactive_agent.__main__ import _parser
from sn_proactive_agent.environment import env_value, set_compatible_env
from sn_proactive_agent.lifecycle import (
    LifecycleError, default_data_root, run_doctor, setup_harness, uninstall_harness,
)
from sn_proactive_agent_connectors.acp.targets.hermes import HermesAcpTarget
from sn_proactive_agent_connectors.hermes.classic import hermes_hook
from sn_proactive_agent_connectors.hermes import tui


class NamingCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        home = patch.object(Path, "home", return_value=self.home)
        environment = patch.dict(os.environ, {}, clear=True)
        home.start()
        environment.start()
        self.addCleanup(home.stop)
        self.addCleanup(environment.stop)

    def test_new_default_is_read_only(self):
        expected = self.home / ".sn-proactive-agent"
        self.assertEqual(default_data_root(), expected)
        self.assertEqual(_parser().parse_args(["serve"]).data_root, expected)
        self.assertFalse(expected.exists())

    def test_existing_data_is_reused_without_moving_or_rewriting(self):
        legacy = self.home / ".proactive-memory"
        legacy.mkdir()
        journal = legacy / "runtime.jsonl"
        content = b'{"event_type":"legacy-evidence"}\n'
        journal.write_bytes(content)
        self.assertEqual(default_data_root(), legacy)
        self.assertEqual(_parser().parse_args(["serve"]).data_root, legacy)
        report = run_doctor()
        self.assertIn(str(legacy), next(c.detail for c in report.checks if c.name == "data_root"))
        result = uninstall_harness("hermes", hermes_home=self.home / "hermes", dry_run=True, output=io.StringIO())
        self.assertEqual(result.data_root, legacy.resolve())
        self.assertEqual(journal.read_bytes(), content)
        self.assertFalse((self.home / ".sn-proactive-agent").exists())

    def test_existing_new_directory_wins_without_merging(self):
        for name in (".proactive-memory", ".sn-proactive-agent"):
            (self.home / name).mkdir()
        self.assertEqual(default_data_root(), self.home / ".sn-proactive-agent")

    def test_data_root_precedence_and_legacy_environment(self):
        os.environ["PROACTIVE_MEMORY_DATA_ROOT"] = str(self.home / "legacy-config")
        self.assertEqual(default_data_root(), self.home / "legacy-config")
        self.assertEqual(_parser().parse_args(["serve"]).data_root, self.home / "legacy-config")
        os.environ["SN_PROACTIVE_AGENT_DATA_ROOT"] = str(self.home / "new-config")
        self.assertEqual(default_data_root(), self.home / "new-config")
        args = _parser().parse_args(["serve", "--data-root", str(self.home / "explicit")])
        self.assertEqual(args.data_root, self.home / "explicit")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_new_explicit_empty_setting_overrides_legacy(self):
        os.environ["PROACTIVE_MEMORY_DATA_ROOT"] = "/legacy"
        os.environ["SN_PROACTIVE_AGENT_DATA_ROOT"] = ""
        self.assertEqual(env_value("SN_PROACTIVE_AGENT_DATA_ROOT"), "")
        self.assertEqual(default_data_root(), self.home / ".sn-proactive-agent")

    def test_legacy_cli_options_are_read_but_new_names_win(self):
        os.environ.update(PROACTIVE_MEMORY_HOST="localhost", PROACTIVE_MEMORY_PORT="9080",
                          PROACTIVE_MEMORY_HERMES="legacy-hermes", PROACTIVE_MEMORY_HERMES_MODEL="model-old",
                          PROACTIVE_MEMORY_HERMES_PROVIDER="provider", PROACTIVE_MEMORY_SERVICE_URL="http://localhost:9080")
        args = _parser().parse_args(["serve"])
        self.assertEqual((args.host, args.port, args.hermes, args.hermes_model, args.hermes_provider),
                         ("localhost", 9080, "legacy-hermes", "model-old", "provider"))
        self.assertEqual(_parser().parse_args(["respond", "id", "approve"]).url, "http://localhost:9080")
        os.environ["SN_PROACTIVE_AGENT_PORT"] = "9081"
        self.assertEqual(_parser().parse_args(["serve"]).port, 9081)

    def test_child_environment_keeps_legacy_recursion_and_source_guards(self):
        result = HermesAcpTarget("hermes").environment({})
        self.assertEqual(result["SN_PROACTIVE_AGENT_INTERNAL_REASONER"], "1")
        self.assertEqual(result["PROACTIVE_MEMORY_INTERNAL_REASONER"], "1")
        set_compatible_env(result, "SN_PROACTIVE_AGENT_SOURCE_SUGGESTION_ID", "approved-id")
        self.assertEqual(result["PROACTIVE_MEMORY_SOURCE_SUGGESTION_ID"], "approved-id")

    def test_standalone_hooks_accept_legacy_settings(self):
        os.environ.update(PROACTIVE_MEMORY_SERVICE_URL="http://localhost:9080/",
                          PROACTIVE_MEMORY_HTTP_TIMEOUT="0.5", PROACTIVE_MEMORY_HERMES_PLATFORMS="desktop",
                          PROACTIVE_MEMORY_SOURCE_SUGGESTION_ID="old-approved")
        self.assertEqual(tui._service_url(), "http://localhost:9080")
        self.assertEqual(tui._timeout(), 0.5)
        self.assertTrue(tui._is_tui({"platform": "desktop"}))
        payload = {"session_id": "s", "hook_event_name": "post_llm_call",
                   "extra": {"turn_id": "t", "user_message": "question", "assistant_response": "answer"}}
        self.assertEqual(hermes_hook._map_payload(payload)[1]["source_suggestion_id"], "old-approved")
        os.environ["SN_PROACTIVE_AGENT_SOURCE_SUGGESTION_ID"] = "new-approved"
        self.assertEqual(hermes_hook._map_payload(payload)[1]["source_suggestion_id"], "new-approved")
        os.environ["SN_PROACTIVE_AGENT_SERVICE_URL"] = "http://localhost:9081"
        self.assertEqual(tui._service_url(), "http://localhost:9081")
        with patch.object(hermes_hook.urllib.request, "urlopen") as request:
            hermes_hook._post("turn.completed", {})
        self.assertEqual(request.call_args.args[0].full_url, "http://localhost:9081/v1/events/turn.completed")

    def test_legacy_internal_reasoner_does_not_ingest(self):
        os.environ["PROACTIVE_MEMORY_INTERNAL_REASONER"] = "1"
        with patch("sys.stdout", io.StringIO()) as output, patch.object(hermes_hook, "_post") as post:
            hermes_hook.main()
        post.assert_not_called()
        self.assertEqual(output.getvalue(), "{}\n")

    def test_old_connector_blocks_setup_before_any_write(self):
        for relative in (
            "plugins/proactive-memory-tui/__init__.py",
            "skills/productivity/proactive-memory/connectors/hermes/classic/hermes_hook.py",
            "skills/productivity/proactive-memory/scripts/hermes_hook.py",
            "config.yaml",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                target = home / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("plugins:\n  enabled: [proactive-memory-tui]\n")
                before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
                for dry_run in (True, False):
                    with self.assertRaisesRegex(LifecycleError, "Legacy"):
                        setup_harness("hermes", hermes_home=home, dry_run=dry_run, output=io.StringIO())
                after = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
                self.assertEqual(before, after)

    def test_unreadable_ownership_check_fails_without_exposing_config(self):
        (self.home / "config.yaml").write_text("custom: keep\n")
        with patch.object(Path, "read_text", side_effect=PermissionError("sensitive config detail")):
            with self.assertRaises(LifecycleError) as raised:
                setup_harness("hermes", hermes_home=self.home, output=io.StringIO())
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertFalse((self.home / "plugins").exists())


if __name__ == "__main__":
    unittest.main()
