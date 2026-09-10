from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from sn_proactive_agent.lifecycle import (
    DoctorCheck,
    DoctorReport,
    LifecycleError,
    run_doctor,
    setup_harness,
    uninstall_harness,
)
from sn_proactive_agent.packaged_resources import connector_resource


class LifecycleTests(unittest.TestCase):
    def test_setup_installs_classic_hook_without_copying_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            home.mkdir()
            output = io.StringIO()
            result = setup_harness(
                "hermes",
                hermes_home=home,
                install_tui_plugin=False,
                output=output,
            )

            self.assertTrue(result.config_changed)
            self.assertTrue(result.hook_path.is_file())
            skill_path = (
                home / "skills" / "productivity" / "sn-proactive-agent" / "SKILL.md"
            )
            self.assertFalse(skill_path.exists())
            config = result.config_path.read_text(encoding="utf-8")
            self.assertIn(str(result.hook_path), config)
            self.assertIn("pre_llm_call", config)
            self.assertIn("post_llm_call", config)

    def test_setup_is_idempotent_and_does_not_overwrite_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            home.mkdir()
            first = setup_harness(
                "hermes", hermes_home=home, install_tui_plugin=False
            )
            original = first.config_path.read_text(encoding="utf-8")
            second = setup_harness(
                "hermes", hermes_home=home, install_tui_plugin=False
            )
            self.assertFalse(second.config_changed)
            self.assertEqual(second.config_path.read_text(encoding="utf-8"), original)

            conflict = Path(directory) / "conflict"
            conflict.mkdir()
            (conflict / "config.yaml").write_text(
                "hooks:\n  other:\n    - command: do-not-touch\n",
                encoding="utf-8",
            )
            with self.assertRaises(LifecycleError):
                setup_harness(
                    "hermes", hermes_home=conflict, install_tui_plugin=False
                )

    def test_setup_copies_tui_observer_resources_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            setup_harness(
                "hermes",
                hermes_home=home,
                hermes_executable="/usr/bin/true",
            )
            plugin = home / "plugins" / "sn-proactive-agent-tui"
            self.assertTrue((plugin / "__init__.py").is_file())
            self.assertTrue((plugin / "plugin.yaml").is_file())
            self.assertTrue(
                (plugin / "patches" / "hermes-tui-acp-submit.patch").is_file()
            )

    def test_uninstall_removes_connector_but_preserves_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "hermes"
            data = root / "memory"
            data.mkdir()
            marker = data / "runtime.jsonl"
            marker.write_text("keep\n", encoding="utf-8")
            setup_harness("hermes", hermes_home=home, install_tui_plugin=False)

            result = uninstall_harness(
                "hermes", hermes_home=home, data_root=data
            )
            self.assertTrue(result.config_changed)
            hook = (
                home
                / "skills"
                / "productivity"
                / "sn-proactive-agent"
                / "connectors"
                / "hermes"
                / "classic"
                / "hermes_hook.py"
            )
            self.assertFalse(hook.exists())
            self.assertTrue(marker.is_file())
            self.assertNotIn(
                "sn-proactive-agent",
                home.joinpath("config.yaml").read_text(encoding="utf-8"),
            )

    def test_doctor_is_read_only_and_json_serialisable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "memory"
            report = run_doctor(data_root=data)
            self.assertFalse(data.exists())
            payload = report.to_dict()
            json.dumps(payload, ensure_ascii=False)
            self.assertEqual(payload["scope"], "runtime")
            self.assertTrue(all("status" in check for check in payload["checks"]))
            names = {check["name"] for check in payload["checks"]}
            self.assertIn("python", names)
            self.assertIn("data_root", names)

    def test_setup_rejects_unknown_harness(self) -> None:
        with self.assertRaises(LifecycleError):
            setup_harness("openclaw")

    def test_doctor_can_check_health_endpoint(self) -> None:
        response = Mock()
        response.read.return_value = b'{"status":"ok"}'
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *_args: None
        response.status = 200
        with patch("urllib.request.urlopen", return_value=response):
            report = run_doctor(
                data_root=tempfile.gettempdir(),
                service_url="http://service.test",
            )
        service = next(check for check in report.checks if check.name == "service")
        self.assertTrue(service.ok)


class DoctorIntegrationTests(unittest.TestCase):
    def _install_observer_fixture(self, home: Path) -> Path:
        plugin = home / "plugins" / "sn-proactive-agent-tui"
        plugin.mkdir(parents=True)
        for filename in ("__init__.py", "plugin.yaml"):
            source = connector_resource(f"hermes/tui/{filename}")
            (plugin / filename).write_bytes(source.read_bytes())
        return plugin

    def _report(self, home: Path) -> DoctorReport:
        return run_doctor(
            harness="hermes",
            hermes_home=home,
            hermes_executable=sys.executable,
            data_root=home.parent / "memory",
        )

    def test_fresh_home_does_not_claim_connector_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "fresh-hermes"
            report = self._report(home)
            checks = {check.name: check for check in report.checks}
            self.assertFalse(home.exists())
            self.assertFalse((home.parent / "memory").exists())
            self.assertFalse(report.ok)
            self.assertEqual(report.scope, "hermes")
            self.assertEqual(checks["hermes_observer_installed"].status, "failed")
            self.assertEqual(checks["hermes_turn_capture"].status, "unverified")
            self.assertEqual(checks["hermes_same_session_resume"].status, "unverified")

    def test_observer_and_patch_presence_do_not_prove_live_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            plugin = self._install_observer_fixture(home)
            (plugin / "patches").mkdir()
            (plugin / "patches" / "hermes-tui-acp-submit.patch").write_text(
                "An unapplied patch is not a running bridge.\n", encoding="utf-8"
            )
            report = self._report(home)
            checks = {check.name: check for check in report.checks}
            self.assertEqual(checks["hermes_observer_installed"].status, "passed")
            self.assertFalse(checks["hermes_same_session_resume"].ok)
            self.assertTrue(checks["hermes_same_session_resume"].required)
            self.assertEqual(checks["hermes_turn_capture"].status, "unverified")
            self.assertFalse(report.ok)

    def test_doctor_respects_home_environment_and_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_home = root / "environment-hermes"
            self._install_observer_fixture(environment_home)
            with patch.dict(os.environ, {"HERMES_HOME": str(environment_home)}):
                report = run_doctor(
                    harness=" Hermes ",
                    hermes_executable=sys.executable,
                    data_root=root / "memory",
                )
                overridden = self._report(root / "explicit-hermes")
            self.assertEqual(report.scope, "hermes")
            self.assertTrue(next(
                c for c in report.checks if c.name == "hermes_observer_installed"
            ).ok)
            self.assertFalse(next(
                c for c in overridden.checks if c.name == "hermes_observer_installed"
            ).ok)

    def test_explicit_nonexistent_executable_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = run_doctor(
                harness="hermes",
                hermes_executable=str(Path(directory) / "missing-hermes"),
                hermes_home=Path(directory) / "hermes",
                data_root=Path(directory) / "memory",
            )
            executable = next(c for c in report.checks if c.name == "hermes_executable")
            self.assertEqual(executable.status, "failed")

    def test_executable_name_is_resolved_through_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("shutil.which", return_value=sys.executable) as which:
                report = run_doctor(
                    harness="hermes", hermes_executable="custom-hermes",
                    hermes_home=Path(directory) / "hermes", data_root=directory,
                )
            which.assert_called_once_with("custom-hermes")
            self.assertTrue(next(c for c in report.checks if c.name == "hermes_executable").ok)

    def test_partial_observer_is_reported_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            plugin = self._install_observer_fixture(home)
            (plugin / "plugin.yaml").unlink()
            check = next(
                c for c in self._report(home).checks if c.name == "hermes_observer_installed"
            )
            self.assertEqual(check.status, "failed")
            self.assertIn("plugin.yaml", check.detail)

    def test_custom_observer_is_unverified_and_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            plugin = self._install_observer_fixture(home)
            module = plugin / "__init__.py"
            content = module.read_bytes() + b"\n# Local customization\n"
            module.write_bytes(content)
            check = next(
                c for c in self._report(home).checks if c.name == "hermes_observer_installed"
            )
            self.assertEqual(check.status, "unverified")
            self.assertEqual(module.read_bytes(), content)

    def test_unreadable_observer_is_unverified_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            self._install_observer_fixture(home)
            with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
                report = self._report(home)
            self.assertEqual(next(
                c for c in report.checks if c.name == "hermes_observer_installed"
            ).status, "unverified")

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_observer_symlink_does_not_read_arbitrary_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            plugin = self._install_observer_fixture(home)
            module = plugin / "__init__.py"
            module.unlink()
            module.symlink_to(Path(directory) / "unrelated-file")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read")):
                report = self._report(home)
            self.assertEqual(next(
                c for c in report.checks if c.name == "hermes_observer_installed"
            ).status, "unverified")

    def test_doctor_has_no_model_process_config_or_data_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            self._install_observer_fixture(home)
            config = home / "config.yaml"
            config.write_bytes(b"Do not read or modify this fixture.\n")
            before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
            with (
                patch("subprocess.run", side_effect=AssertionError("must not launch")),
                patch("urllib.request.urlopen", side_effect=AssertionError("must not contact")),
                patch.object(Path, "read_text", side_effect=AssertionError("must not read config")),
            ):
                self._report(home)
            after = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((home.parent / "memory").exists())

    def test_report_statuses_and_required_unverified_block_success(self) -> None:
        self.assertEqual(DoctorCheck("a", True, "ok").status, "passed")
        self.assertEqual(DoctorCheck("a", False, "missing").status, "failed")
        unknown = DoctorCheck("a", False, "not probed", verified=False)
        self.assertEqual(unknown.to_dict()["status"], "unverified")
        self.assertFalse(DoctorReport((unknown,)).ok)
        optional = DoctorCheck("a", False, "optional", required=False, verified=False)
        self.assertTrue(DoctorReport((optional,)).ok)

    def test_health_rejects_unrelated_response_without_echoing_body(self) -> None:
        for body in (b"private HTML", b"[]", b'{"status":"error"}'):
            with self.subTest(body=body):
                response = Mock()
                response.read.return_value = body
                response.__enter__ = lambda self: self
                response.__exit__ = lambda self, *_args: None
                response.status = 200
                with patch("urllib.request.urlopen", return_value=response):
                    report = run_doctor(
                        data_root=tempfile.gettempdir(), service_url="http://service.test"
                    )
                check = next(c for c in report.checks if c.name == "service")
                self.assertEqual(check.status, "failed")
                self.assertNotIn(body.decode(), check.detail)

    def test_base_runtime_check_does_not_inspect_hermes_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "sn_proactive_agent.lifecycle._hermes_integration_checks",
                side_effect=AssertionError("runtime scope must not inspect Hermes"),
            ):
                report = run_doctor(data_root=directory)
            self.assertEqual(report.scope, "runtime")
            self.assertNotIn("hermes_turn_capture", {c.name for c in report.checks})

    def test_missing_bundled_plugin_does_not_hide_available_classic_hook(self) -> None:
        def resource(path: str):
            if path == "hermes/tui/plugin.yaml":
                raise FileNotFoundError(path)
            return connector_resource(path)

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "sn_proactive_agent.lifecycle.connector_resource", side_effect=resource,
            ):
                report = run_doctor(data_root=directory)
                hermes_report = self._report(Path(directory) / "hermes")
            checks = {c.name: c for c in report.checks}
            self.assertTrue(checks["hermes_connector"].ok)
            self.assertFalse(checks["hermes_tui_plugin"].ok)
            self.assertFalse(checks["hermes_tui_plugin"].required)
            self.assertTrue(report.ok)
            self.assertTrue(next(
                c for c in hermes_report.checks if c.name == "hermes_tui_plugin"
            ).required)


if __name__ == "__main__":
    unittest.main()
