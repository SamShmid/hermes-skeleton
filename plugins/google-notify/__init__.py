"""google-notify: tell the chat that started a Google sign-in how it ended.

1. post_tool_call on google_connect_start: remember which session asked (keyed by the sign-in's one-time state).
2. A background thread watches $HERMES_HOME/google/events/ (written by the sign-in return page) and injects a
   short notice into that session, so the agent tells the owner the account connected (or why it failed).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("hermes_plugins.google_notify")
HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
EVENTS = HOME / "google" / "events"
ORIGINS = HOME / "google" / "origins.json"
STATE_RE = re.compile(r"[?&]state=([A-Za-z0-9_\-]+)")
KEEP = 3600  # seconds an origin is remembered
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(ORIGINS.read_text())
    except Exception:
        return {}


def _save(d: dict):
    ORIGINS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = ORIGINS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(ORIGINS)


def _session_key() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_KEY") or ""
    except Exception:
        return ""


def on_tool(tool_name: str = "", result=None, **_kw):
    if not str(tool_name).endswith("google_connect_start"):
        return None
    m, key = STATE_RE.search(str(result or "")), _session_key()
    if m and key:
        with _lock:
            d = {s: v for s, v in _load().items() if time.time() - v.get("ts", 0) < KEEP}
            d[m.group(1)[:16]] = {"session_key": key, "ts": time.time()}
            _save(d)
    return None


def message(ev: dict) -> str:
    if ev.get("ok"):
        text = (f"[Google sign-in update] Sign-in finished: Google account {ev.get('email')} is now connected "
                f"as '{ev.get('account')}'.")
        if ev.get("warning"):
            text += f" Warning: {ev['warning']}"
        return text + " Tell the owner in one short line."
    return (f"[Google sign-in update] Sign-in did NOT finish: {ev.get('error') or 'unknown error'}. "
            "Tell the owner in one short line and offer a new sign-in link.")


def deliver_pending(inject) -> int:
    """Deliver queued events; returns how many were handled."""
    if not EVENTS.is_dir():
        return 0
    done = 0
    for f in sorted(EVENTS.glob("*.json")):
        try:
            ev = json.loads(f.read_text())
        except Exception:
            f.unlink(missing_ok=True)
            continue
        with _lock:
            d = _load()
            origin = d.pop(str(ev.get("state", ""))[:16], None)
            _save(d)
        if origin and origin.get("session_key"):
            ok = inject(message(ev), session_key=origin["session_key"])
            log.info("google-notify: delivered=%s ok=%s", bool(ok), ev.get("ok"))
        else:
            log.info("google-notify: no originating chat for a sign-in event; dropped")
        f.unlink(missing_ok=True)
        done += 1
    return done


def _loop(ctx, stop: threading.Event):
    while not stop.wait(3):
        try:
            deliver_pending(lambda text, session_key: ctx.inject_message(text, session_key=session_key))
        except Exception as exc:
            log.warning("google-notify: delivery pass failed: %s", exc)


def register(ctx):
    ctx.register_hook("post_tool_call", on_tool)
    import sys
    if sys.argv[1:3] == ["gateway", "run"]:
        stop = threading.Event()
        threading.Thread(target=_loop, args=(ctx, stop), name="google-notify", daemon=True).start()
        if hasattr(ctx, "on_unload"):
            ctx.on_unload(stop.set)
