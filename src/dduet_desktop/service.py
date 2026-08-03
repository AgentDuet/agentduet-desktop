"""Starting, stopping and inspecting the daemon — from anywhere, including when it is dead.

WHY THIS IS SEPARATE FROM tools.py

`tools.py` is the SECRETARY registry: what the agent knows and may do. This is about the
process that runs it. Different power, and worth keeping visibly apart — starting a process
and making it persistent is not the same authority as reading the owner's knowledge and
replying as them.

WHY IT MATTERS MORE THAN IT LOOKS

On 2026-08-03 the daemon stopped and nobody noticed for twelve minutes. The log ends cleanly on
`inbound is live`; the window had been closed. It was found by accident while checking something
unrelated. With no owner interface, an assistant asking these questions is the ONLY way anyone
learns the secretary is off the air — and "stopped" is indistinguishable from "nobody called".

WHY THE DAEMON MUST OUTLIVE ITS CALLER

`service_start` is invoked from an MCP server that the host spawned and will kill when the
session ends. A child of that would die with it. So the daemon is started fully detached, in
its own session, with its output going to a file — not inherited pipes, which would also tie it
to the parent.
"""

import os
import pathlib
import signal
import subprocess
import sys
import time
from datetime import datetime

from . import paths

PIDFILE = paths.RUN / "secretary.pid"
LOGFILE = paths.RUN / "daemon.log"

#: SIGKILL does not exist on Windows — `signal.SIGKILL` raises AttributeError there, so the old
#: stop path crashed rather than escalating. On Windows os.kill() with any non-CTRL signal calls
#: TerminateProcess, which is already the forceful option, so SIGTERM is the right fallback.
FORCE = getattr(signal, "SIGKILL", signal.SIGTERM)

#: How long to wait for a graceful exit before forcing. SIGTERM is caught somewhere in the async
#: stack and does not always exit, so this is a real wait, not a formality.
GRACE_SECONDS = 10


def _alive(pid: int) -> bool:
    """Running, and not a corpse waiting to be reaped.

    `os.kill(pid, 0)` succeeds on a ZOMBIE, which is how `service_stop` came to report "it
    ignored both signals" about a process it had just killed — SIGKILL cannot be ignored. The
    daemon is spawned by the mcp server and stays its child, so between exiting and the parent
    reaping it there is a window where the pid still answers.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    # Linux: state is the third field of /proc/<pid>/stat, after a comm that may contain spaces.
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2] != "Z"
    except (OSError, ValueError, IndexError):
        pass
    # No procfs (macOS): ask ps. If that is unavailable too, fall back to "it answered".
    try:
        out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return not out.stdout.strip().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return True


def running_pid() -> int | None:
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _last_lines(n: int = 3) -> list[str]:
    """The tail of the daemon log — what it was doing when it stopped.

    This is the difference between "it is not running" and "it stopped cleanly at 10:21 having
    just connected", which is the sentence that would have saved twelve minutes.
    """
    try:
        lines = [l for l in LOGFILE.read_text(errors="replace").splitlines() if l.strip()]
    except OSError:
        return []
    # Access-log noise drowns the events worth reading.
    noise = ('"GET /', '"POST /', "HTTP Request:")
    events = [l for l in lines if not any(n in l for n in noise)]
    return events[-n:]


def service_status() -> str:
    """Is the secretary running, and if not, what was it doing when it stopped?"""
    from . import connector, llm

    pid = running_pid()
    out = []
    if pid:
        try:
            started = datetime.fromtimestamp(pathlib.Path(f"/proc/{pid}").stat().st_ctime)
            since = f", up since {started:%H:%M}"
        except (OSError, ValueError):
            since = ""
        out.append(f"  RUNNING — pid {pid}{since}")
    else:
        out.append("  STOPPED — nobody outside can reach this secretary right now.")

    # configured(), not verify(): verify calls the model, and a status check must not spend a
    # token every time an assistant is curious.
    out.append(f"  model      {'attached' if llm.configured() else 'NOT ATTACHED'}")
    out.append(f"  connector  {'configured' if connector.configured() else 'NOT SET'}")

    tail = _last_lines()
    if tail:
        out.append("  last from the log:")
        out += [f"    {l[:150]}" for l in tail]
    if not pid:
        out.append("\n  Start it with service_start, or `dduet-desktop run` in a terminal.")
    return "\n".join(out)


def service_start() -> str:
    """Start the daemon, detached, so it survives whatever started it."""
    if pid := running_pid():
        return f"Already running (pid {pid})."

    from . import connector, llm
    ok, why = llm.verify()          # a real call here IS worth it: starting without one is worse
    if not ok:
        # Starting it would produce a daemon that connects and then cannot answer anyone.
        return (f"Not started: no working model ({why}). Secrets must be set at a terminal — "
                f"run `dduet-desktop init`.")
    if not connector.configured():
        # Not fatal — everything local works — but say it, because a running daemon that never
        # hears from anyone looks identical to one nobody has contacted.
        pass

    paths.RUN.mkdir(parents=True, exist_ok=True)
    log = open(LOGFILE, "a", buffering=1)
    kwargs = {}
    if hasattr(os, "setsid"):
        kwargs["start_new_session"] = True          # POSIX: escape the caller's process group
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    # Reap anything a previous start left behind, so the pid file cannot point at a corpse.
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except (ChildProcessError, OSError, AttributeError):
        pass

    subprocess.Popen(
        [sys.executable, "-m", "dduet_desktop.cli", "run", "--headless"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, close_fds=True, **kwargs)

    for _ in range(20):
        time.sleep(0.5)
        if pid := running_pid():
            note = "" if connector.configured() else \
                   " No connector, so nothing will arrive until one is set."
            return f"Started (pid {pid}).{note}"
    return ("Started, but it has not written a pid within 10s. Check the log:\n" +
            "\n".join(f"  {l[:150]}" for l in _last_lines(5)))


def handover() -> str:
    """Start the INSTALLED daemon detached, then let this process exit.

    WHY THIS EXISTS

    The process an owner launches IS the daemon — shell.py joins the worker thread and stays in
    the foreground. So after the installer copies the binary to ~/.local/bin, the secretary still
    running is the file they downloaded. Tidy up the Downloads folder and it keeps working until
    the next restart, then vanishes with nothing to explain why.

    WHY THE CHILD WAITS

    Both processes want port 8899 and, worse, the same connector — and one client per connector
    is a hard constraint. So the replacement waits for THIS pid to disappear before starting,
    rather than racing it. Passing our pid is deterministic; a sleep is a guess.
    """
    from . import install
    target = install.installed_path()
    if not (target.is_symlink() and target.resolve().is_file()):
        return "Not installed yet, so there is nothing to hand over to."

    paths.RUN.mkdir(parents=True, exist_ok=True)
    log = open(LOGFILE, "a", buffering=1)
    kwargs = {"start_new_session": True} if hasattr(os, "setsid") else {}
    try:
        subprocess.Popen([str(target), "run", "--headless", "--after-pid", str(os.getpid())],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         close_fds=True, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not start the installed copy: {exc}"
    return (f"Handing over to {target}. This page will stop responding in a moment — that is "
            f"the secretary moving into the background.")


def wait_for_exit(pid: int, timeout: float = 30.0) -> None:
    """Block until `pid` is gone. Used by the replacement daemon during a hand-over."""
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.25)


def service_stop() -> str:
    """Stop the daemon. Verified, not assumed.

    A stop that lies leaves two daemons sharing one connector, and the survivor may be running
    older code — so this checks, and escalates rather than reporting success on a signal it
    merely sent.
    """
    pid = running_pid()
    if pid is None:
        return "Not running."
    os.kill(pid, signal.SIGTERM)
    for _ in range(GRACE_SECONDS * 2):
        time.sleep(0.5)
        if not _alive(pid):
            return f"Stopped (pid {pid}). Nobody outside can reach this secretary now."
    os.kill(pid, FORCE)
    time.sleep(1)
    if _alive(pid):
        return f"Could NOT stop pid {pid} — it ignored both signals. Kill it by hand."
    return f"Stopped (pid {pid}) — it ignored SIGTERM and had to be forced."
