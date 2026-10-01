"""Person summaries against the REAL local model, from scripted calls — no call needed.

    PYTHONPATH=src .venv-build/bin/python tests/test_briefs.py [--runs N] [--compare] [--show]

Each scenario hands `brief.update` written-out conversations in a transcript's `them:`/`you:`
shape, one call at a time as they would arrive, and checks the summary the model writes
against the rules in `brief.PROMPT`. A model decides, so read the TALLY over several runs, not
one red line (as in test_behaviour.py).

THE OWNER'S DATA IS NEVER WRITTEN: the model and its files come from the real instance, but
every brief is saved to a throwaway folder, and calls, chat and names are scripted.

`--compare` also runs the PREVIOUS About rule (2026-10-01 morning: a list of slots) on the same
calls, which is how the current one was chosen: a list of slots made the model fill them — it
called a caller "a colleague" that nothing said.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest.mock as mock
from collections import defaultdict

from agentduet_desktop import brief, llm, names, owner

CURRENT_ABOUT = brief.PROMPT[brief.PROMPT.index("  About: "):brief.PROMPT.index("  Open: ")]
PREVIOUS_ABOUT = '''  About: what the conversations show about them — who they are to {owner} (colleague,
        customer, supplier, friend), where they work and their role if it was said, what
        they usually call about, and how to deal with them (the language they use or mix,
        times they prefer, anything they asked for). Only what was said; never their name.
'''
PRIORITY = [l for l in brief.PROMPT.splitlines(keepends=True) if "still matter in a month" in l
            or "small talk first" in l]


def previous_prompt() -> str:
    p = brief.PROMPT.replace(CURRENT_ABOUT, PREVIOUS_ABOUT)
    for line in PRIORITY:
        p = p.replace(line, "")
    return p


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
    ]),
]


def run_once(prompt: str, show: bool) -> dict[str, bool]:
    results = {}
    for key, name, calls, checks in SCENARIOS:
        who = "+659111" + str(abs(hash(key)) % 10000).zfill(4)
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="briefs-"))
        with mock.patch.object(brief, "_dir", lambda: tmp), \
             mock.patch.object(brief, "PROMPT", prompt), \
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


def tally(label: str, prompt: str, runs: int, show: bool) -> dict[str, int]:
    print(f"\n== {label} — {runs} run(s)")
    counts: dict[str, int] = defaultdict(int)
    for _ in range(runs):
        for check, passed in run_once(prompt, show).items():
            counts[check] += passed
    return counts


if __name__ == "__main__":
    if not llm.configured():
        sys.exit("No local model is set up on this machine.")
    args = sys.argv[1:]
    runs = int(args[args.index("--runs") + 1]) if "--runs" in args else 1
    show = "--show" in args
    now = tally("the current rule", brief.PROMPT, runs, show)
    before = tally("the previous rule", previous_prompt(), runs, show) if "--compare" in args else None
    print(f"\n{'check':<44} {'current':>8}" + (f" {'previous':>9}" if before else ""))
    for check in now:
        print(f"{check:<44} {now[check]:>5}/{runs}" + (f" {before[check]:>6}/{runs}" if before else ""))
    failed = sum(runs - v for v in now.values())
    print(f"\n  current rule: {failed} check(s) failed across {runs} run(s)")
    sys.exit(1 if failed else 0)
