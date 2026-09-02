"""The owner's window — the existing local site, in a native frame.

WHY A WEBVIEW AND NOT WIDGETS

The owner's view is already HTML: a three-column layout with live websocket push and an embedded
canvas. Rebuilding that in Tk or Qt would be a rewrite for no functional gain, and Qt would add
50-100 MB to every platform binary. `pywebview` renders the same page in the OS's own webview
(WebKit on macOS, WebView2 on Windows, GTK/WebKit on Linux) for a few MB.

It also removes the token from view: the site is loopback-only with a per-machine token in the
query string, which is fine but looks like a debug artifact when a person is asked to paste it.

THREADING, WHICH IS NOT OPTIONAL

A GUI event loop must own the main thread — on macOS it is a hard platform requirement, not a
convention. The daemon is asyncio, so the daemon runs on a worker thread and the window on the
main thread. Getting this backwards works on Linux and fails on macOS, which is exactly the kind
of bug that only appears on the reviewer's laptop.

DEGRADING

No pywebview, no system webview, or a headless machine: fall back to the default browser, and if
that fails, print the URL. The site is the primary owner surface now, so it has to come up by
some route on every platform rather than depending on one library being importable.
"""

import logging
import threading
import time
import sys
import webbrowser

from . import paths

logger = logging.getLogger("dduet.shell")

TITLE = "AgentDuet Desktop"


def site_url(timeout: float = 20.0) -> str | None:
    """Wait for the daemon to write its token, then build the owner URL.

    Polls the token file rather than importing `web`: the daemon owns that module, and reading a
    file it has already written is the one signal available from another thread without
    reaching into its state.
    """
    import os

    port = os.getenv("SECRETARY_WEB_PORT", "8899")
    recorded = paths.RUN / "site-url"
    tok = paths.RUN / "web-token"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # What the daemon actually bound wins over what this process would guess.
        if recorded.is_file():
            u = recorded.read_text().strip()
            if u:
                return u
        if tok.is_file():
            t = tok.read_text().strip()
            if t:
                return f"http://127.0.0.1:{port}/?t={t}"
        time.sleep(0.25)
    return None


def open_in_browser(url: str) -> bool:
    try:
        # new=2 asks for a TAB rather than a window; browsers that are closed entirely will
        # start up instead. Either way the person who lost their tab gets it back.
        return webbrowser.open(url, new=2)
    except Exception as exc:                     # a headless box has no browser to open
        logger.warning("could not open a browser (%s)", exc)
        return False


def window_support() -> tuple[bool, str]:
    """Can this build open a native window, and if not why not.

    IMPORTING IS NOT ENOUGH TO ANSWER THIS, and that is the whole reason the function exists.
    `webview` imports cleanly with no GUI backend behind it and only raises from `start()` — so
    the failure lands after the daemon is already up, is caught by design, and reads as a
    browser preference rather than a missing dependency. On macOS the backend is pyobjc, which
    pip installs and PyInstaller collects, so a frozen binary CAN have a real window; on Linux
    the bindings are system libraries that do not bundle, which is why a onefile build there
    falls back for ever with nothing to fix in the app.

    So probe the backend the platform actually uses, and report it on `status` next to the
    providers and the speech engine — the build says what it can do rather than letting the
    owner infer it from what happens.
    """
    try:
        import webview                                            # noqa: F401
    except ImportError:
        return False, "pywebview is not in this build — the owner view opens in your browser"
    if sys.platform == "darwin":
        try:
            import objc, WebKit                                   # noqa: F401
        except ImportError as exc:
            return False, f"pywebview is here but its macOS backend is not ({exc})"
        return True, ""
    if sys.platform.startswith("win"):
        return True, ""                                           # WebView2, part of the OS
    # Linux: GTK/Qt python bindings are system libraries, absent from a onefile build.
    try:
        import gi                                                 # noqa: F401
        return True, ""
    except ImportError:
        return False, "no GUI backend on this machine — the owner view opens in your browser"


def run_with_window(start_daemon, want_window: bool = True) -> int:
    """Run the daemon on a worker thread and show the owner's view.

    `start_daemon` is a callable that blocks — the daemon's own entry point.
    """
    stop = threading.Event()

    def _daemon():
        try:
            start_daemon()
        finally:
            stop.set()                            # the window should not outlive the daemon

    worker = threading.Thread(target=_daemon, name="dduet-daemon", daemon=True)
    worker.start()

    url = site_url()
    if url is None:
        print("  the owner site did not come up — see the log in", paths.RUN / "secretary.log")
        worker.join()
        return 1

    webview = None
    if want_window:
        try:
            import webview                        # noqa: F811  (optional dependency)
        except ImportError:
            logger.info("pywebview not installed — using the default browser")

    if webview is not None:
        # Closing the window ends the run, which is what a person expects of an app window.
        # Background operation is `run --no-window`; a tray icon is the better answer and is
        # not built yet, so the honest behaviour is the predictable one.
        try:
            print(f"  owner view: {url}")
            webview.create_window(TITLE, url, width=1360, height=900, min_size=(900, 600))
            webview.start()
            return 0
        except Exception as exc:
            # The window is OPTIONAL and the site is the primary surface, so a windowing
            # failure must never end the run. Catching only ImportError was not enough: the
            # import succeeds and `start()` raises when no GUI backend is present — which is
            # the normal case for a one-file binary, since GTK/Qt Python bindings are system
            # libraries that do not bundle. Observed as a double-clicked binary that created
            # its instance, bound the site, and then vanished.
            logger.warning("native window unavailable (%s) — falling back to the browser", exc)
            print(f"  (no native window on this machine — opening your browser instead)")

    # OPEN THE BROWSER ON FIRST RUN ONLY. It used to open on every start, from when the page was
    # the only way to configure anything. `init` covers that now, and a daemon that seizes a
    # browser tab every time it restarts is noise — during development it is a tab per restart.
    #
    # Not removed outright, because on macOS and Windows the page IS the documented setup
    # surface: a brand new install that opened nothing would leave the owner with a running
    # process and no way to find it. `setup-done` is the same marker the site uses to decide
    # between the wizard and the hub, so the two agree by construction.
    #
    # The url is printed either way — that is what makes it findable without being taken over.
    # An instance that predates the marker must not be treated as brand new — it would open a
    # tab on every restart forever. A configured connector says setup happened, whenever it was,
    # which is the same fallback the site's needs_setup() uses so the two cannot disagree.
    from . import connector
    first_run = not (paths.RUN / "setup-done").exists() and not connector.configured()
    if first_run and open_in_browser(url):
        print(f"  owner view opened: {url}")
    else:
        print(f"  owner view: {url}")
    worker.join()
    return 0
