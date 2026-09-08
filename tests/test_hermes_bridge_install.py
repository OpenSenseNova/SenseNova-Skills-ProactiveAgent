from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from proactive_memory_service import hermes_bridge_install as installer
from proactive_memory_service.lifecycle import LifecycleError, _enable_tui_plugin, setup_harness, uninstall_harness


class BridgeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "hermes-home"
        self.base = self.root / "ui-tui"
        self.source = self.base / installer.TARGETS[0]
        self.source.parent.mkdir(parents=True)
        self.source.write_text(installer.IMPORT_ANCHOR + "function useMainApp() {\n" + installer.HOOK_ANCHOR + "}\n")
        self.original = self.source.read_bytes()
        self.build = self.base / "scripts/build.mjs"
        self.build.parent.mkdir()
        self.build.write_bytes(b"// controlled test builder\n")
        self.dist = self.base / "dist/entry.js"
        self.dist.parent.mkdir()
        self.dist.write_bytes(b"original built TUI\n")
        self.dist.chmod(0o755)
        self.addCleanup(patch.stopall)
        patch.object(installer, "SOURCE_HASH", installer.digest(self.original)).start()
        patch.object(installer, "BUILD_HASH", installer.digest(self.build.read_bytes())).start()

    def build_fixture(self, base):
        (base / "dist/entry.js").write_bytes(b"compiled fixture Web bridge\n")

    def install(self):
        with installer.install_bridge(self.root, self.home, builder=self.build_fixture):
            pass

    def test_dry_run_is_read_only(self):
        with installer.install_bridge(self.root, self.home, dry_run=True):
            pass
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse((self.base / installer.STATE_DIR).exists())

    def test_install_is_idempotent_and_uninstall_restores_original_build(self):
        with patch.object(installer, "_build", side_effect=self.build_fixture) as build:
            with installer.install_bridge(self.root, self.home):
                pass
            with installer.install_bridge(self.root, self.home):
                pass
            self.assertEqual(build.call_count, 1)
        self.assertTrue(installer.inspect_install(self.root)[0])
        installer.uninstall_bridge(self.root)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.dist.read_bytes(), b"original built TUI\n")
        self.assertEqual(self.dist.stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.base / installer.TARGETS[1]).exists())
        self.assertFalse(installer.uninstall_bridge(self.root))

    def test_unknown_source_fails_before_writes(self):
        self.source.write_bytes(self.original + b"// local changes")
        with self.assertRaises(installer.BridgeInstallError):
            self.install()
        self.assertFalse((self.base / installer.STATE_DIR).exists())
        self.assertTrue(self.source.read_bytes().endswith(b"// local changes"))

    def test_unknown_build_script_fails_before_writes(self):
        self.build.write_bytes(b"untrusted build")
        with self.assertRaises(installer.BridgeInstallError):
            self.install()
        self.assertFalse((self.base / installer.STATE_DIR).exists())

    def test_failed_build_rolls_back_partial_output(self):
        def fail(base):
            (base / "dist/entry.js").write_bytes(b"partial build")
            raise installer.BridgeInstallError("build failed")
        with self.assertRaises(installer.BridgeInstallError):
            with installer.install_bridge(self.root, self.home, builder=fail):
                pass
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.dist.read_bytes(), b"original built TUI\n")
        self.assertFalse((self.base / installer.TARGETS[1]).exists())
        self.install()  # A rolled-back installation is safely retryable.
        self.assertTrue(installer.inspect_install(self.root)[0])

    def test_setup_failure_after_build_restores_all_owned_files(self):
        self.home.mkdir()
        config = self.home / "config.yaml"
        config.write_text("custom: keep\n")
        with (
            patch.object(installer, "_build", side_effect=self.build_fixture),
            patch("proactive_memory_service.lifecycle._enable_tui_plugin", return_value="enable failed"),
            self.assertRaises(LifecycleError),
        ):
            setup_harness("hermes", hermes_home=self.home, hermes_root=self.root,
                          install_resume_bridge=True, hermes_executable="hermes", output=io.StringIO())
        self.assertEqual(config.read_text(), "custom: keep\n")
        self.assertFalse((self.home / "plugins/proactive-memory-tui/__init__.py").exists())
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.dist.read_bytes(), b"original built TUI\n")

    def test_config_conflict_stops_before_bridge_build(self):
        self.home.mkdir()
        (self.home / "config.yaml").write_text("hooks:\n  existing: []\n")
        with self.assertRaises(LifecycleError):
            setup_harness("hermes", hermes_home=self.home, hermes_root=self.root,
                          install_resume_bridge=True, output=io.StringIO())
        self.assertFalse((self.base / installer.STATE_DIR).exists())

    def test_full_setup_and_uninstall_keep_data_and_restore_bridge(self):
        data = self.root / "memory"
        data.mkdir()
        (data / "runtime.jsonl").write_bytes(b"user data\n")
        with (
            patch.object(installer, "_build", side_effect=self.build_fixture),
            patch("proactive_memory_service.lifecycle._enable_tui_plugin", return_value=None),
        ):
            result = setup_harness("hermes", hermes_home=self.home, hermes_root=self.root,
                hermes_executable="hermes", install_resume_bridge=True, output=io.StringIO())
        self.assertEqual(result.bridge_root, self.root.resolve())
        with self.assertRaises(LifecycleError):
            uninstall_harness("hermes", hermes_home=self.home)
        uninstall_harness("hermes", hermes_home=self.home, hermes_root=self.root,
                          data_root=data, output=io.StringIO())
        self.assertEqual((data / "runtime.jsonl").read_bytes(), b"user data\n")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_later_user_edits_are_not_overwritten_by_uninstall(self):
        self.install()
        self.source.write_bytes(self.source.read_bytes() + b"// later edit")
        with self.assertRaises(installer.BridgeInstallError):
            installer.uninstall_bridge(self.root)
        self.assertTrue(self.source.read_bytes().endswith(b"// later edit"))

    def test_tampered_backup_stops_before_any_restore(self):
        self.install()
        (self.base / installer.STATE_DIR / "0.bak").write_bytes(b"tampered")
        before = self.source.read_bytes()
        with self.assertRaises(installer.BridgeInstallError):
            installer.uninstall_bridge(self.root)
        self.assertEqual(self.source.read_bytes(), before)

    def test_other_home_cannot_reuse_installation(self):
        self.install()
        with self.assertRaises(installer.BridgeInstallError):
            with installer.install_bridge(self.root, self.root / "other-home"):
                pass

    def test_other_home_cannot_uninstall_bridge_even_without_pointer(self):
        self.install()
        with self.assertRaises(LifecycleError):
            uninstall_harness("hermes", hermes_home=self.root / "other-home",
                              hermes_root=self.root, output=io.StringIO())
        self.assertTrue(installer.inspect_install(self.root)[0])

    @unittest.skipIf(os.name == "nt", "symlink creation may need Windows privileges")
    def test_observer_parent_symlink_is_rejected_before_source_changes(self):
        self.home.mkdir()
        other = self.root / "unrelated-plugins"
        other.mkdir()
        (self.home / "plugins").symlink_to(other, target_is_directory=True)
        with self.assertRaises(LifecycleError):
            setup_harness("hermes", hermes_home=self.home, hermes_root=self.root,
                          install_resume_bridge=True, output=io.StringIO())
        self.assertEqual(list(other.iterdir()), [])
        self.assertFalse((self.base / installer.STATE_DIR).exists())

    def test_full_install_requires_explicit_source_root(self):
        with self.assertRaises(LifecycleError):
            setup_harness("hermes", hermes_home=self.home, install_resume_bridge=True)
        self.assertFalse(self.home.exists())

    def test_plugin_enable_has_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("hermes", 30)) as run:
            self.assertIsNotNone(_enable_tui_plugin("hermes", self.home))
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_plugin_enable_does_not_echo_arbitrary_config_errors(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="private config content")
        with patch("subprocess.run", return_value=result):
            message = _enable_tui_plugin("hermes", self.home)
        self.assertNotIn("private config content", message)

    @unittest.skipIf(os.name == "nt", "symlink creation may need Windows privileges")
    def test_source_symlink_is_rejected(self):
        self.source.unlink()
        other = self.root / "unrelated"
        other.write_bytes(self.original)
        self.source.symlink_to(other)
        with self.assertRaises(installer.BridgeInstallError):
            self.install()
        self.assertEqual(other.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
