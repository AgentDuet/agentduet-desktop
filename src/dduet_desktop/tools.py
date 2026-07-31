"""Owner tools — the single implementation behind BOTH owner surfaces.

`secretary_mcp.py` (MCP) and `web.py` (local site) are thin wrappers over this module.
Implementing a command twice is how the two faces drift apart, so they don't.

SECURITY: this module is the OWNER's tool registry — it can grant folder access and
send messages as the owner. The external-facing path (`secretary_agent.on_message`)
must never import or reach it. `test_isolation.py` asserts that.
"""

import hashlib
import json
import os
import pathlib
import re
from collections import Counter
from datetime import date, datetime, timedelta

from . import asker_actions
from . import capabilities
from . import llm
from . import identity
from . import folder_index
from . import people
from . import schedule
from . import policy
from . import permissions

from . import connector
from . import owner
from . import paths

RUN = paths.RUN
LOG = RUN / "queries.jsonl"
SESSIONS = RUN / "sessions.json"
OUTBOX = RUN / "outbox.jsonl"
RESOLVED = RUN / "resolved.json"        # escalation ids the owner has dealt with


def _resolved() -> dict:
    if not RESOLVED.exists():
        return {}
    try:
        return json.loads(RESOLVED.read_text())
    except json.JSONDecodeError:
        return {}


def _mark(ids: list[str], how: str) -> int:
    """Escalations need a lifecycle. Without one the queue only grows: replying to
    someone left the item sitting there, and the same question asked twice showed
    twice. Kept in a side file so the query log stays append-only."""
    if not ids:
        return 0
    RUN.mkdir(exist_ok=True)
    data = _resolved()
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for i in ids:
        if i not in data:
            data[i] = {"how": how, "at": now}
            n += 1
    RESOLVED.write_text(json.dumps(data, indent=2))
    return n


#: How much a reason should dominate a thread. A negotiation that happens to end with an
#: ungrounded question is still a negotiation — taking the LATEST message's reason filed a
#: live 20% discount thread as "not_grounded", which is what you'd triage past.
#:
#: Higher wins. Acts that bind the owner outrank everything, then the owner's own
#: standing rules, then things that merely need a decision about the conversation, then
#: the "we couldn't answer" family.
REASON_WEIGHT = {
    "policy:commitment": 90,
    "policy:negotiation": 90,
    "policy:legal_binding": 90,
    "policy:scheduling": 80,
    "policy:person_rule": 70,
    "policy:contradiction": 60,
    "policy:meta_queue": 50,
    "policy:meta_agent": 50,
    "policy:missing_knowledge": 30,
    "policy:out_of_scope": 20,
    "policy:not_grounded": 10,
    "policy:no_permitted_folders": 10,
    "policy:no_answer": 10,
}


def _weight(reason: str) -> int:
    return REASON_WEIGHT.get(reason, 40)


#: How many escalations the last read hid as aged-out. Reported in the UI so nothing
#: disappears without the owner being able to see that it did.
_aged_count = [0]


def aged_out() -> int:
    return _aged_count[0]


def _row_id(r: dict) -> str:
    """Stable handle for a logged row. Rows written before ids existed get one derived
    from their own content, so history stays resolvable rather than stuck in the queue."""
    if r.get("id"):
        return r["id"]
    seed = f"{r.get('at')}|{r.get('asker')}|{r.get('question')}"
    return "h" + hashlib.sha256(seed.encode()).hexdigest()[:7]


def open_escalations() -> list[dict]:
    """Open escalations as THREADS, newest first.

    An evolving ask is one item, not several. "Can you do 20%?" followed by "actually
    15%, over two years" is one negotiation the owner should see the current state of —
    previously each phrasing was its own queue entry, so the owner read three stale
    versions of the same request.

    A thread is keyed by (identity, verification state, conversation):

    - **verification state is part of the key**, so an unverified claim of an identity
      can never append to — or reshape — the real person's pending thread. That would let
      an impostor edit someone else's ask.
    - **conversation** is the natural unit of one evolving request; a new chat session is
      a new ask.

    The newest briefing wins (it was generated with the most context), while every
    question in the thread is kept so the owner can see how the ask developed.
    """
    # Owner sees the union: their own resolutions plus anything the sender withdrew.
    done = {**_resolved(), **asker_actions.withdrawn_ids()}
    priority, merged = asker_actions.reorg_state()
    threads: dict[tuple, dict] = {}
    aged: set = set()
    for r in rows():
        rid = _row_id(r)
        if r["outcome"] != "escalated" or rid in done:
            continue
        # Reasons are derived data — recompute against current rules rather than trusting
        # the label stored when the row was written.
        reason = policy.reclassify(r["question"], r["reason"])
        if reason == "stale:handled_today":
            continue        # would be handled today, so not an ask of the owner
        if policy.expired(reason, r["at"]):
            aged.add(rid)   # hidden from the queue, still in the log
            continue
        # A conversation can hold unrelated asks. Threading on the conversation alone
        # merged a real 20% discount request into a queue-management request, so the
        # headline and briefing described only the meta ask and the negotiation was
        # buried. Meta messages are about the workflow, never about the owner's business,
        # so they get their own thread. Substantive asks still merge, which is what
        # refinement (15% -> 20%) needs.
        family = "meta" if reason.startswith("policy:meta_") else "ask"
        # Group by SUBJECT, not by conversation. One chat routinely carries unrelated asks
        # — a discount and an MSA signature — and keying on the conversation made them one
        # thread, so the headline came from the latest message while the reason came from
        # the highest-weighted: the MSA request displayed as a negotiation, and marking the
        # discount urgent flagged the MSA too.
        #
        # An explicit merge by the sender still wins: they know what is the same ask.
        topic = (r.get("topic") or "").strip().lower()
        if rid in merged:
            key = (r["asker"], bool(r.get("verified")), merged[rid])
        elif topic:
            key = (r["asker"], bool(r.get("verified")), topic, family)
        else:
            # Pre-topic rows fall back to the old behaviour rather than all collapsing
            # into one bucket.
            key = (r["asker"], bool(r.get("verified")), r.get("conversation") or rid, family)
        t = threads.get(key)
        if t is None:
            threads[key] = {
                "ids": [rid], "count": 1, "asker": r["asker"],
                "verified": bool(r.get("verified")),
                "question": r["question"], "questions": [r["question"]],
                "reason": reason, "at": r["at"], "topic": topic,
                "briefing": r.get("briefing") or {},
                "urgent": rid in priority,
            }
            continue
        t["ids"].append(rid)
        t["count"] += 1
        t["at"] = r["at"]
        # Most significant reason wins, not the most recent one.
        if _weight(reason) > _weight(t["reason"]):
            t["reason"] = reason
        t["question"] = r["question"]                  # latest phrasing heads the item
        if r["question"] not in t["questions"]:
            t["questions"].append(r["question"])
        if rid in priority:
            t["urgent"] = True
        if r.get("briefing", {}).get("wants"):
            t["briefing"] = r["briefing"]              # refined by the newest briefing
    # Sender-flagged urgent floats up. POC: taken at face value.
    out = sorted(threads.values(), key=lambda t: (t.get("urgent", False), t["at"]),
                 reverse=True)
    _aged_count[0] = len(aged)     # surfaced so expiry is never silent
    return out


def rows() -> list[dict]:
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------- read

def owner_context(asker: str, messages: int = 8, focus: str = "") -> str:
    """What the owner's assistant should already know about the person on screen.

    Drafting a reply is a judgement, and the deficit was EVIDENCE, not reasoning: asked to
    "help me reply to Pauline about the discount" the assistant ran with `tools: []` and
    invented a 10% counter-offer — never reading her messages, and never seeing that the
    escalation briefing already carries a `draft` the external-facing agent wrote *with* her
    actual thread in front of it.

    So this hands it over up front rather than hoping it asks. Same fix that took
    `_which_close` from wrong-and-24s to right-and-0.8s.

    Reuses `open_escalations()` and `conversation_with()` — no second view of "what is open",
    which is exactly how the owner's two faces drifted apart before.

    `focus` is the thread the owner has NAVIGATED to in the queue. Without it the screen and
    the assistant disagreed: the owner walks to an item, says "draft a reply to this", and the
    assistant guesses which of five open threads is meant — observed picking a stale id and
    reporting "the thread ID is not matching". Pointing at something is a form of saying it.
    """
    if not asker:
        return ""
    threads = [g for g in open_escalations()
               if g["asker"].strip().lower() == asker.strip().lower()]
    out = [f"CONTEXT — the owner is looking at {who_label(asker, identity.is_verified(asker))}."]
    if threads:
        out.append(f"{len(threads)} thing(s) open with the owner:")
        for g in threads:
            here = focus and focus in (g.get("ids") or [])
            out.append(f"- [{g['ids'][0]}] {g['question']}"
                       + (f"   (subject: {g['topic']})" if g.get("topic") else "")
                       + ("   <-- THE OWNER IS LOOKING AT THIS ONE" if here else ""))
            # Later phrasings are how the ask was refined (15% -> 20%) — the part that decides
            # what a reply should actually say.
            for q in (g.get("questions") or [])[1:3]:
                out.append(f"    also asked: {q}")
            b = g.get("briefing") or {}
            for field in ("wants", "facts", "decision", "draft"):
                if b.get(field):
                    label = "SUGGESTED DRAFT" if field == "draft" else field
                    out.append(f"    {label}: {b[field]}")
    else:
        out.append("Nothing open with the owner.")
    d = pending_draft(asker)
    if d:
        out.append("")
        out.append(f"A DRAFT is already waiting for them, written {d['at']} and NOT sent"
                   + (f" (about: {d['about']})" if d.get("about") else "") + ":")
        out.append(f"  {d['text']}")
        out.append("If the owner says to send it, send THIS text — do not rewrite it. What they "
                   "approved is what should go out.")
    out.append("")
    out.append(conversation_with(asker, messages))
    out.append("")
    if focus and any(focus in (g.get("ids") or []) for g in threads):
        out.append(f"If the owner says \"this\", \"this one\", \"it\" or \"that request\" "
                   f"without naming a thread, they mean [{focus}] — the one marked above.")
        out.append("")
    out.append("Use the above instead of guessing. If you draft a reply, ground it in what "
               "they actually asked; prefer refining the suggested draft over writing a new "
               "one. Do not invent figures, dates or commitments that are not above.")
    return "\n".join(out)


def who_label(asker: str, verified: bool = True) -> str:
    """Name the person first, address second — for every owner-facing string.

    The assistant reads these tool outputs verbatim, so a raw identity leaks straight into
    what it says back: "+6591234567 (Renewal Discount): they want a 20% discount". The owner
    thinks in names. The address stays, in brackets, because it is what every other tool takes
    as an argument — dropping it would leave the assistant unable to act on its own output.

    An unverified identity is named as a visitor, never as the person it claimed to be.
    """
    if not verified or not identity.is_verified(asker):
        return identity.display(asker)
    name = people.display_name(asker, True)
    return f"{name} ({asker})" if name and name != asker else asker


def pending_escalations() -> str:
    """OPEN escalations — what the secretary handed you and you haven't dealt with.

    Repeats are grouped, and anything you've replied to or resolved drops off.
    """
    esc = open_escalations()
    if not esc:
        return "Nothing waiting — all escalations dealt with."
    out = [f"{len(esc)} waiting for you:"]
    for g in esc[:20]:
        who = who_label(g["asker"], g["verified"])
        flag = "  [SENDER MARKED URGENT]" if g.get("urgent") else ""
        subj = f"  ({g['topic']})" if g.get("topic") else ""
        out.append(f"[{g['ids'][0]}] {who} — {g['question']}{subj}{flag}"
                   f"\n  {g['reason'].removeprefix('policy:')} · {g['at']}")
        if len(g.get("questions", [])) > 1:
            out.append("  thread:")
            out += [f"    {i+1}. {q}" for i, q in enumerate(g["questions"])]
        b = g.get("briefing") or {}
        if b.get("wants"):
            out.append(f"  wants   : {b['wants']}")
        if b.get("facts"):
            out.append(f"  facts   : {b['facts']}")
        if b.get("decision"):
            out.append(f"  decide  : {b['decision']}")
        if b.get("draft"):
            out.append(f"  draft   : {b['draft']}")
    out.append("\nResolve with resolve_escalation(id), or just reply_to(asker, ...) "
               "which clears their open items.")
    return "\n".join(out)


def resolve_escalation(escalation_id: str, note: str = "") -> str:
    """Mark an escalation dealt with so it leaves the queue. Use the [id] shown."""
    for g in open_escalations():
        if escalation_id in g["ids"]:
            _mark(g["ids"], note or "resolved")
            return f"Resolved: {who_label(g['asker'], g['verified'])} — {g['question']}"
    return f"No open escalation with id {escalation_id}."


def resolve_all(match: str = "") -> str:
    """Clear the escalation queue in bulk. `match` filters by question or asker
    substring; empty clears everything open.

    Exists because an automated run can leave dozens of items behind, and a queue full
    of test noise is one the owner stops reading.
    """
    m = match.strip().lower()
    if not m:
        return ("Refusing to clear the whole queue — resolving is not reversible from "
                "here, and a bulk clear once wiped 27 unreviewed escalations. Pass a "
                "`match` substring, or resolve items individually.")
    ids, n = [], 0
    for g in open_escalations():
        if m not in g["question"].lower() and m not in g["asker"].lower():
            continue
        ids.extend(g["ids"]); n += 1
    if not ids:
        return "Nothing matched — queue unchanged."
    _mark(ids, f"bulk:{match or 'all'}")
    return f"Resolved {n} escalation(s)." + (f" (matching {match!r})" if match else "")


def digest(day: str = "") -> str:
    """Report of who asked what. `day` as YYYY-MM-DD; defaults to today."""
    day = day or date.today().isoformat()
    day_rows = [r for r in rows() if r["at"].startswith(day)]
    if not day_rows:
        return f"No queries on {day}."

    answered = [r for r in day_rows if r["outcome"] == "answered"]
    escalated = [r for r in day_rows if r["outcome"] == "escalated"]
    out = [f"{day}: {len(day_rows)} queries from "
           f"{len(set(r['asker'] for r in day_rows))} people "
           f"({len(answered)} answered, {len(escalated)} escalated)"]
    if escalated:
        out.append("\nNEEDS YOU")
        out += [f"- {who_label(r['asker'], r.get('verified', True))}: {r['question']}" for r in escalated]
    if answered:
        out.append("\nHANDLED FOR YOU")
        out += [f"- {who_label(r['asker'], r.get('verified', True))}: {r['question']}\n  -> {r['answer']}"
                for r in answered]

    reasons = Counter(policy.reclassify(r["question"], r["reason"]).removeprefix("policy:")
                      for r in escalated)
    if reasons:
        out.append("\nEscalation reasons: "
                   + ", ".join(f"{k} x{v}" for k, v in reasons.most_common())
                   + "\n(recurring ones are candidates for grant_folder / add_knowledge)")
    return "\n".join(out)


def search_queries(query: str, days: int = 7) -> str:
    """Search what people have asked, e.g. 'has anyone asked about pricing?'"""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    q = query.lower()
    hits = [r for r in rows()
            if r["at"] >= cutoff and (q in r["question"].lower() or q in r["asker"].lower())]
    if not hits:
        return f"No matches for '{query}' in the last {days} days."
    return f"{len(hits)} matches:\n" + "\n".join(
        f"- {r['at']} {who_label(r['asker'], r.get('verified', True))} "
        f"[{r['outcome']}] — {r['question']}" for r in hits[-25:])


def conversation_with(asker: str, limit: int = 20) -> str:
    """The full exchange with one person — what they asked and what your secretary said.

    The per-person view the digest and keyword search don't give you. Unverified turns
    are marked: the same address can appear as a claim by someone who could not prove it,
    and reading those as the same person would be misleading.
    """
    if not asker:
        return "Give an identity (email or number)."
    mine = [r for r in rows() if r["asker"].strip().lower() == asker.strip().lower()]
    if not mine:
        return f"No messages from {asker}."

    name = people.display_name(asker, True)
    head = f"{asker}" + (f" [{name}]" if name else "") + f" — {len(mine)} message(s)"
    out = [head, ""]
    for r in mine[-limit:]:
        flag = "" if r.get("verified") else "  (UNVERIFIED — could be someone else)"
        conv = f" · convo {r['conversation'][:8]}" if r.get("conversation") else ""
        out.append(f"{r['at'][11:16]}{flag}{conv}")
        if r.get("outcome") == "owner_reply":
            # Your own answer, not something they said — it was rendering under "them:".
            out.append(f"  YOU : {r['answer']}")
        else:
            out.append(f"  them: {r['question']}")
            out.append(f"  us  : {r['answer']}")
        if r["outcome"] == "escalated":
            out.append(f"        ^ escalated ({r['reason'].removeprefix('policy:')})")
        if r.get("sources"):
            out.append(f"        answered from: {', '.join(x.split('/')[-1] for x in r['sources'][:4])}")
        out.append("")
    if len(mine) > limit:
        out.append(f"({len(mine) - limit} earlier message(s) not shown)")
    return "\n".join(out)


def list_permissions() -> str:
    """Which folders the secretary may read, for everyone and per person.

    Grants live in two places — permissions.json and each person's profile '## Folders'.
    Both are shown: a view that omits one is worse than no view.
    """
    p = permissions.load()
    out = ["Everyone: " + (", ".join(p.get("default", {}).get("folders", [])) or "(none)")]

    who_all = set(p.get("askers", {})) | set(people.list_profiles())
    if who_all:
        out.append("\nPer identity (in addition, verified channels only):")
        for who in sorted(who_all):
            json_grants = p.get("askers", {}).get(who, {}).get("folders", [])
            prof_grants = people.folders_for(who, True)
            bits = []
            if json_grants:
                bits.append(f"{', '.join(json_grants)} (permissions.json)")
            if prof_grants:
                bits.append(f"{', '.join(prof_grants)} (profile)")
            name = people.display_name(who, True)
            label = f"{who} [{name}]" if name else who
            out.append(f"- {label}: {' + '.join(bits) or '(none)'}")

    out.append("\nProfile grants apply on verified channels only — an anonymous claim of "
               "the same identity gets the default set.")
    out.append("Access only. Money/commitments/scheduling still escalate wherever they live.")
    return "\n".join(out)


# ---------------------------------------------------------------- write

def _which_close(text: str, threads: list[dict]) -> tuple[list[dict], bool, str]:
    """Ask the model which threads this reply answers. Falls back to closing nothing.

    Biased to leaving open: on any failure, or anything uncertain, the thread stays in the
    queue. See policy.REPLY_CLOSES_PROMPT for why this is model-decided while the action
    gate is not.
    """
    from . import brain   # for the shared model client / config
    c = brain.client()
    if not threads:
        return [], False, "nothing was open"
    if c is None:
        return [], False, "no model available — nothing closed"
    # Show the ACTUAL asks, not a label. This previously sent `topic or question`, truncated
    # to 80 chars — so a thread with a topic had its question discarded, and a thread with
    # several phrasings showed only one. The model was then asked which requests a reply is
    # "about" while being shown neither the words the person used nor how the ask evolved.
    # Withholding the evidence and paying for reasoning is the wrong trade: this is free.
    def _evidence(n: int, t: dict) -> str:
        label = (t.get("topic") or "").strip()
        asks = t.get("questions") or [t["question"]]
        head = f"{n}. " + (f"[{label}] " if label else "") + asks[0][:220]
        # Later phrasings are how an ask was refined ("15%" -> "20%"), which is exactly what
        # decides whether a reply addresses it. Capped so a long thread can't crowd out the
        # others.
        return "\n".join([head] + [f"   also asked: {a[:220]}" for a in asks[1:3]])

    listing = "\n".join(_evidence(i + 1, t) for i, t in enumerate(threads))
    try:
        # Through the seam, like every other model call. This used to be a raw
        # vendor-shaped call, which broke the moment the provider changed — and broke
        # SILENTLY, because the `except` below turns any failure into "nothing closed".
        # Deciding which threads a reply closes is a judgement call, so thinking is on.
        raw = c.complete(policy.REPLY_CLOSES_PROMPT.format(
            owner=brain.OWNER, threads=listing, text=text))
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(raw)
        why = str(out.get("why", "")).strip()
        picked = [threads[n - 1] for n in
                  (int(x) for x in out.get("closes", []) if str(x).isdigit())
                  if 1 <= n <= len(threads)]
        # `holding` no longer vetoes a named thread. It used to, which meant "I'll sign the
        # MSA this week" left the MSA request open: true about fulfilment, wrong about the
        # queue. It now only matters when the reply names nothing at all.
        return picked, bool(out.get("holding")) and not picked, why
    except Exception as exc:
        return [], False, f"could not tell ({exc}) — nothing closed"


def record_delivery(asker: str, text: str, conversation: str = "") -> None:
    """Note that a held reply finally reached someone.

    No new log row: `reply_to` already wrote the `owner_reply` row the owner's view
    renders, and the "awaiting delivery" count drops on its own once the reply is taken.
    What was missing is the ASKER's transcript — the reply appeared in their page but
    vanished on refresh, because it was never part of the stored conversation.
    """
    from . import memory
    memory.append(memory.key(asker, True, conversation),
                  "(owner replied)", text, reason="owner:delivered")


def _context_line(picked: list[dict], mine: list[dict]) -> str:
    """Open the reply by naming what it is about — but only when that is ONE clear subject.

    The point was a reply arriving days later with nothing tying it to the question:
    "I'd rather keep my salary private" reads oddly on its own. Naming one subject fixes
    that. Naming several does not — `About renewal discount and "Also please send the signed
    MSA" — ...` is worse than no prefix, and it edits words the owner chose. The owner writing
    in the composer has already said what they meant; we are not here to rewrite it.

    So: one subject, from THEIR wording, or nothing. No quoted raw questions, no lists, no
    "(and 2 more)".
    """
    if len(picked) != 1:
        return ""                      # several subjects, or none — leave their words alone
    label = (picked[0].get("topic") or "").strip()
    if not label:
        return ""                      # no clean subject name; a quoted question reads worse
    return f"About {label} — "


# The owner may well write their own opener; a second one reads badly.
_HAS_CONTEXT = re.compile(r"^\s*(about|re:|regarding|on your|as (for|to)|you asked)\b", re.I)


DRAFTS = RUN / "drafts.json"


def _drafts() -> dict:
    try:
        return json.loads(DRAFTS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def draft_reply(asker: str, text: str, about: str = "") -> str:
    """Write a reply WITHOUT sending it. Nothing leaves the machine and no thread closes.

    Exists because there was no way to draft. Asked to "draft a short reply to Pauline about the
    signed MSA", the assistant called `reply_to` — which sends and closes — so an explicit
    request to compose something performed an outward action and consumed a pending item.

    This function has no send path at all. That is the guarantee: drafting cannot send by
    mistake, because the code to do it is not here. Sending stays `reply_to`, which the owner
    has to ask for separately.
    """
    if not asker or not text.strip():
        return "Give the person and the text to draft."
    data = _drafts()
    data[asker.strip().lower()] = {"text": text.strip(), "about": about.strip(),
                                   "at": datetime.now().isoformat(timespec="seconds")}
    DRAFTS.parent.mkdir(parents=True, exist_ok=True)
    DRAFTS.write_text(json.dumps(data, indent=2))
    who = who_label(asker, identity.is_verified(asker))
    return (f"DRAFT for {who} — nothing sent, nothing closed"
            + (f" (about: {about.strip()})" if about.strip() else "") + ":\n"
            f"{text.strip()}\n\n"
            f"To send it, the owner must ask you to send — then use reply_to with this text.")


def pending_draft(asker: str) -> dict | None:
    """The draft waiting for this person, if any — so "send it" sends what was approved."""
    return _drafts().get((asker or "").strip().lower())


def discard_draft(asker: str) -> str:
    """Throw away the draft waiting for someone, without sending it."""
    data = _drafts()
    if data.pop((asker or "").strip().lower(), None) is None:
        return "No draft for them."
    DRAFTS.write_text(json.dumps(data, indent=2))
    return "Draft discarded."


def reply_to(asker: str, text: str, about: str = "", close: bool | None = None) -> str:
    """Answer someone as the owner, and close the thread you answered.

    `about` names which thread — a topic ("meeting time") or an escalation id. Give it
    whenever they have more than one open: replying about scheduling used to clear their
    discount and contract threads too, because clearing was scoped to the person rather
    than the ask.

    The answer is always recorded and the thread always closed. Delivery is separate: it
    is queued only when their chat session is still live, since DDUET is passive and we
    cannot open a conversation.
    """
    _d = _drafts()
    if _d.pop((asker or "").strip().lower(), None) is not None:
        DRAFTS.write_text(json.dumps(_d, indent=2))   # sent: the draft is no longer pending
    if not asker or not text:
        return "Give an identity and the reply text."

    mine = [g for g in open_escalations() if g["asker"].strip().lower() == asker.strip().lower()]
    # Nothing open is NOT a reason to refuse. The owner writing to someone unprompted — "your
    # order is on its way", "sorry I missed you" — is an ordinary thing to want, and the
    # composer in the owner view offers it. Refusing here also created a second, tempting
    # place to implement sending, which is how the two owner surfaces drift apart.
    # It simply closes nothing, because there is nothing to close.

    note = ""
    if about:
        # Explicit wins: the owner said which thread, so don't second-guess it.
        a = about.strip().lower()
        picked = [g for g in mine if a in (g.get("topic") or "").lower()
                  or a in g["ids"] or a in g["question"].lower()]
        if not picked:
            return (f"No open thread for {asker} matching {about!r}. Open: "
                    + "; ".join(f"{g.get('topic') or g['question'][:30]} [{g['ids'][0]}]"
                                for g in mine))
    elif close is False:
        picked, note = [], "left open — you asked me not to close anything"
    else:
        # Which threads does this reply actually answer? Model-decided, biased to leaving
        # open. A holding reply ("let me check") answers nothing, and a reply may answer
        # some requests and not others.
        picked, holding, why = _which_close(text, mine)
        note = ("holding reply — nothing closed" if holding
                else (why or "")) if not picked else (why or "")
        # No blind fallback. An earlier version closed the person's threads anyway when they
        # had one or two open, on the theory that the model was under-closing. Two things
        # changed: `_which_close` now gets the real questions instead of truncated labels (it
        # went from wrong-and-24s to right-and-0.8s), and the composer allows messages that
        # are not replies at all. Together they made the fallback actively harmful — it closed
        # a 40% discount thread on the strength of "the signed copy is on its way", while the
        # model's own recorded reason said the two did not match.
        #
        # So: if the reply names nothing, nothing closes. `about=` and `close=True` remain for
        # when the owner knows better than the model.
    if close is True and not picked:
        picked = mine        # explicit override: close everything open for them
        note = "closed all open threads — you asked me to"

    # Legacy rows written before topics existed group by conversation, so the same
    # question can sit in two threads: closing one left the other on her list. Answering
    # an ask closes every record of that ask from that person.
    answered_qs = {q.strip().lower() for g in picked for q in g.get("questions", [g["question"]])}
    ids = {i for g in picked for i in g["ids"]}
    for r in rows():
        if (r["outcome"] == "escalated"
                and r["asker"].strip().lower() == asker.strip().lower()
                and r["question"].strip().lower() in answered_qs):
            ids.add(_row_id(r))
    closed = _mark(sorted(ids), f"answered: {text[:100]}") if picked else 0

    # What actually goes out: the owner's words, opened with what they are about. Computed
    # after the close decision so it can name the threads this reply answers — and used
    # for the log row too, so the owner's view shows what was sent, not a cleaner draft.
    sent = text if _HAS_CONTEXT.match(text) else _context_line(picked, mine) + text

    # Record the answer against the log so it shows in the conversation history.
    RUN.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({
            "id": None, "at": datetime.now().isoformat(timespec="seconds"),
            "asker": asker, "question": "(owner reply)", "network": "",
            "verified": True, "conversation": "", "outcome": "owner_reply",
            "reason": "owner:answered", "answer": sent, "sources": [],
            "briefing": {}, "topic": (picked[0].get("topic") or "") if picked else "",
        }) + "\n")

    sessions = json.loads(SESSIONS.read_text()) if SESSIONS.exists() else {}
    live = sessions.get(asker)
    what = (", ".join(g.get("topic") or g["question"][:28] for g in picked)
            if picked else "nothing")
    still = [g for g in mine if g not in picked]
    tail = ""
    if note:
        tail += f" ({note})"
    if still:
        tail += ("  Still open: "
                 + "; ".join(g.get("topic") or g["question"][:26] for g in still[:4])
                 + (f" +{len(still)-4} more" if len(still) > 4 else ""))
    if not live:
        # Held, not dropped. Closing the escalation while nothing was sent made the queue
        # claim it was handled when the person had heard nothing.
        asker_actions.queue_reply(asker, sent)
        return (f"Closed: {what}.{tail} HELD for delivery — {asker} has no live "
                f"chat session and DDUET cannot start one, so I will send this the moment "
                f"they next write. It is visible as awaiting delivery until then.")

    with OUTBOX.open("a") as f:
        f.write(json.dumps({"asker": asker, "text": sent,
                            "queued_at": datetime.now().isoformat(timespec="seconds")}) + "\n")
    return f"Closed: {what}.{tail} Sending to {asker} now."


# ---- knowledge writes -------------------------------------------------------
# Three rules, each closing a way an owner-saved "memory" used to go wrong.
#
# 1. DESTINATION. Everything landed in public/learned.md, so a fact about a subject an
#    existing document already owns created a SECOND source for it. That is the drift this
#    project keeps finding — the agent quoting one price while the order form charges
#    another. The model proposes the file; code checks it.
# 2. WRITE BOUNDARY. Reads may point anywhere the owner granted, including real repositories
#    outside the instance (the index currently holds a product-hub README). Appending there
#    would edit the owner's actual source tree, so writes are confined to $DDUET_HOME/knowledge
#    whatever the model asks for.
# 3. BOUNDS ARE NOT KNOWLEDGE. "We're open till 22:00" written as a fact makes the agent SAY
#    22:00 while check_bounds still refuses 21:30 — the agent contradicting itself out loud.
#    A fact that disagrees with a declared bound is refused and pointed at the bound.

# Only the numeric bounds are compared. A boolean like verified_only cannot be judged from a
# sentence without guessing, and guessing here would block legitimate facts.
_TIME24 = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_TIME12 = re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.I)
# Deliberately narrow: "open"/"close" only. "last order 20:30" and "we are open on Sunday" are
# legitimate facts, and a looser trigger refused both.
_HOURS_WORD = re.compile(r"\b(open|opens|opening|close|closes|closing)\b", re.I)
_QTY_WORD = re.compile(r"\b(max|maximum|at most|no more than|up to|limit of)\b", re.I)
_KM = re.compile(r"\b(\d+(?:\.\d+)?)\s*km\b", re.I)
_INT = re.compile(r"\b(\d+)\b")


def _minutes(text: str) -> list[int]:
    """Clock times in a sentence, as minutes past midnight."""
    out = [int(h) * 60 + int(m) for h, m in _TIME24.findall(text) if int(h) < 24 and int(m) < 60]
    for h, ap in _TIME12.findall(text):
        h = int(h) % 12
        out.append(h * 60 + (12 * 60 if ap.lower() == "pm" else 0))
    return out


def _declared(bound: str) -> dict:
    """{value: [capability names]} for one bound across every declared capability."""
    out: dict = {}
    for name, cap in (capabilities.all_capabilities() or {}).items():
        v = ((cap or {}).get("bounds") or {}).get(bound)
        if v not in (None, ""):
            out.setdefault(v, []).append(name)
    return out


def _bound_conflict(fact: str) -> tuple[str, str]:
    """(refusal, note). Refuses only when the fact can be attributed to ONE declared value.

    Comparing against every capability over-triggered: with two capabilities declaring
    different `max_quantity`, a fact about one was refused for disagreeing with the other.
    Code cannot tell which subject a sentence is about, so when several values exist it says
    so and saves the fact rather than guessing.
    """
    def judge(bound, said, fmt):
        vals = _declared(bound)
        if not said or not vals:
            return "", ""
        if len(vals) > 1:
            listed = "; ".join(f"{v} ({', '.join(n)})" for v, n in vals.items())
            return "", (f"Note: capabilities declare different {bound} values — {listed}. "
                        f"Saved as written; use set_capability_bound to change one.")
        value, names = next(iter(vals.items()))
        if fmt(value, said):
            return "", ""
        return (f"That disagrees with the `{names[0]}` capability, which enforces "
                f"{bound} {value}. Bookings would still be refused, so the agent would say "
                f"one thing and do another.\n"
                f"  Change the bound first: set_capability_bound(name=\"{names[0]}\", "
                f"bound=\"{bound}\", value=...).", "")

    notes = []
    if _HOURS_WORD.search(fact):
        said = _minutes(fact)
        bad, note = judge("hours", said,
                          lambda v, s: "-" not in str(v) or len(_minutes(str(v))) != 2
                                       or all(x in _minutes(str(v)) for x in s))
        if bad:
            return bad, ""
        notes.append(note)
    if _QTY_WORD.search(fact):
        nums = [int(n) for n in _INT.findall(fact)]
        bad, note = judge("max_quantity", nums, lambda v, s: int(v) in s)
        if bad:
            return bad, ""
        notes.append(note)
    km = [float(k) for k in _KM.findall(fact)]
    if km:
        bad, note = judge("radius_km", km, lambda v, s: float(v) in s)
        if bad:
            return bad, ""
        notes.append(note)
    return "", " ".join(n for n in notes if n)


def _knowledge_target(file: str) -> tuple[pathlib.Path | None, str]:
    """Resolve a requested destination inside the knowledge root, or explain the refusal."""
    root = paths.KNOWLEDGE.resolve()
    if not file.strip():
        return None, ("Name the destination. There is deliberately no general file: a catch-all "
                      "becomes a second home for subjects that already have one, and the two "
                      "then disagree. Use list_knowledge, then pick by KIND of fact:\n"
                      "  - about a domain the agent can also ACT in -> that capability's "
                      "document, named after it (see the index)\n"
                      "  - about the owner -> about.md\n"
                      "  - about how the secretary itself works -> secretary.md\n"
                      "  - about ONE person, or anything not everyone should hear -> "
                      "note_person, NOT a knowledge file: knowledge/ is readable by anyone "
                      "who writes in\n"
                      "  - a subject with no capability -> the document for that subject\n"
                      "If none fits, ask the owner where it belongs rather than inventing a "
                      "general file.")
    want = file.strip().replace("\\", "/")
    if want.startswith("/"):
        # Stripping the slash would silently reinterpret /etc/x.md as knowledge/etc/x.md —
        # safe, but the refusal then blames a missing folder instead of the boundary.
        return None, (f"'{file}' is an absolute path. Give a path inside the knowledge "
                      f"folder, e.g. public/learned.md.")
    rel = want.removeprefix("knowledge/")
    if not rel.endswith(".md"):
        return None, f"Knowledge files are markdown; '{file}' is not a .md file."
    target = (paths.KNOWLEDGE / rel).resolve()
    if not target.is_relative_to(root):
        # Covers ../ and symlink escapes. Reads may range wider than writes on purpose.
        return None, (f"'{file}' is outside {root}. Knowledge writes stay inside the "
                      f"knowledge folder — granted folders can be real source trees.")
    if not target.parent.is_dir():
        have = ", ".join(sorted(d.name for d in paths.KNOWLEDGE.iterdir() if d.is_dir()))
        return None, (f"There is no '{rel.rsplit('/', 1)[0]}' folder. A new folder would also "
                      f"be readable by nobody until it is granted. Existing: {have}.")
    return target, ""


def doc_for(capability: str) -> pathlib.Path:
    """The document that says what a capability's domain is about.

    Named after the capability (`pizza_delivery` -> `pizza-delivery.md`) so the say-side and
    the do-side are a visible pair. It used to be a filename chosen by hand plus a comment in
    the document asking whoever edits the bounds to remember to edit the prose too — which is
    the kind of coupling that survives exactly as long as the person who wrote it.
    """
    return paths.KNOWLEDGE / (capability.strip().lower().replace("_", "-") + ".md")


def _statements(f: pathlib.Path) -> list[str]:
    """The individually editable assertions in a document: bullets and headings.

    The unit matters. If a document is one prose blob, the agent can only append to it or
    rewrite it wholesale; a bullet is a thing that can be corrected in place.
    """
    out = []
    for line in f.read_text().splitlines():
        s = line.strip()
        if s.startswith("- ") and len(s) > 4:
            out.append(s[2:].strip())
        elif s.startswith("#"):
            out.append(s.lstrip("# ").strip())
    return out


def _duplicated() -> list[str]:
    """Subjects asserted in more than one document — the shape a contradiction takes here.

    Found by term overlap, not meaning, so it over-reports rather than missing: two documents
    both saying something about "channels" is worth the agent's attention even when they agree.
    Written because the agent corrected a channel count in one file and left the other one
    asserting the old number, leaving the knowledge base holding both.
    """
    root = paths.KNOWLEDGE
    seen = []
    for f in sorted(root.rglob("*.md")):
        for s in _statements(f):
            terms = {w for w in permissions._terms(s) if len(w) > 3}
            if terms:
                seen.append((f, s, terms))
    out = []
    for i, (f1, s1, t1) in enumerate(seen):
        for f2, s2, t2 in seen[i + 1:]:
            if f1 == f2:
                continue
            shared = t1 & t2
            if len(shared) < 2 or len(shared) < 0.5 * min(len(t1), len(t2)):
                continue
            out.append(f"  '{', '.join(sorted(shared))}' [consolidate: correct one, "
                       f"delete the other]\n"
                       f"      {f1.relative_to(root.parent).as_posix()}: \"{s1[:70]}\"\n"
                       f"      {f2.relative_to(root.parent).as_posix()}: \"{s2[:70]}\"")
    return out


def _readers_of(target: pathlib.Path) -> str:
    """Who can be answered from this file.

    One audience: a fact only one person may hear belongs in people/<identity>.md, not in a
    knowledge folder with a narrower grant.
    """
    return "anyone who writes in"


def list_knowledge() -> str:
    """Index of the knowledge documents — what each asserts, and any subject in two of them."""
    root = paths.KNOWLEDGE
    if not root.is_dir():
        return "No knowledge folder yet."
    files = sorted(p for p in root.rglob("*.md") if p.is_file())
    if not files:
        return "No knowledge files yet."
    out = ["KNOWLEDGE INDEX — one subject belongs in ONE document. Correct in place; do not "
           "add a second version of something already stated."]
    for f in files:
        sts = _statements(f)
        out.append(f"\n{f.relative_to(root.parent).as_posix()}  "
                   f"(readable by: {_readers_of(f)})")
        for s in sts[:12]:
            out.append(f"    - {s[:96]}")
        if len(sts) > 12:
            out.append(f"    … +{len(sts) - 12} more (read_knowledge for the rest)")
    caps = capabilities.all_capabilities() or {}
    if caps:
        out.append("\nCAPABILITY DOCUMENTS — what may be SAID about a domain the agent may ACT "
                   "in. Same name on both sides, so they cannot drift apart unnoticed.")
        for name in caps:
            d = doc_for(name)
            out.append(f"    {name}  ->  {d.relative_to(paths.KNOWLEDGE.parent).as_posix()}"
                       + ("" if d.exists() else "   MISSING — the agent can act here but has "
                                                "nothing documented to say about it"))
    dupes = _duplicated()
    if dupes:
        out.append("\nSAME SUBJECT IN MORE THAN ONE DOCUMENT — each line says what to do. "
                   "Overlap is flagged by wording, so some pairs will already agree.")
        out.extend(dupes[:8])
    return "\n".join(out)


def _overlapping(text: str, skip: pathlib.Path | None) -> list[tuple[pathlib.Path, str]]:
    """Existing statements about the same subject, anywhere in knowledge/."""
    terms = {w for w in permissions._terms(text) if len(w) > 3}
    if not terms:
        return []
    out = []
    for f in sorted(paths.KNOWLEDGE.rglob("*.md")):
        if skip and f.resolve() == skip.resolve():
            continue
        for s in _statements(f):
            st = {w for w in permissions._terms(s) if len(w) > 3}
            shared = terms & st
            if len(shared) >= 2 and len(shared) >= 0.5 * min(len(terms), len(st)):
                out.append((f, s))
    return out


def _replaces(new_fact: str, candidates: list[tuple[pathlib.Path, str]]) -> list[tuple]:
    """Which existing statements the new one would CONTRADICT or supersede.

    Term overlap alone cannot refuse a write — adding a second item to a menu overlaps with
    the first — so code narrows and the model judges. Cleaning at write time is the point: a
    contradiction that reaches the documents gets answered to an external party before anyone notices.
    """
    if not candidates:
        return []
    try:
        from . import brain
        c = brain.client()
    except Exception:                  # no model SDK installed (the model-free test suite)
        c = None
    if c is None:
        return []                      # no model: allow the write, do not silently guess
    listed = "\n".join(f"{i + 1}. {s}" for i, (_, s) in enumerate(candidates))
    prompt = (
        "A new fact is being saved to a knowledge base. Below are existing statements about a "
        "similar subject, visible to the same readers.\n\n"
        f"NEW FACT: {new_fact}\n\nEXISTING:\n{listed}\n\n"
        "Which existing statements does the new fact CONTRADICT or REPLACE — meaning both "
        "cannot be true at once, or the new one is an updated version of the old one? "
        "Statements that are simply about the same topic and can both stand are NOT replaced.\n"
        'Reply with only JSON: {"replaces": [<numbers>]}')
    try:
        raw = c.complete(prompt).strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        nums = json.loads(raw).get("replaces") or []
        return [candidates[int(n) - 1] for n in nums
                if str(n).isdigit() and 1 <= int(n) <= len(candidates)]
    except Exception:
        return []


def _write_time_check(fact: str, target: pathlib.Path) -> str:
    """"" if the fact can be saved as-is, else what must be resolved first."""
    hits = _replaces(fact, _overlapping(fact, skip=None))
    if not hits:
        return ""
    lines = [f"  - {f.relative_to(paths.KNOWLEDGE.parent).as_posix()}: \"{s[:100]}\""
             for f, s in hits]
    return ("This contradicts or replaces what is already recorded:\n" + "\n".join(lines)
            + "\nFix the existing statement with edit_knowledge instead of adding a second "
              "version. Two answers to one question means the agent will sometimes give the "
              "wrong one.")


EDIT_LOG = RUN / "knowledge-edits.jsonl"


def read_knowledge(file: str) -> str:
    """Show a knowledge document, so it can be corrected instead of appended to."""
    target, why = _knowledge_target(file)
    if target is None:
        return f"Cannot read: {why}"
    if not target.exists():
        return f"{file} does not exist yet. list_knowledge shows what does."
    where = target.resolve().relative_to(paths.KNOWLEDGE.resolve().parent).as_posix()
    return (f"{where} — readable by: {_readers_of(target)}\n"
            f"Edit it with edit_knowledge(file, old, new); `old` must appear exactly once.\n"
            f"-----\n{target.read_text()}")


def edit_knowledge(file: str, old: str, new: str = "") -> str:
    """Replace an exact snippet in a knowledge document — correct or delete a stale fact.

    Exact-and-unique on purpose. A fuzzy edit to the disclosure surface is a silent change to
    what external parties get told, and appending a correction instead (the only option before this)
    left both versions readable — the agent then answered with whichever one retrieval
    happened to surface.
    """
    if not old.strip():
        return "Give the exact text to replace. Use read_knowledge first."
    target, why = _knowledge_target(file)
    if target is None:
        return f"NOT edited. {why}"
    if not target.exists():
        return f"NOT edited. {file} does not exist. list_knowledge shows what does."
    text = target.read_text()
    hits = text.count(old)
    if hits == 0:
        return (f"NOT edited. That text is not in {file} — it may be worded differently. "
                f"Call read_knowledge('{file}') and copy the line exactly.")
    if hits > 1:
        return (f"NOT edited. That text appears {hits} times in {file}. Include enough "
                f"surrounding text to identify one of them.")
    if new.strip():
        clash, _ = _bound_conflict(new)
        if clash:
            return f"NOT edited. {clash}"

    after = text.replace(old, new, 1)
    if not new.strip():                       # a deletion should not leave a blank gap
        after = re.sub(r"\n{3,}", "\n\n", after)
    # Append-only record of every change to what external parties may be told. The edit itself is
    # destructive; this is what makes it recoverable and auditable.
    EDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EDIT_LOG.open("a") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                            "file": str(target), "before": text, "after": after}) + "\n")
    target.write_text(after)
    where = target.resolve().relative_to(paths.KNOWLEDGE.resolve().parent).as_posix()
    verb = "Removed from" if not new.strip() else "Edited"
    return (f"{verb} {where}\n  - was: {old.strip()[:160]}\n"
            + (f"  - now: {new.strip()[:160]}\n" if new.strip() else "")
            + f"Readable by: {_readers_of(target)}")


#: The headings owner.py parses out of settings.md. Only these may be set, because a typo would
#: write a section the code never reads — the same silent failure as the heading rename that
#: emptied the never-say list.
SETTING_FIELDS = {"name": "Name", "pronoun": "Pronoun", "voice": "Voice",
                  "never_say": "Never say"}


def _section_bullets(doc: pathlib.Path, heading: str) -> list[str]:
    """The `- ` bullets under one `## ` heading."""
    if not doc.is_file():
        return []
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)",
                  doc.read_text(), re.S | re.M)
    if not m:
        return []
    return [l.strip()[2:].strip() for l in m.group(1).splitlines() if l.strip().startswith("- ")]


def current_setup() -> dict:
    """What setup would prefill: the CURRENT state, not the answers that produced it.

    Re-running setup is how an owner changes their mind, so it has to open on what is true now.
    The free-text answers were never stored — the model turned them into settings and bullets —
    so the bullets ARE the answer, and showing them is more honest than showing a stale
    transcript of what was once typed.
    """
    doc = paths.KNOWLEDGE / "owner.md"
    return {
        "name": owner.name() if owner.name() != owner.DEFAULT_NAME else "",
        "pronoun": owner.pronoun_raw(),
        "does": "\n".join(_section_bullets(doc, "Who")),
        "contacts": "\n".join(_section_bullets(doc, "Contacts")),
        "available": "\n".join(_section_bullets(doc, "Availability")),
        "never": "\n".join(owner.never_say()),
        "configured": owner.name() != owner.DEFAULT_NAME,
    }


def set_setting(field: str, value: str) -> str:
    """Set one owner setting: name, pronoun, voice or never_say. Not knowledge — never quoted."""
    key = field.strip().lower().replace(" ", "_").replace("-", "_")
    heading = SETTING_FIELDS.get(key)
    if not heading:
        return f"Unknown setting {field!r}. One of: {', '.join(sorted(SETTING_FIELDS))}."
    path = paths.SETTINGS
    text = path.read_text() if path.is_file() else "# Settings\n"
    body = value.strip()
    if key == "never_say":
        # A list, one topic per line — stored as bullets so owner.never_say() reads it back.
        items = [l.strip("-• ").strip() for l in body.splitlines() if l.strip()]
        body = "\n".join(f"- {i}" for i in items)
    block = f"## {heading}\n{body}\n"
    pattern = re.compile(rf"^## {re.escape(heading)}\b.*?(?=^## |\Z)", re.S | re.M)
    text = pattern.sub(block + "\n", text, count=1) if pattern.search(text) \
        else text.rstrip() + f"\n\n{block}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return f"Set {heading} in settings.md (not knowledge — never quoted to anyone):\n  {body[:160]}"


def add_knowledge(fact: str, file: str = "", section: str = "") -> str:
    """Teach the secretary a fact. Name the file that already owns the subject."""
    fact = fact.strip()
    if not fact:
        return "Nothing to add."
    clash, note = _bound_conflict(fact)
    if clash:
        return f"NOT saved. {clash}"
    target, why = _knowledge_target(file)
    if target is None:
        return f"NOT saved. {why}"
    stale = _write_time_check(fact, target)
    if stale:
        return f"NOT saved. {stale}"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = target.read_text() if target.exists() else f"# {target.stem.replace('-', ' ').title()}\n"
    # WHERE the bullet lands. A structured document's last section is not a safe default — a
    # calzone price once landed under "## Not covered here", a heading that says the opposite of
    # what the bullet claims. So: append under the named section when one is given, and fall back
    # to a section of our own rather than to whatever happens to be last.
    #
    # `section` exists because without it the caller could not comply with the instruction to put
    # a fact under the heading that owns its subject: every fact went to "## Added since",
    # including the ones belonging in the "## Who" and "## Availability" headings sitting empty
    # above it.
    want = (section or "").strip().lstrip("#").strip()
    placed = False
    if want:
        pat = re.compile(rf"^##\s+{re.escape(want)}\s*$(.*?)(?=^##\s|\Z)", re.S | re.M)
        m = pat.search(text)
        if m:
            body = m.group(1).rstrip()
            text = text[:m.start(1)] + f"{body}\n- {fact}\n\n" + text[m.end(1):]
            placed = True
        else:
            text = text.rstrip() + f"\n\n## {want}\n\n- {fact}\n"
            placed = True
    if not placed:
        SECTION = "## Added since"
        if "\n## " in text and SECTION not in text:
            text = text.rstrip() + f"\n\n{SECTION}\n\nFacts the owner added later, newest last.\n"
        text = text.rstrip() + f"\n- {fact}\n"
    target.write_text(text)
    where = target.resolve().relative_to(paths.KNOWLEDGE.resolve().parent).as_posix()
    return (f"Added to {where}\n  - {fact}\nReadable by: {_readers_of(target)}"
            + (f"\n{note}" if note else ""))


def grant_folder(asker: str, folder: str, note: str = "") -> str:
    """Let one verified person be answered from one more folder.

    Indexes it immediately, so the first question after a grant isn't the slowest one
    — and so the reply says how big the thing you just granted actually is.
    """
    out = permissions.grant(asker, folder, note)
    if out.startswith("Granted"):
        idx = folder_index.build(folder)
        out += (f"\nIndexed: {idx['file_count']} files, {idx['chunk_count']} chunks, "
                f"{idx['bytes'] // 1024} KB")
    return out


def revoke_folder(asker: str, folder: str) -> str:
    """Take back a folder grant."""
    return permissions.revoke(asker, folder)


def index_status() -> str:
    """What each granted folder actually contains, and how fresh the index is.

    Worth checking before granting a repo root: the grant is the security boundary,
    so its size and contents should not be a surprise.
    """
    folders = set(permissions.load().get("default", {}).get("folders", []))
    for cfg in permissions.load().get("askers", {}).values():
        folders.update(cfg.get("folders", []))
    for who in people.list_profiles():
        folders.update(people.folders_for(who, True))
    if not folders:
        return "No folders granted."

    out = []
    for s in folder_index.status(sorted(folders)):
        if s.get("missing"):
            out.append(f"- {s['folder']}: MISSING on disk")
            continue
        line = (f"- {s['folder']}: {s['files']} files, {s['chunks']} chunks, "
                f"{s['bytes'] // 1024} KB")
        if s["stale"]:
            c = s["changes"]
            line += (f"  [STALE: +{c['added']} ~{c['changed']} -{c['removed']}"
                     f" — rebuilds on next query]")
        out.append(line)
        out.append(f"    indexed {s['indexed_at']} · last checked {s['last_scanned']}")
    return "\n".join(out) + f"\n\nIndex: {folder_index.index_dir()}"


# ---------------------------------------------------------------- people

def who_is(asker: str) -> str:
    """Show what the secretary knows about a person."""
    if not people.exists(asker):
        return (f"No profile for {asker}. Create one with add_person so the secretary "
                f"adapts its tone and access for them.")
    secs = people.sections(asker)
    out = [f"{asker}"]
    for name in ("Who", "Comms", "Folders", "Always escalate", "Observed"):
        if secs.get(name):
            out.append(f"\n{name}:\n{secs[name]}")
    return "\n".join(out)


def list_people() -> str:
    """Everyone the secretary has a profile for."""
    names = people.list_profiles()
    if not names:
        return "No profiles yet."
    return f"{len(names)} profiles:\n" + "\n".join(f"- {n}" for n in names)


def add_person(asker: str, who: str = "", comms: str = "") -> str:
    """Start a profile for a VERIFIED person — who they are and how to write to them."""
    return people.create(asker, who, comms)


def note_person(asker: str, section: str, note: str) -> str:
    """Add a curated note. section: Who | Comms | Folders | Always escalate.

    'Folders' grants a readable folder to this person; 'Always escalate' adds a topic
    the secretary must never answer for them.
    """
    valid = {"Who", "Comms", "Folders", "Always escalate"}
    if section not in valid:
        return f"section must be one of: {', '.join(sorted(valid))}"
    return people.add_note(asker, section, note)


def profile_suggestions() -> str:
    """Proposed profile updates from the query log. Code never writes these itself —
    accept one with accept_observation."""
    sug = people.suggest_observations(rows())
    if not sug:
        return "Nothing to suggest yet."
    out = []
    for who, tips in sug.items():
        out.append(f"{who}:")
        out += [f"  - {t}" for t in tips]
    return "\n".join(out) + "\n\nAccept one with accept_observation(asker, note)."


def accept_observation(asker: str, note: str) -> str:
    """Record an observation into a person's profile."""
    return people.accept_observation(asker, note)


# ---------------------------------------------------------------- registry

#: The owner's tool surface. Consumed by secretary_mcp.py and web.py — never by the
#: external-facing agent.
# ---- attaching a model -------------------------------------------------------

def model_status() -> str:
    """Which model is attached and whether it actually works right now."""
    ok, why = llm.verify()
    return ("OK   " if ok else "FAIL ") + why


def attach_model(key: str, model: str = "") -> str:
    """Attach a model by API key: verify it works, then save it to this instance.

    Removes the need for any external CLI — the framework owns the credential step, which
    is what makes `init` possible on a machine with nothing installed but DDuet Desktop.

    Verify BEFORE writing. A credential that is saved and broken produces the worst failure
    this agent has: it starts, connects, and silently escalates every single message,
    because "no working model" and "nothing to answer" look the same from outside.

    The key is written to $DDUET_HOME/.env at 0600 and is never echoed, logged, or returned
    — the confirmation reports its length and last four characters only.
    """
    key = (key or "").strip()
    if not key:
        return "Give the API key to attach."
    m = (model or "").strip() or os.getenv("SECRETARY_MODEL") or ""
    if not m:
        return "Say which model to attach (e.g. claude-sonnet-5, gemini-3.6-flash)."

    var = llm.key_name(m)
    before = os.environ.get(var)
    os.environ[var], os.environ["SECRETARY_MODEL"] = key, m
    llm.forget()                       # drop any client cached under the old credential
    ok, why = llm.verify(m)
    if not ok:
        # Put the environment back: a failed attach must change nothing.
        if before is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = before
        llm.forget()
        return f"NOT saved — {why}"

    _write_env({var: key, "SECRETARY_MODEL": m})
    return (f"Attached {m}. {why}\n"
            f"  saved {var} to {paths.ENV_FILE} (chmod 600, {len(key)} chars, "
            f"ending {key[-4:]})")


def save_connector(api_key: str, connector_uuid: str) -> str:
    """Write the B3 connector credential to this instance. Verify FIRST (see connector.verify).

    Deliberately not in OWNER_TOOLS: handing a secret to the assistant means typing it into a
    chat box, which sends it to the model provider and writes it to run/owner_chat.json in
    plaintext. Credentials are entered on a page.
    """
    api_key, connector_uuid = api_key.strip(), connector_uuid.strip()
    if not api_key or not connector_uuid:
        return "Give both the API key and the connector uuid."
    _write_env({connector.API_KEY: api_key, connector.UUID: connector_uuid})
    # Visible to this process immediately — and the channel loop polls the environment, so it
    # picks this up within seconds without a restart.
    os.environ[connector.API_KEY] = api_key
    os.environ[connector.UUID] = connector_uuid
    return (f"Saved the connector to {paths.ENV_FILE} (chmod 600). "
            f"Key {len(api_key)} chars ending {api_key[-4:]}, connector {connector_uuid}. "
            f"The channel connects within a few seconds — no restart needed.")


def _write_env(values: dict) -> None:
    """Upsert keys in the instance .env, preserving everything else and the file mode."""
    path = paths.ENV_FILE
    lines = path.read_text().splitlines() if path.is_file() else []
    for var, val in values.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == var:
                lines[i] = f"{var}={val}"
                break
        else:
            lines.append(f"{var}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


# ---- capabilities: bounded authority to act ----------------------------------
# Thin wrappers. The mechanism lives in capabilities.py / schedule.py; these exist so the
# owner reaches it by talking, over either surface, and so the docstring's first line
# becomes the tool description the model sees (see web._tool_docs).

def list_capabilities() -> str:
    """Show what the agent may commit to on your behalf, and within what limits."""
    return capabilities.listing()


def list_examples() -> str:
    """Ready-made capabilities the owner can turn on — what each does, and its limits."""
    root = paths.EXAMPLES
    if not root.is_dir():
        return "No examples shipped with this install."
    out = ["Ready-made capabilities. Each is a matched set: limits, a document to answer from, "
           "and optionally its own page."]
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            spec = json.loads((d / "capability.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for name, cap in spec.items():
            bounds = cap.get("bounds") or {}
            limits = ", ".join(f"{k}={v}" for k, v in bounds.items()) or "none declared"
            page = "yes" if list(d.glob("*.html")) else "no"
            here = " — ALREADY INSTALLED" if capabilities.get(name) else ""
            out.append(f"\n- {name}{here}\n    {cap.get('what','')}\n"
                       f"    limits: {limits}\n    own page: {page}")
    return "\n".join(out)


def install_example(name: str, bounds: str = "") -> str:
    """Turn on a ready-made capability: its document, its page, and its declared limits.

    Copies OUT of the install (which for a one-file binary is a temp directory that disappears
    when the process exits — so telling an owner to copy files by hand cannot work).

    Declaring the capability is granting authority, so this is deliberately an action the OWNER
    takes. Setup may SUGGEST it; the interview prompt forbids the model from declaring one on
    its own initiative.

    `bounds` overrides the example's own limits, as `key=value` pairs separated by commas —
    e.g. "hours=11:00-22:00,max_quantity=4". Without this an owner installing the pizza example
    got a pizzeria's opening hours and had to notice and correct them afterwards, which is
    exactly the say/do mismatch the rest of the design works to prevent.
    """
    import shutil

    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    src = None
    for d in (paths.EXAMPLES.iterdir() if paths.EXAMPLES.is_dir() else []):
        if not d.is_dir():
            continue
        try:
            spec = json.loads((d / "capability.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if key in spec:
            src, cap = d, spec[key]
            break
    if src is None:
        return f"No example named {name!r}. Use list_examples to see what there is."
    if capabilities.get(key):
        return (f"{key} is already declared. Change its limits with set_capability_bound, or "
                f"remove it first — installing again would overwrite what you have tuned.")

    done = []
    for doc in src.glob("*.md"):
        if doc.name.lower() == "readme.md":
            continue
        dest = paths.KNOWLEDGE / doc.name
        if dest.exists():
            done.append(f"kept your existing {dest.name} (not overwritten)")
        else:
            paths.KNOWLEDGE.mkdir(parents=True, exist_ok=True)
            shutil.copy(doc, dest)
            done.append(f"added knowledge/{dest.name}")
    for page in src.glob("*.html"):
        dest = paths.CANVAS / page.name
        if dest.exists():
            done.append(f"kept your existing canvas/{dest.name}")
        else:
            paths.CANVAS.mkdir(parents=True, exist_ok=True)
            shutil.copy(page, dest)
            done.append(f"added canvas/{dest.name}")
    declared = dict(cap.get("bounds") or {})
    for pair in [b for b in bounds.split(",") if "=" in b]:
        k, _, v = pair.partition("=")
        k, v = k.strip(), v.strip()
        if k not in capabilities.CHECKED and k != "radius_km":
            done.append(f"ignored unknown limit {k!r}")
            continue
        if v.lower() in ("true", "false"):
            declared[k] = v.lower() == "true"
        elif v.replace(".", "", 1).isdigit():
            declared[k] = float(v) if "." in v else int(v)
        else:
            declared[k] = v
        done.append(f"limit {k} = {declared[k]} (yours, not the example's)")
    out = capabilities.add(key, cap.get("what", ""), cap.get("action", "book_slot"), declared)
    if cap.get("canvas_label"):
        capabilities.set_bound(key, "canvas_label", cap["canvas_label"])
    done.append(out.splitlines()[0] if out else f"declared {key}")
    return "Installed " + key + ":\n  - " + "\n  - ".join(done)


def declare_capability(name: str, what: str, action: str = "book_slot",
                       bounds: dict | None = None) -> str:
    """Let the agent act on your behalf for one kind of ask, within explicit limits."""
    return capabilities.add(name, what, action, bounds or {})


def _stale_prose(capability: str) -> list[str]:
    """Statements in the paired document that now disagree with the declared bounds.

    Reuses `_bound_conflict`, the same check `add_knowledge` runs on a new fact — so the two
    directions cannot drift apart. Its judgement is "does this sentence contradict a bound",
    which is exactly the question to ask of existing prose after a bound moves.
    """
    doc = doc_for(capability)
    if not doc.is_file():
        return []
    out = []
    for s in _statements(doc):
        bad, _ = _bound_conflict(s)
        if bad:
            out.append(s)
    return out


def set_capability_bound(name: str, bound: str, value: str = "") -> str:
    """Change one limit on a capability — e.g. the hours, or the max per order."""
    out = capabilities.set_bound(name, bound, value)
    if out.lower().startswith(("could not", "no capability", "unknown", "give ")):
        return out
    # The guard was one-directional: a fact contradicting a bound was refused, but moving the
    # bound left the prose stale in silence — the agent would then quote the old hours while
    # booking to the new ones. Report the exact lines so they can be fixed in the same turn.
    stale = _stale_prose(name)
    if stale:
        lines = "\n".join(f"  - {s[:110]}" for s in stale)
        doc = doc_for(name)
        where = doc.resolve().relative_to(paths.KNOWLEDGE.resolve().parent).as_posix()
        out += (f"\n\nWARNING — {where} now contradicts this bound:\n{lines}\n"
                f"Fix it with edit_knowledge now. Until you do, the agent will SAY the old "
                f"limit while BOOKING to the new one.")
    return out


def remove_capability(name: str) -> str:
    """Withdraw a capability. Asks it covered go back to escalating to you."""
    return capabilities.remove(name)


def list_bookings(day: str = "") -> str:
    """What the agent has committed you to — bookings it made without asking."""
    rows = schedule.bookings(day)
    if not rows:
        return f"Nothing booked{' on ' + day if day else ''}."
    return "\n".join(
        f"  [{r['id']}] {r['at'].replace('T', ' ')} ({r['minutes']}m)  {r['what']}"
        f"  — for {r['who']}" for r in rows)


def cancel_booking(booking_id: str) -> str:
    """Cancel a booking the agent made. Frees the slot."""
    return (f"Cancelled {booking_id}." if schedule.cancel(booking_id)
            else f"No booking with id {booking_id!r}.")


OWNER_TOOLS = {
    "pending_escalations": (pending_escalations, {}),
    "digest": (digest, {"day": "YYYY-MM-DD, defaults to today"}),
    "search_queries": (search_queries, {"query": "text to search for",
                                        "days": "how many days back (default 7)"}),
    "list_permissions": (list_permissions, {}),
    "draft_reply": (draft_reply, {
        "asker": "their email or number",
        "text": "the reply you have composed — it will NOT be sent",
        "about": "which thread it answers (topic or escalation id), optional"}),
    "discard_draft": (discard_draft, {"asker": "their email or number"}),
    "reply_to": (reply_to, {"asker": "their email or number",
                            "text": "what to say as the owner",
                            "about": "optional: force a specific thread (topic or id)",
                            "close": "optional: true closes everything, false closes nothing"}),
    "add_knowledge": (add_knowledge, {
        "fact": "the fact to remember, phrased the way an ASKER would ask about it",
        "file": "destination from list_knowledge, e.g. pizza-delivery.md — "
                "choose the file that already owns the subject",
        "section": "the '## ' heading to put it under, e.g. Who or Availability. "
                   "Use one the document already has; a new one is created if it does not"}),
    "set_setting": (set_setting, {
        "field": "name, pronoun, voice or never_say",
        "value": "the value; for never_say, one topic per line"}),
    "list_knowledge": (list_knowledge, {}),
    "read_knowledge": (read_knowledge, {"file": "e.g. pizza-delivery.md"}),
    "edit_knowledge": (edit_knowledge, {
        "file": "the document to correct, e.g. about.md",
        "old": "the exact existing text to replace — must appear exactly once",
        "new": "its replacement; leave empty to delete the text"}),
    "grant_folder": (grant_folder, {"asker": "their email", "folder": "folder path",
                                    "note": "why (optional)"}),
    "revoke_folder": (revoke_folder, {"asker": "their email", "folder": "folder path"}),
    "resolve_all": (resolve_all, {"match": "question/asker substring, or empty for all"}),
    "resolve_escalation": (resolve_escalation, {"escalation_id": "the [id] shown",
                                                "note": "how it was dealt with (optional)"}),
    "index_status": (index_status, {}),
    "who_is": (who_is, {"asker": "their email or number"}),
    "conversation_with": (conversation_with, {"asker": "their email or number",
                                              "limit": "how many recent messages (default 20)"}),
    "list_people": (list_people, {}),
    "add_person": (add_person, {"asker": "their verified email or number",
                                "who": "role and relationship", "comms": "how to write to them"}),
    "note_person": (note_person, {"asker": "their email or number",
                                  "section": "Who | Comms | Folders | Always escalate",
                                  "note": "the line to add"}),
    "profile_suggestions": (profile_suggestions, {}),
    "accept_observation": (accept_observation, {"asker": "their email", "note": "what to record"}),
    "model_status": (model_status, {}),
    # attach_model is deliberately NOT in this registry. Using it would require the owner
    # to paste an API key into the assistant's chat box — which sends it to the model
    # provider and stores it in run/owner_chat.json in plaintext. Keys are entered on the
    # settings page. `model_status` stays: reading which model is attached leaks nothing.
    "list_examples": (list_examples, {}),
    "install_example": (install_example, {
        "name": "the capability name from list_examples",
        "bounds": "optional overrides, e.g. \"hours=11:00-22:00,max_quantity=4\""}),
    "list_capabilities": (list_capabilities, {}),
    "declare_capability": (declare_capability, {
        "name": "short name, e.g. pizza delivery",
        "what": "what it covers, in your words",
        "action": f"one of: {', '.join(capabilities.ACTIONS)} (default book_slot)",
        "bounds": "dict of limits, e.g. {\"hours\": \"11:00-21:00\", \"max_quantity\": 4}"}),
    "set_capability_bound": (set_capability_bound, {
        "name": "the capability name", "bound": f"one of: {', '.join(capabilities.CHECKED)}"
                                                " (others are kept as advisory)",
        "value": "the new value, or empty to remove the bound"}),
    "remove_capability": (remove_capability, {"name": "the capability name"}),
    "bookings": (list_bookings, {"day": "YYYY-MM-DD, or empty for everything"}),
    "cancel_booking": (cancel_booking, {"booking_id": "the id shown in bookings"}),
}


def people_summary(include_unverified: bool = False) -> list[dict]:
    """Everyone who has written, newest first — the entry point for reading history.

    The flat log answers "what happened at 17:19". This answers "what have we said to
    Pauline", which is how the owner actually thinks about a secretary.

    Unverified identities are excluded by default. They are not a separate KIND of person — a
    `visitor:` row is just an identity the channel could not vouch for — but a list mixing them
    in reads as if the owner has relationships they do not have, and every such row is someone
    they cannot reply to anyway. The rows are derived from the append-only log, so nothing is
    destroyed: `include_unverified=True` (the site's debug view) shows them again.
    """
    seen: dict[str, dict] = {}
    for r in rows():
        who = r["asker"]
        if not who:
            continue
        if not include_unverified and not identity.is_verified(who):
            continue
        # The OWNER is not one of the people who wrote in. Their own replies are logged as rows
        # too, so counting every asker put them in their own list of contacts — the list answers
        # "who is waiting on me", and that is never the owner.
        if str(r.get("outcome", "")).startswith("owner"):
            continue
        e = seen.setdefault(who, {"asker": who, "messages": 0, "open": 0, "last": ""})
        e["messages"] += 1
        e["last"] = max(e["last"], r["at"])
    # Count VERIFIED and unverified separately. Threads already key on verification state,
    # but this row keyed on the identity string alone — so 12 asks from someone merely
    # CLAIMING Pauline's number showed as "13 open" against Pauline, who was waiting on one
    # thing. Beyond being wrong, it lets an unverified claimant inflate a real contact's
    # badge, which is a cheap way to misdirect the owner's attention.
    open_by, claim_by = {}, {}
    for th in open_escalations():
        bucket = open_by if th["verified"] else claim_by
        bucket[th["asker"]] = bucket.get(th["asker"], 0) + 1
    for who, e in seen.items():
        e["open"] = open_by.get(who, 0)
        e["unverified"] = claim_by.get(who, 0)
        # A visitor identity must NOT resolve to the profile of whoever they claimed to be:
        # that would put "Pauline" on a row belonging to someone who merely typed her number.
        e["name"] = (identity.display(who) if not identity.is_verified(who)
                     else people.display_name(who, True))
    return sorted(seen.values(), key=lambda e: e["last"], reverse=True)


def conversation_rows(asker: str, limit: int = 60) -> list[dict]:
    """One person's exchange, oldest last — including the owner's own replies.

    Each row carries `name`: who to credit the incoming message to on the site. Resolved
    per ROW, not per person, because `verified` is a property of the message: an unverified
    turn in the same thread must NOT be captioned with the profile's name. Naming a
    claimant "Pauline" in the owner's own view is the identity confusion the whole
    verified/unverified split exists to prevent, so `display_name` returns "" there and the
    site falls back to the raw identity.
    """
    mine = [r for r in rows() if r["asker"].strip().lower() == asker.strip().lower()]
    out = []
    for r in mine[-limit:]:
        r = dict(r)
        r["name"] = _row_sender_name(r)
        out.append(r)
    return out


def _row_sender_name(r: dict) -> str:
    """How to caption one incoming message's sender, same wording as the people list.

    Treated as unverified when EITHER the identity id says so (the `v-` visitor prefix) OR
    the row's own `verified` flag is false. The second half matters for rows written before
    the prefix existed: they carry only the flag, and resolving one to the profile of
    whoever it claimed to be is the exact confusion the split prevents.
    """
    who = r.get("asker", "")
    if not identity.is_verified(who) or not r.get("verified"):
        return identity.display(who)
    return people.display_name(who, True)


#: View preferences. NOT settings.md: that file is parsed by heading and holds what the AGENT
#: is (name, pronoun, never-say) — a knowledge edit that renamed a heading once silently emptied
#: the never-say list, so it is not a place to put unrelated keys. This is derived instance
#: state, which is what run/ is for.
#:
#: Server-side rather than localStorage because the owner site is rendered by THREE engines now
#: (browser, WebKitGTK in the pywebview window, WebKit in the macOS .app) and the window has no
#: localStorage at all — referencing it there raises ReferenceError.
UI_PREFS = paths.RUN / "ui.json"


def ui_prefs() -> dict:
    try:
        return json.loads(UI_PREFS.read_text())
    except (OSError, ValueError):
        return {}


def set_ui_pref(key: str, value) -> str:
    prefs = ui_prefs()
    prefs[key] = value
    try:
        UI_PREFS.parent.mkdir(parents=True, exist_ok=True)
        UI_PREFS.write_text(json.dumps(prefs, indent=2))
    except OSError as exc:
        return f"could not save: {exc}"
    return f"{key} = {value}"


def state() -> dict:
    """Structured snapshot for the web dashboard (chat is not good at showing state).

    The CHANNEL belongs in here, not on one route. There are three consumers — GET /api/state,
    the canvas-submit push, and the log-watcher push — and adding it to only the first meant
    every websocket push overwrote the header chip with a channel-less state, i.e. "connect a
    number" on an install that was connected. Same enumeration-drift failure as the MCP face.
    """
    from . import status
    all_rows = rows()
    today = date.today().isoformat()
    esc = open_escalations()
    perms = permissions.load()
    return {
        "channel": status.snapshot(),
        "ui": ui_prefs(),
        "today": today,
        "total": len(all_rows),
        "today_count": len([r for r in all_rows if r["at"].startswith(today)]),
        "escalations": esc[:20],
        "aged_out": aged_out(),
        "pending_delivery": asker_actions.pending_delivery_count(),
        "recent": all_rows[-20:][::-1],
        "people": people_summary(),
        # Same list with the unverified identities left in, for the site's debug view — the
        # owner can still see who has been claiming an identity without it cluttering the list
        # of people they actually deal with.
        "people_all": people_summary(include_unverified=True),
        "permissions": {
            "default": perms.get("default", {}).get("folders", []),
            "askers": {k: v.get("folders", []) for k, v in perms.get("askers", {}).items()},
        },
    }
