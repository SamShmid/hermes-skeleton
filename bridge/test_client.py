"""Tiny MCP client for the Hermes bridge: spawns it over stdio and calls tools.

  test_client.py list
  test_client.py hermes_status
  test_client.py ask "message" [conversation]
  test_client.py approval-test [elicit]   # harmless flagged command; always answers "deny"
"""
import asyncio
import re
import sys
from pathlib import Path

import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
APPROVAL_MSG = ("Bridge approval-path test (harmless): in the terminal run exactly "
                "`chmod 777 /tmp/hermes-bridge-approval-test.txt` (create the file first with touch). "
                "If it is blocked or denied, do not work around it; just say in one line what happened.")


async def _deny_elicitation(context, params):
    print(f"  [elicitation shown to user] {params.message}", file=sys.stderr)
    return types.ElicitResult(action="accept", content={"decision": "deny"})


async def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "hermes_status"
    use_elicit = "elicit" in sys.argv[2:]
    params = StdioServerParameters(command=str(HERE / ".venv/bin/python"),
                                   args=[str(HERE / "hermes_bridge_mcp.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, elicitation_callback=_deny_elicitation if use_elicit else None) as s:
            await s.initialize()

            async def call(tool, args):
                async def prog(p, total, msg):
                    print(f"  [progress {p:.0f}s] {msg}", file=sys.stderr)
                res = await s.call_tool(tool, args, progress_callback=prog)
                return "\n".join(getattr(c, "text", str(c)) for c in res.content)

            if cmd == "list":
                for t in (await s.list_tools()).tools:
                    print(f"- {t.name}: {t.description[:110]}...")
            elif cmd == "ask":
                args = {"message": sys.argv[2]}
                if len(sys.argv) > 3:
                    args["conversation"] = sys.argv[3]
                print(await call("ask_hermes", args))
            elif cmd == "approval-test":
                out = await call("ask_hermes", {"message": APPROVAL_MSG, "conversation": "bridge-test"})
                print("ask_hermes ->", out)
                m = re.search(r"turn_id=(\w+)", out)
                if out.startswith("APPROVAL NEEDED") and m:
                    print("hermes_continue(deny) ->", await call("hermes_continue",
                                                                  {"turn_id": m.group(1), "approval": "deny"}))
            else:
                print(await call(cmd, {}))


asyncio.run(main())
