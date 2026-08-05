"""Where a customer's tools live, and who may switch one on.

THE ASSISTANT MAY WRITE A TOOL. IT MAY NOT INSTALL ONE.

This is the whole point of the module, and it is not ceremony. The owner drives this product
through an AI assistant, so "the owner installed it" and "the assistant installed it" would be
the same event — and that assistant reads escalations and call transcripts written by strangers.
If it could switch on a tool, then anything able to talk to it could add one, and the two-part
split we built the product around would have a back door.

So a proposal and an approval are different acts, in different places:

    propose_tool()            an mcp tool — the assistant writes JS into tools/pending/
    dduet-desktop tools ok    a command the OWNER types — moves it into tools/

The daemon loads from `tools/` and never looks in `pending/`. There is deliberately no
`approve_tool` in the owner registry: adding one would undo this in a single line, so its absence
is asserted by a test rather than left to memory.

Same reasoning as secrets, different risk. A credential must not enter a model's CONTEXT; a tool
must not be granted by a model's DECISION.
"""

import pathlib
import re

from . import paths

ACTIVE = paths.HOME / "tools"
PENDING = ACTIVE / "pending"

#: Tool names become filenames, and a name is written by a model on behalf of a stranger's
#: request. Anything that could climb out of the folder is refused rather than sanitised — a
#: silently rewritten name is one the owner cannot then find.
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def _path(name: str, pending: bool = False) -> pathlib.Path:
    return (PENDING if pending else ACTIVE) / f"{name}.js"


def propose(name: str, source: str) -> str:
    """Write a tool where the owner can look at it. It does NOT become active."""
    name = name.strip().lower().replace(" ", "_")
    if not _NAME.match(name):
        return ("A tool name must be lowercase letters, digits, - or _, and start with a letter "
                "or digit.")
    if not source.strip():
        return "The tool has no code."
    PENDING.mkdir(parents=True, exist_ok=True)
    _path(name, pending=True).write_text(source)
    return (f"Proposed {name!r}. It is NOT active — nothing will run it yet.\n"
            f"Written to {_path(name, pending=True)}\n\n"
            f"Read it, then switch it on yourself with:\n"
            f"    dduet-desktop tools approve {name}\n\n"
            f"That step is a command you type on purpose. I cannot do it for you, and neither "
            f"can anything that talks to me.")


def approve(name: str) -> str:
    """Make a proposed tool active. Called from the CLI, never from the registry."""
    src = _path(name, pending=True)
    if not src.is_file():
        return f"No proposed tool called {name!r}. `dduet-desktop tools list` shows what is waiting."
    ACTIVE.mkdir(parents=True, exist_ok=True)
    _path(name).write_text(src.read_text())
    src.unlink()
    return f"{name} is active. Callers can reach it from now on."


def remove(name: str) -> str:
    gone = [p for p in (_path(name), _path(name, pending=True)) if p.is_file()]
    for p in gone:
        p.unlink()
    return f"Removed {name}." if gone else f"No tool called {name!r}."


def active() -> list[str]:
    return sorted(p.stem for p in ACTIVE.glob("*.js")) if ACTIVE.is_dir() else []


def pending() -> list[str]:
    return sorted(p.stem for p in PENDING.glob("*.js")) if PENDING.is_dir() else []


def source(name: str) -> str:
    """The code of an ACTIVE tool. Only active — the daemon must never run a proposal."""
    p = _path(name)
    return p.read_text() if p.is_file() else ""
