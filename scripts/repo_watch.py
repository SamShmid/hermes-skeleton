#!/usr/bin/env python3
"""repo_watch: scheduled checks for code repos, security findings and uptime.

Script-only (no LLM). Prints Discord-friendly markdown to stdout; empty stdout means
"nothing to say" for the alert mode, so a cron runner that treats empty output as
silent can deliver it directly.

Sources (each optional, all config-driven):
  * GitHub (via the `gh` CLI): repo inventory, open PRs/issues, latest Actions run per
    workflow on the default branch, Dependabot / code-scanning / secret-scanning alerts.
  * Forgejo/Gitea (REST API + token): inventory, open PRs/issues, Actions runs.
  * Local scans of a shallow clone: osv-scanner (dependency vulns) and gitleaks
    (secrets in recent commits) for repos without built-in alerts.
  * Uptime Kuma: monitor status and 7-day uptime per group/project.

Modes:
  --mode weekly   full report (always prints)
  --mode daily    only prints when something is NEW or newly broken since the last run
  --dry-run       don't write the state file

Config: $REPO_WATCH_CONFIG, else $HERMES_HOME/config/repo_watch.json,
else ~/.config/repo_watch.json.  See DEFAULT_CONFIG for keys.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import copy
import datetime as dt
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- config

SEVERITIES = ["low", "medium", "high", "critical"]
SEV_RANK = {"unknown": 1, "low": 0, "medium": 1, "high": 2, "critical": 3}
SEV_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪", "unknown": "🟡"}

DEFAULT_CONFIG: dict = {
    "github": {"enabled": True, "owners": [], "include_archived": False, "include_forks": False},
    "forgejo": {
        "enabled": False,
        "url": "",                      # or set url_var
        "url_var": "FORGEJO_URL",
        "token_var": "FORGEJO_TOKEN",
        "include_archived": False,
        "include_mirrors": False,
    },
    "uptime_kuma": {
        "enabled": False,
        "url": "",
        "url_var": "UPTIME_KUMA_URL",
        "username_var": "UPTIME_KUMA_USERNAME",
        "password_var": "UPTIME_KUMA_PASSWORD",
        "ignore_monitors": [],          # fnmatch patterns on monitor names
        "degraded_below": 99.0,         # 7-day uptime % below this counts as degraded
        "uptime_days": 7,
    },
    "local_scan": {
        "forgejo": True,                # always scan Forgejo repos (no built-in alerts)
        "github": "fallback",           # true | false | "fallback" (only when GitHub alerts are off)
        "osv_scanner": "osv-scanner",
        "gitleaks": "gitleaks",
        "commits": 100,                 # gitleaks scans this many recent commits
        "max_repo_mb": 500,
        "workers": 3,
        "timeout_s": 600,
    },
    "cache_dir": "~/.cache/repo_watch",
    "cache_prune_days": 30,
    "state_path": "",                   # default: $HERMES_HOME/state/repo_watch.json
    "secret_command": [],               # e.g. ["vault", "get"]; env vars are checked first
    "ignore_repos": [],                 # fnmatch on "gh/owner/name", "fj/owner/name" or bare name
    "ignore_findings": [],              # fnmatch on finding ids or advisory ids (GHSA-..., CVE-...)
    "severity_threshold": "medium",     # weekly report lists findings at or above this
    "alert_severity": "high",           # daily alert fires for NEW findings at or above this
    "active_days": 30,                  # "active repo" = pushed within this many days
    "stale_days": 180,
    "max_items": 8,                     # per list in the report
    "message_limit": 1900,
    "title": "Repo watch",
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def hermes_home() -> Path | None:
    h = os.environ.get("HERMES_HOME")
    return Path(h).expanduser() if h else None


def default_config_path() -> Path:
    if os.environ.get("REPO_WATCH_CONFIG"):
        return Path(os.environ["REPO_WATCH_CONFIG"]).expanduser()
    hh = hermes_home()
    if hh:
        return hh / "config" / "repo_watch.json"
    return Path("~/.config/repo_watch.json").expanduser()


def load_config(path: Path | None) -> dict:
    data = {}
    if path and path.exists():
        data = json.loads(path.read_text())
    cfg = deep_merge(DEFAULT_CONFIG, data)
    for key in ("severity_threshold", "alert_severity"):
        if normalize_severity(cfg[key]) == "unknown":
            raise ValueError(f"{key} must be one of {SEVERITIES}")
        cfg[key] = normalize_severity(cfg[key])
    return cfg


def state_path(cfg: dict) -> Path:
    if cfg.get("state_path"):
        return Path(cfg["state_path"]).expanduser()
    hh = hermes_home()
    if hh:
        return hh / "state" / "repo_watch.json"
    return Path("~/.local/state/repo_watch/state.json").expanduser()


def get_secret(name: str, cfg: dict) -> str:
    if not name:
        return ""
    if os.environ.get(name):
        return os.environ[name]
    cmd = [os.path.expanduser(c) for c in cfg.get("secret_command") or []]
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd + [name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


# --------------------------------------------------------------------------- helpers

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def ago(t: dt.datetime | None, now: dt.datetime) -> str:
    if not t:
        return "never"
    s = (now - t).total_seconds()
    if s < 3600:
        return f"{max(1, int(s // 60))}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    d = int(s // 86400)
    if d < 60:
        return f"{d}d ago"
    return f"{d // 30}mo ago"


def normalize_severity(s) -> str:
    s = str(s or "").strip().lower()
    if s in ("moderate", "medium", "warning"):
        return "medium"
    if s in ("low", "note", "negligible"):
        return "low"
    if s in ("high", "error"):
        return "high"
    if s == "critical":
        return "critical"
    return "unknown"


def cvss_to_severity(score) -> str:
    try:
        v = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if v >= 9.0:
        return "critical"
    if v >= 7.0:
        return "high"
    if v >= 4.0:
        return "medium"
    if v > 0:
        return "low"
    return "unknown"


def sev_at_least(sev: str, threshold: str) -> bool:
    return SEV_RANK.get(sev, 1) >= SEV_RANK[threshold]


def matches_any(value: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(value.lower(), p.lower()) for p in patterns or [])


def repo_ignored(key: str, patterns) -> bool:
    """key like 'gh/owner/name'. Patterns may target the key, 'owner/name' or bare 'name'."""
    parts = key.split("/", 1)
    owner_name = parts[1] if len(parts) > 1 else key
    bare = owner_name.split("/")[-1]
    return any(matches_any(v, patterns) for v in (key, owner_name, bare))


def finding_ignored(f: dict, patterns) -> bool:
    vals = [f["id"]] + list(f.get("aliases") or [])
    return any(matches_any(v, patterns) for v in vals)


def run(cmd, timeout=120, env=None, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)


def which(name: str) -> str | None:
    p = os.path.expanduser(name)
    if os.path.sep in p:
        return p if os.access(p, os.X_OK) else None
    found = shutil.which(p)
    if found:
        return found
    local = Path("~/.local/bin").expanduser() / p
    return str(local) if os.access(local, os.X_OK) else None


# --------------------------------------------------------------------------- parsers

def latest_ci(runs: list[dict], branch: str, kind: str) -> dict:
    """Latest completed run per workflow on `branch`. Returns {workflow: status} where status
    is 'success' | 'failure' | other conclusion. Runs must be newest-first."""
    out: dict[str, str] = {}
    for r in runs:
        if kind == "github":
            if r.get("head_branch") != branch or r.get("event") == "pull_request":
                continue
            if r.get("status") != "completed":
                continue
            wf = r.get("name") or r.get("path") or str(r.get("workflow_id"))
            concl = r.get("conclusion") or "unknown"
            if concl in ("timed_out", "startup_failure"):
                concl = "failure"
        else:  # forgejo / gitea
            if r.get("prettyref") != branch or r.get("event") in ("pull_request", "pull_request_target"):
                continue
            st = r.get("status")
            if st in ("running", "waiting", "blocked", "unknown", None):
                continue
            wf = r.get("workflow_id") or "?"
            concl = st
        out.setdefault(wf, concl)
    return out


def dependabot_title(pkg: str, summary: str) -> str:
    summary = summary.strip()
    if summary.lower().startswith(pkg.lower()):
        summary = summary[len(pkg):].lstrip(" :-")
    return f"{pkg}: {summary}" if summary else pkg


def parse_dependabot(alerts: list[dict], repo: str) -> list[dict]:
    out = []
    for a in alerts:
        adv = a.get("security_advisory") or {}
        vuln = a.get("security_vulnerability") or {}
        pkg = (vuln.get("package") or {}).get("name") or (a.get("dependency") or {}).get("package", {}).get("name", "?")
        sev = normalize_severity(vuln.get("severity") or adv.get("severity"))
        aliases = [adv.get("ghsa_id"), adv.get("cve_id")]
        rec = {
            "id": f"dependabot:{repo}:{a.get('number')}", "repo": repo, "source": "dependabot",
            "severity": sev, "title": dependabot_title(pkg, adv.get("summary") or adv.get("ghsa_id") or ""),
            "aliases": [x for x in aliases if x], "url": a.get("html_url", ""),
        }
        patched = (vuln.get("first_patched_version") or {}).get("identifier")
        if patched:
            eco = (vuln.get("package") or {}).get("ecosystem", "")
            rec["fix"] = fix_command({"pip": "pypi"}.get(eco, eco), pkg, patched)
        out.append(rec)
    return out


def parse_code_scanning(alerts: list[dict], repo: str) -> list[dict]:
    out = []
    for a in alerts:
        rule = a.get("rule") or {}
        sev = normalize_severity(rule.get("security_severity_level") or rule.get("severity"))
        loc = ((a.get("most_recent_instance") or {}).get("location") or {}).get("path", "")
        out.append({
            "id": f"code-scanning:{repo}:{a.get('number')}", "repo": repo, "source": "code-scanning",
            "severity": sev, "title": f"{rule.get('id') or rule.get('name')} {loc}".strip(),
            "aliases": [], "url": a.get("html_url", ""),
        })
    return out


def parse_secret_scanning(alerts: list[dict], repo: str) -> list[dict]:
    return [{
        "id": f"secret-scanning:{repo}:{a.get('number')}", "repo": repo, "source": "secret-scanning",
        "severity": "high", "title": a.get("secret_type_display_name") or a.get("secret_type") or "secret",
        "aliases": [], "url": a.get("html_url", ""),
    } for a in alerts]


def _vkey(v: str) -> tuple:
    """Loose version sort key: numeric parts compare as numbers (1.10.0 > 1.9.2)."""
    import re as _re
    return tuple((0, int(x)) if x.isdigit() else (1, x) for x in _re.split(r"[.\-+_]", (v or "").lstrip("v")) if x)


def pick_fix(current: str, fixed: list[str]) -> str:
    """Smallest fixed version above `current`, preferring the same major line."""
    cur = _vkey(current)
    above = sorted({f for f in fixed if f and _vkey(f) > cur}, key=_vkey)
    if not above:
        return ""
    same = [f for f in above if _vkey(f)[:1] == cur[:1]]
    return (same or above)[0]


def fix_command(eco: str, name: str, version: str) -> str:
    e = (eco or "").lower()
    if e == "npm":
        return f"npm install {name}@{version}"
    if e == "pypi":
        return f"uv pip install \"{name}>={version}\""
    if e == "go":
        return f"go get {name}@v{version.lstrip('v')}"
    if e == "crates.io":
        return f"cargo update -p {name} --precise {version}"
    if e == "rubygems":
        return f"bundle update {name}"
    return f"upgrade {name} to {version}"


def osv_fixed_versions(vuln: dict, name: str) -> list[str]:
    out = []
    for aff in vuln.get("affected") or []:
        if (aff.get("package") or {}).get("name", "").lower() != name.lower():
            continue
        for r in aff.get("ranges") or []:
            out += [ev["fixed"] for ev in r.get("events") or [] if ev.get("fixed")]
    return out


def parse_osv(data: dict, repo: str, root: str = "") -> list[dict]:
    """osv-scanner v2 JSON -> findings, one per (package, version, vuln group)."""
    out = []
    for res in data.get("results") or []:
        src = ((res.get("source") or {}).get("path") or "")
        if root and src.startswith(root):
            src = src[len(root):].lstrip("/")
        for p in res.get("packages") or []:
            pkg = p.get("package") or {}
            name, ver, eco = pkg.get("name", "?"), pkg.get("version", "?"), pkg.get("ecosystem", "")
            vulns = {v.get("id"): v for v in p.get("vulnerabilities") or []}
            groups = p.get("groups") or [{"ids": [vid]} for vid in vulns]
            for g in groups:
                ids = [i for i in g.get("ids") or [] if i]
                aliases = sorted(set(ids + list(g.get("aliases") or [])))
                if not ids:
                    continue
                sev = cvss_to_severity(g.get("max_severity"))
                if sev == "unknown":
                    for vid in ids:
                        ds = (vulns.get(vid) or {}).get("database_specific") or {}
                        s = normalize_severity(ds.get("severity"))
                        if s != "unknown" and (sev == "unknown" or SEV_RANK[s] > SEV_RANK[sev]):
                            sev = s
                primary = sorted(ids, key=lambda i: (not i.startswith("GHSA"), not i.startswith("CVE"), i))[0]
                summary = ""
                fixed = []
                for vid in ids:
                    summary = (vulns.get(vid) or {}).get("summary") or summary
                    fixed += osv_fixed_versions(vulns.get(vid) or {}, name)
                fix_v = pick_fix(ver, fixed)
                rec = {
                    "id": f"osv:{repo}:{eco}/{name}@{ver}:{primary}", "repo": repo, "source": "osv",
                    "severity": sev, "title": f"{name} {ver} {primary}" + (f" ({src})" if src else ""),
                    "aliases": aliases, "summary": summary[:120], "url": f"https://osv.dev/{primary}",
                }
                if fix_v:
                    rec["fix"] = fix_command(eco, name, fix_v)
                out.append(rec)
    return out


def parse_gitleaks(items: list[dict], repo: str) -> list[dict]:
    out = []
    for it in items or []:
        commit = (it.get("Commit") or "")[:12]
        fp = it.get("Fingerprint") or f"{it.get('Commit')}:{it.get('File')}:{it.get('RuleID')}:{it.get('StartLine')}"
        out.append({
            "id": f"gitleaks:{repo}:{it.get('RuleID')}:{it.get('File')}:{commit}", "repo": repo,
            "source": "gitleaks", "severity": "high",
            "title": f"{it.get('RuleID')} in {it.get('File')}:{it.get('StartLine')} @{commit[:7]}",
            "aliases": [], "fingerprint": fp,
        })
    return out


def uptime_from_beats(beats: list) -> float | None:
    """Percent of UP beats (status 1); maintenance (3) is excluded from the denominator."""
    total = up = 0
    for b in beats:
        st = b.get("status") if isinstance(b, dict) else b
        st = int(getattr(st, "value", st))
        if st == 3:
            continue
        total += 1
        up += 1 if st == 1 else 0
    return round(100.0 * up / total, 2) if total else None


def status_since(beats: list, status: int) -> str | None:
    """Time of the first beat of the current status streak ('YYYY-MM-DD HH:MM', Kuma's UTC)."""
    since = None
    for b in reversed(beats or []):
        st = int(getattr(b.get("status"), "value", b.get("status")))
        if st != status:
            break
        since = b.get("time")
    return since[:16] if since else None


# --------------------------------------------------------------------------- GitHub

class GitHub:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.gh = which("gh")

    def api(self, path: str):
        cmd = [self.gh, "api", "-H", "Accept: application/vnd.github+json", path]
        r = run(cmd, timeout=120)
        if r.returncode != 0:
            msg = (r.stdout or "") + (r.stderr or "")
            code = 0
            for c in (403, 404, 401, 422, 451):
                if f"HTTP {c}" in msg or f'"status":"{c}"' in msg:
                    code = c
                    break
            raise ApiError(code, msg.strip().splitlines()[0][:200] if msg.strip() else "gh failed")
        txt = r.stdout.strip()
        return json.loads(txt) if txt else None

    def repos(self) -> list[dict]:
        if not self.gh:
            raise RuntimeError("gh CLI not found")
        out = []
        q = ("query($login:String!,$cursor:String){repositoryOwner(login:$login){repositories(first:100,after:$cursor,"
             "ownerAffiliations:OWNER,orderBy:{field:PUSHED_AT,direction:DESC}){pageInfo{hasNextPage endCursor} nodes{"
             "name nameWithOwner isArchived isFork isPrivate isEmpty pushedAt url diskUsage "
             "defaultBranchRef{name} issues(states:OPEN){totalCount} pullRequests(states:OPEN){totalCount}}}}}")
        for owner in self.cfg["github"]["owners"]:
            cursor = None
            while True:
                cmd = [self.gh, "api", "graphql", "-f", f"query={q}", "-f", f"login={owner}"]
                if cursor:
                    cmd += ["-f", f"cursor={cursor}"]
                r = run(cmd, timeout=120)
                if r.returncode != 0:
                    raise RuntimeError(f"gh graphql failed for {owner}: {r.stderr.strip()[:200]}")
                data = json.loads(r.stdout)["data"]["repositoryOwner"]
                if not data:
                    raise RuntimeError(f"GitHub owner {owner} not found")
                page = data["repositories"]
                for n in page["nodes"]:
                    out.append({
                        "key": f"gh/{n['nameWithOwner']}", "host": "github", "full_name": n["nameWithOwner"],
                        "name": n["name"], "archived": n["isArchived"], "fork": n["isFork"],
                        "private": n["isPrivate"], "empty": n["isEmpty"], "pushed_at": n["pushedAt"],
                        "url": n["url"], "size_kb": n.get("diskUsage") or 0,
                        "default_branch": (n.get("defaultBranchRef") or {}).get("name"),
                        "open_issues": n["issues"]["totalCount"], "open_prs": n["pullRequests"]["totalCount"],
                        "clone_url": f"https://github.com/{n['nameWithOwner']}.git",
                    })
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]
        return out

    def ci(self, repo: dict) -> dict:
        b = repo.get("default_branch")
        if not b:
            return {}
        d = self.api(f"repos/{repo['full_name']}/actions/runs?branch={urllib.parse.quote(b)}&per_page=30"
                     "&exclude_pull_requests=true")
        return latest_ci((d or {}).get("workflow_runs") or [], b, "github")

    def alerts(self, repo: dict) -> tuple[list[dict], dict]:
        """Returns (findings, coverage) where coverage maps feature -> 'on' | 'off' | 'error: ...'."""
        findings, cov = [], {}
        fn = repo["full_name"]
        for feat, path, parser in (
            ("dependabot", f"repos/{fn}/dependabot/alerts?state=open&per_page=100", parse_dependabot),
            ("code-scanning", f"repos/{fn}/code-scanning/alerts?state=open&per_page=100", parse_code_scanning),
            ("secret-scanning", f"repos/{fn}/secret-scanning/alerts?state=open&per_page=100", parse_secret_scanning),
        ):
            try:
                data = self.api(path) or []  # first 100 open alerts is plenty for a report
                findings += parser(data, repo["key"])
                cov[feat] = "on"
            except ApiError as e:
                if e.code in (403, 404):
                    # 404 "no analysis found" = code scanning available but never run.
                    cov[feat] = "off"
                else:
                    cov[feat] = f"error: {e.msg}"
        return findings, cov


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


# --------------------------------------------------------------------------- Forgejo

class Forgejo:
    def __init__(self, cfg: dict):
        fc = cfg["forgejo"]
        self.base = (fc.get("url") or get_secret(fc.get("url_var"), cfg)).rstrip("/")
        self.token = get_secret(fc.get("token_var"), cfg)
        self.cfg = cfg

    def get(self, path: str):
        req = urllib.request.Request(f"{self.base}/api/v1/{path.lstrip('/')}")
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("Authorization", f"token {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, f"HTTP {e.code} {path.split('?')[0]}") from None

    def repos(self) -> list[dict]:
        if not self.base:
            raise RuntimeError("Forgejo URL not configured")
        out, page = [], 1
        while True:
            batch = self.get(f"user/repos?limit=50&page={page}") or []
            for r in batch:
                out.append({
                    "key": f"fj/{r['full_name']}", "host": "forgejo", "full_name": r["full_name"],
                    "name": r["name"], "archived": r.get("archived", False), "fork": r.get("fork", False),
                    "mirror": r.get("mirror", False), "private": r.get("private", False),
                    "empty": r.get("empty", False), "pushed_at": r.get("updated_at"),
                    "url": r.get("html_url", ""), "size_kb": r.get("size") or 0,
                    "default_branch": r.get("default_branch"), "open_issues": r.get("open_issues_count", 0),
                    "open_prs": r.get("open_pr_counter", 0), "clone_url": r.get("clone_url", ""),
                    "has_actions": r.get("has_actions", False),
                })
            if len(batch) < 50:
                break
            page += 1
        return out

    def last_push(self, repo: dict) -> str | None:
        b = repo.get("default_branch")
        if not b or repo.get("empty"):
            return repo.get("pushed_at")
        try:
            d = self.get(f"repos/{repo['full_name']}/branches/{urllib.parse.quote(b, safe='')}")
            return ((d or {}).get("commit") or {}).get("timestamp") or repo.get("pushed_at")
        except ApiError:
            return repo.get("pushed_at")

    def ci(self, repo: dict) -> dict:
        b = repo.get("default_branch")
        if not b or not repo.get("has_actions"):
            return {}
        try:
            d = self.get(f"repos/{repo['full_name']}/actions/runs?limit=50")
        except ApiError as e:
            if e.code == 404:
                return {}
            raise
        return latest_ci((d or {}).get("workflow_runs") or [], b, "forgejo")


# --------------------------------------------------------------------------- local scans

def git_env(repo: dict, fj: Forgejo | None) -> dict:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if repo["host"] == "forgejo" and fj and fj.token:
        host = urllib.parse.urlsplit(repo["clone_url"])
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{host.scheme}://{host.netloc}/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: token {fj.token}",
        })
    return env


def sync_clone(repo: dict, dest: Path, depth: int, env: dict) -> str:
    """Shallow clone or refresh `dest` to the tip of the default branch. Returns HEAD sha."""
    b = repo["default_branch"]
    if (dest / ".git").exists():
        r = run(["git", "-C", str(dest), "fetch", "--quiet", "--depth", str(depth), "--no-tags",
                 "origin", b], timeout=600, env=env)
        if r.returncode == 0:
            run(["git", "-C", str(dest), "reset", "--quiet", "--hard", "FETCH_HEAD"], env=env)
            run(["git", "-C", str(dest), "clean", "-qfdx"], env=env)
        else:
            shutil.rmtree(dest, ignore_errors=True)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = run(["git", "clone", "--quiet", "--depth", str(depth), "--single-branch", "--branch", b,
                 "--no-tags", repo["clone_url"], str(dest)], timeout=900, env=env)
        if r.returncode != 0:
            raise RuntimeError("clone failed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:160])
    os.utime(dest, None)
    return run(["git", "-C", str(dest), "rev-parse", "HEAD"]).stdout.strip()


def osv_scan(binary: str, path: Path, repo_key: str, timeout: int) -> list[dict]:
    r = run([binary, "scan", "source", "-r", "--format", "json", "--allow-no-lockfiles", str(path)],
            timeout=timeout)
    if r.returncode not in (0, 1):
        tail = (r.stderr.strip().splitlines() or ["?"])[-1][:160]
        raise RuntimeError(f"osv-scanner exit {r.returncode}: {tail}")
    out = r.stdout.strip()
    if not out:
        return []
    return parse_osv(json.loads(out), repo_key, root=str(path))


def gitleaks_scan(binary: str, path: Path, repo_key: str, commits: int, timeout: int) -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        report = tf.name
    try:
        r = run([binary, "git", "--no-banner", "--redact", "--exit-code", "0", "--report-format", "json",
                 "--report-path", report, "--log-level", "error", "-i", str(path),
                 f"--log-opts=-n {int(commits)}", str(path)], timeout=timeout)
        if r.returncode != 0:
            tail = (r.stderr.strip().splitlines() or ["?"])[-1][:160]
            raise RuntimeError(f"gitleaks exit {r.returncode}: {tail}")
        txt = Path(report).read_text().strip()
        return parse_gitleaks(json.loads(txt) if txt else [], repo_key)
    finally:
        os.unlink(report)


def gitleaks_ignored(path: Path) -> set[str]:
    f = path / ".gitleaksignore"
    if not f.exists():
        return set()
    return {ln.strip() for ln in f.read_text(errors="ignore").splitlines() if ln.strip() and not ln.startswith("#")}


def cache_dir_for(cfg: dict, repo: dict) -> Path:
    return Path(cfg["cache_dir"]).expanduser() / repo["key"].replace("/", "__")


def prune_cache(cfg: dict, keep: set[Path], now: float | None = None) -> list[str]:
    root = Path(cfg["cache_dir"]).expanduser()
    if not root.exists():
        return []
    now = now or time.time()
    cutoff = now - cfg["cache_prune_days"] * 86400
    removed = []
    for d in root.iterdir():
        # Dirs used this run were just touched; anything else goes once it is older than the cutoff.
        if d.is_dir() and d not in keep and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed


def wants_local_scan(repo: dict, cov: dict, cfg: dict) -> tuple[bool, bool]:
    """(run osv, run gitleaks) for this repo."""
    ls = cfg["local_scan"]
    if repo.get("empty") or not repo.get("default_branch"):
        return False, False
    if repo.get("size_kb", 0) > ls["max_repo_mb"] * 1024:
        return False, False
    mode = ls["forgejo"] if repo["host"] == "forgejo" else ls["github"]
    if mode is True:
        return True, True
    if mode == "fallback":
        return cov.get("dependabot") != "on", cov.get("secret-scanning") != "on"
    return False, False


# --------------------------------------------------------------------------- Uptime Kuma

def kuma_collect(cfg: dict, attempts: int = 2) -> dict:
    for i in range(attempts):
        try:
            return _kuma_collect(cfg)
        except Exception:  # noqa: BLE001 - socket.io hiccups are common; retry once
            if i == attempts - 1:
                raise
            time.sleep(5)
    return {}


def _kuma_collect(cfg: dict) -> dict:
    kc = cfg["uptime_kuma"]
    url = (kc.get("url") or get_secret(kc.get("url_var"), cfg)).rstrip("/")
    user = get_secret(kc.get("username_var"), cfg)
    pw = get_secret(kc.get("password_var"), cfg)
    if not url:
        raise RuntimeError("Uptime Kuma URL not configured")
    try:
        import warnings
        warnings.filterwarnings("ignore", category=SyntaxWarning)
        from uptime_kuma_api import UptimeKumaApi  # type: ignore
    except ImportError:
        return kuma_from_metrics(url, user, pw, kc)
    api = UptimeKumaApi(url, timeout=60)
    try:
        api.login(user, pw)
        monitors = api.get_monitors()
        mtype_of = lambda m: str(getattr(m.get("type"), "value", m.get("type"))).lower()
        groups = {m["id"]: m["name"] for m in monitors if mtype_of(m) == "group"}
        out = []
        for m in monitors:
            if m["id"] in groups or not m.get("active", True):
                continue
            if matches_any(m["name"], kc["ignore_monitors"]):
                continue
            beats = api.get_monitor_beats(m["id"], int(kc["uptime_days"]) * 24)
            last = beats[-1] if beats else None
            status = int(getattr(last["status"], "value", last["status"])) if last else -1
            out.append({
                "name": m["name"], "project": groups.get(m.get("parent")) or m["name"].split(" — ")[0],
                "status": status, "uptime": uptime_from_beats(beats), "type": mtype_of(m),
                "msg": (last or {}).get("msg", "")[:100] if last else "",
                "since": status_since(beats, status),
            })
        return {"monitors": out, "source": "api"}
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def kuma_from_metrics(url: str, user: str, pw: str, kc: dict) -> dict:
    """Fallback: status only, from the Prometheus /metrics endpoint (no uptime history)."""
    import re
    req = urllib.request.Request(url + "/metrics")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode()
    out = []
    for m in re.finditer(r'^monitor_status\{monitor_name="([^"]*)",monitor_type="([^"]*)"[^}]*\}\s+(\d+)', text, re.M):
        name, mtype, st = m.group(1), m.group(2), int(m.group(3))
        if mtype == "group" or matches_any(name, kc["ignore_monitors"]):
            continue
        out.append({"name": name, "project": name.split(" — ")[0], "status": st, "uptime": None,
                    "type": mtype, "msg": ""})
    return {"monitors": out, "source": "metrics"}


# --------------------------------------------------------------------------- collection

def collect(cfg: dict, log=lambda *a: None) -> dict:
    """Gather everything. Network-heavy; the rest of the module is pure."""
    rep = {"repos": [], "ci": {}, "findings": [], "coverage": {}, "errors": {}, "monitors": None,
           "scanned": {}, "gitleaks_ignore": {}}
    gh = fj = None
    if cfg["github"]["enabled"] and cfg["github"]["owners"]:
        gh = GitHub(cfg)
        try:
            rep["repos"] += gh.repos()
        except Exception as e:  # noqa: BLE001
            rep["errors"]["github"] = str(e)[:200]
    if cfg["forgejo"]["enabled"]:
        try:
            fj = Forgejo(cfg)
            rep["repos"] += fj.repos()
        except Exception as e:  # noqa: BLE001
            rep["errors"]["forgejo"] = str(e)[:200]

    def keep(r):
        side = cfg["github"] if r["host"] == "github" else cfg["forgejo"]
        if r.get("archived") and not side.get("include_archived"):
            return False
        if r.get("fork") and not side.get("include_forks", False):
            return False
        if r.get("mirror") and not side.get("include_mirrors", False):
            return False
        return not repo_ignored(r["key"], cfg["ignore_repos"])

    rep["repos"] = [r for r in rep["repos"] if keep(r)]

    def per_repo(r):
        res = {"ci": {}, "findings": [], "coverage": {}, "errors": {}}
        try:
            if r["host"] == "github":
                res["ci"] = gh.ci(r)
            else:
                r["pushed_at"] = fj.last_push(r)
                res["ci"] = fj.ci(r)
        except Exception as e:  # noqa: BLE001
            res["errors"][f"ci:{r['key']}"] = str(e)[:160]
        if r["host"] == "github":
            try:
                res["findings"], res["coverage"] = gh.alerts(r)
            except Exception as e:  # noqa: BLE001
                res["errors"][f"alerts:{r['key']}"] = str(e)[:160]
        return r, res

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for r, res in ex.map(per_repo, rep["repos"]):
            rep["ci"][r["key"]] = res["ci"]
            rep["findings"] += res["findings"]
            rep["coverage"][r["key"]] = res["coverage"]
            rep["errors"].update(res["errors"])

    # Local scans
    ls = cfg["local_scan"]
    osv_bin, gl_bin = which(ls["osv_scanner"]), which(ls["gitleaks"])
    targets = []
    for r in rep["repos"]:
        do_osv, do_gl = wants_local_scan(r, rep["coverage"].get(r["key"], {}), cfg)
        do_osv, do_gl = do_osv and bool(osv_bin), do_gl and bool(gl_bin)
        if do_osv or do_gl:
            targets.append((r, do_osv, do_gl))
    if targets and not (osv_bin and gl_bin):
        rep["errors"]["local_scan"] = "osv-scanner or gitleaks binary not found"

    def scan(t):
        r, do_osv, do_gl = t
        res = {"findings": [], "errors": {}, "scanned": [], "ignore": set()}
        dest = cache_dir_for(cfg, r)
        try:
            sync_clone(r, dest, int(ls["commits"]), git_env(r, fj))
        except Exception as e:  # noqa: BLE001
            res["errors"][f"clone:{r['key']}"] = str(e)[:160]
            return r, res, dest
        if do_osv:
            try:
                res["findings"] += osv_scan(osv_bin, dest, r["key"], int(ls["timeout_s"]))
                res["scanned"].append("osv")
            except Exception as e:  # noqa: BLE001
                res["errors"][f"osv:{r['key']}"] = str(e)[:160]
        if do_gl:
            try:
                res["findings"] += gitleaks_scan(gl_bin, dest, r["key"], int(ls["commits"]), int(ls["timeout_s"]))
                res["scanned"].append("gitleaks")
                res["ignore"] = gitleaks_ignored(dest)
            except Exception as e:  # noqa: BLE001
                res["errors"][f"gitleaks:{r['key']}"] = str(e)[:160]
        return r, res, dest

    keep_dirs = set()
    with cf.ThreadPoolExecutor(max_workers=max(1, int(ls["workers"]))) as ex:
        for r, res, dest in ex.map(scan, targets):
            log(f"scanned {r['key']}: {res['scanned']} {len(res['findings'])} findings")
            keep_dirs.add(dest)
            rep["findings"] += res["findings"]
            rep["errors"].update(res["errors"])
            rep["scanned"][r["key"]] = res["scanned"]
            rep["gitleaks_ignore"][r["key"]] = sorted(res["ignore"])
    rep["pruned"] = prune_cache(cfg, keep_dirs)

    if cfg["uptime_kuma"]["enabled"]:
        try:
            rep["monitors"] = kuma_collect(cfg)
        except Exception as e:  # noqa: BLE001
            rep["errors"]["uptime_kuma"] = str(e)[:200]
    return rep


# --------------------------------------------------------------------------- state / diff

def load_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, path)


def failing_ci(ci: dict) -> dict[str, str]:
    """{'repo_key::workflow': 'failure'} for every failing latest run."""
    return {f"{rk}::{wf}": st for rk, wfs in ci.items() for wf, st in (wfs or {}).items() if st == "failure"}


def down_monitors(mon: dict | None) -> dict[str, int]:
    if not mon:
        return {}
    return {m["name"]: m["status"] for m in mon["monitors"] if m["status"] in (0, 2)}


def update_state(prev: dict | None, rep: dict, cfg: dict, now: dt.datetime) -> tuple[dict, dict]:
    """Merge this run into the state; return (new_state, diff). Pure."""
    prev = prev or {}
    first_run = not prev
    ts = now.isoformat(timespec="seconds")
    old_f = prev.get("findings", {})

    current = {f["id"]: f for f in rep["findings"] if not finding_ignored(f, cfg["ignore_findings"])}
    # gitleaks findings are sticky: leaked secrets stay in history even after they roll out of the
    # scanned commit window. They clear only via ignore_findings or the repo's .gitleaksignore, or
    # when the repo itself disappears / is ignored.
    repo_keys = {r["key"] for r in rep["repos"]}
    for fid, f in old_f.items():
        if f.get("source") != "gitleaks" or fid in current or f.get("repo") not in repo_keys:
            continue
        if finding_ignored({"id": fid, "aliases": []}, cfg["ignore_findings"]):
            continue
        if f.get("fingerprint") in set(rep.get("gitleaks_ignore", {}).get(f["repo"], [])):
            continue
        current[fid] = dict(f, id=fid)
    # A repo whose scan/alerts errored this run keeps its previous findings (no false "resolved").
    errored = {k.split(":", 1)[1] for k in rep["errors"] if ":" in k}
    for fid, f in old_f.items():
        if fid not in current and f.get("repo") in errored and f.get("repo") in repo_keys:
            current[fid] = dict(f, id=fid, stale=True)

    findings = {}
    for fid, f in current.items():
        keep_keys = ("repo", "source", "severity", "title", "aliases", "url", "fingerprint", "summary", "fix")
        rec = {k: f[k] for k in keep_keys if f.get(k) not in (None, "", [])}
        rec["first_seen"] = old_f.get(fid, {}).get("first_seen", ts)
        findings[fid] = rec
    new_findings = [dict(findings[i], id=i) for i in findings if i not in old_f]
    resolved = [dict(old_f[i], id=i) for i in old_f if i not in findings]

    def track(old: dict, cur: dict) -> tuple[dict, list, list]:
        merged = {k: old.get(k, ts) for k in cur}
        return merged, [k for k in cur if k not in old], [k for k in old if k not in cur]

    ci_state, ci_new, ci_fixed = track(prev.get("ci_failing", {}), failing_ci(rep["ci"]))
    if rep.get("monitors") is None:  # Kuma unreachable: keep last known state
        down_state, down_new, down_up = prev.get("down", {}), [], []
    else:
        down_state, down_new, down_up = track(prev.get("down", {}), down_monitors(rep["monitors"]))
    src_errors = {k: v for k, v in rep["errors"].items() if ":" not in k}
    err_state, err_new, err_gone = track(prev.get("errors", {}), src_errors)

    old_repos = set(prev.get("repos", []))
    state = {
        "version": 1, "last_run": ts, "findings": findings, "ci_failing": ci_state, "down": down_state,
        "errors": err_state, "repos": sorted(repo_keys),
        # Everything found on the very first run is the baseline, never reported as "new".
        "baseline": prev.get("baseline", ts),
    }
    diff = {
        "first_run": first_run, "new_findings": new_findings, "resolved_findings": resolved,
        "ci_new": ci_new, "ci_fixed": ci_fixed, "down_new": down_new, "down_recovered": down_up,
        "errors_new": err_new, "errors_gone": err_gone,
        "repos_new": sorted(repo_keys - old_repos) if old_repos else [],
        "repos_gone": sorted(old_repos - repo_keys) if old_repos else [],
        "prev_run": prev.get("last_run"),
    }
    return state, diff


# --------------------------------------------------------------------------- rendering

def short(key: str) -> str:
    """'gh/owner/name' -> 'gh/name'."""
    host, _, rest = key.partition("/")
    return f"{host}/{rest.split('/')[-1]}"


def clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def esc(s: str) -> str:
    for ch in ("*", "_", "~", "|", ">"):
        s = s.replace(ch, "\\" + ch)
    return s


def bullets(items: list[str], limit: int) -> list[str]:
    out = items[:limit]
    if len(items) > limit:
        out.append(f"- …and {len(items) - limit} more")
    return out


def paginate(blocks: list[str], limit: int) -> list[str]:
    """Pack blocks (sections) into messages of at most `limit` chars, never splitting a block
    unless it alone is too long (then split on lines)."""
    msgs, cur = [], ""
    for b in blocks:
        b = b.strip("\n")
        if not b:
            continue
        pieces = [b]
        if len(b) > limit:
            pieces, acc = [], ""
            for ln in b.split("\n"):
                ln = ln if len(ln) <= limit else ln[: limit - 1] + "…"
                if acc and len(acc) + 1 + len(ln) > limit:
                    pieces.append(acc)
                    acc = ln
                else:
                    acc = f"{acc}\n{ln}" if acc else ln
            if acc:
                pieces.append(acc)
        for p in pieces:
            if cur and len(cur) + 2 + len(p) > limit:
                msgs.append(cur)
                cur = p
            else:
                cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        msgs.append(cur)
    return msgs


def sort_findings(fs: list[dict], now_iso_cut: str = "") -> list[dict]:
    return sorted(fs, key=lambda f: (-SEV_RANK.get(f.get("severity"), 1), f.get("first_seen", "") < now_iso_cut,
                                     f.get("repo", ""), f.get("title", "")))


def finding_line(f: dict, new: bool) -> str:
    sev = f.get("severity", "unknown")
    tag = "🆕 " if new else ""
    src = {"dependabot": "dependabot", "code-scanning": "code scan", "secret-scanning": "secret scan",
           "osv": "osv", "gitleaks": "gitleaks"}.get(f.get("source"), f.get("source", ""))
    line = f"- {tag}{SEV_EMOJI.get(sev, '🟡')} `{short(f['repo'])}` {esc(clip(f.get('title', ''), 70))} _({src})_"
    if f.get("fix"):
        line += f"\n  ↳ fix: `{clip(f['fix'], 80)}`"
    return line


SECRET_SOURCES = ("gitleaks", "secret-scanning")


def finding_name(f: dict) -> str:
    """Short label: package name for dependency vulns, rule for code/secret findings."""
    t = f.get("title", "")
    if f.get("source") == "osv":
        return t.split(" ")[0]
    if f.get("source") == "dependabot":
        return t.split(":")[0]
    return t.split(" in ")[0].split(" ")[0]


def group_findings(fs: list[dict]) -> list[dict]:
    """Aggregate findings per repo (vulns) and per repo+file (secrets), worst first."""
    groups: dict[tuple, dict] = {}
    for f in fs:
        secret = f.get("source") in SECRET_SOURCES
        g = groups.setdefault((f["repo"], secret), {"repo": f["repo"], "secret": secret, "findings": []})
        g["findings"].append(f)
    out = []
    for g in groups.values():
        c = sev_counts(g["findings"])
        worst = max(g["findings"], key=lambda f: SEV_RANK.get(f.get("severity"), 1))["severity"]
        names: list[str] = []
        if g["secret"]:
            files = {}
            for f in g["findings"]:
                fname = (f.get("title", "").split(" in ", 1)[-1]).rsplit(":", 1)[0].split(" @")[0]
                files[fname] = files.get(fname, 0) + 1
            names = [k for k, _ in sorted(files.items(), key=lambda kv: -kv[1])]
        else:
            for f in sorted(g["findings"], key=lambda f: -SEV_RANK.get(f.get("severity"), 1)):
                n = finding_name(f)
                if n not in names:
                    names.append(n)
        out.append(dict(g, counts=c, worst=worst, names=names, n=len(g["findings"])))
    return sorted(out, key=lambda g: (not g["secret"], -SEV_RANK.get(g["worst"], 1),
                                      -sum(g["counts"][s] for s in ("critical", "high")), -g["n"], g["repo"]))


def group_line(g: dict, new_ids: set | None = None) -> str:
    c = g["counts"]
    sev = " ".join(f"{c[s]}{SEV_EMOJI[s]}" for s in ("critical", "high", "medium", "low") if c[s])
    n_new = sum(1 for f in g["findings"] if new_ids is not None and f["id"] in new_ids)
    tag = f" (🆕 {n_new})" if n_new else ""
    names = ", ".join(g["names"][:3]) + (f" +{len(g['names']) - 3}" if len(g["names"]) > 3 else "")
    if g["secret"]:
        return f"- 🔑 `{short(g['repo'])}` {g['n']} possible secret(s){tag} in {esc(clip(names, 70))}"
    return f"- {SEV_EMOJI.get(g['worst'], '🟡')} `{short(g['repo'])}` {sev}{tag} — {esc(clip(names, 60))}"


def sev_counts(fs) -> dict[str, int]:
    c = {s: 0 for s in ["critical", "high", "medium", "low", "unknown"]}
    for f in fs:
        c[f.get("severity", "unknown")] = c.get(f.get("severity", "unknown"), 0) + 1
    return c


def counts_str(c: dict) -> str:
    parts = [f"{SEV_EMOJI[s]} {c[s]} {s}" for s in ("critical", "high", "medium", "low", "unknown") if c.get(s)]
    return " · ".join(parts) if parts else "✅ none"


def status_emoji(st: int) -> str:
    return {0: "🔴", 1: "🟢", 2: "🟡", 3: "🔧"}.get(st, "⚪")


def project_uptime(monitors: list[dict]) -> list[tuple[str, float | None, list[dict]]]:
    by: dict[str, list[dict]] = {}
    for m in monitors:
        by.setdefault(m["project"], []).append(m)
    out = []
    for p, ms in sorted(by.items()):
        ups = [m["uptime"] for m in ms if m["uptime"] is not None]
        out.append((p, round(sum(ups) / len(ups), 2) if ups else None, ms))
    return out


def render_weekly(rep: dict, state: dict, diff: dict, cfg: dict, now: dt.datetime) -> str:
    lim = cfg["max_items"]
    week_ago = (now - dt.timedelta(days=7)).isoformat(timespec="seconds")
    repos = rep["repos"]
    n_gh = sum(r["host"] == "github" for r in repos)
    n_fj = sum(r["host"] == "forgejo" for r in repos)
    pushed = lambda r, days: (parse_ts(r.get("pushed_at")) or now - dt.timedelta(days=9999)) > now - dt.timedelta(days=days)
    findings = [dict(f, id=i) for i, f in state["findings"].items()]
    fail = state["ci_failing"]
    blocks = []

    # --- header / summary
    hdr = [f"## 🛰️ {cfg['title']} — weekly ({now.strftime('%b %d')})"]
    hdr.append(f"**Repos:** {n_gh} GitHub · {n_fj} Forgejo · {sum(pushed(r, 7) for r in repos)} pushed this week")
    hdr.append("**CI:** " + (f"🔴 {len(fail)} failing workflow(s)" if fail else "✅ all default branches green"))
    base = state.get("baseline", "")
    new_week = [f for f in findings if f.get("first_seen", "") >= week_ago and f.get("first_seen") != base]
    hdr.append(f"**Security:** {counts_str(sev_counts(findings))}"
               + (f" (🆕 {len(new_week)} this week)" if new_week else ""))
    mon = rep.get("monitors")
    if mon:
        down = [m for m in mon["monitors"] if m["status"] in (0, 2)]
        hdr.append("**Uptime:** " + (f"🔴 {len(down)} monitor(s) down" if down else
                                      f"🟢 all {len(mon['monitors'])} monitors up"))
    elif cfg["uptime_kuma"]["enabled"]:
        hdr.append("**Uptime:** ⚠️ Uptime Kuma unreachable")
    blocks.append("\n".join(hdr))

    # --- failing CI
    if fail:
        lines = ["### 🔴 Failing CI (default branch)"]
        items = [f"- `{short(k.split('::')[0])}` {esc(k.split('::', 1)[1])} — since {v[:10]}"
                 for k, v in sorted(fail.items())]
        blocks.append("\n".join(lines + bullets(items, lim)))

    # --- security
    shown = [f for f in findings if sev_at_least(f.get("severity", "unknown"), cfg["severity_threshold"])]
    if findings:
        lines = [f"### 🛡️ Security (≥ {cfg['severity_threshold']})"]
        new_ids = {f["id"] for f in new_week}
        groups = group_findings(shown)
        sec = [group_line(g, new_ids) for g in groups if g["secret"]]
        dep = [group_line(g, new_ids) for g in groups if not g["secret"]]
        if sec:
            lines += ["**🔑 Possible secrets in git (rotate or ignore)**"] + bullets(sec, lim)
        if dep:
            lines += ["**📦 Vulnerable dependencies / code alerts**"] + bullets(dep, lim)
        if not groups:
            lines.append(f"- nothing at or above {cfg['severity_threshold']}")
        hidden = len(findings) - len(shown)
        if hidden:
            lines.append(f"- _{hidden} lower-severity finding(s) not listed_")
        res_week = [f for f in diff["resolved_findings"]]
        if res_week:
            lines.append(f"- ✅ {len(res_week)} resolved since last run")
        blocks.append("\n".join(lines))
    elif diff["resolved_findings"]:
        blocks.append(f"### 🛡️ Security\n- ✅ {len(diff['resolved_findings'])} resolved, nothing open")

    # --- uptime
    if mon:
        lines = [f"### 📡 Uptime ({cfg['uptime_kuma']['uptime_days']}d)"]
        for p, up, ms in project_uptime(mon["monitors"]):
            worst = min(ms, key=lambda m: (m["status"] != 0, m["uptime"] if m["uptime"] is not None else 101))
            em = "🔴" if any(m["status"] == 0 for m in ms) else (
                "🟡" if (up is not None and up < cfg["uptime_kuma"]["degraded_below"]) or any(m["status"] == 2 for m in ms)
                else "🟢")
            upt = f"{up:.2f}%" if up is not None else "n/a"
            lines.append(f"- {em} **{esc(p)}** {upt} ({len(ms)} monitors)")
        bad = [m for m in mon["monitors"] if m["status"] in (0, 2)
               or (m["uptime"] is not None and m["uptime"] < cfg["uptime_kuma"]["degraded_below"])]
        for m in sorted(bad, key=lambda m: (m["status"] != 0, m["uptime"] or 0))[:lim]:
            upt = f"{m['uptime']:.1f}%" if m["uptime"] is not None else "n/a"
            since = m.get("since") or state["down"].get(m["name"], "")[:16].replace("T", " ")
            tail = f" — down since {since} UTC" if m["status"] == 0 and since else ""
            lines.append(f"  - {status_emoji(m['status'])} {esc(clip(m['name'], 60))} {upt}{tail}")
        blocks.append("\n".join(lines))

    # --- activity
    active = sorted([r for r in repos if pushed(r, cfg["active_days"])],
                    key=lambda r: r.get("pushed_at") or "", reverse=True)
    lines = [f"### 📦 Active repos (pushed ≤{cfg['active_days']}d)"]
    items = []
    for r in active:
        ci = rep["ci"].get(r["key"]) or {}
        ci_s = "" if not ci else (" · CI 🔴" if any(v == "failure" for v in ci.values()) else " · CI ✅")
        extra = []
        if r.get("open_prs"):
            extra.append(f"{r['open_prs']} PR")
        if r.get("open_issues"):
            extra.append(f"{r['open_issues']} issues")
        items.append(f"- `{short(r['key'])}` {ago(parse_ts(r.get('pushed_at')), now)}"
                     + (" · " + " · ".join(extra) if extra else "") + ci_s)
    lines += bullets(items, max(lim, 12)) if items else ["- none"]
    busy = sorted([r for r in repos if r not in active and (r.get("open_prs") or r.get("open_issues"))],
                  key=lambda r: -(r.get("open_prs", 0) + r.get("open_issues", 0)))
    if busy:
        lines.append("Open items elsewhere: " + ", ".join(
            f"`{short(r['key'])}` {r.get('open_prs', 0)}PR/{r.get('open_issues', 0)}iss" for r in busy[:5]))
    stale = [r for r in repos if not pushed(r, cfg["stale_days"])]
    if stale:
        lines.append(f"_{len(stale)} repo(s) untouched >{cfg['stale_days']}d_")
    if diff["repos_new"]:
        lines.append("🆕 new: " + ", ".join(f"`{short(k)}`" for k in diff["repos_new"][:6]))
    blocks.append("\n".join(lines))

    # --- coverage / errors
    lines = ["### ⚙️ Coverage"]
    gh_repos = [r for r in repos if r["host"] == "github"]
    cov = rep["coverage"]
    off = lambda feat, rs: [r for r in rs if cov.get(r["key"], {}).get(feat) == "off"]
    dep_off = off("dependabot", gh_repos)
    if gh_repos:
        if dep_off:
            owner = gh_repos[0]["full_name"].split("/")[0]
            lines.append(f"- Dependabot alerts off on {len(dep_off)}/{len(gh_repos)} GitHub repos → enable in "
                         f"Settings → Code security, or `gh api -X PUT repos/{owner}/REPO/vulnerability-alerts`")
        pub = [r for r in gh_repos if not r.get("private")]
        ss_off, cs_off = off("secret-scanning", pub), off("code-scanning", pub)
        if ss_off or cs_off:
            lines.append(f"- Public repos without secret scanning: {len(ss_off)}, without code scanning: "
                         f"{len(cs_off)} (both free for public repos)")
    n_local = sum(1 for v in rep["scanned"].values() if v)
    if n_local:
        lines.append(f"- Local scans (osv-scanner/gitleaks): {n_local} repos")
    errs = rep["errors"]
    if errs:
        lines.append(f"- ⚠️ {len(errs)} check error(s): " + "; ".join(
            f"{k}: {clip(v, 60)}" for k, v in list(errs.items())[:4]))
    if len(lines) > 1:
        blocks.append("\n".join(lines))
    return "\n\n".join(paginate(blocks, cfg["message_limit"]))


def render_daily(rep: dict, state: dict, diff: dict, cfg: dict, now: dt.datetime) -> str:
    """Empty string unless something is new or newly broken."""
    lim = cfg["max_items"]
    if diff["first_run"]:
        n = len(state["findings"])
        return (f"## 🛰️ {cfg['title']} — baseline recorded\n- {len(rep['repos'])} repos, {n} open finding(s), "
                f"{len(state['ci_failing'])} failing CI, {len(state['down'])} monitor(s) down. "
                "Daily alerts will cover changes from now on.")
    new_f = [f for f in diff["new_findings"] if sev_at_least(f.get("severity", "unknown"), cfg["alert_severity"])]
    mon_names = {m["name"]: m for m in (rep.get("monitors") or {}).get("monitors", [])}
    if not (new_f or diff["ci_new"] or diff["down_new"] or diff["errors_new"]):
        return ""
    lines = [f"## 🚨 {cfg['title']} — new since {(diff['prev_run'] or '')[:10] or 'last run'}"]
    if diff["down_new"]:
        lines.append("**📡 Monitors down**")
        lines += bullets([f"- {status_emoji(mon_names.get(n, {}).get('status', 0))} {esc(clip(n, 70))}"
                          + (f" — {esc(clip(mon_names[n]['msg'], 60))}" if mon_names.get(n, {}).get("msg") else "")
                          for n in diff["down_new"]], lim)
    if diff["ci_new"]:
        lines.append("**🔴 CI newly failing**")
        lines += bullets([f"- `{short(k.split('::')[0])}` {esc(k.split('::', 1)[1])}" for k in diff["ci_new"]], lim)
    if new_f:
        lines.append(f"**🛡️ New findings (≥ {cfg['alert_severity']})**")
        if len(new_f) <= lim:
            lines += [finding_line(f, True) for f in sort_findings(new_f)]
        else:
            lines += bullets([group_line(g) for g in group_findings(new_f)], lim)
    if diff["errors_new"]:
        lines.append("**⚠️ Check errors**")
        lines += bullets([f"- {k}: {esc(clip(rep['errors'].get(k, ''), 100))}" for k in diff["errors_new"]], lim)
    good = []
    if diff["down_recovered"]:
        good.append(f"{len(diff['down_recovered'])} monitor(s) back up")
    if diff["ci_fixed"]:
        good.append(f"{len(diff['ci_fixed'])} CI workflow(s) fixed")
    if good:
        lines.append("✅ Also: " + ", ".join(good))
    return "\n\n".join(paginate(["\n".join(lines)], cfg["message_limit"]))


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=["weekly", "daily"], default="weekly")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="don't write the state file")
    ap.add_argument("--verbose", action="store_true", help="progress to stderr")
    a = ap.parse_args(argv)
    cfg = load_config(a.config or default_config_path())
    log = (lambda *x: print(*x, file=sys.stderr)) if a.verbose else (lambda *x: None)
    now = now_utc()
    sp = state_path(cfg)
    prev = load_state(sp)
    rep = collect(cfg, log)
    state, diff = update_state(prev, rep, cfg, now)
    out = (render_weekly if a.mode == "weekly" else render_daily)(rep, state, diff, cfg, now)
    if not a.dry_run:
        save_state(sp, state)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
