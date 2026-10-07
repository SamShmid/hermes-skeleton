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


if __name__ == "__main__":
    unittest.main()
