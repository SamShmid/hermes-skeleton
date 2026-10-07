#!/usr/bin/env bash
# Install the Hermes skeleton on a fresh (or existing) Ubuntu host/container, as the service user.
#
#   bin/install.sh [--dry-run] [--hermes-home PATH] [--with-kuma] [--no-units] [--no-vault] [--force]
#
# Steps (each one is idempotent, so re-running is safe):
#   1. Hermes itself: if $HERMES_HOME/hermes-agent is missing, run the upstream installer pinned to
#      HERMES_VERSION (--commit, --non-interactive). If present, pin it to HERMES_VERSION the same way
#      bin/update.sh does (fetch, detached checkout, `hermes pm install`, post-update maintenance).
#   2. Copy plugins, scripts, the locales overlay and the vault MCP server into $HERMES_HOME.
#   3. Build the vault's own venv ($HERMES_HOME/vault/.venv: mcp<2, cryptography).
#   4. systemd user units: gateway (Hermes's own unit, else systemd/ template) + memory-limit drop-in,
#      and with --with-kuma the Uptime Kuma push timer.
#   5. Print the manual next steps (config, secrets, linger, start).
# It never touches config.yaml, .env, auth, databases, the vault's data or anything personal.
set -euo pipefail

FORCE=0 WITH_KUMA="" NO_UNITS=0 NO_VAULT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --hermes-home) HERMES_HOME="$2"; shift ;;
        --with-kuma) WITH_KUMA="--with-kuma" ;;
        --no-units) NO_UNITS=1 ;;
        --no-vault) NO_VAULT=1 ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
export DRY_RUN="${DRY_RUN:-0}" HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" FORCE
# shellcheck source=bin/lib.sh
. "$(dirname "$0")/lib.sh"
AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
load_version

[ "$(id -u)" != 0 ] || warn "running as root: Hermes and its user units belong to the service user, not root"
for cmd in git curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || die "missing $cmd (sudo apt install -y git curl python3 python3-venv)"
done
[ "$DRY_RUN" = 1 ] && say "DRY RUN: nothing will be changed"
say "Hermes skeleton -> $HERMES_HOME (pin ${HERMES_COMMIT:0:12}, ${HERMES_LABEL:-})"

# 1. Hermes itself -----------------------------------------------------------------------------
if [ -d "$AGENT_DIR/.git" ]; then
    ok "Hermes checkout found at $AGENT_DIR"
    if [ "$(git -C "$AGENT_DIR" rev-parse HEAD)" != "$HERMES_COMMIT" ]; then
        pin_checkout "$HERMES_COMMIT"
        post_checkout
    else
        ok "already pinned to $HERMES_COMMIT"
    fi
else
    say "Installing Hermes with the upstream installer, pinned to $HERMES_COMMIT"
    installer="$(mktemp)"
    run curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o "$installer"
    run bash "$installer" --commit "$HERMES_COMMIT" --hermes-home "$HERMES_HOME" --non-interactive
    rm -f "$installer"
    if [ "$DRY_RUN" != 1 ]; then
        [ "$(git -C "$AGENT_DIR" rev-parse HEAD)" = "$HERMES_COMMIT" ] \
            || die "upstream installer finished but $AGENT_DIR is not at $HERMES_COMMIT"
    fi
    ok "Hermes installed"
fi

# 2-4. Skeleton layer ---------------------------------------------------------------------------
deploy_skeleton
[ "$NO_VAULT" = 1 ] || vault_venv
[ "$NO_UNITS" = 1 ] || install_units "$WITH_KUMA"

# 5. Next steps -------------------------------------------------------------------------------
cat <<EOF

${_B}Done.${_N} Next steps (manual, personal; nothing below is in this repo):
  1. Model + platforms:   hermes model ; hermes gateway setup     (tokens go to $HERMES_HOME/.env)
  2. Config overlay:      merge config/config.template.yaml into $HERMES_HOME/config.yaml,
                          replacing every <PLACEHOLDER> (or use: hermes config set <key> <value>)
  3. Check it:            hermes config check ; hermes plugins list ; hermes doctor
  4. Secrets vault:       $HERMES_HOME/vault/.venv/bin/python $HERMES_HOME/vault/vault_mcp.py set NAME --service S < value
  5. Services survive logout:  sudo loginctl enable-linger $USER
  6. Start:               systemctl --user restart hermes-gateway ; hermes gateway status
  7. Optional cron jobs (script-only): scripts/cleanup.py (daily), scripts/brain_tidy.py (nightly),
                          scripts/brain_report.py (hourly; prints only in its report hour),
                          scripts/backup_data.py (nightly restic backup; see README "Backups")
Later updates: bin/update.sh (always lands on the commit in HERMES_VERSION).
EOF
