#!/usr/bin/env bash
# Run every test suite in this repo.
#
#   bin/test.sh [--hermes PATH_TO_HERMES_LAUNCHER]
#
# Suites that import Hermes (brain, topic-router) run on Hermes's own runtime through
# `hermes --run-module unittest`, with HERMES_HOME pointed at a throwaway directory so no real
# config or data is read. Without a Hermes install those suites are reported as skipped.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HERMES="${HERMES_LAUNCHER:-$HOME/.hermes/hermes-agent/.hermes/bin/hermes}"
[ "${1:-}" = "--hermes" ] && HERMES="$2"
PY="${PYTHON:-python3}"
TMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TMP_HOME"' EXIT
fail=0

suite() {  # suite NAME RUNNER... -- runs unittest discovery for a tests dir
    local name="$1"; shift
    echo "=== $name"
    if "$@"; then echo "--- $name: PASS"; else echo "--- $name: FAIL"; fail=1; fi
}

cd "$REPO_ROOT"
# Plain-Python suites (stdlib; vault needs `cryptography`, skipped if absent).
suite approval-expiry-notice "$PY" -m unittest discover -s plugins/approval-expiry-notice/tests -v
suite quiet-background "$PY" -m unittest discover -s plugins/quiet-background/tests -v
suite repo-tests "$PY" -m unittest discover -s tests -v
# Bridge (MCP client side): needs `mcp` and `httpx`; set BRIDGE_PYTHON to the bridge venv's python.
BPY="${BRIDGE_PYTHON:-$PY}"
if "$BPY" -c "import mcp, httpx" 2>/dev/null; then
    suite bridge "$BPY" -m unittest discover -s bridge/tests -v
else
    echo "=== bridge: SKIPPED (no mcp/httpx in $BPY; set BRIDGE_PYTHON)"
fi

if [ -x "$HERMES" ]; then
    for p in brain topic-router; do
        suite "$p" env HERMES_HOME="$TMP_HOME" "$HERMES" --run-module unittest discover -s "plugins/$p/tests" -v
    done
else
    echo "=== brain, topic-router: SKIPPED (no Hermes launcher at $HERMES; pass --hermes PATH)"
fi
exit $fail
