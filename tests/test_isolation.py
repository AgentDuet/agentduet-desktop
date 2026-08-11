"""The security test that matters: an external party can never reach an owner tool.

The external-facing path (secretary_agent.on_message) answers unauthenticated-to-us
outsiders. The owner tools can grant folder access and send messages as the owner.
If those ever share a code path, a prompt-injected message becomes privilege escalation.

    python test_isolation.py
"""

import ast
import os
import pathlib
import sys
import tempfile

# ISOLATION FIRST, BEFORE ANY IMPORT FROM THE PACKAGE — `paths` reads this at import time.
#
# Every folder and profile check below used to run against the OWNER'S OWN INSTANCE. On a machine
# with no profiles they were vacuous and passed; on this one they read real documents. Neither is
# a test. A seeded home makes them mean the same thing everywhere.
_HOME = pathlib.Path(tempfile.mkdtemp(prefix="iso-test-"))
os.environ["DDUET_HOME"] = str(_HOME)

HERE = pathlib.Path(__file__).parent
# The SOURCE tree, not this folder. `HERE / "secretary_agent.py"` resolved to tests/, which does
# not exist, so this suite died with a FileNotFoundError before reaching a single assertion —
# and a suite that crashes enforces nothing. Invariant 9 was unguarded for as long as that stood.
SRC = HERE.parent / "src" / "dduet_desktop"
# Imports go through the PACKAGE. They used to be bare (`import tools`), which worked when the
# modules were flat and stopped working the day they became a package with relative imports —
# a module doing `from . import paths` cannot be imported as a top-level module at all. That is
# the second reason this file has not run in a while, and it hides the first.
sys.path.insert(0, str(SRC.parent))
OWNER_ONLY = {"tools", "web", "secretary_mcp"}
failures: list[str] = []


def imports_of(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


# 1 · the daemon may only touch owner modules at the top level of main(), never from
#     the inbound message handler. Simplest enforceable rule: the module-level imports
#     of secretary_agent must not include the owner surface.
agent = SRC / "secretary_agent.py"
tree = ast.parse(agent.read_text())
top_level = set()
for node in tree.body:
    if isinstance(node, ast.Import):
        top_level.update(a.name.split(".")[0] for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        top_level.add(node.module.split(".")[0])

leaked = top_level & OWNER_ONLY
if leaked:
    failures.append(f"secretary_agent.py imports owner module(s) at top level: {leaked}")

# 2 · the inbound handler must not reference any owner tool by name.
from dduet_desktop import tools  # noqa: E402

owner_tool_names = set(tools.OWNER_TOOLS)
handler_src = ""
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message":
        handler_src = ast.unparse(node)
for name in owner_tool_names:
    if name in handler_src:
        failures.append(f"on_message references owner tool '{name}'")

# Seed the instance: one public document, and one person whose profile carries a folder grant
# and an escalation rule. Without the profile, checks 5 and 5b iterate an empty list and pass
# without testing anything — the failure mode that hid behind this suite not running at all.
from dduet_desktop import paths  # noqa: E402

paths.KNOWLEDGE.mkdir(parents=True, exist_ok=True)
(paths.KNOWLEDGE / "hours.md").write_text("We open at 9am on weekdays.\n")
paths.PEOPLE.mkdir(parents=True, exist_ok=True)
(_HOME / "partners").mkdir(exist_ok=True)
(_HOME / "partners" / "rates.md").write_text("Partner rate: 12.\n")
(paths.PEOPLE / "partner@example.com.md").write_text(
    "# partner@example.com\n\n## Folders\n\n- partners\n\n## Always escalate\n\n- pricing\n")

# 3 · permissions must reject a symlink escape from an allowed folder.
from dduet_desktop import permissions  # noqa: E402

secret = _HOME / "_iso_secret.md"
secret.write_text("TOPSECRET")
# Planted in the folder permissions ACTUALLY reads. It used to go into `tests/knowledge/`, a
# directory that does not exist and that nothing would have read if it did — so this check has
# never once exercised the symlink guard it is named for.
link = paths.KNOWLEDGE / "_iso_link.md"
try:
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(secret, link)
    ctx, _ = permissions.context_for("external party@example.com", False)
    if "TOPSECRET" in ctx:
        failures.append("symlink escaped an allowed folder")
finally:
    if link.is_symlink():
        link.unlink()
    secret.unlink(missing_ok=True)

# 4 · an external party with no grants must not see a scoped folder.
ctx, srcs = permissions.context_for("external party@example.com", False)
if any("partners" in s for s in srcs):
    failures.append(f"external party can read a scoped folder: {srcs}")

# 5 · a profile must never apply on an unverified channel — otherwise anyone who
#     self-declares an identity inherits that person's tone, scope and access.
from dduet_desktop import people  # noqa: E402

for identity in people.list_profiles():
    if people.profile_for(identity, False):
        failures.append(f"profile for {identity} applied to an unverified identity")
    if people.folders_for(identity, False):
        failures.append(f"{identity} inherited profile folders while unverified")
    if people.always_escalate(identity, False):
        failures.append(f"{identity} inherited person rules while unverified")
    ok = set(permissions.folders_for(identity, True))
    anon = set(permissions.folders_for(identity, False))
    if not anon <= ok or anon != set(permissions.load()["default"]["folders"]):
        failures.append(f"{identity} unverified saw more than the public default: {anon}")

# 5b · verification is carried by the IDENTITY, never inferred from the transport.
#      A self-vouching network must not smuggle a profile in for an unverified claim.
for identity in people.list_profiles():
    for net in ("WA", "WHATSAPP", "TELCO"):
        if people.default_verified(net) and not people.profile_for(identity, False):
            continue        # default only applies when the caller passes nothing
    if people.profile_for(identity, False) or people.folders_for(identity, False):
        failures.append(f"unverified claim of {identity} still resolved a profile")

# 6 · a profile filename can never escape the people/ folder.
if "/" in people.path_for("../../etc/passwd").name:
    failures.append("profile path traversal possible")

# 7 · the simulator must be OFF unless explicitly enabled — it can forge a verified
#     identity, which bypasses the whole identity model.
if os.getenv("SECRETARY_SIM") == "1":
    failures.append("SECRETARY_SIM=1 in this environment — simulator must be off by default")

if failures:
    print("FAIL")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS — owner tools isolated from the external party path; folder scoping holds")
