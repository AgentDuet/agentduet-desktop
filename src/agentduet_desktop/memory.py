"""Short-term conversation memory.

Without it every message is standalone, so follow-ups fail. Observed live: "When would
a subscription renew?" was answered from product-hub, then "Can you help me change the
configured number of days?" escalated as not_grounded — "the configured number of days"
is meaningless on its own, and retrieval had nothing to key on.

TWO THINGS THAT MATTER

1. **The conversation key is the SESSION where one exists, otherwise the person.** No live
   channel supplies a session id today — WhatsApp and telco both key on the identity, which
   on those channels IS the person: one number, one thread. The distinction is kept because a
   channel that allows several concurrent conversations per identity (a web chat with a
   per-visit token, say) must not let them bleed into each other, and that is a property of
   the channel rather than something to rediscover when one arrives.

2. **Verified and unverified streams never share history.** Keyed with a `v:`/`u:`
   prefix. Otherwise an unverified claim of an identity could read back answers that
   were given to the *verified* person — including content from folders the claimant was
   never granted. History would become a way around the access model.

What the MODEL sees is short-lived on purpose: an hours-old exchange resurfacing in a new
question is noise, not context. What is RETAINED is longer, because the asker's own transcript
is restored from it — see the two windows below.
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timedelta

from . import paths

STORE = paths.RUN / "conversations.json"

# Two different windows, deliberately. RETENTION is how long the exchange still EXISTS —
# it is what the asker's own transcript is restored from, so a short value made their history
# vanish overnight while the owner side still had it. CONTEXT is how much of it may colour a
# NEW question (retry detection, query expansion, the action gate reading what the thread
# already is); a two-day-old question resurfacing there is noise, which is what the original
# single 120-minute TTL was protecting against.
# Owner-tunable: how long an external party can still see their own thread, and how much of it
# may colour a new question. Policy choices, not internals — so they read from the environment
# (set in $AGENTDUET_HOME/.env) with the shipped default as the fallback.
RETAIN_MINUTES = int(os.getenv("SECRETARY_RETAIN_MINUTES", 3 * 24 * 60))
CONTEXT_MINUTES = int(os.getenv("SECRETARY_CONTEXT_MINUTES", 120))
MAX_TURNS = 60                   # per conversation, oldest dropped (retention cap)
MAX_TEXT = 400                   # trim stored text; we need the gist, not transcripts


def key(asker: str, verified: bool, conversation: str | None = None) -> str:
    """`v:`/`u:` + identity + session id.

    The identity stays in the key even when a session id is present. Real Nexus session
    uids are unique per visitor so it makes no difference there, but any caller that
    reuses a conversation id across identities (the simulator, a future channel keyed on
    something coarser) would otherwise hand one person another's history.
    """
    who = (asker or "").strip().lower()
    convo = (conversation or "").strip()
    return f"{'v' if verified else 'u'}:{who}:{convo}"


def _load() -> dict:
    if not STORE.exists():
        return {}
    try:
        return json.loads(STORE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _fresh(turns: list[dict], minutes: int = RETAIN_MINUTES) -> list[dict]:
    cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return [t for t in turns if t.get("at", "") >= cutoff][-MAX_TURNS:]


def turns(k: str) -> list[dict]:
    """The retained conversation, oldest first — everything still on record."""
    return _fresh(_load().get(k, []))


def context(k: str) -> list[dict]:
    """Only the part recent enough to inform a NEW question.

    Kept separate from `turns()` so lengthening retention does not silently widen what the
    model treats as the live thread.
    """
    return _fresh(_load().get(k, []), CONTEXT_MINUTES)


def append(k: str, question: str, answer: str, reason: str = "") -> None:
    data = _load()
    data[k] = _fresh(data.get(k, [])) + [{
        "q": question[:MAX_TEXT],
        "a": answer[:MAX_TEXT],
        "reason": reason,
        "at": datetime.now().isoformat(timespec="seconds"),
    }]
    # drop conversations that have aged out entirely, so the file doesn't grow forever
    data = {kk: vv for kk, vv in ((kk, _fresh(vv)) for kk, vv in data.items()) if vv}
    STORE.parent.mkdir(exist_ok=True)
    STORE.write_text(json.dumps(data, indent=2))


def one_sided(t: dict) -> bool:
    """A turn nobody asked for: the owner's own reply, passed on after the fact.

    Stored through the same q/a shape for simplicity, with a placeholder question. Every
    reader has to know that placeholder is not something the person said — `as_prompt` was
    feeding the model `Them: (owner replied)`, so the agent believed they had said it.
    Keyed on the reason, which existing rows already carry, so no stored data needs fixing.
    """
    return t.get("reason") == "owner:delivered"


def as_prompt(k: str) -> str:
    """History block for the prompt, or '' when there is none."""
    ts = context(k)
    if not ts:
        return ""
    lines = [f"You (passing on the owner's own reply): {t['a']}" if one_sided(t)
             else f"Them: {t['q']}\nYou: {t['a']}" for t in ts]
    return ("EARLIER IN THIS CONVERSATION (most recent last) — use it to resolve what "
            "they mean by \"it\", \"that\", \"the ... you mentioned\":\n" + "\n".join(lines))


def as_brief_history(k: str) -> str:
    """History WITH times — the briefing has to be able to say when the ask changed.
    `as_prompt` deliberately omits times: the external-facing agent has no business
    narrating timestamps back at people.

    Also the CONTEXT window, not the retained one: the times here are HH:MM with no date, so a
    turn from two days ago would read as if it happened this morning."""
    ts = context(k)
    if not ts:
        return ""
    return "EARLIER TURNS (oldest first):\n" + "\n".join(
        f"[{t['at'][11:16]}] Owner replied: {t['a'][:160]}" if one_sided(t)
        else f"[{t['at'][11:16]}] Them: {t['q']}\n         You: {t['a'][:160]}"
        for t in ts)


def retrieval_query(k: str, question: str, lookback: int = 2) -> str:
    """Question enriched with recent ones, for the FIRST retrieval.

    The initial search keys off the raw message, so a referential follow-up ("change
    the configured number of days") retrieves nothing on its own. Prepending the recent
    questions restores the missing nouns. The model's own `search` calls can refine from
    there.
    """
    ts = context(k)[-lookback:]
    if not ts:
        return question
    return " ".join([t["q"] for t in ts] + [question])


def recent_questions(k: str, lookback: int = 5) -> list[str]:
    """Recent questions from this conversation, oldest first, excluding one-sided turns.

    An owner reply is stored with a placeholder question, so it must not be offered as
    something the asker could be retrying.
    """
    return [t["q"] for t in context(k)[-lookback:] if t.get("q") and not one_sided(t)]


def recent_reasons(k: str, lookback: int = 2) -> list[str]:
    """Gate reasons from the last few turns.

    The action gate reads one message at a time, so a referential revision ("actually
    make it 20%") carries no keyword of its own and slipped through. Knowing what the
    conversation already IS lets the gate keep classifying it correctly.
    """
    return [t.get("reason", "") for t in context(k)[-lookback:] if t.get("reason")]


def forget(k: str) -> None:
    data = _load()
    if data.pop(k, None) is not None:
        STORE.write_text(json.dumps(data, indent=2))
