from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sn_proactive_agent.__main__ import _doctor, _parser
from sn_proactive_agent.lifecycle import DoctorCheck, DoctorReport


class ParserDefaultsTests(unittest.TestCase):
    def test_new_user_defaults_to_home_memory_directory(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch.object(Path, "home", return_value=Path(home)):
                with patch.dict(os.environ, {"SN_PROACTIVE_AGENT_DATA_ROOT": ""}):
                    args = _parser().parse_args(["serve"])

        self.assertEqual(args.data_root, Path(home) / ".sn-proactive-agent")

    def test_explicit_data_root_environment_overrides_default(self) -> None:
        with patch.dict(
            os.environ,
            {"SN_PROACTIVE_AGENT_DATA_ROOT": "/tmp/sn-proactive-agent-test"},
        ):
            args = _parser().parse_args(["serve"])

        self.assertEqual(args.data_root, Path("/tmp/sn-proactive-agent-test"))

    def test_command_line_data_root_takes_precedence(self) -> None:
        with patch.dict(
            os.environ,
            {"SN_PROACTIVE_AGENT_DATA_ROOT": "/tmp/sn-proactive-agent-env"},
        ):
            args = _parser().parse_args(
                ["serve", "--data-root", "/tmp/sn-proactive-agent-cli"]
            )

        self.assertEqual(args.data_root, Path("/tmp/sn-proactive-agent-cli"))

    def test_web_only_disables_native_tui_suggestion_rendering(self) -> None:
        args = _parser().parse_args(["serve", "--web-only"])

        self.assertTrue(args.web_only)

    def test_web_only_defaults_to_false_for_compatibility(self) -> None:
        args = _parser().parse_args(["serve"])

        self.assertFalse(args.web_only)

    def test_doctor_home_argument_overrides_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"HERMES_HOME": str(root / "env")}):
                default = _parser().parse_args(["doctor", "--harness", "hermes"])
                explicit = _parser().parse_args([
                    "doctor", "--harness", "hermes", "--hermes-home", str(root / "explicit"),
                ])
            self.assertEqual(default.hermes_home, root / "env")
            self.assertEqual(explicit.hermes_home, root / "explicit")

    def test_doctor_passes_target_home_and_unknown_result_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = _parser().parse_args([
                "doctor", "--harness", "hermes", "--hermes-home", directory, "--json",
            ])
            report = DoctorReport((
                DoctorCheck("hermes_same_session_resume", False, "not probed", verified=False),
            ), scope="hermes")
            output = io.StringIO()
            with (
                patch("sn_proactive_agent.__main__.run_doctor", return_value=report) as run,
                patch("sys.stdout", output),
            ):
                code = _doctor(args)
            self.assertEqual(run.call_args.kwargs["hermes_home"], Path(directory))
            self.assertEqual(code, 1)
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["scope"], "hermes")
            self.assertEqual(payload["checks"][0]["status"], "unverified")

    def test_doctor_text_distinguishes_unverified_from_passed(self) -> None:
        report = DoctorReport((
            DoctorCheck("hermes_same_session_resume", False, "not probed", verified=False),
        ), scope="hermes")
        output = io.StringIO()
        with (
            patch("sn_proactive_agent.__main__.run_doctor", return_value=report),
            patch("sys.stdout", output),
        ):
            code = _doctor(_parser().parse_args(["doctor", "--harness", "hermes"]))
        self.assertEqual(code, 1)
        self.assertIn("? hermes_same_session_resume", output.getvalue())
        self.assertNotIn("Doctor: OK", output.getvalue())

    def test_doctor_base_runtime_success_exits_zero(self) -> None:
        report = DoctorReport((DoctorCheck("python", True, "supported"),))
        output = io.StringIO()
        with (
            patch("sn_proactive_agent.__main__.run_doctor", return_value=report),
            patch("sys.stdout", output),
        ):
            code = _doctor(_parser().parse_args(["doctor", "--json"]))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["scope"], "runtime")


if __name__ == "__main__":
    unittest.main()
