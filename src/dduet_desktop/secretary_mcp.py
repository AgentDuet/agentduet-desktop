"""MCP face for the desktop secretary — a thin wrapper over tools.py.

The daemon (`secretary_agent.py`) owns the socket and answers external parties. This is the
OWNER's side: it lets your own AI app ("what's waiting for me?", "reply to Celine…", "grant her
the partner folder") read and drive the secretary's state.

TWO PROCESSES, AND ONLY ONE OF THEM IS ALWAYS ON

Over stdio the HOST spawns this server as a child and talks to it on stdin/stdout, so its
lifetime is the host session's — it is not a daemon and cannot be one. MCP is model-initiated:
a server only acts when the host's model calls a tool. It can never hold a WebSocket open or
react to a message at 2am. That is the daemon's job, and the daemon exists regardless.

Consequences to design around, not surprises to discover:

  - This process inherits the HOST's environment. `$DDUET_HOME` must resolve the same way here
    as in the daemon, or the face quietly operates on a different instance.
  - It holds NO credentials. Replies are queued to an outbox and the daemon sends them.
  - Both processes write the same instance files, with no locking. The outbox exists for exactly
    this reason on the send path; knowledge and settings writes can still interleave.

The local site (`web.py`) is the other face over the same `tools.py`.

REGISTERED FROM THE REGISTRY, NOT BY HAND

This used to list each tool in source and had drifted to 16 of 33 — so an owner could ask their
assistant for something the secretary plainly does and be told it could not. Enumerating a
registry by hand is the same bug this codebase keeps paying for, so the list is derived and
cannot fall behind.

    claude mcp add secretary -- /path/to/.venv/bin/python -m dduet_desktop.secretary_mcp

Run as a MODULE, not a file path: the imports are package-relative.
"""

from mcp.server import MCPServer

from . import tools

#: `mcp` 2.x renamed FastMCP to MCPServer. One import, so a future rename shows up in one place
#: rather than as a runtime ImportError in someone else's session.
mcp = MCPServer("secretary")


def _register() -> list[str]:
    """Expose every owner operation. Returns the names so a test can assert none went missing.

    The registry's argument hints are folded into the description: the host's model sees only
    the signature and the docstring, and several of these take an ISO date or an identity uuid
    where a plain string would be guessed wrong.
    """
    names = []
    for name, entry in tools.OWNER_TOOLS.items():
        fn = entry[0]
        hints = entry[1] if len(entry) > 1 else {}
        doc = (fn.__doc__ or name).strip()
        if hints:
            doc += "\n\nArguments: " + "; ".join(f"{k} — {v}" for k, v in hints.items())
        mcp.add_tool(fn, name=name, description=doc)
        names.append(name)
    return names


# SERVICE TOOLS — the process, not the secretary. Registered explicitly, and separately from
# the derived 33, because they are a different power: starting a process is not the same
# authority as reading the owner's knowledge and replying as them.
#
# They live HERE, on the stdio server the host spawns, rather than anywhere inside the daemon —
# a service cannot report being stopped over its own endpoint. That distinction costs nothing
# today (everything is stdio) and becomes load-bearing if the secretary tools ever move to HTTP.
from . import service

SERVICE_TOOLS = [service.service_status, service.service_start, service.service_stop]
for _fn in SERVICE_TOOLS:
    mcp.add_tool(_fn, name=_fn.__name__, description=(_fn.__doc__ or "").strip())

REGISTERED = _register() + [f.__name__ for f in SERVICE_TOOLS]


# A PROMPT, not a tool — the difference is who starts it.
#
# Tools are model-initiated: nothing happens until the host's model decides to call one. A prompt
# is OWNER-initiated, and hosts surface it as a named thing to click or type. That makes it the
# only surface here that answers "I installed this, now what?" — which was otherwise a question
# the owner had to already know the answer to.
#
# WHY AN MCP PROMPT AND NOT A GOOSE RECIPE
#
# Goose has recipes and they are richer. But a recipe is one vendor's config format, and this
# product is deliberately assistant-agnostic — the same prompt appears in Goose Desktop, Claude
# Desktop, and anything else speaking MCP, with nothing installed per host.
#
# The text lives in prompts/ with the others rather than inline, so the suite's template checks
# cover it too.
@mcp.prompt(name="get-started-with-dduet",
            title="Get started with DDuet",
            description="Set up your secretary, then call it yourself to see it answer.")
def get_started() -> str:
    from . import prompts
    return prompts.render("owner-getting-started")


@mcp.prompt(name="audit-my-tools",
            title="Audit my tools",
            description="Review what your secretary exposes to strangers, as an API security review.")
def audit_tools() -> str:
    """Static review of the owner's declared capabilities.

    WHY THIS SHIPS IN THE PRODUCT

    Tools are endpoints and the caller's words steer the client, so a customer adding capabilities
    is writing an API for an attacker-steered client — usually without API-security experience. The
    review discipline exists and is well understood; what is missing is a customer who knows to
    apply it. Shipping it as a prompt makes the guide an artifact they can run, not advice.

    STATIC ONLY, and the prompt says so twice. A live probe of a secretary rings a real phone and
    writes real records. Sandboxed testing is not built.

    It also disclaims the part it cannot judge: the five built-in asker tools are the product's own
    boundary, reviewed by its authors and pinned by tests. An owner's audit covers what the OWNER
    declared.
    """
    from . import prompts
    return prompts.render("owner-audit-tools")


PROMPTS = ["get-started-with-dduet", "audit-my-tools"]


if __name__ == "__main__":
    mcp.run()
