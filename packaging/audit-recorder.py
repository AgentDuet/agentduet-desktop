"""Audit a built RECORDER app: prove there is no AI in it, by listing what is IN the bundle.

    python packaging/audit-recorder.py "dist-bin/AgentDuet Recorder.app"
    python packaging/audit-recorder.py --shell macos/.build/release/AgentDuetShell

This is the check a partner's security review will make, so it reads the SHIPPED artifact — the
module archive embedded in the daemon executable, the libraries beside it, the data files, and
the native shell's strings — never the build's intermediate files. Exits non-zero on any finding.

What "AI" means is `edition.py`'s lists (modules, libraries, data), read from the source tree, so
this cannot disagree with the build or with tests/test_recorder.py. The shell's strings are
checked against words only AI code would put there.
"""
from __future__ import annotations

import fnmatch
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from agentduet_desktop import edition  # noqa: E402

#: Native libraries of the AI runtimes, by file name.
AI_LIBS = re.compile(r"(llama|ggml|whisper|mtmd|onnxruntime|tokenizers|wasmtime|ctranslate)", re.I)
#: Words in the shell binary that only the AI views would put there. Whole words, so a view
#: model's `model` property and a balloon's `caption` do not count.
AI_WORDS = re.compile(r"\b(Personal Assistant|assistant|transcripts?|transcription|summary|"
                      r"captions|Gemma|Qwen|Whisper|decider|Decision model|/api/chat\w*|"
                      r"/api/proposals?|/api/summary|/api/setup/stt|/api/models)\b", re.I)

findings: list[str] = []


def finding(what: str) -> None:
    findings.append(what)
    print(f"  FOUND  {what}")


def shell_strings(shell: pathlib.Path) -> None:
    out = subprocess.run(["strings", "-n", "4", str(shell)], capture_output=True, text=True).stdout
    for h in sorted({m.group(0) for m in AI_WORDS.finditer(out)}):
        finding(f"{shell.name}: the string {h!r}")


def main(app: pathlib.Path) -> int:
    contents = app / "Contents"
    daemon = contents / "MacOS" / "agentduet-desktop"
    print(f"auditing {app}")

    # 1. THE PYTHON MODULES, from the archive inside the executable.
    from PyInstaller.archive.readers import CArchiveReader
    pkg = CArchiveReader(str(daemon))
    pyz_name = next(n for n in pkg.toc if n.endswith(".pyz"))
    names = sorted(pkg.open_embedded_archive(pyz_name).toc)
    ours = sorted(n.split(".", 1)[1] for n in names if n.startswith("agentduet_desktop."))
    print(f"  {len(names)} modules, {len(ours)} of ours: {' '.join(ours)}")
    for n in names:
        if n.startswith("agentduet_desktop.") and n.split(".")[1] in edition.AI_MODULES:
            finding(f"AI module {n}")
        if any(n == lib or n.startswith(lib + ".") for lib in edition.AI_LIBRARIES):
            finding(f"AI library {n}")
    # STAMPED AS THE RECORDER, or the daemon would run as the full edition with its half missing.
    try:
        code = pkg.open_embedded_archive(pyz_name).extract("agentduet_desktop._edition")
        if edition.RECORDER not in getattr(code, "co_consts", ()):
            finding("the build is not stamped as the recorder (_edition)")
    except Exception as exc:                    # absent: the edition would read "full"
        finding(f"no _edition stamp in the archive ({exc})")

    # 2. NATIVE LIBRARIES, anywhere in the bundle.
    for f in contents.rglob("*"):
        if f.is_file() and AI_LIBS.search(f.name) and f.suffix in (".dylib", ".so", ".dll", ""):
            if f.suffix or f.stat().st_mode & 0o111:
                finding(f"AI runtime library {f.relative_to(app)}")
    for helper in ("agentduet-stt",):
        if (contents / "MacOS" / helper).exists():
            finding(f"speech helper Contents/MacOS/{helper}")

    # 3. DATA FILES the recorder leaves out.
    data = contents / "Resources" / "agentduet_desktop"
    if data.is_dir():
        for f in data.rglob("*"):
            rel = f.relative_to(data).as_posix()
            if f.is_file() and any(fnmatch.fnmatch(rel, g) or
                                   (g.endswith("/**/*") and rel.startswith(g[:-4]))
                                   for g in edition.AI_DATA):
                finding(f"AI data {rel}")

    # 4. THE NATIVE SHELL'S STRINGS — what someone running `strings` on the app would read.
    for shell in (f for f in (contents / "MacOS").iterdir()
                  if f.is_file() and f.name != "agentduet-desktop"):
        shell_strings(shell)

    print(f"\n  {'CLEAN — no AI found' if not findings else str(len(findings)) + ' finding(s)'}")
    return 1 if findings else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--shell":
        # THE SHELL ALONE, for the compile check that builds no daemon (macos-shell.yml).
        shell_strings(pathlib.Path(sys.argv[2]))
        print(f"  {'CLEAN' if not findings else str(len(findings)) + ' finding(s)'}")
        sys.exit(1 if findings else 0)
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(pathlib.Path(sys.argv[1])))
