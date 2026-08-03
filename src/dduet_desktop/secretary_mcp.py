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


REGISTERED = _register()


if __name__ == "__main__":
    mcp.run()
