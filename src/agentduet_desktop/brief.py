"""A short running summary of each person the owner talks to, kept up to date in the background.

WHY. Asked about someone, the assistant was handed up to five of their full transcripts on every
question — about 5,000 tokens, re-read from scratch each time the owner opened a different
person. A brief of ~150 words carries what matters (who they are, what is open, the last
contact), and only calls it has not absorbed yet still go in whole.

SOURCES: that person's calls, and the owner's chat with the assistant ABOUT them (a turn asked
while their page was open). Shaped only by the prompt below — nobody edits a brief by hand.

LATEST WINS. Each update gives the model the current brief plus the new, DATED information, and
tells it the newer fact replaces the older where they disagree, and that the owner's word
outranks the caller's about the same thing. The dates it keeps are what let the next update
tell which fact is newer.

INCREMENTAL, NEVER REGENERATED. An update folds only what is new into the current brief. A
summary rewritten from everything each time wears facts away, and a small model compounds its
own mistakes.

WATERMARKS. A brief records the newest call and the newest chat turn it absorbed. An update
with nothing newer ends before touching the model — which is what makes `sweep()` after every
merge cheap, and makes a restart harmless.

A brief is DERIVED FROM WHAT A CALLER SAID, so where it enters a prompt it is marked with
`tools.untrusted`, the same as the transcripts it came from.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from . import gate, jobs, paths

logger = logging.getLogger("secretary.brief")

#: Calls folded in one update. More wait for the next one, which is requested straight away.
CALLS_PER_UPDATE = 3
#: Characters of one transcript given to an update.
CALL_CHARS = 4000

#: TWO PASSES, ONE JOB EACH (2026-10-01). One prompt kept all three parts and they competed: an
#: allergy that came up while choosing lunch went into the plan, a trip went into About, and every
#: rewording traded one for the other (4-5 of 16 checks failing in tests/test_briefs.py, whatever
#: the wording). About is lasting and grows slowly; Open comes and goes and is all dates — so each
#: is its own question, and "Last contact" is not asked at all: code knows it exactly.

ABOUT_PROMPT = """You keep the ABOUT part of a brief about one person {owner} talks to. {owner}'s
assistant reads it before answering questions about them.

THIS BRIEF IS ABOUT: {person}. Never write about anyone else as if they were this person.
NEVER WRITE THEIR NAME OR NUMBER: it is shown beside it, and changes when {owner} renames them.

ABOUT is LASTING facts about the person that would help the next time {owner} deals with them —
for example an allergy, that they drive, where they work, what they usually call about. A
lasting fact counts even when it came up while making a plan: an allergy mentioned while choosing
lunch, or "I'll drive" while planning a trip.
NOT plans, appointments, one-off events or what a call was about — those are kept elsewhere.
A fact is still true next month: "They drive" is a fact; "they will drive to the client on
Friday" is a plan, and "they will be late today" is an event — neither goes here. Keep the name
of the place they work if it was said.

- KEEP every fact already in ABOUT unless the new information changes it. The newer one wins, and
  {owner}'s word outranks theirs.
- Say who they are to {owner} (a customer, a friend) ONLY if it was said — never guess it.
- Never write which language they speak: that is kept separately, from what code counts.
- Use only what is below. Do not guess.
- At most {words} words, as short plain sentences.

Reply with the ABOUT text only, never these rules. If nothing at all is known, reply: NONE

CURRENT ABOUT:
{current}

NEW INFORMATION, oldest first:
{new}
"""

OPEN_PROMPT = """You keep the list of OPEN items for one person {owner} talks to: every
appointment, meeting, visit, delivery, promise or follow-up either side mentioned that has not
happened yet. A request to call back, or a promise to call, is a follow-up.
NOT facts about the person — those are kept elsewhere.

- One item per line, exactly:  YYYY-MM-DD HH:MM — what
  or, when no time was said:   YYYY-MM-DD — what
- Work out each date from the dates written beside the call it was said in. Never invent a date
  or a time.
- KEEP every item already in the list unless the new information changes, cancels or completes
  it. A call about one thing says nothing about the others.
- Never list what the assistant did or will do (running a tool, drafting).
- Never list the call or message itself: it has already happened. Only what it arranged.

Reply with the lines only, never these rules. If there are none, reply: NONE

CURRENT LIST:
{current}

NEW INFORMATION, oldest first:
{new}
"""

#: A CORRECTION IS NOT A CONTACT (2026-10-01): given as the newest dated item it came back as
#: "Last contact: … CORRECTION from Stanley: name". So it has its own section, in both passes.
CORRECTION = """
CORRECTION FROM {owner} — not a conversation. Apply it: change or remove whatever it says is
wrong, keep everything else, and never mention the correction itself.
{correction}
"""

#: An Open line as the pass writes it. Anything else is not an item.
_OPEN_LINE = re.compile(r"^\s*[-*•]?\s*(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?\s*[—–-]+\s*(.+?)\s*$")


def _now() -> datetime:
    """Now. One place, so a test can set the clock the summary is judged by."""
    return datetime.now()


def _dir():
    return paths.RUN / "briefs"


def _file(who: str):
    return _dir() / (re.sub(r"[^A-Za-z0-9+@._-]+", "_", who.strip()) + ".json")


def load(who: str) -> dict:
    """The record, with `summary` rendered NOW from its parts — so an appointment that has
    passed is gone from it without anything rewriting the file."""
    try:
        rec = json.loads(_file(who).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if "about" in rec or "open" in rec:
        rec["summary"] = _render(rec)
    return rec


def _upcoming(items: list[dict]) -> list[dict]:
    """Open items not yet past, soonest first. A date with no time lasts the whole day."""
    now, out = _now(), []
    for it in items:
        try:
            when = datetime.fromisoformat(it["when"])
        except (KeyError, ValueError):
            continue
        if (when if len(it["when"]) > 10 else when.replace(hour=23, minute=59)) >= now:
            out.append(it)
    return sorted(out, key=lambda it: it["when"])


def _render(rec: dict) -> str:
    """The three parts as the card and the assistant read them. Code writes this, not a model."""
    about = " ".join(x for x in (rec.get("about") or "", _language_line(rec)) if x).strip()
    lines = ["About: " + (about or "Nothing yet."), "Open:"]
    for it in _upcoming(rec.get("open") or []):
        when = datetime.fromisoformat(it["when"])
        day = f"{when:%A} {when.day} {when:%B}" + (f", {when:%H:%M}" if len(it["when"]) > 10 else "")
        lines.append(f"{day} — {it['what']}")
    if len(lines) == 2:
        lines[-1] = "Open: None."
    # An older one-pass brief has no `last`: how far it read is when the last contact was.
    last = rec.get("last") or {}
    if not last.get("at"):
        call, chat = rec.get("through_call", ""), rec.get("through_chat", "")
        if call or chat:
            last = {"at": max(call, chat), "how": "a call" if call >= chat else "a message"}
    if last.get("at"):
        lines.append(f"Last contact: {_day(last['at'])}, {last.get('how', 'a call')}.")
    return "\n".join(lines)


def _parse_open(text: str) -> list[dict]:
    """The pass's lines as items. A line in any other shape is dropped, never guessed at."""
    out = []
    for line in (text or "").splitlines():
        m = _OPEN_LINE.match(line)
        if not m:
            continue
        date, time, what = m.groups()
        when = f"{date} {time.zfill(5)}" if time else date
        try:
            datetime.fromisoformat(when)
        except ValueError:
            continue
        out.append({"when": when, "what": what.strip().rstrip(".")})
    return out


def _sections(summary: str) -> tuple[str, str]:
    """An older one-pass summary as (about, open text), for the first two-pass update."""
    about = re.search(r"(?:About|Who):\s*(.*?)(?=\n\s*Open:|$)", summary or "", re.S)
    opened = re.search(r"Open:\s*(.*?)(?=\n\s*Last contact:|$)", summary or "", re.S)
    return ((about.group(1).strip() if about else ""), (opened.group(1).strip() if opened else ""))


def _save(who: str, rec: dict) -> None:
    f = _file(who)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def _transcript(row: dict) -> str | None:
    """The call's transcript text, "" for a call with nothing to say, None while still coming."""
    from . import carry
    if row.get("note") in ("missed", "missed in the app"):
        return ""
    folder, names = carry.call_audio(row.get("recordings", []), row.get("call_id", ""))
    # NO AUDIO AT ALL is nothing said, not "still coming". The index is written when a call ends,
    # with its legs already on disk, so a row with no files will never have a transcript — and
    # waiting a day on one blocked every later call of that person behind it. (Found on a call
    # that rang out before calls were labelled "missed".)
    if not names:
        return ""
    for n in names:
        t = (folder / n).with_suffix(".txt")
        if t.is_file():
            return carry.read_body(t)[:CALL_CHARS]      # the words, not the call's header
    # NOT YET TRANSCRIBED — or never will be. A row a day old with no transcript is not coming.
    try:
        age = (datetime.now() - datetime.fromisoformat(row.get("at", ""))).total_seconds()
    except ValueError:
        return ""
    return None if age < 86400 else ""


def _calls_after(who: str, after: str) -> tuple[list[tuple[str, str]], bool]:
    """(calls newer than `after`, oldest first, with their text), and whether more remain.

    Stops at the first call still being transcribed, so the watermark never jumps past one.
    """
    from . import calls as _calls
    rows = [r for r in _calls.for_person(who) if (r.get("at") or "") > after]
    rows.sort(key=lambda r: r.get("at", ""))
    out = []
    for r in rows:
        text = _transcript(r)
        if text is None:
            break
        out.append((r.get("at", ""), text))
        if len(out) >= CALLS_PER_UPDATE:
            return out, len(rows) > len(out)
    return out, False


def _about_them(q: str, who: str) -> bool:
    """Whether a question asked on `who`'s page is ABOUT them (2026-09-30).

    Asked on Cen's page, "email Kok Choong" is about Kok Choong — and filed by page it made Cen's
    brief say "Who: Kok Choong". A question that names this person counts; one that names
    somebody else does not; one that names nobody is about the page it was asked on.
    """
    from . import calls, names
    if names.mentions(q, who):
        return True
    return not any(names.mentions(q, other) for other in calls.people() if other != who)


def _chat_after(who: str, after: str) -> list[tuple[str, str]]:
    """The owner's words to the assistant about `who`, newer than `after`, oldest first.

    THE OWNER'S WORDS ONLY — not the assistant's reply, whose "I need to run a tool" came back
    as an open item in the brief.
    """
    from .assistant import OwnerChat
    try:
        shown = json.loads(OwnerChat.STORE.read_text())
    except (OSError, ValueError):
        return []
    return [(t.get("at", ""), t.get("q", "")) for t in shown
            if t.get("about") == who and "q" in t and not t.get("pending")
            and (t.get("at") or "") > after and _about_them(t.get("q", ""), who)]


def _person(who: str) -> str:
    """How the prompt names them: the name the hub shows, or plainly that there is none."""
    from . import names
    name = names.name_for(who)
    return f"{name} ({who})" if name and name != who else \
        f"a person known only by their number, {who} — do not give them a name"


def _current(rec: dict) -> tuple[str, str]:
    """(about, open lines) as the passes read them — from the parts, or an older summary."""
    if "about" in rec or "open" in rec:
        lines = "\n".join(f"{it['when']} — {it['what']}" for it in rec.get("open") or [])
        return rec.get("about") or "", lines
    return _sections(rec.get("summary", ""))


#: WHAT THE MODEL MAY NOT INVENT, checked by code (2026-10-01). Told twice in its prompt not to
#: guess, it still wrote "Customer." about an accountant nobody called a customer, and put "if we
#: do lunch" on Monday 5 October.
_RELATIONS = ("customer", "client", "colleague", "coworker", "co-worker", "supplier", "vendor",
              "friend", "partner", "boss", "manager", "employee", "staff")
_WEEKDAYS_ZH = ("一", "二", "三", "四", "五", "六", "日")


#: Which relationship each word names. A word not here (partner, manager…) is too ambiguous to
#: check against a category, so it stands only when SOME relationship was stated.
_RELATION_KIND = {"customer": "customer", "client": "customer", "colleague": "colleague",
                  "coworker": "colleague", "co-worker": "colleague", "supplier": "supplier",
                  "vendor": "supplier", "friend": "friend"}
_RELATION_SENTENCE = {"customer": "A customer.", "colleague": "A colleague.",
                      "supplier": "A supplier.", "friend": "A friend."}
#: A relationship the decision model found is WRITTEN into About only above this confidence; below
#: it, it only stops a sentence that names it from being dropped. Measured: right answers came at
#: 0.25-0.72, so the bar is for adding a claim, not for keeping one the model already made.
RELATION_ADD = 0.5


def _stated_relation(said: str) -> tuple[str, float] | None:
    """(relationship the conversations state, confidence) from the decision model — "not said"
    included — or None when the model is not available, so the word rule decides instead."""
    from . import decider, owner
    if not decider.ready():
        decider.fetch_in_background()
        return None
    name = owner.name() or "the owner"
    q = decider.choice(
        f"Going only by what was SAID in these conversations, what is the caller to {name}? "
        f"Choose 'not said' unless they make the relationship clear.",
        {"not said": "the conversations do not make the relationship clear",
         "customer": f"the caller buys from {name} or his company",
         "colleague": f"the caller works with {name}",
         "supplier": f"the caller sells to, or provides a service to, {name}",
         "friend": "a personal friend or family member"})
    got = (decider.ask(said, {"relation": q}) or {}).get("relation")
    return (got["choice"], float(got.get("confidence", 0))) if got else None


def _keep_relations(about: str, relation: str | None) -> str:
    """About with only the relationship that was stated: a sentence naming another kind goes, and
    with none stated every relationship sentence goes."""
    keep = []
    for sent in re.split(r"(?<=[.!?])\s+", about.strip()):
        low = sent.lower()
        named = [r for r in _RELATIONS if re.search(rf"\b{re.escape(r)}s?\b", low)]
        if named and (relation is None or any(_RELATION_KIND.get(r, relation) != relation for r in named)):
            continue
        keep.append(sent)
    return " ".join(keep).strip()


def _said_relations(about: str, said: str) -> str:
    """THE FALLBACK, when the decision model is not here: About without any sentence naming a
    relationship word the conversations never used. Cruder — a word can be said without stating a
    relationship ("drive us to the client") — which is what the model replaced."""
    low = said.lower()
    keep = [s for s in re.split(r"(?<=[.!?])\s+", about.strip())
            if not any(r in s.lower() and r not in low for r in _RELATIONS)]
    return " ".join(keep).strip()


def _dated_by(item: dict, sources: list[tuple[str, str]]) -> bool:
    """Whether an Open item's date can be traced to what was said in a call or message."""
    try:
        when = datetime.fromisoformat(item["when"]).date()
    except (KeyError, ValueError):
        return False
    for at, text in sources:
        try:
            said_on = datetime.fromisoformat(at).date()
        except ValueError:
            continue
        low = text.lower()
        offset = (when - said_on).days
        if (when.strftime("%A").lower() in low
                or (offset == 0 and re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", low))
                or (offset == 1 and "tomorrow" in low)
                or re.search(rf"\b{when.day}(st|nd|rd|th)?\b", low) and when.strftime("%b").lower() in low
                or f"星期{_WEEKDAYS_ZH[when.weekday()]}" in text or f"周{_WEEKDAYS_ZH[when.weekday()]}" in text
                or (offset == 1 and ("明天" in text or "明日" in text))
                or (offset == 0 and "今天" in text)):
            return True
    return False


def _passes(who: str, rec: dict, items: list[str], correction: str = "",
            sources: list[tuple[str, str]] | None = None) -> tuple[str, list[dict]] | None:
    """Both passes. (about, open items), or None when the model returned nothing for either."""
    from . import budget, llm, owner
    name = owner.name() or "the owner"
    about_now, open_now = _current(rec)
    new = "\n\n".join(items) if items else "(nothing new)"
    fix = CORRECTION.format(owner=name, correction=correction) if correction else ""
    client = llm.client()
    about = client.complete(ABOUT_PROMPT.format(
        owner=name, person=_person(who), words=max(40, budget.split()["person_words"] * 2 // 3),
        current=about_now or "(nothing yet)", new=new) + fix).strip()
    opened = client.complete(OPEN_PROMPT.format(
        owner=name, current=open_now or "(none)", new=new) + fix).strip()
    if not about and not opened:
        return None
    about = "" if about.upper().rstrip(".") == "NONE" else about
    # A PASS THAT RETURNED NOTHING USABLE KEEPS WHAT WAS THERE: an empty or rule-shaped reply is
    # not "they have no plans".
    items_out = _parse_open(opened) if opened.upper().rstrip(".") != "NONE" else []
    if opened and not items_out and opened.upper().rstrip(".") != "NONE":
        items_out = rec.get("open") or []
    if sources is not None:
        # NEW ITEMS NEED A DATE SOMEONE SAID; items already kept, and a correction's, stand.
        before = {(it["when"], it["what"]) for it in rec.get("open") or []}
        items_out = [it for it in items_out
                     if (it["when"], it["what"]) in before or _dated_by(it, sources)]
        said = "\n".join(text for _, text in sources)
        found = _stated_relation(said)
        if found is None:
            about = _said_relations(about, said + "\n" + (rec.get("about") or "")) if about else about
        else:
            label, conf = found
            # REMEMBERED: a call that does not restate a relationship does not unsay it.
            if label != "not said":
                rec["relation"] = {"label": label, "confidence": round(conf, 3), "at": _now().isoformat(timespec="seconds")}
            known = (rec.get("relation") or {})
            about = _keep_relations(about, known.get("label"))
            if (known.get("label") and known.get("confidence", 0) >= RELATION_ADD
                    and not any(re.search(rf"\b{re.escape(r)}s?\b", about.lower()) for r in _RELATIONS)):
                about = (_RELATION_SENTENCE[known["label"]] + " " + about).strip()
    return (about or rec.get("about", "")), items_out


#: Scripts a caller's words can be in, and the language each says. CODE SEES THIS, so the model
#: only has to keep it (2026-10-01): asked to "say which language they spoke", it never did —
#: a caller's Mandarin went unremarked in every run, with every wording.
_SCRIPTS = (("Chinese", re.compile(r"[\u4e00-\u9fff]")), ("Japanese", re.compile(r"[\u3040-\u30ff]")),
            ("Korean", re.compile(r"[\uac00-\ud7af]")), ("Thai", re.compile(r"[\u0e00-\u0e7f]")),
            ("Tamil", re.compile(r"[\u0b80-\u0bff]")), ("Hindi", re.compile(r"[\u0900-\u097f]")),
            # Letters ONLY Vietnamese uses — not French or Spanish é, à, ñ, so they cannot trip it.
            ("Vietnamese", re.compile(r"[ăắằẳẵặâấầẩẫậđêếềểễệôốồổỗộơớờởỡợưứừửữựảạẻẹẽỉịĩỏọủụũỷỵỹ]", re.I)))


#: A LANGUAGE MUST BE SPOKEN, NOT GLIMPSED (Stanley, 2026-10-01). The speech engine misreads short
#: sounds — an English "mm" or "ah" can come back as 嗯 or 啊, a "Hi" as Cantonese — and once
#: About holds "speaks Chinese" every later update keeps it. A misread is a short piece inside an
#: English turn; someone who really speaks a language says at least one whole turn in it. So a
#: language counts only for a TURN that is LANGUAGE_SHARE in it, with LANGUAGE_MIN of it —
#: characters for scripts written without spaces (Chinese, Japanese, Thai, Korean…), words for
#: Vietnamese. Fillers never count. A code-switcher who mixes inside every turn is missed: the
#: cautious direction.
LANGUAGE_SHARE = 0.8
LANGUAGE_MIN = 10
#: Vietnamese is measured by WORDS carrying a Vietnamese-only letter, and many of its words carry
#: no mark at all ("em", "anh", "mai") — a sentence wholly in Vietnamese scores about half.
VIETNAMESE_SHARE = 0.3
_FILLERS = re.compile(r"[嗯啊哦呃嘛吧呀哈喔噢唉诶欸咯啦呢]")
_PUNCT = re.compile(r"[\s\W_]+", re.U)


def _turn_language(turn: str) -> tuple[str, int]:
    """(the language a turn is spoken in, how much of it) if it is one of _SCRIPTS — or ("", 0)."""
    turn = _FILLERS.sub("", turn)
    for name, pat in _SCRIPTS:
        if name == "Vietnamese":
            words = [w for w in re.split(r"\s+", turn) if re.search(r"\w", w)]
            units = sum(1 for w in words if pat.search(w))
            total = len(words)
        else:
            letters = _PUNCT.sub("", turn)
            units = len(pat.findall(letters))
            # Latin words count as one unit each against the script's characters.
            total = units + len(re.findall(r"[A-Za-z]+", turn))
        share = VIETNAMESE_SHARE if name == "Vietnamese" else LANGUAGE_SHARE
        enough = (total if name == "Vietnamese" else units) >= LANGUAGE_MIN
        if enough and units / max(1, total) >= share:
            return name, (total if name == "Vietnamese" else units)
    return "", 0


def _language_turns(text: str) -> dict[str, list[int]]:
    """{language: [size of each of the caller's turns spoken in it]} for one call."""
    out: dict[str, list[int]] = {}
    for line in (text or "").splitlines():
        if line.startswith("them:"):
            name, size = _turn_language(line[len("them:"):])
            if name:
                out.setdefault(name, []).append(size)
    return out


#: THE LANGUAGE IS CODE'S, NOT THE MODEL'S (Stanley, 2026-10-01). A tally per person, across
#: calls, and a sentence only past a threshold — so one misheard sentence never lands, and one
#: that slips through is diluted by the next calls rather than repeated by the model.
#: Shown with LANGUAGE_SHOW_TURNS turns AND (LANGUAGE_SHOW_CALLS calls OR one turn of
#: LANGUAGE_LONG); said firmly ("Speaks X.") with LANGUAGE_FIRM_TURNS turns over the calls.
LANGUAGE_SHOW_TURNS = 2
LANGUAGE_SHOW_CALLS = 2
LANGUAGE_LONG = 20
LANGUAGE_FIRM_TURNS = 3


def _tally_languages(rec: dict, calls: list[tuple[str, str]]) -> None:
    """Add these calls' qualifying turns to the person's tally."""
    seen = rec.setdefault("languages", {})
    for at, text in calls:
        for name, sizes in _language_turns(text).items():
            t = seen.setdefault(name, {"turns": 0, "calls": 0, "longest": 0, "last": ""})
            t["turns"] += len(sizes)
            t["calls"] += 1
            t["longest"] = max(t["longest"], max(sizes))
            t["last"] = max(t["last"], at)


def _language_line(rec: dict) -> str:
    """The language sentence for About, from the tally and the owner's corrections, or ""."""
    off = set(rec.get("languages_off") or [])
    forced = [n for n in rec.get("languages_on") or [] if n not in off]
    firm, some = list(forced), []
    for name, t in sorted((rec.get("languages") or {}).items()):
        if name in off or name in forced:
            continue
        shown = t["turns"] >= LANGUAGE_SHOW_TURNS and (t["calls"] >= LANGUAGE_SHOW_CALLS
                                                       or t["longest"] >= LANGUAGE_LONG)
        if not shown:
            continue
        (firm if t["calls"] >= 2 and t["turns"] >= LANGUAGE_FIRM_TURNS else some).append(name)
    out = []
    if firm:
        out.append(f"Speaks {' and '.join(firm)}.")
    if some:
        out.append(f"Has spoken some {' and '.join(some)} on calls.")
    return " ".join(out)


_NEGATION = re.compile(r"\b(not|no|never|don't|doesn't|dont|doesnt|isn't|cannot|can't)\b", re.I)


def _relation_correction(rec: dict, said: str) -> None:
    """The owner naming a relationship sets it ("she's a customer") or, negated just before the
    word, clears it ("not a colleague"). In "a customer, not a colleague" each word is judged by
    its own few words before it, so the customer is set and the colleague cleared."""
    low = said.lower()
    for word, kind in _RELATION_KIND.items():
        for m in re.finditer(rf"\b{re.escape(word)}s?\b", low):
            before = " ".join(low[: m.start()].split()[-3:])
            if _NEGATION.search(before):
                if (rec.get("relation") or {}).get("label") == kind:
                    rec.pop("relation", None)
            else:
                rec["relation"] = {"label": kind, "confidence": 1.0, "by": "owner"}


def _language_correction(rec: dict, said: str) -> None:
    """An owner's correction naming a language turns it off (with a negation) or on."""
    for name, _ in _SCRIPTS:
        if name.lower() in said.lower() or (name == "Chinese" and re.search(r"mandarin|cantonese", said, re.I)):
            on, off = set(rec.get("languages_on") or []), set(rec.get("languages_off") or [])
            if _NEGATION.search(said):
                off.add(name); on.discard(name)
            else:
                on.add(name); off.discard(name)
            rec["languages_on"], rec["languages_off"] = sorted(on), sorted(off)


def _week(at: str) -> str:
    """The seven days after a call, by name: "Thursday is 1 October, Friday is 2 October, …".

    THE WEEKDAY IS WORKED OUT HERE TOO (2026-10-01): told "today" and "tomorrow", Gemma still
    put a Friday said on Monday 28 September on "Friday 1 October" — a weekday miscount, at
    temperature 0, every run.
    """
    from datetime import timedelta
    try:
        start = datetime.fromisoformat(at)
    except ValueError:
        return ""
    days = [start + timedelta(days=n) for n in range(1, 8)]
    return ", ".join(f"{d:%A} is {d.day} {d:%B}" for d in days)


def _day(at: str, plus: int = 0) -> str:
    """"Friday 25 September 2026" for an ISO timestamp, `plus` days on."""
    from datetime import timedelta
    try:
        return (datetime.fromisoformat(at) + timedelta(days=plus)).strftime("%A %d %B %Y")
    except ValueError:
        return at


def update(who: str) -> bool:
    """Fold what is new about `who` into their brief. True when the model was asked."""
    from . import llm, owner, tools
    rec = load(who)
    calls, more = _calls_after(who, rec.get("through_call", ""))
    chat = _chat_after(who, rec.get("through_chat", ""))
    if not calls and not chat:
        return False                                  # the watermark: nothing new
    if not chat and not any(text for _, text in calls):
        # ONLY CALLS WITH NOTHING SAID (missed, or no audio): nothing to fold, so move the
        # watermark past them without asking the model.
        rec["through_call"] = calls[-1][0]
        _save(who, rec)
        if more:
            request(who)
        return False
    if not llm.configured():
        return False
    # RELATIVE DATES ARE WORKED OUT HERE, NOT BY THE MODEL. Told only the call's timestamp, Gemma
    # put "lunch tomorrow" said on the 25th on the 25th in some runs and the 26th in others, at
    # temperature 0. So each call says what "today" and "tomorrow" meant in it.
    items = [(at, f"{_day(at)} — a call with them. In this call \"today\" means {_day(at)} and "
                  f"\"tomorrow\" means {_day(at, 1)}. The week after it: {_week(at)}.\n"
                  + tools.untrusted(text))
             for at, text in calls if text]
    items += [(at, f"{_day(at)} — {owner.name() or 'the owner'} said to the assistant (\"tomorrow\" "
                   f"means {_day(at, 1)}): {q}") for at, q in chat]
    items.sort()
    done = _passes(who, rec, [x for _, x in items],
                   sources=[(at, t) for at, t in calls if t] + [(at, q) for at, q in chat])
    if done is None:
        return True
    rec["about"], rec["open"] = done
    _tally_languages(rec, [(at, t) for at, t in calls if t])
    newest = items[-1][0]
    rec["last"] = {"at": newest,
                   "how": "a call" if any(at == newest for at, t in calls if t) else "a message"}
    rec.pop("summary", None)
    rec["updated"] = _now().isoformat(timespec="seconds")
    if calls:
        rec["through_call"] = calls[-1][0]
    if chat:
        rec["through_chat"] = chat[-1][0]
    _save(who, rec)
    logger.info("brief for %s updated from %d call(s) and %d turn(s)", who, len(calls), len(chat))
    if more:
        request(who)
    return True


def correct(who: str, correction: str) -> str:
    """Fold the owner's correction into `who`'s brief NOW, as the newest and strongest fact.

    Through the model, never a text edit: the owner says what is wrong in their own words and
    the brief is rewritten around it, with the rest kept (Stanley, 2026-09-30).
    """
    from . import llm, owner
    rec = load(who)
    if not rec.get("summary"):
        return "There is no brief about them yet."
    if not llm.configured():
        return "No model is set up to rewrite the brief."
    said = " ".join((correction or "").split())[:600]
    if not said:
        return "Nothing to correct."
    # AS THE BRIEF'S OWN JOB, not as the question it was asked in: `gate` saves and restores the
    # assistant's cached conversation around a PERSON job, and a rewrite at QUESTION priority
    # would overwrite it mid-turn.
    with gate.priority(gate.PERSON):
        done = _passes(who, rec, [], correction=said)
    if done is None:
        return "The model returned nothing, so the brief is unchanged."
    rec["about"], rec["open"] = done
    _language_correction(rec, said)
    _relation_correction(rec, said)
    rec.pop("summary", None)
    rec["updated"] = _now().isoformat(timespec="seconds")
    _save(who, rec)
    logger.info("brief for %s corrected by the owner", who)
    return "Corrected. The brief now reads:\n" + load(who)["summary"]


def request(who: str) -> None:
    if who:
        jobs.request("person:" + who, gate.PERSON, lambda: update(who))


def sweep() -> int:
    """Request an update for everyone with a call newer than their brief. Cheap when idle."""
    from . import calls as _calls
    # EVERYONE, by their newest call — not the newest 200 calls, which left anyone quieter than
    # that with a summary that never caught up.
    newest = {who: at for who, at in _calls.people().items() if who and who != "?"}
    n = 0
    for who, at in newest.items():
        if at > load(who).get("through_call", ""):
            request(who)
            n += 1
    return n


def for_prompt(who: str) -> str:
    """What the assistant is handed about `who`: the brief, and calls it has not absorbed yet."""
    from . import tools
    rec = load(who)
    parts = []
    if rec.get("summary"):
        # THE NAME ALONGSIDE, NOT INSIDE: the brief never carries it (see PROMPT), so the
        # assistant is told here whose it is.
        from . import names
        label = names.display(who)
        parts.append(f"WHAT YOU KNOW ABOUT {label} (a running brief, as of {rec.get('updated', '')}):\n"
                     + tools.untrusted(rec["summary"]))
    calls, _ = _calls_after(who, rec.get("through_call", ""))
    for at, text in calls[-2:]:
        if text:
            parts.append(f"--- {at} with {who} (newer than the brief) ---\n" + tools.untrusted(text))
    return "\n\n".join(parts)
