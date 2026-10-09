"""What happened on a call, as the recorder's own record.

WHY NOT `brain.record`. That is a QUERY log — asker, question, outcome, reason, answer — built
for an agent that was asked something and decided what to say. A carried call has no question and
no answer: two people talked and we kept the audio. Writing it there would mean inventing a
question to satisfy a schema, and then every reader of that log has to know which rows are real
queries. The recorder gets its own noun instead.

TWO STORES, ONE RECORD (2026-10-03). `calls.jsonl` is still written, one line per call, appended
and never rewritten: restart-safe, readable with `cat` at 3am, and what an older build reads if
the owner goes back to one. `calls.db` (SQLite) is the INDEX over it, and every read goes there.

WHY AN INDEX. Every reader used to load the whole file — the hub on each five-second poll, the
summaries, the assistant's tools — so the cost grew with every call ever made (measured: 27 ms at
10,000 calls, 0.7 s at 365,000), and to bound it most readers saw only the newest 200 calls,
about ten days for a busy owner, after which people silently dropped off the list
(docs/limits.md). The index answers "everyone, newest first" and "this person's calls" without
reading the rest. It is derived: lines the database has not seen — an older build appended them,
or it was deleted — are imported the next time it is opened, so the file stays the record.

The caller is the point. Recording filenames carry a call id, so without this there is no way
back from a `.wav` to a person — which is exactly what a per-person view needs.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime

from . import paths

logger = logging.getLogger("dduet.calls")

#: One JSON object per line, appended. Never rewritten.
LOG = paths.RUN / "calls.jsonl"
#: The index over it. Rebuildable from LOG at any time: delete it and it comes back.
DB = paths.RUN / "calls.db"

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,          -- when it was filed (its end)
    call_id TEXT,
    caller TEXT,               -- as written
    person TEXT NOT NULL,      -- person_of(row): the number alone
    mode TEXT,
    outgoing INTEGER,
    recordings TEXT,           -- JSON list
    note TEXT,
    started TEXT,
    row TEXT NOT NULL          -- the line as written, so a field added later is never lost
);
CREATE INDEX IF NOT EXISTS calls_person_at ON calls (person, at);
CREATE INDEX IF NOT EXISTS calls_at ON calls (at);
CREATE INDEX IF NOT EXISTS calls_call_id ON calls (call_id);
CREATE INDEX IF NOT EXISTS calls_person_in_at ON calls (person, outgoing, at);
-- ONE ROW PER PERSON, kept as calls are added, so the hub's list reads people, not calls.
CREATE TABLE IF NOT EXISTS persons (
    person TEXT PRIMARY KEY,
    calls INTEGER NOT NULL,
    last TEXT NOT NULL,        -- newest call's `at`
    last_in TEXT NOT NULL      -- newest INCOMING call's `at`, "" if none
);
CREATE INDEX IF NOT EXISTS persons_last ON persons (last);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")      # readers never wait on the writer
    con.executescript(_SCHEMA)
    return con


def _insert(con: sqlite3.Connection, row: dict) -> None:
    at, incoming = row.get("at", ""), not row.get("outgoing")
    con.execute(
        "INSERT INTO persons VALUES (?, 1, ?, ?) ON CONFLICT(person) DO UPDATE SET "
        "calls = calls + 1, last = MAX(last, excluded.last), "
        "last_in = MAX(last_in, excluded.last_in)",
        (person_of(row), at, at if incoming else ""))
    con.execute(
        "INSERT INTO calls (at, call_id, caller, person, mode, outgoing, recordings, note, started,"
        " row) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (row.get("at", ""), row.get("call_id", ""), row.get("caller", ""), person_of(row),
         row.get("mode", ""), int(bool(row.get("outgoing"))), json.dumps(row.get("recordings") or []),
         row.get("note", ""), row.get("started", ""), json.dumps(row)))


def _catch_up(con: sqlite3.Connection) -> None:
    """Import the lines of LOG the index has not seen. A stat when there are none.

    By BYTE OFFSET, recorded in `meta`: the file is only ever appended, so what lies past the
    offset is exactly what is new. A file SHORTER than the offset was replaced, not appended —
    the index is then rebuilt from it whole.
    """
    # THE PER-PERSON TALLY, built once from the calls for an index made before it existed.
    if not con.execute("SELECT 1 FROM meta WHERE key='persons'").fetchone():
        con.execute("DELETE FROM persons")
        con.execute("INSERT INTO persons SELECT person, COUNT(*), MAX(at), "
                    "COALESCE(MAX(CASE WHEN outgoing = 0 THEN at END), '') FROM calls GROUP BY person")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('persons', '1')")
        con.commit()
    try:
        size = LOG.stat().st_size
    except OSError:
        return
    got = con.execute("SELECT value FROM meta WHERE key='log_bytes'").fetchone()
    seen = int(got[0]) if got else 0
    if size == seen:
        return
    if size < seen:
        con.execute("DELETE FROM calls")
        con.execute("DELETE FROM persons")
        seen = 0
    with LOG.open("rb") as f:
        f.seek(seen)
        chunk = f.read()
    # A LINE STILL BEING WRITTEN is left for next time: only whole lines are imported.
    whole = chunk[: chunk.rfind(b"\n") + 1]
    n = 0
    for line in whole.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            _insert(con, json.loads(line))
            n += 1
        except ValueError:
            continue                          # one bad line must not lose the rest
    con.execute("INSERT OR REPLACE INTO meta VALUES ('log_bytes', ?)", (str(seen + len(whole)),))
    con.commit()
    if n:
        logger.info("calls: indexed %d call(s) from %s", n, LOG.name)


def _query(sql: str, args: tuple = ()) -> list[dict]:
    """Rows as the dicts the file holds. [] when the index cannot be opened — never raises."""
    try:
        with _lock:
            con = _connect()
            try:
                _catch_up(con)
                return [json.loads(r["row"]) for r in con.execute(sql, args)]
            finally:
                con.close()
    except sqlite3.Error as exc:
        logger.warning("calls: the index could not be read (%s)", exc)
        return []


def record(call_id: str, caller: str, mode: str, *, recordings: list[str] | None = None,
           note: str = "", outgoing: bool = False, started: float | None = None,
           at: float | None = None) -> None:
    """Append one call. Never raises: losing the audio matters, losing the index does not."""
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        # UNDER THE LOCK, so `forget` rewriting the file cannot drop a line appended meanwhile.
        with _lock, LOG.open("a") as f:
            f.write(json.dumps({
                # `at` GIVEN only for a call filed after the fact (AgentDuet AI reading a
                # recording later): its end, not the moment it was read.
                "at": (datetime.fromtimestamp(at) if at else datetime.now()).isoformat(timespec="seconds"),
                "call_id": call_id,
                # E.164 where the platform gives it. "?" when it does not — better an honest
                # unknown than a row silently attributed to the wrong person.
                "caller": caller or "?",
                "mode": mode,                      # "carried" | "answered"
                # WHICH WAY THE CALL WENT, as a field. It briefly lived inside `caller` as a
                # "to "/"from " prefix, which split one person into several — see carry.handle.
                "outgoing": outgoing,
                "recordings": recordings or [],
                "note": note,
                # WHEN IT BEGAN. `at` is when the call was FILED — its end — and stays that, since
                # the person briefs use it as their watermark. The history orders and labels calls
                # by this instead: a missed call that the platform closed late was filed after
                # the call that followed it, and showed below it (2026-09-29).
                "started": (datetime.fromtimestamp(started).isoformat(timespec="seconds")
                            if started else ""),
            }) + "\n")
    except OSError as exc:
        logger.warning("could not record call %s: %s", call_id, exc)
    # THE INDEX CATCHES UP from the file, here or at the next read — one path, so the two
    # cannot disagree about what was written.
    _query("SELECT row FROM calls WHERE 0")


def forget(call_id: str) -> int:
    """Remove one call from the record — the owner deleted it. Returns how many lines went.

    THE ONE PLACE `calls.jsonl` IS REWRITTEN, which the docstring above says never happens: an
    owner deleting a call (or answering an erasure request) is the reason it must. Written whole
    and renamed, so a reader sees the old file or the new one. The file is then shorter than the
    index's offset, and `_catch_up` rebuilds the index from it.
    """
    if not call_id:
        return 0
    with _lock:
        try:
            lines = LOG.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return 0
        kept, gone = [], 0
        for line in lines:
            try:
                same = json.loads(line).get("call_id") == call_id
            except ValueError:
                same = False
            if same:
                gone += 1
            else:
                kept.append(line)
        if not gone:
            return 0
        tmp = LOG.with_name(LOG.name + ".part")
        tmp.write_text("".join(kept), encoding="utf-8")
        tmp.replace(LOG)
        # REBUILT, not trusted to notice: an index that had not caught up could hold an offset
        # still inside the shorter file, and would then keep the deleted row.
        try:
            con = _connect()
            try:
                con.execute("DELETE FROM calls")
                con.execute("DELETE FROM persons")
                con.execute("INSERT OR REPLACE INTO meta VALUES ('log_bytes', '0')")
                con.commit()
            finally:
                con.close()
        except sqlite3.Error as exc:
            logger.warning("calls: the index could not be cleared (%s)", exc)
    _query("SELECT row FROM calls WHERE 0")
    return gone


def recent(limit: int | None = 200) -> list[dict]:
    """Newest first; all of them with `limit=None`. Prefer the narrower reads below."""
    sql = "SELECT row FROM calls ORDER BY at DESC, id DESC"
    return _query(sql + (" LIMIT ?" if limit is not None else ""),
                  (limit,) if limit is not None else ())


def since(cut: str) -> list[dict]:
    """Calls filed at or after `cut` (ISO), newest first."""
    return _query("SELECT row FROM calls WHERE at >= ? ORDER BY at DESC, id DESC", (cut,))


def for_person(who: str, limit: int | None = None) -> list[dict]:
    """One person's calls, newest first."""
    sql = "SELECT row FROM calls WHERE person = ? ORDER BY at DESC, id DESC"
    return _query(sql + (" LIMIT ?" if limit is not None else ""),
                  (who, limit) if limit is not None else (who,))


def outgoing_since(who: str, since: str) -> list[dict]:
    """One person's calls the OWNER placed, filed at or after `since` (ISO), newest first."""
    return _query("SELECT row FROM calls WHERE person = ? AND outgoing = 1 AND at >= ? "
                  "ORDER BY at DESC, id DESC", (who, since or ""))


def get(call_id: str) -> dict | None:
    """The row for one call, or None."""
    rows = _query("SELECT row FROM calls WHERE call_id = ? ORDER BY id DESC LIMIT 1", (call_id,))
    return rows[0] if rows else None


def summary(seen: dict[str, str]) -> dict[str, dict]:
    """Per person, for the hub's list: how many calls, the newest, and how many are UNREAD.

    READ FROM `persons`, one row per person kept as calls are added, so the list costs the same
    however many calls there are (measured: counting the calls on each poll grew to 150 ms at
    100,000; every call as a row before that, 157 ms). `seen` maps a person to the newest item
    the owner has seen; an incoming call newer than that is unread, and is counted only for the
    people whose newest incoming call IS newer — a handful, not everyone. `last_in` is their newest
    incoming call, which the first look at the list marks as seen.
    """
    out: dict[str, dict] = {}
    try:
        with _lock:
            con = _connect()
            try:
                _catch_up(con)
                for r in con.execute("SELECT person, calls, last, last_in FROM persons "
                                     "ORDER BY last DESC"):
                    unread = 0
                    if r["last_in"] and r["last_in"] > seen.get(r["person"], ""):
                        unread = con.execute(
                            "SELECT COUNT(*) FROM calls WHERE person = ? AND outgoing = 0 "
                            "AND at > ?", (r["person"], seen.get(r["person"], ""))).fetchone()[0]
                    out[r["person"]] = {"calls": r["calls"], "last": r["last"],
                                        "last_in": r["last_in"], "unread": unread}
            finally:
                con.close()
    except sqlite3.Error as exc:
        logger.warning("calls: the index could not be read (%s)", exc)
    return out


def people() -> dict[str, str]:
    """Everyone who has a call, with their newest call's `at` — newest first. Never capped."""
    try:
        with _lock:
            con = _connect()
            try:
                _catch_up(con)
                return {r["person"]: r["last"] for r in con.execute(
                    "SELECT person, last FROM persons ORDER BY last DESC")}
            finally:
                con.close()
    except sqlite3.Error as exc:
        logger.warning("calls: the index could not be read (%s)", exc)
        return {}


#: Identities written before the direction moved to its own field. Stripped on read so an
#: existing log merges instead of showing one person two or three times.
_LEGACY_PREFIXES = ("to ", "from ")


def person_of(row: dict) -> str:
    """The person a call belongs to: the number alone, whichever way the call went."""
    who = (row.get("caller") or "?").strip()
    for p in _LEGACY_PREFIXES:
        if who.startswith(p):
            who = who[len(p):].strip()
            break
    return who or "?"


def by_person(limit: int | None = 200) -> dict[str, list[dict]]:
    """Calls grouped by who was on them, newest first within each — the newest `limit` calls.

    GROUPED BY NUMBER, NOT BY THE STRING IN THE ROW. A direction word briefly lived inside
    `caller`, so the same number appeared as "+65…", "to +65…" and "from +65…" — three people
    with three histories, one of whom had rung the other two. `person_of` strips that, which
    also merges any rows already written that way rather than leaving them stranded.
    """
    grouped: dict[str, list[dict]] = {}
    for row in recent(limit):
        grouped.setdefault(person_of(row), []).append(row)
    return grouped
