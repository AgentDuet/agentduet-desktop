"""Undo what installing this left behind — registrations always, data only when asked.

macOS HAS NO UNINSTALL CONVENTION. The norm is drag-to-Trash with everything under
~/Library left for ever: Dropbox, checked on 2026-09-03, ships no uninstaller at all and leaves
678 MB behind. For litter that is fine, and nobody asks.

It is not fine for what WE register. Two of our leftovers are breakage rather than litter:

  - the login item. Trash the app while "Start at Login" is on and System Settings -> General ->
    Login Items keeps an entry for an app that no longer exists — and nothing can remove it,
    because the only code that could unregister it went into the Trash with the bundle.
  - the MCP registrations. Three assistants are left configured to launch a binary that is gone,
    so each one fails at a tool call and blames whatever it was told to run.

Hence the tiers, which are about kind and not about caution:

  registrations   always removed. This is the breakage.
  models          --models. Re-downloadable, 0.7-5 GB each, pure disk.
  instance        --data only. Knowledge, people, call recordings and transcripts, and .env with
                  the model key and the connector credential. Deleting what the agent learned
                  about its owner is a sentence they type, never a default — the same reasoning
                  as the "never wipe $AGENTDUET_HOME" rule in CLAUDE.md.

KNOWN WART: importing `paths` seeds a missing instance, so running this on a machine that has
none will CREATE one and then report keeping it. Harmless — net zero on disk — and not worth
making instance creation lazy for, but it reads oddly the first time.

ORDER IS LOAD-BEARING: stop the daemon, unregister the login item WHILE THE BUNDLE STILL EXISTS,
then remove the installed copies. Reversing the middle two is how the dangling entry happens.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

from . import hosts, install, loginitem, paths

#: The Swift shell answers this and exits without becoming an application. Only the bundle can
#: unregister its own login item — SMAppService.mainApp means "the caller's app", so asking from
#: this CLI binary would be asking about something with no bundle at all.
UNREGISTER_FLAG = "--unregister-login-item"


def _bytes(p: pathlib.Path) -> int:
    if p.is_symlink():
        return 0
    try:
        if p.is_file():
            return p.stat().st_size
    except OSError:
        return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def speech_caches() -> list[pathlib.Path]:
    """faster-whisper's downloads inside the SHARED Hugging Face cache.

    Globbed for our models by name, never the cache wholesale: ~/.cache/huggingface belongs to
    every tool on the machine that uses the hub, so `rm -rf` there would delete somebody else's
    gigabytes. `models--*faster-whisper*` matches only the speech models this app fetches.
    """
    root = pathlib.Path(os.getenv("HF_HOME") or (pathlib.Path.home() / ".cache/huggingface"))
    hub = root / "hub"
    if not hub.is_dir():
        return []
    return sorted(d for d in hub.glob("models--*faster-whisper*") if d.is_dir())


def app_bundle() -> pathlib.Path | None:
    """The installed .app, if it is where a Mac user would have dragged it."""
    for base in (pathlib.Path("/Applications"), pathlib.Path.home() / "Applications"):
        app = base / "AgentDuet Desktop.app"
        if (app / "Contents/MacOS/AgentDuet Desktop").is_file():
            return app
    return None


def survey() -> list[tuple[str, str, str]]:
    """(tier, what, where and how big) for everything this install can have left behind."""
    rows: list[tuple[str, str, str]] = []
    link = install.installed_path()
    if link.exists() or link.is_symlink():
        rows.append(("registration", "installed command", str(link)))
    if install.VERSIONS_DIR.is_dir():
        rows.append(("registration", "installed payloads",
                     f"{install.VERSIONS_DIR} ({_human(_bytes(install.VERSIONS_DIR))})"))
    if loginitem.MAC_PLIST.is_file():
        rows.append(("registration", "legacy login item", str(loginitem.MAC_PLIST)))
    for label, state in hosts.registration():
        if state == "registered" or state.startswith("registered, but"):
            rows.append(("registration", f"{label} registration", "as `%s`" % hosts.SERVER_NAME))
    weights = paths.HOME / "models"
    if weights.is_dir() and any(weights.iterdir()):
        rows.append(("models", "local model weights", f"{weights} ({_human(_bytes(weights))})"))
    for d in speech_caches():
        rows.append(("models", "speech model", f"{d} ({_human(_bytes(d))})"))
    if paths.HOME.is_dir():
        rows.append(("data", "your instance", f"{paths.HOME} ({_human(_bytes(paths.HOME))})"))
    app = app_bundle()
    if app:
        rows.append(("app", "the app itself", f"{app} — drag it to the Trash yourself"))
    return rows


def _rm(p: pathlib.Path) -> str:
    try:
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            return f"    already gone: {p}"
        return f"    removed {p}"
    except OSError as exc:
        return f"    could NOT remove {p}: {exc}"


def _unregister_login_item(apply: bool) -> list[str]:
    """Both mechanisms, and the bundle one first while it can still answer."""
    out = []
    app = app_bundle()
    if app:
        shell = app / "Contents/MacOS/AgentDuet Desktop"
        if not apply:
            out.append(f"    would run: {shell} {UNREGISTER_FLAG}")
        else:
            try:
                done = subprocess.run([str(shell), UNREGISTER_FLAG],
                                      capture_output=True, text=True, timeout=30)
                said = (done.stdout or done.stderr or "").strip()
                out.append("    " + (said or f"exit {done.returncode}"))
            except (OSError, subprocess.SubprocessError) as exc:
                out.append(f"    could not ask the app to unregister it: {exc}")
    else:
        # SAY SO RATHER THAN SKIP. If the app has already been trashed, the registration may
        # still be live and only System Settings can clear it now.
        out.append("    no installed .app found — if it was already moved to the Trash while "
                   "Start at Login was on, clear it in System Settings -> General -> Login Items")
    if loginitem.MAC_PLIST.is_file():
        out.append("    " + (loginitem.remove_login_item().strip().splitlines()[-1] if apply
                             else f"would remove {loginitem.MAC_PLIST}"))
    return out


def _deregister_assistants(apply: bool) -> list[str]:
    """Remove only what `connect` wrote: Claude Code and Goose. Nothing else is ours to touch."""
    out = []
    claude = hosts.resolve_bin("claude")
    if claude:
        cmd = [claude, "mcp", "remove", hosts.SERVER_NAME, "-s", "user"]
        if not apply:
            out.append("    would run: " + " ".join(cmd))
        else:
            try:
                done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                out.append(f"    Claude Code: {'removed' if done.returncode == 0 else 'nothing to remove'}")
            except (OSError, subprocess.SubprocessError) as exc:
                out.append(f"    Claude Code: could not run claude: {exc}")
    if hosts.GOOSE_CONFIG.is_file():
        try:
            import yaml
            cfg = yaml.safe_load(hosts.GOOSE_CONFIG.read_text()) or {}
            if (cfg.get("extensions") or {}).get(hosts.SERVER_NAME) is None:
                out.append("    Goose: nothing to remove")
            elif not apply:
                out.append(f"    would remove the `{hosts.SERVER_NAME}` extension from {hosts.GOOSE_CONFIG}")
            else:
                del cfg["extensions"][hosts.SERVER_NAME]
                hosts.GOOSE_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))
                out.append(f"    Goose: removed the `{hosts.SERVER_NAME}` extension")
        except (OSError, ValueError, ImportError) as exc:
            out.append(f"    Goose: could not edit {hosts.GOOSE_CONFIG}: {exc}")
    return out


def uninstall(*, models: bool = False, data: bool = False, apply: bool = True) -> str:
    out: list[str] = []
    verb = "" if apply else "would "

    from . import service
    if service.running_pid():
        out.append(f"  {verb}stop the daemon")
        if apply:
            out.append("    " + service.service_stop().strip().splitlines()[-1])

    out.append(f"  {verb}unregister the login item")
    out += _unregister_login_item(apply)

    out.append(f"  {verb}remove the assistant registrations")
    out += _deregister_assistants(apply)

    out.append(f"  {verb}remove the installed command")
    for p in (install.installed_path(), install.VERSIONS_DIR,
              install.DESKTOP_DIR / f"{install.APP_ID}.desktop"):
        if p.exists() or p.is_symlink():
            out.append(_rm(p) if apply else f"    would remove {p}")

    weights = paths.HOME / "models"
    if models or data:
        out.append(f"  {verb}delete downloaded models")
        for p in ([weights] if weights.is_dir() else []) + speech_caches():
            out.append(_rm(p) if apply else f"    would remove {p} ({_human(_bytes(p))})")

    if data:
        out.append(f"  {verb}delete your instance")
        out.append(_rm(paths.HOME) if apply else
                   f"    would remove {paths.HOME} ({_human(_bytes(paths.HOME))})")

    out.append("")
    if not data:
        # THE PART AN OWNER WOULD NOT GUESS. "Keep my data" also keeps the credentials, and this
        # is the only moment anyone is thinking about the directory at all.
        out.append(f"  KEPT: {paths.HOME} ({_human(_bytes(paths.HOME))})")
        out.append("    your knowledge, the people who contacted you, call recordings and")
        out.append("    transcripts — and .env, which holds your model key and connector")
        out.append("    credential. Delete it with `--data`, or remove the folder yourself.")
    if not (models or data) and (weights.is_dir() or speech_caches()):
        kept = _bytes(weights) + sum(_bytes(d) for d in speech_caches())
        out.append(f"  KEPT: downloaded models ({_human(kept)}) — delete with `--models`")
    app = app_bundle()
    if app:
        out.append(f"  LAST STEP: drag {app} to the Trash.")
        out.append("    Do it after this command, never before — only the app itself can")
        out.append("    unregister its login item.")
    return "\n".join(out)
