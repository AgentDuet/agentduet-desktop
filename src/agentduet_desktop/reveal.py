"""Talk to the desktop: open a folder, and ask the owner to choose one.

Both live here because they share the same precondition — a desktop session to draw into — and
the same platform split. On a headless box neither is possible, and that is not a fault: it is
the normal state of a self-hosted install reached over ssh, where the console path sets the
folder instead.

WHY A MODULE AND NOT THREE LINES IN A ROUTE. The command differs per platform, the failure
modes differ per platform, and one of them (Linux with no desktop session) is not a failure at
all — it is the normal case for a self-hosted box reached over ssh, where there is no file
manager to open and saying so is the right answer.

WHAT IT WILL NOT DO: open a path it was handed. The caller names WHICH folder it wants by a
key, and this resolves it. A route that opens an arbitrary path is a way to launch a file
manager on anything readable, from a page that is only as private as its token.
"""

import logging
import os
import pathlib
import platform
import shutil
import subprocess

logger = logging.getLogger("dduet.reveal")


def folders() -> dict:
    """The folders an owner may be shown, by key. The ONLY paths this module will open."""
    from . import carry, voice
    root = carry.recordings()
    return {"recordings": root, "answered": root / voice.ANSWERED}


def available() -> tuple[bool, str]:
    """Whether a file manager can be opened at all."""
    system = platform.system()
    if system in ("Darwin", "Windows"):
        return True, ""
    # Linux: needs both the tool and a session to open into. Over ssh there is neither, and a
    # button that silently does nothing is worse than one that is not there.
    if not shutil.which("xdg-open"):
        return False, "no xdg-open on this machine"
    if not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        return False, "no desktop session — this looks like a headless machine"
    return True, ""


def open_folder(key: str) -> str:
    """Show one of `folders()` in the desktop's file manager."""
    target = folders().get(key)
    if target is None:
        return f"No folder called {key!r}."
    ok, why = available()
    if not ok:
        return f"Cannot open a folder here: {why}. It is at {target}"
    # Created rather than refused: an owner clicking this before the first call should see the
    # empty folder, not an error about a directory that simply has not been needed yet.
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Could not create {target}: {exc}"

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(target)])
        elif system == "Windows":
            os.startfile(str(target))              # noqa: S606  (Windows-only)
        else:
            # Detached: a file manager started here must not die with the daemon, and must not
            # inherit its stdout — a chatty xdg-open would end up interleaved in the log.
            subprocess.Popen(["xdg-open", str(target)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception as exc:
        logger.warning("could not open %s: %s", target, exc)
        return f"Could not open the file manager: {exc}"
    return f"Opened {target}"


# ---- asking the owner to choose one ---------------------------------------------------------
#
# A NATIVE DIALOG, NOT `<input type="file" webkitdirectory>`. The browser control deliberately
# does not expose an absolute path — it hands back a relative name — so it cannot answer the one
# question being asked. The daemon runs on the owner's own machine, so it can ask the desktop
# properly and get a real path back.

#: Long enough for someone to actually browse, short enough that a dialog nobody answered does
#: not hold a thread for the life of the process.
PICK_TIMEOUT = 180


def can_pick() -> tuple[bool, str]:
    """Whether a folder chooser can be shown."""
    system = platform.system()
    if system == "Darwin":
        return (True, "") if shutil.which("osascript") else (False, "no osascript")
    if system == "Windows":
        return True, ""
    if not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        return False, "no desktop session"
    for tool in ("zenity", "kdialog", "qarma", "yad"):
        if shutil.which(tool):
            return True, ""
    return False, "no folder chooser installed (zenity or kdialog)"


def pick_folder(start: str = "") -> str:
    """Show the desktop's folder chooser. Returns the chosen absolute path, or "".

    "" means CANCELLED, which is not an error and must not be reported as one — a caller that
    treats an empty result as a failure turns "changed my mind" into a red message.
    """
    ok, why = can_pick()
    if not ok:
        raise RuntimeError(why)
    system = platform.system()
    start = start or str(pathlib.Path.home())
    if system == "Darwin":
        script = ('POSIX path of (choose folder with prompt "Where should recordings go?" '
                  f'default location POSIX file {start!r})')
        cmd = ["osascript", "-e", script]
    elif system == "Windows":
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
        cmd = ["powershell", "-NoProfile", "-Command", ps]
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", start]
    else:
        tool = next(t for t in ("zenity", "qarma", "yad") if shutil.which(t))
        cmd = [tool, "--file-selection", "--directory", f"--filename={start}/"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=PICK_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ""
    # A non-zero exit is how every one of these reports "cancelled", so it is not logged as a
    # failure and not distinguished from one — there is nothing the owner needs to do either way.
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()
