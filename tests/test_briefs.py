"""Person summaries against the REAL local model, from scripted calls — no call needed.

    PYTHONPATH=src .venv-build/bin/python tests/test_briefs.py [--runs N] [--show]

Each scenario hands `brief.update` written-out conversations in a transcript's `them:`/`you:`
shape, one call at a time as they would arrive, and checks the summary the model writes
against the rules in `brief.PROMPT`. A model decides, so read the TALLY over several runs, not
one red line (as in test_behaviour.py).

THE OWNER'S DATA IS NEVER WRITTEN: the model and its files come from the real instance, but
every brief is saved to a throwaway folder, and calls, chat and names are scripted.

HISTORY. The one-pass version (a single prompt writing all three parts) plateaued at 4-5 of 16
checks failing whatever the wording: a list of slots made the model invent "a colleague"; a
"Nothing yet" line emptied About with an allergy in front of it. That is why the summary is two
passes now (About, Open) with Last contact written by code.

THE CLOCK IS SET to an hour after each scenario's last call, because code drops an appointment
once it has passed, and the scripted ones are in September.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest.mock as mock
from collections import defaultdict
from datetime import datetime, timedelta

from agentduet_desktop import brief, llm, names, owner

def about(text: str) -> str:
    return text.lower().split("about:")[-1].split("open:")[0]


def opened(text: str) -> str:
    return text.lower().split("open:")[-1].split("last contact:")[0]


RELATIONS = ("colleague", "customer", "client", "supplier", "friend", "partner", "vendor")

#: (key, name, calls, checks). A check is (label, test(summary) -> bool).
SCENARIOS = [
    ("lift", "Mei Ling", [
        ("2026-09-28T10:00:00",
         "them: Hi Stanley, it's me. About Friday — I can drive us to the client in Jurong, I'll "
         "pick you up at nine.\nyou: Great, thanks.\n"
         "them: And for lunch after, not the Thai place please. I'm allergic to peanuts.\n"
         "you: Noted, we'll find somewhere else."),
        ("2026-09-30T15:00:00",
         "them: 我们星期五还是九点吗？\nyou: Yes, Friday at nine, you're driving.\n"
         "them: OK, 没问题. See you Friday."),
    ], [
        ("About: they drive", lambda t: bool(re.search(r"\bdriv", about(t)))),
        ("About: the peanut allergy", lambda t: "peanut" in about(t) or "allerg" in about(t)),
        ("About: they mix in Mandarin", lambda t: "mandarin" in about(t) or "chinese" in about(t)),
        ("About: no invented relation", lambda t: not any(r in about(t) for r in RELATIONS)),
        ("About: not the trip itself", lambda t: "jurong" not in about(t)),
        ("Open: Friday 2 Oct at nine", lambda t: bool(re.search(r"\b0?2(nd)? oct", opened(t)))
                                                  and ("nine" in opened(t) or "9" in opened(t))),
    ]),
    ("quiet", "Raj", [
        ("2026-09-29T11:00:00",
         "them: Hello, calling about the parcel.\nyou: Yes?\n"
         "them: It will come tomorrow between two and four.\nyou: OK, thank you.\nthem: Bye."),
    ], [
        ("About: no invented relation", lambda t: not any(r in about(t) for r in RELATIONS)),
        ("About: nothing lasting invented", lambda t: len(about(t).split()) <= 12),
        ("Open: the delivery, 30 Sep", lambda t: "30 sep" in opened(t)),
    ]),
    ("unnamed", "Joanne", [
        ("2026-09-29T09:00:00",
         "them: Hi, it's Joanne from Tan and Co, the accounting firm. About the audit.\n"
         "you: Sure.\nthem: Not Thursdays after six for me — my daughter Sophie has her piano "
         "recital every Thursday evening.\nyou: Understood.\n"
         "them: And if we do lunch, I'm vegetarian.\nyou: Noted."),
    ], [
        ("About: works at Tan and Co", lambda t: "tan" in about(t)),
        ("About: no Thursdays after six", lambda t: "thursday" in about(t)),
        ("About: vegetarian", lambda t: "vegetarian" in about(t)),
        ("no name in the summary", lambda t: "joanne" not in t.lower()),
        ("About: no invented relation", lambda t: not any(r in about(t) for r in RELATIONS)),
        ("Open: not the call itself", lambda t: "call" not in opened(t)),
    ]),
]


def run_once(show: bool) -> dict[str, bool]:
    results = {}
    for key, name, calls, checks in SCENARIOS:
        who = "+659111" + str(abs(hash(key)) % 10000).zfill(4)
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="briefs-"))
        with mock.patch.object(brief, "_dir", lambda: tmp), \
             mock.patch.object(brief, "_now", lambda c=calls: datetime.fromisoformat(c[-1][0]) + timedelta(hours=1)), \
             mock.patch.object(brief, "_chat_after", lambda w, after: []), \
             mock.patch.object(names, "name_for", lambda w, seen=None, n=name, me=who: n if w == me else ""), \
             mock.patch.object(owner, "name", lambda: "Stanley"):
            for i in range(len(calls)):
                done = calls[: i + 1]
                with mock.patch.object(brief, "_calls_after",
                                       lambda w, after, d=done: ([c for c in d if c[0] > after], False)):
                    brief.update(who)
            text = brief.load(who).get("summary", "")
        if show:
            print(f"\n   [{key}]\n" + "\n".join("   | " + l for l in text.splitlines()))
        results[f"{key}: three parts"] = all(p in text for p in ("About:", "Open:", "Last contact:"))
        for label, test in checks:
            results[f"{key}: {label}"] = bool(test(text))
    return results


def tally(runs: int, show: bool) -> dict[str, int]:
    print(f"\n== {runs} run(s)")
    counts: dict[str, int] = defaultdict(int)
    for _ in range(runs):
        for check, passed in run_once(show).items():
            counts[check] += passed
    return counts


if __name__ == "__main__":
    if not llm.configured():
        sys.exit("No local model is set up on this machine.")
    args = sys.argv[1:]
    runs = int(args[args.index("--runs") + 1]) if "--runs" in args else 1
    show = "--show" in args
    now = tally(runs, show)
    print(f"\n{'check':<44} {'passed':>8}")
    for check in now:
        print(f"{check:<44} {now[check]:>5}/{runs}")
    failed = sum(runs - v for v in now.values())
    print(f"\n  {failed} check(s) failed across {runs} run(s)")
    sys.exit(1 if failed else 0)
