"""scripts/backup_data.py: config loading, consistent SQLite snapshots, failure output (no restic needed)."""
import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup_data.py"


def load(home, **env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("BACKUP_")}
    with mock.patch.dict(os.environ, {**clean, "HERMES_HOME": str(home), **env}, clear=True):
        spec = importlib.util.spec_from_file_location("backup_data_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class BackupDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".hermes"
        self.home.mkdir()
        self.mod = load(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_file_and_env_override(self):
        (self.home / "backup.conf").write_text("# c\nBACKUP_REPO=sftp:h:/r\nBACKUP_TAGS='a,b'\n")
        with mock.patch.dict(os.environ, {"BACKUP_TAGS": "x"}):
            cfg = self.mod.load_config()
        self.assertEqual(cfg["BACKUP_REPO"], "sftp:h:/r")
        self.assertEqual(cfg["BACKUP_TAGS"], "x")
        self.assertEqual(self.mod.csv(" a, ,b "), ["a", "b"])

    def test_snapshot_of_wal_database_is_consistent(self):
        src = self.home / "live.db"
        con = sqlite3.connect(src)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (x)")
        con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
        con.commit()  # rows still sit in the -wal file; a plain file copy could miss them
        dst = Path(self.tmp.name) / "snap" / "live.db"
        self.mod.snapshot_db(src, dst)
        con.close()
        out = sqlite3.connect(dst)
        self.assertEqual(out.execute("SELECT count(*) FROM t").fetchone()[0], 500)
        self.assertEqual(out.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        out.close()

    def test_missing_repo_fails_with_one_line(self):
        with mock.patch.dict(os.environ, {"BACKUP_REPO": ""}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(self.mod.main([]), 1)
        self.assertEqual(len(buf.getvalue().strip().splitlines()), 1)
        self.assertIn("BACKUP_REPO", buf.getvalue())

    def test_missing_required_db_fails_before_restic(self):
        (self.home / "backup.conf").write_text("BACKUP_REPO=sftp:h:/r\n")
        with mock.patch.object(self.mod.Restic, "run", side_effect=AssertionError("restic called")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(self.mod.main([]), 1)
        self.assertIn("required database missing: state.db", buf.getvalue())
        self.assertFalse((self.home / "backup-staging").exists())


if __name__ == "__main__":
    unittest.main()
