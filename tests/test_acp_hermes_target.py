from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONNECTORS_ROOT = ROOT / "src" / "sn_proactive_agent_connectors"
if str(CONNECTORS_ROOT) not in sys.path:
    sys.path.insert(0, str(CONNECTORS_ROOT))

from acp import HermesAcpTarget  # noqa: E402


class HermesAcpTargetTests(unittest.TestCase):
    def test_builds_real_hermes_acp_command_and_isolates_hooks(self) -> None:
        target = HermesAcpTarget("/opt/hermes")

        self.assertEqual(target.command, ("/opt/hermes", "acp"))
        environment = target.environment({"PATH": "/bin"})
        self.assertEqual(environment["PATH"], "/bin")
        self.assertEqual(environment["SN_PROACTIVE_AGENT_INTERNAL_REASONER"], "1")
        self.assertEqual(environment["HERMES_IGNORE_RULES"], "1")
        self.assertEqual(environment["HERMES_SAFE_MODE"], "1")

    def test_safe_mode_can_be_disabled_without_enabling_public_hooks(self) -> None:
        environment = HermesAcpTarget("hermes", safe_mode=False).environment(
            {"HERMES_SAFE_MODE": "1"}
        )

        self.assertNotIn("HERMES_SAFE_MODE", environment)
        self.assertEqual(environment["SN_PROACTIVE_AGENT_INTERNAL_REASONER"], "1")
        self.assertEqual(environment["HERMES_IGNORE_RULES"], "1")


if __name__ == "__main__":
    unittest.main()
