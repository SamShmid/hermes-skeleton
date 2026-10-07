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
  requirements.txt          mcp>=1.2,<2 and cryptography (installed into the vault's own venv)
scripts/                    deployed to $HERMES_HOME/scripts (only *.py)
  brain_cli.py              bulk import / search / one pipeline pass
  brain_tidy.py             nightly: dedupe, merge/contradiction proposals, wiki page refresh, secret-text flags
  brain_report.py           daily "what the brain learned" report (silent when nothing changed)
  cleanup.py                daily housekeeping: stale browser/IMAP processes, old caches, logs, checkpoints
  check_public.sh           privacy scan for this repo (not deployed)
locales/en.yaml             string overlay (expired-approval footer that tells you what to type)
systemd/                    gateway unit template, memory-limit drop-in, Uptime Kuma push timer + script
config/config.template.yaml only the settings that differ from Hermes defaults, with <PLACEHOLDERS>
docs/                       GitHub Pages: index.html, privacy.html
tests/                      vault and cleanup tests (plugin tests live next to each plugin)
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
(the vault ones also need `cryptography`, plus `mcp` for the tool-registration test).

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
