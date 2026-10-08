"""Sofos connector: offline tests with a fake HTTP session (no network, no real password).

Run with the vault venv: .venv/bin/python -m unittest tests.test_sofos
"""
import datetime as dt
import sys
from pathlib import Path

import unittest

_VAULT = Path(__file__).resolve().parent.parent / "vault"
sys.path.insert(0, str(_VAULT if (_VAULT / "sofos_mcp.py").exists() else _VAULT.parent))  # repo or deployed layout
import sofos_mcp as m  # noqa: E402

W_HOME, W_PP = "a" * 24, "b" * 24
P_HERMES, P_SUB, P_OLD = "c" * 24, "d" * 24, "e" * 24
NOW = dt.datetime(2026, 10, 8, 16, 0, tzinfo=dt.timezone.utc)  # Thu Oct 8, 12:00 PM New York


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.content = b"x" if data is not None else b""

    def json(self):
        return self._data


class FakeHTTP:
    def __init__(self):
        self.calls, self.logins, self.expire_once = [], 0, False
        self.tasks = [
            {"id": "1" * 24, "title": "Overdue all-day", "dueAt": "2026-10-07T00:00:00.000Z", "dueHasTime": False,
             "workspace": W_PP, "project": P_HERMES},
            {"id": "2" * 24, "title": "Today timed", "dueAt": "2026-10-08T22:00:00.000Z", "dueHasTime": True,
             "workspace": W_PP, "project": P_SUB, "priority": "high"},
            {"id": "3" * 24, "title": "Next week", "dueAt": "2026-10-12T00:00:00.000Z", "dueHasTime": False,
             "workspace": W_HOME},
            {"id": "4" * 24, "title": "Undated high", "priority": "high", "workspace": W_HOME},
            {"id": "5" * 24, "title": "Undated low", "priority": "low", "workspace": W_HOME},
            {"id": "6" * 24, "title": "Done one", "completed": True, "status": "done", "workspace": W_HOME,
             "dueAt": "2026-10-01T00:00:00.000Z"},
        ]

    def post(self, url, json=None, timeout=None):
        self.logins += 1
        return Resp(200 if json["password"] == "pw" else 401, {"token": f"t{self.logins}"})

    def request(self, method, url, json=None, params=None, timeout=None, headers=None):
        self.calls.append((method, url.split("/api", 1)[1], json, params))
        if self.expire_once:
            self.expire_once = False
            return Resp(401, {"error": {"message": "expired"}})
        path = url.split("/api", 1)[1]
        if path == "/workspaces":
            return Resp(200, {"data": {"owned": [], "shared": [
                {"id": W_HOME, "name": "Home", "isDefault": True}, {"id": W_PP, "name": "Personal Projects"}]}})
        if path == "/projects":
            if params["workspace"] == W_PP:
                return Resp(200, {"data": [
                    {"id": P_HERMES, "name": "Hermes", "workspace": W_PP, "parentProject": None},
                    {"id": P_SUB, "name": "Bridge", "workspace": W_PP, "parentProject": P_HERMES},
                    {"id": P_OLD, "name": "Old Hermes", "workspace": W_PP, "parentProject": None,
                     "archivedAt": "2026-01-01"}]})
            return Resp(200, {"data": []})
        if path == "/tasks" and method == "GET":
            return Resp(200, {"data": self.tasks})
        if path == "/tasks" and method == "POST":
            return Resp(201, {"data": {"id": "9" * 24, **json}})
        if path.startswith("/tasks/") and method == "GET":
            return Resp(200, {"data": next(t for t in self.tasks if t["id"] == path[7:])})
        if path.startswith("/tasks/") and method == "PATCH":
            if json.get("completed") and path[7:] == "2" * 24 and not json.get("completeSubtasks"):
                return Resp(409, {"error": {"code": "INCOMPLETE_SUBTASKS", "message": "This task has 2 unfinished subtasks."}})
            t = next(t for t in self.tasks if t["id"] == path[7:])
            return Resp(200, {"data": {**t, **json}})
        return Resp(404, {"error": {"message": "nope"}})


def ids(rows):
    return [r["id"][0] for r in rows]


class SofosTests(unittest.TestCase):
    def setUp(self):
        self.s = m.Sofos(password="pw", http=FakeHTTP(), base="https://x")

    def test_parse_due(self):
        assert m.parse_due("2026-10-09") == ("2026-10-09T00:00:00.000Z", False)
        assert m.parse_due("2026-10-09 14:30") == ("2026-10-09T18:30:00.000Z", True)  # EDT
        assert m.parse_due("2026-12-09 14:30") == ("2026-12-09T19:30:00.000Z", True)  # EST
        assert m.parse_due("none") == (None, False)
        with self.assertRaises(ValueError):
            m.parse_due("next friday")

    def test_views(self):
        assert ids(self.s.tasks("open", now=NOW)) == ["1", "2", "3", "4", "5"]
        assert ids(self.s.tasks("overdue", now=NOW)) == ["1"]
        assert ids(self.s.tasks("today", now=NOW)) == ["2"]
        assert ids(self.s.tasks("week", now=NOW)) == ["2", "3"]
        assert ids(self.s.tasks("done", now=NOW)) == ["6"]
        assert ids(self.s.tasks("open", query="undated", now=NOW)) == ["4", "5"]

    def test_timed_task_overdue_after_its_time(self):
        later = dt.datetime(2026, 10, 8, 23, 0, tzinfo=dt.timezone.utc)
        assert "2" in ids(self.s.tasks("overdue", now=later))

    def test_briefing(self):
        b = self.s.briefing(now=NOW)
        assert {k: ids(v) for k, v in b.items()} == {"overdue": ["1"], "today": ["2"], "week": ["3"], "high": ["4"]}

    def test_resolve_and_paths(self):
        assert self.s.resolve("") == {"workspace": W_HOME}
        assert self.s.resolve("personal projects") == {"workspace": W_PP}
        assert self.s.resolve("Personal Projects/Hermes/Bridge") == {"project": P_SUB}
        assert self.s.resolve("bridge") == {"project": P_SUB}
        assert self.s.resolve(P_HERMES) == {"project": P_HERMES}
        with self.assertRaises(m.SofosError):
            self.s.resolve("Nothing Like This")
        assert self.s.path(self.s.http.tasks[1]) == "Personal Projects › Hermes › Bridge"

    def test_archived_projects_not_matched(self):
        # "Old Hermes" is archived, so "hermes" matches only the active project.
        assert self.s.resolve("hermes") == {"project": P_HERMES}

    def test_add_defaults_to_home_and_subtask_inherits_container(self):
        t = self.s.add("Buy milk")
        assert t["workspace"] == W_HOME and "project" not in t
        t = self.s.add("Sub", parent_task_id="2" * 24, due="2026-10-10", priority="High")
        assert t["parentTask"] == "2" * 24 and t["project"] == P_SUB
        assert t["dueAt"] == "2026-10-10T00:00:00.000Z" and t["priority"] == "high"

    def test_update_and_move(self):
        self.s.update("1" * 24, due="none", priority="none", where="Home")
        body = self.s.http.calls[-1][2]
        assert body == {"dueAt": None, "dueHasTime": False, "priority": None, "workspace": W_HOME}
        with self.assertRaises(m.SofosError):
            self.s.update("1" * 24)
        with self.assertRaises(m.SofosError):
            self.s.update("1" * 24, status="done")

    def test_bad_ids_and_priority(self):
        with self.assertRaises(m.SofosError):
            self.s.complete("123")
        with self.assertRaises(m.SofosError):
            self.s.add("x", priority="urgent")

    def test_complete_with_subtasks_needs_confirmation(self):
        with self.assertRaises(m.SofosError) as e:
            self.s.complete("2" * 24)
        assert e.exception.code == "INCOMPLETE_SUBTASKS"
        assert self.s.complete("2" * 24, include_subtasks=True)["completed"] is True

    def test_relogin_on_expired_token(self):
        self.s.tasks("open", now=NOW)
        self.s.http.expire_once = True
        self.s.tasks("open", now=NOW)
        assert self.s.http.logins == 2

    def test_wrong_password(self):
        with self.assertRaises(m.SofosError):
            m.Sofos(password="bad", http=FakeHTTP(), base="https://x").tasks()

    def test_mcp_tools_registered(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("mcp missing")
        import asyncio
        srv = m.build_server(self.s)
        names = {t.name for t in asyncio.run(srv.list_tools())}
        assert names == {"sofos_overview", "sofos_tasks", "sofos_add_task", "sofos_update_task",
                         "sofos_complete_task", "sofos_reopen_task", "sofos_briefing"}


if __name__ == "__main__":
    unittest.main()
