"""Frozen-binary entry point.

PyInstaller needs a real script, not a console-script name from pyproject.toml — the entry
points metadata does not exist inside a frozen bundle.
"""

import os
import pathlib
import sys


def _trust_the_bundled_cas() -> None:
    """Point OpenSSL at the CA bundle we ship. WITHOUT THIS THE BINARY CANNOT REACH THE PLATFORM.

    The a9 build connected to nothing: every channel attempt failed with `AuthenticationError:
    SSL/TLS error during connection`, retrying every two minutes forever, while the SAME COMMIT
    run from source connected in under a second. Twenty seconds apart, same connector, same
    network — so it was never the platform.

    `certifi/cacert.pem` is inside the app. Nothing told Python about it. A frozen build carries
    its own OpenSSL, whose compiled-in CA path points at the machine that built it, so
    `ssl.create_default_context()` loads no roots at all and every handshake fails to verify.
    The SDK does not set a context — with no client certificate `create_ssl_context` returns
    None and `websockets` builds the default — so this is ours to fix and there is nowhere else
    it could be fixed.

    Set only when unset, so an operator pointing at a corporate root still wins. Silent when the
    file is missing rather than raising: the owner site and everything local must still come up.

    THE LESSON FOR VERIFYING A RELEASE: booting the daemon proves the imports work, which is what
    a6 taught. It does not prove the channel connects — a9 shipped, signed and notarized, having
    passed that check. A release check has to reach the platform.
    """
    if not getattr(sys, "frozen", False):
        return
    here = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(sys.executable).parent))
    for candidate in (here / "certifi" / "cacert.pem",
                      # --onedir on macOS: the executable sits in Contents/MacOS while the
                      # collected data lands in Contents/Resources and Contents/Frameworks.
                      here.parent / "Resources" / "certifi" / "cacert.pem",
                      here.parent / "Frameworks" / "certifi" / "cacert.pem"):
        if candidate.is_file():
            os.environ.setdefault("SSL_CERT_FILE", str(candidate))
            os.environ.setdefault("REQUESTS_CA_BUNDLE", str(candidate))
            return


def _readable_mime_files() -> None:
    """Keep only the MIME type files this process may open, before anything imports aiohttp.

    IN APPLE'S SANDBOX (the App Store spike, 2026-09-29) the owner site did not start at all.
    `aiohttp` builds its MIME table AT IMPORT, and Python's `mimetypes` reads
    `/etc/apache2/mime.types` among its `knownfiles`: a file the sandbox forbids raises
    PermissionError where a missing one is skipped, so the import itself failed and the daemon
    carried on with no pages. The built-in table covers every type these pages serve.

    By OPENING each file, not `os.access`: the sandbox checks at open, and access() reports the
    Unix permissions, which allow the read.
    """
    import mimetypes

    def opens(path: str) -> bool:
        try:
            with open(path, "rb"):
                return True
        except OSError:
            return False

    mimetypes.knownfiles[:] = [f for f in mimetypes.knownfiles if opens(f)]


_trust_the_bundled_cas()
_readable_mime_files()

from agentduet_desktop.cli import main  # noqa: E402  — both must run before anything is imported

if __name__ == "__main__":
    sys.exit(main())
