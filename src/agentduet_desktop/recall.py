"""The assistant's running summary of its chat with the owner, kept up to date in the background.

WHY. Recent chat is carried word for word, but only so much of it (`OwnerChat.HISTORY_WORDS`),
and an hour of quiet starts a new conversation. What the owner told the assistant — how they
like things done, what they decided, what they asked it to follow up — used to fall out with the
oldest lines. This keeps it: ~200 words, carried in every prompt, across conversations.

ONLY FROM THE ASSISTANT CHAT: the owner's questions and the assistant's answers. Never the call
text a lookup returned — that belongs to the person's brief (`brief.py`), which is where a
caller's words are summarised.

FOLDED AFTER EVERY TURN, while the owner reads the answer (`jobs`, FOLD priority, behind
questions and briefs, never during a call). Incremental: the current summary plus only the turns
since its watermark. Newer wins. Then the next prompt's start is read again (a prewarm), because
the summary sits near the top of every prompt and changing it would otherwise make the next
question read cold.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import gate, jobs, paths

logger = logging.getLogger("secretary.recall")

#: Turns folded in one pass. More wait for the next, requested straight away.
TURNS_PER_FOLD = 10

PROMPT = """Today is {today}.

You keep a short memory for {owner}'s assistant, so it remembers what matters from
earlier conversations with {owner}.

Update the memory with the new exchanges below.
- Keep, in this order:
  1. Instructions {owner} gave about how the assistant should answer (length, tone, language).
  2. Facts {owner} told the assistant about their work, and decisions {owner} made.
  3. Anything {owner} asked the assistant to do or remind them of later.
- Leave out small talk, and details of individual callers' calls (those are kept elsewhere).
- Where the new exchanges and the memory disagree, the NEWER one wins.
- KEEP every line already in the memory unless the new exchanges change or finish it.
- Drop a line only when the new exchanges say it is finished or no longer true.
- Use only the memory and the exchanges. Do not guess.
- At most {words} words, as short plain lines.

Reply with the memory only, and never repeat these rules. If there is nothing worth keeping,
reply with the current memory unchanged — or, if it is empty, with exactly: (empty)

CURRENT MEMORY (as of {asof}):
{current}

NEW EXCHANGES, oldest first:
{new}
"""


def _file():
    return paths.RUN / "assistant_memory.json"


def load() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(rec: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def _turns_after(after: str) -> list[dict]:
    from .assistant import OwnerChat
    try:
        shown = json.loads(OwnerChat.STORE.read_text())
    except (OSError, ValueError):
        return []
    return [t for t in shown if "q" in t and t.get("a") and not t.get("pending")
            and (t.get("at") or "") > after]


def fold() -> bool:
    """Fold the turns since the watermark into the memory. True when the model was asked."""
    from . import llm, owner
    rec = load()
    turns = _turns_after(rec.get("through", ""))
    if not turns:
        return False                                   # the watermark: nothing new
    if not llm.configured():
        return False
    batch, more = turns[:TURNS_PER_FOLD], len(turns) > TURNS_PER_FOLD
    who = owner.name() or "the owner"
    new = "\n\n".join(f"{t.get('at', '')}\n{who}: {t['q']}\nAssistant: {t['a']}" for t in batch)
    from . import budget
    words = budget.split()["memory_words"]
    prompt = PROMPT.format(today=datetime.now().strftime("%A %d %B %Y"), owner=who, words=words,
                           asof=rec.get("updated", "never"),
                           current=rec.get("summary") or "(empty)", new=new)
    summary = llm.client().complete(prompt).strip()
    rec["through"] = batch[-1].get("at", "")
    if summary and summary != "(empty)":
        rec.update(summary=summary, updated=datetime.now().isoformat(timespec="seconds"))
    _save(rec)
    logger.info("assistant memory folded %d turn(s)", len(batch))
    if more:
        request()
    return True


def for_prompt() -> str:
    summary = load().get("summary", "")
    return f"WHAT YOU REMEMBER FROM EARLIER CONVERSATIONS WITH THEM:\n{summary}" if summary else ""


def request(then=None) -> None:
    """Fold in the background; `then` runs after it (the assistant's prewarm)."""
    def job():
        try:
            fold()
        finally:
            if then:
                then()
    jobs.request("fold:assistant", gate.FOLD, job)
