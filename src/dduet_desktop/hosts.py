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
        # THE SYMLINK, not sys.executable. An installed build lives at a versioned path
        # (~/.local/share/dduet-desktop/versions/0.1.0a2) and sys.executable resolves to it —
        # so registering that would go stale on the next update and the owner's assistant would
        # quietly lose the secretary. ~/.local/bin/dduet-desktop always points at current.
        from . import install
        link = install.installed_path()
        if link.is_symlink() and link.resolve().is_file():
            return [str(link), "mcp"]
        return [sys.executable, "mcp"]          # not installed yet: register where it is
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

    # "Already registered" is not the same as "registered correctly". An entry made before the
    # install points at wherever the binary was then — a download folder, or a dev venv — and
    # leaving it would defeat the reason we register the symlink at all. Replace it.
    if any("already exists" in l for l in err):
        try:
            subprocess.run(["claude", "mcp", "remove", SERVER_NAME, "-s", "user"],
                           capture_output=True, text=True, timeout=30)
            again = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"    already registered, and could not be updated: {exc}"
        if again.returncode == 0:
            return f"    updated `{SERVER_NAME}` to point at {launch_command()[0]}"
        return f"    already registered as `{SERVER_NAME}`, and re-registering failed"
    return "    claude refused: " + (err[0] if err else f"exit {done.returncode}")


#: Goose's own documented shape for an external MCP server. Verified against a real installed
#: config.yaml on this machine and against the published schema — the previous version printed a
#: JSON blob for the owner to paste into a YAML file, which would have corrupted it.
GOOSE_CONFIG = pathlib.Path.home() / ".config/goose/config.yaml"


def _register_goose(apply: bool) -> str:
    """Add the dduet extension to Goose's config. Serves the CLI and the Desktop app both —
    they share this file."""
    cmd, *args = launch_command()
    entry = {"name": SERVER_NAME, "cmd": cmd, "args": args, "enabled": True,
             "envs": {}, "type": "stdio", "timeout": 300}
    if not apply:
        return (f"    would add a `{SERVER_NAME}` stdio extension to {GOOSE_CONFIG}\n"
                f"      cmd: {cmd}  args: {args}")
    try:
        import yaml
    except ImportError:
        return "    pyyaml is not available, so the config cannot be edited safely"
    try:
        cfg = yaml.safe_load(GOOSE_CONFIG.read_text()) if GOOSE_CONFIG.is_file() else {}
    except (OSError, yaml.YAMLError) as exc:
        return f"    could not read {GOOSE_CONFIG}: {exc}"
    if not isinstance(cfg, dict):
        return f"    {GOOSE_CONFIG} is not a mapping — refusing to overwrite it"

    existing = (cfg.get("extensions") or {}).get(SERVER_NAME)
    if existing == entry:
        return f"    already registered in {GOOSE_CONFIG.name}"

    cfg.setdefault("extensions", {})[SERVER_NAME] = entry
    try:
        # Back up first. This is someone's working assistant config, and we are the second
        # program to write it.
        GOOSE_CONFIG.with_suffix(".yaml.dduet-backup").write_text(GOOSE_CONFIG.read_text()
                                                                 if GOOSE_CONFIG.is_file() else "")
        GOOSE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        GOOSE_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    except (OSError, yaml.YAMLError) as exc:
        return f"    could not write {GOOSE_CONFIG}: {exc}"
    verb = "updated" if existing else "added"
    return (f"    {verb} the `{SERVER_NAME}` extension in {GOOSE_CONFIG.name} "
            f"(backup alongside it)\n"
            f"    Works in both the Goose CLI and Goose Desktop — they share this file.")


def _manual(label: str, where: str) -> str:
    """For hosts we can detect but will not write to. Print what to paste."""
    entry = json.dumps({SERVER_NAME: {"command": launch_command()[0],
                                      "args": launch_command()[1:]}}, indent=2)
    return (f"    found, but not configured automatically — its format is untested here.\n"
            f"    Add this to {where}:\n"
            + "\n".join(f"      {l}" for l in entry.splitlines()))


#: Goose's own installer. Pinned so an install is reproducible and we can say what we put on
#: someone's machine; `stable` would be whatever it resolved to that day.
#:
#: block/goose 301-redirects to aaif-goose/goose — an org rename, verified: same repo, same
#: star count, same push date. The script's own REPO variable says aaif-goose, which looks
#: alarming next to a block/... URL and is not.
GOOSE_SCRIPT = "https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh"
GOOSE_VERSION = ""          # empty = stable; set to pin, e.g. "v1.0.25"


def install_goose(apply: bool = True) -> str:
    """Install Goose using its OWN installer, driven by its documented env vars.

    WHY THE SCRIPT AND NOT A HAND-ROLLED DOWNLOAD

    The usual objections to `curl | bash` are that you cannot pin a version, you do not learn
    where it put things, and it may want a TTY. Reading the script answers all three:
    GOOSE_BIN_DIR, GOOSE_VERSION and CONFIGURE=false. It is `set -eu` and checks its
    dependencies first. Re-implementing it would be more code, worse tested, and would drift
    from their release process.

    We DOWNLOAD then RUN, rather than piping, only so the exact bytes we executed are on disk
    if something goes wrong. That is an audit trail, not a security boundary — we are running
    their binary either way.
    """
    bin_dir = pathlib.Path.home() / ".local/bin"
    env = {**os.environ, "GOOSE_BIN_DIR": str(bin_dir)}
    if GOOSE_VERSION:
        env["GOOSE_VERSION"] = GOOSE_VERSION

    # USE THE KEY THE OWNER ALREADY GAVE US. Goose's installer takes GOOSE_PROVIDER, GOOSE_MODEL
    # and the provider's own key variable, and configures itself when they are present — so a
    # DashScope owner does not have to run `goose configure` and paste the same key a second
    # time. Its provider name for DashScope is `alibaba`, read off a real installed config.
    #
    # Without a key there is nothing to configure with, so CONFIGURE stays false rather than
    # risking an interactive prompt in an installer that may have no TTY.
    key = os.getenv("DASHSCOPE_API_KEY", "")
    model = os.getenv("SECRETARY_MODEL", "")
    if key:
        env["GOOSE_PROVIDER"] = "alibaba"
        env["DASHSCOPE_API_KEY"] = key
        if model:
            env["GOOSE_MODEL"] = model
    else:
        env["CONFIGURE"] = "false"

    if not apply:
        shown = " ".join(f"{k}={'<redacted>' if 'KEY' in k else v}"
                         for k, v in sorted(env.items())
                         if k.startswith(("GOOSE_", "CONFIGURE", "DASHSCOPE")))
        return ("    would download " + GOOSE_SCRIPT + "\n"
                f"    and run it with {shown}")

    import tempfile
    import urllib.request
    try:
        with urllib.request.urlopen(GOOSE_SCRIPT, timeout=60) as r:
            script = r.read()
    except Exception as exc:
        return f"    could not download the Goose installer: {exc}"
    tmp = pathlib.Path(tempfile.gettempdir()) / "goose-install.sh"
    tmp.write_bytes(script)
    try:
        done = subprocess.run(["bash", str(tmp)], env=env, capture_output=True,
                              text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"    the Goose installer failed to run: {exc}"
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
        detail = "\n".join(f"      {l}" for l in tail)
        return (f"    the Goose installer exited {done.returncode}:\n{detail}\n"
                f"      (the script it ran is at {tmp})")

    where = bin_dir / "goose"
    if not where.exists():
        return f"    the installer reported success but {where} is not there."
    configured = "GOOSE_PROVIDER" in env
    return (
        f"    installed Goose at {where}\n"
        + (f"    Configured it for alibaba/{model or 'default'} with the key you already gave —\n"
           f"    no need to run `goose configure`. If Goose says it has no credential, run it\n"
           f"    once and it will store the key itself.\n"
           if configured else
           f"    Not configured — no DashScope key in this instance. Run `goose configure`.\n")
        + f"    Registering this secretary with it now.\n"
        f"    Set its permission mode to APPROVE, not SmartApprove: SmartApprove asks an\n"
        f"    LLM whether a tool call is safe, and the text your secretary hands it was\n"
        f"    written by strangers. A judge the attacker can talk to is not a control.")


def connect(apply: bool = True, install: str = "") -> str:
    """Register with every assistant found. `apply=False` shows without changing anything."""
    found = detect()
    head = ["  This secretary is driven from an AI assistant.",
            f"  It will be launched as:  {' '.join(launch_command())}", ""]
    if not getattr(sys, "frozen", False):
        head.insert(2, "  (running from source — an installed build registers its own binary)")

    if install == "goose":
        return "\n".join(head + ["  Installing Goose (its own installer, non-interactive)", "",
                                 install_goose(apply)])

    if not found:
        return "\n".join(head + [
            "  No AI assistant found.",
            "",
            "  `dduet-desktop connect --install goose` will install one. Goose is the option we",
            "  suggest: it works with any model provider including local ones, the software is",
            "  free, and it has real per-tool permission modes.",
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
        elif label == "Goose":
            lines.append(_register_goose(apply))
        else:
            where = next(w for l, _, w in KNOWN if l == label)
            lines.append(_manual(label, where))
    if apply:
        lines += ["", "  Restart the assistant — MCP servers are started when it starts."]
    return "\n".join(lines)
