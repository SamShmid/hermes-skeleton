"""sofos-tasks: the /todo slash command (/tasks is a built-in alias of /agents). Runs vault/sofos_mcp.py in the vault venv (the gateway
process does not have the vault's crypto deps), so the Sofos password never enters this process."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
PY = HOME / "vault/.venv/bin/python"
SCRIPT = HOME / "vault/sofos_mcp.py"
VIEWS = ("open", "overdue", "today", "week", "dated", "done", "all")
HELP = ("/todo: overdue, today, this week and undated high-priority tasks\n"
        "/todo <view> [where]: view is open, overdue, today, week, dated, done or all; "
        "where is a workspace or project (e.g. /todo open Homelab)\n"
        "/todo find <words>: search open tasks\n"
        "To add or change tasks, just say it in chat (e.g. \"add a task: renew passport, due Nov 1\").")


def run(args: list[str]) -> str:
    env = {**os.environ, "VAULT_OWNER_NAME": os.environ.get("VAULT_OWNER_NAME", "the owner")}
    try:
        r = subprocess.run([str(PY), str(SCRIPT), *args], capture_output=True, text=True, timeout=90, env=env)
    except subprocess.TimeoutExpired:
        return "Sofos did not answer in time."
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return (r.stderr or "Sofos error").strip()[-500:]
    return out or "Nothing."


def cmd_tasks(raw: str) -> str:
    words = (raw or "").strip().split()
    if not words:
        return "**Tasks**\n" + run(["briefing"])
    head = words[0].lower()
    if head in ("help", "-h", "?"):
        return HELP
    if head in ("find", "search") and len(words) > 1:
        return run(["tasks", "open", "--query", " ".join(words[1:]), "--limit", "30"])
    if head in VIEWS:
        args = ["tasks", head, "--limit", "40"]
        if len(words) > 1:
            args += ["--where", " ".join(words[1:])]
        return run(args)
    return run(["tasks", "open", "--where", " ".join(words), "--limit", "40"])


def register(ctx):
    ctx.register_command("todo", cmd_tasks, description="Sofos tasks: due soon, or a list (try /todo help)",
                         args_hint="[help | open | overdue | today | week | done | find <words>] [where]")
