"""Sofos connector: an MCP server over the Sofos API (a self-hosted task and project app).

Hermes logs in as its own agent account (register it with isAgent=true; the owner shares their account
with it as writer). The password lives ENCRYPTED
in the password vault (vault_mcp.Vault, default name SOFOS_HERMES_PASSWORD). It is read in code and
never printed or returned by any tool.

Tasks only: list, add, edit, complete and reopen. No deletes (the owner uses the Sofos trash for that).

  sofos_mcp.py serve                       run the MCP server (stdio) - what Hermes starts
  sofos_mcp.py overview                    workspaces and projects with open-task counts
  sofos_mcp.py tasks [VIEW] [--where W] [--json]
                                           VIEW: open (default), overdue, today, week, dated, done, all
  sofos_mcp.py briefing [--json]           overdue / today / this week (dated or high priority only)

Environment: SOFOS_URL (default http://localhost:3000), SOFOS_USER (default hermes),
SOFOS_PASSWORD_NAME (vault entry), SOFOS_TZ (default America/New_York), VAULT_OWNER_NAME, VAULT_HOME.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_mcp  # noqa: E402

OWNER = vault_mcp.OWNER
BASE = ((os.environ.get("SOFOS_URL") or "").strip() or "http://localhost:3000").rstrip("/")
USER = (os.environ.get("SOFOS_USER") or "").strip() or "hermes"
PASSWORD_NAME = (os.environ.get("SOFOS_PASSWORD_NAME") or "").strip() or "SOFOS_HERMES_PASSWORD"
TZ = ZoneInfo((os.environ.get("SOFOS_TZ") or "").strip() or "America/New_York")
ID_RE = re.compile(r"^[0-9a-f]{24}$")
PRIORITIES = ("low", "medium", "high")
VIEWS = ("open", "overdue", "today", "week", "dated", "done", "all")
CACHE_SECONDS = 300


class SofosError(Exception):
    def __init__(self, msg: str, status: int = 0, code: str = ""):
        super().__init__(msg)
        self.status = status
        self.code = code


# ---- dates ---------------------------------------------------------------------------------------
def parse_due(s: str) -> tuple[str | None, bool]:
    """'2026-10-09' -> all-day (midnight UTC, the Sofos convention); '2026-10-09 14:30' or an ISO
    timestamp -> a real instant (local time if no offset). 'none'/'clear' -> (None, False)."""
    s = (s or "").strip()
    if s.lower() in ("none", "clear", "null", "no due date"):
        return None, False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        dt.date.fromisoformat(s)
        return f"{s}T00:00:00.000Z", False
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), True


def due_of(task: dict) -> tuple[dt.date | None, dt.datetime | None]:
    """(due date in local terms, exact instant or None for all-day)."""
    raw = task.get("dueAt")
    if not raw:
        return None, None
    t = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if task.get("dueHasTime"):
        return t.astimezone(TZ).date(), t
    return t.astimezone(dt.timezone.utc).date(), None


def due_label(task: dict) -> str:
    d, t = due_of(task)
    if not d:
        return ""
    s = f"{d:%a %b} {d.day}"
    if t:
        lt = t.astimezone(TZ)
        s += f", {lt.hour % 12 or 12}:{lt:%M %p}"
    return s


def is_done(task: dict) -> bool:
    return bool(task.get("completed")) or task.get("status") == "done"


def in_view(task: dict, view: str, now: dt.datetime) -> bool:
    if view == "all":
        return True
    if view == "done":
        return is_done(task)
    if is_done(task):
        return False
    if view == "open":
        return True
    d, t = due_of(task)
    if not d:
        return False
    today = now.astimezone(TZ).date()
    if view == "dated":
        return True
    if view == "overdue":
        return (t < now) if t else d < today
    if view == "today":
        return d == today
    if view == "week":
        return today <= d <= today + dt.timedelta(days=7)
    raise ValueError(f"view must be one of {', '.join(VIEWS)}")


# ---- API -----------------------------------------------------------------------------------------
class Sofos:
    """API access. `http` (a requests-like session) and `password` are injectable for offline tests."""

    def __init__(self, vault=None, password: str | None = None, http=None, base: str = BASE):
        self._password = password
        self._vault = vault
        if http is None:
            import requests
            http = requests.Session()
        self.http = http
        self.base = base.rstrip("/") + "/api"
        self._token = None
        self._tree = None
        self._tree_at = 0.0

    def _login(self):
        if self._password is None:
            try:
                self._password = (self._vault or vault_mcp.Vault()).get(PASSWORD_NAME)
            except Exception as e:  # noqa: BLE001
                raise SofosError(f"Sofos password unavailable from the vault ({PASSWORD_NAME}): {e}")
        r = self.http.post(self.base + "/auth/login", json={"username": USER, "password": self._password}, timeout=30)
        if r.status_code != 200:
            raise SofosError(f"Sofos login failed (HTTP {r.status_code})", r.status_code)
        self._token = r.json()["token"]

    def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        for attempt in (1, 2):
            if not self._token:
                self._login()
            r = self.http.request(method, self.base + path, json=body, params=params, timeout=30,
                                  headers={"Authorization": f"Bearer {self._token}"})
            if r.status_code == 401 and attempt == 1:
                self._token = None
                continue
            if r.status_code >= 400:
                try:
                    err = r.json().get("error") or {}
                except ValueError:
                    err = {}
                msg = err.get("message") or f"HTTP {r.status_code}"
                if err.get("details") and isinstance(err["details"], list):
                    msg += ": " + "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in err["details"][:3])
                raise SofosError(msg, r.status_code, err.get("code", ""))
            return r.json() if r.content else {}
        raise SofosError("Sofos session could not be refreshed")

    # ---- containers --------------------------------------------------------------------------------
    def tree(self, fresh: bool = False) -> dict:
        """{'workspaces': {id: ws}, 'projects': {id: p}, 'home': id}. Cached for a few minutes."""
        if self._tree and not fresh and time.time() - self._tree_at < CACHE_SECONDS:
            return self._tree
        data = self.call("GET", "/workspaces")["data"]
        wss = {w["id"]: w for w in (data.get("owned") or []) + (data.get("shared") or [])}
        projects = {}
        for wid in wss:
            for p in self.call("GET", "/projects", params={"workspace": wid, "deep": "1"})["data"]:
                projects[p["id"]] = p
        home = next((w for w, v in wss.items() if v.get("isDefault")), next(iter(wss), None))
        self._tree, self._tree_at = {"workspaces": wss, "projects": projects, "home": home}, time.time()
        return self._tree

    def path(self, task_or_project: dict) -> str:
        t = self.tree()
        pid = task_or_project.get("project") if "title" in task_or_project else task_or_project.get("id")
        pid = pid.get("_id") if isinstance(pid, dict) else pid
        names = []
        seen = set()
        while pid and pid in t["projects"] and pid not in seen:
            seen.add(pid)
            p = t["projects"][pid]
            names.append(p["name"])
            pid = p.get("parentProject")
            pid = pid.get("_id") if isinstance(pid, dict) else pid
        wid = task_or_project.get("workspace")
        wid = wid.get("_id") if isinstance(wid, dict) else wid
        ws = t["workspaces"].get(wid, {}).get("name", "?")
        return " › ".join([ws] + names[::-1])

    def resolve(self, where: str) -> dict:
        """'' -> Home. Accepts an id, a workspace name, a project name, or a path like
        'Personal Projects/Hermes' (case-insensitive). Returns {'workspace': id} or {'project': id}."""
        t = self.tree()
        w = (where or "").strip()
        if not w:
            return {"workspace": t["home"]}
        if ID_RE.match(w):
            if w in t["projects"]:
                return {"project": w}
            if w in t["workspaces"]:
                return {"workspace": w}
            raise SofosError(f"No workspace or project with id {w}")
        want = [x.strip().lower() for x in re.split(r"\s*(?:/|›|>)\s*", w) if x.strip()]
        cands = [{"workspace": i, "path": v["name"]} for i, v in t["workspaces"].items()]
        cands += [{"project": i, "path": self.path(p)} for i, p in t["projects"].items() if not p.get("archivedAt")]
        def fits(c):
            parts = [x.lower() for x in c["path"].split(" › ")]
            return parts[-len(want):] == want if len(want) <= len(parts) else False
        hits = [c for c in cands if fits(c)]
        if not hits:
            hits = [c for c in cands if want[-1] in c["path"].split(" › ")[-1].lower()]
        if len(hits) == 1:
            hits[0].pop("path")
            return hits[0]
        if not hits:
            raise SofosError(f"No workspace or project matches '{where}'. Call sofos_overview to see the names.")
        raise SofosError(f"'{where}' is ambiguous: " + "; ".join(c["path"] for c in hits[:8]) + ". Use the full path.")

    # ---- tasks -------------------------------------------------------------------------------------
    def tasks(self, view: str = "open", where: str = "", query: str = "", now: dt.datetime | None = None) -> list:
        if view not in VIEWS:
            raise SofosError(f"view must be one of {', '.join(VIEWS)}")
        params = self.resolve(where) if where else None
        rows = self.call("GET", "/tasks", params=params)["data"]
        now = now or dt.datetime.now(dt.timezone.utc)
        q = (query or "").lower().strip()
        rows = [r for r in rows if in_view(r, view, now)
                and (not q or q in r.get("title", "").lower() or q in (r.get("content") or "").lower())]
        far = dt.date(9999, 1, 1)
        prank = {"high": 0, "medium": 1, "low": 2}
        rows.sort(key=lambda r: (due_of(r)[0] or far, prank.get(r.get("priority"), 3), r.get("createdAt", "")))
        return rows

    def get(self, task_id: str) -> dict:
        return self.call("GET", f"/tasks/{_id(task_id)}")["data"]

    def add(self, title: str, where: str = "", due: str = "", priority: str = "", notes: str = "",
            parent_task_id: str = "") -> dict:
        body = {"title": title.strip()[:120]}
        if parent_task_id:
            parent = self.get(parent_task_id)
            body["parentTask"] = parent["id"]
            body.update(_container(parent))
        else:
            body.update(self.resolve(where))
        if due:
            body["dueAt"], body["dueHasTime"] = parse_due(due)
        if priority:
            body["priority"] = _priority(priority)
        if notes:
            body["content"] = notes
        return self.call("POST", "/tasks", body)["data"]

    def update(self, task_id: str, **fields) -> dict:
        body = {}
        if fields.get("title"):
            body["title"] = fields["title"].strip()[:120]
        if fields.get("due"):
            body["dueAt"], body["dueHasTime"] = parse_due(fields["due"])
        if fields.get("priority"):
            body["priority"] = None if fields["priority"].lower() in ("none", "clear") else _priority(fields["priority"])
        if fields.get("notes") is not None and fields.get("notes") != "":
            body["content"] = fields["notes"]
        if fields.get("status"):
            if fields["status"] not in ("todo", "in_progress"):
                raise SofosError("status must be todo or in_progress (use complete/reopen for done)")
            body["status"] = fields["status"]
        if fields.get("where"):
            body.update(self.resolve(fields["where"]))  # Sofos re-files on either key
        if not body:
            raise SofosError("Nothing to change")
        return self.call("PATCH", f"/tasks/{_id(task_id)}", body)["data"]

    def complete(self, task_id: str, include_subtasks: bool = False) -> dict:
        body = {"completed": True}
        if include_subtasks:
            body["completeSubtasks"] = True
        return self.call("PATCH", f"/tasks/{_id(task_id)}", body)["data"]

    def reopen(self, task_id: str) -> dict:
        return self.call("PATCH", f"/tasks/{_id(task_id)}", {"completed": False})["data"]

    def briefing(self, now: dt.datetime | None = None) -> dict:
        """Overdue / today / rest of the week, plus undated high-priority tasks."""
        now = now or dt.datetime.now(dt.timezone.utc)
        rows = self.tasks("open", now=now)
        out = {"overdue": [], "today": [], "week": [], "high": []}
        for r in rows:
            if in_view(r, "overdue", now):
                out["overdue"].append(r)
            elif in_view(r, "today", now):
                out["today"].append(r)
            elif in_view(r, "week", now):
                out["week"].append(r)
            elif not r.get("dueAt") and r.get("priority") == "high":
                out["high"].append(r)
        return out


def _id(task_id: str) -> str:
    t = (task_id or "").strip()
    if not ID_RE.match(t):
        raise SofosError(f"'{task_id}' is not a Sofos task id (24 hex characters)")
    return t


def _priority(p: str) -> str:
    p = p.strip().lower()
    if p not in PRIORITIES:
        raise SofosError("priority must be low, medium or high")
    return p


def _container(task: dict) -> dict:
    proj = task.get("project")
    proj = proj.get("_id") if isinstance(proj, dict) else proj
    if proj:
        return {"project": proj}
    ws = task.get("workspace")
    return {"workspace": ws.get("_id") if isinstance(ws, dict) else ws}


# ---- formatting ----------------------------------------------------------------------------------
def line(s: Sofos, r: dict, show_path: bool = True) -> str:
    bits = []
    if r.get("dueAt"):
        bits.append("due " + due_label(r))
    if r.get("priority"):
        bits.append(r["priority"])
    if r.get("status") == "in_progress":
        bits.append("in progress")
    if is_done(r):
        bits.append("done")
    if r.get("parentTask"):
        bits.append("subtask")
    if show_path:
        bits.append(s.path(r))
    return f"[{r['id']}] {r['title']}" + (" — " + " · ".join(bits) if bits else "")


def task_list_text(s: Sofos, rows: list, limit: int) -> str:
    if not rows:
        return "No matching tasks."
    shown = rows[:limit]
    text = "\n".join(line(s, r) for r in shown)
    if len(rows) > limit:
        text += f"\n…and {len(rows) - limit} more (narrow with `where`, `query` or a view)."
    return f"{len(rows)} task(s):\n" + text


def overview_text(s: Sofos) -> str:
    t = s.tree(fresh=True)
    rows = s.call("GET", "/tasks")["data"]
    counts = {}
    for r in rows:
        if is_done(r):
            continue
        key = _container(r)
        counts[key.get("project") or key.get("workspace")] = counts.get(key.get("project") or key.get("workspace"), 0) + 1
    out = []
    for wid, w in sorted(t["workspaces"].items(), key=lambda kv: kv[1]["name"]):
        direct = sum(1 for r in rows if not is_done(r) and not _container(r).get("project")
                     and _container(r).get("workspace") == wid)
        out.append(f"{w['name']} [{wid}] — {direct} open task(s) directly in the workspace")
        kids = {}
        for pid, p in t["projects"].items():
            if p.get("archivedAt") or p.get("workspace") != wid:
                continue
            parent = p.get("parentProject")
            kids.setdefault(parent.get("_id") if isinstance(parent, dict) else parent, []).append(pid)

        def walk(parent, depth):
            for pid in sorted(kids.get(parent, []), key=lambda i: t["projects"][i]["name"].lower()):
                p = t["projects"][pid]
                st = "" if p.get("status") in (None, "active") else f" ({p['status']})"
                out.append(f"{'  ' * depth}- {p['name']}{st} [{pid}] — {counts.get(pid, 0)} open")
                walk(pid, depth + 1)
        walk(None, 1)
    return "\n".join(out)


def briefing_text(s: Sofos) -> str:
    b = s.briefing()
    parts = []
    for key, title in (("overdue", "Overdue"), ("today", "Today"), ("week", "This week"), ("high", "High priority, no date")):
        if b[key]:
            parts.append(f"{title}:\n" + "\n".join("- " + line(s, r) for r in b[key]))
    return "\n\n".join(parts) or "Nothing overdue, due this week, or high priority."


# ---- MCP -----------------------------------------------------------------------------------------
def build_server(s: Sofos | None = None):
    """The FastMCP server with the Sofos tools registered (not started)."""
    from mcp.server.fastmcp import FastMCP

    s = s or Sofos()
    mcp = FastMCP("sofos")

    def safe(fn):
        try:
            return fn()
        except SofosError as e:
            if e.code == "INCOMPLETE_SUBTASKS":
                return (f"Not completed: {e}. Ask {OWNER} whether to complete the whole task tree; only if they "
                        "say yes, call sofos_complete_task again with include_subtasks=true.")
            return f"Sofos error: {e}"
        except ValueError as e:
            return f"Invalid input: {e}"

    @mcp.tool(description=f"{OWNER}'s Sofos (task/project app) layout: every workspace and project (nested), with "
                          "ids and open-task counts. Use it to pick where a task belongs.")
    def sofos_overview() -> str:
        return safe(lambda: overview_text(s))

    @mcp.tool(description="List Sofos tasks. view: open (default), overdue, today, week (next 7 days), dated "
                          "(any due date), done, all. where: optional workspace/project name, path like "
                          "'Personal Projects/Hermes', or id (includes sub-projects). query: optional text filter. "
                          "Each line starts with the task id in brackets.")
    def sofos_tasks(view: str = "open", where: str = "", query: str = "", limit: int = 40) -> str:
        return safe(lambda: task_list_text(s, s.tasks(view, where, query), max(1, min(int(limit), 200))))

    @mcp.tool(description=f"Add a task to {OWNER}'s Sofos. where: workspace/project name, path or id; empty = the "
                          "Home workspace (use it when the right place is unclear). due: 'YYYY-MM-DD' (all-day) or "
                          "'YYYY-MM-DD HH:MM' (New York time). priority: low|medium|high. notes: markdown details. "
                          "parent_task_id: make it a subtask (it then lives where the parent lives).")
    def sofos_add_task(title: str, where: str = "", due: str = "", priority: str = "", notes: str = "",
                       parent_task_id: str = "") -> str:
        return safe(lambda: "Added " + line(s, s.add(title, where, due, priority, notes, parent_task_id)))

    @mcp.tool(description="Edit a Sofos task by id. Only the fields you pass change. due: 'YYYY-MM-DD', "
                          "'YYYY-MM-DD HH:MM' or 'none' to clear. priority: low|medium|high|none. status: todo|"
                          "in_progress. notes replaces the description. where moves it to another workspace/project.")
    def sofos_update_task(task_id: str, title: str = "", due: str = "", priority: str = "", notes: str = "",
                          status: str = "", where: str = "") -> str:
        return safe(lambda: "Updated " + line(s, s.update(task_id, title=title, due=due, priority=priority,
                                                          notes=notes or None, status=status, where=where)))

    @mcp.tool(description="Mark a Sofos task done. If it has unfinished subtasks Sofos refuses; then ask "
                          f"{OWNER} and only with their yes call again with include_subtasks=true.")
    def sofos_complete_task(task_id: str, include_subtasks: bool = False) -> str:
        return safe(lambda: "Completed " + line(s, s.complete(task_id, include_subtasks)))

    @mcp.tool(description="Reopen a completed Sofos task (Sofos also reopens its completed parent tasks).")
    def sofos_reopen_task(task_id: str) -> str:
        return safe(lambda: "Reopened " + line(s, s.reopen(task_id)))

    @mcp.tool(description="Task digest for briefings and 'what's on my plate': overdue, due today, due in the next "
                          "7 days, and undated high-priority tasks.")
    def sofos_briefing() -> str:
        return safe(lambda: briefing_text(s))

    return mcp


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sofos connector for Hermes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("overview")
    t = sub.add_parser("tasks")
    t.add_argument("view", nargs="?", default="open", choices=VIEWS)
    t.add_argument("--where", default="")
    t.add_argument("--query", default="")
    t.add_argument("--limit", type=int, default=60)
    t.add_argument("--json", action="store_true")
    b = sub.add_parser("briefing")
    b.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        build_server().run()
        return 0
    s = Sofos()
    try:
        if a.cmd == "overview":
            print(overview_text(s))
        elif a.cmd == "tasks":
            rows = s.tasks(a.view, a.where, a.query)
            if a.json:
                print(json.dumps([{**r, "path": s.path(r)} for r in rows], default=str))
            else:
                print(task_list_text(s, rows, a.limit))
        elif a.cmd == "briefing":
            if a.json:
                print(json.dumps({k: [{"id": r["id"], "title": r["title"], "due": due_label(r),
                                       "priority": r.get("priority"), "path": s.path(r)} for r in v]
                                  for k, v in s.briefing().items()}))
            else:
                print(briefing_text(s))
    except SofosError as e:
        print(f"Sofos error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
