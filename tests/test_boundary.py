"""The recorder must not depend on the secretary — checked, not intended.

WHY THIS EXISTS. The separation was DESIGNED and it still drifted back. `callmode.claim()` is a
real guard, `voice.register()` genuinely is not called in carry mode, `RECORDER_TOOLS` is a real
firewall — and none of that stopped `from . import brain` reappearing at the top of
`secretary_agent.py`, or `transcribe._record()` writing every carried call into the agent's query
log. Nothing went red, so nothing stopped it. An intention that nothing enforces is a comment.

TWO CHECKS, because one kind of leak hides from the other:

  1. SOURCE. A lazy `from . import brain` inside a function is invisible to an import test — the
     module loads clean and reaches for the secretary at runtime, which is exactly how the
     transcript leak survived. Parsed with `ast`, so it sees the import wherever it is written.

  2. IMPORT. A recorder module that imports something innocent which imports the secretary is
     invisible to a source scan. Run in a FRESH interpreter per module, because sys.modules is
     global and one earlier import in this process would poison every later answer.

WHAT THIS DOES NOT COVER, said plainly rather than implied: `web.py` imports `tools.py`, which
imports most of the secretary, and both products need the site. Splitting those two files is a
separate job. This pins the boundary that can be pinned today, and it will fail the moment
someone widens it.

Run:  python3 tests/test_boundary.py
"""

import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "agentduet_desktop"

#: The nine modules that exist ONLY so an agent can answer a call. If nobody is answered, none
#: of them has a subject.
SECRETARY = {"voice", "brain", "policy", "capabilities", "permissions", "asker_actions",
             "canvas", "toolstore", "wasm_host"}

#: The recorder: carry a call, record both legs, transcribe on this machine. Nobody is answered.
RECORDER = ["carry", "calls", "callmode", "transcribe", "machine", "connector"]

#: The entry point. It must be able to run EITHER product, so importing it must commit to
#: neither — the secretary is loaded when answer mode is chosen, not when the module is read.
ENTRY = ["secretary_agent"]

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
            print("        " + detail.replace("\n", "\n        "))


def referenced_secretary(path):
    """Every secretary module this file names, at any depth — module level or inside a def."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from . import brain` — the module is an alias, not the module being imported from.
            if node.module is None:
                found |= {a.name for a in node.names} & SECRETARY
            # `from .brain import record`
            elif node.module.split(".")[0] in SECRETARY:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] == "agentduet_desktop" and len(parts) > 1 and parts[1] in SECRETARY:
                    found.add(parts[1])
    return found


def loads_secretary(module):
    """What a FRESH interpreter pulls in when it imports this module. ('' on an import error.)"""
    code = (
        "import sys, json\n"
        f"import agentduet_desktop.{module}\n"
        "sec = %r\n" % (sorted(SECRETARY),) +
        "print(json.dumps(sorted({n.split('.')[-1] for n in sys.modules\n"
        "    if n.startswith('agentduet_desktop.')} & set(sec))))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                              "HOME": "/tmp", "AGENTDUET_HOME": "/tmp/dduet-boundary-test"})
    if out.returncode != 0:
        return None, out.stderr.strip().splitlines()[-1:] and out.stderr.strip().splitlines()[-1]
    import json
    return json.loads(out.stdout.strip().splitlines()[-1]), ""


print("\n-- the recorder names no secretary module, at any depth --")
for m in RECORDER:
    leaked = referenced_secretary(PKG / f"{m}.py")
    check(f"{m}.py", not leaked,
          f"reaches for {', '.join(sorted(leaked))} — a carried call has no agent in it")

print("\n-- nor pulls one in through anything else --")
for m in RECORDER:
    got, err = loads_secretary(m)
    if got is None:
        check(f"import {m}", False, f"could not import: {err}")
    else:
        check(f"import {m}", not got, f"loaded {', '.join(got)}")

print("\n-- reading the entry point commits to neither product --")
for m in ENTRY:
    got, err = loads_secretary(m)
    if got is None:
        check(f"import {m}", False, f"could not import: {err}")
    else:
        check(f"import {m}", not got,
              f"loaded {', '.join(got)} — carry mode pays for the secretary at startup")

# `tools.py` was 2035 lines and the recorder used about 100 of them. The secretary half moved to
# `secretary_tools.py` on 2026-08-27, and the ONE thing that keeps that split from closing again
# is the direction of the arrow: secretary_tools imports tools, never the reverse. Where a shared
# function needs an answer only the secretary has, it goes through `tools._capabilities()` — a
# lazy call, guarded on capabilities.json existing, so a recorder install never imports it.
print("\n-- the shared tools do not reach back into the secretary's --")
src = (PKG / "tools.py").read_text()
tree = ast.parse(src)
top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
named = set()
for n in top:
    if isinstance(n, ast.ImportFrom) and n.module is None:
        named |= {a.name for a in n.names}
    elif isinstance(n, ast.ImportFrom) and n.module:
        named.add(n.module.split(".")[0])
check("tools.py does not import secretary_tools", "secretary_tools" not in named,
      "the arrow reversed — the recorder is paying for the secretary again")

got, err = loads_secretary("tools")
extra = None
if got is None:
    check("import tools", False, f"could not import: {err}")
else:
    code = ("import sys\nimport agentduet_desktop.tools\n"
            "print('secretary_tools' in sys.modules or "
            "'agentduet_desktop.secretary_tools' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                              "HOME": "/tmp", "AGENTDUET_HOME": "/tmp/dduet-boundary-test"})
    check("importing tools does not load secretary_tools",
          out.stdout.strip() == "False", out.stdout.strip() + out.stderr.strip()[-200:])
    check("nor any of the nine", not got, f"loaded {', '.join(got)}")

print(f"\n  {passed} passed, {failed} failed\n")
sys.exit(1 if failed else 0)
