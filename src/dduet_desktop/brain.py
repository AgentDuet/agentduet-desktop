"""The decision core — gates, grounded answer, escalation, logging.

Deliberately knows nothing about transports. `secretary_agent.py` (real DDUET) and the
local simulator (`web.py /sim`) both call `handle_query()`, so **the simulator exercises
the real path** rather than a parallel copy that can quietly drift. Whatever you see in
the simulator is what a real inbound message would get.
"""

import asyncio
import json
import logging
import os
import pathlib
import uuid
from datetime import datetime

from dotenv import load_dotenv

from . import llm
from . import paths

# brain owns the model client, so it loads the env itself. Only the daemon used to call
# load_dotenv(), which meant anything importing brain another way — the MCP server, a
# script — got no API key and silently degraded: replies then closed no threads at all.
# The key lives with the INSTANCE, not the install — an upgrade must not take it away.
load_dotenv(paths.ENV_FILE)

from . import identity
from . import folder_index
from . import asker_actions
from . import memory
from . import owner
from . import people
from . import permissions
from . import capabilities
from . import policy
from . import schedule
from .notify import escalate_to_owner

RUN = paths.RUN
LOG = RUN / "queries.jsonl"

logger = logging.getLogger("secretary.brain")

OWNER = owner.name()
OWNER_PRONOUN = owner.pronoun()
MODEL = os.getenv("SECRETARY_MODEL", "gemini-3.1-flash")

# Says the owner's name twice rather than using a pronoun. The model-generated replies get
# their pronoun from owner.pronoun() (configured, never inferred from a name); this constant
# was missed and hardcoded "They", which is the exact wording that was objected to.
HOLDING_REPLY = (f"Thanks — I've passed this to {OWNER} directly. "
                 f"{OWNER} will come back to you on it.")

def client():
    """The attached model, or None. Delegates to `llm` — brain names no vendor.

    Kept as a function on `brain` because `tools._which_close` and the retrieval loop use
    it as an availability check, and one indirection is cheaper than changing both.
    """
    return llm.client(MODEL)


def record(asker: str, question: str, outcome: str, reason: str, answer: str,
           sources: list[str] | None = None, network: str = "",
           briefing: dict | None = None, verified: bool = False,
           conversation: str = "") -> str:
    """Append one query. Returns its id — escalations need a stable handle so the
    owner can resolve *this* one, not "the most recent from that person"."""
    RUN.mkdir(exist_ok=True)
    qid = uuid.uuid4().hex[:8]
    with LOG.open("a") as f:
        f.write(json.dumps({
            "id": qid,
            "at": datetime.now().isoformat(timespec="seconds"),
            "asker": asker, "question": question, "network": network,
            "verified": verified, "conversation": conversation,
            "outcome": outcome, "reason": reason, "answer": answer,
            "sources": sources or [],     # provenance: which files grounded the answer
            "briefing": briefing or {},    # owner-facing only; never sent to the asker
            # Subject of the ask, from the briefing. Threads key on THIS rather than the
            # conversation: one chat routinely carries unrelated asks, which made the
            # headline and the reason come from different requests.
            "topic": (briefing or {}).get("topic", ""),
        }) + "\n")
    return qid


MAX_SEARCHES = int(os.getenv("SECRETARY_MAX_SEARCHES", "3"))


async def _text(prompt: str, think: bool = False) -> str:
    """One model call, plain text back, "" on any failure.

    `think` is for JUDGEMENT calls — classify, extract, decide — not for wording a reply.
    Reasoning roughly quintuples latency (0.5s -> 2.5s on qwen3.6-flash), which is worth
    paying to decide whether an ask commits the owner, and not worth paying to phrase a
    holding sentence. Providers that always think, or cannot, accept the flag and ignore it.

    This is now the ONLY place brain talks to a model — every other decision function goes
    through here or `_json`. That is what makes the provider swappable: `llm` decides
    whether "the model" is Gemini or Claude, and nothing above this line can tell.

    Failure is always "" so a caller can fall back to a fixed sentence — a capability
    confirmation must never depend on the model being reachable.
    """
    c = client()
    if c is None:
        return ""
    try:
        return await asyncio.to_thread(c.complete, prompt, think)
    except Exception as exc:
        logger.warning("model call failed: %s", exc)
        return ""


async def _json(prompt: str) -> dict | None:
    """`_text` plus JSON parsing. None means "could not tell" — never a guess.

    Thinking is OFF here, reversing a change made on a bad measurement. These callers run
    on the INBOUND path — capability extraction, withdrawal, reorganise — several per
    message. Thinking measured 2.5s on a toy prompt but 24.6s on a real one, so per-message
    it costs minutes. The judgement it protects is also the kind our design already backs
    with code: bounds and conflicts are checked in `capabilities`/`schedule`, not trusted
    to the model. Owner-reply-time judgement (`tools._which_close`) still thinks — it runs
    once, not per message.
    """
    raw = await _text(prompt)
    if not raw:
        return None
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        logger.warning("model returned non-JSON: %.80s", raw)
        return None


def _parse_search(text: str) -> str | None:
    """A {"search": "..."} action, or None if this is an answer."""
    t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not t.startswith("{"):
        return None
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return None
    q = obj.get("search")
    return q.strip() if isinstance(q, str) and q.strip() else None


async def _ask_model(question: str, asker: str, verified: bool,
                     mkey: str = "") -> tuple[str, list[str], list[str]]:
    """Grounded answer via an agentic retrieval loop.

    Returns (answer, sources, searches). The model may re-query the permitted sources
    with better vocabulary before answering — `search` routes through the same
    permitted-folder retrieval, so no phrasing can reach an ungranted folder.
    """
    # First retrieval uses the question enriched with recent ones — a referential
    # follow-up has none of the nouns the documents are indexed on.
    first_q = memory.retrieval_query(mkey, question) if mkey else question
    context, sources = permissions.context_for(asker, verified, first_q)
    # Declared capabilities are part of what may be SAID, not only what may be DONE. They are
    # not retrieved from a folder, so they are added unconditionally and labelled as their own
    # source — provenance stays honest about where the fact came from.
    caps = capabilities.disclosable()
    if caps:
        context += ("\n\n--- WHAT THE OWNER HAS AUTHORISED THIS AGENT TO ARRANGE ---\n"
                    + caps)
        sources = sources + ["capabilities (declared)"]
    if not permissions.folders_for(asker, verified):
        return policy.ABSTAIN, [], []

    # Profile applies only on a verified channel; shapes tone/scope, never quoted.
    profile = people.profile_for(asker, verified)
    profile_block = f"NOTES ON THIS PERSON (never reveal these):\n{profile}" if profile else ""
    # What this person's folders are about — lets the model separate "not written down"
    # from "not our subject", which used to collapse into one reason.
    scope = folder_index.scope_digest(permissions.folders_for(asker, verified)) or "(nothing)"

    c = client()
    if c is None:
        return policy.ABSTAIN, sources, []      # no key -> escalate rather than guess

    seen = list(sources)
    searches: list[str] = []
    extra = ""

    for _ in range(MAX_SEARCHES + 1):
        prompt = policy.SYSTEM_PROMPT.format(
            owner=OWNER, pronoun=OWNER_PRONOUN, asker=asker, knowledge=context + extra,
            profile=profile_block, history=memory.as_prompt(mkey) if mkey else "", scope=scope)
        # owner.prompt_block() was dead code: the owner could set a Voice and it reached the
        # model nowhere. Appended here rather than inside SYSTEM_PROMPT so it sits next to the
        # knowledge it applies to, and is absent when nothing is configured.
        block = owner.prompt_block()
        if block:
            prompt += f"\n\n{block}"
        if len(searches) < MAX_SEARCHES:
            prompt += policy.SEARCH_INSTRUCTIONS % (MAX_SEARCHES - len(searches), policy.ABSTAIN)

        out = await _text(f"{prompt}\n\nQuestion: {question}")

        query = _parse_search(out) if len(searches) < MAX_SEARCHES else None
        if not query:
            return out, seen, searches

        searches.append(query)
        more, more_src = permissions.context_for(asker, verified, query)
        fresh = [s for s in more_src if s not in seen]
        seen.extend(fresh)
        extra += f"\n\n--- additional results for '{query}' ---\n{more}"
        logger.info("  search %d: %r -> %d new source(s)", len(searches), query, len(fresh))

    return policy.ABSTAIN, seen, searches


async def _refusal(question: str, asker: str, verified: bool, scope: str) -> str:
    """A refusal that tells the sender what we CAN help with. Falls back to the flat
    holding reply on any failure — a broken refusal must not become a broken escalation."""
    c = client()
    if c is None or not scope:
        return HOLDING_REPLY
    try:
        out = await _text(policy.REFUSAL_PROMPT.format(
                owner=OWNER, scope=scope, question=question, pronoun=OWNER_PRONOUN))
        return out if 10 < len(out) < 600 else HOLDING_REPLY
    except Exception as exc:
        logger.warning("refusal generation failed: %s", exc)
        return HOLDING_REPLY


async def _withdrawal(question: str, asker: str, verified: bool) -> str | None:
    """If the sender is calling off one of their own asks, do it and confirm.

    Goes through `asker_actions`, not `tools`: the asker may act on their own requests,
    never on the owner's handling of them. Verified only — cancelling someone else's live
    request would be silent and destructive.
    """
    if not verified or not policy.WITHDRAW_HINT.search(question):
        return None
    asks = asker_actions.open_asks(asker, verified)
    if not asks:
        return None
    c = client()
    if c is None:
        return None
    listing = "\n".join(f"{i+1}. {a['question']}" for i, a in enumerate(asks[:10]))
    try:
        raw = await _text(policy.WITHDRAW_PROMPT.format(
                asks=listing, question=question))
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(raw)
        picked = [int(n) for n in out.get("withdraw", []) if str(n).isdigit()]
        ids = [asks[n - 1]["id"] for n in picked if 1 <= n <= len(asks)]
        if not ids:
            return None
        n = asker_actions.withdraw(asker, verified, ids, question)
        if not n:
            return None
        logger.info("  withdrew %d ask(s) for %s", n, asker)
        reply = str(out.get("reply", "")).strip()
        return reply or f"Understood — I have dropped that request. Nothing has gone to {OWNER}."
    except Exception as exc:
        logger.warning("withdrawal check failed: %s", exc)
        return None


async def _reorganise(question: str, asker: str, verified: bool) -> str | None:
    """Let the sender prioritise or merge their OWN asks by talking.

    Their set, their call. POC: no guard stops them marking everything urgent — priority
    is taken at face value rather than weighed against other senders.
    """
    if not verified or not policy.REORG_HINT.search(question):
        return None
    asks = asker_actions.open_asks(asker, verified)
    if not asks:
        return None
    c = client()
    if c is None:
        return None
    listing = "\n".join(f"{i+1}. {a['question']}" for i, a in enumerate(asks[:10]))
    try:
        raw = await _text(policy.REORG_PROMPT.format(
                owner=OWNER, pronoun=OWNER_PRONOUN, asks=listing, question=question))
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(raw)
        pick = lambda key: [asks[n - 1]["id"] for n in
                            (int(x) for x in out.get(key, []) if str(x).isdigit())
                            if 1 <= n <= len(asks)]
        did = 0
        did += asker_actions.set_priority(asker, verified, pick("urgent"), "urgent")
        did += asker_actions.set_priority(asker, verified, pick("normal"), "normal")
        did += asker_actions.merge(asker, verified, pick("merge"))
        if not did:
            return None
        logger.info("  reorganised %d ask(s) for %s", did, asker)
        return str(out.get("reply", "")).strip() or "Noted — I have updated your requests."
    except Exception as exc:
        logger.warning("reorganise failed: %s", exc)
        return None


async def _manage_asks(question: str, asker: str, verified: bool) -> str | None:
    """Tidy the sender's OWN outstanding asks, then report what changed.

    Executes rather than asks: when someone says "clean this up", handing the list back is
    the mess restated. It merges duplicates and retires superseded versions, then
    summarises so they can refine. Nothing is deleted — a retired ask stays in its thread,
    so the owner can still see the history.
    """
    if not verified or not policy.MANAGE_HINT.search(question):
        return None
    # Oldest first: the prompt reasons about what a LATER ask superseded, so the order
    # it is given must actually be chronological.
    asks = asker_actions.open_asks(asker, verified, oldest_first=True)
    if not asks:
        return f"You have nothing outstanding with {OWNER} at the moment."
    c = client()
    # Bucket by kind before asking. One judgement over 14 mixed items missed four
    # consecutive scheduling duplicates; four small same-kind judgements are much easier,
    # and it stays a single call.
    kinds: dict[str, list[str]] = {}
    for i, a in enumerate(asks[:12]):
        q = a["question"]
        # The stored reason can be stale — rules change. Anything that would be HANDLED
        # rather than escalated today (an instruction to the agent, a withdrawal, a
        # question about the agent) is not an outstanding ask, so say so instead of
        # letting an old label like NEGOTIATION vouch for it. "Combine the escalation
        # list on the discount" survived a cleanup precisely because its bucket claimed
        # it was a discount request.
        reason = policy.reclassify(q, a.get("reason", ""))
        kind = ("NOT-A-REQUEST (an instruction to you, not an ask of the owner)"
                if reason.startswith("stale:") or reason.startswith("policy:meta_")
                else reason.removeprefix("policy:").upper() or "OTHER")
        kinds.setdefault(kind, []).append(f"{i+1}. {q}")
    listing = "\n".join(f"[{k}]\n" + "\n".join(v) for k, v in kinds.items())
    if c is None:
        return f"Here is what is still open with {OWNER}:\n{listing}"
    try:
        raw = await _text(policy.CLEANUP_PROMPT.format(
                owner=OWNER, pronoun=OWNER_PRONOUN, asks=listing, question=question))
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(raw)
        ids = lambda ns: [asks[n - 1]["id"] for n in
                          (int(x) for x in ns if str(x).isdigit()) if 1 <= n <= len(asks)]

        merged = sum(asker_actions.merge(asker, verified, ids(grp))
                     for grp in out.get("merge", []) if isinstance(grp, list))
        retired = asker_actions.withdraw(asker, verified, ids(out.get("retire", [])),
                                         "superseded — tidied at sender's request")
        noise = asker_actions.withdraw(asker, verified, ids(out.get("not_requests", [])),
                                       "not an outstanding request — tidied")
        logger.info("  cleanup for %s: merged %d, retired %d, cleared %d non-requests",
                    asker, merged, retired, noise)
        return str(out.get("summary", "")).strip() or (
            f"I have tidied your requests: merged {merged}, retired {retired}, "
            f"removed {noise} that were not requests.")
    except Exception as exc:
        logger.warning("cleanup failed: %s", exc)
        return None


async def _contradiction(question: str, mkey: str) -> str | None:
    """A clarifying question when the sender's asks genuinely conflict, else None.

    Only a real contradiction — a clean revision ("actually make it 20%") supersedes and
    needs no question. Putting the choice back to the asker is safe because it is THEIR
    question being clarified; deciding the answer stays the owner's.
    """
    if not mkey or not memory.context(mkey):
        return None
    c = client()
    if c is None:
        return None
    try:
        raw = await _text(policy.CONTRADICTION_PROMPT.format(
                owner=OWNER, history=memory.as_brief_history(mkey), question=question))
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(raw)
        ask = str(out.get("ask", "")).strip()
        return ask if out.get("conflict") and 10 < len(ask) < 400 else None
    except Exception as exc:
        logger.warning("contradiction check failed: %s", exc)
        return None


async def _meta_reply(question: str) -> str:
    """Reply to a message about the agent or the owner's workflow.

    Kept apart from the normal refusal because that one offers help with unrelated
    subjects — which reads as a non-sequitur when someone asked to reorganise their own
    requests.
    """
    c = client()
    if c is None:
        return HOLDING_REPLY
    try:
        out = await _text(policy.META_REPLY_PROMPT.format(
                owner=OWNER, question=question, pronoun=OWNER_PRONOUN))
        return out if 10 < len(out) < 600 else HOLDING_REPLY
    except Exception as exc:
        logger.warning("meta reply failed: %s", exc)
        return HOLDING_REPLY


async def _meta_brief(question: str, asker: str) -> dict:
    """Briefing for a meta message — no retrieval, and no assumption of a deal.

    The generic briefing invented "what terms or discount structure to offer" from a
    message that contained no offer, because the reason said negotiation.
    """
    c = client()
    if c is None:
        return {}
    try:
        raw = await _text(policy.META_BRIEF_PROMPT.format(
                owner=OWNER, asker=asker, question=question))
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(raw)
        return {k: str(out.get(k, "")).strip() for k in ("wants", "facts", "decision", "draft")}
    except Exception as exc:
        logger.warning("meta briefing failed: %s", exc)
        return {}


async def _brief(question: str, asker: str, verified: bool, reason: str,
                 mkey: str = "") -> dict:
    """Turn a bare escalation into something the owner can act on.

    Retrieves in its own right: the action gate fires before any retrieval, so a
    "can you agree to 20% off" reaches the owner with no context attached unless we go
    and get it. Scoped to the asker's permitted folders — see policy.BRIEF_PROMPT.
    """
    c = client()
    if c is None:
        return {}
    context, _ = permissions.context_for(
        asker, verified, memory.retrieval_query(mkey, question) if mkey else question)
    prompt = policy.BRIEF_PROMPT.format(
        owner=OWNER, asker=asker, reason=reason.removeprefix("policy:"),
        knowledge=context or "(no permitted sources)", question=question,
        history=memory.as_brief_history(mkey) if mkey else "")
    try:
        raw = await _text(prompt)
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(raw)
        return {k: str(out.get(k, "")).strip()
                for k in ("topic", "wants", "facts", "decision", "draft")}
    except Exception as exc:                      # a failed briefing must not lose the escalation
        logger.warning("briefing failed: %s", exc)
        return {}


async def _try_capability(question: str, asker: str, verified: bool) -> dict | None:
    """The EVALUATE step: can the agent deal with this itself, under declared authority?

    Returns None when nothing applies — the caller then escalates exactly as before, which
    is the safe default and the behaviour for every ask until the owner declares something.

    The division of labour is the point. The MODEL reads the sentence: which capability,
    what time, how many. The CODE decides whether that is allowed: `check_bounds` and the
    conflict check are plain comparisons the model cannot argue with. Letting the model
    judge "is 21:20 close enough to closing" would make the bounds decorative.
    """
    caps = capabilities.candidates()
    if not caps or client() is None:
        return None

    listing = "\n".join(
        f"- {c['name']}: {c['what']}\n    limits: "
        + ", ".join(f"{k}={v}" for k, v in c["bounds"].items())
        + ("" if all(k in capabilities.CHECKED for k in c["bounds"])
           else "  (unlisted ones are advisory)")
        for c in caps)

    out = await _json(policy.CAPABILITY_PROMPT.format(
        owner=OWNER, caps=listing, question=question,
        now=datetime.now().isoformat(timespec="minutes")))
    if not out:
        return None
    name = (out.get("capability") or "").strip()
    if not name or not capabilities.get(name):
        return None

    at = (out.get("at") or "").strip()
    what = (out.get("what") or question)[:120]
    missing = [str(m) for m in (out.get("missing") or []) if str(m).strip()]
    minutes = capabilities.block_minutes(name)

    # Incomplete asks are not refusals — ask for the missing piece and stay out of the
    # owner's queue. Without this, "can I get a pizza?" with no time would escalate.
    # The framework knows what it needs in order to book: a time. Anything else the model
    # decides is "missing" — an address, a phone number, payment — is noise it invented, and
    # asking for it stalls an order that was already complete. Widening the capability's
    # description to cover "a delivery for tonight" was enough to make it start asking for an
    # address on an order that had item, quantity AND time.
    #
    # The prompt already forbids this. It did not hold, so the rule moves into code — same
    # reason bounds are checked here rather than trusted to the model.
    NOT_REQUIRED = ("address", "phone", "contact", "number", "payment", "card", "name",
                    "email", "location")
    missing = [m for m in missing if not any(w in m.lower() for w in NOT_REQUIRED)]

    # Check the bounds that CAN be checked before asking for anything more. "can I order 9
    # pizzas?" used to be answered "happy to arrange that — what time?", and the asker only
    # learned about the limit of 6 after supplying a time. The quantity was already known and
    # already over. check_bounds skips the checks whose inputs are absent, so a partial ask is
    # safe to test — and the same applies to an unverified asker on a verified-only capability.
    ok, why = capabilities.check_bounds(
        name, verified, quantity=out.get("quantity"), at=at, minutes=minutes)
    if not ok:
        # No alternative time offered here. A bounds refusal is not about availability —
        # suggesting "the nearest slot" to someone who asked for 9 pizzas answers a
        # question they did not ask and hides the actual limit.
        reply = await _text(policy.CAPABILITY_REFUSE_PROMPT.format(
            owner=OWNER, pronoun=OWNER_PRONOUN, question=question, why=why, alt=""))
        return {"reply": reply or f"Sorry — {why}.", "reason": f"capability:{name}:refused",
                "booked": None}

    if missing or not at:
        want = ", ".join(missing) or "what time you would like it"
        # Offer the clickable surface HERE specifically: the ask is a closed set of choices
        # (which pizza, which slot) and the asker has just been asked to type one out. The
        # chat stays open — this is an alternative, not a redirect.
        link = ""
        try:
            from . import canvas
            base = os.getenv("SECRETARY_PUBLIC_URL", "http://127.0.0.1:8899")
            link = f" You can also pick from the menu here: {base}{canvas.link_for(asker, verified, name)}"
        except Exception as exc:                       # never let a link break the reply
            logger.warning("canvas link unavailable: %s", exc)
        return {"reply": f"Happy to arrange that — could you tell me {want}?{link}",
                "reason": f"capability:{name}:incomplete", "booked": None}

    try:
        row = schedule.book(at, minutes, what, asker)
    except schedule.Conflict as clash:
        nxt = schedule.next_free(at, minutes,
                                 str((capabilities.get(name) or {}).get("bounds", {}).get("hours", "")))
        why = f"that time is already taken ({clash})"
        alt = f"The nearest free slot is {nxt.replace('T', ' ')}." if nxt else ""
        reply = await _text(policy.CAPABILITY_REFUSE_PROMPT.format(
            owner=OWNER, pronoun=OWNER_PRONOUN, question=question, why=why, alt=alt))
        return {"reply": reply or f"Sorry — {why}. {alt}".strip(),
                "reason": f"capability:{name}:conflict", "booked": None}

    logger.info("  capability %s booked %s for %s", name, row["at"], asker)
    reply = await _text(policy.CAPABILITY_CONFIRM_PROMPT.format(
        owner=OWNER, pronoun=OWNER_PRONOUN, what=what, at=row["at"].replace("T", " ")))
    return {"reply": reply or f"Confirmed for {row['at'].replace('T', ' ')}.",
            "reason": f"capability:{name}", "booked": row}


async def handle_query(asker: str, question: str, network: str,
                       verified: bool | None = None,
                       conversation: str | None = None) -> dict:
    """The whole decision, for one inbound message.

    Returns {reply, outcome, reason, sources, profile_applied, folders}.
    The caller delivers the reply however its transport requires.
    """
    # Verification travels with the IDENTITY, not the channel: DDUET carries both a
    # logged-in Nexus visitor and a walk-up one. Callers pass it explicitly; only when
    # they can't do we fall back to "does the transport vouch for it".
    if verified is None:
        # The channel vouches, or the owner has vouched for this specific address.
        verified = people.default_verified(network) or permissions.trusted(asker)

    # Resolve to a single identity. Verification is a property OF THE IDENTITY, not of this
    # message (see identity.py), so from here on `asker` IS the identity and `verified` is
    # readable from it — the two can no longer be paired wrongly by any caller.
    #
    # The concrete effect: an unverified sender who types someone else's address gets their
    # own identity instead of landing in that person's row. Twelve such asks were being
    # counted against the real Pauline, which verification had specifically not established.
    asker, verified = identity.resolve(asker, verified)
    sources: list[str] = []
    searches: list[str] = []
    # Keyed on the SESSION where the channel has one, and separately per verification
    # state so an unverified claim can never read back a verified person's history.
    mkey = memory.key(asker, verified, conversation)

    # "try again" carries no subject. Resolve it to whatever was last asked, so a retry
    # re-enters the path the original took. Without this it fell through to document
    # retrieval — `retrieval_query` glued it onto the previous question ("clean up the
    # escalation list try again"), found nothing, and escalated as not_grounded. The retried
    # request was a queue action, which lives nowhere in the documents.
    #
    # `question` is left untouched for the LOG and MEMORY: the transcript should show what
    # they actually typed. Only interpretation uses the resolved text.
    asked = question
    retried = policy.retry_of(question, memory.recent_questions(mkey))
    if retried:
        asked = retried
        logger.info("  retry: re-asking %r", retried[:60])

    # 1 · ACTION — commitment rules first, never delegated to the model, never
    #     overridden by a grant. Per-person overrides stack on top (verified only).
    # The owner may have answered while there was no channel to deliver on. DDUET is
    # passive, so this inbound is the first chance to pass it on — prepend it.
    held = asker_actions.take_pending_replies(asker) if verified else []
    held_prefix = ""
    if held:
        lines = " ".join(h["text"] for h in held)
        held_prefix = f"{OWNER} asked me to pass this on: {lines}\n\n"
        logger.info("  flushed %d held reply(ies) to %s", len(held), asker)

    # A withdrawal is not a new ask — handle it before the gates so it neither
    # escalates nor gets answered from documents.
    dropped = await _withdrawal(asked, asker, verified)
    if dropped:
        memory.append(mkey, question, dropped)
        qid = record(asker, question, "withdrawn", "asker:withdrew", dropped,
                     [], network, {}, verified, conversation or "")
        logger.info("[%s] %s → withdrawn", network, asker)
        return {"id": qid, "reply": dropped, "outcome": "withdrawn",
                "reason": "asker:withdrew", "sources": [], "searches": [],
                "verified": verified, "profile_applied": False,
                "profile_name": people.display_name(asker, verified),
                "folders": permissions.folders_for(asker, verified),
                "turns": len(memory.turns(mkey))}

    # Prioritising or merging their own asks — theirs to do, by talking.
    reorged = await _reorganise(asked, asker, verified)
    if reorged:
        memory.append(mkey, question, reorged, "asker:reorganised")
        qid = record(asker, question, "handled", "asker:reorganised", reorged,
                     [], network, {}, verified, conversation or "")
        logger.info("[%s] %s → reorganised own asks", network, asker)
        return {"id": qid, "reply": reorged, "outcome": "handled",
                "reason": "asker:reorganised", "sources": [], "searches": [],
                "verified": verified, "profile_applied": False,
                "profile_name": people.display_name(asker, verified),
                "folders": permissions.folders_for(asker, verified),
                "turns": len(memory.turns(mkey))}

    # Seeing or tidying their own asks is theirs to do — not a question for the owner,
    # so it neither escalates nor gets answered from documents.
    managed = await _manage_asks(asked, asker, verified)
    if managed:
        memory.append(mkey, question, managed, "asker:managed")
        qid = record(asker, question, "handled", "asker:managed", managed,
                     [], network, {}, verified, conversation or "")
        logger.info("[%s] %s → listed own asks", network, asker)
        return {"id": qid, "reply": managed, "outcome": "handled",
                "reason": "asker:managed", "sources": [], "searches": [],
                "verified": verified, "profile_applied": False,
                "profile_name": people.display_name(asker, verified),
                "folders": permissions.folders_for(asker, verified),
                "turns": len(memory.turns(mkey))}

    scope = folder_index.scope_digest(permissions.folders_for(asker, verified))
    must, reason = policy.check(
        asked, people.always_escalate(asker, verified) + owner.never_say(),
        memory.recent_reasons(mkey))
    is_meta = reason.startswith("policy:meta_")

    # 1b · CAPABILITY — can the agent deal with this itself, under declared authority?
    #
    # Tried whether or not the action gate fired. Gating it on `must` looked tidier and was
    # wrong: the gate is phrasing-dependent, so "I'd like to order a pizza for 7pm" fired
    # it but "Can I get a pizza at 7:15pm?" did not — the second fell through to retrieval
    # and escalated as not_grounded. An order is an order regardless of the question mark.
    #
    # Two things still win over a capability, and both are owner-authored: a meta message
    # (about the agent itself, not a request), and a person rule / never-say. Authority the
    # owner granted must not override a prohibition the owner wrote.
    capability = None
    if not is_meta and reason != "policy:person_rule":
        capability = await _try_capability(asked, asker, verified)
        if capability:
            must, reason = False, capability["reason"]

    if capability:
        # Already dealt with under declared authority: no retrieval (a booking is not a
        # disclosure) and no escalation (that is the whole point).
        reply = capability["reply"]
    elif must:
        # A genuine conflict in their own asks is theirs to resolve — ask, don't guess.
        clash = None if is_meta else await _contradiction(question, mkey)
        if clash:
            reply, reason = clash, "policy:contradiction"
        else:
            reply = (await _meta_reply(question) if is_meta
                     else await _refusal(question, asker, verified, scope))
    else:
        # 2 · DISCLOSURE — answered from the granted folders, with a retrieval loop
        reply, sources, searches = await _ask_model(asked, asker, verified, mkey)
        must, reason = policy.check_answer(reply)
        if must:
            reply = await _refusal(question, asker, verified, scope)
            reason = reason if sources else "policy:no_permitted_folders"

    if held_prefix:
        reply = held_prefix + reply

    # "acted" is its own outcome, not folded into "answered": the agent changed something
    # on the owner's behalf. That deserves to be visibly different from having replied.
    # Only a real booking counts — a refusal or a request for the missing time is a reply,
    # and labelling those "acted" would inflate the one signal that means "state changed".
    outcome = ("escalated" if must
               else "acted" if (capability and capability.get("booked")) else "answered")
    briefing: dict = {}
    if must:
        briefing = (await _meta_brief(question, asker) if is_meta
                    else await _brief(question, asker, verified, reason, mkey))
        escalate_to_owner(asker, question, reason)

    memory.append(mkey, question, reply, reason)
    qid = record(asker, question, outcome, reason, reply, [] if must else sources, network,
                 briefing, verified, conversation or "")
    logger.info("[%s%s] %s → %s%s%s", network, "" if verified else " anon", asker, outcome,
                f" ({reason})" if reason else "",
                f" after {len(searches)} search(es)" if searches else "")

    return {
        "id": qid,
        "reply": reply,
        "briefing": briefing,
        "outcome": outcome,
        "reason": reason,
        "sources": [] if must else sources,
        "searches": searches,
        "turns": len(memory.turns(mkey)),
        "verified": verified,
        "profile_applied": bool(people.profile_for(asker, verified)),
        "profile_name": people.display_name(asker, verified),
        "folders": permissions.folders_for(asker, verified),
    }
