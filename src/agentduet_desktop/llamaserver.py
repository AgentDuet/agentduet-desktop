"""Local models in llama.cpp's own `llama-server`, one process per model (2026-10-09).

WHY. `llama-cpp-python`, which every local model used to run in, is one maintainer, and its
releases trail llama.cpp by weeks: EmbeddingGemma 2 reached llama.cpp on 2026-10-06 and no release
could load it. `llama-server` is the llama.cpp team's own, so a model works the day it lands, and
its HTTP API changes far less than the C API the package wraps. Search moved first; the rest
follows in the same shape. The binary is built by `packaging/build-llama-server.sh`.

PRIVATE BY CONSTRUCTION:
  - It listens on a UNIX SOCKET, never a TCP port, in a folder only the owner can open — so no
    other account, and no web page, can reach it. There is no port to rebind or to squat.
  - It also wants a key, read from a 0600 file rather than the command line, where `ps` would
    show it to every account on the machine. A fresh key per start.
  - It is told `--offline` and `--no-webui`: it serves the one file it was given, and nothing else.

IT CANNOT OUTLIVE US. The server runs under a small shell that waits on a pipe from this process.
When the daemon exits — however it exits, SIGKILL included — the pipe closes and the shell stops
the server. Without that, a killed daemon would leave a model holding memory until the next start.

An AI module (edition.AI_MODULES): the recorder builds neither this nor the binary.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

from . import paths

logger = logging.getLogger("secretary")

#: How long a server gets to load its model and answer /health.
START_SECONDS = 60
#: The longest one request may take. Generous: a cold GPU compiles its kernels on the first.
REQUEST_SECONDS = 120

#: The shell between us and the server — see "IT CANNOT OUTLIVE US".
_WATCH = 'trap "" INT; "$0" "$@" & c=$!; read _; kill $c 2>/dev/null; wait $c'


def binary() -> pathlib.Path | None:
    """The `llama-server` to run, or None when this build has none.

    Beside the daemon in a built app (`Contents/MacOS`); in a source checkout, where
    `build-llama-server.sh` puts it. `AGENTDUET_LLAMA_SERVER` overrides both, for a developer.
    """
    here = pathlib.Path(__file__).resolve()
    for c in (os.getenv("AGENTDUET_LLAMA_SERVER", ""),
              pathlib.Path(sys.executable).resolve().parent / "llama-server" if getattr(sys, "frozen", False) else "",
              here.parents[2] / "packaging" / "bin" / "llama-server"):
        if c and pathlib.Path(c).is_file() and os.access(c, os.X_OK):
            return pathlib.Path(c)
    return None


def _private_dir() -> pathlib.Path:
    """A folder only the owner can open, for the sockets and keys.

    In the instance when its path is short enough for a socket — macOS allows 104 bytes — else a
    fresh temporary one, which is private too.
    """
    d = paths.RUN / "engine"
    if len(str(d / "embed.sock")) > 100:
        return pathlib.Path(tempfile.mkdtemp(prefix="agentduet-engine-"))
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


class _Conn(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class Server:
    """One model in one `llama-server`. Started on first use; `stop()` frees its memory."""

    def __init__(self, name: str, model: pathlib.Path, args: list[str]):
        self.name, self.model, self.args = name, model, list(args)
        self._proc: subprocess.Popen | None = None
        self._sock = self._key = ""
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _request(self, method: str, path: str, body: dict | None, timeout: float) -> tuple[int, dict]:
        c = _Conn(self._sock, timeout)
        try:
            c.request(method, path, json.dumps(body) if body is not None else None,
                      {"Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
            r = c.getresponse()
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
        finally:
            c.close()

    def _start(self) -> bool:
        exe = binary()
        if exe is None or not self.model.is_file():
            return False
        d = _private_dir()
        self._sock = str(d / f"{self.name}.sock")
        key = d / f"{self.name}.key"
        pathlib.Path(self._sock).unlink(missing_ok=True)
        key.unlink(missing_ok=True)
        self._key = secrets.token_urlsafe(24)
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self._key + "\n")
        log = open(paths.RUN / f"llama-server-{self.name}.log", "w")
        self._proc = subprocess.Popen(
            ["/bin/sh", "-c", _WATCH, str(exe), "-m", str(self.model), "--host", self._sock,
             "--api-key-file", str(key), "--offline", "--no-webui", *self.args],
            stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        until = time.time() + START_SECONDS
        while time.time() < until:
            if self._proc.poll() is not None:
                break
            try:
                if self._request("GET", "/health", None, 5)[0] == 200:
                    logger.info("%s model loaded (llama-server, pid %d)", self.name, self._proc.pid)
                    return True
            except OSError:
                pass
            time.sleep(0.2)
        logger.warning("%s: llama-server did not come up — see llama-server-%s.log",
                       self.name, self.name)
        self._stop()
        return False

    def _stop(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.stdin.close()                    # the watcher stops the server
            p.wait(timeout=10)
        except Exception:
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass
        pathlib.Path(self._sock).unlink(missing_ok=True)

    def stop(self) -> None:
        with self._lock:
            self._stop()

    def post(self, path: str, body: dict) -> dict | None:
        """One request, starting the server if it is not running. None when it cannot be had.

        A server found dead is started again, once.
        """
        with self._lock:
            for attempt in (1, 2):
                if not self.running() and not self._start():
                    return None
                try:
                    status, got = self._request("POST", path, body, REQUEST_SECONDS)
                except OSError as exc:
                    logger.warning("%s: llama-server request failed (%s)", self.name, exc)
                    self._stop()
                    continue
                if status == 200:
                    return got
                logger.warning("%s: llama-server answered %d: %s", self.name, status,
                               str(got)[:200])
                return None
            return None
