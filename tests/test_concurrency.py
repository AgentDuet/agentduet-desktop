"""How many calls AgentDuet Team can record at once on this Mac (2026-10-23). Slow: run by hand.

    PYTHONPATH=src .venv-build/bin/python tests/test_concurrency.py [counts…]   (default 1 10 25 50)

Each count runs in its own throwaway team instance (the switch off: record, then merge). N calls of
30 s go through the REAL `carry.handle` at once, on fake SDK objects whose two sides stream 20 ms of
24 kHz audio every 20 ms. Every call and side has its own tone, so audio crossing between calls is
caught. Then the queue merges all N, one at a time, as the daemon does.

It fails if any recording is missing, the wrong length, or holds another call's audio. It prints
the real-time recording CPU (share of one core), peak memory and the queue's time to save all N,
which are the numbers in docs/limits.md. Not measured: the SDK receiving audio (binary frames, about
as cheap as ours; limits.md doubles for it), and transcription (off by default in the team app).

Needs numpy (the [local] extra) to make and check the tones.
"""
from __future__ import annotations

import asyncio
import json
import math
import resource
import sys
import time

# ONLY THE RUN ITSELF IMPORTS THE PACKAGE, in its throwaway instance: importing it reads (and on a
# fresh machine creates) the default instance, which the parent must never touch.
ONE = __name__ == "__main__" and sys.argv[1:2] == ["--one"]
if ONE:
    import numpy as np
    from agentduet_desktop import audiofile, calls, carry, merge

N, SECONDS = (int(sys.argv[2]), float(sys.argv[3])) if ONE else (0, 0.0)
RATE, CHUNK_S = 24_000, 0.02                    # carry.SAMPLE_RATE, checked in main()
CHUNK = int(RATE * CHUNK_S)


def tone(call: int, leg: int) -> float:
    """A frequency per call and side, far enough apart to tell them all apart."""
    return 300.0 + 37.0 * call + (1000.0 if leg else 0.0)


class Addr:
    def __init__(self, value): self.value = value


class Noti:
    def __init__(self, i):
        self.call_id = f"sim-{i:03d}"
        self.subscriber_id = "+6560000100"
        self.from_address = Addr(f"+659{i:07d}")
        self.to_address = Addr(f"+656000{i % 5:04d}")      # five employees' lines


class Result:
    success, error_code = True, None
    def __bool__(self): return True


class Party:
    def __init__(self, hz, stop):
        self.hz, self.stop = hz, stop

    async def audio_stream(self):
        t0, n = time.monotonic(), 0
        while not self.stop.is_set() and n * CHUNK_S < SECONDS:
            i = np.arange(n * CHUNK, (n + 1) * CHUNK)
            pcm = (8000 * np.sin(2 * math.pi * self.hz * i / RATE)).astype("<i2").tobytes()
            yield pcm
            n += 1
            # REAL TIME: the platform delivers 20 ms of audio every 20 ms.
            await asyncio.sleep(max(0.0, t0 + n * CHUNK_S - time.monotonic()))


class Call:
    def __init__(self, i):
        self.id = Noti(i).call_id
        self._stop = asyncio.Event()
        self.caller = Party(tone(i, 0), self._stop)
        self.callee = Party(tone(i, 1), self._stop)
        self._hangup = None

    def on_hangup(self, fn):
        self._hangup = fn
        return fn

    async def connect(self, ring_time_seconds=30):
        async def end():
            await asyncio.sleep(SECONDS)
            self._stop.set()
            if self._hangup:
                self._hangup({"reason": "remote_hangup"})
        asyncio.get_running_loop().create_task(end())
        return Result()

    async def spy(self): return Result()
    async def disconnect(self): return Result()
    async def close(self): return Result()


class Session:
    def __init__(self, i): self.i = i
    async def process_call(self, noti): return Call(self.i)


class SM:
    def __init__(self): self.n = 0
    async def open_session(self, sid, subscriber):
        s = Session(self.n); self.n += 1
        return s


def dominant(pcm: np.ndarray) -> float:
    spec = np.abs(np.fft.rfft(pcm.astype(float)))
    return float(np.argmax(spec[1:]) + 1) * RATE / len(pcm)


async def main():
    assert carry.SAMPLE_RATE == RATE, "the calls' rate changed; update RATE"
    sm = SM()
    r0, t0 = resource.getrusage(resource.RUSAGE_SELF), time.monotonic()
    await asyncio.gather(*(carry.handle(sm, Noti(i)) for i in range(N)))
    r1, t1 = resource.getrusage(resource.RUSAGE_SELF), time.monotonic()
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    merge.once(settled=lambda w: True)
    r2, t2 = resource.getrusage(resource.RUSAGE_SELF), time.monotonic()
    c2 = resource.getrusage(resource.RUSAGE_CHILDREN)

    kept = sorted(p for p in carry.recordings().iterdir() if audiofile.is_recording(p.name))
    bad = []
    for p in kept:
        cid = carry.call_id_of(p.name.rsplit(".", 1)[0])
        i = int(cid.split("-")[1])
        ch, rate, width, frames = audiofile.read(p)
        a = np.frombuffer(frames, "<i2").reshape(-1, ch)
        mid = a[len(a) // 4: len(a) // 4 + RATE]                     # one second from the middle
        got = (dominant(mid[:, 0]), dominant(mid[:, 1]))
        want = (tone(i, 0), tone(i, 1))
        secs = len(a) / rate
        if any(abs(g - w) > 3 for g, w in zip(got, want)) or abs(secs - SECONDS) > 1.5:
            bad.append({"file": p.name, "got": got, "want": want, "seconds": round(secs, 2)})
    cpu = lambda a, b: (b.ru_utime + b.ru_stime) - (a.ru_utime + a.ru_stime)
    print(json.dumps({
        "calls": N, "seconds": SECONDS,
        "record_wall": round(t1 - t0, 2), "record_cpu": round(cpu(r0, r1), 2),
        "record_cpu_pct_of_one_core": round(100 * cpu(r0, r1) / (t1 - t0), 1),
        "merge_wall": round(t2 - t1, 2), "merge_cpu": round(cpu(r1, r2) + cpu(c1, c2), 2),
        "peak_rss_mb": round(r2.ru_maxrss / 1024 / 1024, 1),
        "kept": len(kept), "ext": sorted({p.suffix for p in kept}), "bad": bad,
        "lines": {l["line"]: l["calls"] for l in calls.lines()},
        "example": kept[0].name if kept else "",
    }))


def run_all(counts=(1, 10, 25, 50), seconds=30.0) -> int:
    """Each count in its own throwaway team instance; a table, and 1 if any recording is wrong."""
    import os, pathlib, subprocess, tempfile
    root = pathlib.Path(__file__).resolve().parent.parent
    print(f"  {'calls':>5} {'record CPU':>11} {'peak MB':>8} {'queue s':>8}  recordings")
    failed = 0
    for n in counts:
        env = dict(os.environ, AGENTDUET_EDITION="team", AGENTDUET_HOME=tempfile.mkdtemp(),
                   PYTHONPATH=str(root / "src"))
        out = subprocess.run([sys.executable, __file__, "--one", str(n), str(seconds)], env=env,
                             capture_output=True, text=True)
        try:
            r = json.loads(out.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            print(f"  {n:>5}  could not run: {out.stderr[-300:]}")
            failed = 1
            continue
        ok = r["kept"] == n and not r["bad"]
        failed |= not ok
        print(f"  {n:>5} {r['record_cpu_pct_of_one_core']:>10}% {r['peak_rss_mb']:>8} "
              f"{r['merge_wall']:>8}  {r['kept']}/{n} {'ok' if ok else 'WRONG: ' + json.dumps(r['bad'][:2])}")
    return failed


if __name__ == "__main__":
    if ONE:
        asyncio.run(main())
    else:
        counts = tuple(int(x) for x in sys.argv[1:]) or (1, 10, 25, 50)
        sys.exit(run_all(counts))
