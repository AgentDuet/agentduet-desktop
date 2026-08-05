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
# Build:  pyinstaller packaging/dduet-desktop.spec --noconfirm
# Result: dist/dduet-desktop (dist/dduet-desktop.exe on Windows)

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
    (Path(SPECPATH).parent / "src" / "dduet_desktop" / "_build.py").write_text(
        f'COMMIT = "{_sha}{"+dirty" if _dirty else ""}"\n'
        f'BUILT = "{_built}"\n')

datas = collect_data_files("dduet_desktop",
                           includes=["*.html", "templates/**/*", "examples/**/*",
                                     # Prompts are DATA. Without this the binary builds
                                     # clean and voice dies at render time on a real call.
                                     "prompts/**/*",
                                     # The JS engine a customer tool runs inside. 1.3 MB, one
                                     # artifact for every platform.
                                     "wasm/**/*"])

# THE WASM RUNTIME'S NATIVE LIBRARY, ADDED BY HAND.
#
# `--collect-all wasmtime` DOES NOT WORK, and fails in the worst way: the build succeeds, the
# binary is suspiciously small, and it dies at runtime on "Failed to load dynlib
# _libwasmtime.so". The library is loaded through ctypes from a path computed at import, so
# PyInstaller never sees it as a dependency. Verified in a frozen onefile build 2026-08-05.
#
# The platform directory is part of the destination: wasmtime looks for it under
# wasmtime/<platform>/, so flattening it into the root does not help.
_wasm_binaries = []
try:
    import wasmtime as _wt
    _wt_root = Path(_wt.__file__).parent
    for _lib in _wt_root.rglob("_libwasmtime.*"):
        _wasm_binaries.append((str(_lib), f"wasmtime/{_lib.parent.name}"))
except Exception as _exc:
    print(f"WARNING: wasmtime not collected ({_exc}) — customer tools will fail at runtime")

hiddenimports = [
    # OUR OWN modules, all of them. Several are imported lazily inside functions (`web` from the
    # daemon, `tools`/`brain`/`canvas` from each other) to keep import order and startup cost
    # under control — and PyInstaller's static analysis follows none of those. Without this the
    # binary builds, starts, and then fails to open the owner site: "cannot import name 'web'".
    *collect_submodules("dduet_desktop"),
    # Providers. Absent ones are tolerated at runtime — llm.py catches ImportError and reports
    # which credential is missing — so a build machine without all three still produces a
    # working binary for the providers it does have.
    "google.genai", "anthropic", "httpx",
    # aiohttp resolves parts of itself dynamically.
    *collect_submodules("aiohttp"),
    # NOT collect_submodules("mcp"): that imports every submodule to enumerate it, and
    # `mcp.cli` calls sys.exit(1) at import time when its optional CLI extras are absent —
    # which aborts the BUILD. Only the server surface is actually used.
    "mcp.server.fastmcp",
    "yaml",          # writing Goose config.yaml
    # The window shell, optional at runtime — absent, `run` uses the browser.
    "webview",
]

a = Analysis(
    [str(Path(SPECPATH).parent / "entry.py")],
    pathex=[str(Path(SPECPATH).parent / "src")],
    datas=datas,
    binaries=_wasm_binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "test", "unittest"],   # nothing here draws a GUI
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="dduet-desktop",
    console=True,          # it IS a terminal tool: init is an interview, run prints the URL
    onefile=True,
    upx=False,             # UPX compression is a reliable way to trip antivirus heuristics
)

# macOS: also wrap it in a .app, because a bare Unix executable is not something you hand to
# someone evaluating how easy this is to start. Downloaded, it arrives without the executable
# bit and double-clicking opens a Terminal window — which tells you nothing about the product.
# A .app double-clicks, appears in the Dock, and behaves like software.
#
# It is NOT signed or notarized, so the first launch still trips Gatekeeper ("the developer
# cannot be verified"); right-click -> Open clears it once. Signing needs an Apple Developer ID
# and is the honest fix before anyone outside the team sees this.
#
# console=True above means stdout exists but nobody sees it when launched from Finder, so the
# daemon also logs to $DDUET_HOME/run/daemon.log — otherwise a failed start is silent.
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="DDuet Desktop.app",
        icon=None,
        bundle_identifier="com.b3networks.dduet-desktop",
        info_plist={
            "CFBundleName": "DDuet Desktop",
            "CFBundleDisplayName": "DDuet Desktop",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            # It answers the phone while the owner is away, so it must not be culled when it
            # has no visible window.
            "LSUIElement": False,
        },
    )
