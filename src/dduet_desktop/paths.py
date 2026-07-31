"""Where everything lives — the one place that knows install from instance.

DDuet Desktop is meant to be distributed: a user installs the code, attaches their own
model, and is interviewed into a configuration. That only works if there is a hard line
between what an upgrade REPLACES and what it must never touch:

  install   the code, plus assets that ship with it (web.html, sim.html, templates).
            Replaced wholesale on upgrade. The user never edits it.
  instance  owner.md, knowledge/, people/, permissions.json, capabilities.json, .env and
            run/ — everything the user or the agent authored. Lives under $DDUET_HOME
            (default ~/.dduet). Never touched by an upgrade; one directory to back up.

Before this module, instance data sat *inside* the install directory, so "upgrade the
code" and "keep my configuration" were the same directory. Fine for a POC on one laptop,
fatal for anything shipped.

`$DDUET_HOME` was already the convention for the search index (folder_index.home()), so
this consolidates rather than invents.

MIGRATION is by COPY, not move: the originals stay put as a backup until the owner deletes
them. A one-way move of a live instance — 250+ logged queries, live bookings, profiles —
is not something a refactor should do on import.
"""

import os
import pathlib
import shutil
import sys

#: The install directory. Code and shipped assets only.
INSTALL = pathlib.Path(__file__).parent


def home() -> pathlib.Path:
    """The instance directory. Overridable, which is also what makes tests cheap."""
    return pathlib.Path(os.getenv("DDUET_HOME", pathlib.Path.home() / ".dduet"))


HOME = home()

# ---- instance paths ----------------------------------------------------------
RUN = HOME / "run"                       # append-only logs + derived state
# SETTINGS the code parses by heading (Name, Pronoun, Voice, Never say). Outside knowledge/ on
# purpose: knowledge/ is quotable and LLM-editable, and a knowledge edit that renamed a heading
# silently emptied the never-say list — a safety rule switched off with no error anywhere. Also
# self-contradictory to store text the prompt calls "never quote this" in the retrieval corpus.
# FACTS about the owner (who, availability) stay in knowledge/owner.md, readable like anything else.
SETTINGS = HOME / "settings.md"
# Per-capability canvas pages, one per capability that has a custom surface. Outside knowledge/
# because an .html file is not something to answer questions FROM, and outside the install
# because the shape of the page follows the capability, not the framework — the framework's own
# page is a generic fallback (see canvas.page_for).
CANVAS = HOME / "canvas"
PEOPLE = HOME / "people"
KNOWLEDGE = HOME / "knowledge"
PERMISSIONS = HOME / "permissions.json"
CAPABILITIES = HOME / "capabilities.json"
ENV_FILE = HOME / ".env"
INDEX = HOME / "index"

#: (instance path, legacy path inside the install dir). Order matters only for readability.
# Seed files that ship WITH the code and are copied into the instance once. Kept in their own
# folder so the package's importable modules and the owner's starting config are never confused
# for each other — before the split they sat side by side, and a stale seed was migrated into a
# fresh instance because nothing distinguished "template" from "leftover instance data".
TEMPLATES = INSTALL / "templates"
#: Working capabilities to copy from or read. NEVER installed — a new owner should not inherit
#: someone else's business.
EXAMPLES = INSTALL / "examples"

_MIGRATE = [
    (SETTINGS, TEMPLATES / "settings.md"),
    (PEOPLE, TEMPLATES / "people"),
    (KNOWLEDGE, TEMPLATES / "knowledge"),
    (PERMISSIONS, TEMPLATES / "permissions.json"),
    (CAPABILITIES, TEMPLATES / "capabilities.json"),
]


def migrate() -> list[str]:
    """Copy any instance data still sitting in the install directory. Idempotent.

    Only ever copies when the destination is ABSENT — so it can never overwrite newer
    instance data with a stale copy left behind in the install directory, which is the one
    way this could destroy something.
    """
    moved = []
    HOME.mkdir(parents=True, exist_ok=True)
    for dest, legacy in _MIGRATE:
        if dest.exists() or not legacy.exists():
            continue
        if legacy.is_dir():
            shutil.copytree(legacy, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, dest)
        moved.append(f"{legacy.name} → {dest}")
    return moved


def legacy_leftovers() -> list[pathlib.Path]:
    """Instance data still present in the install dir after migration — safe to delete.

    Surfaced rather than deleted automatically: the owner should be the one to remove
    their own data, and until they do it is a free backup.
    """
    return [legacy for dest, legacy in _MIGRATE if dest.exists() and legacy.exists()]


# Run on import. Every module below imports this one, so instance data is in place before
# anything reads it — and a fresh install simply finds nothing to migrate.
_migrated = migrate()
if _migrated and os.getenv("DDUET_QUIET") != "1":
    print(f"dduet: moved instance data to {HOME}", file=sys.stderr)
    for line in _migrated:
        print(f"  {line}", file=sys.stderr)
