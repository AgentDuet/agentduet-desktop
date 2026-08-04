"""Register this secretary with whatever AI assistant the owner already has.

WHY THIS IS THE INSTALLER'S REAL JOB

With no owner interface, the assistant IS the owner's surface. So the step between "installed"
and "usable" is one line of MCP configuration — and it is exactly the step that loses people,
because it is a path, a module name and a flag they have no way to guess.

WHY WE DO OFFER TO INSTALL ONE (reversed 2026-08-03)

This said "considered and rejected", and the product shipped the opposite. The objection was
that it picks a winner, makes us a distributor of someone else's CVEs, and doubles the download.
What changed is the weight, not the objection: with no owner interface the assistant IS the only
way to drive this product, so "bring your own" is a dead end for anyone who has never installed
one. Detection still wins by default; Goose is offered only as an alternative the owner picks.
Nothing is bundled, so the download is unchanged. It comes from their PREBUILT RELEASE, never
from git — Goose from source means a Rust toolchain and minutes of compilation.

AND WHY WE OFFER TO LAUNCH IT

Installing an assistant the owner has never used and then leaving them at a browser page is
where the flow stopped. They came for a secretary; being handed a configured tool with no way
to open it is not a finished install. See `launch_assistant`.

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

#: Where assistants land when they install per-user, in PATH order. Checked IN ADDITION to PATH
#: because PATH is not reliable here: a double-clicked app inherits the desktop session's
#: environment, not the shell's, and `~/.local/bin` is frequently missing from it. That is not
#: hypothetical — it is the reported bug. Launched from the applications menu, `which("claude")`
#: returned None and step 4 said "None found" about a Claude Code that was plainly installed.
#:
#: It breaks REGISTRATION as well as detection, which is the worse half: `claude mcp add` is run
#: by name, so the same missing PATH turns a working install into a silent failure. Hence this
#: resolves to an ABSOLUTE path that both callers use.
#:
#: `~/.local/bin` is first because it is where Claude Code's own installer puts its symlink, and
#: where we put Goose.
EXTRA_BINS = [
    HOME / ".local/bin",
    HOME / ".claude/local",          # older npm-local Claude Code installs
    pathlib.Path("/usr/local/bin"),
    pathlib.Path("/opt/homebrew/bin"),
    pathlib.Path("/opt/local/bin"),
]


def resolve_bin(name: str) -> str | None:
    """The absolute path of an assistant's binary, PATH or not."""
    if found := shutil.which(name):
        return found
    for d in EXTRA_BINS:
        candidate = d / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None

#: Goose DESKTOP, if the owner has it. We never install this one: on Linux it ships only as
#: deb/rpm (no AppImage), both of which need root, and nothing else in this product does. macOS
#: and Windows ship it as a zip, so it is reachable there — hence looking on every OS. Defined
#: before KNOWN because detection reads it; a lambda would defer that, but relying on definition
#: order being irrelevant is how a later edit breaks it.
GOOSE_DESKTOP = [
    pathlib.Path("/Applications/Goose.app"),
    pathlib.Path.home() / "Applications/Goose.app",
    pathlib.Path("/usr/share/applications/goose.desktop"),
    pathlib.Path("/opt/goose/goose"),
    pathlib.Path.home() / ".local/share/applications/goose.desktop",
]

#: (label, how to detect it, where its config lives — for the message only)
#:
#: Detection needs REAL evidence: a binary on PATH, or a config file the host actually writes.
#: A directory alone is not enough — ~/.cursor survived on this machine from March with no
#: cursor binary and no mcp.json, so directory-existence reported an assistant that was not
#: there. Telling an owner to configure software they do not have is worse than saying nothing:
#: they cannot act on it, and it makes the rest of the output look untrustworthy.
KNOWN = [
    ("Claude Code", lambda: resolve_bin("claude") is not None, "~/.claude.json"),
    # Goose is a RUNNABLE, not a config file. It used to count `~/.config/goose/config.yaml`,
    # which was meant to catch a Desktop install that never put `goose` on PATH — but a config
    # outlives an uninstall, so removing the binary left detection reporting an assistant that
    # could not be opened, while `launch_assistant` correctly found nothing. Desktop is now
    # looked for as an APP, which covers the original case honestly and cannot go stale.
    ("Goose", lambda: resolve_bin("goose") is not None
     or any(p.exists() for p in GOOSE_DESKTOP), "~/.config/goose/config.yaml"),
    ("Cursor", lambda: resolve_bin("cursor") is not None
     or (HOME / ".cursor/mcp.json").is_file(), "~/.cursor/mcp.json"),
    ("Claude Desktop",
     lambda: (HOME / ".config/Claude/claude_desktop_config.json").is_file()
     or (HOME / "Library/Application Support/Claude/claude_desktop_config.json").is_file(),
     "claude_desktop_config.json"),
]


def detect() -> list[str]:
    """Which assistants appear to be installed."""
    return [label for label, present, _ in KNOWN if _safe(present)]


def registration() -> list[tuple[str, str]]:
    """(assistant, state) for each one installed. State is what to DO, not just what is.

    WHY THIS EXISTS

    An assistant installed but not registered is invisible. `status` listed the model, the
    knowledge, the providers and the voice adapter, and said nothing about whether any assistant
    could actually reach the secretary — so a Goose Desktop with no `dduet` extension looked
    identical to a working one until the owner opened Goose and found no tools. That is the whole
    product silently unreachable, reported as healthy.

    It also checks the PATH the assistant recorded, not just that an entry exists. A registration
    pointing at a binary that has since moved — a download folder, an old versioned payload — is
    worse than none: the host tries, fails, and blames whatever it was told to launch.
    """
    # The INSTALLED command when one exists, not this process's own launch_command(). Run from
    # source, launch_command() returns the dev incantation (`python -m ...`), so comparing
    # against it flagged a correctly-registered assistant as pointing somewhere wrong. The
    # question is "can the host reach the secretary", not "did this particular process register
    # it" — a false alarm here sends the owner to re-run `connect` against nothing.
    from . import install
    link = install.installed_path()
    want = ([str(link), "mcp"] if link.is_symlink() and link.resolve().is_file()
            else launch_command())
    out = []
    for label in detect():
        try:
            if label == "Claude Code":
                cfg = json.loads((HOME / ".claude.json").read_text())
                entry = (cfg.get("mcpServers") or {}).get(SERVER_NAME)
                got = [entry["command"], *entry.get("args", [])] if entry else None
            elif label == "Goose":
                import yaml
                cfg = yaml.safe_load(GOOSE_CONFIG.read_text()) or {}
                entry = (cfg.get("extensions") or {}).get(SERVER_NAME)
                got = [entry["cmd"], *entry.get("args", [])] if entry else None
            else:
                # We never write these, so we cannot claim anything about them.
                out.append((label, "not configured automatically — see `connect`"))
                continue
        except (OSError, ValueError, KeyError, ImportError, Exception):
            out.append((label, "could not read its config"))
            continue
        if got is None:
            out.append((label, "NOT registered — run `dduet-desktop connect`"))
        elif got != want:
            out.append((label, f"registered, but pointing at {got[0]} — run `connect` to fix"))
        else:
            out.append((label, "registered"))
    return out


def _safe(check) -> bool:
    try:
        return bool(check())
    except OSError:
        return False


def _register_claude_code(apply: bool) -> str:
    # The ABSOLUTE path, not the bare name. Invoking "claude" needs it on PATH, and a
    # double-clicked app often has a PATH without ~/.local/bin — which turned registration into
    # "could not run `claude`" on a machine where Claude Code was installed and working.
    claude = resolve_bin("claude")
    if claude is None:
        return "    could not find the `claude` command"
    cmd = [claude, "mcp", "add", SERVER_NAME, "-s", "user", "--", *launch_command()]
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
            subprocess.run([claude, "mcp", "remove", SERVER_NAME, "-s", "user"],
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
    # THE ORIENTATION BELONGS HERE, not with the other defaults.
    #
    # It tells the model to "use the `dduet` tools", and it was written by
    # configure_goose_defaults — a separate step that runs BEFORE this one. Get an install where
    # defaults are applied and registration is not, and Goose opens instructing its model to use
    # tools that do not exist. That happened on this machine: 16 extensions, no `dduet`, and an
    # orientation confidently naming it. A missing extension is a gap; one the model has been
    # told to use is a lie it will act on.
    #
    # Written in the same write as the extension, so the claim and the thing it claims cannot
    # come apart.
    if cfg.get("GOOSE_MOIM_MESSAGE_TEXT") != GOOSE_ORIENTATION:
        cfg["GOOSE_MOIM_MESSAGE_TEXT"] = GOOSE_ORIENTATION
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
    # NOT the orientation. It names the `dduet` tools, so it is written by _register_goose in the
    # same write that registers them — see the comment there.

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


#: Terminal emulators and how each takes a command. `x-terminal-emulator` is the Debian
#: alternatives symlink and points at whatever the owner actually uses, so it goes first.
TERMINALS = [
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("foot", []),
    ("tilix", ["-e"]),
    ("xterm", ["-e"]),
]


def launch_assistant() -> str:
    """Open the owner's assistant, so the install ends somewhere usable.

    WHY THIS EXISTS

    Step 4 installed Goose, configured it, and registered the secretary — then left the owner on
    a web page with nothing to click. For someone whose first AI assistant this is, "now open a
    terminal and type goose" is where the install ends in practice.

    ORDER MATTERS: Desktop before the CLI. A graphical app is the right thing to hand someone
    who did not come here for a terminal. We only ever install the CLI, so Desktop turns up only
    if they already had it — but when it is there it is the better answer.

    WHY THE TERMINAL IS SPAWNED WITH A SHELL AFTER IT

    `goose session` in a bare `-e` window vanishes the instant anything goes wrong, taking the
    error with it. Keeping a shell alive afterwards means a failure is readable instead of a
    window that blinked.
    """
    for app in GOOSE_DESKTOP:
        if not app.exists():
            continue
        try:
            if app.suffix == ".app":
                subprocess.Popen(["open", "-a", str(app)], start_new_session=True)
            elif app.suffix == ".desktop":
                subprocess.Popen(["gtk-launch", app.stem], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen([str(app)], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Could not open {app.name}: {exc}"
        return f"Opening {app.name}. The secretary's tools appear once it has started."

    goose = resolve_bin("goose")
    if not goose:
        return ("No assistant found to open. Install one at step 4, or start yours the way you "
                "normally do — the secretary is registered either way.")

    if sys.platform == "darwin":
        # Terminal.app takes a script, not argv, so this is the one place a string is right.
        try:
            subprocess.Popen(["open", "-a", "Terminal", goose], start_new_session=True)
            return "Opening Goose in Terminal."
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Could not open Terminal: {exc}"

    inner = f"{goose} session; echo; echo '[goose exited — press enter to close]'; read"
    for name, flag in TERMINALS:
        if not shutil.which(name):
            continue
        try:
            subprocess.Popen([name, *flag, "bash", "-lc", inner], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        return (f"Opening Goose in {name}. Say hello, then ask it "
                f'"what is waiting for me?"')
    return (f"No terminal program found to open it in. Run this yourself:\n    {goose} session")


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

#: Goose DESKTOP release assets, by machine. Verified against the real release: the zip contains
#: `Goose.app` at the top level and is code-signed (`Contents/_CodeSignature`).
GOOSE_DESKTOP_ASSET = {
    "arm64": "Goose.zip",              # Apple Silicon
    "x86_64": "Goose_intel_mac.zip",   # Intel
}
GOOSE_DESKTOP_URL = "https://github.com/block/goose/releases/latest/download/"


def _mac_apps_dir() -> pathlib.Path:
    """Where an app can be written without a password.

    /Applications is group-writable by admin, which the default macOS user is — so no sudo. A
    non-admin account falls back to ~/Applications, which macOS treats as a real applications
    folder and which `open -a` and our own detection both already look in.
    """
    system = pathlib.Path("/Applications")
    if os.access(system, os.W_OK):
        return system
    user = HOME / "Applications"
    user.mkdir(parents=True, exist_ok=True)
    return user


def install_goose_desktop(apply: bool = True) -> str:
    """Install Goose DESKTOP on macOS: a GUI, and no root.

    WHY DESKTOP HERE AND THE CLI ON LINUX

    A terminal is not a deliverable for someone who came for a secretary and has never used an AI
    assistant. On macOS Desktop is a zip into /Applications — no password, no package manager —
    so there is no reason to hand them a CLI. On Linux the same app ships only as deb/rpm, both
    of which need root, and nothing else in this product does; desktop-Linux users are also the
    ones for whom a terminal is fine.

    WHY `ditto` AND NOT zipfile

    Python's zipfile drops the executable bit and turns symlinks into regular files. Both are
    fatal to an .app: `Contents/MacOS/Goose` must stay executable, and the framework layout is
    symlinks. `ditto -x -k` is Apple's own tool for this, present on every Mac.

    WHY THIS AVOIDS THE GATEKEEPER PROMPT

    `com.apple.quarantine` is applied by the application that DOWNLOADS a file — a browser. A
    programmatic fetch does not set it, so the extracted app opens without the "unidentified
    developer" refusal and without teaching the owner to right-click past a security warning.
    That is a side effect worth having deliberately, not a trick: the app is signed either way.
    """
    import platform
    machine = platform.machine()
    asset = GOOSE_DESKTOP_ASSET.get("arm64" if machine in ("arm64", "aarch64") else "x86_64")
    url = GOOSE_DESKTOP_URL + asset
    dest = _mac_apps_dir()
    app = dest / "Goose.app"

    if apply and app.exists():
        # Not an error, and not a reinstall: an app they already have is theirs. Configure it and
        # move on, which is what the owner actually wants from this button.
        return (f"    Goose Desktop is already at {app}\n" + configure_goose_defaults())

    if not apply:
        return (f"    would download {url}\n"
                f"    and extract Goose.app into {dest} with `ditto`")

    import tempfile
    import urllib.request
    tmp = pathlib.Path(tempfile.gettempdir()) / asset
    try:
        # Streamed, not read() — this is ~200 MB and holding it in memory is pointless.
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, length=1024 * 256)
    except Exception as exc:
        return f"    could not download Goose Desktop: {exc}"

    try:
        done = subprocess.run(["ditto", "-x", "-k", str(tmp), str(dest)],
                              capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"    could not extract Goose Desktop: {exc}"
    finally:
        tmp.unlink(missing_ok=True)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-2:]
        return "    ditto failed:\n" + "\n".join(f"      {l}" for l in tail)
    if not app.exists():
        return f"    extracted, but {app} is not there."

    return (f"    installed Goose Desktop at {app}\n"
            + configure_goose_defaults() + "\n"
            + "    Registering this secretary with it now.")


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
    # macOS gets the GUI. See install_goose_desktop for why the platforms differ.
    if sys.platform == "darwin":
        return install_goose_desktop(apply)

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
