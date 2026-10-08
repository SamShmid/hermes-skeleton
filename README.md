# Hermes Skeleton

The reusable skeleton of a personal [Hermes Agent](https://github.com/NousResearch/hermes-agent) setup
(by SamShmid). It holds the **code** that turns a stock Hermes install into this setup: plugins, a
password-vault MCP server, maintenance scripts, systemd units, a config overlay and a pinned upstream
version with install/update scripts. It does **not** hold anything personal.

Hermes itself is never forked or patched. Everything here sits on Hermes's public extension points
(plugins, MCP, script-only cron jobs, the locales overlay, config), so a new upstream version can be
tested and adopted by changing one line: the pinned commit in `HERMES_VERSION`.

The `docs/` folder is a small GitHub Pages site. It is also the homepage and privacy policy of the
private, single-user Google OAuth app named "Hermes".

## Layout

```
HERMES_VERSION              pinned upstream commit + repo URL (the only thing an update changes)
bin/
  install.sh                fresh host/container -> Hermes at the pin + this skeleton (idempotent, --dry-run)
  update.sh                 stable update: move to exactly the pin, run Hermes's post-update steps, redeploy, restart, health check
  bump-version.sh           change the pin after testing a candidate
  test.sh                   run every test suite
  lib.sh                    shared helpers
plugins/
  brain/                    memory provider: SQLite+FTS5 facts + wiki recall, brain_search / brain_remember tools
  brain-ops/                /brain and /project commands, brain_extract / brain_approve LLM tasks, background worker
  topic-router/             per-channel "same topic or new session?" decider with a visible divider
  quiet-background/         subagent/background results become a short summary or [SILENT], never raw dumps
  approval-expiry-notice/   when Discord approval buttons expire, post how to answer by typing
vault/
  vault_mcp.py              password vault MCP server (Fernet-encrypted SQLite; the agent uses secrets without seeing them)
  google_mcp.py             Google connector MCP server: several accounts, Gmail read/filters, Drive, Calendar, Docs, Sheets
  calendly_mcp.py           Calendly connector MCP server: links, upcoming bookings, changes, availability, one-time links
  requirements.txt          mcp<2, cryptography, google-auth, google-api-python-client (the vault's own venv)
scripts/                    deployed to $HERMES_HOME/scripts (only *.py)
  brain_cli.py              bulk import / search / one pipeline pass
  brain_tidy.py             nightly: dedupe, merge/contradiction proposals, wiki page refresh, secret-text flags
  brain_report.py           daily "what the brain learned" report (silent when nothing changed)
  cleanup.py                daily housekeeping: stale browser/IMAP processes, old caches, logs, checkpoints,
                            manual ~/backups older than 30 days (only after a confirmed restic backup)
  backup_data.py            nightly restic backup: SQLite online-backup snapshots + secrets/config/wiki/skills,
                            forget --prune (7 daily / 4 weekly / 6 monthly), weekly check
  repo_watch.py             repo + security + uptime report: GitHub/Forgejo inventory and CI, Dependabot/code/secret
                            scanning, osv-scanner + gitleaks on shallow clones, Uptime Kuma 7-day uptime
  repo_watch_weekly.py      cron wrappers (cron can't pass script arguments): full weekly report /
  repo_watch_daily.py       daily alert that prints only when something is new or newly broken
  calendly_watch.py         every 15 min: one line per new Calendly booking or cancellation (silent otherwise)
  research_digest.py        ~5 new papers + ~5 news stories matching your interests, one "why it matters" line each
  code_task.py              hand a coding job to Codex / Claude Code in a disposable sandbox, get a PR back
  check_public.sh           privacy scan for this repo (not deployed)
bridge/
  hermes_bridge_mcp.py      stdio MCP server so Claude Code, the Claude desktop app and Codex can talk to Hermes
  test_client.py            tiny MCP client for trying the bridge from a terminal
  config.example.json       bridge settings (URL, owner name, Keychain item)
sandbox/setup_sandbox.sh    provision the coding-agent sandbox container (Node 22, uv, Docker, Codex, Claude Code, user agent)
skills/software-development/code-handoff/SKILL.md   tells the agent how to run code_task.py (copy to $HERMES_HOME/skills/...)
locales/en.yaml             string overlay (expired-approval footer that tells you what to type)
systemd/                    gateway unit template, memory-limit drop-in, Uptime Kuma push timer + script
config/backup.conf.example settings for backup_data.py (copy to $HERMES_HOME/backup.conf)
config/repo_watch.example.json settings for repo_watch.py (copy to $HERMES_HOME/config/repo_watch.json)
config/research_interests.example.json interests for research_digest.py (copy to $HERMES_HOME/config/research_interests.json)
config/code_task.example.json settings for code_task.py (copy to $HERMES_HOME/config/code_task.json)
config/config.template.yaml only the settings that differ from Hermes defaults, with <PLACEHOLDERS>
docs/                       GitHub Pages: index.html, privacy.html
tests/                      vault, Google and Calendly connectors, cleanup, backup, repo_watch, research_digest and code_task tests (plugin tests live next to each plugin)
```

## Install

On a fresh Ubuntu 24.04 host or container, as the user that will run Hermes (not root):

```bash
sudo apt install -y git curl python3 python3-venv rsync
git clone <this repo> ~/hermes-skeleton && cd ~/hermes-skeleton
bin/install.sh --dry-run        # see every step first
bin/install.sh                  # add --with-kuma for the Uptime Kuma push timer
sudo loginctl enable-linger "$USER"   # user services keep running after logout
```

`install.sh`:

1. Installs Hermes with the upstream installer pinned to `HERMES_VERSION`
   (`install.sh --commit <sha> --non-interactive`), or, if Hermes is already installed, pins the existing
   checkout the same way `update.sh` does.
2. Copies the plugins, scripts, locales overlay and vault server into `$HERMES_HOME` (default `~/.hermes`).
3. Builds the vault's venv (`$HERMES_HOME/vault/.venv`).
4. Installs the gateway unit (Hermes's own `hermes gateway install`, falling back to `systemd/`) plus the
   memory-limit drop-in, and optionally the Kuma push timer.
5. Prints the manual next steps.

It never touches `config.yaml`, `.env`, auth files, databases or vault data. Then, by hand:

- `hermes model`, `hermes gateway setup`: provider login and messaging tokens (stored in `.env`).
- Merge `config/config.template.yaml` into `$HERMES_HOME/config.yaml`, replacing each `<PLACEHOLDER>`
  (channel IDs, models, owner name). The brain's LLM prompts use `plugins.entries.brain.settings.owner_name`
  and `owner_pronouns`. Unset, they say "the user".
- Optional script-only cron jobs (`hermes cron`): `cleanup.py` daily, `brain_tidy.py` nightly (run it with
  Hermes's runtime Python, since it imports Hermes's LLM client), `brain_report.py` hourly (it prints only
  during `--hour` in `--tz`).
- `systemctl --user restart hermes-gateway && hermes gateway status`.

## Backups (restic)

`scripts/backup_data.py` makes an encrypted, deduplicated nightly backup with
[restic](https://restic.net). It copies every SQLite database with SQLite's online backup API (never the
live `.db`/`-wal` files), checks each copy with `PRAGMA quick_check`, backs up the copies plus `.env`,
`auth.json`, `config.yaml`, `vault/vault.key`, memories, wiki, skills, scripts and plugins, then runs
`restic forget --prune` and, on Sundays, `restic check --read-data-subset 5%`. It is silent on a normal
night, prints one line on failure (exit 1, so the cron failure notice fires) and one summary line on Sundays.

Setup, on the Hermes host:

1. Install restic (distro package or the official binary).
2. A repository somewhere else, e.g. an SFTP account on another server. A dedicated sftp-only user
   (`ForceCommand internal-sftp`, `ChrootDirectory`) and a dedicated SSH key with `restrict` in
   `authorized_keys` keep that key from doing anything but file transfer. Put the host in `~/.ssh/config`.
3. Store a long random repository password in the vault, never in a file:
   `python3 -c "import secrets; print(secrets.token_urlsafe(36))" | $HERMES_HOME/vault/.venv/bin/python $HERMES_HOME/vault/vault_mcp.py set RESTIC_PASSWORD --service backup --description "restic repo password"`.
   **Also keep a copy in a password manager**: the vault lives on the same host, and without the password
   the backups cannot be read.
4. `cp config/backup.conf.example $HERMES_HOME/backup.conf`, fill in `BACKUP_REPO`, then
   `RESTIC_REPOSITORY=... RESTIC_PASSWORD_COMMAND="... vault_mcp.py get RESTIC_PASSWORD" restic init`.
5. Run `backup_data.py --dry-run`, then `backup_data.py --force-summary` once, and test a restore
   (`restic restore latest --target /tmp/r --include '*/brain.db'`, then `PRAGMA integrity_check`).
6. Cron: `hermes cron create "30 7 * * *" --name hermes-backup --script backup_data.py --no-agent
   --deliver <target> --failure-deliver <target>`.

## Repo watch

`scripts/repo_watch.py` is a script-only (no LLM) report on your code repos and services. Everything is
driven by `$HERMES_HOME/config/repo_watch.json` (see `config/repo_watch.example.json`):

- **Inventory:** GitHub repos of the configured owners (via `gh`) and Forgejo/Gitea repos (API token):
  last push, open PRs/issues, latest Actions run per workflow on the default branch.
- **Security:** GitHub Dependabot, code-scanning and secret-scanning alerts ("off" is reported with how to
  enable). Repos without built-in alerts (Forgejo, or GitHub with alerts off when `local_scan.github` is
  `"fallback"`) are shallow-cloned into `cache_dir` (unused clones pruned after `cache_prune_days`) and
  scanned with [osv-scanner](https://github.com/google/osv-scanner) (dependency vulns) and
  [gitleaks](https://github.com/gitleaks/gitleaks) (secrets in the last `commits` commits, redacted).
  Install both official release binaries to `~/.local/bin`. Gitleaks hits stay listed until you add the
  fingerprint to the repo's `.gitleaksignore` or a pattern to `ignore_findings`.
- **Uptime:** Uptime Kuma monitor status and 7-day uptime per group, via the `uptime-kuma-api` package
  (falls back to `/metrics`, status only).
- **State:** findings, failing CI and down monitors with first-seen times, so the daily mode reports only
  what is new. The first run records a baseline.

Secrets are read from the environment first, then from `secret_command` + the secret name (e.g. the vault
CLI). Setup:

```bash
uv venv $HERMES_HOME/venvs/repo-watch && uv pip install --python $HERMES_HOME/venvs/repo-watch/bin/python uptime-kuma-api
cp config/repo_watch.example.json $HERMES_HOME/config/repo_watch.json   # then edit
$HERMES_HOME/venvs/repo-watch/bin/python $HERMES_HOME/scripts/repo_watch.py --mode weekly --dry-run --verbose
hermes cron create "0 13 * * 1" --name repo-watch-weekly --script repo_watch_weekly.py --no-agent \
  --interpreter $HERMES_HOME/venvs/repo-watch/bin/python --deliver <target> --failure-deliver <target>
hermes cron create "15 12 * * *" --name repo-watch-daily --script repo_watch_daily.py --no-agent \
  --interpreter $HERMES_HOME/venvs/repo-watch/bin/python --deliver <target> --failure-deliver <target>
```

## Coding-agent sandbox (code_task.py)

`scripts/code_task.py` lets the agent hand a coding job to [Codex CLI](https://github.com/openai/codex)
(or Claude Code) and get a pull request back, without the coding agent ever holding git credentials:

1. Hermes clones the repo locally (Forgejo/Gitea token or `gh auth token`, passed to git only through
   `GIT_CONFIG_*` environment variables, so never in `.git/config` or the process list) and creates
   `hermes/<slug>-<yyyymmdd>`.
2. It rsyncs the checkout to a **sandbox** over LAN SSH and runs the agent there non-interactively
   (`codex exec ... -` with the prompt on stdin) under a hard `timeout`, streaming the log back.
3. It rsyncs the working tree back (never `.git`, no symlinks leaving the tree, `.gitignore` honoured,
   files over 20 MB skipped), takes `HERMES_SUMMARY.md` out of the tree as the PR description, scans the
   staged diff with gitleaks (a hit blocks the push), commits with hooks disabled, pushes the new branch
   (never the base branch, never `--force`) and opens a PR via the Forgejo API or `gh pr create`. It never merges.
4. It deletes the job dir on the sandbox and keeps `job.json`, `agent.log`, the summary, PR body and
   `diff.patch` in `$HERMES_HOME/code_tasks/<job-id>/`. The last stdout line is
   `CODE_TASK_RESULT status=... job=... pr=... branch=... note=...`.

```bash
code_task.py run --repo owner/name --task @task.md [--agent codex|claude|dummy] [--base main] [--timeout 1800] [--no-push]
code_task.py status <job-id>
code_task.py list
```

`--agent dummy` is a built-in fake agent (writes a file and a summary) for testing the whole pipeline
without a model login; `tests/test_code_task.py` runs it against a local bare repo with a local-directory
sandbox (`"sandbox": "local:/dir"`).

**Sandbox setup** (example: an unprivileged Proxmox LXC; any small Ubuntu 24.04 VM works):

```bash
# on the Proxmox host
pct create <CTID> local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst --hostname sandbox \
  --unprivileged 1 --features nesting=1,keyctl=1 --cores 4 --memory 6144 --swap 2048 \
  --rootfs local-lvm:40 --net0 name=eth0,bridge=vmbr0,firewall=1,gw=<GW>,ip=<SANDBOX_IP>/24,type=veth \
  --onboot 1 --description "Disposable coding-agent sandbox; holds no secrets at rest"
pct start <CTID>
# on the Hermes host
ssh-keygen -t ed25519 -N "" -f ~/.ssh/sandbox_ed25519
# copy sandbox/setup_sandbox.sh into the container and run it as root:
pct push <CTID> sandbox/setup_sandbox.sh /root/setup_sandbox.sh
pct exec <CTID> -- bash /root/setup_sandbox.sh "<contents of ~/.ssh/sandbox_ed25519.pub>" <HERMES_IP>
# log the coding agent in once (device code flow; the token then lives on the sandbox only)
pct exec <CTID> -- su - agent -c "codex login --device-auth"
```

Then on the Hermes host: add a `Host sandbox` block to `~/.ssh/config` (HostName, `User agent`,
`IdentityFile ~/.ssh/sandbox_ed25519`, `IdentitiesOnly yes`, `BatchMode yes`), pin its host key
(`ssh-keyscan` and compare the fingerprint with `pct exec <CTID> -- ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`),
copy `config/code_task.example.json` to `$HERMES_HOME/config/code_task.json`, and copy
`skills/software-development/code-handoff/` to `$HERMES_HOME/skills/software-development/`. The agent runs
jobs with the terminal tool in the background with completion notification, as the skill describes.

Security model: the sandbox is the isolation boundary, so Codex runs with its own sandbox bypassed
(`--dangerously-bypass-approvals-and-sandbox`; Codex's bubblewrap/landlock sandbox does not work in an
unprivileged container and would block the network test suites need). It has internet egress but no
forge credentials, no VPN/tailnet membership and only one authorized SSH key (restricted to the Hermes
host's IP). The coding agent's own login token is the one secret on it. Recommended extra hardening: a
per-guest firewall that blocks the sandbox from the LAN except DNS, and allows inbound SSH only from the
Hermes host. Destroying and recreating the sandbox loses nothing but that login.

## Research digest

`scripts/research_digest.py` recommends a few new papers and news stories. No API keys: the arXiv API
(queries from the config, 3 s apart), Hugging Face Daily Papers (upvotes give a boost) and RSS/Atom feeds.
Everything personal is in `$HERMES_HOME/config/research_interests.json` (see
`config/research_interests.example.json`): topics with keyword weights (title hits count double),
`applies_to`, `exclude`, negative keywords, arXiv queries, feeds and per-category news quotas.

- **Ranking:** keyword score + recency + per-feed base score (and position bonus for editor-ranked front
  pages); near-duplicate stories and arXiv/HF copies are merged.
- **Optional LLM rerank:** on Hermes's runtime Python (`import hermes_bootstrap` works) the top 20 candidates
  per list go to the auxiliary client with task slot `research_rank`, which picks the final ones and writes
  the why-lines. Feed text is passed as untrusted data and only returned ids are used. Anywhere else, or on
  any model error, it falls back to keyword ranking.
- **No repeats:** shown items are remembered in `$HERMES_HOME/data/research_digest_seen.json` (120 days);
  `--dry-run` never writes it.

```bash
cp config/research_interests.example.json $HERMES_HOME/config/research_interests.json   # then edit
python3 $HERMES_HOME/scripts/research_digest.py --dry-run --no-llm --format plain
# optional LLM rerank: set auxiliary.research_rank in config.yaml (see config/config.template.yaml)
hermes cron create "0 13 * * *" --name research-digest --script research_digest.py --no-agent \
  --interpreter <Hermes runtime python> --deliver <target> --failure-deliver <target>
```

Output is Discord-friendly (`--format plain|json` for other uses): masked links with `<url>` so no
preview cards, one line per item.

## Google connector

`vault/google_mcp.py` is a second MCP server that runs from the vault's venv. It connects any number of
Google accounts, each under a short label you choose (for example `personal`, `work`). Each account's
OAuth token is stored **encrypted in the vault** (`GOOGLE_TOKEN_<LABEL>`, service `google`). Access tokens
refresh automatically and the refreshed token is written back.

Tools (every data tool takes `account`):

| Area | Tools |
|---|---|
| accounts | `google_accounts`, `google_connect_start`, `google_connect_finish`, `google_disconnect` |
| Gmail | `gmail_search`, `gmail_read`, `gmail_list_filters`, `gmail_create_filter` (trash / archive / label), `gmail_delete_filter` |
| Drive | `drive_search`, `drive_read` (Docs/Slides as text, Sheets as CSV, small text files), `drive_upload`, `drive_create_folder` |
| Calendar | `calendar_list`, `calendar_create_event` |
| Docs / Sheets | `docs_create`, `sheets_read`, `sheets_append` |

There is deliberately **no** tool that sends email, deletes email or deletes files. The scopes are
`openid`, `userinfo.email`, `gmail.modify`, `gmail.settings.basic`, `drive`, `calendar`, `documents` and
`spreadsheets`. `gmail.send` is never requested. Results are capped and email/file content is labelled
as untrusted data. `drive_upload` refuses the vault, `.env`, `auth.json`, `~/.ssh` and key files.

**One-time setup (Google Cloud console):**

1. Create a project, enable the Gmail, Drive, Calendar, Docs and Sheets APIs.
2. OAuth consent screen: External, add the scopes above, then publish it. For personal use (well under
   100 users) the unverified app works without verification; each account sees one "unverified app" warning.
   Leaving it in "Testing" also works, but refresh tokens then expire after 7 days.
3. Credentials: create an OAuth client of type **Desktop app**, download the JSON to
   `$HERMES_HOME/google/client_secret.json` and `chmod 600` it.
4. Add the `mcp_servers.google` entry from `config/config.template.yaml` and restart the gateway.

**Connecting an account (works from chat, no browser on the server):**

1. Ask the agent to connect, e.g. "connect my work Google account". It calls
   `google_connect_start("work")` and sends a sign-in link (PKCE, offline access; valid 30 minutes).
2. Open the link, pick the account, click Allow (on the warning: *Advanced -> Go to <app name>*) and
   tick every permission box.
3. The browser then lands on `http://localhost:1/?state=...&code=...`, which fails to load. That is
   expected. Copy that address and paste it back to the agent.
4. The agent calls `google_connect_finish("work", "<address>")`. That exchanges the code, reads the email
   address, stores the token and warns about any permission that was left unticked.

The same flow is available from a shell:
`google_mcp.py connect-start work`, `google_mcp.py connect-finish work '<address>'` (or `-` to read it from
stdin), `google_mcp.py accounts`, and `google_mcp.py test work` (one harmless read per API).
`google_disconnect` revokes the token at Google and deletes it from the vault.

## Calendly connector

`vault/calendly_mcp.py` is a third MCP server in the vault venv (it uses `requests`). It talks to the
Calendly API v2 for your own account with a **personal access token stored in the vault**
(`CALENDLY_TOKEN`, read in code and never printed or returned).

| tool | what it does |
|---|---|
| `calendly_links` | main booking page + each event type (name, duration, active, scheduling URL) |
| `calendly_upcoming(days=14)` | booked meetings: invitee names/emails, local start/end, join link, cancel/reschedule URLs |
| `calendly_recent_changes(hours=24)` | meetings newly booked or canceled in that window |
| `calendly_availability` | availability schedules: weekly hours and upcoming date overrides |
| `calendly_single_use_link(event_type="30min")` | a one-time booking link (expires after one booking) |
| `calendly_cancel(event_uuid, reason)` | cancel a meeting; the description says only when the owner explicitly asks |

Everything else is read-only. Calendly's edge returns 403 to Python's default User-Agent, so every request
sends `User-Agent: hermes-calendly/1.0` and `Accept: application/json`.

**Setup:** create a personal access token (Calendly > Integrations > API & Webhooks), save it with
`vault_mcp.py set CALENDLY_TOKEN --service calendly < token-file`, add the `mcp_servers.calendly` entry from
`config/config.template.yaml`, restart the gateway. CLI: `calendly_mcp.py links | upcoming [days] |
changes [hours] | availability` (add `--json` for raw output).

**Notifications:** `scripts/calendly_watch.py` is a script-only cron job. Each run it compares the scheduled
events (yesterday to a year ahead) with `$HERMES_HOME/state/calendly_watch.json` and prints
`📅 New Calendly booking: <name> · <Thu Oct 8, 2:00 PM> · <type>` or `❌ Calendly booking canceled: ...`;
nothing otherwise. The first run only records a baseline. Two failed runs in a row stay silent; the third
exits non-zero so the failure notice fires. Run it with the vault venv's Python:

```bash
hermes cron create "*/15 * * * *" "Calendly bookings watch" --name calendly-watch --script calendly_watch.py \
  --no-agent --interpreter $HERMES_HOME/vault/.venv/bin/python --deliver <platform:chat_id>
```

If Calendly is connected to a Google calendar, bookings also show up there as ordinary calendar events,
so a calendar-based briefing lists them once without asking Calendly.

## Bridge: Claude Code, Claude desktop, Codex

`bridge/hermes_bridge_mcp.py` is a small stdio MCP server that runs on your own computer and lets other
agents talk to Hermes, for example "ask Hermes what is on my calendar tomorrow" from Claude Code.

| tool | what it does |
|---|---|
| `ask_hermes(message, conversation)` | sends a message and returns Hermes's reply (streams; waits up to `timeout`, default 10 min) |
| `hermes_continue(turn_id, approval)` | answers an approval request, or keeps waiting on a long turn |
| `new_hermes_conversation(conversation)` | starts that conversation fresh (long-term memory is unaffected) |
| `hermes_status()` | health, version, platforms, model, the client's conversations |

**How it talks to Hermes.** Hermes's built-in API server, `POST /v1/chat/completions` with `stream: true` and
the `X-Hermes-Session-Id` header. With that header Hermes loads the conversation from its own session
database, so the bridge sends only the new message, history survives gateway restarts, and context
compression is handled server-side (the bridge follows the rotated session id). Each conversation name maps
to one session id in `state.json`; give every client its own name (`HERMES_BRIDGE_CONVERSATION`) so Claude
Code and Codex do not share a thread. `X-Hermes-Session-Key: bridge:<name>` gives memory providers a stable
scope. (`/v1/responses` with `conversation` also chains, but its store keeps only the last 100 responses.)

**Approvals.** When Hermes wants to run a flagged command, the stream carries an `approval.request` event.
If the client supports MCP elicitation, the bridge asks you directly (once / session / deny). Otherwise
`ask_hermes` returns `APPROVAL NEEDED (turn_id=...)` and the calling agent must ask you and pass your answer to
`hermes_continue`; the tool descriptions forbid approving on your behalf. If the bridge exits first, Hermes
withdraws the request and the command does not run. Clarify questions are not available over the API.

**Setup.**

1. Hermes host: add the `API_SERVER_*` lines from `config/config.template.yaml` to `$HERMES_HOME/.env`, add
   `platform_toolsets.api_server`, restart the gateway. Publish it on the tailnet only:
   `sudo tailscale serve --bg --https=8642 http://127.0.0.1:8642`.
2. Your computer:

   ```bash
   mkdir -p ~/hermes-bridge && cp bridge/hermes_bridge_mcp.py ~/hermes-bridge/
   cp bridge/config.example.json ~/hermes-bridge/config.json      # then edit url/owner/about
   uv venv ~/hermes-bridge/.venv && uv pip install --python ~/hermes-bridge/.venv/bin/python -r bridge/requirements.txt
   # key into the macOS Keychain without echoing it (or set HERMES_API_KEY in the client's env)
   security add-generic-password -a hermes-bridge -s hermes-api-key -U -w "$(ssh <hermes-host> 'grep ^API_SERVER_KEY= ~/.hermes/.env | cut -d= -f2-')"
   ~/hermes-bridge/.venv/bin/python ~/hermes-bridge/hermes_bridge_mcp.py --check
   ```

3. Register it:

   - Claude Code:
     `claude mcp add --scope user hermes -e HERMES_BRIDGE_CONVERSATION=claude-code -e "HERMES_BRIDGE_CLIENT=Claude Code" -- ~/hermes-bridge/.venv/bin/python ~/hermes-bridge/hermes_bridge_mcp.py`
   - Claude desktop: in `~/Library/Application Support/Claude/claude_desktop_config.json` add
     `"mcpServers": {"hermes": {"command": "<home>/hermes-bridge/.venv/bin/python", "args": ["<home>/hermes-bridge/hermes_bridge_mcp.py"], "env": {"HERMES_BRIDGE_CONVERSATION": "claude-desktop"}}}`
     and restart the app.
   - Codex (`~/.codex/config.toml`); raise the tool timeout, Codex's default is 60 s:

     ```toml
     [mcp_servers.hermes]
     command = "<home>/hermes-bridge/.venv/bin/python"
     args = ["<home>/hermes-bridge/hermes_bridge_mcp.py"]
     tool_timeout_sec = 900

     [mcp_servers.hermes.env]
     HERMES_BRIDGE_CONVERSATION = "codex"
     ```

Try it: `~/hermes-bridge/.venv/bin/python bridge/test_client.py ask "what's on my calendar tomorrow? one line"`.
Tests: `BRIDGE_PYTHON=~/hermes-bridge/.venv/bin/python bin/test.sh`.

**Security.** The API server can do anything Hermes can, including shell commands, so the key is the
whole boundary: keep it in the Keychain (or the client's env), keep `API_SERVER_HOST=127.0.0.1`, and publish
it only with `tailscale serve` (never `funnel`). Rotate by changing `API_SERVER_KEY`, restarting the gateway
and updating the Keychain item.

## Stable updates

`hermes update` always tracks upstream `main`. This repo pins a commit instead, so the install only
moves when you decide it should:

```bash
# 1. try a candidate on the live install (or better, a test container first)
bin/update.sh --sha <new-upstream-sha>
#    ...use it for a while; check plugins, cron, Discord. To roll back:  bin/update.sh  (re-pins)

# 2. happy? make it the pin
bin/bump-version.sh <new-upstream-sha> --commit

# 3. every machine: land on exactly the pin
bin/update.sh
```

`update.sh` does what `hermes update` does, but against the pin:

1. A quick state snapshot (`hermes backup --quick`).
2. Stops the gateway before replacing the code it runs.
3. `git fetch`, then `git checkout --detach <pin>`. It refuses a dirty checkout; `--force` stashes the edits.
4. Hermes's own post-update steps:
   - `hermes pm install`: dependency and tool sync against the new lockfile.
   - `hermes --run-module hermes_cli.post_update --scope all`: non-interactive config migration with
     backup and restore, skills sync, the state.db guard, launcher publication and runtime provisioning.
   - `hermes config check`.
5. Redeploys the skeleton files and refreshes the vault venv if its requirements changed.
6. Restarts the gateway and checks that it stays up. If it doesn't, it shows the journal and the rollback
   command.

`install.sh` and `update.sh` both take `--dry-run`. Rolling back the code doesn't roll back data: a config migration that
already ran stays applied. Its backup is the `config.yaml.bak-*` file next to `config.yaml`, and
`hermes backup --quick` snapshots are in each profile's `state-snapshots/`.

**Upstream-fragile spots** to check when bumping. `plugins/topic-router/bridge.py` calls a handful of
private gateway methods, all isolated in that one file. `brain-ops` uses `agent.memory_provider.spawn_context_thread`.
`approval-expiry-notice` uses `tools.approval.has_blocking_approval`. Run `bin/test.sh` against the
candidate before bumping.

## Tests

```bash
bin/test.sh                       # uses ~/.hermes/hermes-agent/.hermes/bin/hermes for Hermes-importing suites
bin/test.sh --hermes /path/to/hermes
```

The brain and topic-router suites import Hermes, so they run on Hermes's runtime through
`hermes --run-module unittest` with a throwaway `HERMES_HOME`. The other suites need only Python
(the vault ones also need `cryptography`, plus `mcp` for the tool-registration test; the Google connector
tests are offline with mocked Google clients and need the vault venv's packages, so run them with
`$HERMES_HOME/vault/.venv/bin/python -m unittest tests/test_google.py`; the same goes for
`tests/test_calendly.py`, which uses a fake HTTP session).

## What is NOT here

- **Secrets:** `.env`, `auth.json`, OAuth tokens, `vault.db`, `vault.key`, the Kuma push URL.
- **Personal config:** the real `config.yaml`, channel IDs and prompts, model choices, cron job list, SOUL/personality.
- **Data:** `state.db`, sessions, the brain database, the wiki, memories, email/task/home-automation data and scripts.
- **Personal scripts:** email, briefing, home-assistant and task integrations.

All of that is the personal layer. It lives on the owner's server and in a separate private repository.
`scripts/check_public.sh` guards this repo. It scans for IDs, emails, IPs, home paths, tokens and
forbidden files, plus personal strings from a private pattern file kept outside the repo
(`~/.config/hermes-skeleton/private-patterns.txt`). Run it before every push.

## License

MIT. See `LICENSE`.
