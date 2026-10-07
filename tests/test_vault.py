"""Vault MCP server: storage, encryption, env-file export (MCP transport itself is not exercised).

Needs `cryptography` (the vault venv has it); skipped otherwise.
"""
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

VAULT = Path(__file__).resolve().parent.parent / "vault" / "vault_mcp.py"
try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


def load(home):
    with mock.patch.dict(os.environ, {"VAULT_HOME": str(home)}):
        spec = importlib.util.spec_from_file_location("vault_mcp_test", VAULT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "vault"
        self.mod = load(self.home)
        self.v = self.mod.Vault()

    def tearDown(self):
        self.v.con.close()
        self.tmp.cleanup()

    def test_save_get_list_delete(self):
        self.assertEqual(self.v.save("example_token", "s3cr3t value", "example", "test"), "saved EXAMPLE_TOKEN")
        self.assertEqual(self.v.save("EXAMPLE_TOKEN", "new", "example"), "updated EXAMPLE_TOKEN")
        self.assertEqual(self.v.get("example_token"), "new")
        rows = self.v.list()
        self.assertEqual([r["name"] for r in rows], ["EXAMPLE_TOKEN"])
        self.assertNotIn("value", rows[0])
        self.assertEqual(self.v.delete("EXAMPLE_TOKEN"), "deleted EXAMPLE_TOKEN")
        with self.assertRaises(KeyError):
            self.v.get("EXAMPLE_TOKEN")

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.v.save("bad name!", "x")
        with self.assertRaises(ValueError):
            self.v.save("GOOD_NAME", "")

    def test_encrypted_at_rest_and_private_files(self):
        self.v.save("PLAIN_CHECK", "find-me-in-the-db")
        self.assertNotIn(b"find-me-in-the-db", (self.home / "vault.db").read_bytes())
        for f in ("vault.db", "vault.key"):
            self.assertEqual(stat.S_IMODE((self.home / f).stat().st_mode), 0o600, f)

    def test_env_file_is_sourceable_private_and_swept(self):
        self.v.save("A_TOKEN", "it's \"quoted\" $HOME")
        path = Path(self.v.env_file(["a_token"]))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        out = subprocess.run(["bash", "-c", f'set -a; . "{path}"; set +a; printf %s "$A_TOKEN"'],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "it's \"quoted\" $HOME")
        old = time.time() - self.mod.ENV_FILE_TTL - 5
        os.utime(path, (old, old))
        self.mod.Vault.sweep()
        self.assertFalse(path.exists())

    def test_cli_set_and_list(self):
        env = dict(os.environ, VAULT_HOME=str(self.home))
        subprocess.run([sys.executable, str(VAULT), "set", "CLI_TOKEN", "--service", "demo"], input="v1\n",
                       text=True, env=env, check=True, capture_output=True)
        listed = subprocess.run([sys.executable, str(VAULT), "list"], text=True, env=env, check=True,
                                capture_output=True).stdout
        self.assertIn("CLI_TOKEN", listed)
        self.assertNotIn("v1", listed)

    def test_owner_name_is_configurable(self):
        self.assertEqual(self.mod.OWNER, "the user")
        with mock.patch.dict(os.environ, {"VAULT_HOME": str(self.home), "VAULT_OWNER_NAME": "Alex"}):
            spec = importlib.util.spec_from_file_location("vault_mcp_owner", VAULT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        self.assertEqual(mod.OWNER, "Alex")


try:
    import mcp.server.fastmcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False


@unittest.skipUnless(HAVE_CRYPTO and HAVE_MCP, "needs cryptography + mcp (the vault venv)")
class McpToolTests(unittest.TestCase):
    def test_tools_registered_with_generic_owner(self):
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            mod = load(Path(tmp) / "vault")
            v = mod.Vault()
            tools = {t.name: t.description for t in asyncio.run(mod.build_server(v).list_tools())}
            v.con.close()
        self.assertEqual(set(tools), {"vault_list", "vault_save", "vault_delete", "vault_use", "vault_reveal"})
        self.assertIn("Only when the user explicitly asks", tools["vault_reveal"])


if __name__ == "__main__":
    unittest.main()
