"""scripts/cleanup.py: helpers everywhere; a real --dry-run pass on Linux (needs /proc)."""
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cleanup.py"


def load(home, **env):
    with mock.patch.dict(os.environ, {"HERMES_HOME": str(home), **env}):
        spec = importlib.util.spec_from_file_location("cleanup_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".hermes"
        self.home.mkdir()
        self.mod = load(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_human(self):
        self.assertEqual(self.mod.human(512), "512 B")
        self.assertEqual(self.mod.human(3 * 1024 ** 3), "3.0 GB")

    def test_protected_services_from_env(self):
        self.assertEqual(self.mod.PROTECTED_SERVICES, ("hermes-gateway",))
        mod = load(self.home, HERMES_CLEANUP_PROTECTED_SERVICES="hermes-gateway, my-db")
        self.assertEqual(mod.PROTECTED_SERVICES, ("hermes-gateway", "my-db"))
        self.assertIn("my-db", mod.PROTECTED_WORDS)

    def test_hermes_command_prefers_checkout_launcher(self):
        launcher = self.home / "hermes-agent" / ".hermes" / "bin" / "hermes"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        self.assertEqual(self.mod.hermes_command(), [str(launcher)])

    def test_classify(self):
        old = {"age": 10 ** 6, "cmd": "python -m browser_harness.daemon --x", "pid": 5, "ppid": 1}
        self.assertEqual(self.mod.classify(old), "browser harness daemon")
        self.assertIsNone(self.mod.classify(dict(old, age=5)))
        self.assertEqual(self.mod.classify({"age": 4000, "cmd": "/usr/bin/himalaya envelope list"}),
                         "hung himalaya (IMAP)")

    @unittest.skipUnless(Path("/proc/uptime").exists(), "needs Linux /proc")
    def test_dry_run_changes_nothing(self):
        stale = self.home / "cache" / "scratch" / "old-job"
        stale.mkdir(parents=True)
        (stale / "f.txt").write_text("x")
        old = 1_000_000_000
        os.utime(stale / "f.txt", (old, old))
        os.utime(stale, (old, old))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(self.mod.main(["--dry-run", "--verbose"]), 0)
        out = buf.getvalue()
        self.assertIn("DRY RUN", out)
        self.assertIn("old-job", out)
        self.assertTrue(stale.exists())

    def _old_backup(self, name="pre-change-2020"):
        root = Path(self.tmp.name) / "backups"
        entry = root / name
        entry.mkdir(parents=True)
        (entry / "state.db").write_text("x")
        old = 1_000_000_000
        os.utime(entry / "state.db", (old, old))
        os.utime(entry, (old, old))
        return load(self.home, HERMES_CLEANUP_BACKUPS_DIR=str(root)), entry

    def test_manual_backups_kept_without_confirmed_restic_backup(self):
        mod, entry = self._old_backup()
        self.assertFalse(mod.restic_backup_confirmed()[0])  # no marker
        found = mod.old_manual_backups()
        self.assertEqual([e[0] for e in found], [entry])
        with mock.patch.object(mod, "stale_processes", return_value=([], {})), \
                mock.patch.object(mod, "checkpoints", return_value=(0, None)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                mod.main([])
        self.assertTrue(entry.exists())
        self.assertIn("kept 1 old manual backup", buf.getvalue())

    def test_manual_backups_deleted_only_when_confirmed(self):
        mod, entry = self._old_backup()
        fresh = entry.parent / "fresh"
        fresh.mkdir()
        (fresh / "f").write_text("y")
        with mock.patch.object(mod, "stale_processes", return_value=([], {})), \
                mock.patch.object(mod, "checkpoints", return_value=(0, None)), \
                mock.patch.object(mod, "restic_backup_confirmed", return_value=(True, "ok")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                mod.main(["--dry-run"])
            self.assertTrue(entry.exists())
            self.assertIn("would delete", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                mod.main([])
        self.assertFalse(entry.exists())
        self.assertTrue(fresh.exists())

    def test_marker_too_old_is_not_confirmed(self):
        (self.home / "state").mkdir()
        (self.home / "state" / "backup_last.json").write_text(
            '{"time": "2020-01-01T00:00:00+00:00", "snapshot": "abc"}')
        ok, why = self.mod.restic_backup_confirmed()
        self.assertFalse(ok)
        self.assertIn("h old", why)


if __name__ == "__main__":
    unittest.main()
