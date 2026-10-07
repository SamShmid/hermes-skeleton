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


if __name__ == "__main__":
    unittest.main()
