"""Password vault: an MCP server over an encrypted SQLite database.

Values are encrypted at rest (Fernet; key file next to the DB, mode 600). The agent normally never sees a
value: it asks for a short-lived env file and sources it inside a terminal command, so the command (and Hermes's
own approval prompts) still go through the normal terminal tool.

  vault_mcp.py serve                 run the MCP server (stdio) - what Hermes starts
  vault_mcp.py list | get NAME       local admin from a shell
  vault_mcp.py set NAME --service S --description D   (value read from stdin)
  vault_mcp.py import-env NAME... --service S [--env-file PATH]   copy keys out of a .env file

Environment: VAULT_HOME (default $HERMES_HOME/vault, i.e. ~/.hermes/vault); VAULT_OWNER_NAME is how tool
descriptions refer to the person who owns the secrets (default "the user").
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import sqlite3
import sys
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
HOME = Path(os.environ.get("VAULT_HOME") or HERMES_HOME / "vault")
OWNER = (os.environ.get("VAULT_OWNER_NAME") or "").strip() or "the user"
DB, KEY, TMP = HOME / "vault.db", HOME / "vault.key", HOME / "tmp"
ENV_FILE_TTL = 600  # seconds an exported env file lives
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def _setup():
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    TMP.mkdir(exist_ok=True, mode=0o700)
    if not KEY.exists():
        KEY.write_bytes(Fernet.generate_key())
        KEY.chmod(0o600)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, service TEXT NOT NULL DEFAULT '',
                   description TEXT NOT NULL DEFAULT '', value BLOB NOT NULL, created REAL, updated REAL)""")
    con.commit()
    DB.chmod(0o600)
    return con, Fernet(KEY.read_bytes())


class Vault:
    def __init__(self):
        self.con, self.f = _setup()
        self.lock = threading.Lock()

    def list(self, service: str = ""):
        q = "SELECT name, service, description, updated FROM secrets"
        rows = self.con.execute(q + (" WHERE service=?" if service else "") + " ORDER BY service, name",
                                (service,) if service else ()).fetchall()
        return [{"name": n, "service": s, "description": d,
                 "updated": time.strftime("%Y-%m-%d", time.localtime(u or 0))} for n, s, d, u in rows]

    def save(self, name: str, value: str, service: str = "", description: str = "") -> str:
        name = name.strip().upper()
        if not NAME_RE.match(name):
            raise ValueError("name must look like SERVICE_THING (A-Z, 0-9, _)")
        if not value:
            raise ValueError("empty value")
        now = time.time()
        with self.lock:
            existed = self.con.execute("SELECT 1 FROM secrets WHERE name=?", (name,)).fetchone()
            self.con.execute("""INSERT INTO secrets(name, service, description, value, created, updated)
                                VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                                service=excluded.service, description=excluded.description,
                                value=excluded.value, updated=excluded.updated""",
                             (name, service, description, self.f.encrypt(value.encode()), now, now))
            self.con.commit()
        return f"{'updated' if existed else 'saved'} {name}"

    def get(self, name: str) -> str:
        row = self.con.execute("SELECT value FROM secrets WHERE name=?", (name.strip().upper(),)).fetchone()
        if not row:
            raise KeyError(f"no secret named {name}")
        return self.f.decrypt(row[0]).decode()

    def delete(self, name: str) -> str:
        with self.lock:
            n = self.con.execute("DELETE FROM secrets WHERE name=?", (name.strip().upper(),)).rowcount
            self.con.commit()
        return f"deleted {name}" if n else f"no secret named {name}"

    def env_file(self, names) -> str:
        self.sweep()
        lines = [f"export {n.strip().upper()}={shlex.quote(self.get(n))}" for n in names]
        path = TMP / f"{secrets.token_hex(8)}.env"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return str(path)

    @staticmethod
    def sweep():
        cutoff = time.time() - ENV_FILE_TTL
        for p in TMP.glob("*.env"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except FileNotFoundError:
                pass


def build_server(v=None):
    """The FastMCP server with the vault tools registered (not started)."""
    from mcp.server.fastmcp import FastMCP

    v = v or Vault()
    mcp = FastMCP("vault")

    @mcp.tool()
    def vault_list(service: str = "") -> list:
        """List saved passwords/tokens (names, service, description; never values). Optional service filter."""
        return v.list(service)

    @mcp.tool(description="Save or replace a secret. name like GITHUB_TOKEN; service like 'github'; description "
                          f"says what it is for. Use when {OWNER} gives you a password/token to keep.")
    def vault_save(name: str, value: str, service: str = "", description: str = "") -> str:
        return v.save(name, value, service, description)

    @mcp.tool(description=f"Delete a secret by name (only when {OWNER} asks).")
    def vault_delete(name: str) -> str:
        return v.delete(name)

    @mcp.tool()
    def vault_use(names: list[str]) -> str:
        """Prepare secrets for a terminal command WITHOUT seeing them. Returns a temp env-file path (expires in 10
        minutes). Then run in the terminal: `set -a; . <path>; set +a; your-command "$NAME"`. Never print the values."""
        path = v.env_file(names)
        return (f"env file: {path} (expires in {ENV_FILE_TTL // 60} min). Use in ONE terminal command: "
                f"set -a; . {path}; set +a; <command using \"${names[0].upper()}\"> ; rm -f {path}")

    @mcp.tool(description=f"Return a secret's actual value. Only when {OWNER} explicitly asks to see it.")
    def vault_reveal(name: str) -> str:
        return v.get(name)

    return mcp


def serve():
    mcp = build_server()

    def _sweeper():
        while True:
            time.sleep(60)
            Vault.sweep()

    threading.Thread(target=_sweeper, daemon=True).start()
    mcp.run()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("list")
    g = sub.add_parser("get")
    g.add_argument("name")
    s = sub.add_parser("set")
    s.add_argument("name")
    s.add_argument("--service", default="")
    s.add_argument("--description", default="")
    d = sub.add_parser("delete")
    d.add_argument("name")
    i = sub.add_parser("import-env")
    i.add_argument("names", nargs="+")
    i.add_argument("--service", default="")
    i.add_argument("--description", default="")
    i.add_argument("--env-file", default=str(HERMES_HOME / ".env"))
    a = p.parse_args(argv)
    if a.cmd == "serve":
        return serve()
    v = Vault()
    if a.cmd == "list":
        for r in v.list():
            print(f"{r['name']:40} {r['service']:15} {r['updated']}  {r['description']}")
    elif a.cmd == "get":
        print(v.get(a.name))
    elif a.cmd == "set":
        print(v.save(a.name, sys.stdin.readline().rstrip("\n"), a.service, a.description))
    elif a.cmd == "delete":
        print(v.delete(a.name))
    elif a.cmd == "import-env":
        env = {}
        for line in Path(a.env_file).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, val = line.split("=", 1)
                env[k.strip()] = val.strip().strip('"').strip("'")
        for n in a.names:
            print(v.save(n, env[n], a.service, a.description) if n in env else f"missing {n}")


if __name__ == "__main__":
    main()
