#!/usr/bin/env python3
"""Nightly encrypted, deduplicated backup of Hermes data with restic. Meant for a script-only
Hermes cron job (`hermes cron create ... --script backup_data.py --no-agent`).

What it does, in order
  1. Takes consistent copies of the SQLite databases with SQLite's online backup API (never a raw
     copy of a live .db / -wal) into a private staging dir ($HERMES_HOME/backup-staging, mode 700),
     and runs `PRAGMA quick_check` on every copy.
  2. `restic backup` of the staging dir plus the plain files/dirs below (secrets, config, memories,
     wiki, skills, scripts, plugins, ...), tagged.
  3. `restic forget --prune` with keep-daily 7, keep-weekly 4, keep-monthly 6 (configurable).
  4. On Sundays (in BACKUP_TZ): `restic check --read-data-subset 5%`.
  5. Writes $HERMES_HOME/backup-staging/../state/backup_last.json (time + snapshot id) so other
     jobs (cleanup.py) can confirm a recent backup exists, then deletes the staging copies.

Output: nothing on a normal night (silent cron). One line on failure (exit code 1, so the cron
failure notice fires) and one summary line on Sundays (repo size, snapshot count).

Configuration: KEY=VALUE lines in $HERMES_HOME/backup.conf (environment variables override).
  BACKUP_REPO               restic repository, e.g. sftp:backup-host:/hermes-restic   (required)
  BACKUP_PASSWORD_COMMAND   command that prints the repo password (default: the vault CLI,
                            `vault_mcp.py get RESTIC_PASSWORD`); the password is never logged
  BACKUP_HOST               restic --host value (default: this machine's hostname)
  BACKUP_TAGS               comma-separated tags (default: hermes,nightly)
  BACKUP_TZ                 time zone for "is it Sunday" (default: UTC)
  BACKUP_KEEP               daily,weekly,monthly (default: 7,4,6)
  BACKUP_CHECK_SUBSET       read-data-subset for the weekly check (default: 5%)
  BACKUP_REQUIRED_DBS       comma-separated DB paths (relative to $HERMES_HOME) that must exist;
                            other listed DBs are skipped quietly when absent (default: state.db)
  BACKUP_EXTRA_DBS          extra comma-separated DB paths to snapshot
  BACKUP_EXTRA_PATHS        extra comma-separated files/dirs to include
  RESTIC                    restic binary (default: restic on PATH)

Usage: backup_data.py [--dry-run] [--force-summary] [--force-check]
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()

# SQLite databases (relative to HERMES_HOME). Absent ones are skipped unless required.
DBS = [
    "state.db", "brain/brain.db", "vault/vault.db", "taskdb/tasks.sqlite3", "data/email-review.db",
    "kanban.db", "cron/executions.db", "cron/deliveries.db", "home-assistant-ops/home_ops.sqlite3",
    "verification_evidence.db",
]
# Plain files and directories (relative to HERMES_HOME). Absent ones are skipped.
PATHS = [
    "vault/vault.key", "google/client_secret.json", ".env", "auth.json", "config.yaml", "SOUL.md",
    "memories", "cron/jobs.json", "wiki", "quarantine", "skills", "scripts", "plugins", "locales",
]
EXCLUDES = ["__pycache__", "*.pyc", ".venv", "node_modules", "*.db-wal", "*.db-shm", "*.lock"]


def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    conf = HERMES / "backup.conf"
    if conf.is_file():
        for line in conf.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.startswith("BACKUP_") or k == "RESTIC":
            cfg[k] = v
    return cfg


def csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class Fail(Exception):
    pass


class Restic:
    def __init__(self, cfg: dict[str, str]):
        self.bin = cfg.get("RESTIC") or shutil.which("restic") or "restic"
        self.env = dict(os.environ)
        self.env.pop("RESTIC_PASSWORD", None)
        self.env["RESTIC_REPOSITORY"] = cfg["BACKUP_REPO"]
        self.env["RESTIC_PASSWORD_COMMAND"] = cfg.get("BACKUP_PASSWORD_COMMAND") or (
            f"{shlex.quote(str(HERMES / 'vault/.venv/bin/python'))} "
            f"{shlex.quote(str(HERMES / 'vault/vault_mcp.py'))} get RESTIC_PASSWORD")
        self.env.setdefault("RESTIC_CACHE_DIR", str(Path.home() / ".cache/restic"))

    def run(self, *args: str, timeout: int = 3000, ok_codes: tuple[int, ...] = (0,)) -> str:
        self.last_code = 0
        try:
            p = subprocess.run([self.bin, *args], env=self.env, capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise Fail(f"restic {args[0]} timed out after {timeout}s")
        except OSError as exc:
            raise Fail(f"cannot run restic: {exc}")
        if p.returncode not in ok_codes:
            tail = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["no output"]
            raise Fail(f"restic {args[0]} exited {p.returncode}: {tail[0][:200]}")
        self.last_code = p.returncode
        return p.stdout


def snapshot_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dst)
    try:
        s.backup(d, pages=4096, sleep=0.05)
    finally:
        s.close()
    try:
        d.execute("PRAGMA journal_mode=DELETE")
        result = d.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        d.close()
    if result != "ok":
        raise Fail(f"quick_check failed on snapshot of {src.name}: {result[:100]}")
    os.chmod(dst, 0o600)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nightly restic backup of Hermes data (see docstring).")
    ap.add_argument("--dry-run", action="store_true", help="Snapshot DBs and run restic backup --dry-run only")
    ap.add_argument("--force-summary", action="store_true", help="Print the summary line even if not Sunday")
    ap.add_argument("--force-check", action="store_true", help="Run the weekly restic check now")
    args = ap.parse_args(argv)

    cfg = load_config()
    if not cfg.get("BACKUP_REPO"):
        print(f"❌ Hermes backup: BACKUP_REPO is not set ({HERMES / 'backup.conf'})")
        return 1
    tz = cfg.get("BACKUP_TZ", "UTC")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now(timezone.utc)
    sunday = now.weekday() == 6
    host = cfg.get("BACKUP_HOST") or socket.gethostname()
    tags = csv(cfg.get("BACKUP_TAGS")) or ["hermes", "nightly"]
    keep = (csv(cfg.get("BACKUP_KEEP")) + ["7", "4", "6"][len(csv(cfg.get("BACKUP_KEEP"))):])[:3]
    required = set(csv(cfg.get("BACKUP_REQUIRED_DBS")) or ["state.db"])
    restic = Restic(cfg)

    staging = HERMES / "backup-staging"
    state_dir = HERMES / "state"
    lock = None
    started = time.monotonic()
    try:
        import fcntl
        state_dir.mkdir(parents=True, exist_ok=True)
        lock = open(state_dir / "backup.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Fail("another backup run is still in progress")

        # 1. consistent DB copies
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(mode=0o700)
        os.chmod(staging, 0o700)
        db_dir = staging / "db"
        skipped = []
        for rel in DBS + csv(cfg.get("BACKUP_EXTRA_DBS")):
            src = HERMES / rel
            if not src.is_file():
                if rel in required:
                    raise Fail(f"required database missing: {rel}")
                skipped.append(rel)
                continue
            try:
                snapshot_db(src, db_dir / rel)
            except sqlite3.Error as exc:
                raise Fail(f"sqlite backup of {rel} failed: {exc}")

        # 2. restic backup
        paths = [str(staging)] + [str(HERMES / p) for p in PATHS + csv(cfg.get("BACKUP_EXTRA_PATHS"))
                                  if (HERMES / p).exists()]
        cmd = ["backup", "--json", "--host", host, "--exclude-caches"]
        for t in tags:
            cmd += ["--tag", t]
        for e in EXCLUDES:
            cmd += ["--exclude", e]
        if args.dry_run:
            cmd.append("--dry-run")
        restic.run("unlock", timeout=300)  # removes only stale locks
        # exit 3 = snapshot made but some files were unreadable: keep going, report at the end
        out = restic.run(*cmd, *paths, timeout=3000, ok_codes=(0, 3))
        partial = restic.last_code == 3
        summary = {}
        for line in out.splitlines():
            if '"message_type":"summary"' in line.replace(" ", ""):
                summary = json.loads(line)
        if args.dry_run:
            print(f"🧪 Hermes backup dry run: {summary.get('total_files_processed', '?')} files, "
                  f"{human(summary.get('total_bytes_processed', 0))} would be read, "
                  f"{human(summary.get('data_added', 0))} new; skipped DBs: {', '.join(skipped) or 'none'}")
            return 0
        snap = summary.get("snapshot_id", "")[:8]

        # 3. retention
        restic.run("forget", "--prune", "--host", host, "--tag", ",".join(tags),
                   "--keep-daily", keep[0], "--keep-weekly", keep[1], "--keep-monthly", keep[2],
                   timeout=3000)

        (state_dir / "backup_last.json").write_text(json.dumps({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "snapshot": summary.get("snapshot_id", ""), "repo": cfg["BACKUP_REPO"], "host": host,
            "data_added": summary.get("data_added", 0),
        }) + "\n")

        # 4. weekly check
        checked = ""
        if sunday or args.force_check:
            restic.run("check", f"--read-data-subset={cfg.get('BACKUP_CHECK_SUBSET', '5%')}", timeout=3000)
            checked = f" · check OK ({cfg.get('BACKUP_CHECK_SUBSET', '5%')} read)"

        if partial:
            raise Fail(f"snapshot {snap} saved, but some files could not be read (restic exit 3)")
        if sunday or args.force_summary or args.force_check:
            snaps = json.loads(restic.run("snapshots", "--json", "--host", host, "--tag", ",".join(tags),
                                          timeout=600) or "[]")
            stats = json.loads(restic.run("stats", "--json", "--mode", "raw-data", timeout=600) or "{}")
            print(f"🗄️ Hermes backup OK · snapshot {snap} · repo {human(stats.get('total_size', 0))} "
                  f"(+{human(summary.get('data_added', 0))} tonight) · {len(snaps)} snapshots · "
                  f"{time.monotonic() - started:.0f}s{checked}")
        return 0
    except Fail as exc:
        print(f"❌ Hermes backup FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - one line to chat, never a traceback with paths
        print(f"❌ Hermes backup FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
