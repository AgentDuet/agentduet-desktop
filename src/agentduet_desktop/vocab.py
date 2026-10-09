"""Words the owner taught the speech model by correcting a transcript (2026-10-09).

WHY. A Mandarin-English speaker said "dim sum" and the transcript read "dinner": a sound-alike in
mixed speech, which nothing after transcription can know. Qwen3-ASR takes free-text context, so a
word given as a hint is one it leans toward when the sound fits. Measured on that call and nine
other call sides, the same day:

  - THIRTY regional words made things WORSE: Mandarin came back translated into English, one
    piece looped ("Huh? Huh? ..." 27 times), English drifted ("not a tester" -> "do not
    disturb"), and transcription took 2.2x as long.
  - ONE word ("dim sum") fixed both sides of that call, left a real "dinner" alone and every
    Mandarin piece untouched, at no cost in time — though any change to the hint nudges a few
    unrelated words ("one P" -> "one piece").

So there is no built-in list: only words the owner chose, and few of them (`MAX`).

HOW A WORD GETS HERE. The owner corrects a transcript, by hand — no model touches the text, and
none chooses the words. `suggest` diffs the old text against the new and offers what was put in;
the owner ticks what to keep. A tidied sentence must not quietly fill the list, so nothing joins
without that tick.

An AI module: the recorder shows no transcripts and runs no speech engine.
"""
from __future__ import annotations

import difflib
import json
import re
import time

from . import paths

#: How many words the hint carries. Every word added changes how other pieces come out a little,
#: which is why this is small; the oldest goes first.
MAX = 10
#: The longest span offered, in words. Longer is a rewrite, not a mishearing.
MAX_WORDS = 3
#: Not vocabulary: filler, and the transcript's own labels.
_SKIP = {"ah", "uh", "eh", "um", "hmm", "oh", "er", "erm", "mm", "them", "you"}
#: A word, or one character of a script written without spaces (Chinese, Japanese, Thai).
_TOKEN = re.compile(r"[぀-ヿ㐀-鿿฀-๿]|[^\W_]+(?:['’][^\W_]+)?")
_UNSPACED = re.compile(r"[぀-ヿ㐀-鿿฀-๿]")


def _file():
    return paths.HOME / "vocabulary.json"


def words() -> list[str]:
    """The owner's words, newest first."""
    try:
        got = json.loads(_file().read_text(encoding="utf-8"))
        return [w["word"] for w in got if isinstance(w, dict) and w.get("word")][:MAX]
    except (OSError, ValueError):
        return []


def _save(ws: list[str]) -> None:
    _file().parent.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".part")
    tmp.write_text(json.dumps([{"word": w, "at": time.strftime("%Y-%m-%dT%H:%M:%S")} for w in ws],
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_file())


def add(new: list[str]) -> list[str]:
    """Keep these words, newest first; a word already kept moves to the front. Returns the list."""
    clean = [" ".join(str(w).split()) for w in new if str(w).strip()]
    keep = clean + [w for w in words() if w.lower() not in {c.lower() for c in clean}]
    _save(keep[:MAX])
    return words()


def remove(word: str) -> list[str]:
    _save([w for w in words() if w.lower() != str(word).strip().lower()])
    return words()


def hint() -> str:
    """The sentence the speech model is given, or "" with no words."""
    ws = words()
    return f"Words that may come up: {', '.join(ws)}." if ws else ""


def _join(tokens: list[str]) -> str:
    return "".join(tokens) if all(_UNSPACED.fullmatch(t) for t in tokens) else " ".join(tokens)


def suggest(old: str, new: str) -> list[str]:
    """What the owner PUT IN when correcting `old` to `new`: candidates for the list, to tick.

    Word by word (character by character in Chinese and Thai), only what was inserted or
    replaced, and only short spans. Not a change of case or punctuation, not numbers or times,
    not filler, not a word already kept.
    """
    a, b = _TOKEN.findall(old or ""), _TOKEN.findall(new or "")
    sm = difflib.SequenceMatcher(None, [t.lower() for t in a], [t.lower() for t in b], autojunk=False)
    kept = {w.lower() for w in words()}
    out: list[str] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op not in ("replace", "insert"):
            continue
        span = b[j1:j2]
        limit = MAX_WORDS * 2 if all(_UNSPACED.fullmatch(t) for t in span) else MAX_WORDS
        if not span or len(span) > limit:
            continue
        span = [t for t in span if t.lower() not in _SKIP and not t.isdigit()]
        if not span:
            continue
        phrase = _join(span)
        if phrase.lower() in kept or phrase.lower() in {o.lower() for o in out}:
            continue
        if phrase.lower() == _join(a[i1:i2]).lower():
            continue
        out.append(phrase)
    return out[:5]
