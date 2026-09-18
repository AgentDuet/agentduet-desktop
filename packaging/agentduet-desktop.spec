# PyInstaller spec — one file per platform, built ON that platform (no cross-compiling).
#
# Two things PyInstaller cannot work out by itself for this package, both of which fail only at
# RUNTIME on the user's machine, which is the worst place to find them:
#
# 1. HIDDEN IMPORTS. `llm.py` imports each provider SDK lazily, inside the function that uses it
#    (`from google import genai`, `import anthropic`, `import httpx`), so the model is config
#    rather than a hard dependency. PyInstaller's static analysis never sees those lines, so the
#    binary would build cleanly and then fail to attach a model. Listed explicitly below.
#
# 2. PACKAGE DATA. The templates a fresh instance is seeded from, the examples, and the three
#    served HTML pages are data files. Without them the binary starts, creates an empty instance,
#    and serves a blank owner site.
#
# Build:  pyinstaller packaging/agentduet-desktop.spec --noconfirm
# Result: dist/agentduet-desktop (dist/agentduet-desktop.exe on Windows)

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Stamp the build id into the package before collecting it. "0.1.0a2" is true of every binary
# built today, and the first question about any bug report is which one — so `--version` needs
# more than the version. Written here rather than committed: it is a build artifact, gitignored.
import subprocess as _sp
try:
    _sha = _sp.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                   cwd=str(Path(SPECPATH).parent)).stdout.strip()
    _dirty = bool(_sp.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                          cwd=str(Path(SPECPATH).parent)).stdout.strip())
except Exception:
    _sha, _dirty = "", False
#: A UTC build timestamp, ALWAYS, not only for dirty trees. Two reasons a commit id alone is
#: not enough:
#:
#:  1. It cannot separate two builds of the same dirty tree, which during an alpha is the normal
#:     case — one `+dirty` id can name a dozen different binaries built minutes apart.
#:  2. It has no ORDER. Given two builds, a sha cannot say which is newer, and "is the one I
#:     installed older than the one I am running?" is exactly what the installer must answer.
import datetime as _dt
_built = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
if _sha:
    (Path(SPECPATH).parent / "src" / "agentduet_desktop" / "_build.py").write_text(
        f'COMMIT = "{_sha}{"+dirty" if _dirty else ""}"\n'
        f'BUILT = "{_built}"\n')

datas = collect_data_files("agentduet_desktop",
                           includes=["*.html", "*.css",
                                     # The brand mark, served at /logo.png and used as the
                                     # favicon. Without this the frozen build serves a 404 where
                                     # every page shows its logo — which looks like a broken
                                     # asset rather than a missing packaging line.
                                     "*.png",
                                     # The icon font, subset to the fifteen icons the pages
                                     # use. Without it every icon renders as its own LIGATURE
                                     # NAME — "smart_toy Personal Assistant" — which is what
                                     # loading it from Google did on a machine whose network
                                     # was not up yet (2026-09-13).
                                     "fonts/**/*",
                                     "templates/**/*", "examples/**/*",
                                     # Prompts are DATA. Without this the binary builds
                                     # clean and voice dies at render time on a real call.
                                     "prompts/**/*",
                                     # The JS engine a customer tool runs inside. 1.3 MB, one
                                     # artifact for every platform.
                                     "wasm/**/*"])

# THE CA ROOTS, NAMED ON PURPOSE — and this is load-bearing, not tidiness.
#
# `certifi/cacert.pem` was in the bundle by ACCIDENT: PyInstaller's certifi hook fires only
# because some dependency imports certifi, and nothing here pinned that. A frozen build carries
# its own OpenSSL whose compiled-in CA path points at the machine that built it, so without this
# file `ssl.create_default_context()` loads no roots and EVERY handshake fails to verify. That
# shipped as a9: signed, notarized, stapled, and unable to reach the platform at all.
#
# `entry.py` reads it and is deliberately SILENT when it is missing, so the owner site still
# comes up — which means a dependency change that dropped certifi would reproduce a9 with CI
# still green. Naming it here is what stops that.
datas += collect_data_files("certifi")

# THE WASM RUNTIME'S NATIVE LIBRARY, ADDED BY HAND.
#
# `--collect-all wasmtime` DOES NOT WORK, and fails in the worst way: the build succeeds, the
# binary is suspiciously small, and it dies at runtime on "Failed to load dynlib
# _libwasmtime.so". The library is loaded through ctypes from a path computed at import, so
# PyInstaller never sees it as a dependency. Verified in a frozen onefile build 2026-08-05.
#
# The platform directory is part of the destination: wasmtime looks for it under
# wasmtime/<platform>/, so flattening it into the root does not help.
#
# TWO NAMES, because Windows does not use the `lib` prefix. Unix ships `_libwasmtime.so` and
# `_libwasmtime.dylib`; the win_amd64 wheel ships `wasmtime/win32-x86_64/_wasmtime.dll`. The
# glob was written for Unix and silently matched NOTHING on Windows, so the first Windows build
# produced an .exe that ran, printed most of `status`, and then died on
# "Failed to load dynlib _wasmtime.dll" (2026-09-18) — the exact failure the note above
# describes, in the one naming convention it did not anticipate.
_wasm_binaries = []
try:
    import wasmtime as _wt
    _wt_root = Path(_wt.__file__).parent
    for _pattern in ("_libwasmtime.*", "_wasmtime.dll"):
        for _lib in _wt_root.rglob(_pattern):
            _wasm_binaries.append((str(_lib), f"wasmtime/{_lib.parent.name}"))
except Exception as _exc:
    print(f"WARNING: wasmtime not collected ({_exc}) — customer tools will fail at runtime")
# ZERO IS ALWAYS WRONG, exactly as for the speech engine below. A runtime that was never
# collected is invisible until a customer tool runs, and the glob that missed it looked correct.
if not _wasm_binaries:
    print("WARNING: no wasmtime runtime collected — customer tools will fail at runtime")

# THE LOCAL LLM ENGINE, ADDED BY HAND FOR THE SAME REASON AS WASMTIME.
#
# `llama_cpp/llama_cpp.py` computes `dirname(__file__)/"lib"` at import and ctypes-loads
# libllama from it. PyInstaller's analysis sees a string, not a dependency — so
# `--collect-all llama_cpp` produces a binary that imports the package happily and dies on
# "Shared library with base name 'llama' not found" the first time a model is loaded. Which is
# after the owner has downloaded several gigabytes of weights.
#
# All five libraries go in the SAME directory: libllama links the three libggml ones through an
# $ORIGIN rpath, so splitting them breaks the load with a much less obvious error.
#
# Absent, the binary is still correct — models.available() reports that local models are not in
# this build, hosted providers work, and calls are carried and recorded with no model at all.
_llama_binaries = []
try:
    import llama_cpp as _lc
    _lc_root = Path(_lc.__file__).parent
    for _pat in ("*.so", "*.dylib", "*.dll"):
        for _lib in (_lc_root / "lib").glob(_pat):
            _llama_binaries.append((str(_lib), "llama_cpp/lib"))
    if not _llama_binaries:
        print("WARNING: llama_cpp found but its lib/ is empty — local models will not run")
except Exception as _exc:
    print(f"NOTE: llama_cpp not collected ({_exc}) — this binary has no local models")

hiddenimports = [
    # OUR OWN modules, all of them. Several are imported lazily inside functions (`web` from the
    # daemon, `tools`/`brain`/`canvas` from each other) to keep import order and startup cost
    # under control — and PyInstaller's static analysis follows none of those. Without this the
    # binary builds, starts, and then fails to open the owner site: "cannot import name 'web'".
    *collect_submodules("agentduet_desktop"),
    # Providers. Absent ones are tolerated at runtime — llm.py catches ImportError and reports
    # which credential is missing — so a build machine without all three still produces a
    # working binary for the providers it does have.
    "google.genai", "anthropic", "httpx",
    # aiohttp resolves parts of itself dynamically.
    *collect_submodules("aiohttp"),
    # Paired with the `datas` entry above: the data file is what OpenSSL reads, the module is
    # what makes the hook collect it in the first place.
    "certifi",
    # NOT collect_submodules("mcp"): that imports every submodule to enumerate it, and
    # `mcp.cli` calls sys.exit(1) at import time when its optional CLI extras are absent —
    # which aborts the BUILD. Only the server surface is actually used.
    "mcp.server.fastmcp",
    "yaml",          # writing Goose config.yaml
    # The window shell, optional at runtime — absent, `run` uses the browser.
    "webview",
    # THE LOCAL SPEECH ENGINE. Imported lazily inside transcribe._load(), and its presence is
    # probed with find_spec rather than an import — so PyInstaller's analysis sees NEITHER, and
    # without this the binary builds clean and reports the engine missing for ever. Exactly the
    # lazy-import gotcha at the top of CLAUDE.md.
    #
    # WAS faster_whisper + ctranslate2 + onnxruntime + av + tokenizers until 2026-09-09. Those
    # packages are no longer installed, and `collect_submodules` on an absent package does not
    # fail loudly — so leaving them here would have shipped a binary with NO speech engine at
    # all off a green build. That is the a6 failure exactly: signed, notarized, and broken.
    *collect_submodules("pywhispercpp"),
    # The C extension, which is a SIBLING of the package rather than inside it, so the import
    # graph does not always reach it.
    "_pywhispercpp",
    # THE LOCAL LLM. Imported inside models.load() and probed with find_spec in
    # models.available() — so PyInstaller sees neither, exactly like faster_whisper above.
    *collect_submodules("llama_cpp"),
    "diskcache", "jinja2",      # llama_cpp's own runtime dependencies, imported lazily by it
]

# THE SPEECH ENGINE'S NATIVE LIBRARIES. whisper.cpp is `libwhisper` plus five `libggml`
# backends, and ONE OF THEM IS METAL — which is the entire reason this engine replaced
# faster-whisper, so shipping without it would leave a binary that transcribes correctly and
# twelve times too slowly, with `status` reporting the GPU it is not using. Silent degradation,
# which is worse than a failure.
#
# collect_dynamic_libs DOES find these, unlike the ctranslate2 case this replaces: the wheel is
# delocated, so the real libraries sit in `pywhispercpp/.dylibs/` INSIDE the package and the
# copies at the site-packages root are version aliases of the same six files. Checked rather
# than assumed — the previous engine kept them in a sibling directory where the same call
# returned nothing at all.
_stt_libs = []
try:
    from PyInstaller.utils.hooks import collect_dynamic_libs as _cdl
    _stt_libs = _cdl("pywhispercpp")
    # AND THE AUDITWHEEL SIBLING, which is the LINUX layout and collected NOTHING without this.
    # A delocated macOS wheel keeps its libraries in `pywhispercpp/.dylibs/` INSIDE the package,
    # where collect_dynamic_libs finds them; a manylinux wheel puts them in a SIBLING
    # `pywhispercpp.libs/` and the same call returns an empty list. The a13 build proved it:
    # macOS collected six, Linux collected zero, and the build went green either way — which is
    # the same shape as the ctranslate2 note this replaced, on the platform I did not check.
    import pywhispercpp as _pw
    _pw_root = Path(_pw.__file__).parent
    for _d in (_pw_root.parent / "pywhispercpp.libs", _pw_root / ".libs"):
        if _d.is_dir():
            _stt_libs += [(str(_f), _d.name) for _f in _d.iterdir() if _f.is_file()]
    _names = {Path(_s).name for _s, _ in _stt_libs}
    # ZERO IS ALWAYS WRONG, on every platform. The Metal check below only speaks for macOS, so
    # it said nothing about a Linux binary with no speech engine in it at all.
    if not _stt_libs:
        print("WARNING: NO speech engine libraries collected — this binary will not transcribe. "
              f"Looked inside pywhispercpp and beside it at {_pw_root.parent}")
    if not any("metal" in _n for _n in _names) and sys.platform == "darwin":
        # LOUD, because the build would otherwise succeed. A Mac binary without the Metal
        # backend is the "why is this so slow" bug nobody can see in a listing.
        print("WARNING: no libggml-metal in the collected speech libraries — a macOS build "
              "without it falls back to the CPU and `backend()` will say so")
    print(f"NOTE: collected {len(_stt_libs)} speech engine libraries: {sorted(_names)}")
except Exception as _exc:
    print(f"WARNING: speech libraries not collected ({_exc}) — transcription will fail")

a = Analysis(
    [str(Path(SPECPATH).parent / "entry.py")],
    pathex=[str(Path(SPECPATH).parent / "src")],
    datas=datas,
    binaries=_wasm_binaries + _stt_libs + _llama_binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "test", "unittest"],   # nothing here draws a GUI
    noarchive=False,
)
pyz = PYZ(a.pure)

# ONEDIR ON macOS, ONEFILE EVERYWHERE ELSE — and this is a startup-time decision, not tidiness.
#
# A onefile binary unpacks its ENTIRE bundle into a temp directory on every launch and then
# imports back out of a compressed archive. Measured on an M-series Mac (a7): 3.87s from launch
# to a bound owner site, against 0.23s for the same code from source. `--version` alone was 0.82s
# vs 0.01s, and the gap grows with how much a command imports.
#
# It is also the wrong SHAPE for a Mac app. Dropbox's Contents/MacOS holds a 108K launcher with
# 344M laid out in Contents/Frameworks; ours held one 92M executable with Frameworks empty. A
# .app is meant to be a directory of files, not a self-extracting archive that happens to live
# in one.
#
# THE FIX IS NOT A NATIVE SHELL, which is the tempting conclusion: the Swift shell starts this
# same frozen binary and would pay the identical seconds behind a nicer window.
#
# LINUX KEEPS ONEFILE, deliberately. INSTALL.md promises "the binary is a single file" there and
# tells the owner to `chmod +x` it — which a directory cannot satisfy. Windows will want the same
# treatment as macOS when it lands.
#
# CONSEQUENCE FOR ANYTHING THAT INVOKES THE BUILD: on macOS `dist-bin/agentduet-desktop` is now a
# DIRECTORY, and the executable is `dist-bin/agentduet-desktop/agentduet-desktop`. CI's smoke
# steps and any local script that runs the bare path must branch on the OS.
if sys.platform == "darwin":
    exe = EXE(
        pyz, a.scripts,
        exclude_binaries=True,      # the binaries go to COLLECT, not inside the executable
        name="agentduet-desktop",
        console=True,      # it IS a terminal tool: init is an interview, run prints the URL
        upx=False,         # UPX compression is a reliable way to trip antivirus heuristics
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False,
        upx=False,
        name="agentduet-desktop",
    )
    # A .app double-clicks, appears in the Dock, and behaves like software — where a bare Unix
    # executable arrives without the executable bit and opens a Terminal window, which tells you
    # nothing about the product.
    #
    # It IS signed, notarized and stapled when the certificate secret exists (build.yml), and
    # `packaging/sign-macos.sh` does the same locally — so a normal double-click opens it and the
    # old right-click dance is gone. It signs many inner binaries now rather than one, which both
    # already handle by signing inner binaries first, deepest-last.
    #
    # console=True above means stdout exists but nobody sees it when launched from Finder, so the
    # daemon also logs to $AGENTDUET_HOME/run/daemon.log — otherwise a failed start is silent.
    app = BUNDLE(
        coll,
        name="AgentDuet Desktop.app",
        icon=None,
        bundle_identifier="com.b3networks.agentduet-desktop",
        info_plist={
            "CFBundleName": "AgentDuet Desktop",
            "CFBundleDisplayName": "AgentDuet Desktop",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            # It answers the phone while the owner is away, so it must not be culled when it
            # has no visible window. Flipping this to True is HALF of becoming a menu bar app —
            # see docs/design.md: without an NSStatusItem it leaves a running app nobody can
            # find, so the two land together or not at all.
            "LSUIElement": False,
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas,
        name="agentduet-desktop",
        console=True,
        onefile=True,
        upx=False,
    )
