#!/usr/bin/env python3
"""Hermes bridge: a stdio MCP server that lets Claude Code, the Claude desktop app, Codex
and other MCP clients talk to a Hermes agent through the Hermes API server.

Transport: POST /v1/chat/completions (streaming) with the X-Hermes-Session-Id header.
Hermes then loads the conversation history from its own session database, so only the new
message is sent each turn, history survives gateway restarts, and context-compression
rotations are followed server-side. Each named conversation maps to one Hermes session id,
kept in a small local state file; new_hermes_conversation() rotates that id.

Approvals: when Hermes needs approval for a flagged action, the stream carries an
``approval.request`` event. The bridge first asks the human directly through MCP elicitation
(if the client supports it); otherwise it returns the request to the calling agent, which
must get the user's explicit answer and pass it to hermes_continue(). Unanswered requests
are denied by Hermes when the bridge disconnects (silence is never consent).

Configuration: a JSON file (default: config.json next to this script, or
$HERMES_BRIDGE_CONFIG), overridden by environment variables:

  url            / HERMES_BRIDGE_URL          base URL of the Hermes API server (required)
  owner          / HERMES_BRIDGE_OWNER        the person the agent works for (default "the user")
  about          / HERMES_BRIDGE_ABOUT        one paragraph on what Hermes can do (tool descriptions)
  keychain_service / HERMES_BRIDGE_KEYCHAIN_SERVICE  macOS Keychain item (default hermes-api-key)
  keychain_account / HERMES_BRIDGE_KEYCHAIN_ACCOUNT  (default hermes-bridge)
  timeout        / HERMES_BRIDGE_TIMEOUT      seconds to wait per call (default 600)
  state_path     / HERMES_BRIDGE_STATE        conversation map (default state.json next to this script)
  HERMES_API_KEY                              bearer key (else read from the Keychain)
  HERMES_BRIDGE_CONVERSATION                  default conversation name for this client
  HERMES_BRIDGE_CLIENT                        client label shown to Hermes, e.g. "Claude Code"

Run with --check to print status and exit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent


def _load_config() -> dict:
    path = Path(os.environ.get("HERMES_BRIDGE_CONFIG", str(HERE / "config.json")))
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


_CFG = _load_config()


def _setting(key: str, env: str, default: Any = None) -> Any:
    return os.environ.get(env) or _CFG.get(key) or default


BASE_URL = str(_setting("url", "HERMES_BRIDGE_URL", "")).rstrip("/")
OWNER = _setting("owner", "HERMES_BRIDGE_OWNER", "the user")
ABOUT = _setting("about", "HERMES_BRIDGE_ABOUT", (
    f"Hermes is {OWNER}'s personal assistant agent running on their own server, with long-term memory, "
    "tools, scheduled jobs and whatever integrations they have connected."))
KEYCHAIN_SERVICE = _setting("keychain_service", "HERMES_BRIDGE_KEYCHAIN_SERVICE", "hermes-api-key")
KEYCHAIN_ACCOUNT = _setting("keychain_account", "HERMES_BRIDGE_KEYCHAIN_ACCOUNT", "hermes-bridge")
TIMEOUT = float(_setting("timeout", "HERMES_BRIDGE_TIMEOUT", 600))
STATE_PATH = Path(_setting("state_path", "HERMES_BRIDGE_STATE", str(HERE / "state.json"))).expanduser()
DEFAULT_CONVERSATION = os.environ.get("HERMES_BRIDGE_CONVERSATION", "claude")
CLIENT_LABEL = os.environ.get("HERMES_BRIDGE_CLIENT", "an AI agent")

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP(
    "hermes",
    instructions=(
        f"{ABOUT} Use ask_hermes to ask Hermes to look things up or do things on {OWNER}'s behalf, for "
        "example \"what's on my calendar tomorrow\", \"remind me Friday to ...\", \"what did I decide about "
        "X\", \"find the email from Y\". Hermes keeps context per conversation name."
    ),
)


# --------------------------------------------------------------------------- plumbing

def _api_key() -> str:
    key = os.environ.get("HERMES_API_KEY", "").strip()
    if not key and sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=10, check=True)
            key = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            key = ""
    if not key:
        raise RuntimeError(
            "No Hermes API key: set HERMES_API_KEY or store it in the macOS Keychain with "
            f"`security add-generic-password -a {KEYCHAIN_ACCOUNT} -s {KEYCHAIN_SERVICE} -w`.")
    return key


def _headers(extra: Optional[dict] = None) -> dict:
    h = {"Authorization": f"Bearer {_api_key()}"}
    h.update(extra or {})
    return h


def _require_url() -> None:
    if not BASE_URL:
        raise RuntimeError("Hermes API URL not configured (set `url` in config.json or HERMES_BRIDGE_URL).")


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return (s or DEFAULT_CONVERSATION)[:48]


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def _session_for(conversation: str, *, rotate: bool = False) -> tuple[str, str]:
    slug = _slug(conversation)
    state = _load_state()
    convs = state.setdefault("conversations", {})
    entry = convs.get(slug)
    if rotate or not entry:
        entry = {"session_id": f"bridge-{slug}-{time.strftime('%Y%m%d-%H%M%S')}",
                 "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        convs[slug] = entry
        _save_state(state)
    return slug, entry["session_id"]


def _remember_session(slug: str, session_id: str) -> None:
    """Follow a server-side rotation (context compression) so later turns use the live tip."""
    state = _load_state()
    entry = state.setdefault("conversations", {}).get(slug)
    if entry and session_id and entry.get("session_id") != session_id:
        entry["session_id"] = session_id
        _save_state(state)


def _system_note() -> str:
    return (
        f"This message is relayed through the Hermes bridge (MCP) from {CLIENT_LABEL} on {OWNER}'s computer. "
        f"The sender is an AI agent working for {OWNER}, or {OWNER} typing through it; treat requests as "
        f"{OWNER}'s. Reply in plain text that can be shown as-is. Clarify questions cannot be answered over "
        "this channel, so make a sensible choice and say what you chose. Approval prompts do reach "
        f"{OWNER} through the bridge."
    )


# --------------------------------------------------------------------------- turns

class Turn:
    """One streaming Hermes turn, consumed in a background task so it can outlive a tool call
    (needed when an approval has to go back to the calling agent)."""

    def __init__(self, message: str, slug: str, session_id: str):
        self.id = uuid.uuid4().hex[:12]
        self.slug, self.session_id = slug, session_id
        self.events: asyncio.Queue = asyncio.Queue()
        self.parts: list[str] = []
        self.tools: list[str] = []
        self.started = time.monotonic()
        self.progress_note = "Hermes is working"
        self.done = False
        self.task = asyncio.create_task(self._run(message))

    async def _run(self, message: str) -> None:
        body = {"model": "hermes-agent", "stream": True, "messages": [
            {"role": "system", "content": _system_note()},
            {"role": "user", "content": message}]}
        try:
            headers = _headers({"X-Hermes-Session-Id": self.session_id,
                                "X-Hermes-Session-Key": f"bridge:{self.slug}",
                                "Accept": "text/event-stream"})
            # Read timeout only guards a dead connection: Hermes sends a keepalive every 10 s.
            timeout = httpx.Timeout(connect=20, read=90, write=60, pool=20)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", f"{BASE_URL}/v1/chat/completions",
                                         json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        text = (await resp.aread()).decode(errors="replace")[:800]
                        raise RuntimeError(f"Hermes API returned HTTP {resp.status_code}: {text}")
                    sid = resp.headers.get("X-Hermes-Session-Id")
                    if sid:
                        self.session_id = sid
                    await self._consume(resp)
            _remember_session(self.slug, self.session_id)
            await self.events.put(("done", None))
        except httpx.ConnectError as exc:
            await self.events.put(("error", f"Could not reach Hermes at {BASE_URL} ({exc}). Is this machine "
                                            "on the network/tailnet and is the Hermes gateway running?"))
        except Exception as exc:  # surfaced to the caller as text
            await self.events.put(("error", f"Hermes request failed: {exc}"))
        finally:
            self.done = True

    async def _consume(self, resp: httpx.Response) -> None:
        event = ""
        async for line in resp.aiter_lines():
            if not line:
                event = ""
                continue
            if line.startswith(":"):
                await self.events.put(("tick", None))
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            if event == "approval.request" and isinstance(payload, dict):
                await self.events.put(("approval", payload))
            elif event == "hermes.tool.progress" and isinstance(payload, dict):
                if payload.get("status") == "running":
                    label = payload.get("label") or payload.get("tool") or "a tool"
                    self.tools.append(str(payload.get("tool") or label))
                    self.progress_note = f"Hermes is using {label}"
                    await self.events.put(("tick", None))
            elif event:
                continue  # hermes.status and other named events: progress only
            elif isinstance(payload, dict):
                if isinstance(payload.get("error"), (dict, str)) and not payload.get("choices"):
                    err = payload["error"]
                    raise RuntimeError(err.get("message", err) if isinstance(err, dict) else err)
                for choice in payload.get("choices") or []:
                    delta = choice.get("delta") if isinstance(choice, dict) else None
                    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                        self.parts.append(delta["content"])

    def reply(self) -> str:
        text = "".join(self.parts).strip()
        return text or "(Hermes finished without a text reply.)"


_TURNS: dict[str, Turn] = {}


class ApprovalAnswer(BaseModel):
    decision: str = Field(
        json_schema_extra={"enum": ["once", "session", "deny"]},
        description="once = allow this one action; session = allow this kind of action for the rest of "
                    "this conversation; deny = block it")


def _describe_approval(ev: dict) -> str:
    what = ev.get("command") or ev.get("description") or "an action"
    desc = ev.get("description") or ""
    return f"`{what}`" + (f" ({desc})" if desc and desc != what else "")


async def _post_approval(turn: Turn, ev: dict, choice: str) -> Optional[str]:
    run_id = ev.get("run_id") or ev.get("id")
    body: dict = {"choice": choice}
    if ev.get("request_id"):
        body["request_id"] = ev["request_id"]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{BASE_URL}/v1/runs/{run_id}/approval", json=body, headers=_headers())
    if r.status_code >= 300:
        return f"Hermes did not accept the approval answer (HTTP {r.status_code}: {r.text[:300]})"
    return None


async def _elicit(ctx: Optional[Context], ev: dict) -> Optional[str]:
    """Ask the human directly when the client supports MCP elicitation. None = not possible."""
    if ctx is None:
        return None
    try:
        caps = ctx.request_context.session.client_params.capabilities
        if not getattr(caps, "elicitation", None):
            return None
        res = await ctx.elicit(
            f"Hermes asks for approval to run {_describe_approval(ev)}. Allow it?", schema=ApprovalAnswer)
    except Exception:
        return None
    if res.action == "accept" and res.data is not None:
        decision = str(res.data.decision).strip().lower()
        return decision if decision in ("once", "session", "deny") else "deny"
    return "deny"


async def _drive(turn: Turn, ctx: Optional[Context]) -> str:
    """Wait for the turn's next outcome: final reply, an approval to hand back, or a timeout."""
    deadline = time.monotonic() + TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (f"Hermes is still working after {int(TIMEOUT)} s. Call hermes_continue(turn_id=\"{turn.id}\") "
                    "to keep waiting for the reply.")
        try:
            kind, data = await asyncio.wait_for(turn.events.get(), timeout=remaining)
        except asyncio.TimeoutError:
            continue
        if kind == "tick":
            if ctx is not None:
                try:
                    elapsed = time.monotonic() - turn.started
                    await ctx.report_progress(elapsed, None, turn.progress_note)
                except Exception:
                    pass
            continue
        if kind == "done":
            _TURNS.pop(turn.id, None)
            return turn.reply()
        if kind == "error":
            _TURNS.pop(turn.id, None)
            return data
        if kind == "approval":
            choice = await _elicit(ctx, data)
            if choice is not None:
                err = await _post_approval(turn, data, choice)
                if err:
                    return err
                continue
            turn.pending = data  # type: ignore[attr-defined]
            choices = ", ".join(data.get("choices") or ["once", "deny"])
            return (
                f"APPROVAL NEEDED (turn_id={turn.id}). Hermes wants to run {_describe_approval(data)}. "
                f"Show this to {OWNER} and ask them. Only after {OWNER} explicitly answers, call "
                f"hermes_continue(turn_id=\"{turn.id}\", approval=<one of: {choices}>). Never approve on your "
                f"own. If {OWNER} says no, pass approval=\"deny\".")


# --------------------------------------------------------------------------- tools

@mcp.tool(description=(
    f"Send a message to Hermes, {OWNER}'s personal assistant agent, and return its reply. {ABOUT} "
    f"Ask it to look up or do things on {OWNER}'s behalf: calendar and email questions, scheduling, reminders "
    f"and tasks, what was decided or remembered about something, files, server status, running a job later. "
    "Write the message as you would to a capable human assistant. Context is kept per `conversation` name "
    "across calls and restarts, so follow-ups can say \"that\" or \"the second one\". Replies take seconds "
    "to several minutes when Hermes uses tools. If the result starts with APPROVAL NEEDED, follow its "
    "instructions."
))
async def ask_hermes(message: str, conversation: str = DEFAULT_CONVERSATION, ctx: Context | None = None) -> str:
    if not (message or "").strip():
        return "Nothing to send: the message is empty."
    try:
        _require_url()
        _api_key()
    except RuntimeError as exc:
        return str(exc)
    slug, session_id = _session_for(conversation)
    turn = Turn(message, slug, session_id)
    _TURNS[turn.id] = turn
    return await _drive(turn, ctx)


@mcp.tool(description=(
    "Continue a Hermes turn that ask_hermes left open: either it needed approval (pass `approval` with "
    f"{OWNER}'s explicit answer: once, session or deny) or it was still working (omit `approval` to keep "
    f"waiting). Never invent an approval; only relay {OWNER}'s own answer."
))
async def hermes_continue(turn_id: str, approval: str = "", ctx: Context | None = None) -> str:
    turn = _TURNS.get(turn_id)
    if turn is None:
        return ("Unknown or finished turn_id. (The bridge may have restarted; an unanswered approval is "
                "denied automatically.)")
    pending = getattr(turn, "pending", None)
    if approval:
        if not pending:
            return "That turn is not waiting for an approval."
        choice = approval.strip().lower()
        if choice not in (pending.get("choices") or ["once", "session", "always", "deny"]):
            return f"Invalid approval; use one of: {', '.join(pending.get('choices') or [])}"
        err = await _post_approval(turn, pending, choice)
        if err:
            return err
        turn.pending = None  # type: ignore[attr-defined]
    elif pending:
        return f"Still waiting for {OWNER}'s answer to: {_describe_approval(pending)}"
    return await _drive(turn, ctx)


@mcp.tool(description=(
    "Start a fresh Hermes conversation under the given name: Hermes drops the earlier back-and-forth of that "
    f"conversation (its long-term memory of {OWNER} is unaffected). Use when switching to an unrelated topic "
    "or when the conversation has become long."
))
async def new_hermes_conversation(conversation: str = DEFAULT_CONVERSATION) -> str:
    slug, session_id = _session_for(conversation, rotate=True)
    return f"Started a new Hermes conversation '{slug}' (session {session_id})."


@mcp.tool(description=(
    f"Check that Hermes ({OWNER}'s personal assistant agent) is reachable: health, version, connected "
    "platforms, the model it is using and this client's conversations."
))
async def hermes_status() -> str:
    lines = [f"Endpoint: {BASE_URL or '(not configured)'}"]
    try:
        _require_url()
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{BASE_URL}/health/detailed", headers=_headers())
            if r.status_code != 200:
                return "\n".join(lines + [f"HTTP {r.status_code}: {r.text[:300]}"])
            health = r.json()
            caps = (await client.get(f"{BASE_URL}/v1/capabilities", headers=_headers())).json()
            try:
                opts = (await client.get(f"{BASE_URL}/api/model/options", headers=_headers())).json()
            except (httpx.HTTPError, ValueError):
                opts = {}
    except httpx.HTTPError as exc:
        return "\n".join(lines + [f"Unreachable: {exc}"])
    except RuntimeError as exc:
        return "\n".join(lines + [str(exc)])
    lines.append(f"Status: {health.get('status')} (Hermes {health.get('version')}, gateway "
                 f"{health.get('gateway_state')})")
    platforms = health.get("platforms") or {}
    if isinstance(platforms, dict) and platforms:
        lines.append("Platforms: " + ", ".join(
            f"{k}={(v.get('state') if isinstance(v, dict) else v)}" for k, v in sorted(platforms.items())))
    if opts.get("model"):
        lines.append(f"Model: {opts.get('model')} via {opts.get('provider')}")
    feats = caps.get("features") or {}
    lines.append("API features: " + ", ".join(sorted(k for k, v in feats.items() if v is True)))
    convs = _load_state().get("conversations") or {}
    if convs:
        lines.append("Conversations: " + ", ".join(
            f"{k} -> {v.get('session_id')}" for k, v in sorted(convs.items())))
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        print(asyncio.run(hermes_status()))
        return
    mcp.run()


if __name__ == "__main__":
    main()
