import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("gate", Path(__file__).resolve().parents[1] / "__init__.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
hook = gate._on_pre_tool_call


class GateTests(unittest.TestCase):
    def test_send_always_asks_with_fresh_key(self):
        a = hook("mcp__google__gmail_send_draft", {"account": "p", "to": "x", "subject": "Hi"})
        b = hook("mcp__google__gmail_send_draft", {"account": "p", "to": "x", "subject": "Hi"})
        self.assertEqual(a["action"], "approve")
        self.assertIn("Subject: Hi", a["message"])
        self.assertNotEqual(a["rule_key"], b["rule_key"])

    def test_rsvp_asks(self):
        r = hook("mcp__google__calendar_rsvp", {"account": "p", "response": "declined", "title": "Standup"})
        self.assertEqual(r["action"], "approve")
        self.assertIn("declined", r["message"])
        self.assertIn("Standup", r["message"])

    def test_event_asks_only_with_guests(self):
        self.assertIsNone(hook("mcp__google__calendar_create_event", {"title": "Gym", "guests": ""}))
        r = hook("mcp__google__calendar_create_event", {"title": "Call", "start": "2026-11-05T10:00",
                                                         "account": "p", "guests": "a, b"})
        self.assertEqual(r["action"], "approve")
        self.assertIn("invite a, b", r["message"])

    def test_other_tools_pass(self):
        self.assertIsNone(hook("mcp__google__calendar_list", {}))
        self.assertIsNone(hook("terminal", None))


if __name__ == "__main__":
    unittest.main()
