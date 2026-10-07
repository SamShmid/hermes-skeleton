#!/usr/bin/env python3
"""Daily Hermes housekeeping. Meant for a script-only Hermes cron job (e.g. "hermes-cleanup", early morning).

Conservative by design: it only touches things that are clearly Hermes-owned AND
clearly stale, and it always prints a short report (the cron job delivers it to chat).

What it does
  Processes (owned by this user, older than the age limit, never protected ones):
    - agent-browser daemons      ~/.hermes/tools/agent-browser-*/...      > 24 h
    - headless Chromium          ~/.hermes/tools/chromium-*/chrome (main + crashpad) > 24 h
    - browser_harness daemons    python -m browser_harness.daemon         > 24 h
    - hung himalaya IMAP calls   himalaya / himalaya-real                 > 60 min
    SIGTERM, then SIGKILL after 5 s. Never touched: the services in
    $HERMES_CLEANUP_PROTECTED_SERVICES (default: hermes-gateway; their main PIDs and everything
    under them), anything matching gitea/forgejo/docker/podman/containerd/sshd/tmux/systemd/dbus,
    bash shells, and this script's own tree.
  Files (top-level entries whose newest file is older than the limit; dotfiles skipped):
    - ~/.hermes/cache/{scratch,terminal-output,spillover,partials,exec}   > 7 days
      (a scratch browser profile still used by a running process is kept)
    - /tmp entries owned by this user named hermes*, agent-browser*, browser_harness*,
      browser-use*, tmp*                                                   > 7 days
    - rotated logs ~/.hermes/logs/*.log.<n>[.gz] and logs/process-results/* > 30 days
  Manual backups: top-level entries of $HERMES_CLEANUP_BACKUPS_DIR (default ~/backups) whose
    newest file is older than $HERMES_CLEANUP_BACKUPS_DAYS (default 30) days — ONLY when the
    nightly restic backup (backup_data.py) is confirmed: its marker state/backup_last.json is
    under 36 h old AND restic lists that snapshot with a recent time. Otherwise nothing is
    deleted and the report says why. Disabled when backup.conf has no BACKUP_REPO.
  Checkpoints: runs the official `hermes checkpoints prune` with the same limits as
    config.yaml (7 days, 500 MB). Dry-run only shows the store size.
  Disk: warns when / is more than 85 % full and names the biggest home directories.

Never deleted: user data ($HERMES_HOME/data, memories, sessions, state.db, quarantine,
$HERMES_HOME/backups, workspace, skills, config), anything outside the paths above, symlink targets.
Report timestamps use the host's local time zone (set TZ to change it).

Usage: cleanup.py [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME", HOME / ".hermes")).expanduser()
TOOLS = HERMES / "tools"

PROC_MAX_AGE = 24 * 3600
HIMALAYA_MAX_AGE = 60 * 60
CACHE_MAX_AGE_DAYS = 7
TMP_MAX_AGE_DAYS = 7
LOG_MAX_AGE_DAYS = 30
DISK_WARN_PCT = 85
BACKUPS_DIR = Path(os.environ.get("HERMES_CLEANUP_BACKUPS_DIR", HOME / "backups")).expanduser()
BACKUPS_MAX_AGE_DAYS = int(os.environ.get("HERMES_CLEANUP_BACKUPS_DAYS", "30"))
BACKUP_MARKER_MAX_AGE_H = 36
CACHE_DIRS = ["scratch", "terminal-output", "spillover", "partials", "exec"]
TMP_PREFIXES = ("hermes", "agent-browser", "browser_harness", "browser-use", "tmp")
PROTECTED_SERVICES = tuple(s for s in os.environ.get("HERMES_CLEANUP_PROTECTED_SERVICES", "hermes-gateway")
                           .replace(",", " ").split() if s)
PROTECTED_WORDS = ("gitea", "forgejo", "docker", "podman", "containerd", "sshd", "tmux",
                   "systemd", "dbus", "hermes_cli") + PROTECTED_SERVICES


# ---------------------------------------------------------------- helpers
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def tree_size_and_newest(path: Path) -> tuple[int, float]:
    """Total bytes and newest mtime under path (no symlink following)."""
    st = path.lstat()
    size, newest = st.st_size, st.st_mtime
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                try:
                    s = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                size += s.st_size
                newest = max(newest, s.st_mtime)
    return size, newest


def remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


# ---------------------------------------------------------------- processes
def _clock_ticks() -> int:
    return os.sysconf(os.sysconf_names["SC_CLK_TCK"])


def list_processes() -> dict[int, dict]:
    uid = os.getuid()
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    ticks = _clock_ticks()
    procs: dict[int, dict] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            stat = (entry / "stat").read_text()
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        rest = stat[stat.rindex(")") + 2:].split()
        procs[int(entry.name)] = {
            "pid": int(entry.name),
            "ppid": int(rest[1]),
            "age": uptime - int(rest[19]) / ticks,
            "cmd": cmd,
        }
    return procs


def service_pid(name: str) -> int:
    try:
        out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", name],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def descendants(procs: dict[int, dict], roots: set[int]) -> set[int]:
    out = set(roots)
    changed = True
    while changed:
        changed = False
        for p in procs.values():
            if p["ppid"] in out and p["pid"] not in out:
                out.add(p["pid"])
                changed = True
    return out


def classify(p: dict) -> str | None:
    cmd = p["cmd"]
    exe = cmd.split(" ", 1)[0]
    if exe.startswith(str(TOOLS / "agent-browser-")) and p["age"] > PROC_MAX_AGE:
        return "browser daemon"
    if exe.startswith(str(TOOLS / "chromium-")) and "--type=" not in cmd and p["age"] > PROC_MAX_AGE:
        return "headless Chromium"
    if "-m browser_harness.daemon" in cmd and p["age"] > PROC_MAX_AGE:
        return "browser harness daemon"
    if os.path.basename(exe) in ("himalaya", "himalaya-real") or "/.local/bin/himalaya" in cmd:
        if p["age"] > HIMALAYA_MAX_AGE:
            return "hung himalaya (IMAP)"
    return None


def stale_processes() -> tuple[list[tuple[dict, str]], dict[int, dict]]:
    procs = list_processes()
    protected = {os.getpid(), os.getppid(), 1}
    # this script's own ancestry (cron worker / gateway)
    pid = os.getpid()
    while pid in procs and procs[pid]["ppid"] > 1:
        pid = procs[pid]["ppid"]
        protected.add(pid)
    roots = {service_pid(s) for s in PROTECTED_SERVICES} - {0}
    protected |= descendants(procs, roots)
    found = []
    for p in procs.values():
        if p["pid"] in protected:
            continue
        lowered = p["cmd"].lower()
        if any(w in lowered for w in PROTECTED_WORDS) or lowered.startswith(("-bash", "bash", "/bin/bash")):
            continue
        reason = classify(p)
        if reason:
            found.append((p, reason))
    return found, procs


def stop(pids: set[int]) -> set[int]:
    """SIGTERM, wait up to 5 s, then SIGKILL. Returns the pids that are gone."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    alive = set(pids)
    while alive and time.monotonic() < deadline:
        time.sleep(0.25)
        alive = {p for p in alive if Path(f"/proc/{p}").exists()}
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.2)
    return {p for p in pids if not Path(f"/proc/{p}").exists()}


# ---------------------------------------------------------------- files
def stale_entries(procs: dict[int, dict]) -> list[tuple[str, Path, int]]:
    now = time.time()
    found: list[tuple[str, Path, int]] = []
    in_use = " ".join(p["cmd"] for p in procs.values())

    def consider(group: str, path: Path, max_days: int) -> None:
        try:
            size, newest = tree_size_and_newest(path)
        except OSError:
            return
        if now - newest < max_days * 86400:
            return
        if str(path) in in_use:  # e.g. a browser profile dir a live process still uses
            return
        found.append((group, path, size))

    for name in CACHE_DIRS:
        base = HERMES / "cache" / name
        if base.is_dir():
            for entry in base.iterdir():
                if not entry.name.startswith("."):
                    consider("cache", entry, CACHE_MAX_AGE_DAYS)
    uid = os.getuid()
    for entry in Path("/tmp").iterdir():
        try:
            if entry.lstat().st_uid != uid:
                continue
        except OSError:
            continue
        if entry.name.startswith(TMP_PREFIXES) and not entry.is_socket():
            consider("/tmp", entry, TMP_MAX_AGE_DAYS)
    logs = HERMES / "logs"
    if logs.is_dir():
        for entry in logs.iterdir():
            parts = entry.name.split(".log.", 1)
            if entry.is_file() and len(parts) == 2 and parts[1].split(".")[0].isdigit():
                consider("logs", entry, LOG_MAX_AGE_DAYS)
        results = logs / "process-results"
        if results.is_dir():
            for entry in results.iterdir():
                consider("logs", entry, LOG_MAX_AGE_DAYS)
    return found


# ---------------------------------------------------------------- manual backups
def old_manual_backups() -> list[tuple[Path, int, float]]:
    """(path, bytes, newest mtime) for every top-level entry of BACKUPS_DIR, oldest first."""
    if not BACKUPS_DIR.is_dir() or BACKUPS_DIR.is_symlink():
        return []
    out = []
    for entry in BACKUPS_DIR.iterdir():
        try:
            size, newest = tree_size_and_newest(entry)
        except OSError:
            continue
        out.append((entry, size, newest))
    return sorted(out, key=lambda e: e[2])


def restic_backup_confirmed() -> tuple[bool, str]:
    """True only if the nightly restic backup is recent AND restic itself lists that snapshot."""
    import json
    marker = HERMES / "state" / "backup_last.json"
    try:
        data = json.loads(marker.read_text())
        when = datetime.fromisoformat(data["time"])
        snap = data["snapshot"]
    except (OSError, ValueError, KeyError, TypeError):
        return False, "no restic backup marker (backup_data.py has not succeeded yet)"
    age_h = (datetime.now(when.tzinfo) - when).total_seconds() / 3600
    if age_h > BACKUP_MARKER_MAX_AGE_H:
        return False, f"last restic backup is {age_h:.0f} h old"
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import backup_data  # same scripts dir; reads $HERMES_HOME/backup.conf
        cfg = backup_data.load_config()
        if not cfg.get("BACKUP_REPO"):
            return False, "restic backup not configured"
        out = backup_data.Restic(cfg).run("snapshots", "--json", snap, timeout=300)
        snaps = json.loads(out or "[]")
    except Exception as exc:  # noqa: BLE001 - any doubt means "don't delete"
        return False, f"could not confirm the restic snapshot ({str(exc)[:80]})"
    if not snaps:
        return False, f"restic snapshot {snap[:8]} not found in the repository"
    return True, f"restic snapshot {snap[:8]} ({age_h:.0f} h old) confirmed"


def hermes_command() -> list[str]:
    """The checkout's own launcher, else a legacy venv, else `hermes` on PATH."""
    launcher = HERMES / "hermes-agent" / ".hermes" / "bin" / "hermes"
    if launcher.exists():
        return [str(launcher)]
    legacy = HERMES / "hermes-agent" / "venv" / "bin" / "python"
    if legacy.exists():
        return [str(legacy), "-m", "hermes_cli.main"]
    return [shutil.which("hermes") or "hermes"]


def checkpoints(dry_run: bool) -> tuple[int, str | None]:
    """(bytes freed, error). Uses the official CLI so the shadow git store stays valid."""
    store = HERMES / "checkpoints"
    if not store.exists():
        return 0, None
    before = tree_size_and_newest(store)[0]
    if dry_run:
        return 0, None
    try:
        proc = subprocess.run(
            hermes_command() + ["checkpoints", "prune", "--retention-days", "7", "--max-size-mb", "500", "-f"],
            cwd=str(HERMES / "hermes-agent"), capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 0, f"checkpoint prune failed: {exc}"
    if proc.returncode != 0:
        return 0, f"checkpoint prune exited {proc.returncode}"
    return max(0, before - tree_size_and_newest(store)[0]), None


# ---------------------------------------------------------------- disk
def disk_lines() -> list[str]:
    usage = shutil.disk_usage("/")
    pct = usage.used * 100 / usage.total
    lines = [f"💽 Disk: {pct:.0f}% used ({human(usage.free)} free)"]
    if pct > DISK_WARN_PCT:
        sizes = []
        for entry in HOME.iterdir():
            if entry.is_symlink():
                continue
            try:
                out = subprocess.run(["du", "-xsb", str(entry)], capture_output=True, text=True, timeout=120).stdout
                sizes.append((int(out.split()[0]), entry.name))
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                continue
        top = ", ".join(f"~/{n} {human(s)}" for s, n in sorted(sizes, reverse=True)[:4])
        lines.append(f"⚠️ Root disk is over {DISK_WARN_PCT}% full. Biggest in home: {top}. "
                     "Nothing extra was deleted; Docker images/volumes are not touched by this job.")
    return lines


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily Hermes housekeeping (see module docstring).")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be cleaned; change nothing")
    ap.add_argument("--verbose", action="store_true", help="List every process and path")
    args = ap.parse_args(argv)
    dry = args.dry_run

    procs_found, procs = stale_processes()
    files_found = stale_entries(procs)
    errors: list[str] = []

    stopped: list[tuple[dict, str]] = []
    if procs_found:
        if dry:
            stopped = procs_found
        else:
            gone = stop({p["pid"] for p, _ in procs_found})
            stopped = [(p, r) for p, r in procs_found if p["pid"] in gone]
            if len(stopped) < len(procs_found):
                errors.append(f"{len(procs_found) - len(stopped)} process(es) would not stop")

    freed: dict[str, int] = {}
    removed: list[tuple[str, Path, int]] = []
    for group, path, size in files_found:
        if not dry:
            try:
                remove(path)
            except OSError as exc:
                errors.append(f"couldn't remove {path}: {exc.strerror}")
                continue
        freed[group] = freed.get(group, 0) + size
        removed.append((group, path, size))
    # manual backups (~/backups): only once a recent restic backup is confirmed
    now = time.time()
    backups_all = old_manual_backups()
    backups_old = [e for e in backups_all if now - e[2] > BACKUPS_MAX_AGE_DAYS * 86400]
    backups_gate = (False, "nothing old enough")
    backups_removed: list[tuple[Path, int, float]] = []
    if backups_old:
        backups_gate = restic_backup_confirmed()
        if backups_gate[0]:
            for entry, size, newest in backups_old:
                if not dry:
                    try:
                        remove(entry)
                    except OSError as exc:
                        errors.append(f"couldn't remove {entry}: {exc.strerror}")
                        continue
                backups_removed.append((entry, size, newest))
                freed["old backups"] = freed.get("old backups", 0) + size
                removed.append(("old backups", entry, size))
        else:
            errors.append(f"kept {len(backups_old)} old manual backup(s) in {BACKUPS_DIR}: {backups_gate[1]}")
    ckpt_freed, ckpt_err = checkpoints(dry)
    if ckpt_err:
        errors.append(ckpt_err)
    if ckpt_freed:
        freed["checkpoints"] = ckpt_freed

    stamp = datetime.now().astimezone().strftime("%a %b %-d, %-I:%M %p %Z")
    verb_free, verb_stop = ("Would free", "Would stop") if dry else ("Freed", "Stopped")
    lines = [f"**🧹 Hermes cleanup{' (DRY RUN — nothing changed)' if dry else ''}** · {stamp}"]
    total = sum(freed.values())
    detail = " · ".join(f"{g} {human(b)}" for g, b in freed.items())
    lines.append(f"• {verb_free} {human(total)}" + (f" ({detail}; {len(removed)} items)" if detail else " (nothing old enough)"))
    if stopped:
        kinds: dict[str, int] = {}
        for _, reason in stopped:
            kinds[reason] = kinds.get(reason, 0) + 1
        lines.append(f"• {verb_stop} {len(stopped)} stale process(es): " + ", ".join(f"{n} {k}" for k, n in kinds.items()))
    else:
        lines.append("• No stale processes")
    if dry:
        lines.append(f"• Checkpoints: {human(tree_size_and_newest(HERMES / 'checkpoints')[0]) if (HERMES / 'checkpoints').exists() else '0 B'} "
                     "(the real run calls `hermes checkpoints prune`, 7 days / 500 MB)")
    lines.extend(disk_lines())
    for err in errors[:5]:
        lines.append(f"⚠️ {err}")
    if args.verbose or dry:
        lines.append(f"• Manual backups in {BACKUPS_DIR} (deleted after {BACKUPS_MAX_AGE_DAYS} days, "
                     f"only with a confirmed restic backup):" if backups_all else
                     f"• Manual backups: {BACKUPS_DIR} is empty or missing")
        for entry, size, newest in backups_all:
            age = (now - newest) / 86400
            due = datetime.fromtimestamp(newest + BACKUPS_MAX_AGE_DAYS * 86400).astimezone().strftime("%b %-d")
            state = (("would delete" if dry else "deleted") if (entry, size, newest) in backups_removed
                     else ("old enough but kept" if age > BACKUPS_MAX_AGE_DAYS else f"eligible {due}"))
            lines.append(f"  - {entry.name} ({human(size)}, {age:.0f} d old): {state}")
    if args.verbose:
        for p, reason in stopped:
            lines.append(f"  - pid {p['pid']} ({reason}, {p['age'] / 3600:.0f}h): {p['cmd'][:120]}")
        for group, path, size in removed:
            lines.append(f"  - [{group}] {path} ({human(size)})")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
