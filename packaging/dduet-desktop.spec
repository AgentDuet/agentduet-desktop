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

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("dduet_desktop", includes=["*.html", "templates/**/*", "examples/**/*"])

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
    # The window shell, optional at runtime — absent, `run` uses the browser.
    "webview",
]

a = Analysis(
    [str(Path(SPECPATH).parent / "entry.py")],
    pathex=[str(Path(SPECPATH).parent / "src")],
    datas=datas,
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
