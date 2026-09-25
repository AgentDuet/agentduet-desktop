"""The macOS permissions setup asks for, one at a time — and the Documents folder they unlock.

WHY DOCUMENTS (Stanley, 2026-09-25). Recordings and their transcripts are documents the owner
opens, so on a Mac they belong in `~/Documents/AgentDuet`, not in a hidden `~/.agentduet-desktop`.

THE TRAP IN THAT, and the reason this module exists: iCloud "Desktop & Documents" sync is on for
many Macs (it is on the one this was written on), with Optimize Mac Storage. A recordings folder
in Documents would then UPLOAD every call — breaking "stored only on your machine", the claim a
regulated buyer is asking about — and macOS could later evict a recording to a placeholder. So
the folder is marked `com.apple.fileprovider.ignore#P`, which iCloud honours. Measured on macOS
26.6.2 against an unmarked control, 2026-09-25: the control uploaded within 20 s; the marked
folder never became an iCloud item, nor did a subfolder made later inside it; the mark survived
a rename; and marking an already-synced folder took it out of iCloud within 25 s. It is set
again on first use in every process, so the protection comes back if something strips it.

THE PERMISSION. Documents is privacy-protected: the first access asks "AgentDuet would like to
access files in your Documents folder" and BLOCKS until answered. There is no API to read the
answer without asking, so this never touches Documents until the owner presses Allow in setup,
and records the outcome. After that a check is safe: once decided, access either works or fails
at once with a PermissionError, and never prompts again.

WHO GETS IT. Only an install that asked and was granted. An existing install has no record and
keeps `$AGENTDUET_HOME/run/recordings`, and so does any install whose old folder already holds
recordings — moving the default under an owner would split their history across two folders.
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import subprocess
import sys
import threading

from . import paths

#: The mark iCloud Drive's File Provider honours as "do not sync this".
IGNORE_XATTR = "com.apple.fileprovider.ignore#P"

#: The only places System Settings is opened to. Fixed: no caller supplies a URL.
PRIVACY = {
    "privacy": "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders",
    "privacy-mic": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}

_asking = threading.Event()
_marked: set[str] = set()


def applies() -> bool:
    return sys.platform == "darwin"


def documents_folder() -> pathlib.Path:
    return pathlib.Path.home() / "Documents" / "AgentDuet"


def _state_file() -> pathlib.Path:
    return paths.RUN / "permissions.json"


def _read() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(key: str, value: str) -> None:
    d = _read()
    d[key] = value
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d), encoding="utf-8")


def mark_local(folder: pathlib.Path) -> bool:
    """Keep `folder` out of iCloud. True when the mark is on. A no-op off macOS."""
    if not applies():
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    value = b"1"
    rc = libc.setxattr(os.fsencode(str(folder)), IGNORE_XATTR.encode(), value, len(value), 0, 0)
    return rc == 0


def _open_documents() -> None:
    """Create and mark the folder. The first call is the one macOS asks about."""
    folder = documents_folder()
    folder.mkdir(parents=True, exist_ok=True)
    os.listdir(folder)
    mark_local(folder)
    _marked.add(str(folder))


def documents_state() -> str:
    """"not-asked", "asking", "granted" or "denied". Never prompts: see the module docstring."""
    if not applies():
        return "granted"
    if _asking.is_set():
        return "asking"
    rec = _read().get("documents")
    if rec is None:
        return "not-asked"
    try:
        _open_documents()
    except PermissionError:
        if rec != "denied":
            _write("documents", "denied")
        return "denied"
    except OSError:
        return "denied"
    if rec != "granted":
        _write("documents", "granted")
    return "granted"


def request_documents() -> None:
    """Ask, in the background: the call blocks until the owner answers the macOS prompt."""
    if not applies() or _asking.is_set():
        return

    def _go() -> None:
        try:
            _open_documents()
            _write("documents", "granted")
        except OSError:
            _write("documents", "denied")
        finally:
            _asking.clear()

    _asking.set()
    threading.Thread(target=_go, name="documents-permission", daemon=True).start()


def open_privacy_settings(pane: str = "privacy") -> None:
    if applies() and pane in PRIVACY:
        subprocess.Popen(["open", PRIVACY[pane]])


def recordings_default(legacy: pathlib.Path) -> pathlib.Path | None:
    """`~/Documents/AgentDuet` when this install was granted it and has no older recordings.

    Cheap, because it is read on every recordings lookup: one small JSON read, and the folder
    is touched only the first time in a process — to re-apply the iCloud mark.
    """
    if not applies() or _read().get("documents") != "granted":
        return None
    try:
        if legacy.is_dir() and any(legacy.iterdir()):
            return None
    except OSError:
        pass
    folder = documents_folder()
    if str(folder) not in _marked:
        try:
            _open_documents()
        except OSError:
            return None
    return folder
