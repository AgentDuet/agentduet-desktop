"""The only things an ASKER may do to the escalation queue.

Deliberately not part of `tools.py`. That module is the owner's surface — grant folders,
send as the owner, resolve items — and `brain` must never be able to reach it
(`test_isolation.py` enforces that). But an asker withdrawing their own request is
legitimate: a real secretary accepts "never mind, forget the discount".

So the split is content vs disposition:

  ASKER  may withdraw or consolidate THEIR OWN asks — statements about what they want.
  OWNER  decides that something is handled. Never delegated.

Two conditions:

- **Verified identity only.** Withdrawal is destructive and invisible: an impostor
  claiming an address could silently kill a live negotiation. Reading someone's items is
  bad; cancelling them is worse.
- **Never deletes.** A withdrawal is recorded, so the owner sees "withdrawn by the
  sender" rather than an item vanishing, and the earlier turns stay in the thread.
"""
from __future__ import annotations


import json
import pathlib
from datetime import datetime

from . import paths

RUN = paths.RUN
LOG = RUN / "queries.jsonl"
RESOLVED = RUN / "resolved.json"     # owner's resolutions
ASKERS = RUN / "askers"              # one file per (identity, verified) — see _store()


def _safe(identity: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._@+-]", "_", identity.strip().lower())


def _store(asker: str, verified: bool) -> pathlib.Path:
    """One withdrawal file per identity.

    Structural rather than conditional isolation: this function can only ever name the
    caller's own file, so a withdrawal cannot reach another identity even if a filter
    elsewhere were wrong. Previously one shared file kept them apart by intersecting ids
    — correct, but one forgotten check away from cross-identity cancellation.

    Verified state is in the filename too, so an unverified claim gets its own empty
    file rather than the real person's.
    """
    ASKERS.mkdir(parents=True, exist_ok=True)
    return ASKERS / f"{'v' if verified else 'u'}-{_safe(asker)}.json"


def _mine(asker: str, verified: bool) -> dict:
    """This identity's own state: withdrawals, priorities and merge groups.

    POC shape — no guards on relative ordering yet, so a sender's "urgent" is taken at
    face value. Deliberate: see my-agenda.md.
    """
    p = _store(asker, verified)
    blank = {"withdrawn": {}, "priority": {}, "merge": {}, "pending_replies": []}
    if not p.is_file():
        return blank
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return blank
    if "withdrawn" not in d:                       # migrate the old flat map
        d = {"withdrawn": d, "priority": {}, "merge": {}}
    for k, v in blank.items():
        d.setdefault(k, [] if isinstance(v, list) else {})
    return d


def _save(asker: str, verified: bool, data: dict) -> None:
    _store(asker, verified).write_text(json.dumps(data, indent=2))


def _rows() -> list[dict]:
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]


def _resolved() -> dict:
    if not RESOLVED.exists():
        return {}
    try:
        return json.loads(RESOLVED.read_text())
    except json.JSONDecodeError:
        return {}


def _row_id(r: dict) -> str:
    if r.get("id"):
        return r["id"]
    import hashlib
    seed = f"{r.get('at')}|{r.get('asker')}|{r.get('question')}"
    return "h" + hashlib.sha256(seed.encode()).hexdigest()[:7]


def open_asks(asker: str, verified: bool, oldest_first: bool = False) -> list[dict]:
    """This person's own open escalations. Nobody else's, ever.

    Merged groups collapse to one entry, so what they see matches what a cleanup did —
    otherwise a summary saying "I merged those" is contradicted by the list.

    `oldest_first` matters for any caller reasoning about supersession: a cleanup once
    retired the LATEST discount ask and kept the stale one, because the list was
    newest-first while the prompt claimed oldest-first.
    """
    if not verified or not asker:
        return []
    state = _mine(asker, verified)
    done = {**_resolved(), **state["withdrawn"]}
    merged = state["merge"]
    seen, groups, out = set(), set(), []
    for r in reversed(_rows()):
        if r["outcome"] != "escalated" or not r.get("verified"):
            continue
        if r["asker"].strip().lower() != asker.strip().lower():
            continue
        rid = _row_id(r)
        if rid in done:
            continue
        from . import policy
        if policy.would_be_handled(r["question"]):
            continue                        # an instruction, not an outstanding ask
        if policy.expired(r.get("reason", ""), r["at"]):
            continue                        # aged out of the working list
        g = merged.get(rid)
        if g:                                  # one entry per merged group
            if g in groups:
                continue
            groups.add(g)
        k = r["question"].strip().lower()
        if k in seen:
            continue
        seen.add(k)
        out.append({"id": rid, "question": r["question"], "at": r["at"],
                    "reason": r.get("reason", ""), "merged": bool(g)})
    if oldest_first:
        out.reverse()
    return out


def withdraw(asker: str, verified: bool, ids: list[str], said: str = "") -> int:
    """Mark this person's own asks withdrawn. Returns how many were affected.

    Scoped by construction: ids not belonging to `asker` are ignored rather than trusted,
    so a wrong id cannot reach into someone else's queue.
    """
    if not verified or not ids:
        return 0
    mine = {a["id"] for a in open_asks(asker, verified)}
    targets = [i for i in ids if i in mine]
    if not targets:
        return 0
    data = _mine(asker, verified)
    now = datetime.now().isoformat(timespec="seconds")
    for i in targets:
        data["withdrawn"][i] = {
            "how": f"withdrawn by sender: {said[:120]}" if said else "withdrawn by sender",
            "at": now, "by": asker}
    _save(asker, verified, data)
    return len(targets)


def set_priority(asker: str, verified: bool, ids: list[str], level: str = "urgent") -> int:
    """Mark the sender's own asks urgent (or back to normal). Their own set only."""
    if not verified or not ids:
        return 0
    mine = {a["id"] for a in open_asks(asker, verified)}
    data = _mine(asker, verified)
    n = 0
    for i in (x for x in ids if x in mine):
        if level == "normal":
            data["priority"].pop(i, None)
        else:
            data["priority"][i] = level
        n += 1
    _save(asker, verified, data)
    return n


def merge(asker: str, verified: bool, ids: list[str]) -> int:
    """Group the sender's own asks into one item, even across conversations.

    Non-destructive: nothing is dropped, the questions all remain — it only changes how
    they are grouped.
    """
    if not verified or len(ids) < 2:
        return 0
    mine = {a["id"] for a in open_asks(asker, verified)}
    targets = [i for i in ids if i in mine]
    if len(targets) < 2:
        return 0
    data = _mine(asker, verified)
    group = data["merge"].get(targets[0]) or f"g-{targets[0]}"
    for i in targets:
        data["merge"][i] = group
    _save(asker, verified, data)
    return len(targets)


def queue_reply(asker: str, text: str) -> None:
    """Hold an owner reply that could not be delivered yet.

    DDUET is passive: we can only write into a session the sender opened. Previously an
    undeliverable reply was recorded, the escalation was closed, and nothing was sent —
    so the queue read as handled while the person had heard nothing. Held here instead
    and flushed the next time they write.
    """
    data = _mine(asker, True)
    data.setdefault("pending_replies", []).append(
        {"text": text, "at": datetime.now().isoformat(timespec="seconds")})
    _save(asker, True, data)


def pending_replies(asker: str) -> list[dict]:
    return _mine(asker, True).get("pending_replies", [])


def take_pending_replies(asker: str) -> list[dict]:
    """Return and clear — called when we finally have a live channel to them."""
    data = _mine(asker, True)
    out = data.get("pending_replies", [])
    if out:
        data["pending_replies"] = []
        _save(asker, True, data)
    return out


def pending_delivery_count() -> int:
    """Across everyone, for the owner's view — an undelivered reply must be visible."""
    n = 0
    if ASKERS.is_dir():
        for f in ASKERS.glob("v-*.json"):
            try:
                n += len(json.loads(f.read_text()).get("pending_replies", []))
            except json.JSONDecodeError:
                continue
    return n


def reorg_state() -> tuple[dict, dict]:
    """(priority, merge) across all identities, for the OWNER's aggregated queue."""
    prio, mrg = {}, {}
    if ASKERS.is_dir():
        for f in ASKERS.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            prio.update(d.get("priority", {}))
            mrg.update(d.get("merge", {}))
    return prio, mrg


def withdrawn_ids() -> dict:
    """Every self-withdrawal, for the OWNER's aggregated view. Read-only union across
    the per-identity files — the owner sees everything, each asker only their own."""
    out: dict = {}
    if ASKERS.is_dir():
        for f in ASKERS.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            out.update(d.get("withdrawn", d if "priority" not in d else {}))
    return out
