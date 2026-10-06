#!/usr/bin/env python3
"""Fail if anything in a .app needs a newer macOS than the app says it runs on.

    python packaging/check-minos.py "dist-bin/AgentDuet Desktop.app"

WHY (2026-10-06). The app declared macOS 13 (`LSMinimumSystemVersion`), and macOS opens it on 13.
But b8's bundle held llama.cpp built for macOS 26 — compiled from source on the macOS 26 runner,
which builds for itself unless told otherwise — and numpy and onnxruntime wheels built for 14. On
anything older than 26 the local models could not load: no transcripts, no summaries, no
assistant, while recording carried on as if nothing were wrong. Nothing checked; this does.

Every Mach-O file is read with `vtool -show-build` (the LC_BUILD_VERSION `minos`). A file with no
build version (old-style or data) is skipped.
"""
from __future__ import annotations

import pathlib
import plistlib
import subprocess
import sys


def version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split(".") if p.isdigit())


def minos(path: pathlib.Path) -> str:
    try:
        out = subprocess.run(["vtool", "-show-build", str(path)], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "minos":
            return parts[1]
    return ""


def is_macho(path: pathlib.Path) -> bool:
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    return magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def main(app: str) -> int:
    root = pathlib.Path(app)
    floor = plistlib.loads((root / "Contents" / "Info.plist").read_bytes()).get("LSMinimumSystemVersion", "")
    if not floor:
        print("no LSMinimumSystemVersion in Info.plist")
        return 1
    seen, over = 0, []
    for f in sorted(root.rglob("*")):
        if f.is_symlink() or not f.is_file() or not is_macho(f):
            continue
        m = minos(f)
        if not m:
            continue
        seen += 1
        if version(m) > version(floor):
            over.append((m, f.relative_to(root)))
    print(f"{seen} binaries checked against the app's minimum, macOS {floor}")
    for m, f in over:
        print(f"  needs macOS {m}: {f}")
    if over:
        print(f"FAIL: {len(over)} binaries need a newer macOS than the app says it runs on")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]) if len(sys.argv) == 2 else 2)
