#!/usr/bin/env bash
# Stable update: move Hermes to exactly the commit in HERMES_VERSION (never "latest main"), run
# Hermes's own post-update steps, redeploy the skeleton files, restart the gateway, health-check.
#
#   bin/update.sh [--dry-run] [--sha SHA] [--no-restart] [--no-backup] [--force] [--hermes-home PATH]
#
#   --sha SHA     try a candidate commit instead of the pin (test it, then make it the pin with
#                 bin/bump-version.sh SHA). Roll back with: bin/update.sh  (re-pins HERMES_VERSION)
#   --no-restart  leave the gateway stopped/running as it was; you restart it yourself
#   --no-backup   skip the quick state snapshot (hermes backup --quick)
#   --force       stash local edits in the Hermes checkout instead of refusing
#
# Why not `hermes update`: it always tracks upstream main. This script does the same phases
# against a pinned commit: snapshot -> stop gateway -> fetch + detached checkout ->
# `hermes pm install` (deps/tools) -> `hermes_cli.post_update --scope all` (non-interactive
# config migration with backup/restore, skills sync, state.db guard, launchers, runtimes) ->
# redeploy plugins/scripts/locales/vault -> start gateway -> health check.
set -euo pipefail

SHA="" RESTART=1 BACKUP=1 FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --sha) SHA="$2"; shift ;;
        --no-restart) RESTART=0 ;;
        --no-backup) BACKUP=0 ;;
        --force) FORCE=1 ;;
        --hermes-home) HERMES_HOME="$2"; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
export DRY_RUN="${DRY_RUN:-0}" HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" FORCE
# shellcheck source=bin/lib.sh
. "$(dirname "$0")/lib.sh"
AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
load_version

TARGET="${SHA:-$HERMES_COMMIT}"
if [ -n "$SHA" ] && ! [[ "$SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
    die "--sha must be a hex commit id"
fi
[ -d "$AGENT_DIR/.git" ] || die "no Hermes checkout at $AGENT_DIR (run bin/install.sh first)"
HERMES="$(hermes_bin)"
[ -n "$HERMES" ] || die "no hermes launcher found"
[ "$DRY_RUN" = 1 ] && say "DRY RUN: nothing will be changed"
CURRENT="$(git -C "$AGENT_DIR" rev-parse HEAD)"
say "Hermes: ${CURRENT:0:12} -> ${TARGET:0:12}${SHA:+ (candidate, not the pin)}"

# Resolve a short candidate SHA once fetched.
if [ ${#TARGET} -lt 40 ]; then
    run git -C "$AGENT_DIR" fetch --quiet origin
    TARGET="$(git -C "$AGENT_DIR" rev-parse --verify --quiet "$TARGET^{commit}")" || die "unknown commit $SHA"
fi

# 1. snapshot ---------------------------------------------------------------------------------
if [ "$BACKUP" = 1 ] && [ "$CURRENT" != "$TARGET" ]; then
    say "Quick state snapshot (config, state.db, .env, auth, cron)"
    run "$HERMES" backup --quick --label "skeleton-pre-${TARGET:0:12}" \
        || warn "snapshot failed; continuing (rerun with --no-backup to silence)"
fi

# 2. stop the gateway before replacing the code it runs -------------------------------------------
was_active=0
if have_systemd_user && systemctl --user is-active --quiet hermes-gateway.service; then
    was_active=1
fi
if [ "$CURRENT" != "$TARGET" ] && [ "$was_active" = 1 ]; then
    say "Stopping hermes-gateway"
    run systemctl --user stop hermes-gateway.service
fi

# 3-4. code + Hermes's own post-update steps -------------------------------------------------------
if [ "$CURRENT" != "$TARGET" ]; then
    pin_checkout "$TARGET"
    post_checkout
else
    ok "Hermes already at ${TARGET:0:12}; skipping checkout and dependency sync"
fi

# 5. skeleton layer ---------------------------------------------------------------------------
deploy_skeleton
vault_venv
if have_systemd_user; then
    install_file "$REPO_ROOT/systemd/hermes-gateway.service.d/limits.conf" \
        "$SYSTEMD_USER_DIR/hermes-gateway.service.d/limits.conf"
    run systemctl --user daemon-reload
fi

# 6. restart + health ---------------------------------------------------------------------------
if [ "$RESTART" = 1 ] && have_systemd_user; then
    say "Restarting hermes-gateway"
    run systemctl --user restart hermes-gateway.service
    if [ "$DRY_RUN" != 1 ]; then
        healthy=0
        for _ in $(seq 1 30); do
            if systemctl --user is-active --quiet hermes-gateway.service; then healthy=1; break; fi
            sleep 2
        done
        sleep 5  # catch an immediate crash loop
        if [ "$healthy" = 1 ] && systemctl --user is-active --quiet hermes-gateway.service; then
            ok "hermes-gateway is active"
        else
            journalctl --user -u hermes-gateway.service -n 40 --no-pager >&2 || true
            die "hermes-gateway is not healthy. Roll back: bin/update.sh --sha $CURRENT"
        fi
    fi
elif [ "$was_active" = 1 ] && [ "$RESTART" = 0 ] && [ "$CURRENT" != "$TARGET" ]; then
    warn "gateway was stopped for the update and --no-restart was given: systemctl --user start hermes-gateway"
fi

say "Health check"
run "$HERMES" --version
run "$HERMES" plugins list || warn "could not list plugins"
if have_systemd_user; then
    run "$HERMES" gateway status || true
fi
if [ "$DRY_RUN" != 1 ]; then
    now="$(git -C "$AGENT_DIR" rev-parse HEAD)"
    [ "$now" = "$TARGET" ] || die "checkout is at $now, expected $TARGET"
    ok "Hermes is at $now"
    [ "$TARGET" = "$HERMES_COMMIT" ] || warn "running a candidate, not the pin. Keep it: bin/bump-version.sh $TARGET"
fi
echo "Previous commit (rollback target): $CURRENT"
