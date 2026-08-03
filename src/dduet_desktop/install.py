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

#: LINUX layout. macOS is handled separately in install(): there the .app bundle is the unit and
#: /Applications is the destination, so neither of these applies.
#:
#: XDG user-level binaries. On PATH by default on most desktop Linux; we check rather than
#: assume, because a PATH that does not include it turns `dduet-desktop` into "command not found"
#: at exactly the moment the owner is told to type it.
BIN_DIR = pathlib.Path.home() / ".local/bin"
DESKTOP_DIR = pathlib.Path.home() / ".local/share/applications"
APP_ID = "dduet-desktop"

#: VERSIONED PAYLOAD + SYMLINK, copied from how Claude Code installs itself:
#:
#:     ~/.local/bin/dduet-desktop  ->  ~/.local/share/dduet-desktop/versions/0.1.0a2
#:
#: Three reasons, and the third is the one that matters most here:
#:
#:  1. Atomic. Write the new version beside the old, then flip one symlink. There is no window
#:     in which the binary on PATH is half-written.
#:  2. Rollback is a symlink flip. For an alpha that ships weekly and will occasionally break,
#:     that is worth more than the disk it costs.
#:  3. The path registered with the owner's assistant STAYS VALID. `hosts.connect` records a
#:     launch command; if that pointed at the versioned file it would go stale on every update
#:     and the assistant would quietly lose the secretary. It points at the symlink instead.
VERSIONS_DIR = pathlib.Path.home() / ".local/share" / APP_ID / "versions"


def version_path(version: str) -> pathlib.Path:
    return VERSIONS_DIR / version


def installed_versions() -> list[str]:
    try:
        return sorted(p.name for p in VERSIONS_DIR.iterdir() if p.is_file())
    except OSError:
        return []


def current_version() -> str:
    """What the symlink points at, or "" if there is no install."""
    link = installed_path()
    try:
        return pathlib.Path(os.readlink(link)).name if link.is_symlink() else ""
    except OSError:
        return ""


def running_from() -> pathlib.Path:
    """The binary (frozen) or the interpreter (source)."""
    return pathlib.Path(sys.executable).resolve()


def installed_path() -> pathlib.Path:
    return BIN_DIR / APP_ID


def is_installed() -> bool:
    """Installed means: the symlink exists and points at a version we have."""
    link = installed_path()
    return link.is_symlink() and link.resolve().is_file()


def on_path() -> bool:
    return str(BIN_DIR) in os.environ.get("PATH", "").split(os.pathsep)


def status() -> dict:
    """Everything the installer page needs to decide what to offer."""
    mac = sys.platform == "darwin"
    bundle = _app_bundle() if mac else None
    return {
        "frozen": bool(getattr(sys, "frozen", False)),
        "running_from": str(bundle or running_from()),
        "target": "/Applications" if mac else str(installed_path()),
        "installed": bool(bundle and str(bundle).startswith("/Applications/")) if mac
                     else is_installed(),
        "current_version": current_version(),
        "versions": installed_versions(),
        "on_path": on_path(),
        "platform": sys.platform,
    }


def _app_bundle() -> pathlib.Path | None:
    """The .app this binary lives inside, if any. macOS only."""
    for parent in running_from().parents:
        if parent.suffix == ".app":
            return parent
    return None


def install() -> str:
    """Put this where it belongs. Idempotent."""
    if not getattr(sys, "frozen", False):
        return ("Running from source, so there is nothing to install — the installed layout "
                "only applies to the downloaded binary.")

    # macOS: the .app IS the unit, and dragging it to Applications is the idiom every Mac user
    # already knows. We deliberately do NOT copy the bundle ourselves — that is untested code
    # moving an application directory, and a half-copied bundle is worse than an instruction.
    if sys.platform == "darwin":
        bundle = _app_bundle()
        if bundle is None:
            return ("This is the bare binary, not the app. Open the DMG and drag "
                    "DDuet Desktop to your Applications folder.")
        if str(bundle).startswith("/Applications/"):
            return f"Installed — running from {bundle}."
        return (f"Running from {bundle}.\n"
                f"Drag DDuet Desktop to your Applications folder, then open it from there. "
                f"Until you do, your AI assistant will be told to launch the secretary from "
                f"this location — and moving it later would break that link.")

    from . import __version__
    src = running_from()
    payload = version_path(__version__)
    link = installed_path()

    if src == payload:
        return f"Already running the installed copy ({__version__})."

    try:
        VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        # Write beside, then move into place: an interrupted copy must never leave a truncated
        # file that the symlink would happily point at.
        tmp = payload.with_suffix(".partial")
        shutil.copy2(src, tmp)
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        tmp.replace(payload)

        # Flip the symlink atomically — os.replace on a temp link, not unlink-then-link, so
        # there is no instant where the command on PATH does not exist.
        tmp_link = link.with_suffix(".partial")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(payload)
        os.replace(tmp_link, link)
    except OSError as exc:
        return f"Could not install: {exc}"

    notes = [f"Installed {__version__} to {payload}",
             f"  {link} → {payload.name}"]
    others = [v for v in installed_versions() if v != __version__]
    if others:
        notes.append(f"  kept for rollback: {', '.join(others)}")
    notes.append(_desktop_entry(link))
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


def rollback(version: str) -> str:
    """Point the symlink at an older version. The whole reason for keeping them."""
    payload = version_path(version)
    if not payload.is_file():
        return f"No installed version {version!r}. Have: {', '.join(installed_versions()) or 'none'}"
    link = installed_path()
    try:
        tmp = link.with_suffix(".partial")
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(payload)
        os.replace(tmp, link)
    except OSError as exc:
        return f"Could not roll back: {exc}"
    return f"Rolled back to {version}. Restart the daemon for it to take effect."


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
