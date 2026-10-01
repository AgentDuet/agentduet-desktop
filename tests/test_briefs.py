"""Person summaries against the REAL local model, from scripted calls — no call needed.

    PYTHONPATH=src .venv-build/bin/python tests/test_briefs.py [--fifth]

Each scenario hands `brief.update` written-out conversations in a transcript's `them:`/`you:`
shape and prints the summary the model writes, then checks it against the rules in
`brief.PROMPT`. A model decides, so a single red line is a reason to run it again, not a
verdict (as in test_behaviour.py).

THE OWNER'S DATA IS NEVER WRITTEN: the model and its files come from the real instance, but
every brief is saved to a throwaway folder, and calls, chat and names are scripted.

`--fifth` adds a candidate fifth part to About — personal details that matter when meeting or
helping someone — so the two versions of the rules can be compared on the same calls.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest.mock as mock

from agentduet_desktop import brief, llm, names, owner

WHO, NAME = "+6591112222", "Mei Ling"
FAILS = 0

CALLS = [
    ("2026-09-28T10:00:00",
     "them: Hi Stanley, it's me. About Friday — I can drive us to the client in Jurong, I'll pick "
     "you up at nine.\n"
     "you: Great, thanks.\n"
     "them: And for lunch after, not the Thai place please. I'm allergic to peanuts.\n"
     "you: Noted, we'll find somewhere else."),
    ("2026-09-30T15:00:00",
     "them: 我们星期五还是九点吗？\n"
     "you: Yes, Friday at nine, you're driving.\n"
     "them: OK, 没问题. See you Friday."),
]

FIFTH = ("        Also any personal details they mentioned that would matter when meeting or "
         "helping them\n        — diet or allergies, whether they drive, family.\n")


def ok(name: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n        {detail}" if not cond and detail else ""))
    FAILS += 0 if cond else 1


def run(fifth: bool) -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="briefs-"))
    prompt = brief.PROMPT
    if fifth:
        prompt = prompt.replace("Only what was said; never their name.\n",
                                "Only what was said; never their name.\n" + FIFTH)
        assert prompt != brief.PROMPT, "the About rule moved; update FIFTH's anchor"
    print(f"\n== rules: {'with the fifth part' if fifth else 'as shipped'}")
    with mock.patch.object(brief, "_dir", lambda: tmp), \
         mock.patch.object(brief, "PROMPT", prompt), \
         mock.patch.object(brief, "_chat_after", lambda who, after: []), \
         mock.patch.object(names, "name_for", lambda w, seen=None: NAME if w == WHO else ""), \
         mock.patch.object(owner, "name", lambda: "Stanley"):
        # One call at a time, as they would arrive — the second update starts from the first.
        for i in range(len(CALLS)):
            done = CALLS[: i + 1]
            with mock.patch.object(brief, "_calls_after",
                                   lambda who, after, d=done: ([c for c in d if c[0] > after], False)):
                brief.update(WHO)
        text = brief.load(WHO).get("summary", "")
        print("\n" + "\n".join("   | " + line for line in text.splitlines()) + "\n")
        low = text.lower()
        ok("it is written in three parts", all(p in text for p in ("About:", "Open:", "Last contact:")))
        ok("no name, no number", NAME.split()[0].lower() not in low and "1112222" not in text, text)
        ok("About says they drive", bool(re.search(r"\bdriv", low)))
        ok("About keeps the allergy", "peanut" in low or "allerg" in low)
        ok("it notices they mix Mandarin", "mandarin" in low or "chinese" in low)
        opened = low.split("open:")[-1].split("last contact:")[0]
        ok("Open keeps Friday's pick-up at nine, dated",
           bool(re.search(r"\b0?2(nd)? october", opened)) and ("nine" in opened or "9" in opened), opened)
        ok("Last contact is the newest call, 30 September", "30 september" in low.split("last contact:")[-1])
        # A CORRECTION: applied, and not a contact.
        before = low.split("last contact:")[-1]
        brief.correct(WHO, "She is a customer from Acme, not a colleague.")
        after = brief.load(WHO).get("summary", "")
        print("   after a correction:\n" + "\n".join("   | " + l for l in after.splitlines()) + "\n")
        al = after.lower()
        ok("the correction is applied", "customer" in al and "acme" in al, after)
        ok("and is not a contact: Last contact unchanged",
           "correction" not in al and "30 september" in al.split("last contact:")[-1], after)
        ok("everything else is kept", "peanut" in al or "allerg" in al)


if __name__ == "__main__":
    if not llm.configured():
        sys.exit("No local model is set up on this machine.")
    run("--fifth" in sys.argv)
    print(f"\n  {FAILS} failed")
    sys.exit(1 if FAILS else 0)
