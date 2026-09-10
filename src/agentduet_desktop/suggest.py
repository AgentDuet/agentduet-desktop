"""Read what was said, and offer ONE thing to do about it — a calendar entry.

The owner sees a small line under the balloon it came from: what the event would be, and a
button. Pressing it opens Google Calendar prefilled, and they still press Save there. Nothing
here writes to a calendar, and nothing here acts on its own.

WHY THIS IS NOT A TOOL CALL. The assistant can already do this when ASKED — `add_to_calendar`
is in its registry. That needs the owner to notice the appointment, remember the assistant can
help, and type a request. The whole value of this pass is that it happens without being asked,
next to the words that prompted it.

FOUR THINGS IT MUST NOT DO, and the shape follows from them.

1. **Never run on a render.** The hub polls, so a model call on the request path would put the
   owner's own page behind a model that can take seconds. The pass runs on the daemon's queue
   and the page reads a stored verdict.
2. **Never ask twice.** A verdict is stored against a DIGEST of the exact text the model read —
   including a NEGATIVE verdict, which is most of them. Without storing "nothing here" a quiet
   inbox would re-ask the model about the same message every time the queue turned over,
   forever. Keying on the digest rather than on a message id also means a suggestion cannot
   outlive the text it came from: when a transcript lands and replaces "pending", the digest
   changes and the old verdict simply stops matching.
3. **Never show a guess.** The model returns typed fields and CODE decides: they must survive
   `links.calendar_url()`, and the date must fall inside `WINDOW_DAYS` of today. A hallucinated
   date, a month name where a date belongs, an event in 1970 — each fails that and is dropped
   rather than rendered. This is the house rule (the model reads, code decides) applied to a
   suggestion instead of to an action.
4. **Never become noise.** One suggestion per item, and the prompt is told to return nothing
   unless a specific date and time were actually agreed. A button under every message is worse
   than no buttons, because then none of them get read.

WHOSE IDEA IT WAS. The text this reads is written by whoever called or wrote in, so a caller can
say "add a calendar entry for Friday" and get a suggestion in front of the owner. That is
acceptable here and worth being precise about why: the suggestion is rendered UNDER THAT
PERSON'S OWN BALLOON, which is the strongest provenance the UI has — the owner reads the words
and the offer together — it commits nothing, and the two steps after it (the click, then Save in
Google) are both the owner's. What it must never gain is the ability to act by itself.
"""

import hashlib
import json
import logging
from datetime import date, datetime, timedelta

from . import paths

logger = logging.getLogger("dduet.suggest")

#: Where verdicts live. Derived instance state, so `run/` — the same argument as `ui.json`:
#: `settings.md` is parsed by heading and holds what the AGENT is, not a cache.
STORE = paths.RUN / "suggestions.json"

#: How far back to look for something to judge. A year of history judged by a local model on
#: first launch would be hours of work nobody asked for, and an appointment from March is not
#: worth offering to add now.
DAYS = 14

#: The furthest ahead a suggested event may be. A date beyond it is taken as a misread rather
#: than a plan — models reach for 2099 when they are guessing. There is no window BEHIND:
#: anything already past is refused outright, since it cannot be an appointment to keep.
WINDOW_DAYS = 365

#: How many items one pass will judge. The queue comes round again, so this is a pace limit
#: rather than a cap: it keeps a backlog from occupying the model for minutes at a stretch on a
#: machine that is also transcribing.
BATCH = 3

#: How long between passes. Nothing waits on this, and a suggestion arriving a minute after the
#: message is no worse than one arriving instantly.
POLL_SECONDS = 45

#: Enough of a message to judge. An appointment is agreed in a sentence or two; handing over a
#: whole 4,000-character transcript makes the model likelier to invent something to find.
MAX_CHARS = 2000

#: Verdict states. "" is the model's own "nothing here", which is stored like any other.
DISMISSED = "dismissed"
ADDED = "added"


def _load() -> dict:
    try:
        rows = json.loads(STORE.read_text())
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(rows: dict) -> None:
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(rows, indent=2))
    except OSError as exc:
        logger.warning("could not store suggestions: %s", exc)


def digest(text: str) -> str:
    """The key for one piece of text. Short, and stable across restarts."""
    return hashlib.sha256(" ".join((text or "").split()).encode()).hexdigest()[:16]


def for_texts(texts: list[str]) -> dict:
    """{digest: suggestion} for the texts a page is about to render. NO MODEL, no network.

    Only live suggestions come back — a dismissed or already-added one is a verdict we keep so
    it is not offered again, not something to draw.
    """
    rows = _load()
    out = {}
    for t in texts:
        key = digest(t)
        hit = rows.get(key)
        if hit and hit.get("kind") == "calendar" and not hit.get("state"):
            out[key] = {"title": hit.get("title", ""), "start": hit.get("start", ""),
                        "end": hit.get("end", ""), "when": hit.get("when", "")}
    return out


def resolve(key: str, action: str) -> str:
    """Act on one suggestion, on the owner's click. THE ONLY PLACE A CALENDAR PAGE OPENS."""
    from . import links
    rows = _load()
    hit = rows.get(key)
    if not hit or hit.get("kind") != "calendar":
        return "That suggestion is no longer there."
    if hit.get("state"):
        return "Already dealt with."
    if action == "dismiss":
        hit["state"] = DISMISSED
        _save(rows)
        return "Dismissed."
    out = links.add_to_calendar(hit.get("title", ""), hit.get("start", ""), hit.get("end", ""))
    # MARKED ONLY IF IT OPENED. `add_to_calendar` returns its own refusal rather than raising,
    # so marking unconditionally would retire a suggestion the owner never actually saw — and
    # there would be no way back to it.
    if out.startswith("Opened"):
        hit["state"] = ADDED
        _save(rows)
    return out


#: WHAT COUNTS AS AN APPOINTMENT, and the wording is load-bearing.
#:
#: THIS ASKED WHETHER THE TWO PEOPLE **AGREED**, and that was too strict — measured against
#: `gemini-flash-latest` on 2026-09-10, which follows the instruction literally. A real call
#: where the owner said "I want to go for lunch tomorrow at 11am" and the other side never
#: confirmed came back `{}`: correct to the letter, and useless to the owner, who had just said
#: a time out loud and would want it captured. `qwen3-8b` offered it anyway, which hid the
#: problem — the loose model looked like the prompt working.
#:
#: So the bar is now that the conversation NAMES a specific appointment, by either party.
#: Agreement is not required, because the owner is the one who decides — the offer is a button
#: they can ignore, and a missed offer costs more than a declined one.
#:
#: WHAT STAYS STRICT: a vague plan, something already past, and anything the model is unsure
#: of. Those were never the complaint.
PROMPT = """Read this conversation and decide whether it names a specific appointment: a \
meeting, a call back, a delivery, a visit, a lunch — with a date and a time.

Today is %s.

Answer with one line of JSON and nothing else.

If an appointment is named, answer:
{"title": "what it is, 6 words at most", "start": "YYYY-MM-DD HH:MM", "end": "YYYY-MM-DD HH:MM"}

If none is, answer:
{}

EITHER PERSON may name it. They do not have to agree out loud, and one of them saying "lunch \
tomorrow at 11am" is enough.

Answer {} unless there is a real date and a real time. Say {} for a vague plan ("next week \
sometime", "I will call you"), for a time that has already passed, and for anything you are \
unsure of. Do not invent a time that was not said.

CONVERSATION:
%s"""


def _judge(text: str, model_client) -> dict:
    """Ask the model about one item. Returns validated fields, or {} for nothing to offer."""
    from . import links
    raw = model_client.complete(PROMPT % (date.today().strftime("%A %d %B %Y"),
                                          text[:MAX_CHARS]))
    body = (raw or "").strip()
    # THE JSON OUT OF THE MIDDLE OF WHATEVER IT SAID. A weak model wraps the object in prose or
    # a code fence however plainly it is asked not to, and throwing that away would drop good
    # answers. Anything with no object at all is the "nothing here" case.
    if "{" not in body or "}" not in body:
        return {}
    try:
        found = json.loads(body[body.index("{"):body.rindex("}") + 1])
    except ValueError:
        return {}
    if not isinstance(found, dict) or not found.get("title") or not found.get("start"):
        return {}
    title = str(found.get("title", ""))[:80]
    start, end = str(found.get("start", "")), str(found.get("end", "") or "")
    # CODE DECIDES. The fields have to survive the same builder the button will use, so a date
    # the model invented in the wrong shape fails HERE, where nothing has been shown yet,
    # instead of under the owner's cursor.
    try:
        links.calendar_url(title, start, end)
        when = links._moment(start)
    except ValueError as exc:
        logger.info("discarded a suggestion: %s", exc)
        return {}
    # ALREADY HAPPENED, so there is nothing to put in a calendar. CODE decides this rather
    # than trusting the prompt's "say {} for a time that has already passed": `qwen3-8b`
    # offered 10:00 on a day when it was already 13:45, and the prompt had asked it not to.
    # A model that is wrong about the clock must not be able to put a stale event on screen.
    if when < datetime.now().astimezone():
        logger.info("discarded a suggestion dated %s — already past", when)
        return {}
    if (when.date() - date.today()).days > WINDOW_DAYS:
        logger.info("discarded a suggestion dated %s — too far ahead", when.date())
        return {}
    return {"kind": "calendar", "title": title, "start": start, "end": end,
            "when": when.strftime("%a %d %b, %H:%M")}


def _recent(text_at: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Only what is worth judging: recent, long enough to hold a plan, and not yet judged."""
    rows = _load()
    cutoff = (datetime.now() - timedelta(days=DAYS)).isoformat(timespec="seconds")
    out = []
    for text, at in text_at:
        if not text or len(text.split()) < 4:
            continue
        if at and at < cutoff:
            continue
        if digest(text) in rows:
            continue
        out.append((text, at))
    return out


def candidates() -> list[tuple[str, str]]:
    """(text, when) for every message and call transcript in the window. No model."""
    from . import calls, carry, tools
    items: list[tuple[str, str]] = []
    for r in tools.rows():
        if r.get("network") not in ("WA", "DDUET"):
            continue
        # THEIRS AND OURS TOGETHER, because an appointment is agreed across the two: "Tuesday
        # at ten?" / "Yes, that works" says nothing on its own from either side alone.
        both = "\n".join(p for p in (r.get("question", ""), r.get("answer", "")) if p)
        items.append((both, r.get("at", "")))
    for who_rows in calls.by_person().values():
        for r in who_rows:
            af, names = carry.call_audio(r.get("recordings", []), r.get("call_id", ""))
            items.append((carry.transcript_of(names, af), r.get("at", "")))
    return _recent(items)


def analyse_once() -> int:
    """Judge up to `BATCH` items. Returns how many were judged. NEVER RAISES."""
    from . import llm
    if not llm.configured():
        return 0
    todo = candidates()[:BATCH]
    if not todo:
        return 0
    try:
        client = llm.client()
    except Exception as exc:                      # a missing key, a provider that will not load
        logger.info("no model for suggestions: %s", exc)
        return 0
    done = 0
    rows = _load()
    for text, at in todo:
        key = digest(text)
        try:
            verdict = _judge(text, client)
        except Exception as exc:                  # one bad item must not stop the queue
            logger.warning("could not judge an item: %s", exc)
            continue
        # STORED EITHER WAY. The empty verdict is the whole reason a quiet inbox does not
        # re-ask the model about the same message for the life of the daemon.
        rows[key] = {**verdict, "at": at, "judged": datetime.now().isoformat(timespec="seconds")}
        if verdict:
            logger.info("suggesting a calendar entry: %r at %s",
                        verdict["title"], verdict["start"])
        done += 1
    _save(rows)
    return done


def summary() -> str:
    """One line for `status`: what the pass has done, and what it is waiting on.

    THIS EXISTS FOR A REMOTE TESTER'S BUG REPORT. With no model the feature is silently absent
    — correctly, since the alternative is an on-screen apology for a gap — and "I see no
    suggestions" then means either that, or a model that judged everything and found nothing,
    or a backlog still being judged. Those need different answers and the screen cannot tell
    them apart, so the diagnostic belongs here, where someone is already looking for one.
    """
    from . import llm
    judged = len(_load())
    waiting = len(candidates())
    if not llm.configured():
        return f"no model, so nothing is judged ({waiting} would be)"
    offered = len([1 for v in _load().values()
                   if v.get("kind") == "calendar" and not v.get("state")])
    return (f"{judged} judged, {offered} offered"
            + (f", {waiting} waiting" if waiting else ""))


async def worker() -> None:
    """Judge new items forever, off the event loop.

    On a THREAD: the loop this shares carries call audio, and a model call blocks for seconds.
    Sleeps FIRST, so a launch is never held up by it.
    """
    import asyncio
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            await asyncio.to_thread(analyse_once)
        except Exception as exc:                  # a worker that dies takes the feature with it
            logger.error("the suggestion pass hit %s: %s", type(exc).__name__, exc)
