"""Calendly connector + calendly_watch: offline tests with a fake HTTP session (no network, no real token).

Needs the vault venv (cryptography, mcp); skipped otherwise.
"""
import asyncio
import datetime as dt
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

VAULT_DIR = Path(__file__).resolve().parent.parent / "vault"
if not (VAULT_DIR / "calendly_mcp.py").exists():  # deployed layout: $HERMES_HOME/vault/tests/
    VAULT_DIR = VAULT_DIR.parent
SCRIPTS_DIR = VAULT_DIR.parent / "scripts"
try:
    import cryptography  # noqa: F401
    import mcp.server.fastmcp  # noqa: F401
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

AT = "@"
API = "https://api.calendly.com"
ME = f"{API}/users/U1"
ET = f"{API}/event_types/ET1"
NOW = dt.datetime.now(dt.timezone.utc)


def ts(delta_hours: float) -> str:
    return (NOW + dt.timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def event(uid, start_h, status="active", created_h=-48, cancel=None):
    e = {"uri": f"{API}/scheduled_events/{uid}", "name": "30 Minute Meeting", "status": status,
         "start_time": ts(start_h), "end_time": ts(start_h + 0.5), "created_at": ts(created_h),
         "updated_at": ts(created_h), "event_type": ET,
         "location": {"type": "google_conference", "join_url": "https://meet.example/abc"}}
    if cancel is not None:
        e["cancellation"] = {"canceled_by": "Pat Guest", "reason": cancel, "created_at": ts(-1)}
    return e


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeHTTP:
    """Routes Calendly API calls to canned data and records every request."""

    def __init__(self, events=None, fail=None):
        self.events, self.calls, self.fail = list(events or []), [], fail

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json, headers))
        if self.fail:
            return Resp(self.fail, {"title": "Nope", "message": "denied"})
        if url == f"{API}/users/me":
            return Resp(200, {"resource": {"uri": ME, "name": "Test Owner", "timezone": "America/New_York",
                                           "scheduling_url": "https://calendly.example/owner"}})
        if url == f"{API}/event_types":
            return Resp(200, {"collection": [
                {"uri": ET, "name": "30 Minute Meeting", "slug": "30min", "duration": 30, "active": True,
                 "secret": False, "scheduling_url": "https://calendly.example/owner/30min"},
                {"uri": f"{API}/event_types/ET2", "name": "Old", "slug": "old", "duration": 60, "active": False,
                 "secret": False, "scheduling_url": "https://calendly.example/owner/old"}],
                "pagination": {"next_page": None}})
        if url == f"{API}/scheduled_events":
            evs = [e for e in self.events if not params.get("status") or e["status"] == params["status"]]
            evs = [e for e in evs if params["min_start_time"] <= e["start_time"] <= params["max_start_time"]]
            return Resp(200, {"collection": evs, "pagination": {"next_page": None}})
        if url.endswith("/invitees"):
            return Resp(200, {"collection": [{"name": "Pat Guest", "email": f"pat{AT}example.com",
                                              "status": "active", "cancel_url": "https://calendly.example/c/1",
                                              "reschedule_url": "https://calendly.example/r/1",
                                              "questions_and_answers": [{"question": "Notes?", "answer": "hi"}]}],
                              "pagination": {"next_page": None}})
        if url == f"{API}/user_availability_schedules":
            return Resp(200, {"collection": [{"name": "Working hours", "default": True,
                                              "timezone": "America/New_York", "rules": [
                {"type": "wday", "wday": "monday", "intervals": [{"from": "09:00", "to": "17:00"}]},
                {"type": "wday", "wday": "sunday", "intervals": []},
                {"type": "date", "date": "2099-01-02", "intervals": []},
                {"type": "date", "date": "2000-01-02", "intervals": []}]}]})
        if url == f"{API}/scheduling_links" and method == "POST":
            return Resp(201, {"resource": {"booking_url": "https://calendly.example/d/xyz", "owner": json["owner"]}})
        if url.endswith("/cancellation") and method == "POST":
            return Resp(201, {"resource": {"reason": json["reason"], "canceled_by": "Test Owner"}})
        return Resp(404, {"title": "Not found", "message": url})


def load(name):
    for p in (str(VAULT_DIR), str(SCRIPTS_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    for m in ("vault_mcp", "calendly_mcp", "calendly_watch"):
        sys.modules.pop(m, None)
    return importlib.import_module(name)


@unittest.skipUnless(HAVE_DEPS, "vault venv deps missing")
class CalendlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"VAULT_HOME": self.tmp.name, "CALENDLY_TZ": ""})
        self.env.start()
        self.cm = load("calendly_mcp")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def client(self, **kw):
        self.http = FakeHTTP(**kw)
        return self.cm.Calendly(token="fake-token", http=self.http)

    def test_headers_include_user_agent_and_accept(self):
        self.client().links()
        hdr = self.http.calls[0][4]
        self.assertTrue(hdr["User-Agent"].startswith("hermes-calendly"))
        self.assertEqual(hdr["Accept"], "application/json")
        self.assertEqual(hdr["Authorization"], "Bearer fake-token")

    def test_token_read_from_vault(self):
        v = self.cm.vault_mcp.Vault()
        v.save("CALENDLY_TOKEN", "vault-token", "calendly")
        http = FakeHTTP()
        self.cm.Calendly(vault=v, http=http).me()
        self.assertEqual(http.calls[0][4]["Authorization"], "Bearer vault-token")

    def test_missing_token_is_clear_error(self):
        c = self.cm.Calendly(vault=self.cm.vault_mcp.Vault(), http=FakeHTTP())
        with self.assertRaisesRegex(RuntimeError, "no Calendly token"):
            c.me()

    def test_api_error_does_not_leak_token(self):
        with self.assertRaises(RuntimeError) as cm:
            self.client(fail=403).me()
        self.assertIn("403", str(cm.exception))
        self.assertNotIn("fake-token", str(cm.exception))

    def test_refuses_non_calendly_url(self):
        with self.assertRaises(ValueError):
            self.client().req("GET", "https://evil.example/x")

    def test_links(self):
        out = self.client().links()
        self.assertEqual(out["main_link"], "https://calendly.example/owner")
        self.assertEqual([t["slug"] for t in out["event_types"]], ["30min", "old"])
        self.assertEqual(out["event_types"][0]["duration_min"], 30)

    def test_upcoming_local_time_and_invitees(self):
        c = self.client(events=[event("E1", 5), event("E2", 6, status="canceled", cancel="x"), event("E3", 24 * 40)])
        rows = c.upcoming(14)
        self.assertEqual([r["uuid"] for r in rows], ["E1"])
        r = rows[0]
        self.assertTrue(r["start"].endswith(("-04:00", "-05:00")))
        self.assertEqual(r["invitees"][0]["cancel_url"], "https://calendly.example/c/1")
        self.assertEqual(r["location"]["join_url"], "https://meet.example/abc")
        self.assertRegex(r["when"], r"^\w{3} \w{3} \d{1,2}, \d{1,2}:\d{2} [AP]M$")

    def test_tz_override(self):
        self.http = FakeHTTP(events=[event("E1", 5)])
        c = self.cm.Calendly(token="t", http=self.http, tz="UTC")
        self.assertTrue(c.upcoming(2)[0]["start"].endswith("+00:00"))

    def test_recent_changes(self):
        c = self.client(events=[event("NEW", 30, created_h=-2), event("OLD", 30, created_h=-100),
                                event("CAN", 40, status="canceled", created_h=-100, cancel="conflict")])
        out = c.recent_changes(24)
        self.assertEqual([e["uuid"] for e in out["booked"]], ["NEW"])
        self.assertEqual([e["uuid"] for e in out["canceled"]], ["CAN"])
        self.assertEqual(out["canceled"][0]["cancellation"]["reason"], "conflict")

    def test_availability(self):
        s = self.client().availability()[0]
        self.assertEqual(s["weekly"]["Monday"], "09:00-17:00")
        self.assertEqual(s["weekly"]["Sunday"], "unavailable")
        self.assertEqual(s["weekly"]["Friday"], "unavailable")
        self.assertEqual([o["date"] for o in s["date_overrides"]], ["2099-01-02"])

    def test_single_use_link(self):
        c = self.client()
        out = c.single_use_link("30min")
        self.assertEqual(out["booking_url"], "https://calendly.example/d/xyz")
        method, url, _, body, _ = self.http.calls[-1]
        self.assertEqual((method, url), ("POST", f"{API}/scheduling_links"))
        self.assertEqual(body, {"max_event_count": 1, "owner": ET, "owner_type": "EventType"})
        self.assertEqual(c.single_use_link("")["event_type"], "30 Minute Meeting")  # only active type
        with self.assertRaises(ValueError):
            c.single_use_link("old")  # inactive

    def test_cancel(self):
        c = self.client()
        out = c.cancel(f"{API}/scheduled_events/abcd-1234-ef", "sick")
        self.assertEqual(out["canceled"], "abcd-1234-ef")
        self.assertEqual(self.http.calls[-1][1], f"{API}/scheduled_events/abcd-1234-ef/cancellation")
        with self.assertRaises(ValueError):
            c.cancel("../../users/me", "x")

    def test_only_expected_writes(self):
        c = self.client(events=[event("E1", 5)])
        c.links(); c.upcoming(); c.recent_changes(); c.availability()
        self.assertEqual({m for m, *_ in self.http.calls}, {"GET"})

    def test_tools_registered(self):
        srv = self.cm.build_server(self.client())
        names = {t.name for t in asyncio.run(srv.list_tools())}
        self.assertEqual(names, {"calendly_links", "calendly_upcoming", "calendly_recent_changes",
                                 "calendly_availability", "calendly_single_use_link", "calendly_cancel"})
        desc = {t.name: t.description for t in asyncio.run(srv.list_tools())}
        self.assertIn("ONLY when", desc["calendly_cancel"])


@unittest.skipUnless(HAVE_DEPS and (SCRIPTS_DIR / "calendly_watch.py").exists(), "watch script/deps missing")
class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state" / "calendly_watch.json"
        self.env = mock.patch.dict(os.environ, {"VAULT_HOME": self.tmp.name, "CALENDLY_TZ": "",
                                                "CALENDLY_WATCH_STATE": str(self.state)})
        self.env.start()
        self.w = load("calendly_watch")
        self.cm = importlib.import_module("calendly_mcp")
        self.http = FakeHTTP(events=[event("E1", 5)])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def tick(self):
        import calendly_mcp
        c = calendly_mcp.Calendly(token="t", http=self.http)
        with mock.patch.object(calendly_mcp, "Calendly", return_value=c), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            rc = self.w.main()
        return rc, out.getvalue()

    def test_first_run_silent_then_new_and_cancel(self):
        self.assertEqual(self.tick(), (0, ""))
        self.assertIn("E1", json.loads(self.state.read_text())["events"])
        self.assertEqual(self.tick(), (0, ""))  # nothing changed
        self.http.events.append(event("E2", 50, created_h=-0.1))
        rc, out = self.tick()
        self.assertRegex(out.strip(), r"^📅 New Calendly booking: Pat Guest · \w{3} \w{3} \d{1,2}, \d{1,2}:\d{2} [AP]M"
                                      r" · 30 Minute Meeting$")
        self.http.events[0] = event("E1", 5, status="canceled", cancel="conflict")
        rc, out = self.tick()
        self.assertTrue(out.startswith("❌ Calendly booking canceled: Pat Guest · "))
        self.assertTrue(out.strip().endswith("— conflict"))
        self.assertEqual(self.tick(), (0, ""))

    def test_failures_tolerated_then_reported(self):
        self.tick()
        self.http.fail = 500
        self.assertEqual(self.tick(), (0, ""))
        self.assertEqual(self.tick(), (0, ""))
        rc, _ = self.tick()
        self.assertEqual(rc, 1)
        self.http.fail = None
        self.assertEqual(self.tick(), (0, ""))
        self.assertEqual(json.loads(self.state.read_text())["failures"], 0)

    def test_old_events_pruned(self):
        msgs, st = self.w.run(self.cm.Calendly(token="t", http=self.http),
                              {"events": {"GONE": {"status": "active", "start": ts(-24 * 30)}}}, NOW)
        self.assertNotIn("GONE", st["events"])
        self.assertIn("E1", st["events"])


if __name__ == "__main__":
    unittest.main()
