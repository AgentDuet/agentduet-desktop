"""Register this secretary with whatever AI assistant the owner already has.

WHY THIS IS THE INSTALLER'S REAL JOB

With no owner interface, the assistant IS the owner's surface. So the step between "installed"
and "usable" is one line of MCP configuration — and it is exactly the step that loses people,
because it is a path, a module name and a flag they have no way to guess.

WHY NOT INSTALL AN ASSISTANT FOR THEM

Considered and rejected. Bundling one contradicts being assistant-agnostic (it picks a winner),
makes us a distributor of someone else's updates and CVEs, and roughly doubles the download —
Goose is a Rust binary, Claude Code needs Node. If it is ever done it must come from their
PREBUILT RELEASES, never from git: Goose from source means shipping a toolchain and minutes of
compilation on the owner's machine.

WHY THE COMMAND IS `<binary> mcp`

Not `python -m dduet_desktop.secretary_mcp`. That works only here: an installed owner has no
python and no module path, and inside a frozen binary `sys.executable` IS the binary. Anything
registered the dev way points at a venv that exists on one machine.

WHY WE ONLY WRITE CONFIG FOR HOSTS WE CAN TEST

Claude Code has a CLI (`claude mcp add`) that owns its own format, so we use it. For the others
we DETECT and print the exact thing to paste. Silently rewriting someone's Goose or Cursor
config, in a format we have never exercised, is how an installer breaks a working setup — and
the owner would blame the thing they just installed. Detection is honest; blind writes are not.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys

#: What an assistant should be told to launch. In a PyInstaller build sys.executable is the
#: binary itself and there is no module to name; from source it is the interpreter plus -m.
def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "mcp"]
    return [sys.executable, "-m", "dduet_desktop.secretary_mcp"]


#: Name the server is registered under. Deliberately not "secretary": a user-scoped server of
#: that name already pointed at the superseded POC on this machine, and anyone driving
#: "secretary" from an assistant had been driving the wrong thing for weeks.
SERVER_NAME = "dduet"

HOME = pathlib.Path.home()

#: (label, how to detect it, where its config lives — for the message only)
#:
#: Detection needs REAL evidence: a binary on PATH, or a config file the host actually writes.
#: A directory alone is not enough — ~/.cursor survived on this machine from March with no
#: cursor binary and no mcp.json, so directory-existence reported an assistant that was not
#: there. Telling an owner to configure software they do not have is worse than saying nothing:
#: they cannot act on it, and it makes the rest of the output look untrustworthy.
KNOWN = [
    ("Claude Code", lambda: shutil.which("claude") is not None, "~/.claude.json"),
    ("Goose", lambda: shutil.which("goose") is not None
     or (HOME / ".config/goose/config.yaml").is_file(), "~/.config/goose/config.yaml"),
    ("Cursor", lambda: shutil.which("cursor") is not None
     or (HOME / ".cursor/mcp.json").is_file(), "~/.cursor/mcp.json"),
    ("Claude Desktop",
     lambda: (HOME / ".config/Claude/claude_desktop_config.json").is_file()
     or (HOME / "Library/Application Support/Claude/claude_desktop_config.json").is_file(),
     "claude_desktop_config.json"),
]


def detect() -> list[str]:
    """Which assistants appear to be installed."""
    return [label for label, present, _ in KNOWN if _safe(present)]


def _safe(check) -> bool:
    try:
        return bool(check())
    except OSError:
        return False


def _register_claude_code(apply: bool) -> str:
    cmd = ["claude", "mcp", "add", SERVER_NAME, "-s", "user", "--", *launch_command()]
    if not apply:
        return "    would run: " + " ".join(cmd)
    # -s user, not local: the owner's secretary is not a property of whichever directory they
    # happened to be in when they installed it.
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"    could not run `claude`: {exc}"
    if done.returncode == 0:
        return f"    registered as `{SERVER_NAME}` (user scope)"
    err = (done.stderr or done.stdout or "").strip().splitlines()
    if any("already exists" in l for l in err):
        return f"    already registered as `{SERVER_NAME}`"
    return "    claude refused: " + (err[0] if err else f"exit {done.returncode}")


def _manual(label: str, where: str) -> str:
    """For hosts we can detect but will not write to. Print what to paste."""
    entry = json.dumps({SERVER_NAME: {"command": launch_command()[0],
                                      "args": launch_command()[1:]}}, indent=2)
    return (f"    found, but not configured automatically — its format is untested here.\n"
            f"    Add this to {where}:\n"
            + "\n".join(f"      {l}" for l in entry.splitlines()))


def connect(apply: bool = True) -> str:
    """Register with every assistant found. `apply=False` shows without changing anything."""
    found = detect()
    head = ["  This secretary is driven from an AI assistant.",
            f"  It will be launched as:  {' '.join(launch_command())}", ""]
    if not getattr(sys, "frozen", False):
        head.insert(2, "  (running from source — an installed build registers its own binary)")

    if not found:
        return "\n".join(head + [
            "  No AI assistant found.",
            "",
            "  DDuet works without one — the daemon still answers calls and messages, and",
            "  `dduet-desktop status` shows what is happening. But there is no way to read",
            "  your escalations or change what it knows until you have one.",
            "",
            "  Any MCP-speaking assistant works. Claude Code and Goose are the two we test.",
            "  Install one, then run `dduet-desktop connect` again."])

    lines = head + [f"  Found: {', '.join(found)}", ""]
    for label in found:
        lines.append(f"  {label}")
        if label == "Claude Code":
            lines.append(_register_claude_code(apply))
        else:
            where = next(w for l, _, w in KNOWN if l == label)
            lines.append(_manual(label, where))
    if apply:
        lines += ["", "  Restart the assistant — MCP servers are started when it starts."]
    return "\n".join(lines)
