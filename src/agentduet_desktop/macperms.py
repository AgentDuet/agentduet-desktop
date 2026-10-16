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
The owner may switch it off: Settings' "Sync to iCloud" (`## Sync to iCloud`, off unless an
explicit yes) removes the mark from the folder in use, after a warning about what Apple then holds.

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
import logging
import threading

from . import paths

logger = logging.getLogger("dduet.macperms")

#: The mark iCloud Drive's File Provider honours as "do not sync this".
IGNORE_XATTR = "com.apple.fileprovider.ignore#P"

#: The only places System Settings is opened to. Fixed: no caller supplies a URL.
PRIVACY = {
    "privacy": "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders",
    "privacy-mic": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "privacy-contacts": "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts",
}

_asking = threading.Event()
_marked: set[str] = set()
#: (folder, sync) pairs already applied in this process — see `sync_choice`.
_applied: set[tuple[str, bool]] = set()


def applies() -> bool:
    return sys.platform == "darwin"


def sandboxed() -> bool:
    """Running in Apple's sandbox — the App Store build. macOS sets this for every sandboxed
    process, the helper daemon included."""
    return bool(os.environ.get("APP_SANDBOX_CONTAINER_ID"))


def _chosen() -> pathlib.Path | None:
    """IN THE SANDBOX, the folder the owner allowed in the system panel, as the shell recorded
    it (`FolderAccess.swift`). The shell holds the grant; this process only learns the path."""
    try:
        raw = (paths.RUN / "documents-folder").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pathlib.Path(raw) if raw else None


def real_documents() -> pathlib.Path:
    """The owner's own Documents. In the sandbox `Path.home()` is the app's CONTAINER."""
    import pwd
    return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) / "Documents"


def documents_folder() -> pathlib.Path:
    """`~/Documents/AgentDuet`. In the sandbox, the folder the owner allowed: setup allows
    Documents itself, and AgentDuet goes inside it; a folder chosen in Settings is the exact
    folder they want, and is used as it is."""
    if sandboxed():
        chosen = _chosen()
        if chosen is None:
            return pathlib.Path.home() / "Documents" / "AgentDuet"
        return chosen / "AgentDuet" if chosen == real_documents() else chosen
    return pathlib.Path.home() / "Documents" / "AgentDuet"


def _state_file() -> pathlib.Path:
    return paths.RUN / "permissions.json"


def _read() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(key: str, value: str) -> None:
    # EVERY CHANGE OF ANSWER IS LOGGED (2026-09-30): a tester's "I pressed Allow and nothing
    # happened" could not be answered from the log, because nothing here ever wrote to it.
    logger.info("permission %s: %s", key, value)
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


def unmark(folder: pathlib.Path) -> bool:
    """Let iCloud sync `folder` again. True when the mark is off, including when it never was."""
    if not applies():
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.removexattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    if libc.removexattr(os.fsencode(str(folder)), IGNORE_XATTR.encode(), 0) == 0:
        return True
    return ctypes.get_errno() == 93          # ENOATTR: it was not marked


def sync_choice(folder: pathlib.Path) -> None:
    """Put the owner's iCloud choice on `folder`: the mark, unless `## Sync to iCloud` says yes.

    OFF IS THE DEFAULT AND IT IS ENFORCED, not assumed: the folder is marked whatever iCloud is
    doing, because "Desktop & Documents" sync can be switched on at any time after setup. Once
    per folder and choice in a process, so the hot path costs a set lookup; a failure is retried
    on the next lookup. A changed setting applies at the next lookup, since the value is part of
    the key; saving it calls `forget_choice` so it is applied again even when unchanged.
    """
    if not applies():
        return
    from . import owner
    want = owner.sync_icloud()
    key = (str(folder), want)
    if key in _applied:
        return
    if unmark(folder) if want else mark_local(folder):
        _applied.add(key)


def forget_choice() -> None:
    """The setting changed: apply it again at the next lookup."""
    _applied.clear()


def _cloud_domain(path: pathlib.Path) -> bool:
    """Whether macOS has put `path` in iCloud Drive's File Provider domain — which it does to
    Desktop and Documents while "Desktop & Documents Folders" is on (seen 2026-10-16)."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    buf = ctypes.create_string_buffer(512)
    n = libc.getxattr(os.fsencode(str(path)), b"com.apple.file-provider-domain-id", buf, 512, 0, 0)
    return n > 0 and b"CloudDocs" in buf.raw[:n]


def in_icloud(folder: pathlib.Path) -> bool:
    """Whether `folder` is somewhere iCloud Drive syncs: iCloud Drive itself, or Desktop or
    Documents while they sync. Only then does `## Sync to iCloud` change anything."""
    if not applies():
        return False
    home = pathlib.Path.home()
    try:
        f = folder.resolve()
        if f.is_relative_to((home / "Library" / "Mobile Documents").resolve()):
            return True
        return any(f.is_relative_to((home / top).resolve()) and _cloud_domain(home / top)
                   for top in ("Desktop", "Documents"))
    except OSError:
        return False


def _open_documents() -> None:
    """Create the folder and put the iCloud choice on it. The first call is the one macOS asks
    about."""
    folder = documents_folder()
    folder.mkdir(parents=True, exist_ok=True)
    os.listdir(folder)
    sync_choice(folder)
    _marked.add(str(folder))


def documents_state() -> str:
    """"not-asked", "asking", "granted" or "denied". Never prompts: see the module docstring."""
    if not applies():
        return "granted"
    if _asking.is_set():
        return "asking"
    rec = _read().get("documents")
    # IN THE SANDBOX the owner has answered once the shell has recorded a folder; the panel
    # is the question, and there is no macOS prompt to have avoided.
    if rec is None and not (sandboxed() and _chosen()):
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
    # IN THE SANDBOX the page asks the shell for the panel instead; there is nothing to touch here.
    if not applies() or sandboxed() or _asking.is_set():
        return

    def _go() -> None:
        try:
            _open_documents()
            _write("documents", "granted")
        except OSError as exc:
            logger.info("documents: macOS refused access (%s)", exc)
            _write("documents", "denied")
        finally:
            _asking.clear()

    logger.info("documents: asking macOS for %s", documents_folder())
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
    # AN EXISTING INSTALL KEEPS ITS FOLDER — except in the sandbox, where the owner has just
    # CHOSEN one in the system panel, and a choice made that explicitly is what they meant.
    try:
        if not sandboxed() and legacy.is_dir() and any(legacy.iterdir()):
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
