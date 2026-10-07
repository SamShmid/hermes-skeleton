"""Offline tests for google-notify (no gateway)."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent


def load(home):
    os.environ["HERMES_HOME"] = home
    spec = importlib.util.spec_from_file_location("gn", HERE / "__init__.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, **kw):
        self.m.EVENTS.mkdir(parents=True, exist_ok=True)
        (self.m.EVENTS / "e1.json").write_text(json.dumps(kw))

    def test_success_reaches_origin_session(self):
        with mock.patch.object(self.m, "_session_key", return_value="agent:main:discord:group:1:2"):
            self.m.on_tool(tool_name="mcp__google__google_connect_start",
                           result="link https://accounts.google.com/x?client_id=a&state=STATE123abc&scope=y")
        self._event(state="STATE123abc", ok=True, email="a@example.com", account="a")
        sent = []
        self.assertEqual(self.m.deliver_pending(lambda t, session_key: sent.append((t, session_key)) or True), 1)
        self.assertEqual(sent[0][1], "agent:main:discord:group:1:2")
        self.assertIn("a@example.com", sent[0][0])
        self.assertFalse(list(self.m.EVENTS.glob("*.json")))

    def test_failure_message(self):
        with mock.patch.object(self.m, "_session_key", return_value="k"):
            self.m.on_tool(tool_name="google_connect_start", result="...&state=S1&...")
        self._event(state="S1", ok=False, error="state mismatch")
        sent = []
        self.m.deliver_pending(lambda t, session_key: sent.append(t) or True)
        self.assertIn("did NOT finish", sent[0])
        self.assertIn("state mismatch", sent[0])

    def test_unknown_state_dropped(self):
        self._event(state="nobody", ok=True)
        sent = []
        self.assertEqual(self.m.deliver_pending(lambda t, session_key: sent.append(t)), 1)
        self.assertEqual(sent, [])

    def test_other_tools_ignored(self):
        with mock.patch.object(self.m, "_session_key", return_value="k"):
            self.m.on_tool(tool_name="gmail_search", result="state=ZZZ")
        self.assertFalse(self.m.ORIGINS.exists())


if __name__ == "__main__":
    unittest.main()
