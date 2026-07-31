"""MCP face for the desktop secretary — a thin wrapper over tools.py.

The daemon (`secretary_agent.py`) owns the socket and answers external parties. This is the
OWNER's side: it lets your own LLM app ("what's waiting for me?", "reply to Celine…",
"grant her the partner folder") read and drive the secretary's state.

Why it's a separate process: MCP is model-initiated — a server only acts when the host's
model calls a tool. It cannot hold a WebSocket open or react to a message at 2am. So the
daemon exists regardless; this is a face over its state. It holds NO credentials: replies
are queued to an outbox and the daemon sends them.

The local site (`web.py`) is the other face over the same `tools.py`.

    claude mcp add secretary -- /path/to/.venv/bin/python /path/to/secretary_mcp.py
"""

from mcp.server.fastmcp import FastMCP

from . import tools

mcp = FastMCP("secretary")

# One wrapper per owner tool. Bodies live in tools.py so the MCP and web faces
# can never drift apart.
mcp.tool()(tools.pending_escalations)
mcp.tool()(tools.digest)
mcp.tool()(tools.search_queries)
mcp.tool()(tools.list_permissions)
mcp.tool()(tools.reply_to)
mcp.tool()(tools.add_knowledge)
mcp.tool()(tools.grant_folder)
mcp.tool()(tools.revoke_folder)
mcp.tool()(tools.index_status)
mcp.tool()(tools.who_is)
mcp.tool()(tools.conversation_with)
mcp.tool()(tools.list_people)
mcp.tool()(tools.add_person)
mcp.tool()(tools.note_person)
mcp.tool()(tools.profile_suggestions)
mcp.tool()(tools.accept_observation)


if __name__ == "__main__":
    mcp.run()
