"""The RECORDER edition runs with no AI in it — proven by deleting the AI and running it.

WHY THIS SHAPE. The recorder binary is the package with every module in `edition.AI_MODULES` left
out (see `edition.py` and the PyInstaller spec). So the honest test is not to inspect imports and
reason about them; it is to build that same tree — a copy of the package with those files
DELETED, and the AI libraries made unimportable — and run the daemon from it. A stray import of an
AI module anywhere on the recorder's path then fails here exactly as it would in the binary.

THREE CHECKS:
  1. Every core module imports from the stripped tree.
  2. The daemon boots from it (site only, no connector) and binds.
  3. Every route the recorder's UI calls answers without a server error, and no AI route exists.

Run:  python3 tests/test_recorder.py      (needs the build venv's aiohttp and SDK, not a model)
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "agentduet_desktop"
sys.path.insert(0, str(ROOT / "src"))
from agentduet_desktop import edition  # noqa: E402  (the list of what to delete)

#: What the recorder's UI calls (GET unless a body is given). The full list a native shell and
#: the frozen HTML pages reach on the recorder path; an AI route here would be a bug in the list.
ROUTES = [
    ("/api/state", None), ("/api/panel", None), ("/api/threads", None),
    ("/api/setup/current", None), ("/api/permissions", None), ("/api/about", None),
    ("/api/ui", None), ("/api/install", None), ("/api/connector/signin", None),
    ("/api/setup/setting", {"field": "name", "value": "Pat"}),
    ("/api/seen", {"who": "+6590000000"}),
    ("/api/name", {"who": "+6590000000", "name": "Sam"}),
    ("/app.css", None), ("/logo.png", None),
]

#: Routes that belong to the AI half. In the recorder they must not exist at all (404), not
#: merely refuse.
AI_ROUTES = ["/api/chat_history", "/api/models", "/api/proposals", "/api/setup/decider",
             "/api/setup/stt", "/api/people", "/api/canvas/available", "/api/pending",
             "/secretary", "/api/setup/questions"]

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")
        if detail:
            print("        " + str(detail).replace("\n", "\n        "))


#: Run first in the child: AI libraries refuse to import, as they are absent from the binary.
BLOCKER = f"""
import importlib.abc, sys
_AI = {list(edition.AI_LIBRARIES)!r}
class _Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == m or name.startswith(m + ".") for m in _AI):
            raise ModuleNotFoundError(f"No module named {{name!r}} (absent from the recorder)")
        return None
sys.meta_path.insert(0, _Absent())
"""


def stripped_tree(at: pathlib.Path) -> pathlib.Path:
    dest = at / "agentduet_desktop"
    shutil.copytree(PKG, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for name in edition.AI_MODULES:
        (dest / f"{name}.py").unlink(missing_ok=True)
    # AND THE DATA the build leaves out (edition.AI_DATA), so a page or template the core still
    # reaches for fails here as it would in the bundle.
    for pattern in edition.AI_DATA:
        for f in sorted(dest.glob(pattern), reverse=True):
            shutil.rmtree(f) if f.is_dir() else f.unlink()
    for d in sorted((p for p in dest.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    (dest / "_edition.py").write_text('NAME = "recorder"\n')
    (at / "_absent.py").write_text(BLOCKER)
    return at


def env(tree: pathlib.Path, home: pathlib.Path, **extra) -> dict:
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")}
    return {**keep, "PYTHONPATH": str(tree), "HOME": str(home.parent),
            "AGENTDUET_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1", **extra}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(base, token, path, body=None, limit=400):
    url = f"{base}{path}{'&' if '?' in path else '?'}t={token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json",
                                          "X-Token": token, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()[:limit]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:limit]


#: Run in the stripped tree: two legs of one call, then the recorder's merge. No transcript is
#: written and none is waited for.
MERGE_CHECK = """
import _absent, wave
from agentduet_desktop import calls, carry, merge
carry.legs().mkdir(parents=True, exist_ok=True)
stem = "20261003T100000-cR"
calls.record("cR", "+6591234567", "carried", outgoing=True, started=1759456800.0)
for leg, start in (("caller", "100.0"), ("callee", "100.5")):
    w = carry.legs() / f"{stem}-{leg}.wav"
    with wave.open(str(w), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(carry.SAMPLE_RATE)
        f.writeframes(b"\\x01\\x00" * carry.SAMPLE_RATE)
    w.with_suffix(".start").write_text(start)
part = carry.legs() / f"{stem}-callee.wav.part"
part.write_text("")
assert merge.once() == 0, "merged while a leg was still being written"
part.unlink()
assert merge.once() == 1
out = carry.merged_wav(stem)
with wave.open(str(out), "rb") as f:
    assert f.getnchannels() == 2, f.getnchannels()
    assert f.getnframes() == int(carry.SAMPLE_RATE * 1.5), f.getnframes()
text = carry.merged_txt(stem).read_text()
head, body = carry.split_txt(text)
assert head.startswith("Call: +6591234567") and "Direction: outgoing" in head, text
assert "Length: 0:02" in head and f"Recording: {stem}.wav" in head, text
assert body == "", "a transcript appeared in the recorder: " + body
assert not list(carry.recordings().glob(".*.part")), "a half-written file was left"
assert carry.transcript_of([f"{stem}.wav"], carry.recordings()) == ""
assert merge.once() == 0, "merged twice"
print("ok")
"""


def boot(tree: pathlib.Path, home: pathlib.Path, log: pathlib.Path, *, recorder: bool) -> None:
    """Start the daemon from `tree` (site only), and check what it serves."""
    port = free_port()
    pre = "import _absent\n" if recorder else ""
    extra = {} if recorder else {"AGENTDUET_EDITION": ""}
    with log.open("w") as f:
        proc = subprocess.Popen(
            [sys.executable, "-c",
             pre + "import sys\nfrom agentduet_desktop import cli\n"
             "sys.exit(cli.main(['run', '--headless', '--no-channel', '--force']))"],
            stdout=f, stderr=subprocess.STDOUT,
            env=env(tree, home, SECRETARY_CHANNEL="0", SECRETARY_WEB_PORT=str(port), **extra))
    try:
        token_file = home / "run" / "web-token"
        deadline = time.time() + 60
        up = False
        while time.time() < deadline and proc.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    up = token_file.exists()
            except OSError:
                pass
            if up:
                break
            time.sleep(0.3)
        check("daemon binds", up, log.read_text()[-1500:])
        if up:
            token = token_file.read_text().strip()
            base = f"http://127.0.0.1:{port}"
            for path, body in ROUTES:
                code, text = request(base, token, path, body)
                check(f"{'POST' if body else 'GET '} {path} → {code}", code < 500,
                      text.decode(errors="replace"))
            code, text = request(base, token, "/api/panel", limit=None)
            panel = json.loads(text or b"{}") if code == 200 else {}
            want = "recorder" if recorder else "full"
            check(f"the panel says {want}", panel.get("edition") == want, panel.get("edition"))
            check("and carries the AI fields only in the full edition",
                  ("model" in panel) != recorder, sorted(panel))
            for path in AI_ROUTES:
                code, _ = request(base, token, path)
                if recorder:
                    check(f"no {path} (got {code})", code == 404)
                else:
                    check(f"{path} is served (got {code})", code != 404 and code < 500)
        time.sleep(0.5)
        text = log.read_text()
        bad = [l for l in text.splitlines()
               if "Traceback" in l or "ModuleNotFoundError" in l or "absent from the recorder" in l]
        check("no traceback in the daemon's log", not bad, "\n".join(bad[:8]) + "\n" + text[-2500:])
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix="recorder-edition-"))
    tree = stripped_tree(work / "tree")
    home = work / "user" / "home"
    home.parent.mkdir(parents=True)

    print("\n-- the edition is the recorder, and the AI is gone --")
    out = subprocess.run([sys.executable, "-c",
                          "import _absent\nfrom agentduet_desktop import edition\n"
                          "print(edition.name(), edition.ai())"],
                         capture_output=True, text=True, env=env(tree, home))
    check("edition reads recorder", out.stdout.strip() == "recorder False", out.stderr[-400:])
    left = [m for m in edition.AI_MODULES if (tree / "agentduet_desktop" / f"{m}.py").exists()]
    check("no AI module in the tree", not left, left)

    print("\n-- a new instance is seeded with the recorder's own settings --")
    out = subprocess.run([sys.executable, "-c",
                          "import _absent\nfrom agentduet_desktop import owner, paths\n"
                          "print(paths.SETTINGS.read_text())\nprint('CALLS=' + owner.calls())\n"
                          "print('KNOWLEDGE=' + str(paths.KNOWLEDGE.exists()))"],
                         capture_output=True, text=True, env=env(tree, work / "user" / "seed"))
    text = out.stdout
    check("seeded, carrying", "CALLS=carry" in text, (out.stdout + out.stderr)[-600:])
    check("with nothing of an agent, a model or a transcript in it",
          not re.search(r"\b(agent|model|transcri\w*|whisper|assistant|AI)\b", text, re.I),
          text[:800])
    check("and no knowledge folder", "KNOWLEDGE=False" in text, text[-200:])
    out = subprocess.run([sys.executable, "-c", "from agentduet_desktop import edition\n"
                          "print(edition.name())"], capture_output=True, text=True,
                         env=env(tree, home, AGENTDUET_EDITION="full"))
    check("and the environment cannot talk a recorder build out of it",
          out.stdout.strip() == "recorder", out.stdout + out.stderr[-300:])

    print("\n-- every core module imports without the AI --")
    core = sorted(p.stem for p in (tree / "agentduet_desktop").glob("*.py")
                  if p.stem not in ("__main__", "_absent"))
    for m in core:
        out = subprocess.run([sys.executable, "-c", f"import _absent\nimport agentduet_desktop.{m}"],
                             capture_output=True, text=True, env=env(tree, home))
        check(f"import {m}", out.returncode == 0, (out.stderr.strip().splitlines() or [""])[-1])

    print("\n-- a call's two legs merge into one file, with no speech engine --")
    out = subprocess.run([sys.executable, "-c", MERGE_CHECK], capture_output=True, text=True,
                         env=env(tree, work / "user" / "merge"))
    check("merged once, both sides aligned, the call's details and no transcript",
          out.stdout.strip() == "ok",
          (out.stdout + out.stderr)[-800:])

    print("\n-- the recorder daemon boots, and its routes answer --")
    boot(tree, home, work / "recorder.log", recorder=True)

    print("\n-- and the FULL edition still serves both halves --")
    full_home = work / "user" / "full"
    boot(ROOT / "src", full_home, work / "full.log", recorder=False)

    print(f"\n  {passed} passed, {failed} failed\n")
    if not failed:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"  (left for a look: {work})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
