# shellcheck shell=bash
# Shared helpers for bin/install.sh, bin/update.sh and bin/bump-version.sh. Source, don't run.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
DRY_RUN="${DRY_RUN:-0}"

# Plugins/scripts this repo owns. Deploy never deletes anything else in $HERMES_HOME.
SKELETON_PLUGINS=(brain brain-ops topic-router quiet-background approval-expiry-notice)

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    _B=$'\033[1m' _G=$'\033[0;32m' _Y=$'\033[0;33m' _R=$'\033[0;31m' _D=$'\033[2m' _N=$'\033[0m'
else
    _B="" _G="" _Y="" _R="" _D="" _N=""
fi
say()  { printf '%s==>%s %s\n' "$_B" "$_N" "$*"; }
ok()   { printf '%s ok%s %s\n' "$_G" "$_N" "$*"; }
warn() { printf '%s  !%s %s\n' "$_Y" "$_N" "$*" >&2; }
die()  { printf '%sERR%s %s\n' "$_R" "$_N" "$*" >&2; exit 1; }

# run CMD...: execute, or only print it under --dry-run.
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '%s   [dry-run]%s %s\n' "$_D" "$_N" "$(printf '%q ' "$@")"
    else
        "$@"
    fi
}

load_version() {
    local file="$REPO_ROOT/HERMES_VERSION"
    [ -r "$file" ] || die "missing $file"
    # shellcheck disable=SC1090
    . "$file"
    [[ "${HERMES_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || die "HERMES_VERSION: HERMES_COMMIT must be a full 40-char SHA"
    HERMES_REPO="${HERMES_REPO:-https://github.com/NousResearch/hermes-agent.git}"
}

# The checkout's own launcher (what systemd runs), else whatever `hermes` is on PATH.
hermes_bin() {
    if [ -x "$AGENT_DIR/.hermes/bin/hermes" ]; then
        echo "$AGENT_DIR/.hermes/bin/hermes"
    elif command -v hermes >/dev/null 2>&1; then
        command -v hermes
    else
        echo ""
    fi
}

have_systemd_user() {
    command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

gateway_unit_exists() {
    have_systemd_user && systemctl --user cat hermes-gateway.service >/dev/null 2>&1
}

# copy_tree SRC DEST: mirror a directory (removing stale files inside DEST only), skipping caches.
copy_tree() {
    local src="$1" dest="$2"
    if command -v rsync >/dev/null 2>&1; then
        run mkdir -p "$dest"
        run rsync -a --delete --exclude '__pycache__/' --exclude '*.pyc' "$src/" "$dest/"
    else
        run rm -rf "$dest"
        run mkdir -p "$(dirname "$dest")"
        run cp -R "$src" "$dest"
        run find "$dest" -name '__pycache__' -prune -exec rm -rf {} +
    fi
}

# install_file SRC DEST [MODE]: copy only when content differs; keeps a .bak of a changed DEST.
install_file() {
    local src="$1" dest="$2" mode="${3:-644}"
    if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
        return 0
    fi
    if [ -f "$dest" ]; then
        run cp -p "$dest" "$dest.bak-skeleton"
    fi
    run mkdir -p "$(dirname "$dest")"
    run install -m "$mode" "$src" "$dest"
    echo "   updated $dest"
}

# deploy_skeleton: copy plugins, scripts, locales overlay and the vault server into $HERMES_HOME.
deploy_skeleton() {
    say "Deploying skeleton files into $HERMES_HOME"
    local p
    for p in "${SKELETON_PLUGINS[@]}"; do
        copy_tree "$REPO_ROOT/plugins/$p" "$HERMES_HOME/plugins/$p"
    done
    local f
    for f in "$REPO_ROOT"/scripts/*.py; do
        install_file "$f" "$HERMES_HOME/scripts/$(basename "$f")" 755
    done
    install_file "$REPO_ROOT/locales/en.yaml" "$HERMES_HOME/locales/en.yaml"
    # vault: code only. vault.db / vault.key are created on first use and never touched here.
    run mkdir -p "$HERMES_HOME/vault"
    run chmod 700 "$HERMES_HOME/vault"
    install_file "$REPO_ROOT/vault/vault_mcp.py" "$HERMES_HOME/vault/vault_mcp.py" 755
    install_file "$REPO_ROOT/vault/google_mcp.py" "$HERMES_HOME/vault/google_mcp.py" 755
    install_file "$REPO_ROOT/vault/requirements.txt" "$HERMES_HOME/vault/requirements.txt"
    ok "skeleton files deployed"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -c1-16; else shasum -a 256 "$1" | cut -c1-16; fi
}

# vault_venv: $HERMES_HOME/vault/.venv with the vault's requirements; skipped when already current.
vault_venv() {
    local venv="$HERMES_HOME/vault/.venv" req="$REPO_ROOT/vault/requirements.txt"
    local stamp="$venv/.skeleton-req-hash" hash
    hash="$(sha256_of "$req")"
    if [ -x "$venv/bin/python" ] && "$venv/bin/python" -c 'import mcp.server.fastmcp, cryptography, googleapiclient' >/dev/null 2>&1 \
        && { [ ! -f "$stamp" ] || [ "$(cat "$stamp")" = "$hash" ]; }; then
        # Working venv and requirements unchanged (or a venv made before this repo: adopt it).
        [ "$DRY_RUN" = 1 ] || echo "$hash" > "$stamp"
        ok "vault venv is current"
        return 0
    fi
    say "Building vault venv at $venv"
    if [ "$DRY_RUN" = 1 ]; then
        run python3 -m venv "$venv"
        run "$venv/bin/python" -m pip install --quiet -r "$req"
        return 0
    fi
    if python3 -m venv "$venv" >/dev/null 2>&1 && [ -x "$venv/bin/pip" ]; then
        "$venv/bin/python" -m pip install --quiet --upgrade pip
        "$venv/bin/python" -m pip install --quiet -r "$req"
    elif command -v uv >/dev/null 2>&1; then
        rm -rf "$venv"
        uv venv --quiet "$venv"
        uv pip install --quiet --python "$venv/bin/python" -r "$req"
    else
        die "need python3-venv (sudo apt install python3-venv) or uv to build the vault venv"
    fi
    "$venv/bin/python" -c 'import mcp.server.fastmcp, cryptography, googleapiclient' || die "vault venv is missing mcp/cryptography/google libraries"
    echo "$hash" > "$stamp"
    ok "vault venv ready"
}

# Rewrite %h/.hermes in a unit when HERMES_HOME is not ~/.hermes.
render_unit() {
    local src="$1"
    if [ "$HERMES_HOME" = "$HOME/.hermes" ]; then
        cat "$src"
    else
        sed "s#%h/.hermes#$HERMES_HOME#g" "$src"
    fi
}

# install_units [--with-kuma]: gateway unit (Hermes's own, else our template) + limits drop-in (+ Kuma push).
install_units() {
    local with_kuma="${1:-}"
    if ! have_systemd_user; then
        warn "no systemd user session (try: sudo loginctl enable-linger $USER, then log in again); skipping units"
        return 0
    fi
    say "Installing systemd user units"
    local hermes; hermes="$(hermes_bin)"
    if gateway_unit_exists; then
        ok "hermes-gateway.service already installed"
    elif [ -n "$hermes" ] && run "$hermes" gateway install --if-missing --no-start-now --start-on-login; then
        ok "hermes-gateway.service installed by Hermes"
    else
        warn "hermes gateway install unavailable; using systemd/hermes-gateway.service template"
        local tmp; tmp="$(mktemp)"
        render_unit "$REPO_ROOT/systemd/hermes-gateway.service" > "$tmp"
        install_file "$tmp" "$SYSTEMD_USER_DIR/hermes-gateway.service"
        rm -f "$tmp"
        run systemctl --user daemon-reload
        run systemctl --user enable hermes-gateway.service
    fi
    install_file "$REPO_ROOT/systemd/hermes-gateway.service.d/limits.conf" \
        "$SYSTEMD_USER_DIR/hermes-gateway.service.d/limits.conf"
    run systemctl --user daemon-reload
    if [ "$with_kuma" = "--with-kuma" ]; then
        install_file "$REPO_ROOT/systemd/hermes-kuma-push" "$HOME/.local/bin/hermes-kuma-push" 755
        install_file "$REPO_ROOT/systemd/hermes-kuma-push.service" "$SYSTEMD_USER_DIR/hermes-kuma-push.service"
        install_file "$REPO_ROOT/systemd/hermes-kuma-push.timer" "$SYSTEMD_USER_DIR/hermes-kuma-push.timer"
        run systemctl --user daemon-reload
        if [ -f "$HOME/.config/hermes-kuma-push.env" ]; then
            run systemctl --user enable --now hermes-kuma-push.timer
        else
            warn "Kuma push is installed but not enabled. Create ~/.config/hermes-kuma-push.env (chmod 600) from"
            warn "  systemd/hermes-kuma-push.env.example, then: systemctl --user enable --now hermes-kuma-push.timer"
        fi
    fi
    ok "units installed"
}

# pin_checkout SHA: fetch upstream and detach the checkout at exactly SHA (refuses a dirty tree).
pin_checkout() {
    local sha="$1"
    [ -d "$AGENT_DIR/.git" ] || die "no Hermes checkout at $AGENT_DIR"
    if [ -n "$(git -C "$AGENT_DIR" status --porcelain --untracked-files=no)" ]; then
        if [ "${FORCE:-0}" = 1 ]; then
            warn "checkout has local changes; stashing them (--force)"
            run git -C "$AGENT_DIR" stash push -m "hermes-skeleton $(date -u +%Y%m%dT%H%M%SZ)"
        else
            git -C "$AGENT_DIR" status --short --untracked-files=no | head -20 >&2
            die "the Hermes checkout has local changes; commit/stash them or rerun with --force (stashes them)"
        fi
    fi
    say "Fetching upstream ($HERMES_REPO)"
    run git -C "$AGENT_DIR" fetch --quiet origin
    if [ "$DRY_RUN" != 1 ]; then
        git -C "$AGENT_DIR" cat-file -e "$sha^{commit}" 2>/dev/null \
            || die "commit $sha not found after fetch (is it on upstream?)"
    fi
    if [ "$(git -C "$AGENT_DIR" rev-parse HEAD)" = "$sha" ]; then
        ok "checkout already at $sha"
        return 0
    fi
    if [ "$DRY_RUN" != 1 ]; then
        git -C "$AGENT_DIR" rev-parse HEAD > "$HERMES_HOME/.skeleton-previous-commit" 2>/dev/null || true
    fi
    run git -C "$AGENT_DIR" -c advice.detachedHead=false checkout --quiet --detach "$sha"
    ok "checkout detached at $sha"
}

# post_checkout: Hermes's own steps after a source revision change (what `hermes update` does,
# minus the pull of main): PM dependency/tool sync, then the home maintenance registry
# (non-interactive config migration with backup/restore, skills sync, state.db guard,
# CLI launcher publication, runtime provisioning).
post_checkout() {
    local hermes; hermes="$(hermes_bin)"
    [ -n "$hermes" ] || die "no hermes launcher found (expected $AGENT_DIR/.hermes/bin/hermes)"
    say "Syncing dependencies (hermes pm install)"
    run "$hermes" pm install
    say "Running Hermes post-update maintenance (config migrate, skills sync, runtimes)"
    run "$hermes" --run-module hermes_cli.post_update --scope all
    say "Checking config for new options"
    run "$hermes" config check || warn "hermes config check reported problems (see above)"
}
