"""Desktop secretary — POC.

Runs on the OWNER's machine. Receives queries from external parties over the AgentDuet
WhatsApp channel, answers from `knowledge.md`, escalates anything the policy won't let it
answer, and logs every query for the daily digest.

    ./start.sh          # run it      ./stop.sh
    python digest.py    # today's report

Messaging is REACTIVE: a reply goes to a participant we have seen on inbound, so we cannot
start a conversation with someone who has never written. That is why escalation reaches the
owner as a DESKTOP notification rather than a message.

WHY WHATSAPP AND NOT DDUET (2026-08-11)
The DDUET channel — Nexus web chat, visitor identified by email — was dropped. It exists only
on the `feature/dduet-channel` branch of the SDK, so using it meant vendoring a wheel built
from a private repository, which blocks publishing this package at all. The released SDK on
PyPI carries TELCO and WA and no DDUET. Neither onboarding path in the August flow uses DDUET
either; both arrive over a trunk or over WhatsApp. Dropping it also ends the base-URL clash,
because DDUET needed a dev endpoint while voice needs prod, and one client has one base URL.

To reverse this, DDUET has to be merged into the SDK's main line and released — at which
point the guard below takes a second network rather than swapping back.
"""
from __future__ import annotations


import asyncio
import json
import logging
import os
import sys
import pathlib
from datetime import datetime


try:
    from agentduet import (
        CallAudioConfig,
        IncomingMessage,
        InboundCallMode,
        TriggerConditionsBuilder,
        Network,
        SendWAMessage,
        Session,
        SessionManager,
        SessionManagerConfig,
        new_session_id,
    )
except ImportError:
    CallAudioConfig = IncomingMessage = InboundCallMode = TriggerConditionsBuilder = Network = SendWAMessage = Session = SessionManager = SessionManagerConfig = new_session_id = None

from . import brain
from . import people

from . import paths
from . import status

HERE = pathlib.Path(__file__).parent
RUN = paths.RUN
LOG = RUN / "queries.jsonl"
SESSIONS = RUN / "sessions.json"   # asker -> who we may reply to (read by secretary_mcp)
OUTBOX = RUN / "outbox.jsonl"      # owner replies queued by secretary_mcp.reply_to

try:
    from dotenv import load_dotenv
    # Explicitly the INSTANCE file. A bare load_dotenv() searches the CWD and found the
    # install-dir .env left behind by the migration — and since load_dotenv never overrides an
    # already-set variable, that stale copy won every race against the real config. The daemon
    # then ran a model the owner had already replaced, reporting nothing wrong.
    load_dotenv(paths.ENV_FILE)
except ImportError:
    pass
# Log to a FILE as well as stdout. Launched from Finder as a .app — the way the owner will
# actually start it — stdout goes nowhere, so without this a failed start is completely
# silent: no window, no error, nothing to send anyone. This file is the first thing to ask
# for in a bug report.
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    paths.RUN.mkdir(parents=True, exist_ok=True)
    _handlers.append(logging.FileHandler(paths.RUN / "daemon.log", encoding="utf-8"))
except OSError:
    pass          # a read-only or missing instance dir must not stop the daemon starting
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=_handlers)
logger = logging.getLogger("secretary")
# The SDK's connect/inbound logs are DEBUG; without this a silent channel looks
# identical to a working one with no traffic.
logging.getLogger("agentduet.session_manager").setLevel(logging.DEBUG)
logging.getLogger("agentduet.session_manager_connection").setLevel(logging.INFO)

def owner_name() -> str:
    """Who the agent says it works for.

    Resolved from settings.md, NOT from a module-level default. It used to be
    `os.getenv("OWNER_NAME", "the owner")`, and with the variable unset the voice prompt read
    "You are the personal assistant for the owner" — which the model treated as a TEMPLATE and
    answered calls with "Hello, this is [Owner's Name]'s assistant." The configured name was
    sitting in settings.md the whole time, written by the setup interview.

    Called per use rather than captured at import, so a name changed in setup applies to the
    next call instead of the next restart. The env var still wins, for tests and for overriding
    an instance without editing it.
    """
    from . import owner
    return os.getenv("OWNER_NAME") or owner.name()

#: How often to look for a connector the owner may have just added on the settings page — and,
#: with it, for a setup they may have just finished. Short enough that saving one feels immediate;
#: both checks are environment and file reads, so the cost is nothing. One constant because it is
#: one behaviour: waiting for the owner to supply something, in the same process.
CONNECTOR_POLL_SECONDS = 3

#: Meta Graph API version quoted on every outbound WhatsApp message, matching the SDK's
#: `examples/wa_echo_bot.py`. A code constant on purpose: it is the same in every environment
#: and is not something an operator retunes — it changes when Meta deprecates a version, which
#: is a code change with a message-shape review attached, not a setting to flip.
WA_API_VERSION = "v23.0"


def _first_text(payload: dict) -> str:
    """The message body, whichever shape it arrives in.

    THE INBOUND SHAPE IS NOT CONFIRMED. The outbound one is — `examples/wa_echo_bot.py` in the
    SDK shows it exactly — but that bot replies with a fixed string and never reads an inbound
    body, so it proves nothing about the direction we need. Meta's message object nests the body
    under `text.body`, sometimes inside a `messages` array; the older Nexus form used typed
    `parts`. All three are accepted rather than guessing one, and anything unrecognised is logged
    in full, because the first real message is what settles this.

    Narrow this once a real payload has been seen. Until then the tolerance is deliberate.
    """
    if isinstance(payload.get("text"), dict):                 # Meta, flat
        return payload["text"].get("body", "")
    for m in payload.get("messages", []) or []:               # Meta, wrapped
        if isinstance(m.get("text"), dict):
            return m["text"].get("body", "")
    for part in payload.get("parts", []) or []:               # Nexus MessageContent
        if part.get("type") == "text":
            return part.get("text", {}).get("body", "")
    logger.warning("could not read a text body from an inbound message — raw payload: %s",
                   json.dumps(payload, default=str)[:2000])
    return ""


def _wa_text(body: str, *, to: str) -> SendWAMessage:
    """One outbound WhatsApp text, shaped as `examples/wa_echo_bot.py` in the SDK shapes it.

    A helper rather than two literals because both senders — answering an asker, and delivering
    a reply the owner queued — must produce the same object. They were separate literals for
    DDUET and the two did drift.
    """
    return SendWAMessage(
        api_version=WA_API_VERSION,
        data={
            "messaging_product": "whatsapp",
            "type": "text",
            "to": to,
            "recipient_type": "individual",
            "preview_url": False,
            "text": {"body": body},
        },
    )


def remember_session(asker: str, subscriber: str) -> None:
    """Remember who we may reply to, and through which subscriber.

    A reply needs the subscriber the message arrived on — for WhatsApp that is the Business
    Account's `phone_number_id`, which nothing else exposes. Persisted so the owner can answer
    later through the MCP tool, in a process that never saw the inbound message.

    No session token any more: DDUET needed a per-conversation Nexus id, WhatsApp routes on the
    participant. Which also means two parallel conversations with one person are ONE thread here
    — correct for WhatsApp, where the number is the person.
    """
    RUN.mkdir(exist_ok=True)
    data = json.loads(SESSIONS.read_text()) if SESSIONS.exists() else {}
    data[asker] = {
        "subscriber": subscriber,
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    SESSIONS.write_text(json.dumps(data, indent=2))




async def run_channel() -> None:
    """One attempt at the AgentDuet channel. Raises if it cannot connect, so main() can retry."""
    # Bound ONCE here, at the top. Kept a lazy import (it reaches the adapters), but it must be
    # bound before first use: a `from . import voice` further down made `voice` local to this
    # whole function, so the CallAudioConfig line above it raised UnboundLocalError.
    from . import voice
    # 24 kHz, NOT the SDK's 16 kHz default. The Qwen adapter declares
    # output_audio_format="pcm24" and emits 24 kHz mono; negotiating 16 kHz meant every sample
    # was played 1.5x too slowly with the pitch dropped about a fifth. Symptom on a real call:
    # an agent that "speaks verrrry slowly" and sounds male even though the voice is female.
    # If the voice model is ever changed, this has to match ITS output rate.
    # TOKEN FIRST, API KEY SECOND. Signing in provisions the connector server-side and hands
    # back a rotating token, so a signed-in install needs neither value in .env. The api_key path
    # is untouched for installs that predate sign-in, and the SDK's `x-api-key` handshake branch
    # is likewise untouched upstream — so both work, and neither has to be migrated.
    #
    # The provider is passed as a CALLABLE, not a token: the SDK calls it before every connect
    # attempt, which is the only moment that knows whether the cached one is still good. Handing
    # over a string here would freeze a credential that expires in thirty minutes into a daemon
    # that runs for weeks.
    from . import oauth
    kwargs = dict(call_audio=CallAudioConfig(sample_rate=voice.CALL_SAMPLE_RATE))
    if oauth.signed_in():
        kwargs["token_provider"] = oauth.token_provider
        kwargs["connector_uuid"] = oauth.connector_uuid()
    else:
        kwargs["api_key"] = os.getenv("AGENTDUET_API_KEY")
        kwargs["connector_uuid"] = os.getenv("AGENTDUET_CONNECTOR_UUID")
    config = SessionManagerConfig.create(**kwargs)

    async with SessionManager(config) as sm:
        sessions: dict[str, Session] = {}

        async def session_for(subscriber: str) -> Session:
            if subscriber not in sessions:
                sessions[subscriber] = await sm.open_session(new_session_id(), subscriber)
            return sessions[subscriber]

        @sm.on_incoming_message
        async def on_message(msg: IncomingMessage):
            if msg.network is not Network.WA:
                # SAY SO. This used to `return` in silence, which meant a message on another
                # network left no trace at all — indistinguishable from the channel being dead,
                # and impossible to test against. The released SDK carries TELCO and WA; we
                # answer WA here, and TELCO arrives as a CALL through voice.register(), not here.
                logger.info("ignored a %s message from %s (subscriber %s) — only WA is "
                            "answered on this channel", msg.network, msg.participant.value,
                            msg.subscriber)
                return

            asker = msg.participant.value          # the sender's wa_id (their phone number)
            question = _first_text(msg.payload)
            logger.info("← %s: %s", asker, question)
            remember_session(asker, msg.subscriber)
            # NOT status.set_number(): the subscriber is the Business Account's
            # `phone_number_id`, a Meta identifier and not a dialable number, so showing it in
            # the header would read as the owner's number while being unusable as one. A real
            # number arrives only on an inbound CALL, where the subscriber is the line it ran on.

            # WhatsApp proves the sender controls the number — Meta authenticates it at
            # registration, which is a stronger claim than an email a web form merely collected.
            # `people.SELF_VOUCHING_NETWORKS` already said WhatsApp self-vouches; it listed the
            # name "WHATSAPP" while the SDK enum is "WA", so the intent never actually fired.
            #
            # Know what this turns on: a verified asker gets their curated profile and their own
            # retained history. It does NOT widen `knowledge/`, which is flat and public to every
            # asker either way, so the disclosure surface is unchanged by this line.
            network = msg.network.value if hasattr(msg.network, "value") else str(msg.network)
            verified = people.default_verified(network)
            # No conversation key: DDUET had a per-conversation Nexus token, WhatsApp has none,
            # so memory falls back to the identity — which on this channel IS the person.
            result = await brain.handle_query(asker, question, network, verified=verified,
                                              conversation=None)
            reply, outcome = result["reply"], result["outcome"]
            logger.info("→ [%s%s] %s", outcome,
                        f" {result['reason']}" if result["reason"] else "", reply)

            send = await (await session_for(msg.subscriber)).send_message(
                _wa_text(reply, to=asker))
            if not send.success:
                logger.error("reply failed: %s (%s)", send.error_code, send.error_content)

        async def drain_outbox() -> None:
            """Send replies the owner queued through the MCP tool."""
            while True:
                await asyncio.sleep(3)
                if not OUTBOX.exists() or OUTBOX.stat().st_size == 0:
                    continue
                lines = [l for l in OUTBOX.read_text().splitlines() if l.strip()]
                OUTBOX.write_text("")          # claim the batch
                stored = json.loads(SESSIONS.read_text()) if SESSIONS.exists() else {}
                for line in lines:
                    item = json.loads(line)
                    s = stored.get(item["asker"])
                    if not s:
                        logger.error("owner reply dropped — no session for %s", item["asker"])
                        continue
                    result = await (await session_for(s["subscriber"])).send_message(
                        _wa_text(item["text"], to=item["asker"]))
                    if result.success:
                        logger.info("→ (from owner) %s: %s", item["asker"], item["text"])
                        brain.record(item["asker"], "(owner reply)", "owner", "", item["text"])
                    else:
                        # Most likely outside WhatsApp's customer-service window: Meta only
                        # allows a free-form reply within 24h of the person's last message, and
                        # after that it needs an approved template we do not have. Surface it
                        # rather than failing silently — the owner's answer did not arrive.
                        logger.error("owner reply failed for %s: %s (%s)",
                                     item["asker"], result.error_code, result.error_content)

        # Inbound messaging is gated by the connector's trigger conditions — the wss-edge
        # plan notes it "reuses the existing inboundMessage/outboundMessage gates
        # (channel-agnostic)". These persist server-side, and the bank demo's VoiceAgent
        # sets inbound_call=ALL on the same connector, which can clear them. So set
        # them here every start rather than assuming an earlier run left them on.
        # Voice registers a call handler on THIS client. VoiceAgent.serve() would open a
        # second SessionManager on the same connector — the race the comment above describes,
        # from the other side. One client, both handlers, one trigger config.
        # ONE HANDLER PER CONNECTOR, so this is a choice and not a pair. `## Calls: carry`
        # bridges the call onward and records both legs; anything else answers it as the
        # secretary, which is the mode that has been in production. Deciding here rather than
        # inside either module keeps the exclusivity visible in one place — two modules each
        # registering "only if the other did not" is how both end up attached.
        from . import owner as owner_settings
        if owner_settings.calls() == owner_settings.CALLS_CARRY:
            from . import carry
            calls_on = carry.register(sm)
            status.set_voice(False)        # no agent speaks in this mode; do not claim one does
        else:
            calls_on = voice.register(sm, owner_name())
            status.set_voice(calls_on)

        builder = (TriggerConditionsBuilder()
                   .inbound_message(True)
                   .outbound_message(True))
        if calls_on:
            builder = builder.inbound_call(InboundCallMode.ALL)
        # NOT FATAL (2026-08-11). This raised, and the raise killed the whole channel: connect,
        # register, die, retry — forever, with the daemon reporting only "channel unavailable".
        #
        # Two facts make dying the wrong response. Trigger conditions PERSIST SERVER-SIDE, so a
        # connector that was configured by an earlier run is still configured when this call
        # fails. And NONE of the SDK's own examples call this at all — `basic_example`,
        # `wa_echo_bot` and `connect_spy_isolated` connect, register a handler and run — which
        # means a connector is expected to work without it. Verified: with this call skipped the
        # socket stays up indefinitely, where with it the server closes the connection.
        #
        # So try it, say plainly what happened, and carry on. Setting triggers is an attempt to
        # ENSURE a state, not a precondition for running — and refusing to answer the phone
        # because we could not re-assert a setting that may already be correct is a worse
        # failure than the one it guards against.
        try:
            await sm.setup_trigger_conditions(builder.build())
            logger.info("trigger conditions set: inbound_message=True, outbound_message=True, "
                        "inbound_call=%s", "ALL" if calls_on else "off")
        except Exception as exc:
            logger.warning(
                "could not set trigger conditions (%s: %s) — carrying on with whatever the "
                "connector already has. If nothing arrives, that is the first thing to check.",
                type(exc).__name__, exc)

        asyncio.create_task(drain_outbox())
        # Recordings become transcripts here, not on the call path. Started unconditionally:
        # the queue is derived from the filesystem, so it is a no-op when nothing was carried,
        # and it also picks up anything a previous run left unfinished.
        from . import transcribe
        asyncio.create_task(transcribe.worker())

        logger.info("AgentDuet channel connected — inbound is live")
        status.set_channel("live")
        try:
            # install_signal_handlers=False is REQUIRED, not a preference: shell.py runs this
            # coroutine on a worker thread so pywebview can own the main one, and the SDK's
            # handler install calls set_wakeup_fd, which raises RuntimeError off the main
            # thread. The SDK means to degrade gracefully there but only catches
            # (NotImplementedError, AttributeError, ValueError), so the RuntimeError escaped
            # and killed the channel one line after "inbound is live" — a connector that
            # connected, set its triggers, then dropped every 5s forever.
            # We do not want them regardless: `cli stop` owns shutdown and escalates to
            # SIGKILL itself.
            await sm.run_forever(install_signal_handlers=False)
        finally:
            # run_forever returning is a disconnect, not a shutdown: main() reconnects.
            status.set_channel("retrying", "disconnected")


def connector_ready() -> bool:
    """Read the ENVIRONMENT every time, not a value captured at startup — the settings page
    writes the credential into this process as well as to .env."""
    return bool(os.getenv("AGENTDUET_API_KEY") and os.getenv("AGENTDUET_CONNECTOR_UUID"))


async def main() -> None:
    """Owner site first, channel second — and never let the channel take the site down.

    Everything used to live inside `async with SessionManager(...)`, so an unreachable
    endpoint killed the whole process. After a laptop restart with the SD-WAN not yet up,
    that meant the owner could not see their OWN queue because a network they were not on
    was down. The queue, the history and the escalations are all local; none of them need
    the channel. Only inbound and outbound messages do.

    So the site binds unconditionally, and the channel is retried behind it with backoff.
    Reconnecting when the VPN returns then costs nothing.
    """
    # Localhost-only + token; see web.py. NOT fatal any more: the site is a transitional
    # surface, and the owner reaches this daemon through the mcp (docs/design.md). Answering
    # a stranger's call must not depend on a UI having bound a port — the daemon IS the
    # product. This used to raise SystemExit(1), which meant a port clash took the phone off
    # the air.
    try:
        from . import web
        logger.info("Owner site: %s", await web.start())
    except Exception as exc:
        logger.warning("Owner site did not start (%s: %s) — carrying on. Inbound is unaffected; "
                       "reach this daemon through the mcp, or `agentduet-desktop status`.",
                       type(exc).__name__, exc)

    logger.info("Secretary up for %s", owner_name())

    # SECRETARY_CHANNEL=0 runs the owner site WITHOUT connecting to AgentDuet. One client per
    # connector is a hard constraint — a second one makes call.answer() race — so anything that
    # needs the local decision path but not real inbound traffic (the behaviour suite, offline
    # work on the site) must be able to skip the channel rather than fight the live daemon.
    if os.getenv("SECRETARY_CHANNEL", "1") == "0":
        logger.info("AgentDuet channel disabled (SECRETARY_CHANNEL=0) — site only")
        status.set_channel("off", "SECRETARY_CHANNEL=0")
        while True:
            await asyncio.sleep(3600)

    # SETUP MODE — the same site-only state as above, entered from the instance's own state
    # instead of from an env var.
    #
    # WHY THIS IS A MODE AND NOT A DETAIL. The process an owner double-clicks is BOTH the
    # installer and the daemon: it serves setup.html and, until now, also took the connector
    # while doing it. One client per connector is a hard constraint, so that is what forces
    # `service.handover` to start the installed copy and have it WAIT on this pid — the installer
    # is holding the one client the connector allows. Nothing in an unfinished setup needs the
    # channel: the only thing anyone can do with this process is fill in the pages.
    #
    # DERIVED FROM STATE, NOT FROM A FLAG. Whoever double-clicks a downloaded binary passes no
    # arguments at all — cli.main() turns an empty argv into `run` — so a `--setup` flag would
    # have to be supplied by the one person who is not there to supply it. State also answers
    # correctly for the case a flag cannot see: an install whose model key was removed stops
    # answering strangers with no brain, instead of holding the channel open.
    #
    # A POLL, NOT ONE CHECK, for the same reason the connector wait below is one: attaching a
    # model and recording a name both happen in THIS process (the setup pages write os.environ as
    # well as .env), so the channel can open the moment setup finishes — pressing Done and
    # handing over to the installed copy is how the owner tidies up, not how they get a channel.
    # cannot_answer, NOT setup_pending: only "answering is impossible" may close the channel.
    # setup_pending is a superset that also covers a blank name, and gating on it took a LIVE
    # secretary off the air — this machine's instance had a working key, a claimed connector and
    # inbound calls being answered, with no name ever filled in. See owner.cannot_answer.
    from . import owner
    if why := owner.cannot_answer():
        logger.info("Cannot answer anyone yet (%s) — serving the setup pages only. The AgentDuet "
                    "channel stays closed and the connector is not claimed until this is fixed.",
                    why)
        status.set_channel("setup", why)
        while owner.cannot_answer():
            await asyncio.sleep(CONNECTOR_POLL_SECONDS)
        logger.info("A model is attached — opening the channel, no restart needed.")

    # No connector configured — the ordinary state on a machine that has just installed this.
    # Entering the retry loop would fill the log with connection failures for a channel the
    # owner has not been given yet, which reads as broken rather than as not-yet-set-up.
    # No connector yet — the ordinary state of a fresh install. Entering the retry loop would
    # fill the log with failures for a channel the owner has not been given yet, which reads as
    # broken rather than as not-yet-set-up.
    #
    # But WAIT for one rather than sleeping forever. The owner adds a connector on the settings
    # page minutes after first launch, and `save_connector` puts it in os.environ of THIS
    # process — so the only thing that made a restart necessary was this branch never looking
    # again. The symptom was a chip reading "not connected" while the credential sat there
    # correct, advising the owner to check a network that was fine.
    if not connector_ready():
        logger.info("No AgentDuet connector configured (AGENTDUET_API_KEY / "
                    "AGENTDUET_CONNECTOR_UUID) — running the owner's view only. "
                    "Everything local works; inbound messages need a connector. "
                    "Waiting for one to be added.")
        status.set_channel("unset")
        while not connector_ready():
            await asyncio.sleep(CONNECTOR_POLL_SECONDS)
        logger.info("A connector was added — connecting without a restart.")

    status.load_number(SESSIONS)      # so a restart shows the number before new traffic
    delay = 5
    while True:
        try:
            status.set_channel("connecting")
            await run_channel()
        except Exception as exc:
            logger.warning("AgentDuet channel unavailable (%s: %s) — owner site stays up, "
                           "retrying in %ds", type(exc).__name__, exc, delay)
            status.set_channel("retrying", f"{type(exc).__name__}: {str(exc)[:120]}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)      # back off, but keep trying: the VPN may return
        else:
            delay = 5                        # a clean exit from run_forever: reconnect promptly


def run() -> int:
    """Synchronous entry point, for `agentduet-desktop run`."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(run())
