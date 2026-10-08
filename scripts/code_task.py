#!/usr/bin/env python3
"""Hand a coding job to a CLI coding agent (Codex, Claude Code) in a disposable sandbox and get a PR back.

    code_task.py run --repo <owner/name | forgejo:owner/name | github:owner/name | URL | /local/repo>
                     --task "<instruction>" [--agent codex|claude|dummy] [--base main] [--timeout 1800]
                     [--model M] [--no-push]
    code_task.py status <job-id> [--json]
    code_task.py list [--limit 20]

Flow (all git credentials stay on this host; the sandbox never sees them):
  1. clone the repo into $CODE_TASK_WORK_DIR/<job-id>/repo (default ~/.cache/code_tasks); the token is
     passed to git through GIT_CONFIG_* environment variables (http.extraHeader), never written to
     .git/config and never visible in the process list; create branch hermes/<slug>-<yyyymmdd>
  2. rsync the checkout to the sandbox (ssh alias, default "sandbox") under jobs/<job-id>/repo
  3. run the agent there non-interactively under a hard `timeout`, streaming its log back here
  4. rsync the working tree back (never .git, never symlinks that leave the tree, .gitignore honoured),
     drop HERMES_SUMMARY.md from the tree and keep it as the PR summary
  5. optional gitleaks scan of the staged diff (blocks the push on a hit), commit as
     "Hermes (via <Agent>)", push the branch (never the base branch, never --force), open a PR
     (Forgejo/Gitea API, or `gh pr create` for GitHub); never merges
  6. remove the job dir on the sandbox; logs, summary and diff stay in $HERMES_HOME/code_tasks/<job-id>/

The last stdout line is always machine-readable:
    CODE_TASK_RESULT status=<status> job=<job-id> pr=<url|-> branch=<branch|-> note=<text>

Config: $CODE_TASK_CONFIG, else $HERMES_HOME/config/code_task.json (all keys optional; see DEFAULT_CONFIG).
Secrets are read from the environment first, then via `secret_command + [NAME]` (e.g. a vault CLI).
Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_CONFIG: dict = {
    # ssh alias of the sandbox (configure HostName/User/IdentityFile in ~/.ssh/config), or
    # "local:/abs/dir" to run agents in a local directory (tests, single-host setups)
    "sandbox": "sandbox",
    "sandbox_jobs_dir": "jobs",            # relative to the sandbox user's home
    "work_dir": "",                        # default ~/.cache/code_tasks
    "state_dir": "",                       # default $HERMES_HOME/code_tasks
    "secret_command": [],                  # e.g. ["/path/python", "/path/vault_mcp.py", "get"]
    "forgejo": {
        "url": "",                         # e.g. https://git.example.com (or set url_var)
        "url_var": "FORGEJO_URL",
        "user": "",
        "user_var": "FORGEJO_USERNAME",
        "token_var": "FORGEJO_TOKEN",
    },
    "github": {"token_command": ["gh", "auth", "token"]},
    "author_email": "hermes@localhost",
    "author_name": "Hermes (via {agent})",
    "branch_prefix": "hermes/",
    "default_base": "",                    # "" = the repo's default branch
    "default_timeout": 1800,
    "clone_depth": 100,
    "max_file_size": "20m",                # rsync --max-size for files coming back
    "gitleaks": "auto",                    # auto (if installed) | required | off
    "keep_workdir_on_success": False,
    "agents": {
        "codex": {
            "label": "Codex",
            # the sandbox host itself is the isolation boundary, so codex's own sandbox is bypassed
            # (its bubblewrap/landlock sandbox does not work in unprivileged containers and blocks
            # the network that test suites need)
            "command": ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
                        "--skip-git-repo-check", "--color", "never",
                        "--output-last-message", "../LAST_MESSAGE.md", "-"],
            "model_flag": ["--model", "{model}"],
            "preflight": ["codex", "login", "status"],
            "preflight_hint": "Codex is not logged in on the sandbox; log in once with `codex login --device-auth` as the sandbox user",
        },
        "claude": {
            "label": "Claude Code",
            "command": ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "text"],
            "model_flag": ["--model", "{model}"],
            "preflight": ["claude", "--version"],
            "preflight_hint": "Claude Code is missing on the sandbox (and must be logged in with `claude` once)",
        },
        "dummy": {"label": "Dummy", "command": [], "model_flag": []},
    },
}

STANDING_INSTRUCTIONS = """\
You are an autonomous coding agent working in a fresh checkout of {repo} on branch {branch}
(based on {base}). Nobody will answer questions: decide sensibly and finish.

TASK:
{task}

RULES:
- Make the minimal, focused change that does the task. Follow the existing code style. No drive-by refactors.
- If the project has tests, linters or type checks, run the relevant ones and fix what your change broke.
- Do not commit, push, create branches or touch git config; the harness commits your working tree.
- No credentials are available here. Do not add secrets, tokens or personal data to the repository.
- When you are done (or if you must stop early) write HERMES_SUMMARY.md in the repository root,
  under 300 words, with these sections: "## What changed", "## Why", "## Tests" (commands run and
  results, or why none), "## Follow-ups" (anything left undone or risky). It is removed before commit
  and becomes the pull request description.
"""

SUMMARY_FILE = "HERMES_SUMMARY.md"
JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")
TERMINAL = {"done", "failed", "timeout", "no_changes", "blocked_secrets", "committed"}


# ----------------------------------------------------------------------------- config / helpers

def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("CODE_TASK_CONFIG") or hermes_home() / "config" / "code_task.json")
    data = json.loads(p.read_text()) if p.exists() else {}
    return deep_merge(DEFAULT_CONFIG, data)


def state_root(cfg: dict) -> Path:
    return Path(cfg.get("state_dir") or hermes_home() / "code_tasks").expanduser()


def work_root(cfg: dict) -> Path:
    return Path(cfg.get("work_dir") or Path.home() / ".cache" / "code_tasks").expanduser()


def get_secret(name: str | None, cfg: dict) -> str:
    """Env var first, then secret_command + [name]. Never logged."""
    if not name:
        return ""
    if os.environ.get(name):
        return os.environ[name]
    cmd = [os.path.expanduser(c) for c in (cfg.get("secret_command") or [])]
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd + [name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


LABEL_RE = re.compile(r"^\s*(#+\s*)?((goal|task|title|summary|objective)\s*:\s*)?", re.I)


def headline(text: str) -> str:
    """First non-empty line, without markdown heading marks or a leading 'Task:'-style label."""
    for ln in text.strip().splitlines():
        ln = LABEL_RE.sub("", ln).strip()
        if ln:
            return " ".join(ln.split())
    return ""


def slugify(text: str, max_words: int = 6, max_len: int = 40) -> str:
    words = re.findall(r"[a-z0-9]+", headline(text).lower())
    out = ""
    for w in words[:max_words]:
        cand = f"{out}-{w}" if out else w
        if len(cand) > max_len:
            if not out:
                out = w[:max_len]
            break
        out = cand
    return out.strip("-") or "task"


def branch_name(task: str, when: dt.datetime, prefix: str = "hermes/", taken: set[str] | None = None) -> str:
    base = f"{prefix}{slugify(task)}-{when:%Y%m%d}"
    taken = taken or set()
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def new_job_id(when: dt.datetime | None = None) -> str:
    when = when or dt.datetime.now()
    return f"{when:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def check_job_id(job_id: str) -> str:
    if not JOB_ID_RE.match(job_id or ""):
        raise SystemExit(f"invalid job id: {job_id!r}")
    return job_id


def tail(text: str, lines: int = 40, max_chars: int = 3000) -> str:
    t = "\n".join(text.rstrip().splitlines()[-lines:])
    return t[-max_chars:]


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean_log(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


# ----------------------------------------------------------------------------- repo spec

class RepoSpec:
    def __init__(self, kind: str, owner: str = "", name: str = "", clone_url: str = "", web_base: str = ""):
        self.kind, self.owner, self.name, self.clone_url, self.web_base = kind, owner, name, clone_url, web_base

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.name}" if self.owner else self.clone_url

    def __repr__(self) -> str:
        return f"RepoSpec({self.kind}, {self.full})"


def _split_owner_name(path: str) -> tuple[str, str]:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) != 2:
        raise ValueError(f"expected owner/name, got {path!r}")
    owner, name = parts
    name = name[:-4] if name.endswith(".git") else name
    if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise ValueError(f"bad owner/name: {path!r}")
    return owner, name


def parse_repo(spec: str, forgejo_url: str = "", forgejo_exists=None) -> RepoSpec:
    """Resolve a repo argument. forgejo_exists(owner, name) -> bool decides bare owner/name (default: Forgejo
    if it has the repo, else GitHub)."""
    s = spec.strip()
    fj = forgejo_url.rstrip("/")
    if s.startswith("/") or s.startswith("file://") or s.startswith("./"):
        path = s[7:] if s.startswith("file://") else s
        return RepoSpec("local", clone_url=str(Path(path).resolve()))
    for pre, kind in (("forgejo:", "forgejo"), ("gitea:", "forgejo"), ("github:", "github"), ("gh:", "github")):
        if s.startswith(pre):
            o, n = _split_owner_name(s[len(pre):])
            return _mk(kind, o, n, fj)
    if s.startswith("https://") or s.startswith("http://"):
        u = urllib.parse.urlparse(s)
        o, n = _split_owner_name(u.path)
        if u.netloc.lower() == "github.com":
            return _mk("github", o, n, fj)
        if fj and u.netloc.lower() == urllib.parse.urlparse(fj).netloc.lower():
            return _mk("forgejo", o, n, fj)
        raise ValueError(f"unknown git host in {spec!r} (only GitHub and the configured Forgejo)")
    o, n = _split_owner_name(s)
    if fj and (forgejo_exists is None or forgejo_exists(o, n)):
        return _mk("forgejo", o, n, fj)
    return _mk("github", o, n, fj)


def _mk(kind: str, owner: str, name: str, fj: str) -> RepoSpec:
    if kind == "github":
        return RepoSpec("github", owner, name, f"https://github.com/{owner}/{name}.git", "https://github.com")
    if not fj:
        raise ValueError("Forgejo repo requested but no Forgejo URL configured")
    return RepoSpec("forgejo", owner, name, f"{fj}/{owner}/{name}.git", fj)


# ----------------------------------------------------------------------------- PR text

def pr_title(task: str, max_len: int = 72) -> str:
    first = headline(task) or "coding task"
    t = first if len(first) <= max_len else first[: max_len - 1].rstrip() + "…"
    return t[0].upper() + t[1:]


def pr_body(*, task: str, summary: str, agent_label: str, job_id: str, diffstat: str, log_excerpt: str,
            elapsed_s: float) -> str:
    summary = summary.strip() or "_The agent did not write HERMES_SUMMARY.md._"
    if len(summary) > 8000:
        summary = summary[:8000] + "\n\n…(truncated)"
    task_q = "\n".join("> " + ln for ln in task.strip().splitlines()) or "> (empty)"
    parts = [
        f"Automated change by **{agent_label}**, handed off by Hermes. Review before merging; this PR is never auto-merged.",
        "### Task", task_q,
        "### Agent summary", summary,
    ]
    if diffstat.strip():
        parts += ["### Files", "```\n" + diffstat.strip()[-3000:] + "\n```"]
    if log_excerpt.strip():
        parts += ["<details><summary>Agent log (last lines)</summary>\n",
                  "```\n" + log_excerpt.replace("```", "ˋˋˋ") + "\n```", "</details>"]
    parts.append(f"<sub>job `{job_id}` · {int(elapsed_s // 60)}m{int(elapsed_s % 60):02d}s</sub>")
    return "\n\n".join(parts)


def commit_message(task: str, agent_label: str, job_id: str) -> str:
    return f"{pr_title(task, 68)}\n\nAutomated change by {agent_label} (Hermes code_task job {job_id}).\n"


def result_line(status: str, job_id: str, pr: str = "", branch: str = "", note: str = "") -> str:
    note = " ".join(note.split())[:200] or "-"
    return f"CODE_TASK_RESULT status={status} job={job_id} pr={pr or '-'} branch={branch or '-'} note={note}"


# ----------------------------------------------------------------------------- sandbox

class Sandbox:
    """Remote (ssh alias) or local-directory sandbox."""

    def __init__(self, target: str, jobs_dir: str = "jobs"):
        self.local = target.startswith("local:")
        self.host = "" if self.local else target
        self.jobs = target[len("local:"):] if self.local else jobs_dir.rstrip("/")

    def job_dir(self, job_id: str) -> str:
        return f"{self.jobs}/{check_job_id(job_id)}"

    def _rs_path(self, path: str) -> str:
        return path if self.local else f"{self.host}:{path}"

    def sh(self, script: str, *, stdout=None, timeout: float | None = None, check: bool = True,
           capture: bool = False) -> subprocess.CompletedProcess:
        cmd = ["bash", "-c", script] if self.local else ["ssh", "-o", "BatchMode=yes", self.host, script]
        kw: dict = {"timeout": timeout, "text": True, "stdin": subprocess.DEVNULL}
        if capture:
            kw.update(capture_output=True)
        elif stdout is not None:
            kw.update(stdout=stdout, stderr=subprocess.STDOUT)
        r = subprocess.run(cmd, **kw)
        if check and r.returncode != 0:
            raise RuntimeError(f"sandbox command failed ({r.returncode}): {script[:120]}")
        return r

    def prepare(self, job_id: str) -> None:
        self.sh(f"mkdir -p {shlex.quote(self.job_dir(job_id))}/repo")

    def push_tree(self, src: Path, job_id: str) -> None:
        dst = self._rs_path(self.job_dir(job_id) + "/repo/")
        _run(["rsync", "-a", "--delete", f"{src}/", dst], timeout=900)

    def put_file(self, local: Path, job_id: str, name: str) -> None:
        _run(["rsync", "-a", str(local), self._rs_path(f"{self.job_dir(job_id)}/{name}")], timeout=120)

    def pull_tree(self, job_id: str, dst: Path, max_size: str = "20m") -> None:
        src = self._rs_path(self.job_dir(job_id) + "/repo/")
        _run(["rsync", "-a", "--delete", "--safe-links", "--no-specials", "--no-devices",
              f"--max-size={max_size}",
              "--exclude=/.git", "--filter=:- .gitignore",
              "--exclude=node_modules/", "--exclude=.venv/", "--exclude=__pycache__/",
              src, f"{dst}/"], timeout=900)

    def fetch_file(self, job_id: str, name: str, dst: Path) -> bool:
        r = subprocess.run(["rsync", "-a", "--safe-links", self._rs_path(f"{self.job_dir(job_id)}/{name}"), str(dst)],
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0

    def run_agent(self, job_id: str, argv: list[str], timeout_s: int, log_fh) -> int:
        """Run argv in the job's repo dir, prompt on stdin, under timeout -k 30. Returns the exit code
        (124/137 = timed out). A local backstop kills the remote process group if ssh itself hangs."""
        jd = shlex.quote(self.job_dir(job_id))
        cmd = " ".join(shlex.quote(a) for a in argv) if argv else "true"
        script = (f"cd {jd}/repo && {{ timeout -k 30 {int(timeout_s)} {cmd} < ../PROMPT.md & "
                  f"echo $! > ../agent.pgid; wait $!; }}")
        try:
            r = self.sh(script, stdout=log_fh, timeout=timeout_s + 120, check=False)
            return r.returncode
        except subprocess.TimeoutExpired:
            self.kill(job_id)
            return 124

    def kill(self, job_id: str) -> None:
        jd = shlex.quote(self.job_dir(job_id))
        try:
            self.sh(f"p=$(cat {jd}/agent.pgid 2>/dev/null) && [ -n \"$p\" ] && kill -KILL -- -$p 2>/dev/null; true",
                    timeout=60, check=False, capture=True)
        except subprocess.TimeoutExpired:
            pass

    def cleanup(self, job_id: str) -> None:
        self.kill(job_id)
        self.sh(f"rm -rf -- {shlex.quote(self.job_dir(job_id))}", timeout=300, check=False, capture=True)


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None, timeout: float = 600,
         check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed ({r.returncode}): {' | '.join(err)}")
    return r


# ----------------------------------------------------------------------------- git

def git_env(auth_header: str = "") -> dict:
    """Git env with credentials only in GIT_CONFIG_* (process env, not argv, not .git/config)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", SSH_ASKPASS="true")
    pairs = [("core.hooksPath", "/dev/null"), ("commit.gpgsign", "false"), ("credential.helper", "")]
    if auth_header:
        pairs.append(("http.extraHeader", auth_header))
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    return env


def auth_header_for(repo: RepoSpec, cfg: dict) -> str:
    if repo.kind == "forgejo":
        tok = get_secret(cfg["forgejo"].get("token_var"), cfg)
        return f"Authorization: token {tok}" if tok else ""
    if repo.kind == "github":
        tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        if not tok:
            try:
                tok = _run(list(cfg["github"]["token_command"]), timeout=30).stdout.strip()
            except Exception:
                tok = ""
        if tok:
            basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
            return f"Authorization: basic {basic}"
    return ""


def remote_branches(repo_dir: Path, env: dict, prefix: str) -> set[str]:
    r = _run(["git", "ls-remote", "--heads", "origin", f"{prefix}*"], cwd=repo_dir, env=env, timeout=120, check=False)
    return {ln.split("refs/heads/", 1)[1] for ln in r.stdout.splitlines() if "refs/heads/" in ln}


# ----------------------------------------------------------------------------- forges

def forgejo_api(cfg: dict, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    base = forgejo_url(cfg)
    tok = get_secret(cfg["forgejo"].get("token_var"), cfg)
    req = urllib.request.Request(f"{base}/api/v1{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("Authorization", f"token {tok}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def forgejo_url(cfg: dict) -> str:
    fj = cfg.get("forgejo") or {}
    return (fj.get("url") or get_secret(fj.get("url_var"), cfg) or "").rstrip("/")


def open_pr(repo: RepoSpec, cfg: dict, *, head: str, base: str, title: str, body: str, body_file: Path) -> str:
    if repo.kind == "forgejo":
        code, data = forgejo_api(cfg, "POST", f"/repos/{repo.owner}/{repo.name}/pulls",
                                 {"title": title, "body": body, "head": head, "base": base})
        if code not in (200, 201):
            raise RuntimeError(f"Forgejo PR create failed: HTTP {code} {str(data.get('message', ''))[:200]}")
        return data.get("html_url") or data.get("url") or ""
    if repo.kind == "github":
        r = _run(["gh", "pr", "create", "--repo", repo.full, "--base", base, "--head", head,
                  "--title", title, "--body-file", str(body_file)], timeout=120)
        return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    raise RuntimeError("no PR for local repos")


def default_branch(repo: RepoSpec, repo_dir: Path, env: dict) -> str:
    r = _run(["git", "ls-remote", "--symref", "origin", "HEAD"], cwd=repo_dir, env=env, timeout=120, check=False)
    m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", r.stdout)
    return m.group(1) if m else "main"


# ----------------------------------------------------------------------------- job state

class Job:
    def __init__(self, cfg: dict, job_id: str):
        self.cfg, self.id = cfg, check_job_id(job_id)
        self.dir = state_root(cfg) / job_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "job.json"
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {"id": job_id}

    def update(self, **kw) -> None:
        self.data.update(kw)
        self.data["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)
        if "status" in kw:
            self.log(f"status -> {kw['status']}")

    def log(self, msg: str) -> None:
        with open(self.dir / "pipeline.log", "a") as fh:
            fh.write(f"{dt.datetime.now():%H:%M:%S} {msg}\n")


# ----------------------------------------------------------------------------- pipeline

def dummy_argv(sleep_s: int = 0) -> list[str]:
    """A fake agent: reads the prompt, edits one file, writes the summary."""
    script = (
        (f"sleep {int(sleep_s)}; " if sleep_s else "")
        + "prompt=$(cat); echo \"dummy agent: got $(printf %s \"$prompt\" | wc -c) prompt bytes\"; "
        "printf '%s\\n' '# Dummy change' '' 'Written by the code_task dummy agent.' > HERMES_DUMMY.md; "
        "printf '%s\\n' '## What changed' 'Added HERMES_DUMMY.md.' '## Why' 'Pipeline test.' "
        "'## Tests' 'None (dummy).' '## Follow-ups' 'None.' > HERMES_SUMMARY.md; echo done"
    )
    return ["bash", "-c", script]


def run_job(args, cfg: dict) -> int:
    t0 = time.time()
    job_id = args.job_id or new_job_id()
    job = Job(cfg, job_id)
    agent = args.agent
    acfg = cfg["agents"].get(agent)
    if not acfg:
        print(result_line("failed", job_id, note=f"unknown agent {agent}"))
        return 2
    label = acfg.get("label", agent)
    job.update(status="starting", repo=args.repo, task=args.task, agent=agent, base=args.base or "",
               timeout=args.timeout, created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               pid=os.getpid())
    print(f"code_task job {job_id}: {label} on {args.repo}", flush=True)
    sandbox = Sandbox(cfg["sandbox"], cfg.get("sandbox_jobs_dir", "jobs"))
    workdir = work_root(cfg) / job_id
    repo_dir = workdir / "repo"
    branch = ""
    pushed_sandbox = False
    try:
        fj = forgejo_url(cfg)

        def fj_exists(o: str, n: str) -> bool:
            return forgejo_api(cfg, "GET", f"/repos/{o}/{n}")[0] == 200

        repo = parse_repo(args.repo, fj, fj_exists)
        job.update(repo_kind=repo.kind, repo_full=repo.full)
        env = git_env(auth_header_for(repo, cfg))

        # 0. agent preflight on the sandbox (fail fast before cloning)
        if acfg.get("preflight"):
            pre = sandbox.sh(" ".join(shlex.quote(a) for a in acfg["preflight"]), timeout=60, check=False, capture=True)
            if pre.returncode != 0:
                raise RuntimeError(acfg.get("preflight_hint") or f"{label} preflight failed")

        # 1. clone + branch
        job.update(status="cloning")
        workdir.mkdir(parents=True, exist_ok=True)
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        clone = ["git", "clone", "--quiet", "--no-tags"]
        if repo.kind != "local" and int(cfg.get("clone_depth") or 0) > 0:
            clone += ["--depth", str(int(cfg["clone_depth"]))]
        tmp_env = env
        if not args.base and not cfg.get("default_base"):
            _run(["git", "init", "-q", str(workdir / ".probe")], timeout=60)
            _run(["git", "remote", "add", "origin", repo.clone_url], cwd=workdir / ".probe", timeout=60)
            base = default_branch(repo, workdir / ".probe", tmp_env)
            shutil.rmtree(workdir / ".probe", ignore_errors=True)
        else:
            base = args.base or cfg["default_base"]
        _run(clone + ["--branch", base, "--single-branch", repo.clone_url, str(repo_dir)], env=env, timeout=1200)
        if "extraheader" in (repo_dir / ".git" / "config").read_text().lower():
            raise RuntimeError("credential leaked into .git/config; aborting")
        taken = remote_branches(repo_dir, env, cfg["branch_prefix"]) if repo.kind != "local" else set()
        local_heads = _run(["git", "branch", "--list", "--format=%(refname:short)"], cwd=repo_dir, env=env).stdout.split()
        branch = branch_name(args.task, dt.datetime.now(), cfg["branch_prefix"], taken | set(local_heads))
        if branch == base or not branch.startswith(cfg["branch_prefix"]):
            raise RuntimeError(f"refusing branch name {branch!r}")
        _run(["git", "checkout", "-q", "-b", branch], cwd=repo_dir, env=env)
        base_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir, env=env).stdout.strip()
        tracked_summary = (repo_dir / SUMMARY_FILE).exists()
        job.update(base=base, branch=branch, base_sha=base_sha)

        # 2. to sandbox
        job.update(status="syncing")
        prompt = STANDING_INSTRUCTIONS.format(repo=repo.full, branch=branch, base=base, task=args.task.strip())
        (job.dir / "PROMPT.md").write_text(prompt)
        sandbox.prepare(job_id)
        pushed_sandbox = True
        sandbox.push_tree(repo_dir, job_id)
        sandbox.put_file(job.dir / "PROMPT.md", job_id, "PROMPT.md")

        # 3. run the agent
        if agent == "dummy":
            argv = dummy_argv(args.dummy_sleep)
        else:
            argv = list(acfg["command"])
            if args.model and acfg.get("model_flag"):
                argv += [a.replace("{model}", args.model) for a in acfg["model_flag"]]
        job.update(status="running", agent_started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        log_path = job.dir / "agent.log"
        with open(log_path, "w") as fh:
            rc = sandbox.run_agent(job_id, argv, args.timeout, fh)
        log_text = clean_log(log_path.read_text(errors="replace"))
        job.update(agent_exit=rc)
        if rc in (124, 137):
            job.update(status="timeout", error=f"agent exceeded {args.timeout}s")
            print(f"agent timed out after {args.timeout}s; log: {log_path}")
            print(result_line("timeout", job_id, branch=branch, note=f"agent exceeded {args.timeout}s; no PR"))
            return 3

        # 4. back from sandbox
        job.update(status="collecting")
        sandbox.fetch_file(job_id, "LAST_MESSAGE.md", job.dir / "LAST_MESSAGE.md")
        sandbox.pull_tree(job_id, repo_dir, cfg.get("max_file_size", "20m"))
        summary = ""
        sp = repo_dir / SUMMARY_FILE
        if sp.is_file() and not sp.is_symlink():
            summary = sp.read_text(errors="replace")
            (job.dir / SUMMARY_FILE).write_text(summary)
            if not tracked_summary:
                sp.unlink()
        if not summary and (job.dir / "LAST_MESSAGE.md").exists():
            summary = (job.dir / "LAST_MESSAGE.md").read_text(errors="replace")
        _run(["git", "add", "-A"], cwd=repo_dir, env=env)
        if _run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir, env=env, check=False).returncode == 0:
            if rc != 0:
                last = (log_text.strip().splitlines() or ["(empty log)"])[-1][:150]
                note = f"agent exited {rc} without changes: {last}"
                job.update(status="failed", error=note)
                print(f"agent failed; log: {log_path}")
                print(result_line("failed", job_id, branch=branch, note=note))
                return 4
            job.update(status="no_changes")
            print(summary.strip()[:1500])
            print(result_line("no_changes", job_id, branch=branch, note="agent made no file changes"))
            return 0
        diffstat = _run(["git", "diff", "--cached", "--stat"], cwd=repo_dir, env=env).stdout
        (job.dir / "diff.patch").write_text(_run(["git", "diff", "--cached"], cwd=repo_dir, env=env).stdout)

        # 5. scan, commit, push, PR
        leaks = gitleaks_scan(repo_dir, env, cfg.get("gitleaks", "auto"), job)
        if leaks:
            job.update(status="blocked_secrets", error=leaks)
            print(result_line("blocked_secrets", job_id, branch=branch, note=leaks + "; nothing pushed"))
            return 5
        cenv = dict(env, GIT_AUTHOR_NAME=cfg["author_name"].format(agent=label),
                    GIT_AUTHOR_EMAIL=cfg["author_email"], GIT_COMMITTER_NAME=cfg["author_name"].format(agent=label),
                    GIT_COMMITTER_EMAIL=cfg["author_email"])
        _run(["git", "commit", "-q", "--no-verify", "-m", commit_message(args.task, label, job_id)], cwd=repo_dir, env=cenv)
        sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir, env=env).stdout.strip()
        job.update(commit=sha, diffstat=diffstat.strip().splitlines()[-1] if diffstat.strip() else "")
        if rc != 0:
            job.log(f"agent exited {rc}; committing what it left (marked in PR)")
        elapsed = time.time() - t0
        body = pr_body(task=args.task, summary=summary, agent_label=label, job_id=job_id, diffstat=diffstat,
                       log_excerpt=tail(log_text), elapsed_s=elapsed)
        if rc != 0:
            body = f"> **Warning:** the agent exited with code {rc}; changes may be incomplete.\n\n" + body
        (job.dir / "PR_BODY.md").write_text(body)

        if args.no_push or repo.kind == "local":
            why = "--no-push" if args.no_push else "local repo"
            job.update(status="committed", note=f"not pushed ({why})")
            print(f"committed {sha[:10]} on {branch} ({why}); {diffstat.strip().splitlines()[-1] if diffstat.strip() else ''}")
            print(result_line("committed", job_id, branch=branch, note=f"commit {sha[:10]}, not pushed ({why})"))
            return 0

        job.update(status="pushing")
        if branch == base:
            raise RuntimeError("refusing to push to the base branch")
        _run(["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo_dir, env=env, timeout=600)
        job.update(status="opening_pr")
        url = open_pr(repo, cfg, head=branch, base=base, title=pr_title(args.task), body=body,
                      body_file=job.dir / "PR_BODY.md")
        job.update(status="done", pr_url=url, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        print(summary.strip()[:1500])
        print(f"PR: {url}")
        print(result_line("done", job_id, pr=url, branch=branch, note=diffstat.strip().splitlines()[-1] if diffstat.strip() else ""))
        return 0
    except Exception as e:  # noqa: BLE001 - report every failure as a result line
        msg = str(e)
        for s in _known_secrets(cfg):
            msg = msg.replace(s, "***")
        job.update(status="failed", error=msg)
        print(f"code_task failed: {msg}", file=sys.stderr)
        print(result_line("failed", job_id, branch=branch, note=msg))
        return 1
    finally:
        if pushed_sandbox and not args.keep_sandbox:
            try:
                sandbox.cleanup(job_id)
                job.log("sandbox job dir removed")
            except Exception as e:  # noqa: BLE001
                job.log(f"sandbox cleanup failed: {e}")
        if job.data.get("status") in ("done", "no_changes") and not cfg.get("keep_workdir_on_success"):
            shutil.rmtree(workdir, ignore_errors=True)
        job.update(elapsed_s=round(time.time() - t0, 1))


def _known_secrets(cfg: dict) -> list[str]:
    out = []
    tv = (cfg.get("forgejo") or {}).get("token_var")
    if tv and os.environ.get(tv):
        out.append(os.environ[tv])
    return out


def gitleaks_scan(repo_dir: Path, env: dict, mode: str, job: Job) -> str:
    if mode == "off":
        return ""
    exe = shutil.which("gitleaks") or (str(Path.home() / ".local/bin/gitleaks")
                                       if (Path.home() / ".local/bin/gitleaks").exists() else "")
    if not exe:
        if mode == "required":
            return "gitleaks required but not installed"
        job.log("gitleaks not installed; skipping secret scan")
        return ""
    r = subprocess.run([exe, "git", "--pre-commit", "--staged", "--redact", "--no-banner", str(repo_dir)],
                       cwd=repo_dir, env=env, capture_output=True, text=True, timeout=300)
    (job.dir / "gitleaks.log").write_text(r.stdout + r.stderr)
    if r.returncode == 1:
        return "gitleaks found possible secrets in the change (see gitleaks.log)"
    if r.returncode not in (0, 1):
        job.log(f"gitleaks error rc={r.returncode}; treating as no findings")
    return ""


# ----------------------------------------------------------------------------- status / list

def cmd_status(args, cfg: dict) -> int:
    job = state_root(cfg) / check_job_id(args.job_id) / "job.json"
    if not job.exists():
        print(f"no such job: {args.job_id}")
        return 1
    data = json.loads(job.read_text())
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    if data.get("status") not in TERMINAL and data.get("pid") and not _pid_alive(data["pid"]):
        data["status"] = f"{data.get('status')} (process gone)"
    for k in ("id", "status", "repo", "agent", "base", "branch", "commit", "pr_url", "diffstat", "error",
              "created_at", "updated_at", "elapsed_s"):
        if data.get(k) not in (None, ""):
            print(f"{k:11} {data[k]}")
    print(f"{'task':11} {' '.join(str(data.get('task', '')).split())[:200]}")
    print(f"{'logs':11} {job.parent}")
    log = job.parent / "agent.log"
    if log.exists() and data.get("status") in ("running",) + tuple(TERMINAL - {"done"}):
        print("--- agent log (tail) ---")
        print(tail(clean_log(log.read_text(errors="replace")), 15, 1500))
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def cmd_list(args, cfg: dict) -> int:
    root = state_root(cfg)
    jobs = sorted((p for p in root.glob("*/job.json")), reverse=True)[: args.limit] if root.exists() else []
    if not jobs:
        print("no code tasks yet")
        return 0
    for p in jobs:
        d = json.loads(p.read_text())
        task = " ".join(str(d.get("task", "")).split())
        print(f"{d.get('id')}  {d.get('status', '?'):15} {d.get('agent', '?'):6} {d.get('repo', ''):30} "
              f"{d.get('pr_url') or d.get('branch') or ''}  {task[:50]}")
    return 0


# ----------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Hand a coding job to Codex/Claude Code in a sandbox; get a PR back.")
    ap.add_argument("--config", help="config JSON (default $HERMES_HOME/config/code_task.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a coding job")
    r.add_argument("--repo", required=True, help="owner/name, forgejo:owner/name, github:owner/name, URL or local path")
    r.add_argument("--task", required=True, help="instruction for the agent (or @file to read it from a file)")
    r.add_argument("--agent", default="codex", help="codex | claude | dummy")
    r.add_argument("--base", default="", help="base branch (default: repo default branch)")
    r.add_argument("--timeout", type=int, default=0, help="agent hard timeout in seconds (default 1800)")
    r.add_argument("--model", default="", help="model override passed to the agent")
    r.add_argument("--no-push", action="store_true", help="stop after the local commit (no push, no PR)")
    r.add_argument("--keep-sandbox", action="store_true", help="leave the job dir on the sandbox (debugging)")
    r.add_argument("--job-id", default="", help=argparse.SUPPRESS)
    r.add_argument("--dummy-sleep", type=int, default=0, help=argparse.SUPPRESS)
    s = sub.add_parser("status", help="show a job")
    s.add_argument("job_id")
    s.add_argument("--json", action="store_true")
    li = sub.add_parser("list", help="list recent jobs")
    li.add_argument("--limit", type=int, default=20)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "run":
        if args.task.startswith("@"):
            args.task = Path(args.task[1:]).expanduser().read_text()
        if not args.task.strip():
            raise SystemExit("--task is empty")
        args.timeout = args.timeout or int(cfg.get("default_timeout") or 1800)
        return run_job(args, cfg)
    if args.cmd == "status":
        return cmd_status(args, cfg)
    return cmd_list(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
