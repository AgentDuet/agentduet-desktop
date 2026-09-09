"""Notice that a newer build has been released, and say so. That is the whole of it.

STAGE ONE OF THREE, on purpose: detect and tell. Downloading and verifying a DMG is stage two,
and a self-updater is stage three and may never be needed. Each stage is separately useful, and
the first one is the only one that cannot go wrong on the owner's machine — it changes nothing,
so the worst case is a line that is out of date.

WHAT IT MUST NEVER DO, and each of these is a constraint the implementation is shaped by:

- **Never block startup.** This app is supposed to work with no network at all — an owner
  self-hosting on a box with no route out is a supported install, not a broken one. So the
  check runs on the daemon's own worker, after the site is bound, and `state()` reads a cached
  file and never touches the network. An offline machine gets no line and no delay.
- **Never poll often.** The GitHub API allows 60 requests an hour unauthenticated, shared by
  every process on the machine's IP — an office behind one NAT is one bucket. Once every
  `CHECK_EVERY` is far inside that and still catches a release the same day.
- **Never act during a call.** Nothing here acts at all, so this holds trivially today. It is
  written down because stage two breaks it the moment it exists: a download competing with live
  call audio, or a restart prompt over a conversation. There is no in-call flag to consult yet;
  whoever builds stage two adds one.

TWO GITHUB TRAPS, both of which produce a confident wrong answer rather than an error.

1. **`/releases/latest` EXCLUDES PRERELEASES.** Every release of this project is a prerelease,
   so that endpoint answers 404 for this repo — checked 2026-09-09 — and a check built on it
   would report "no releases" forever. `/releases` returns them, newest first.
2. **A REUSED TAG DEFEATS A VERSION COMPARISON.** a13 was overwritten rather than superseded
   (installed `0f5996a`, released `29ade2d`), so an install can be behind the release that
   carries its own version number. Comparing tags alone says "up to date" about a binary that
   is not the one shipped. So when the versions match, the build STAMP decides: `__built__` is
   a UTC timestamp baked in by the spec, and a release published after it means this copy is
   older than what is on offer under the same name.
"""

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import __built__, __version__, paths

logger = logging.getLogger("dduet.update")

#: NOT `/releases/latest` — see trap 1 in the module docstring. Five is more than enough to find
#: the newest: they arrive newest-first, and the only reason to look past the first is a draft.
FEED = "https://api.github.com/repos/AgentDuet/agentduet-desktop/releases?per_page=5"

#: Where the answer lives between checks. `run/` is derived instance state, which is exactly
#: what this is — and a plain file rather than an endpoint so the macOS shell can read it
#: without the site token, and without a second poller of its own.
CACHE = paths.RUN / "update.json"

#: Four times a day, against a 60-per-hour budget shared across the machine's whole IP.
CHECK_EVERY = 6 * 3600

#: Not at startup. The first minutes of a launch are when a call may already be arriving and
#: when the owner is looking at the window; a release that appeared this morning can wait.
FIRST_CHECK_AFTER = 180

#: Short, because nothing waits for this and a hung socket must not hold the worker.
TIMEOUT = 10

#: `v0.1.0a13`, and the shapes a future tag might plausibly take. Anything else is not ordered
#: rather than guessed at: a tag we cannot parse must never be reported as newer.
TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(a|alpha|b|beta|rc)\.?(\d+))?$", re.I)

#: A prerelease sorts BEFORE the release of the same triple, which is why the final gets 3.
STAGES = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2}


def _order(tag: str) -> tuple | None:
    """A version as something comparable, or None when it cannot be read."""
    m = TAG.match((tag or "").strip())
    if not m:
        return None
    major, minor, patch, stage, n = m.groups()
    rank = STAGES.get((stage or "").lower(), 3)
    return (int(major), int(minor), int(patch), rank, int(n or 0))


def _built_at() -> datetime | None:
    """When THIS BINARY was built, from the stamp the spec bakes in — or None.

    None from source, and DELIBERATELY none even when a source tree has a stamp. The spec
    writes `_build.py` into `src/`, so any checkout where somebody has run a local build keeps
    a real timestamp lying around (gitignored, and older than whatever has been edited since).
    A source run is not an install: nothing about it can be replaced by downloading a DMG, and
    how fresh it is, is a git question. So the rebuilt-tag comparison — which is the only thing
    that reads this — applies to a frozen build alone. A genuinely HIGHER release version is
    still reported from source, because that is true and useful either way.
    """
    import sys
    if not getattr(sys, "frozen", False):
        return None
    try:
        return datetime.strptime(__built__, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _published(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("published_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def state() -> dict:
    """The last answer, read from disk. NO NETWORK — this is what a page may call."""
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save(row: dict) -> dict:
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(row, indent=2))
    except OSError as exc:
        logger.warning("could not cache the update check: %s", exc)
    return row


def _fetch() -> list[dict]:
    # A User-Agent is REQUIRED by the GitHub API — without one it answers 403, which reads as a
    # rate limit and is not one.
    req = urllib.request.Request(FEED, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"agentduet-desktop/{__version__}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as answer:
        rows = json.loads(answer.read().decode())
    return rows if isinstance(rows, list) else []


def check() -> dict:
    """Ask GitHub once, cache the answer, and return it. NEVER RAISES.

    Blocking, so call it on a thread. An offline machine keeps whatever the last successful
    check found rather than losing the notice — a laptop that leaves the office has not stopped
    being out of date.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = state()
    try:
        rows = _fetch()
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        # OFFLINE IS NORMAL HERE and must not read as a fault. Logged at info for that reason.
        logger.info("could not reach GitHub to check for a release: %s", exc)
        return _save({**previous, "checked": now, "reachable": False})

    mine = _order(__version__)
    best, best_order = None, None
    for row in rows:
        if row.get("draft"):
            continue                        # not published; nobody can install it
        order = _order(row.get("tag_name", ""))
        if order and (best_order is None or order > best_order):
            best, best_order = row, order

    answer = {"checked": now, "reachable": True, "current": __version__,
              "version": "", "url": "", "published": "", "newer": False, "note": ""}
    if best is None or mine is None:
        # Not an error, and not "up to date" either — we simply cannot order these. Saying
        # nothing is the only honest option; a version we cannot parse must never be announced.
        logger.info("no release could be ordered against %s", __version__)
        return _save(answer)

    tag = str(best.get("tag_name", ""))
    answer.update(version=tag.lstrip("v"), url=str(best.get("html_url", "")),
                  published=str(best.get("published_at", "")))
    built, out = _built_at(), _published(best)
    if best_order > mine:
        answer.update(newer=True, note=f"Version {answer['version']} is available.")
    elif best_order == mine and built and out and out > built:
        # THE REUSED-TAG CASE — trap 2. The name matches and the build does not, so a version
        # comparison alone would report this copy as current.
        answer.update(newer=True,
                      note=f"Version {answer['version']} was rebuilt after this copy.")
    if answer["newer"]:
        logger.info("a newer release is available: %s (%s)", answer["version"], answer["url"])
    return _save(answer)


def summary() -> str:
    """One line for `status` and the menu bar, or "" when there is nothing to say."""
    row = state()
    return row.get("note", "") if row.get("newer") else ""


async def worker() -> None:
    """Check on a schedule, off the event loop, forever.

    On a THREAD every time: the loop this runs on also carries call audio, and `urlopen` blocks
    for as long as a socket wants to. Started after the site binds, never on the import path.
    """
    import asyncio
    await asyncio.sleep(FIRST_CHECK_AFTER)
    while True:
        try:
            await asyncio.to_thread(check)
        except Exception as exc:            # a worker that dies takes the notice with it
            logger.error("the update check hit %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(CHECK_EVERY)
