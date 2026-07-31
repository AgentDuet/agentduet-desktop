"""Daily digest — 'what people asked you today'.

The third leg of the POC. Run it on a cron/timer; for now, run it by hand.
    python digest.py            # today
    python digest.py 2026-07-27 # a specific day
"""

import json
import pathlib
import sys
from collections import Counter
from datetime import date

from . import paths

LOG = paths.RUN / "queries.jsonl"


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    if not LOG.exists():
        print("No queries logged yet.")
        return

    rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r["at"].startswith(day)]
    if not rows:
        print(f"No queries on {day}.")
        return

    answered = [r for r in rows if r["outcome"] == "answered"]
    escalated = [r for r in rows if r["outcome"] == "escalated"]
    # `acted` arrived with capabilities and this report was never taught about it, so the one
    # outcome that changed something in the world was counted in the total and then never
    # shown. It goes FIRST: everything else is the agent talking, this is the agent doing.
    acted = [r for r in rows if r["outcome"] == "acted"]

    print(f"\n  SECRETARY DIGEST — {day}")
    print(f"  {len(rows)} queries from {len(set(r['asker'] for r in rows))} people"
          f" · {len(answered)} answered · {len(escalated)} escalated"
          f" · {len(acted)} acted on\n")

    if acted:
        print("  DONE ON YOUR BEHALF")
        for r in acted:
            print(f"    · {r['asker']} — {r['question']}")
            print(f"      → {r['answer']}")
        print()

    if escalated:
        print("  NEEDS YOU")
        for r in escalated:
            print(f"    · {r['asker']} — {r['question']}")
            print(f"      ({r['reason'].removeprefix('policy:')})")
        print()

    if answered:
        print("  HANDLED FOR YOU")
        for r in answered:
            print(f"    · {r['asker']} — {r['question']}")
            print(f"      → {r['answer']}")
        print()

    top = Counter(r["reason"].removeprefix("policy:") for r in escalated)
    if top:
        print("  Escalation reasons: " + ", ".join(f"{k} ×{v}" for k, v in top.most_common()))
        print("  → recurring reasons are candidates for a knowledge.md entry.\n")


if __name__ == "__main__":
    main()
