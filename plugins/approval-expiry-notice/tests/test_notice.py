"""Offline tests (no Hermes imports needed): python3 -m unittest discover -s plugins/approval-expiry-notice/tests"""
import importlib.util
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("aen", os.path.join(HERE, "..", "__init__.py"))
aen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aen)


class T(unittest.TestCase):
    def test_channel_parse(self):
        self.assertEqual(aen.discord_channel("agent:main:discord:group:4242:7"),
                         "4242")
        self.assertEqual(aen.discord_channel("agent:main:discord:dm:123:456"), "123")
        self.assertIsNone(aen.discord_channel("agent:main:telegram:dm:123"))
        self.assertIsNone(aen.discord_channel(""))

    def test_text(self):
        t = aen.notice_text("rm /tmp/approval-test.txt")
        self.assertIn("approve", t)
        self.assertIn("deny", t)
        self.assertIn("rm /tmp/approval-test.txt", t)

    def test_fires_only_when_pending(self):
        posted = []
        with mock.patch.object(aen, "_button_timeout", return_value=-4.9), \
             mock.patch.object(aen, "_post", side_effect=lambda c, t: posted.append(c)), \
             mock.patch.object(aen, "_still_pending", return_value=True):
            aen._on_request(session_key="agent:main:discord:group:42:7", command="rm x")
            time.sleep(0.5)
        self.assertEqual(posted, ["42"])

    def test_cancelled_on_response(self):
        posted = []
        with mock.patch.object(aen, "_button_timeout", return_value=-4.0), \
             mock.patch.object(aen, "_post", side_effect=lambda c, t: posted.append(c)), \
             mock.patch.object(aen, "_still_pending", return_value=True):
            aen._on_request(session_key="agent:main:discord:group:43:7", command="rm x")
            aen._on_response(session_key="agent:main:discord:group:43:7")
            time.sleep(1.5)
        self.assertEqual(posted, [])

    def test_no_post_when_resolved(self):
        posted = []
        with mock.patch.object(aen, "_button_timeout", return_value=-4.9), \
             mock.patch.object(aen, "_post", side_effect=lambda c, t: posted.append(c)), \
             mock.patch.object(aen, "_still_pending", return_value=False):
            aen._on_request(session_key="agent:main:discord:group:44:7", command="rm x")
            time.sleep(0.5)
        self.assertEqual(posted, [])


class FakeGateway:
    def __init__(self):
        self.sent = []
        gw = self

        class Adapter:
            async def send(self, chat_id, text, metadata=None):
                gw.sent.append((chat_id, text))
        self.adapter = Adapter()

    def _session_key_for_source(self, source):
        return "agent:main:discord:group:%s:7" % source.chat_id

    def _is_user_authorized_for_source(self, source):
        return True

    def _plaintext_approval_words(self):
        return {"approve": ("approve", ""), "yes": ("approve", ""), "deny": ("deny", "")}

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _thread_metadata_for_source(self, source):
        return None


class Ev:
    def __init__(self, text, command=None):
        self.text, self._command, self.internal = text, command, False
        self.source = type("S", (), {"chat_id": "42", "is_bot": False})()

    def get_command(self):
        return self._command


class QueuedNoticeTests(unittest.TestCase):
    def run_hook(self, gw, ev, pending=True):
        import asyncio, types
        fake = types.ModuleType("tools.approval")
        fake.has_blocking_approval = lambda key: pending
        fake.get_pending_gateway_approval = lambda key: {"command": "python - <<'PY' ..."}
        tools = types.ModuleType("tools")
        tools.approval = fake
        with mock.patch.dict(sys.modules, {"tools": tools, "tools.approval": fake}):
            return asyncio.run(aen._on_dispatch(event=ev, gateway=gw))

    def setUp(self):
        aen._queued.clear()

    def test_announces_and_counts_while_approval_open(self):
        gw = FakeGateway()
        self.assertIsNone(self.run_hook(gw, Ev("where is my preview")))
        self.run_hook(gw, Ev("hello??"))
        self.assertEqual(len(gw.sent), 2)
        self.assertIn("(2 waiting)", gw.sent[1][1])
        self.assertIn("python - <<'PY'", gw.sent[0][1])

    def test_silent_for_answers_commands_and_when_nothing_pending(self):
        gw = FakeGateway()
        self.run_hook(gw, Ev("approve"))
        self.run_hook(gw, Ev("/deny", command="deny"))
        self.run_hook(gw, Ev("hi"), pending=False)
        self.assertEqual(gw.sent, [])

    def test_response_resets_count(self):
        gw = FakeGateway()
        self.run_hook(gw, Ev("a"))
        aen._on_response(session_key="agent:main:discord:group:42:7")
        self.run_hook(gw, Ev("b"))
        self.assertIn("(1 waiting)", gw.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
