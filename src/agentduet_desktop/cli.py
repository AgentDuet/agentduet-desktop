"""The one entry point, so the package works the same on every platform.

The shell scripts it replaces (`start.sh`, `stop.sh`) used `pkill`, `pgrep` and `ss` — fine on
Linux, absent on Windows. Everything they did that mattered is here in Python instead: find the
running daemon, stop it and confirm it actually stopped, refuse to start a second one.

SIGTERM is caught somewhere in the async stack and does not always exit, so `stop` verifies and
escalates rather than reporting success on a signal it merely sent. A stop that lies leaves two
daemons sharing one connector, and the survivor may be running the older code.
"""

import argparse
import os
import signal
import sys
import time

from . import paths

PIDFILE = paths.RUN / "secretary.pid"


# ONE implementation, in service.py. There were two, and only one learned that a zombie
# answers os.kill(pid, 0) — so `run` refused to start against a corpse in the pid file while
# `stop` correctly reported it dead. A duplicated predicate is a predicate that will disagree.
def _running_pid() -> int | None:
    from . import service
    return service.running_pid()


def cmd_init(args) -> int:
    from . import init
    return init.main(interactive=not args.non_interactive)


def cmd_run(args) -> int:
    # A hand-over: the outgoing process still holds port 8899 and the connector, and one client
    # per connector is a hard constraint. Wait for it rather than racing it.
    if getattr(args, "after_pid", 0):
        from . import service
        service.wait_for_exit(args.after_pid)
    if (pid := _running_pid()) and not args.force:
        # Launching it again almost always means the person lost the tab or window, not that
        # they want a second daemon. Show them the one that IS running rather than refusing —
        # a refusal, from a double-clicked icon with no terminal, looks like nothing happened.
        from . import shell
        url = shell.site_url(timeout=2)
        if url and not args.headless:
            shell.open_in_browser(url)
            print(f"  already running (pid {pid}) — opened {url}")
        else:
            print(f"  already running (pid {pid}). Use `agentduet-desktop stop` to stop it.")
        return 0
    if args.no_channel:
        os.environ["SECRETARY_CHANNEL"] = "0"
    paths.migrate()
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    from . import secretary_agent, shell
    try:
        if args.headless:
            # No surface opened at all: for a machine nobody is sitting at.
            return secretary_agent.run() or 0
        # The owner's view is the primary surface, so it is opened for them rather than
        # printed as a URL they have to notice, copy and paste with a token attached.
        return shell.run_with_window(secretary_agent.run, want_window=not args.no_window)
    finally:
        PIDFILE.unlink(missing_ok=True)


def cmd_stop(args) -> int:
    """Delegates to service.py so the CLI and the mcp cannot disagree about what stopping means
    — and so the Windows SIGKILL fix lands in both."""
    from . import service
    out = service.service_stop()
    print("  " + out.replace("\n", "\n  "))
    return 0 if out.startswith(("Stopped", "Not running")) else 1


def cmd_status(args) -> int:
    from . import llm
    pid = _running_pid()
    print(f"  instance : {paths.HOME}")
    print(f"  daemon   : {'running, pid ' + str(pid) if pid else 'stopped'}")
    print(f"  model    : {llm.describe()}")
    caps = paths.CAPABILITIES
    print(f"  config   : settings{'' if paths.SETTINGS.is_file() else ' MISSING'}, "
          f"knowledge {len(list(paths.KNOWLEDGE.glob('*.md'))) if paths.KNOWLEDGE.is_dir() else 0} doc(s), "
          f"capabilities{'' if caps.is_file() else ' MISSING'}")
    print(f"  examples : {paths.EXAMPLES}")

    # WHAT THIS BUILD CAN ACTUALLY DO. In a frozen binary the answer is decided at BUILD time —
    # a provider or the voice adapter that was not installed on the build machine is simply
    # absent, and every import of it is caught and turned into a polite "not available". That
    # is indistinguishable from a configuration problem to whoever is holding it. The first
    # macOS build shipped without Gemini, Anthropic, soxr and numpy: the setup wizard's default
    # model would have failed on screen one, and voice was silently impossible.
    #
    # So the build reports itself, and CI asserts on these lines.
    providers = []
    for label, mod in (("gemini", "google.genai"), ("anthropic", "anthropic"), ("qwen", "httpx")):
        try:
            __import__(mod)
            providers.append(label)
        except ImportError:
            pass
    print(f"  providers: {', '.join(providers) if providers else 'NONE — no model can be attached'}")

    from . import voice
    ok, why = voice.available()
    print(f"  voice    : {'available' if ok else 'NOT available — ' + why}")

    # RUNS A TOOL, does not merely import the runtime. The wasm runtime is a native library
    # loaded through ctypes from a computed path, and `--collect-all` does not bundle it: the
    # build succeeds and dies at the first real call. An import check would pass in exactly that
    # state, so this compiles and executes a one-line tool.
    from . import wasm_host
    try:
        r = wasm_host.run_tool("result({ok: 1 + 1});", {}, lambda k, q: None)
        good = r.get("result", {}).get("ok") == 2
        print(f"  tools    : {'available' if good else 'NOT available — ' + str(r)[:60]}")
    except Exception as exc:
        print(f"  tools    : NOT available — {type(exc).__name__}: {str(exc)[:70]}")

    # WHETHER ANYONE CAN ACTUALLY DRIVE IT. With no owner interface the assistant IS the only
    # surface, so an unregistered assistant means the product is unreachable — and this command
    # used to report a perfectly healthy secretary in exactly that state.
    from . import hosts
    reg = hosts.registration()
    if not reg:
        print("  assistant: NONE found — nothing can drive this secretary")
    for label, state in reg:
        print(f"  assistant: {label} — {state}")
    return 0


def cmd_install(args) -> int:
    """The setup page is the normal way in; this is for headless machines and for testing."""
    from . import install
    if args.rollback:
        print("  " + install.rollback(args.rollback).replace("\n", "\n  "))
        return 0
    if args.list:
        cur = install.current_version()
        for v in install.installed_versions():
            print(f"  {'*' if v == cur else ' '} {v}")
        return 0
    print("  " + install.install().replace("\n", "\n  "))
    return 0


def cmd_mcp(args) -> int:
    """BE the MCP server, on stdin/stdout.

    Exists so the command an assistant is configured with is stable: `<binary> mcp`. The dev
    incantation `python -m agentduet_desktop.secretary_mcp` cannot be registered on an installed
    machine — there is no python and no module path, and in a frozen binary sys.executable IS
    the binary. Anything registered without this points at a venv that only exists here.
    """
    from . import secretary_mcp
    secretary_mcp.mcp.run()
    return 0


def cmd_tools(args) -> int:
    """Approving a tool is deliberately HERE and not in the mcp registry.

    The owner drives this product through an assistant, so if approval were a registry entry the
    assistant could switch on a tool — and it reads text written by strangers. This is the one
    step that must be a command a person types.
    """
    from . import toolstore
    if args.action == "list":
        on, waiting = toolstore.active(), toolstore.pending()
        print(f"  active   : {', '.join(on) if on else 'none'}")
        print(f"  proposed : {', '.join(waiting) if waiting else 'none'}")
        for n in waiting:
            print(f"      {toolstore.PENDING / (n + '.js')}")
        return 0
    if not args.name:
        print(f"  which tool? `agentduet-desktop tools {args.action} <name>`")
        return 1
    if args.action == "approve":
        print("  " + toolstore.approve(args.name))
    elif args.action == "show":
        p = toolstore.PENDING / f"{args.name}.js"
        p = p if p.is_file() else toolstore.ACTIVE / f"{args.name}.js"
        print(p.read_text() if p.is_file() else f"  no tool called {args.name!r}")
    else:
        print("  " + toolstore.remove(args.name))
    return 0


def cmd_connect(args) -> int:
    from . import hosts
    print(hosts.connect(apply=not args.show, install=args.install))
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import version_string
    p = argparse.ArgumentParser(prog="agentduet-desktop", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=version_string())
    sub = p.add_subparsers(dest="cmd", required=False)

    i = sub.add_parser("init", help="set up this machine (interview)")
    i.add_argument("--non-interactive", action="store_true",
                   help="create the instance only; ask nothing")
    i.set_defaults(fn=cmd_init)

    r = sub.add_parser("run", help="start the daemon in the foreground")
    r.add_argument("--no-channel", action="store_true",
                   help="owner site only; do not connect to DDUET (one client per connector)")
    r.add_argument("--force", action="store_true", help="start even if one appears to be running")
    r.add_argument("--no-window", action="store_true",
                   help="open the owner view in your browser instead of an app window")
    r.add_argument("--after-pid", type=int, default=0,
                   help="wait for this pid to exit first (used when handing over after install)")
    r.add_argument("--headless", action="store_true",
                   help="open nothing; just run (for a server or a machine nobody is at)")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("stop", help="stop the daemon, and verify it stopped")
    s.set_defaults(fn=cmd_stop)

    st = sub.add_parser("status", help="what is installed, attached and running")
    st.set_defaults(fn=cmd_status)

    ins = sub.add_parser("install", help="install this binary, or roll back to an older one")
    ins.add_argument("--list", action="store_true", help="show installed versions")
    ins.add_argument("--rollback", metavar="VERSION", default="",
                     help="point the command back at an older installed version")
    ins.set_defaults(fn=cmd_install)

    m = sub.add_parser("mcp", help="run as an MCP server on stdio (what an assistant launches)")
    m.set_defaults(fn=cmd_mcp)

    t = sub.add_parser("tools", help="see and approve the tools your assistant has written")
    t.add_argument("action", choices=["list", "show", "approve", "remove"])
    t.add_argument("name", nargs="?", default="")
    t.set_defaults(fn=cmd_tools)

    c = sub.add_parser("connect", help="register this secretary with the AI assistants you have")
    c.add_argument("--show", action="store_true",
                   help="print what would be done, change nothing")
    c.add_argument("--install", default="", choices=["", "goose"],
                   help="install an assistant first (currently: goose)")
    c.set_defaults(fn=cmd_connect)

    # Double-clicked from a file manager there are no arguments and often no terminal, so a
    # usage message would be a window that flashes and vanishes. No arguments therefore means
    # the thing a person wants: start, and open the owner's view.
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["run"]
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
