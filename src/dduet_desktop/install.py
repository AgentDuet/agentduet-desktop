"""Put the binary where it belongs, so it can be launched and stays put.

WHY AN INSTALL STEP AT ALL

Without one the binary runs from wherever it was downloaded. That is untidy, and it is also
WRONG in a way that bites later: `hosts.connect` registers the assistant's launch command using
the binary's current path. Register from ~/Downloads, then move it, and the MCP server silently
stops resolving — the owner's assistant loses the secretary and nothing explains why.

So installing is not tidiness. It is what makes the registered path stable.

WHY IT IS DRIVEN FROM A PAGE, NOT A PROMPT

The owner double-clicks a file they downloaded. There is no terminal in that story, and telling
them to open one is where a non-technical owner stops. The daemon already serves a local page,
so first run opens the browser — the same mechanism as the secrets form, and for the same
reason: no GUI toolkit to bundle, and every machine has a browser.

WHY NOT /usr/local/bin

That needs root. Nothing here needs root, and asking for it during a first run is both alarming
and unnecessary — ~/.local/bin is on PATH for most desktop Linux and is the XDG convention.
"""

import os
import pathlib
import shutil
import stat
import sys

#: XDG user-level binaries. On PATH by default on most desktop Linux; we check rather than
#: assume, because a PATH that does not include it turns `dduet-desktop` into "command not found"
#: at exactly the moment the owner is told to type it.
BIN_DIR = pathlib.Path.home() / ".local/bin"
DESKTOP_DIR = pathlib.Path.home() / ".local/share/applications"
APP_ID = "dduet-desktop"


def running_from() -> pathlib.Path:
    """The binary (frozen) or the interpreter (source)."""
    return pathlib.Path(sys.executable).resolve()


def installed_path() -> pathlib.Path:
    return BIN_DIR / APP_ID


def is_installed() -> bool:
    p = installed_path()
    return p.is_file() and (not getattr(sys, "frozen", False) or p == running_from())


def on_path() -> bool:
    return str(BIN_DIR) in os.environ.get("PATH", "").split(os.pathsep)


def status() -> dict:
    """Everything the installer page needs to decide what to offer."""
    return {
        "frozen": bool(getattr(sys, "frozen", False)),
        "running_from": str(running_from()),
        "target": str(installed_path()),
        "installed": is_installed(),
        "on_path": on_path(),
        "platform": sys.platform,
    }


def install() -> str:
    """Copy this binary to ~/.local/bin and add a desktop entry. Idempotent."""
    if not getattr(sys, "frozen", False):
        return ("Running from source, so there is nothing to install — the installed layout "
                "only applies to the downloaded binary.")

    src = running_from()
    dest = installed_path()
    if src == dest:
        return f"Already installed at {dest}."

    try:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        # Copy to a temp name and replace, so a running copy is never truncated mid-write and
        # an interrupted install cannot leave a half-file that looks installed.
        tmp = dest.with_suffix(".new")
        shutil.copy2(src, tmp)
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        tmp.replace(dest)
    except OSError as exc:
        return f"Could not install to {dest}: {exc}"

    notes = [f"Installed to {dest}"]
    notes.append(_desktop_entry(dest))
    if not on_path():
        # Stated conditionally, not as a warning. We can only see the PATH of THIS process — a
        # double-clicked app inherits the desktop session's, not the shell's — so asserting it
        # is missing would be a false alarm on a machine where the shell has it. A false alarm
        # during a first install is worse than a note nobody needed.
        notes.append(
            f"If `{APP_ID}` is not found when you type it in a terminal, add this to "
            f"~/.profile and log in again:\n"
            f'      export PATH="$HOME/.local/bin:$PATH"')
    return "\n".join(notes)


def _desktop_entry(target: pathlib.Path) -> str:
    """A launcher entry, so it appears in the applications menu and survives a reboot.

    Not an autostart entry — starting at login is a separate, deliberate choice, and writing one
    here would make an install silently persistent.
    """
    try:
        DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
        entry = DESKTOP_DIR / f"{APP_ID}.desktop"
        entry.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=DDuet Desktop\n"
            "Comment=A secretary that answers your calls and messages\n"
            f"Exec={target}\n"
            "Terminal=false\n"
            "Categories=Office;Network;\n")
        entry.chmod(0o644)
        return f"Added a launcher entry at {entry}"
    except OSError as exc:
        return f"Could not write a launcher entry: {exc}"
