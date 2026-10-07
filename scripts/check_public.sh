#!/usr/bin/env bash
# Fail if anything that must not be public is in the files this repo would commit.
#
#   scripts/check_public.sh            scan every tracked + untracked (not ignored) file
#
# Generic patterns live here. Personal strings (your hostnames, tailnet name, surname, server
# names...) must NOT be written into this public file: put one extended regex per line in
#   $PUBLIC_CHECK_EXTRA  (default: ~/.config/hermes-skeleton/private-patterns.txt, outside the repo)
# and they are checked too. Lines starting with # are ignored.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
EXTRA="${PUBLIC_CHECK_EXTRA:-$HOME/.config/hermes-skeleton/private-patterns.txt}"
SELF="scripts/check_public.sh"

# "name|extended regex|" (the regex may itself contain |)
CHECKS=(
  "discord/snowflake id|\\b[0-9]{17,20}\\b|"
  "email address|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)*\\.[A-Za-z]{2,}|"
  "tailscale cgnat ip|\\b100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.[0-9]{1,3}\\.[0-9]{1,3}\\b|"
  "tailnet name|\\btail[0-9a-f]{6}\\b|\\.ts\\.net\\b|"
  "lan ip (192.168)|\\b192\\.168\\.[0-9]{1,3}\\.[0-9]{1,3}\\b|"
  "lan ip (10/8)|\\b10\\.[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}\\b|"
  "lan ip (172.16/12)|\\b172\\.(1[6-9]|2[0-9]|3[01])\\.[0-9]{1,3}\\.[0-9]{1,3}\\b|"
  "home path|/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+|"
  "owner first name|\\bSam\\b|"
  "github token|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
  "openai/anthropic-style key|\\bsk-[A-Za-z0-9_-]{20,}|"
  "aws key|AKIA[0-9A-Z]{16}|"
  "slack token|xox[abpr]-[A-Za-z0-9-]{10,}|"
  "private key|-----BEGIN [A-Z ]*PRIVATE KEY|"
  "discord bot token|[MNO][A-Za-z0-9_-]{23,25}\\.[A-Za-z0-9_-]{6}\\.[A-Za-z0-9_-]{27,}|"
  "telegram bot token|\\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\\b|"
  "assigned secret|(password|passwd|secret|token|api[_-]?key)[\"']?[[:space:]]*[:=][[:space:]]*[\"']?[A-Za-z0-9/+_.-]{16,}|"
  "forgejo/gitea url|https?://[^[:space:]\"']*(forgejo|gitea)[^[:space:]\"']*|"
  "uptime kuma push token|/api/push/[A-Za-z0-9]{8,}|"
)

files=()
while IFS= read -r -d '' f; do
    [ "$f" = "$SELF" ] && continue
    [ -f "$f" ] && files+=("$f")
done < <(git ls-files -z -co --exclude-standard)
[ ${#files[@]} -gt 0 ] || { echo "no files to scan"; exit 2; }

fail=0
report() {  # report NAME REGEX
    local name="$1" rx="$2" hits
    hits="$(grep -I -n -E -- "$rx" "${files[@]}" 2>/dev/null || true)"
    if [ -n "$hits" ]; then
        echo "FAIL [$name]"
        echo "$hits" | head -20 | sed 's/^/    /'
        fail=1
    fi
}
for c in "${CHECKS[@]}"; do
    name="${c%%|*}"
    rx="${c#*|}"; rx="${rx%|}"
    report "$name" "$rx"
done

# Files that must never be committed, by name.
bad_names="$(printf '%s\n' "${files[@]}" | grep -E '(^|/)(\.env|.*\.db|.*\.sqlite3?|.*\.key|vault\.json\.enc|auth\.json|config\.yaml)$' || true)"
if [ -n "$bad_names" ]; then
    echo "FAIL [forbidden file]"; echo "$bad_names" | sed 's/^/    /'; fail=1
fi

if [ -r "$EXTRA" ]; then
    n=0
    while IFS= read -r rx || [ -n "$rx" ]; do
        case "$rx" in ''|'#'*) continue ;; esac
        n=$((n + 1))
        report "private pattern #$n" "$rx"
    done < "$EXTRA"
    echo "checked $n private pattern(s) from $EXTRA"
else
    echo "note: no private pattern file ($EXTRA); only generic patterns were checked"
fi

if [ "$fail" = 0 ]; then
    echo "PASS: ${#files[@]} files clean"
fi
exit $fail
