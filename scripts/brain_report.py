#!/usr/bin/env python3
"""Daily brain + wiki change report (script-only cron, no LLM). Prints nothing when nothing changed.

Schedule it hourly-ish in UTC (e.g. two runs that straddle the DST shift); it only reports during --hour
(default 8) in --tz (default $BRAIN_REPORT_TZ, else the host's local zone), so DST is handled. --force skips
that check. Covers the last 24 hours.
"""
import argparse
import os
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
MARK = {"added": "+", "updated": "~", "retired": "-"}
MAX_CHARS = 1900


def brain_section(db_path, since):
    if not Path(db_path).exists():
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM changes WHERE ts >= ? ORDER BY id", (since,)).fetchall()
    rejected = db.execute("SELECT COUNT(*) FROM candidates WHERE status='rejected' AND decided >= ?",
                          (since,)).fetchone()[0]
    if not rows:
        return []
    counts = defaultdict(int)
    by_project = defaultdict(list)
    for r in rows:
        counts[r["action"]] += 1
        line = f"  {MARK.get(r['action'], '?')} #{r['fact_id']} {r['text'][:140]}"
        if r["action"] == "updated" and r["old_text"]:
            line += f" (was: {r['old_text'][:80]})"
        by_project[r["project"] or "general"].append(line)
    out = [f"Brain, last 24h: {counts['added']} added, {counts['updated']} updated, {counts['retired']} retired"
           f" ({rejected} proposals rejected by the approver)"]
    for project in sorted(by_project, key=lambda p: (p == "general", p)):
        out.append(f"{project}:")
        out += by_project[project][:8]
        if len(by_project[project]) > 8:
            out.append(f"  ...and {len(by_project[project]) - 8} more")
    return out


def wiki_section(wiki):
    if not (Path(wiki) / ".git").exists():
        return []
    try:
        log = subprocess.run(["git", "-C", str(wiki), "log", "--since=24 hours ago", "--pretty=format:%h %s"],
                             capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    if not log:
        return []
    return [f"Wiki, last 24h: {len(log)} commit(s)"] + [f"  {line[:140]}" for line in log[:10]]


def report(home=HOME, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    lines = brain_section(Path(home) / "brain" / "brain.db", since) + wiki_section(Path(home) / "wiki")
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 20].rsplit("\n", 1)[0] + "\n  ...(truncated)"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="report even outside the report hour")
    p.add_argument("--hour", type=int, default=int(os.environ.get("BRAIN_REPORT_HOUR") or 8),
                   help="local hour to report in (default 8, or $BRAIN_REPORT_HOUR)")
    p.add_argument("--tz", default=os.environ.get("BRAIN_REPORT_TZ") or "",
                   help="IANA zone, e.g. Europe/Berlin (default $BRAIN_REPORT_TZ, else host local time)")
    a = p.parse_args(argv)
    local = datetime.now(ZoneInfo(a.tz)) if a.tz else datetime.now().astimezone()
    if not a.force and local.hour != a.hour:
        return
    text = report()
    if text:
        print(text)


if __name__ == "__main__":
    main()
