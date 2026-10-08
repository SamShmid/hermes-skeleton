"""Offline tests for the Hermes bridge MCP server (needs `mcp` and `httpx`; no network)."""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _load(tmp: str):
    os.environ["HERMES_BRIDGE_CONFIG"] = str(Path(tmp) / "config.json")
    os.environ["HERMES_BRIDGE_STATE"] = str(Path(tmp) / "state.json")
    os.environ["HERMES_API_KEY"] = "test-key"
    Path(tmp, "config.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "owner": "Alex"}))
    import hermes_bridge_mcp as b
    return importlib.reload(b)


class FakeResp:
    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _turn(b):
    t = object.__new__(b.Turn)
    t.events, t.parts, t.tools = asyncio.Queue(), [], []
    t.progress_note = ""
    return t


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = _load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_and_owner(self):
        self.assertEqual(self.b.BASE_URL, "http://127.0.0.1:9")
        self.assertEqual(self.b.OWNER, "Alex")
        self.assertIn("Alex", self.b._system_note())

    def test_slug(self):
        self.assertEqual(self.b._slug("Claude Code!"), "claude-code")
        self.assertEqual(self.b._slug("../../etc"), "etc")

    def test_session_rotation_and_follow(self):
        slug, s1 = self.b._session_for("codex")
        self.assertEqual(self.b._session_for("codex")[1], s1)
        self.assertTrue(s1.startswith("bridge-codex-"))
        self.b._remember_session(slug, "compressed-tip")
        self.assertEqual(self.b._session_for("codex")[1], "compressed-tip")
        _, s2 = self.b._session_for("codex", rotate=True)
        self.assertNotEqual(s2, "compressed-tip")

    def test_sse_parsing(self):
        lines = [
            ": keepalive", "",
            'data: {"choices":[{"delta":{"role":"assistant"}}]}', "",
            "event: hermes.tool.progress",
            'data: {"tool":"calendar_list","label":"calendar_list","status":"running"}', "",
            "event: hermes.status", 'data: "compressing"', "",
            'data: {"choices":[{"delta":{"content":"Hello "}}]}', "",
            'data: {"choices":[{"delta":{"content":"there."}}]}', "",
            "event: approval.request",
            'data: {"event":"approval.request","run_id":"chatcmpl-1","command":"rm x","choices":["once","deny"]}', "",
            "data: [DONE]", "",
        ]
        t = _turn(self.b)
        asyncio.run(t._consume(FakeResp(lines)))
        self.assertEqual(t.reply(), "Hello there.")
        self.assertEqual(t.tools, ["calendar_list"])
        kinds = []
        while not t.events.empty():
            kinds.append(t.events.get_nowait())
        approvals = [d for k, d in kinds if k == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["run_id"], "chatcmpl-1")

    def test_stream_error_raises(self):
        t = _turn(self.b)
        with self.assertRaises(RuntimeError):
            asyncio.run(t._consume(FakeResp(['data: {"error":{"message":"boom"}}', ""])))

    def test_tools_registered(self):
        names = {t.name for t in asyncio.run(self.b.mcp.list_tools())}
        self.assertEqual(names, {"ask_hermes", "hermes_continue", "new_hermes_conversation", "hermes_status"})

    def test_elicitation_schema_is_valid(self):
        from mcp.server.elicitation import _validate_elicitation_schema
        _validate_elicitation_schema(self.b.ApprovalAnswer)


if __name__ == "__main__":
    unittest.main()
