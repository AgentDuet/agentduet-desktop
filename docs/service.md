# The daemon is a service; every UI is a client

Status: **proposed**, 2026-08-03. Not built.

Companion to `agents.md`, and it answers that document's open question *"where does the MCP
server run — in the daemon process, or beside it?"* — **in it.**

---

## What made this necessary

On 2026-08-03 the daemon stopped at 10:21 and nobody noticed for twelve minutes. The log ends
cleanly on `DDUET channel connected — inbound is live`. No crash, no error, no alert. The window
had been closed, and `shell.py` returns when `webview.start()` does.

It was found by accident, while checking something unrelated.

That is the worst shape a fault can take for this product: **silent, and indistinguishable from
"nobody called."** The owner's only evidence would be something they never learned about. A
secretary that answers while you sleep cannot be ended by the most ordinary gesture a person
makes with a window.

The behaviour was deliberate — `shell.py` says so: *"Closing the window ends the run, which is
what a person expects of an app window."* That reasoning is right for an app and wrong for a
service, and this product is a service with an app attached.

## The shape

```
      ┌──────────────────────────────────────────────────┐
      │  THE SERVICE  (always on)                        │
      │  DDUET channel · voice · brain · instance state  │
      │  the single writer                               │
      └───┬───────────────┬───────────────┬──────────────┘
          │ loopback+token │              │ loopback+token
     ┌────▼─────┐   ┌──────▼──────┐  ┌────▼───────────────┐
     │ browser  │   │ native      │  │ MCP over HTTP      │
     │ tab      │   │ window      │  │ Claude Code, Goose │
     └──────────┘   └─────────────┘  └────────────────────┘
```

Every face is a client. Closing one closes a client. Quitting is a separate, explicit act.

## What this resolves

Three open checklist items collapse into one change:

| open item | how it resolves |
|---|---|
| closing the window stops the agent entirely | the window is a client; the service outlives it |
| nothing starts at login | you register the **service** with launchd / Task Scheduler / systemd-user. The UI is optional and separate |
| MCP face is secondary, per-session, and drifts | MCP becomes a first-class always-on face on the same process |

And two problems disappear rather than being managed:

- **`$DDUET_HOME` mismatch.** A stdio MCP server is spawned by the host and inherits *its*
  environment, so it can silently open a different instance from the daemon. A client connecting
  to a URL talks to whichever instance the service opened. There is only one answer.
- **Two writers.** Today the daemon and a spawned MCP process write the same instance files with
  no locking. The outbox exists for the send path for exactly this reason; knowledge and settings
  edits are still exposed. One process, one writer, gone.

## Why in-process rather than beside

The daemon already runs an aiohttp app on `127.0.0.1:8899` with a per-machine token. `mcp` 2.x
exposes `run_streamable_http_async` alongside `run_stdio_async`. Both Claude Code and Goose can
attach to a remote server — Goose calls it a *Remote Extension (Streaming HTTP)*.

So this is an endpoint on a server that already exists, not a new process to supervise. A
separate process would reintroduce the two-writer problem it is meant to remove.

## The cost, which must not be skipped

**stdio is implicitly access-controlled. HTTP is not.**

Only the process that spawned a stdio server can speak to it. An HTTP endpoint on loopback is
reachable by anything running as the same user — another app, a script, a package
`postinstall`. Unauthenticated, that exposes 33 owner operations including `grant_folder` and
`reply_to` to any local process.

So the MCP endpoint carries the same bearer-token discipline as the site, and `mcp` 2.x supports
auth on streamable HTTP. This is the one place where the right answer costs more than the simple
one, and skipping it would trade a fixed window bug for a genuine privilege-escalation surface.

**This adds a fourth actor to the threat model** (not yet written): a *local unprivileged
process*. Until now the actors were all remote or the owner themselves.

Open: whether the MCP endpoint reuses the site token or carries its own. Its own is tidier —
revoking an assistant's access should not log the owner out of their own dashboard — but it is
a second secret to store and hand over.

## What changes in code

- **`shell.py`** stops owning the daemon. Today it runs the daemon on a worker thread and the
  window on the main thread, so the window's lifetime is the process's. It becomes a client that
  attaches to a running service and starts one only if absent.
- **`cli.py`** grows the distinction: `run` (service, foreground), `open` (attach a UI), `stop`
  (end the service, deliberately). `status` already reports the service correctly.
- **`web.py`** gains the MCP endpoint beside the site, sharing the token check.
- **`secretary_mcp.py`** keeps stdio for hosts that only speak it, but stops being the primary
  path. The registry-derived registration stays as it is.
- **Packaging** gains the login-start unit per platform, registering the *service*, not the app.

## Open — decide before building

- **How does the owner know it is running, and how do they quit?** A service with no visible
  presence is its own failure mode: the owner cannot tell "answering" from "off". A tray icon is
  the conventional answer and is a per-platform GUI dependency; the alternative is that the app
  window is the indicator and quitting lives in it. **This must be answered as part of the
  change, not after** — otherwise the silent-stop bug is replaced by a silent-run bug, where
  nobody can tell it stopped for a different reason.
- **Does the window still start the service if none is running?** Convenient, and it makes
  double-click work exactly as it does today. It also means the first UI to open owns the
  service's lifetime unless it detaches properly.
- **One token or two?** See above.
- **Does the site chat survive** once an external assistant can drive everything over MCP? Same
  question `agents.md` leaves open, and it depends on the same product decision about who the
  owner is.

## Sequencing

1. **MCP over HTTP in the daemon, token-protected.** Additive — nothing existing changes
   behaviour, and it can be tested against both Claude Code and Goose immediately.
2. **Split the window from the service** in `shell.py` / `cli.py`. This is the fix for the
   silent stop.
3. **Decide the presence question** (tray or window), then build it.
4. **Login-start units**, which only make sense once the service is genuinely independent.

Step 1 is worth doing on its own even if the rest waits: it is the piece that makes "bring your
own assistant" testable, and it carries no risk to the current behaviour.
