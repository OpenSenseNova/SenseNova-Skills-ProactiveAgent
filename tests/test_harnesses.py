from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sn_proactive_agent.harnesses import HarnessRegistry


class HarnessRegistryTests(unittest.TestCase):
    def test_defaults_enable_available_hermes_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = HarnessRegistry(
                directory,
                available={"hermes": True, "codex": True},
                connected={"hermes": True},
            )

            self.assertEqual(
                [(item["id"], item["enabled"], item["status"]) for item in registry.snapshot()],
                [
                    ("hermes", True, "connected"),
                    ("openclaw", False, "disabled"),
                    ("codex", False, "disabled"),
                ],
            )
            self.assertTrue(registry.allows_platform("hermes-tui"))
            self.assertFalse(registry.allows_platform("codex-acp"))

    def test_setting_is_persisted_and_applies_to_connector_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = HarnessRegistry(directory, available={"codex": True})

            changed = registry.set_enabled("codex", True)

            self.assertEqual(changed["status"], "ready")
            self.assertTrue(registry.allows_platform("codex-acp"))
            saved = json.loads((Path(directory) / "harnesses.json").read_text())
            self.assertEqual(saved, {"codex": True})

            restored = HarnessRegistry(directory, available={"codex": True})
            self.assertTrue(restored.snapshot()[2]["enabled"])

    def test_unavailable_harness_cannot_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = HarnessRegistry(directory)

            with self.assertRaises(RuntimeError):
                registry.set_enabled("codex", True)


if __name__ == "__main__":
    unittest.main()
