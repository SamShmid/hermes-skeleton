#!/usr/bin/env bash
# Change the pinned Hermes commit after you have tested it (bin/update.sh --sha <sha>).
#
#   bin/bump-version.sh <sha> [--label "v0.22.0 ..."] [--commit]
#
# Verifies the commit exists upstream and is on main (using the local Hermes checkout, or a
# throwaway clone when there is none), rewrites HERMES_VERSION, and with --commit also makes a
# git commit in this repo. It does not touch the running install: run bin/update.sh afterwards.
set -euo pipefail
SHA="" LABEL="" DO_COMMIT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --label) LABEL="$2"; shift ;;
        --commit) DO_COMMIT=1 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) SHA="$1" ;;
    esac
    shift
done
# shellcheck source=bin/lib.sh
. "$(dirname "$0")/lib.sh"
load_version
[[ "$SHA" =~ ^[0-9a-f]{7,40}$ ]] || die "usage: bin/bump-version.sh <commit sha> [--label TEXT] [--commit]"

if [ -d "$AGENT_DIR/.git" ]; then
    src="$AGENT_DIR"
    git -C "$src" fetch --quiet origin
else
    src="$(mktemp -d)"
    trap 'rm -rf "$src"' EXIT
    say "No local checkout; making a blobless clone of $HERMES_REPO to verify"
    git clone --quiet --filter=blob:none --no-checkout "$HERMES_REPO" "$src"
fi
FULL="$(git -C "$src" rev-parse --verify --quiet "$SHA^{commit}")" || die "commit $SHA not found upstream"
git -C "$src" merge-base --is-ancestor "$FULL" origin/main || die "commit $FULL is not on upstream main"
if [ -z "$LABEL" ]; then
    LABEL="$(git -C "$src" describe --tags --always "$FULL" 2>/dev/null || echo "$FULL"), upstream main $(git -C "$src" log -1 --format=%cs "$FULL")"
fi
[ "$FULL" != "$HERMES_COMMIT" ] || { ok "already pinned to $FULL"; exit 0; }

tmp="$(mktemp)"
sed -e "s|^HERMES_COMMIT=.*|HERMES_COMMIT=$FULL|" -e "s|^HERMES_LABEL=.*|HERMES_LABEL=\"${LABEL//\"/}\"|" \
    "$REPO_ROOT/HERMES_VERSION" > "$tmp"
mv "$tmp" "$REPO_ROOT/HERMES_VERSION"
ok "HERMES_VERSION: ${HERMES_COMMIT:0:12} -> ${FULL:0:12} ($LABEL)"
[ "$src" = "$AGENT_DIR" ] && echo "   upstream changes: git -C $src log --oneline ${HERMES_COMMIT:0:12}..${FULL:0:12}"
if [ "$DO_COMMIT" = 1 ]; then
    git -C "$REPO_ROOT" add HERMES_VERSION
    git -C "$REPO_ROOT" commit --quiet -m "Pin Hermes to ${FULL:0:12}" -m "$LABEL"
    ok "committed"
fi
echo "Next: bin/update.sh   (moves the install to the new pin)"
