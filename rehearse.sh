#!/usr/bin/env bash
#
# Park the instance so the WIZARD runs again, for rehearsing a fresh install. Reversible:
# nothing is deleted, and a same-volume rename moves gigabytes of models instantly.
#
# IT TOUCHES ONLY WHAT THE APP OWNS — `$AGENTDUET_HOME`, default ~/.agentduet-desktop — and
# WITHIN that it leaves `models/` alone: downloaded weights are cache, not configuration, and
# re-fetching gigabytes is not a reasonable price for testing a wizard. So it moves everything
# else out and leaves the models where they are, which also means a download in flight is never
# moved out from under the process writing it.
#
# It deliberately does NOT touch ~/.connector or ~/.agentduet. The app never writes those: they
# are only ever read, by connector._from_file, to PREFILL the wizard, and every credential the
# app stores goes to $AGENTDUET_HOME/.env instead. They exist so a rehearsal is a few clicks
# rather than a trip to a terminal to copy a secret — so moving them aside defeats the purpose
# of the reset it looks like part of. (The first version of this script moved them, and the
# rehearsal on 2026-09-08 reported the prefill as broken when the script had hidden its input.)
#
# To rehearse what a STRANGER meets on a new machine, move them yourself for that run:
#   mv ~/.connector ~/.connector.away; mv ~/.agentduet ~/.agentduet.away
set -euo pipefail

H="${AGENTDUET_HOME:-$HOME/.agentduet-desktop}"
BAK="$H.parked"

_running() { pgrep -f "AgentDuet Desktop|agentduet-desktop run" >/dev/null 2>&1; }

case "${1:-}" in
  park)
    _running && { echo "refusing: the app is running — quit it from the menu bar first"; exit 1; }
    [ -e "$BAK" ] && { echo "refusing: $BAK exists already — 'restore' it before parking again"; exit 1; }
    [ -d "$H" ] || { echo "nothing to park: $H is already absent, so the wizard will run"; exit 0; }
    # THE MODELS NEVER MOVE, and that is the point of doing it this way round. Everything else
    # is moved OUT of the instance and `models/` is simply left where it is, so a rehearsal
    # starts with the weights already cached and re-downloading gigabytes is not the price of
    # testing a wizard. It also means a fetch in flight is never touched: moving a directory a
    # running download is writing into leaves it writing to a path that no longer exists.
    #
    # A leftover `models/` does not make the instance look configured — `first_run` is
    # `setup-done` plus a configured connector, and neither lives there.
    mkdir -p "$BAK"
    for item in "$H"/* "$H"/.[!.]*; do
      [ -e "$item" ] || continue
      case "$(basename "$item")" in models) continue;; esac
      mv "$item" "$BAK"/
    done
    [ -e "$H/run" ] && { echo "ERROR: run/ is still in place"; exit 1; }
    echo "parked:   everything but models/ -> $BAK  ($(du -sh "$BAK" | cut -f1))"
    if [ -d "$H/models" ]; then
      echo "kept:     $H/models  ($(du -sh "$H/models" | cut -f1)) — no re-download"
    else
      echo "kept:     nothing cached; models/ did not exist"
    fi
    echo "prefill:  $(test -f "$HOME/.connector" && echo 'kept' || echo 'no ~/.connector')," \
         "$(test -f "$HOME/.agentduet" && echo 'kept' || echo 'no ~/.agentduet')"
    echo "the wizard will run on the next launch"
    ;;
  restore)
    _running && { echo "refusing: the app is running — quit it from the menu bar first"; exit 1; }
    [ -d "$BAK" ] || { echo "nothing to restore: $BAK is absent"; exit 1; }
    # Anything the rehearsal wrote is kept for inspection — except models/, which stays put and
    # is shared by both, so whatever the rehearsal downloaded is simply kept.
    KEEP="$H.rehearsal-$(date +%Y%m%dT%H%M%S)"
    mkdir -p "$KEEP"
    for item in "$H"/* "$H"/.[!.]*; do
      [ -e "$item" ] || continue
      case "$(basename "$item")" in models) continue;; esac
      mv "$item" "$KEEP"/
    done
    rmdir "$KEEP" 2>/dev/null && KEEP=""      # nothing was written; do not leave an empty dir
    # A PARK MADE BY AN OLDER VERSION holds models/ too, because it moved the whole directory.
    # Merge those per model rather than moving the folder, or `mv` nests it as models/models —
    # and never delete: a duplicate goes to the kept instance so both copies still exist.
    if [ -d "$BAK/models" ]; then
      mkdir -p "$H/models"
      for m in "$BAK"/models/*; do
        [ -e "$m" ] || continue
        name="$(basename "$m")"
        if [ -e "$H/models/$name" ]; then
          [ -z "$KEEP" ] && { KEEP="$H.rehearsal-$(date +%Y%m%dT%H%M%S)"; mkdir -p "$KEEP"; }
          mkdir -p "$KEEP/models"
          # COMPLETE BEATS PARTIAL, and "the live one wins" is the wrong rule on its own — the
          # live copy is often a download the rehearsal started and did not finish, while the
          # parked one is the whole file. Keeping the live one then leaves the model reporting
          # itself NOT downloaded with 5 GB of it on the disk twice. A directory holding only
          # a `.part` is the incomplete one.
          _whole() { ls "$1"/*.gguf >/dev/null 2>&1; }
          if _whole "$m" && ! _whole "$H/models/$name"; then
            mv "$H/models/$name" "$KEEP/models/$name.partial"
            mv "$m" "$H/models/$name"
            echo "restored the COMPLETE $name; the unfinished one is in $KEEP"
          else
            mv "$m" "$KEEP/models/$name"
            echo "kept both copies of $name — the live one stays, the parked one is in $KEEP"
          fi
        else
          mv "$m" "$H/models/$name"
        fi
      done
      rmdir "$BAK/models" 2>/dev/null || echo "note: $BAK/models is not empty, left in place"
    fi
    for item in "$BAK"/* "$BAK"/.[!.]*; do
      [ -e "$item" ] || continue
      mv "$item" "$H"/
    done
    rmdir "$BAK"
    [ -n "$KEEP" ] && echo "kept the rehearsal instance at $KEEP"
    echo "restored: $H  ($(du -sh "$H" | cut -f1), models included)"
    ;;
  *)
    echo "usage: ./rehearse.sh park | restore"
    echo "  park     move the instance aside so the wizard runs; keeps the prefill files"
    echo "  restore  put it back, keeping the rehearsal instance for inspection"
    exit 2;;
esac
