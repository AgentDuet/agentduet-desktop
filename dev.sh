#!/usr/bin/env bash
# Run from SOURCE — no PyInstaller build. Use this while iterating.
#
# The binary takes ~35s to rebuild; this takes ~7s, and HTML changes need NEITHER (the pages
# are read per request, so a browser refresh picks them up).
#
# Same $DDUET_HOME as the binary, so the owner's real instance is what you see. For a
# throwaway, prefix with DDUET_HOME=/tmp/whatever SECRETARY_WEB_PORT=8901.
set -e
cd "$(dirname "$0")"

VENV=.venv-build          # the venv holding the working agentduet SDK
[ -x "$VENV/bin/python" ] || { echo "no $VENV — see CLAUDE.md Build"; exit 1; }

# Stop whatever holds the port, binary or source. Never pkill -f: it matches this script.
"$VENV/bin/python" -m dduet_desktop.cli stop 2>/dev/null || true
sleep 1

LOG="${TMPDIR:-/tmp}/dduet-dev.log"
nohup "$VENV/bin/python" -m dduet_desktop.cli run --no-window "$@" > "$LOG" 2>&1 &
for _ in $(seq 20); do
  sleep 1
  URL=$(cat "${DDUET_HOME:-$HOME/.dduet}/run/site-url" 2>/dev/null || true)
  [ -n "$URL" ] && break
done

if [ -n "$URL" ]; then
  echo "  dev daemon up (from source)"
  echo "  $URL"
  echo "  log: $LOG"
else
  echo "  did NOT come up — last lines of $LOG:"; tail -15 "$LOG"; exit 1
fi
