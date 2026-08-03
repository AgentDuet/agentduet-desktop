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


#: Defaults applied ONLY to a Goose we installed ourselves. Never to one that was already
#: there — that really is the owner's assistant and their settings.
#:
#: The distinction matters because of who this user is. They came for a secretary, not for an AI
#: agent, and Goose may be the first one they have ever opened. Whatever it does on their first
#: turn is something WE chose by installing it, so "they can change it in Goose" is not an
#: answer for someone who does not yet know what any of it means.
GOOSE_DEFAULTS = {
    # Per-tool approval. NOT smart_approve: that asks an LLM whether a call is safe, and the
    # text this secretary hands that agent was written by strangers. A judge the attacker can
    # talk to is not a control.
    #
    # KNOWN COST, measured not assumed: approve requires an interactive terminal, so `goose run
    # -t ...` fails with "invalid configuration". Goose Desktop and `goose session` are fine,
    # and those are what this owner uses — but anyone scripting Goose headlessly must set
    # GOOSE_MODE=auto for that run, and should understand what they are turning off.
    "GOOSE_MODE": "approve",
    # goose doctor itself flags the default as verbose (239 tokens to say hello). Less thinking
    # out loud is less to be confused by on a first encounter.
    "GOOSE_THINKING_EFFORT": "low",
}

#: OUR model provider name -> (Goose's provider name, its key variable, a model to start on).
#:
#: WHY A MODEL NAME IS PINNED HERE, having previously argued it should not be
#:
#: The earlier reasoning was that Goose "knows a sensible default per provider" and guessing
#: would be worse. That was simply wrong, and measured: with a provider set and no model, Goose
#: 1.45 refuses with `No model configured. Run 'goose configure' first.` There is no default to
#: fall back on, so declining to choose does not leave the owner with Goose's choice — it leaves
#: them at an error, which is exactly the dead end this install step exists to remove.
#:
#: These are the OWNER-facing agent's models, so they are the capable ones, not SECRETARY_MODEL.
#: An owner who wants a different one has a picker in Goose Desktop and in `goose configure`.
GOOSE_PROVIDERS = {
    "dashscope": ("alibaba", "DASHSCOPE_API_KEY", "qwen3.7-max"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY", "claude-sonnet-4-5"),
    "gemini": ("google", "GEMINI_API_KEY", "gemini-3.1-pro"),
}

#: Where Goose keeps provider keys when the system keyring is not used. Same directory as the
#: config, and shared with Desktop.
GOOSE_SECRETS = pathlib.Path.home() / ".config/goose/secrets.yaml"

#: Shell and file editing, on by default. A secretary's owner does not need it, and it is the
#: single widest thing an injected escalation could reach for. Disabled rather than removed, so
#: turning it back on is one setting away for anyone who does want Goose for code.
GOOSE_DISABLE = ["developer"]

#: Goose's "tom" extension injects this into every turn. A blank prompt tells a first-time user
#: nothing; this tells them what they have and what to ask for.
GOOSE_ORIENTATION = (
    "You are connected to DDuet Desktop, a secretary that answers this person's calls and "
    "messages while they are away. Use the `dduet` tools. If they seem unsure what to do, "
    "suggest: \"what is waiting for me?\", \"who has contacted me?\", \"is my secretary "
    "running?\", or telling you what they do so the secretary can answer for them."
)


def _set_goose_provider(cfg: dict) -> list[str]:
    """Point a freshly installed Goose at the model the owner already configured here.

    WHY WE WRITE THE CONFIG RATHER THAN LET THE INSTALLER DO IT

    Goose's install script offers a `configure` step, and we used to feed it GOOSE_PROVIDER and
    the key. Two things were wrong with that, both found by running it:

     1. Those variables are INERT in Goose 1.45. It records the choice as `active_provider` plus
        a `providers.<name>` block — not as GOOSE_PROVIDER/GOOSE_MODEL — so the install appeared
        to succeed and left the owner needing `goose configure` anyway.
     2. The script's configure step is INTERACTIVE, and its TTY detection does not save us. It
        tests `[ -t 0 ]`, and failing that falls back to `configure < /dev/tty` — which is
        readable from a daemon that inherited a controlling terminal it does not own. Goose then
        tried to drive that terminal and died with `Error: not connected`
        (Rust's ErrorKind::NotConnected), exiting 1 and taking the whole install with it.

    So the installer is now always run with CONFIGURE=false, and the configuration is ours.

    WHY THE KEY GOES IN A FILE RATHER THAN THE KEYRING

    Goose's default is the system secret service, which is the better place — but it does not
    exist on every machine (headless, WSL, some desktops), and a keyring write that fails is how
    this whole step broke the first time. File storage is Goose's own documented alternative and
    behaves the same everywhere. It is 0600, and the same key is already on disk at that mode in
    $DDUET_HOME/.env, so this adds no new exposure — but it IS weaker than a keyring, and the
    installer says so rather than quietly downgrading them.
    """
    from . import llm
    ours = llm.provider(os.getenv("SECRETARY_MODEL", ""))
    mapped = GOOSE_PROVIDERS.get(ours)
    if mapped is None:
        return []
    name, key_var, model = mapped
    key = os.getenv(key_var, "")
    if not key:
        return []

    changed = []
    if cfg.get("active_provider") != name:
        cfg["active_provider"] = name
        changed.append(f"provider={name}, model={model}")
    providers = cfg.setdefault("providers", {})
    if isinstance(providers, dict):
        providers[name] = {"enabled": True, "model": model, "configured": True}
    # Must be set for Goose to read secrets.yaml at all; otherwise it looks in the keyring only.
    if cfg.get("GOOSE_DISABLE_KEYRING") is not True:
        cfg["GOOSE_DISABLE_KEYRING"] = True
        changed.append("stored the key in a 0600 file, not the system keyring")

    try:
        import yaml
        GOOSE_SECRETS.parent.mkdir(parents=True, exist_ok=True)
        # Merge: another provider's key may already be in here.
        existing = {}
        if GOOSE_SECRETS.is_file():
            loaded = yaml.safe_load(GOOSE_SECRETS.read_text())
            existing = loaded if isinstance(loaded, dict) else {}
        if existing.get(key_var) != key:
            existing[key_var] = key
            GOOSE_SECRETS.write_text(yaml.safe_dump(existing, sort_keys=False))
            changed.append(f"gave it your {key_var} — no need to paste it again")
        GOOSE_SECRETS.chmod(0o600)
    except (OSError, ImportError, Exception) as exc:      # yaml errors included
        changed.append(f"could NOT store the key ({exc}) — run `goose configure`")
    return changed


def configure_goose_defaults() -> str:
    """Sensible, safer defaults for a Goose WE installed. Returns what changed."""
    try:
        import yaml
    except ImportError:
        return "    pyyaml unavailable — left Goose at its own defaults"
    try:
        cfg = yaml.safe_load(GOOSE_CONFIG.read_text()) if GOOSE_CONFIG.is_file() else {}
    except (OSError, yaml.YAMLError) as exc:
        return f"    could not read Goose's config: {exc}"
    if not isinstance(cfg, dict):
        return "    Goose's config is not a mapping — left alone"

    changed = _set_goose_provider(cfg)
    for key, value in GOOSE_DEFAULTS.items():
        if cfg.get(key) != value:
            cfg[key] = value
            changed.append(f"{key}={value}")
    for name in GOOSE_DISABLE:
        ext = (cfg.get("extensions") or {}).get(name)
        if isinstance(ext, dict) and ext.get("enabled"):
            ext["enabled"] = False
            changed.append(f"disabled `{name}` (shell and file editing)")
    if cfg.get("GOOSE_MOIM_MESSAGE_TEXT") != GOOSE_ORIENTATION:
        cfg["GOOSE_MOIM_MESSAGE_TEXT"] = GOOSE_ORIENTATION
        changed.append("added an orientation message for a first-time user")

    if not changed:
        return "    Goose already had these defaults"
    try:
        if GOOSE_CONFIG.is_file():
            GOOSE_CONFIG.with_suffix(".yaml.dduet-backup").write_text(GOOSE_CONFIG.read_text())
        GOOSE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        GOOSE_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    except (OSError, yaml.YAMLError) as exc:
        return f"    could not write Goose's config: {exc}"
    return "    set safer defaults:\n" + "\n".join(f"      {c}" for c in changed)


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
    # CONFIGURE=false ALWAYS. Its configure step is interactive and its own TTY check is not
    # enough to stop it — see _set_goose_provider for what that cost. We configure it afterwards
    # ourselves, which we have to do regardless because the env vars it accepts are inert in
    # current Goose.
    #
    # Not passing the key also means the owner's credential never enters the environment of a
    # script we downloaded minutes earlier. That was never the reason for the change, but it is
    # a real improvement and worth keeping deliberate.
    env = {**os.environ, "GOOSE_BIN_DIR": str(bin_dir), "CONFIGURE": "false"}
    if GOOSE_VERSION:
        env["GOOSE_VERSION"] = GOOSE_VERSION

    if not apply:
        shown = " ".join(f"{k}={v}" for k, v in sorted(env.items())
                         if k.startswith(("GOOSE_", "CONFIGURE")))
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
    return (
        f"    installed Goose at {where}\n"
        + configure_goose_defaults() + "\n"
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
