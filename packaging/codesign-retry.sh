#!/usr/bin/env bash
#
# codesign one path, retrying only when Apple's TIMESTAMP SERVICE is what failed.
#
# WHY THIS EXISTS: `--timestamp` is mandatory for notarization, and it is a network call to
# timestamp.apple.com made once per signature. This bundle holds ~140 inner binaries, so a build
# asks Apple for 140 timestamps in a burst and gets throttled. That is the whole of the
# 2026-09-18 macOS build failure: seven files came back "The timestamp service is not available"
# or "A timestamp was expected but was not found", xargs collected the non-zero exits, and the
# step failed — a red build caused by nothing in the tree, on a day the Windows job went green.
#
# ONLY A TIMESTAMP FAILURE IS RETRIED. A missing identity, a rejected entitlement or an
# unreadable file is reported at once: retrying those five times only delays the answer by a
# minute and buries the real message under four repeats.
#
# Used by BOTH signing paths — build.yml and sign-macos.sh — so they cannot drift apart on this.
set -uo pipefail

ATTEMPTS="${CODESIGN_ATTEMPTS:-5}"
err="$(mktemp)"
trap 'rm -f "$err"' EXIT

for n in $(seq 1 "$ATTEMPTS"); do
  if codesign "$@" 2>"$err"; then
    cat "$err" >&2          # "replacing existing signature" and friends belong in the log
    exit 0
  fi
  if ! grep -qi "timestamp" "$err"; then
    cat "$err" >&2
    exit 1
  fi
  if [ "$n" -ge "$ATTEMPTS" ]; then break; fi
  echo "  timestamp service declined (attempt $n of $ATTEMPTS) — retrying in $((n * 5))s" >&2
  sleep "$((n * 5))"
done

cat "$err" >&2
echo "::error::codesign could not reach Apple's timestamp service after $ATTEMPTS attempts" >&2
exit 1
