"""Start the daemon when the owner logs in.

WHY THIS TAKES NO ARGUMENTS

Writing a launch agent is how software survives a reboot. It is also how malware does. This is
offered to an agent that reads escalations and call transcripts written by strangers, so a
parameterised version — one that accepts a path, a command, or arguments — is a path from prompt
injection to persistent autostart on the owner's machine.

With no arguments the blast radius is one boolean: it either registers THIS binary, at the path it
already resolves to, or it does not. The tempting "accept a path so it is flexible" version IS the
vulnerability, and there is no safe way to validate a path supplied by something a stranger can
talk to.

WHY IT REGISTERS THE SYMLINK

`install.installed_path()`, not `sys.executable`. An installed build lives at a versioned path and
`sys.executable` resolves to it, so a login item pointing there would keep launching the old
version after every update — silently, because the new one is never started and nobody looks at a
login item twice.

WHAT IT IS NOT

Not a service manager. It asks the operating system to run one command at login; everything about
whether the daemon is healthy stays with `service_status`. A login item that lies about the
daemon being up would be worse than none.
"""

import os
import pathlib
import subprocess
import sys

LABEL = "com.b3networks.dduet-desktop"

#: macOS. A LaunchAgent (user scope, ~/Library) rather than a LaunchDaemon (root, /Library):
#: nothing here needs root, and a root daemon answering a stranger's phone call is a much larger
#: thing to have installed.
MAC_PLIST = pathlib.Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"

#: Linux. A systemd USER unit, so it needs no sudo and starts with the owner's session.
LINUX_UNIT = pathlib.Path.home() / ".config/systemd/user/dduet-desktop.service"

#: Windows. The per-user Startup folder. Task Scheduler would be tidier, but it means shelling out
#: to schtasks with an XML payload, and a .cmd in Startup is inspectable by the owner — which for
#: a thing that runs at every login is worth more than tidiness.
WIN_STARTUP = (pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home()))
               / "Microsoft/Windows/Start Menu/Programs/Startup" / "dduet-desktop.cmd")


def _target() -> pathlib.Path | None:
    """The command to register: the stable symlink, never the versioned payload."""
    from . import install
    link = install.installed_path()
    if link.is_symlink() and link.resolve().is_file():
        return link
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable)
    return None                      # from source there is nothing durable to register


def _unit_path() -> pathlib.Path:
    if sys.platform == "darwin":
        return MAC_PLIST
    if sys.platform.startswith("win"):
        return WIN_STARTUP
    return LINUX_UNIT


def _plist(exe: pathlib.Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
    <string>run</string>
    <string>--headless</string>
  </array>
  <key>RunAtLoad</key><true/>
  <!-- KeepAlive is deliberately absent. A crash loop that relaunches every second, answering a
       phone line, is worse than a daemon that is down and visible in `status`. -->
</dict>
</plist>
"""


def _unit(exe: pathlib.Path) -> str:
    return f"""[Unit]
Description=DDuet Desktop — answers your calls and messages
After=network-online.target

[Service]
# --headless: at login there is nobody looking at a browser yet, and opening one would be a
# window appearing for no reason on every boot.
ExecStart={exe} run --headless
# on-failure, not always: see the plist comment about crash loops.
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def install_login_item() -> str:
    """Register the daemon to start at login. Idempotent. Takes no arguments, on purpose."""
    exe = _target()
    if exe is None:
        return ("Not installed, so there is nothing durable to register — the path would be a "
                "download folder or a dev venv. Install it first, then try again.")
    path = _unit_path()
    body = (_plist(exe) if sys.platform == "darwin"
            else f'@echo off\r\nstart "" "{exe}" run --headless\r\n'
            if sys.platform.startswith("win") else _unit(exe))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        already = path.is_file() and path.read_text() == body
        if not already:
            path.write_text(body)
    except OSError as exc:
        return f"Could not write {path}: {exc}"

    out = [f"{'Already registered' if already else 'Registered'} to start at login.",
           f"  wrote {path}",
           f"  runs  {exe} run --headless"]
    out.append(_activate(path))
    # SAY WHAT WAS WRITTEN. This is a file that runs a command at every login; an owner who cannot
    # see which file, and delete it, has been given something they cannot take back.
    out.append("Remove it with `remove_login_item`, or delete that file.")
    return "\n".join(out)


def _activate(path: pathlib.Path) -> str:
    """Tell the OS to pick it up now, so it works before the next login too."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "load", "-w", str(path)],
                           capture_output=True, timeout=15)
            return "  loaded with launchctl"
        if sys.platform.startswith("win"):
            return "  active at the next login"
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=15)
        done = subprocess.run(["systemctl", "--user", "enable", "dduet-desktop.service"],
                              capture_output=True, text=True, timeout=15)
        if done.returncode != 0:
            # Common and worth naming: on a headless box or over plain ssh there may be no user
            # bus, so the unit is written but cannot be enabled until a real session exists.
            return ("  written, but systemd could not enable it (no user session bus?) — it will "
                    "apply once you log in graphically")
        return "  enabled with systemctl --user"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"  written, but could not be activated now: {exc}"


def remove_login_item() -> str:
    """Stop starting at login. Takes no arguments."""
    path = _unit_path()
    if not path.is_file():
        return "It was not registered to start at login."
    try:
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "unload", "-w", str(path)],
                           capture_output=True, timeout=15)
        elif not sys.platform.startswith("win"):
            subprocess.run(["systemctl", "--user", "disable", "dduet-desktop.service"],
                           capture_output=True, timeout=15)
        path.unlink()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not remove {path}: {exc}"
    return f"No longer starts at login. Removed {path}."


def login_item_status() -> str:
    """Whether the daemon is registered to start at login — and NOT whether it is running."""
    path = _unit_path()
    if not path.is_file():
        return "Does not start at login. `install_login_item` sets that up."
    exe = _target()
    body = path.read_text()
    stale = exe is not None and str(exe) not in body
    out = [f"Starts at login — {path}"]
    if stale:
        # The failure that would otherwise be silent: an old path still registered, so every
        # login launches a binary that has moved or been replaced.
        out.append(f"  BUT it points somewhere else, not {exe}. Run install_login_item to fix.")
    out.append("  whether it is running right now is a separate question — see service_status")
    return "\n".join(out)
