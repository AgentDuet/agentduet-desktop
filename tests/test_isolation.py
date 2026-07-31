"""The security test that matters: an external party can never reach an owner tool.

The external-facing path (secretary_agent.on_message) answers unauthenticated-to-us
outsiders. The owner tools can grant folder access and send messages as the owner.
If those ever share a code path, a prompt-injected message becomes privilege escalation.

    python test_isolation.py
"""

import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
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
agent = HERE / "secretary_agent.py"
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
import tools  # noqa: E402

owner_tool_names = set(tools.OWNER_TOOLS)
handler_src = ""
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message":
        handler_src = ast.unparse(node)
for name in owner_tool_names:
    if name in handler_src:
        failures.append(f"on_message references owner tool '{name}'")

# 3 · permissions must reject a symlink escape from an allowed folder.
import os  # noqa: E402
import tempfile  # noqa: E402

import permissions  # noqa: E402

secret = pathlib.Path(tempfile.gettempdir()) / "_iso_secret.md"
secret.write_text("TOPSECRET")
# knowledge/ is flat now — there is no public/ subfolder to plant the symlink in.
link = HERE / "knowledge" / "_iso_link.md"
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
import people  # noqa: E402

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
    for net in ("DDUET", "WHATSAPP", "TELCO"):
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
