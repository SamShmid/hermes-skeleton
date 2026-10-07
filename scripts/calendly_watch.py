#!/usr/bin/env python3
"""Script-only cron: print one line per Calendly booking made or canceled since the last run; print nothing
otherwise (empty stdout = no message).

  📅 New Calendly booking: <name> · <Thu Oct 8, 2:00 PM> · <event type>
  ❌ Calendly booking canceled: <name> · <when> · <event type>

The first run only records what exists (silent baseline). Needs the vault venv (run with its python) and
vault/calendly_mcp.py next to this scripts/ directory. State: $HERMES_HOME/state/calendly_watch.json
(override with CALENDLY_WATCH_STATE). A network/API error is tolerated silently twice in a row; the third
consecutive failure exits non-zero so the cron failure notice fires.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or HERE.parent)
STATE = Path(os.environ.get("CALENDLY_WATCH_STATE") or HERMES_HOME / "state" / "calendly_watch.json")
sys.path.insert(0, str(HERMES_HOME / "vault"))
sys.path.insert(0, str(HERE.parent / "vault"))
MAX_SILENT_FAILURES = 2
KEEP_DAYS = 7  # forget events that started more than this long ago


def load_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def who(c, ev: dict) -> str:
    try:
        names = [i.get("name") or i.get("email") or "?" for i in c.invitees(ev["uri"])]
    except Exception:
        names = []
    return ", ".join(names) or "someone"


def run(c, state: dict | None, now: dt.datetime) -> tuple[list[str], dict]:
    """(messages, new_state). state None = first run: baseline, no messages."""
    import calendly_mcp as cm

    first = state is None
    known = dict((state or {}).get("events", {}))
    msgs = []
    tz = c.tz
    for ev in c.events(now - dt.timedelta(days=1), now + dt.timedelta(days=365)):
        uid, status = cm.uuid_of(ev.get("uri", "")), ev.get("status")
        start = cm.parse_ts(ev.get("start_time"))
        prev = known.get(uid)
        if not first and (prev or {}).get("status") != status:
            line = f"{who(c, ev)} · {cm.human(start, tz) if start else '?'} · {ev.get('name') or 'meeting'}"
            if status == "active" and prev is None:
                msgs.append(f"📅 New Calendly booking: {line}")
            elif status == "canceled":
                reason = ((ev.get("cancellation") or {}).get("reason") or "").strip()
                msgs.append(f"❌ Calendly booking canceled: {line}" + (f" — {reason[:200]}" if reason else ""))
        known[uid] = {"status": status, "start": ev.get("start_time")}
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    known = {k: v for k, v in known.items() if (cm.parse_ts(v.get("start")) or now) >= cutoff}
    return msgs, {"last_run": now.isoformat(), "failures": 0, "events": known}


def main() -> int:
    import calendly_mcp as cm

    state = load_state(STATE)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        msgs, new = run(cm.Calendly(), state, now)
    except Exception as e:
        if state is None:
            raise
        state["failures"] = int(state.get("failures", 0)) + 1
        save_state(STATE, state)
        if state["failures"] > MAX_SILENT_FAILURES:
            print(f"calendly_watch: {state['failures']} failed runs in a row: {e}", file=sys.stderr)
            return 1
        return 0
    save_state(STATE, new)
    if msgs:
        print("\n".join(msgs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
