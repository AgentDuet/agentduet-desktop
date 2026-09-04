#!/usr/bin/env bash
# Run from SOURCE — no PyInstaller build. Use this while iterating.
#
# The binary takes ~35s to rebuild; this takes ~7s, and HTML changes need NEITHER (the pages
# are read per request, so a browser refresh picks them up).
#
# Same $AGENTDUET_HOME as the binary, so the owner's real instance is what you see. For a
# throwaway, prefix with AGENTDUET_HOME=/tmp/whatever SECRETARY_WEB_PORT=8901.
set -e
cd "$(dirname "$0")"

VENV=.venv-build          # the venv holding the working agentduet SDK
[ -x "$VENV/bin/python" ] || { echo "no $VENV — see CLAUDE.md Build"; exit 1; }

# FROM SOURCE MEANS FROM src/, and this line is the only thing that makes that true.
#
# The venv holds a NON-EDITABLE install of this package — a real directory in site-packages,
# copied at install time — and `python -m agentduet_desktop.cli` finds that copy, not this tree.
# So every edit since the last `pip install` was invisible: the daemon started, reported the
# right version, and ran old code. Found 2026-09-04 after a fix to an error message did not
# appear in the message, having been "tested from source" for an hour.
#
# PYTHONPATH rather than an editable install, deliberately: this venv is also what builds the
# binary, and how PyInstaller resolves an editable package is not something to discover during
# a release.
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

# Stop whatever holds the port, binary or source. Never pkill -f: it matches this script.
"$VENV/bin/python" -m agentduet_desktop.cli stop 2>/dev/null || true
sleep 1

# Default is headless-with-browser, because that is what iterating wants. `./dev.sh --window`
# opens the native window instead — note that CLOSING that window ends the run, by design.
WINDOW=""
if [ "${1:-}" = "--window" ]; then WINDOW="1"; shift; fi

LOG="${TMPDIR:-/tmp}/dduet-dev.log"
if [ -n "$WINDOW" ]; then
  nohup "$VENV/bin/python" -m agentduet_desktop.cli run "$@" > "$LOG" 2>&1 &
else
  nohup "$VENV/bin/python" -m agentduet_desktop.cli run --no-window "$@" > "$LOG" 2>&1 &
fi
for _ in $(seq 20); do
  sleep 1
  URL=$(cat "${AGENTDUET_HOME:-$HOME/.agentduet-desktop}/run/site-url" 2>/dev/null || true)
  [ -n "$URL" ] && break
done

if [ -n "$URL" ]; then
  echo "  dev daemon up (from source)"
  echo "  $URL"
  echo "  log: $LOG"
else
  echo "  did NOT come up — last lines of $LOG:"; tail -15 "$LOG"; exit 1
fi
