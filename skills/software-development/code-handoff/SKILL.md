---
name: code-handoff
description: >
  Hand a coding job on one of the owner's repos (Forgejo or GitHub) to Codex CLI (or Claude Code) running in the
  disposable sandbox container, and get back a pull request. Use for "fix/implement/refactor X in repo Y",
  "open a PR that ...", "have Codex do ...". Runs code_task.py in the background; reports the PR link.
version: 1.0.0
author: hermes-skeleton
platforms: [linux]
metadata:
  hermes:
    tags: [coding, codex, claude-code, pull-request, forgejo, github, sandbox, delegation]
    related_skills: [coding-agents, github]
    created: "2026-10-07"
---

# Code hand-off (Codex in the sandbox → pull request)

`~/.hermes/scripts/code_task.py` clones the repo here (credentials stay on this host), copies the checkout to
the sandbox container (`ssh sandbox`, no credentials, no tailnet), runs the coding agent there with a hard
timeout, copies the result back, scans it for secrets, commits it on a new `hermes/<slug>-<date>` branch,
pushes that branch and opens a PR. It never pushes to the base branch and never merges.

## When to use

- The owner asks for a code change, bug fix, feature, test, or refactor in a repo and wants it done (not explained).
- The owner says "have Codex do it", "open a PR for ...", "hand this to Codex".
- Not for: questions about code (just read it), changes outside a git repo, or anything urgent on a live
  system (deploys, configs on this host). Never use it to modify Hermes's own `~/.hermes`.

## How to run (always in the background)

1. Write the task to a file with the file-writing tool (avoids shell quoting), e.g. `/tmp/code_task_<short>.md`.
   First line = a short imperative title (it becomes the PR title and branch name, e.g. "Add CSV export to
   reports"). Then make it self-contained: goal, the files/areas involved if known, acceptance criteria, and constraints
   the owner mentioned. The agent cannot ask questions.
2. Start it with the terminal tool, `background: true`, `notify: true`:

```bash
python3 ~/.hermes/scripts/code_task.py run --repo <repo> --task @/tmp/code_task_<short>.md [--agent codex] [--base <branch>] [--timeout 1800]
```

- `<repo>`: `owner/name` (Forgejo if it exists there, else GitHub), or explicit `forgejo:<owner>/<name>`,
  `github:<owner>/<name>`, or a full https URL.
- `--agent codex` (default) | `claude` (Claude Code; not logged in yet) | `dummy` (pipeline test, no AI).
- `--base`: default is the repo's default branch. `--timeout`: seconds for the agent (default 1800, max sensible 3600).
- `--model <name>` to override the agent's model. `--no-push` stops after a local commit (no PR).

3. Tell the owner in one line that it started (repo + job id is printed on the first line). Do not poll; the
   completion notification arrives by itself.
4. When it finishes, read the last line:
   `CODE_TASK_RESULT status=<status> job=<id> pr=<url> branch=<branch> note=<text>`
   and reply briefly:
   - `done` → the PR link + 1–2 sentences from the summary printed above the result line.
   - `no_changes` → say the agent made no changes and why (summary).
   - `timeout` → say it ran out of time; offer to retry with a larger `--timeout` or a narrower task.
   - `blocked_secrets` → nothing was pushed; gitleaks flagged the diff (see the job's `gitleaks.log`). Tell the owner.
   - `failed` → give the note. If it says Codex is not logged in, tell the owner to run on the
     Proxmox host: `pct exec <CTID> -- su - agent -c "codex login --device-auth"`

## Status and history

```bash
python3 ~/.hermes/scripts/code_task.py status <job-id>     # state, branch, PR, error, log tail
python3 ~/.hermes/scripts/code_task.py list                # recent jobs
```

Per-job files: `~/.hermes/code_tasks/<job-id>/` (`job.json`, `agent.log`, `HERMES_SUMMARY.md`, `PR_BODY.md`,
`diff.patch`, `pipeline.log`, `gitleaks.log`). Failed/timed-out checkouts stay in `~/.cache/code_tasks/<job-id>/`.

## Rules

- Only open PRs. Never merge, never push to `main`/the base branch, never force-push. Merging is the owner's call.
- One job per repo at a time unless the owner asks otherwise; the sandbox is small.
- Don't put secrets or tokens in the task text; the sandbox must stay credential-free.
- If the owner asks for follow-up changes on an open PR, run a new job with `--base <the PR branch>` and say so;
  that opens a stacked PR into the branch.
- Report the PR link exactly as printed; don't invent URLs.

## Setup facts

- Sandbox: a disposable container reachable as `ssh sandbox`, LAN only (`ssh sandbox` = user `agent`, key `~/.ssh/sandbox_ed25519`).
  Codex CLI + Claude Code CLI + Node 22 + uv + Docker installed. Disposable: destroying it loses nothing important.
- Config: `~/.hermes/config/code_task.json`. Forgejo token comes from the vault at run time; GitHub uses `gh`.
