"""AGENTDUET AI: it reads the recorder's calls from the folder, and writes only their transcripts.

    python3 tests/test_ai_edition.py      (the build venv's packages; no model, no line)

What must hold (see edition.py and ingest.py):
  1. The edition is AgentDuet AI: its own instance and port, no phone line.
  2. Only the RECORDER'S calls are taken — a recording with its header'd `.txt` — and each is
     filed once, by the number, direction and time its header gives.
  3. A call not yet transcribed is split into its two sides for the transcription queue, and the
     transcript lands BELOW the header; the recording itself is never written.
  4. The daemon runs with no connector at all, serves the AI half, and starts in setup.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

WORK = pathlib.Path(tempfile.mkdtemp(prefix="ai-edition-"))
os.environ["AGENTDUET_EDITION"] = "ai"
os.environ["AGENTDUET_HOME"] = str(WORK / "home")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import unittest.mock as mock                                            # noqa: E402

from agentduet_desktop import calls, carry, edition, ingest, merge, paths, transcribe  # noqa: E402

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


def stereo(path: pathlib.Path, secs: float = 2.0, rate: int = 24000) -> None:
    """Caller at 440 Hz on the left, the owner at 880 Hz on the right."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<hh", int(9000 * math.sin(2 * math.pi * 440 * i / rate)),
                        int(9000 * math.sin(2 * math.pi * 880 * i / rate)))
            for i in range(int(rate * secs))))


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


print("\n-- the edition --")
check("it is AgentDuet AI", edition.name() == edition.AI_ONLY)
check("with the AI half and no phone line", edition.ai() and not edition.calls())
check("its own instance", paths.HOME.name == "home" and edition.home_name() == ".agentduet-ai")
check("and any free port, so it never clashes with the recorder", edition.port() == 0)

FOLDER = WORK / "AgentDuet"
with mock.patch.object(carry, "recordings", lambda: FOLDER):
    # ---- what the recorder leaves --------------------------------------------------------
    rec = "20261003T140512-cH"
    stereo(FOLDER / f"{rec}.wav")
    (FOLDER / f"{rec}.txt").write_text(
        "Call: Cen Lee (+6598554074)\nDirection: incoming\nStarted: 2026-10-03 14:05:12\n"
        f"Length: 0:02\nRecording: {rec}.wav\n\n")
    done = "20261003T150000-cD"                    # already transcribed (say, a second Mac)
    stereo(FOLDER / f"{done}.wav")
    (FOLDER / f"{done}.txt").write_text(
        "Call: +6591112222\nDirection: outgoing\nStarted: 2026-10-03 15:00:00\nLength: 0:02\n"
        f"Recording: {done}.wav\n\nthem: hello\nyou: hi\n")
    # ---- what is not the recorder's ----------------------------------------------------
    stereo(FOLDER / "dropped-in.wav")                                  # no .txt
    stereo(FOLDER / "voice-memo.wav")
    (FOLDER / "voice-memo.txt").write_text("them: a transcript with no header\n")
    stereo(FOLDER / "renamed.wav")                                     # header names another file
    (FOLDER / "renamed.txt").write_text(f"Call: +6590000000\nRecording: {rec}.wav\n\n")
    before = digest(FOLDER / f"{rec}.wav")

    print("\n-- only the recorder's calls are taken --")
    got = {s for s, _, _ in ingest.found()}
    check("the two recorder calls, and nothing else", got == {rec, done}, got)
    n = ingest.once()
    check("both filed", n == 2, n)
    rows = {r["call_id"]: r for r in calls.recent()}
    r = rows.get("cH", {})
    check("by the number in the header, not the name", r.get("caller") == "+6598554074", r)
    check("with its direction and start", r.get("outgoing") is False
          and r.get("started") == "2026-10-03T14:05:12", r)
    check("and filed at its END, not when it was read", r.get("at") == "2026-10-03T14:05:14", r)
    check("pointing at the recording", r.get("recordings") == [f"{rec}.wav"], r)
    check("the outgoing one too", rows.get("cD", {}).get("outgoing") is True, rows.get("cD"))
    check("and nothing is taken twice", ingest.once() == 0)
    check("what was taken is kept in THIS app's instance, not the folder",
          (paths.RUN / "ingested.json").is_file()
          and not any(p.name.startswith(".") or p.suffix == ".json" for p in FOLDER.iterdir()))

    print("\n-- a call to transcribe is split into its two sides --")
    legs = sorted(p.name for p in carry.legs().glob("*.wav"))
    check("the untranscribed call only", legs == [f"{rec}-callee.wav", f"{rec}-caller.wav"], legs)
    with wave.open(str(carry.legs() / f"{rec}-caller.wav"), "rb") as w:
        check("mono, the length of the call", w.getnchannels() == 1
              and abs(w.getnframes() / w.getframerate() - 2.0) < 0.01)
    check("and the queue sees them", sorted(p.name for p in transcribe.pending())
          == [f"{rec}-callee.wav", f"{rec}-caller.wav"])

    print("\n-- the transcript goes below the header; the recording is untouched --")
    for leg, said in (("caller", "is lunch still on"), ("callee", "yes, twelve")):
        w = carry.legs() / f"{rec}-{leg}.wav"
        w.with_suffix(".txt").write_text(said + "\n")
        transcribe._last_segments[str(w)] = [(0.0 if leg == "caller" else 1.0, 1.0, said)]
    check("ready once both sides are transcribed", transcribe.merge_ready() == [rec],
          transcribe.merge_ready())
    check("one written", transcribe.merge_once() == 1)
    text = (FOLDER / f"{rec}.txt").read_text()
    head, body = carry.split_txt(text)
    check("the header is the recorder's, word for word",
          head == "Call: Cen Lee (+6598554074)\nDirection: incoming\nStarted: 2026-10-03 14:05:12\n"
                  f"Length: 0:02\nRecording: {rec}.wav", head)
    check("and the transcript is below it, in speaking order",
          body == "them: is lunch still on\nyou: yes, twelve", body)
    check("the recording is not written", digest(FOLDER / f"{rec}.wav") == before)
    check("the split copies are gone", not list(carry.legs().glob(f"{rec}-*.wav")))
    check("and it is not done twice", transcribe.merge_ready() == [])
    check("the hub reads it as any call", carry.transcript_of(
        *reversed(carry.call_audio([f"{rec}.wav"], "cH"))) == body)


print("\n-- the daemon runs with no line --")
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


home = WORK / "boot"
env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")}
env.update(PYTHONPATH=str(ROOT / "src"), AGENTDUET_EDITION="ai", AGENTDUET_HOME=str(home),
           HOME=str(WORK))                  # NO PORT GIVEN: it takes one, and says which
log = (WORK / "daemon.log").open("w")
proc = subprocess.Popen([sys.executable, "-m", "agentduet_desktop.cli", "run", "--headless"],
                        stdout=log, stderr=subprocess.STDOUT, env=env)
try:
    site = home / "run" / "site-url"
    port = 0
    for _ in range(80):
        time.sleep(0.25)
        if site.is_file() and site.read_text().strip():
            port = int(site.read_text().split(":")[2].split("/")[0])
            break
    check("it says which port it took, and it is not a fixed one",
          port not in (0, 8897, 8899), port)
    t = (home / "run" / "web-token").read_text().strip() if port else ""

    def get(path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}?t={t}", timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    code, panel = get("/api/panel")
    check("it binds and serves", code == 200, (WORK / "daemon.log").read_text()[-1500:])
    check("as AgentDuet AI, with the AI half", panel.get("edition") == "ai" and "model" in panel,
          sorted(panel))
    check("and no channel", panel.get("channel") == "off", panel.get("channel"))
    check("the assistant is there", get("/api/chat_history")[0] == 200)
    check("a new install starts in setup", get("/api/setup/current")[1].get("needs_setup") is True)
    time.sleep(0.5)
    text = (WORK / "daemon.log").read_text()
    check("it never waited for a connector", "connector" not in text.lower()
          or "No AgentDuet connector yet" not in text, text[-1500:])
    check("and nothing raised", "Traceback" not in text, text[-2500:])
finally:
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()

print(f"\n  {passed} passed, {failed} failed\n")
sys.exit(1 if failed else 0)
