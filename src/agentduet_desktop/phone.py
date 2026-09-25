"""The owner's phone, inside the app: a carried call can ring HERE instead of passing through.

WHAT IT IS. In carry mode every inbound call reaches us first, and `carry.handle` decides what
happens to it. With `## Answer here` on and the hub open, the call rings in the page; the owner
answers, and the caller is bridged to the page's microphone and speaker. Declining, or not
answering within `RING_SECONDS`, falls through to the ordinary pass-through — `connect()` to the
connector's configured destination, recorded as before.

NO SIP, deliberately. The SDK already carries a call's audio both ways — `call.answer()`, then
`call.caller.audio_stream()` in and `call.send_audio()` out — which is the SDK's own "Answer a
call" sample with a person where the echo loop is. The platform handles SIP toward the carrier.
A real softphone would bring a GPL stack, TLS and NAT handling and a second credential, only to
have Leg 2 come back to this same machine.

WHY THE PAGE, not native audio. The browser's `getUserMedia({echoCancellation: true})` gives
echo cancellation for free, on macOS and in Windows' WebView2 alike, and the page already talks
to this daemon over loopback. So audio crosses one more local hop — page to daemon over a
WebSocket — and nothing else changes.

This is the RECORDER product: two humans talk, nobody is answered by an agent, nothing is
decided. The secretary's fence has no subject here (see CLAUDE.md, "Two products, one binary").

One page answers. Every open page rings, the first to answer takes the call, and the others stop
ringing. There is one call at a time: a second inbound call while one is live passes through.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("secretary")

#: How long a call rings in the app before it passes through. Short enough that the fallback
#: still reaches the destination before a caller gives up; long enough to cross a room.
RING_SECONDS = 20

#: Mic chunks buffered between the page and the call. At ~20 ms a chunk this is about four
#: seconds — enough to ride out a stall, small enough that a stuck sender cannot grow it.
MIC_QUEUE = 200

#: Every page that has the phone socket open. Presence is what makes "ring here" possible: with
#: no page open there is nobody to ring, and the call passes through without waiting.
_pages: set = set()

#: The one call this phone is ringing or carrying, or None. Holds the decision future, the
#: answering page, and the owner's microphone queue.
_active: dict | None = None


def present() -> bool:
    """Whether any page could ring right now."""
    return bool(_pages)


def state() -> dict:
    """What a newly opened page should show: nothing, a ringing call, or a live one."""
    a = _active
    if not a:
        return {"type": "idle"}
    return {"type": a["state"], "call": a["call_id"], "from": a["from"], "since": a["since"]}


async def _send(ws, obj) -> None:
    try:
        await ws.send_json(obj)
    except Exception:                       # a page that went away is not an error
        _pages.discard(ws)


async def _broadcast(obj) -> None:
    for ws in list(_pages):
        await _send(ws, obj)


async def ring(call_id: str, caller: str, gone: asyncio.Event) -> str:
    """Ring every open page. Returns "answer", "decline", "timeout", "gone" or "busy".

    `gone` is the call's own hangup event: a caller who gives up while it rings must stop the
    ringing, or the page offers to answer a call that no longer exists.
    """
    global _active
    if _active is not None:
        return "busy"
    fut = asyncio.get_running_loop().create_future()
    _active = {"call_id": call_id, "from": caller, "state": "ringing", "since": time.time(),
               "decision": fut, "ws": None, "mic": asyncio.Queue(maxsize=MIC_QUEUE),
               "hangup": asyncio.Event()}
    await _broadcast(state())
    waiter = asyncio.create_task(gone.wait())
    try:
        done, _ = await asyncio.wait({fut, waiter}, timeout=RING_SECONDS,
                                     return_when=asyncio.FIRST_COMPLETED)
        if fut in done:
            return fut.result()
        return "gone" if waiter in done else "timeout"
    finally:
        waiter.cancel()
        if _active is not None and _active["state"] != "live":
            _active = None
            await _broadcast({"type": "idle"})


class _QueueParty:
    """Looks like an SDK `CallParty` to `carry._record_leg`: an `audio_stream()` to drain.

    The recorder is written against a party, and the owner's side of an in-app call is not a
    party the SDK has — it is the microphone. Feeding it through this keeps ONE recorder, so an
    answered-here call lands as the same caller/callee legs a carried call does, and the merge
    and transcription need no second path.
    """

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()

    async def audio_stream(self):
        while True:
            chunk = await self.q.get()
            if chunk is None:
                return
            yield chunk


async def bridge(call, done: asyncio.Event, far_rec: _QueueParty, near_rec: _QueueParty) -> None:
    """Carry an ANSWERED call between the caller and the page that answered, until it ends.

    Two pumps. Caller -> the page's speaker (and the recorder). Page microphone -> the call (and
    the recorder). The call ends on whichever comes first: the caller hanging up (`done`), the
    owner pressing hang up, or the answering page going away — a caller left talking to a
    closed tab is the one outcome worse than dropping the call.
    """
    a = _active
    ws, mic, hangup = a["ws"], a["mic"], a["hangup"]

    async def to_page() -> None:
        async for chunk in call.caller.audio_stream():
            far_rec.q.put_nowait(chunk)
            try:
                await ws.send_bytes(chunk)
            except Exception:
                hangup.set()                # the page is gone; end the call
                return

    async def to_call() -> None:
        while True:
            chunk = await mic.get()
            near_rec.q.put_nowait(chunk)
            try:
                await call.send_audio(chunk)
            except Exception as exc:        # CallClosedError is the normal end; others too
                logger.info("call %s: mic audio stopped (%s)", a["call_id"], type(exc).__name__)
                return

    pumps = [asyncio.create_task(to_page()), asyncio.create_task(to_call())]
    ended = [asyncio.create_task(done.wait()), asyncio.create_task(hangup.wait())]
    try:
        await asyncio.wait(ended, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in pumps + ended:
            t.cancel()
        await asyncio.gather(*pumps, *ended, return_exceptions=True)
        if hangup.is_set() and not done.is_set():
            await _hang_up(call, a["call_id"])


async def _hang_up(call, call_id: str) -> None:
    """End the call for the caller too, and SAY if the platform refused.

    THE RESULT IS CHECKED, because the SDK RETURNS operational failures rather than raising them.
    The first version awaited `disconnect()` and moved on, so when it came back falsy the app
    closed its recording and showed the call as over while the caller's phone stayed connected —
    found on the first real hang-up, 2026-09-25, with nothing in the log to say why.

    disconnect() is the verb that ends the call for everyone. If it is refused, close() — the agent
    leaving — is tried next: on a call the owner answered here the agent IS the owner's side, so
    leaving should drop the leg too. Both results are logged with the platform's reason.
    """
    for verb in ("disconnect", "close"):
        try:
            r = await getattr(call, verb)()
        except Exception as exc:
            logger.warning("call %s: %s raised (%s: %s)", call_id, verb, type(exc).__name__, exc)
            continue
        if r:
            logger.info("call %s: hung up from the app (%s)", call_id, verb)
            return
        logger.warning("call %s: %s was refused: %s (%s)", call_id, verb,
                       getattr(r, "error_message", "?"), getattr(r, "error_code", "?"))
    logger.error("call %s: could not hang up from the app — the caller may still be connected",
                 call_id)


async def finish() -> None:
    """Forget the live call and tell every page."""
    global _active
    _active = None
    await _broadcast({"type": "idle"})


# ---- the page side ------------------------------------------------------------------------


async def on_page(ws) -> None:
    """Serve one page's phone socket until it closes. Text frames are control, binary is mic.

    Control from the page: {"type": "answer"} / {"type": "decline"} while ringing, and
    {"type": "hangup"} during a call. To the page: the state, then the caller's audio as binary.
    """
    from aiohttp import WSMsgType
    _pages.add(ws)
    await _send(ws, state())
    # CALLS IN PROGRESS and their captions so far, so a page opened mid-call catches up rather
    # than showing a call that seems to have started with no words. See `live`.
    from . import live
    await _send(ws, live.snapshot())
    try:
        async for msg in ws:
            a = _active
            if msg.type == WSMsgType.BINARY:
                # ONLY the answering page's microphone reaches the call — a second open tab must
                # not be heard by the caller.
                if a and a["state"] == "live" and a["ws"] is ws:
                    try:
                        a["mic"].put_nowait(msg.data)
                    except asyncio.QueueFull:
                        pass                # a stalled call drops audio rather than growing
                continue
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                kind = (msg.json() or {}).get("type", "")
            except ValueError:
                continue
            if kind == "diag":
                # The page's own account of its microphone — see startAudio() and checkMic() in
                # web.html. Logged with or without a call, since the switch's check runs idle.
                logger.info("phone: microphone as the page sees it: %s", {
                    k: v for k, v in (msg.json() or {}).items() if k != "type"})
                continue
            if not a:
                continue
            if kind == "answer" and a["state"] == "ringing" and not a["decision"].done():
                a["ws"], a["state"], a["since"] = ws, "live", time.time()
                a["decision"].set_result("answer")
                await _broadcast(state())
            elif kind == "decline" and a["state"] == "ringing" and not a["decision"].done():
                a["decision"].set_result("decline")
            elif kind == "hangup" and a["state"] == "live" and a["ws"] is ws:
                a["hangup"].set()
    finally:
        _pages.discard(ws)
        a = _active
        if a and a["state"] == "live" and a["ws"] is ws:
            a["hangup"].set()               # the answering page closed mid-call
