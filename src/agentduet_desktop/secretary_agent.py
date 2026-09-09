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

import asyncio
import json
import logging
import os
import sys
import pathlib
from datetime import datetime

from dotenv import load_dotenv

from agentduet import (
    CallAudioConfig,
    IncomingMessage,
    InboundCallMode,
    TriggerConditionsBuilder,
    Network,
    SendDduetMessage,
    SendWAMessage,
    Session,
    SessionManager,
    SessionManagerConfig,
    new_session_id,
)

# NOT `brain` and `people` — see the message handler, which imports them where it uses them.
# At module level they made carrying a call load the answering agent and the five modules behind
# it, so a product that answers nobody paid for the whole agent at startup. tests/test_boundary.py
# fails if they come back.
from . import paths
from . import status

HERE = pathlib.Path(__file__).parent
RUN = paths.RUN
LOG = RUN / "queries.jsonl"
SESSIONS = RUN / "sessions.json"   # asker -> who we may reply to (read by secretary_mcp)
OUTBOX = RUN / "outbox.jsonl"      # owner replies queued by secretary_mcp.reply_to

# Explicitly the INSTANCE file. A bare load_dotenv() searches the CWD and found the
# install-dir .env left behind by the migration — and since load_dotenv never overrides an
# already-set variable, that stale copy won every race against the real config. The daemon
# then ran a model the owner had already replaced, reporting nothing wrong.
load_dotenv(paths.ENV_FILE)
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


#: How long the owner's own turn may run before it says something. Twelve seconds is past the
#: fast path — a plain question answers in one to five — and well inside the point where someone
#: staring at a phone decides nothing is coming.
OWNER_ACK_AFTER = 12


def _owner_draft_note(chat, answer: str) -> str:
    """Wrap a drafted reply so the owner knows who it is for and how to send it.

    Only when the turn actually produced an unsent draft — an ordinary answer is returned
    untouched, because appending "reply send" to "you have one message" would be an invitation
    to send something that does not exist.
    """
    if chat is None or not answer:
        return answer
    draft = chat.last_draft()
    if not draft or draft.strip() not in answer:
        return answer
    from . import tools
    who = chat.last_draft_for()
    name = (tools._display_for(who) or who) if who else ""
    head = f"Draft for {name}:" if name else "Draft:"
    return f'{head}\n\n{draft}\n\nReply "send" to send it.'


async def _owner_answer(question: str) -> str:
    """What the owner's assistant says to the owner's own message. Always returns something.

    NOT `brain.handle_query`: that is the ASKER path, which answers a stranger from public
    knowledge and cannot act. This is the owner's own surface — the same object the app's chat
    panel drives, which is why `owner_chat()` is shared: ask from the phone, open the app, and
    it is one thread.

    Sending is the caller's job, because `session_for` is a closure inside `register()`.

    A FAILURE IS REPORTED, never swallowed. The owner is standing at their phone waiting, and
    silence there is indistinguishable from the message never having arrived — which is exactly
    the hour that was just spent finding out messages were going to another connector.
    """
    from . import assistant
    chat = assistant.owner_chat()
    if chat is None:
        return ("No model is attached, so I cannot answer that yet. Attach one in Settings on "
                "the machine running AgentDuet.")
    try:
        # NAMED, so the owner's own thread says which of these they asked from their phone.
        # The reply is tagged by the same field: it is the half that went somewhere.
        out = await chat.turn(question, via="whatsapp")
        return (out or {}).get("reply") or "I had nothing to say to that."
    except Exception as exc:
        logger.exception("the owner's assistant failed on a message from their own number")
        return f"That did not go through — {exc}"


def _first_text(payload: dict) -> str:
    """The message body, whichever shape it arrives in.

    THE INBOUND WA SHAPE IS NOW CONFIRMED, from a real message on 2026-09-07 — read out of the
    platform's own logs rather than guessed. `wss-edge` passes Meta's webhook envelope straight
    through (`WaInboundController` forwards `request.content.content`), so the body is nested
    four levels down:

        entry[0].changes[0].value.messages[0].text.body

    None of the three shapes this function originally accepted matched that, so the first real
    message would have been logged as unreadable — the guesses were `text.body`, a top-level
    `messages` array, and the older Nexus `parts`. The Meta envelope is now tried FIRST, and the
    rest are kept: `parts` is DDUET's form and still live on that channel, and the flatter Meta
    shapes cost nothing to accept in case the relay ever unwraps one for us.

    Every level is iterated rather than indexed at [0]. Meta documents `entry` and `changes` as
    arrays and batches them under load, so taking the first would silently drop the rest — and
    `field` must be checked, because a status webhook shares this envelope with no `messages`
    at all (wss-edge drops those, but nothing guarantees we are the only producer).
    """
    for entry in payload.get("entry", []) or []:              # Meta webhook envelope
        for change in entry.get("changes", []) or []:
            if change.get("field") not in (None, "messages"):
                continue
            for m in (change.get("value") or {}).get("messages", []) or []:
                if isinstance(m.get("text"), dict) and m["text"].get("body"):
                    return m["text"]["body"]
    if isinstance(payload.get("text"), dict):                 # Meta, flat
        return payload["text"].get("body", "")
    for m in payload.get("messages", []) or []:               # Meta, wrapped
        if isinstance(m.get("text"), dict):
            return m["text"].get("body", "")
    for part in payload.get("parts", []) or []:               # Nexus MessageContent (DDUET)
        if part.get("type") == "text":
            return part.get("text", {}).get("body", "")
    logger.warning("could not read a text body from an inbound message — raw payload: %s",
                   json.dumps(payload, default=str)[:2000])
    return ""


def _display_name(dd) -> str:
    """Something readable for the owner, from the relay's display hint. Never an identity.

    Seen on the first real message: `{"email": "…@gmail.com", "name": "Stanley Leong"}`. Either
    key may be absent, so the name is preferred, the email is the fallback, and an empty string
    is a fine answer — the app falls back to the uid rather than inventing anything.
    """
    meta = getattr(dd, "user_metadata", None) or {}
    return str(meta.get("name") or meta.get("email") or "").strip()


def _dduet_system(content: dict) -> str:
    """The systemType when a DDUET frame is an EVENT rather than something a person said.

    Nexus relays conversation lifecycle as ordinary inbound with a `system` part — the first one
    seen was CONVO_CREATED, carrying `dataJson: {"title": "hi hi"}`, where the title is the
    proto's "first sentence of the first message". So the words are in there, and they are still
    not a message: answering a creation event means replying to the fact that a conversation
    exists.

    Without this the handler asked the model to answer an empty string, and it loaded a 7.6 GB
    model to do it.
    """
    for part in (content or {}).get("parts", []) or []:
        if part.get("type") == "system":
            return part.get("system", {}).get("systemType", "system")
    return ""


#: Openings recovered from CONVO_CREATED that are WAITING to see whether nexus also sends the
#: real message. Keyed by session uid.
#:
#: The title arrives first and the text frame follows about 35ms later, so acting on the title
#: immediately records the same message twice — once truncated to its first sentence, once
#: whole. Waiting a moment costs nothing on a channel where nobody is being answered live, and
#: it is the only way to tell "nexus sent no text frame" from "it has not sent it yet".
_OPENING_WAIT_SECONDS = 3
_pending_openings: dict[str, str] = {}


def _dduet_opening(content: dict) -> str:
    """The first message, when Nexus delivered only the event announcing the conversation.

    A FALLBACK, NOT THE NORMAL PATH — and the first version of this got that backwards.

    On 2026-08-28 "hi hi" opened a session and CONVO_CREATED was the only frame that ever came,
    so this was written believing the opening message is never relayed. On 2026-08-31 a second
    new conversation showed the truth: the text frame DOES arrive, about 35ms after the event,
    same sessionUid and its own msgUid. Acting on the title immediately therefore recorded the
    message twice — once as the title's first sentence, once whole.

    So the title is held for `_OPENING_WAIT_SECONDS` and used only if no text frame turns up.
    That still covers the "hi hi" case, whatever caused it, without inventing a duplicate in
    the common one.

    The title is the proto's "first sentence of the first message", so what it recovers is the
    opening SENTENCE. Fine as a fallback; not something to prefer over the real frame.
    """
    for part in (content or {}).get("parts", []) or []:
        if part.get("type") != "system":
            continue
        system = part.get("system", {})
        if system.get("systemType") != "CONVO_CREATED":
            continue
        try:
            return str(json.loads(system.get("dataJson") or "{}").get("title") or "").strip()
        except (ValueError, TypeError):
            return ""
    return ""


def _dduet_text(body: str, *, to: str, session_uid: str, ba_uid: str) -> SendDduetMessage:
    """One outbound DDUET (Nexus BaChat) text.

    `to` is the other party's Nexus ACCOUNT UID — not an email; the relay carries account uids
    only. `session_uid` is the Nexus conversation, and a multi-BA connector must send `ba_uid`
    back or the server answers AMBIGUOUS_BA. Ours backs more than one BA (Hallie, 2026-08-28:
    one BA has one connector, but one connector can serve several), so it is always passed.

    Same reason as `_wa_text`: both senders — answering an asker, and delivering a reply the
    owner queued — must build the same object, and two literals drift.
    """
    return SendDduetMessage.text(body, participant=to, session_uid=session_uid, ba_uid=ba_uid)


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


def remember_session(asker: str, subscriber: str, *, network: str = "WA",
                     session_uid: str = "", ba_uid: str = "", display: str = "") -> None:
    """Remember who we may reply to, and everything needed to build that reply.

    A reply needs the subscriber the message arrived on — for WhatsApp that is the Business
    Account's `phone_number_id`, which nothing else exposes. Persisted so the owner can answer
    later through the MCP tool, in a process that never saw the inbound message.

    THE NETWORK IS STORED BECAUSE THE REPLY IS SHAPED BY IT. WhatsApp routes on the participant
    alone; DDUET needs the Nexus `session_uid` and, on a connector serving several BAs, the
    `ba_uid` — without which the server answers AMBIGUOUS_BA. A queued reply built as the wrong
    kind does not degrade, it fails, so the shape has to survive the process that saw the
    inbound message.

    On WhatsApp two parallel conversations with one person are ONE thread here, which is correct
    where the number IS the person. On DDUET the session uid keeps them apart.
    """
    RUN.mkdir(exist_ok=True)
    data = json.loads(SESSIONS.read_text()) if SESSIONS.exists() else {}
    row = {
        "subscriber": subscriber,
        "network": network,
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    if session_uid:
        row["session_uid"] = session_uid
    if ba_uid:
        row["ba_uid"] = ba_uid
    # A NAME TO SHOW, NEVER A NAME TO KEY ON. The identity is the account uid and stays the
    # account uid — the relay's `user_metadata` is documented as "either key possibly absent",
    # is null on every BA-authored relay, and flips to the staff member's address when a
    # colleague replies as the BA. So it is carried alongside for the owner to read, and
    # nothing looks anyone up by it. Without this the app titles a conversation
    # "d7553b51-6567-11f1-a64a-a9511a89ac64".
    if display:
        row["display"] = display
    data[asker] = row
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
    from . import connector, oauth
    kwargs = dict(call_audio=CallAudioConfig(sample_rate=connector.CALL_SAMPLE_RATE))
    if oauth.signed_in():
        # TOKEN ONLY. The SDK rejects a config carrying both — "token_provider is a standalone
        # auth mode: remove api_key / connector_uuid / cert_path". The connector is a CLAIM
        # inside the token, so passing it alongside is at best redundant and at worst a second
        # source of truth that can disagree with the credential actually presented.
        kwargs["token_provider"] = oauth.token_provider
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

        async def use_opening_if_unclaimed(dd, msg, asker: str) -> None:
            """The CONVO_CREATED title, used only if nexus never sent the real message.

            Nexus normally follows the event with a text frame about 35ms later, and that frame
            pops this conversation off `_pending_openings`. So this wakes up, finds nothing to
            do, and returns — which is the expected outcome and not a failure.
            """
            await asyncio.sleep(_OPENING_WAIT_SECONDS)
            opening = _pending_openings.pop(dd.session_uid, None)
            if not opening:
                return                      # the real message arrived; it was handled as itself
            logger.info("DDUET: no text frame followed the conversation title on session %s — "
                        "using the title as the first message", dd.session_uid)

            from . import brain, owner, people
            verified = people.default_verified("DDUET")
            logger.info("← %s: %s", asker, opening)
            if owner.messages() == owner.MESSAGES_CARRY:
                brain.record(asker, opening, "carried", "", "", network="DDUET",
                             verified=verified, conversation=dd.session_uid)
                logger.info("[DDUET] %s → carried to the owner, not answered", asker)
                return
            result = await brain.handle_query(asker, opening, "DDUET", verified=verified,
                                              conversation=dd.session_uid)
            logger.info("→ [%s] %s", result["outcome"], result["reply"])
            send = await (await session_for(msg.subscriber)).send_message(
                _dduet_text(result["reply"], to=asker, session_uid=dd.session_uid,
                            ba_uid=dd.ba_uid))
            if not send.success:
                logger.error("reply failed: %s (%s)", send.error_code, send.error_content)

        @sm.on_incoming_message
        async def on_message(msg: IncomingMessage):
            if msg.network not in (Network.WA, Network.DDUET):
                # SAY SO. This used to `return` in silence, which meant a message on another
                # network left no trace at all — indistinguishable from the channel being dead,
                # and impossible to test against. TELCO arrives as a CALL through
                # voice.register(), not here.
                logger.info("ignored a %s message from %s (subscriber %s) — only WA and DDUET "
                            "are answered on this channel", msg.network, msg.participant.value,
                            msg.subscriber)
                return

            # `participant` is the OTHER party and nexus keeps it sticky across a conversation,
            # so it is who to answer on both channels. On WA it is their phone number; on DDUET
            # it is their Nexus account uid — an identifier, never an email, because the relay
            # carries account uids only.
            asker = msg.participant.value
            conversation = None
            dd = msg.dduet if msg.network is Network.DDUET else None
            if dd is not None:
                # THE WHOLE PAYLOAD, ONCE PER MESSAGE, WHILE THIS IS NEW. We have never seen a
                # real inbound DDUET frame — `user_metadata` is documented as {email, name} with
                # "either key possibly absent", and nothing but a real message settles which
                # arrives. Narrow this to a summary once it has been seen a few times; it is
                # deliberately noisy for now.
                logger.info("DDUET inbound raw: %s", json.dumps(dd.raw, default=str)[:4000])

                # AUTHORSHIP IS THE TWO UIDS, NEVER THE `sender` ROLE STRING — every BA member
                # relays as AGENT, so a role cannot tell our own staff from the customer.
                # user_uid == ba_uid means OUR OWN BA's side wrote this: a colleague replying as
                # the BA from web or mobile, which the relay delivers to us like any inbound.
                # Answering it would have the agent reply to its own organisation.
                if dd.user_uid and dd.user_uid == dd.ba_uid:
                    logger.info("DDUET: skipping a message authored by our own BA (%s) — a "
                                "human on our side replied, session %s", dd.ba_uid, dd.session_uid)
                    return
                conversation = dd.session_uid      # a real per-conversation key, unlike WA
                event = _dduet_system(dd.content)
                opening = _dduet_opening(dd.content) if event else ""
                if opening:
                    # HOLD IT, do not act on it. Nexus usually sends the real message a moment
                    # later; only when it does not is the title all we will ever get.
                    _pending_openings[dd.session_uid] = opening
                    remember_session(asker, msg.subscriber, network="DDUET",
                                     session_uid=dd.session_uid, ba_uid=dd.ba_uid,
                                     display=_display_name(dd))
                    asyncio.create_task(use_opening_if_unclaimed(dd, msg, asker))
                    return
                if event:
                    # Remember the session ANYWAY. This frame carries everything a reply needs —
                    # participant, session_uid, ba_uid — so recording it here means the owner can
                    # answer the conversation even if the person never sends another word.
                    remember_session(asker, msg.subscriber, network="DDUET",
                                     session_uid=dd.session_uid, ba_uid=dd.ba_uid,
                                     display=_display_name(dd))
                    logger.info("DDUET: %s event on session %s — noted, not answered",
                                event, dd.session_uid)
                    return
                # A REAL TEXT FRAME CANCELS ANY HELD OPENING for this conversation. It is the
                # same message, whole rather than clipped to its first sentence, so the title
                # copy must never also land.
                _pending_openings.pop(dd.session_uid, None)
                question = _first_text(dd.content)
            else:
                question = _first_text(msg.payload)
            logger.info("← %s: %s", asker, question)

            # THE OWNER, WRITING TO THEIR OWN AGENT — not a stranger who needs answering.
            #
            # Filing the owner as an asker is wrong twice over: they appear in `people/` as
            # someone to be answered, and what they wanted was their assistant.
            #
            # KNOW WHAT THIS OPENS, because it is the one door this product otherwise does not
            # have. The owner's assistant holds the owner's tools; until now it was reachable
            # only from a loopback page with a per-machine token. This adds a second way in,
            # authenticated by caller id. That is a real claim — Meta authenticates the sending
            # account at registration, which is why `SELF_VOUCHING_NETWORKS` already trusts WA
            # for identity — but it is weaker than the token, because a hijacked WhatsApp
            # account inherits it. So:
            #
            #   * WA ONLY. On DDUET the participant is an account uid, never a number, so the
            #     comparison has no subject and the owner path must not be reachable there.
            #   * FAIL CLOSED. `## Phone` empty means nobody matches — an unset setting must
            #     never promote the first person who writes.
            #   * SAID OUT LOUD in the log, every time, so a message that took this path is
            #     visible rather than inferred.
            from . import assistant as assistant_module, owner as owner_settings
            if dd is None and owner_settings.is_own_number(asker):
                logger.info("[WA] %s is the owner's own number — to their assistant, "
                            "not filed as a person", asker)
                # SIXTY-TWO SECONDS OF SILENCE READS AS BROKEN, and that is measured: a
                # "help me reply" turn ran eight hosted-model round trips at 2-17s each and the
                # owner reported it stuck while it was still working. In the app a typing
                # indicator covers this; on WhatsApp there is nothing, and the owner cannot tell
                # a slow turn from a dead daemon.
                #
                # One extra message, and only when it is actually slow — a fast turn (most of
                # them, 1-5s) sends nothing but its answer. `shield` because the timeout must
                # not cancel the work it is waiting on.
                chat_now = assistant_module.owner_chat()
                # "SEND IT" IS CODE ON BOTH SURFACES NOW. Reaching the model with a send
                # instruction is how the owner got told "the assistant only reads" — true of
                # the model, which has no send tool by design, and false of the product.
                sent = assistant_module.send_if_asked(chat_now, question)
                if sent is not None:
                    back = await (await session_for(msg.subscriber)).send_message(
                        _wa_text(sent, to=asker))
                    if not back.success:
                        logger.error("could not confirm the send to the owner: %s",
                                     back.error_code)
                    return

                # SHOW IT BEFORE THINKING ABOUT IT. The turn used to be recorded only when it
                # finished, so a question asked from the phone left the owner's own thread
                # silent for the whole turn and then both halves landed together.
                if chat_now is not None:
                    chat_now.begin(question, via="whatsapp")
                work = asyncio.create_task(_owner_answer(question))
                try:
                    answer = await asyncio.wait_for(asyncio.shield(work), OWNER_ACK_AFTER)
                except asyncio.TimeoutError:
                    logger.info("[WA] the owner's turn is past %ss — acknowledging it",
                                OWNER_ACK_AFTER)
                    ack = await (await session_for(msg.subscriber)).send_message(
                        _wa_text("Working on that.", to=asker))
                    if not ack.success:
                        logger.error("could not acknowledge the owner: %s", ack.error_code)
                    answer = await work

                # A DRAFT NEEDS ITS RECIPIENT AND ITS VERB, on a channel that has neither a
                # label nor a button. In the window the draft carries "Draft reply to Stanley
                # Leong — not sent" and a Send control; over WhatsApp the owner would otherwise
                # receive a bare paragraph and have to guess both who it is for and how to
                # release it. This is the affordance, not an explanation of one.
                answer = _owner_draft_note(chat_now, answer)
                back = await (await session_for(msg.subscriber)).send_message(
                    _wa_text(answer, to=asker))
                if not back.success:
                    # The owner asked and got nothing. Loud, because they are waiting.
                    logger.error("could not reply to the owner: %s (%s)",
                                 back.error_code, back.error_content)
                return

            remember_session(asker, msg.subscriber,
                             network=("DDUET" if dd is not None else "WA"),
                             session_uid=(dd.session_uid if dd is not None else ""),
                             ba_uid=(dd.ba_uid if dd is not None else ""),
                             display=(_display_name(dd) if dd is not None else ""))
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
            from . import people
            verified = people.default_verified(network)
            from . import brain
            from . import owner

            # CARRY: RELAY IT, DO NOT ANSWER IT. The same shape as a carried call — two humans
            # talk, we are the junction, nobody is impersonated. The message is recorded so the
            # owner can read it and reply from the app, and the session is already stored above,
            # so their reply has everything it needs to go back out.
            #
            # This mode did not exist until 2026-08-28. Before it, `on_incoming_message` went to
            # handle_query unconditionally, so an install with `## Calls: carry` — an owner who
            # had explicitly said the agent must not speak for them — still had it answer their
            # chats. Found on the first real DDUET conversation.
            if owner.messages() == owner.MESSAGES_CARRY:
                brain.record(asker, question, "carried", "", "", network=network,
                             verified=verified, conversation=conversation)
                logger.info("[%s] %s → carried to the owner, not answered", network, asker)
                return

            # WhatsApp has no conversation key, so memory falls back to the identity — which
            # on that channel IS the person. DDUET has one: the Nexus session uid.
            result = await brain.handle_query(asker, question, network, verified=verified,
                                              conversation=conversation)
            reply, outcome = result["reply"], result["outcome"]
            logger.info("→ [%s%s] %s", outcome,
                        f" {result['reason']}" if result["reason"] else "", reply)

            outbound = (_dduet_text(reply, to=asker, session_uid=dd.session_uid,
                                    ba_uid=dd.ba_uid) if dd is not None
                        else _wa_text(reply, to=asker))
            send = await (await session_for(msg.subscriber)).send_message(outbound)
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
                    # Built from what was stored at inbound time, because this process never
                    # saw the message. A DDUET reply sent as a WhatsApp one does not degrade —
                    # it fails — so the network decides the shape here too.
                    if s.get("network") == "DDUET":
                        queued = _dduet_text(item["text"], to=item["asker"],
                                             session_uid=s.get("session_uid", ""),
                                             ba_uid=s.get("ba_uid", ""))
                    else:
                        queued = _wa_text(item["text"], to=item["asker"])
                    result = await (await session_for(s["subscriber"])).send_message(queued)
                    if result.success:
                        # NO SECOND LOG ROW. `reply_to` already wrote the owner_reply row when
                        # the owner pressed send; this is the DELIVERY of that same message, and
                        # recording it again put the reply in the conversation twice — invisible
                        # until the app grew a thread view, then immediately obvious.
                        logger.info("→ (from owner) %s: %s", item["asker"], item["text"])
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
            # AND THE CALLS THIS LINE PLACES. Without this the connector never announces them,
            # so `on_outgoing_call` cannot fire however carefully it is registered — the app
            # simply sees nothing when the owner rings someone from their own phone.
            builder = builder.outbound_call(True)
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
                        "inbound_call=%s, outbound_call=%s",
                        "ALL" if calls_on else "off", calls_on)
        except Exception as exc:
            logger.warning(
                "could not set trigger conditions (%s: %s) — carrying on with whatever the "
                "connector already has. If nothing arrives, that is the first thing to check.",
                type(exc).__name__, exc)

        asyncio.create_task(drain_outbox())
        # THE TRANSCRIPTION WORKER IS NOT STARTED HERE. It used to be, with a comment claiming
        # it was "started unconditionally" — and this function is reached only after `main`
        # waits for a connector, and is then re-entered by the reconnect loop below it. So the
        # claim was false in both directions: no connector meant no transcripts and no merge at
        # all, including for legs a previous run left unfinished, which is the case the comment
        # cited as the reason for starting it; and every channel drop started ANOTHER worker,
        # against a queue derived from the filesystem and documented as strictly sequential.
        # Moved to `main`, once, before the connector wait.

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
    """Whether the channel can be opened — by EITHER route, checked fresh every time.

    Delegates rather than repeating the test. This asked only for the two environment variables,
    which is right for an api-key install and wrong for a signed-in one: signing in provisions
    the connector server-side, so neither variable is ever set and this returned False forever.
    The daemon would have sat polling for a credential that was never going to arrive in the
    environment, while the owner watched a completed sign-in do nothing.

    Read fresh every time, never captured at startup: the settings page and the sign-in callback
    both write into the RUNNING process, so a credential arriving later must take effect without
    a restart."""
    from . import connector
    return connector.configured()


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
    except OSError as exc:
        # THE PORT BEING TAKEN IS A DIFFERENT FAILURE, and it must not be shrugged off. It means
        # ANOTHER DAEMON IS ALREADY RUNNING, and one connector has one client — a second racing
        # `call.answer()` is the documented way to break inbound. Carrying on also corrupts the
        # only way to manage them: this process has already written its pid to the pid file, so
        # `stop` now targets the impostor and leaves the real daemon serving stale code.
        #
        # Cost of getting this wrong, observed 2026-08-26: every `./dev.sh` for an hour started a
        # second daemon that could not bind, took over the pid file, and left the original
        # serving code from two hours earlier. Edits appeared to do nothing; tests of those edits
        # were meaningless.
        if getattr(exc, "errno", None) in (98, 48) or "address already in use" in str(exc).lower():
            logger.error("Port %s is already in use — another AgentDuet daemon is running. "
                         "Not starting a second one: one connector has one client, and two "
                         "would race for every call. Stop the other one first "
                         "(`agentduet-desktop stop`), or set SECRETARY_WEB_PORT.",
                         os.getenv("SECRETARY_WEB_PORT", "8899"))
            raise SystemExit(1)
        logger.warning("Owner site did not start (%s: %s) — carrying on. Inbound is unaffected; "
                       "reach this daemon through the mcp, or `agentduet-desktop status`.",
                       type(exc).__name__, exc)
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
    # RECORDINGS BECOME TRANSCRIPTS REGARDLESS OF THE CHANNEL, and exactly once. The queue is
    # derived from the filesystem, so this is a no-op when nothing was carried — and it is the
    # only thing that picks up legs a previous run left unfinished, which must not depend on a
    # connector the owner may have signed out of. Started here rather than in `run_channel`
    # because that is re-entered on every reconnect: one worker per drop, all draining the same
    # directory, against a queue whose whole design is one file at a time.
    from . import transcribe as _t
    asyncio.create_task(_t.worker())

    # IS THERE A NEWER BUILD? Started here for the same reason as the queue above — after the
    # site is bound, on the daemon's own loop, and regardless of the connector. It sleeps first
    # and asks GitHub four times a day; an offline machine gets a log line at info and nothing
    # else, which is the supported case rather than a fault.
    from . import update as _u
    asyncio.create_task(_u.worker())

    if not connector_ready():
        logger.info("No AgentDuet connector yet — running the owner's view only. "
                    "Sign in, or set AGENTDUET_API_KEY and AGENTDUET_CONNECTOR_UUID. "
                    "Everything local works; only inbound needs a connector. "
                    "Waiting for one to arrive.")
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
