"""Search what was said — on calls and in messages — by meaning and by keyword, on this machine
(2026-10-03).

TWO HALVES, MERGED:
  - MEANING. EmbeddingGemma 2 turns each piece of a transcript into 512 numbers; a question is
    turned the same way, and the closest pieces are its answers. It matches across languages and
    through speech-recognition slips: "who hasn't paid" finds a Vietnamese "hóa đơn … chưa thanh
    toán" and "the in voice is over due". EmbeddingGemma 300M won the first bake-off (2026-10-03:
    14/14, against 12/14 for Qwen3-Embedding 0.6B); version 2 replaced it on 2026-10-09 after a
    harder one — 33 passages with Thai, romanised Tamil, Singlish, Indonesian and a near-miss
    distractor, 7 of 21 queries held out: 21/21 first place, against 19/21. It is Apache-2.0, where
    the first was under the Gemma Terms.
  - KEYWORDS. SQLite's full-text index, with the TRIGRAM tokenizer: exact names, numbers and
    amounts, and Chinese, which has no spaces between words to split on.
A result found by both ranks above one found by either (reciprocal rank fusion).

THE INDEX (`run/search.db`) is derived from the transcripts: a call is (re)indexed whenever its
`.txt` changes, and the whole file can be deleted and rebuilt. It never holds anything the
transcripts do not, and it lives in this instance, never in the recorder's folder.

THE MODEL is one of the occasional ones (slot.py): loaded to index or to answer a question, then
unloaded when idle or when speech or the decision model needs the room. It runs in Google's
LiteRT, in this process, on the CPU (see `embed`): no `llama-cpp-python` release could load
version 2, and LiteRT's text-only build of it is half the size of the GGUF. During a call it waits —
so a search then falls back to keywords only, and indexing waits for the call to end.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading

from . import paths

logger = logging.getLogger("secretary.search")

REPO = "litert-community/embeddinggemma-2-text-270m-litert-lm"
#: PINNED: the file is checked by size, so a new upload must be a deliberate bump.
REVISION = "9be6e8b90982095dc05c2bd162e4b954ee4dbac7"
FILE, SIZE = "embeddinggemma-2-text-270m.litertlm", 164626432
#: The models this one replaced, deleted once the new one is here: version 1, and the GGUF of
#: version 2 that ran in llama-server for a day.
OLD_FILES = ("embeddinggemma-300m-qat-Q8_0.gguf", "embeddinggemma-2-Q8_0.gguf")
#: The first DIMS of the model's 768 numbers, renormalised (Matryoshka). 512, not 256: at 256 the
#: weakest right answer and the strongest non-answer were 0.003 apart, at 512 0.029 (2026-10-09).
DIMS = 512
#: What the model is told each text is, in its own prompt format.
QUERY, DOC = "task: search result | query: {}", "title: none | text: {}"

#: A piece is up to this many turns, the next one starting a turn before this one ends — so an
#: answer split across turns is whole in at least one piece.
TURNS, OVERLAP = 3, 1
#: A single turn longer than this is cut, so one monologue is not one piece.
MAX_CHARS = 600

#: A MEANING HIT BELOW THIS IS NO ANSWER. Without it the closest piece always "matches", and
#: "invoice" on calls that never mentioned one returned three "Okay. Bye" pieces. EmbeddingGemma 2
#: scores everything higher than version 1 did, so this moved with it. Measured 2026-10-09 at 512
#: numbers on the LiteRT file: on 37 pieces of real calls, right answers 0.737-0.791 and topics no
#: call was about 0.619-0.670 — except "job interview", 0.728, against two office meetups, which
#: this is set just above. The bake-off's synthetic passages ran lower (right answers from 0.709,
#: non-answers to 0.699), so its weakest right answers fall under it; keyword search still finds
#: exact words. Thin at the top; re-fit it on more real calls when they show it wrong.
#: Keyword hits are never cut by it.
MIN_SIMILARITY = 0.73

#: How often the indexer looks for calls whose transcripts changed. Woken sooner after a transcript.
POLL_SECONDS = 300

DB = paths.RUN / "search.db"
_db_lock = threading.Lock()
_engine = None
_engine_lock = threading.Lock()


def folder():
    return paths.HOME / "models" / "embed"


def ready() -> bool:
    """The model is here and so is the engine to run it in."""
    from . import litert
    f = folder() / FILE
    return f.is_file() and f.stat().st_size == SIZE and litert.available()


def _drop_old() -> None:
    for name in OLD_FILES:
        (folder() / name).unlink(missing_ok=True)


_fetching = threading.Lock()


def fetch() -> bool:
    from . import models
    if not _fetching.acquire(blocking=False):
        return False
    try:
        folder().mkdir(parents=True, exist_ok=True)
        models.fetch_file(f"https://huggingface.co/{REPO}/resolve/{REVISION}/{FILE}", folder() / FILE, SIZE)
        _drop_old()
        return True
    except Exception as exc:
        logger.warning("search model download stopped: %s", exc)
        return False
    finally:
        _fetching.release()


def start() -> bool:
    """Start the download unless it is running or done — setup's call. True when one is running."""
    if ready():
        _drop_old()
        return False
    if not _fetching.locked():
        logger.info("fetching the search model (%d MB) in the background", SIZE // 2**20)
        threading.Thread(target=fetch, name="search-fetch", daemon=True).start()
    return True


def fetch_in_background() -> None:
    start()


def progress() -> dict:
    """What a progress bar needs, read from disk: the whole file or the partial one."""
    f = folder() / FILE
    part = f.with_name(f.name + ".part")
    got = f.stat().st_size if f.is_file() else (part.stat().st_size if part.is_file() else 0)
    return {"ready": ready(), "mb": SIZE // 2**20, "got_mb": got // 2**20, "running": _fetching.locked()}


# ---- the model, in the slot --------------------------------------------------------------------

def _unload() -> None:
    global _engine
    with _engine_lock:
        if _engine is None:
            return
        try:
            _engine.close()
        except Exception:
            pass
        _engine = None
    from . import slot
    slot.released(slot.EMBED)
    logger.info("search model unloaded")


def _register_slot() -> None:
    from . import slot
    slot.register(slot.EMBED, _unload)


_register_slot()


def embed(texts: list[str]) -> "list[list[float]] | None":
    """Each text as DIMS numbers, unit length. None when the model may not run now (a call is on —
    slot.py) or is not here."""
    from . import slot
    global _engine
    if not texts or not ready() or not slot.claim(slot.EMBED):
        return None
    import numpy as np
    import litert_lm as lm
    with _engine_lock:
        if _engine is None:
            # THE CPU, on purpose: it loads in 0.1 s against 1.8 s on the GPU, never competes with
            # Gemma or speech for it, and indexes 33 pieces in ~1.2 s — nothing waits on indexing.
            from . import litert
            lm.set_min_log_severity(lm.LogSeverity.ERROR)
            _engine = lm.EmbeddingEngine(str(folder() / FILE), backend=lm.Backend.CPU(),
                                         cache_dir=str(litert.cache_dir()))
            logger.info("search model loaded")
        got = _engine.compute_embedding_batch(list(texts), lm.EmbeddingOptions(normalize=True))
    v = np.array([r.embedding for r in got], dtype=np.float32)[:, :DIMS]
    slot.touch(slot.EMBED)
    v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
    return v


# ---- the index ---------------------------------------------------------------------------------

#: BUMPED WHEN THE LAYOUT OR THE MODEL CHANGES: the index is derived, so it is simply rebuilt.
#: 3 = EmbeddingGemma 2 at 512 numbers (2026-10-09); its vectors mean nothing next to version 1's.
#: 4 = the same model as LiteRT's text-only file, whose vectors differ from the GGUF's.
SCHEMA = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pieces (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,         -- 'call' or 'message'
    source TEXT NOT NULL,       -- the call's id, or the person a message thread is with
    person TEXT NOT NULL,
    at TEXT NOT NULL,           -- the call's time, or the first message's in this piece
    idx INTEGER NOT NULL,       -- the piece's place in its source
    turn INTEGER NOT NULL,      -- the first turn (call) or message (thread) the piece covers
    t REAL,                     -- a call piece's start, seconds into the recording; NULL if unknown
    text TEXT NOT NULL,
    vec BLOB                    -- DIMS float16
);
CREATE INDEX IF NOT EXISTS pieces_source ON pieces (kind, source);
CREATE INDEX IF NOT EXISTS pieces_person ON pieces (person);
CREATE VIRTUAL TABLE IF NOT EXISTS pieces_fts USING fts5(text, content='pieces', content_rowid='id',
                                                        tokenize='trigram');
-- What each source was indexed from (`kind:source`), so a changed one is redone.
CREATE TABLE IF NOT EXISTS indexed (key TEXT PRIMARY KEY, stamp TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        got = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.OperationalError:
        got = None
    if not got or got[0] != str(SCHEMA):
        for t in ("pieces_fts", "pieces", "indexed", "meta"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.executescript(_SCHEMA)
        con.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
        con.commit()
    return con


def pieces(turns: list[tuple]) -> list[tuple[str, int, "float | None"]]:
    """Turns (time or None, who, text) as overlapping pieces of up to TURNS turns:
    (text, first turn's index, first turn's time). The text carries who said it, not when."""
    lines: list[tuple[int, "float | None", str]] = []      # (turn index, time, line)
    for i, (at, who, text) in enumerate(turns):
        line = f"{who}: {text}"
        while len(line) > MAX_CHARS:                       # one long turn becomes several
            cut = line.rfind(" ", 0, MAX_CHARS)
            cut = cut if cut > MAX_CHARS // 2 else MAX_CHARS
            lines.append((i, at, line[:cut]))
            line = line[cut:].strip()
        lines.append((i, at, line))
    if not lines:
        return []
    if len(lines) <= TURNS:
        windows = [lines]
    else:
        step = TURNS - OVERLAP
        windows = [lines[i:i + TURNS] for i in range(0, len(lines) - OVERLAP, step)]
    return [("\n".join(l for _, _, l in w), w[0][0], w[0][1]) for w in windows]


def _transcript_file(row: dict) -> tuple["object | None", str]:
    """(path, stamp) of a call's transcript file, or (None, "") — a STAT, not a read, so a pass
    over every call costs a stat each and reads only the files that changed."""
    from . import carry
    folder_, names = carry.call_audio(row.get("recordings", []), row.get("call_id", ""))
    for n in names:
        t = (folder_ / n).with_suffix(".txt")
        try:
            st = t.stat()
        except OSError:
            continue
        return t, f"{st.st_size}:{int(st.st_mtime)}"
    return None, ""


def _threads() -> dict[str, list[dict]]:
    """Messages by person, oldest first — WhatsApp and DDUET, and the owner's own replies — the
    same rows the hub shows (web_ai.threads_extras)."""
    from . import tools
    out: dict[str, list[dict]] = {}
    for r in tools.rows():
        who = r.get("asker") or ""
        owner_sent = r.get("outcome") == "owner_reply"
        if not who or (not owner_sent and r.get("network") not in ("WA", "DDUET")):
            continue
        out.setdefault(who, []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r.get("at", ""))
    return out


def _sources(limit: int) -> list[tuple]:
    """What needs (re)indexing: (kind, source, person, at, turns, stamp), at most `limit`."""
    from . import calls, carry
    with _db_lock:
        con = _connect()
        try:
            done = {r["key"]: r["stamp"] for r in con.execute("SELECT key, stamp FROM indexed")}
        finally:
            con.close()
    todo = []
    for row in calls.recent(None):
        cid = row.get("call_id", "")
        if not cid:
            continue
        path, stamp = _transcript_file(row)
        if path is None or done.get("call:" + cid) == stamp:
            continue
        turns = carry.parse_turns(carry.read_body(path))
        if turns:
            todo.append(("call", cid, calls.person_of(row), row.get("at", ""), turns, stamp, None))
        if len(todo) >= limit:
            return todo
    for who, rows in _threads().items():
        stamp = f"{len(rows)}:{rows[-1].get('at', '')}"
        if done.get("message:" + who) == stamp:
            continue
        # A MESSAGE IS ONE OR TWO TURNS: theirs, and ours where it was answered. Each turn keeps
        # its message's time, which is what the page scrolls to.
        turns, ats = [], []
        for r in rows:
            if r.get("outcome") != "owner_reply" and r.get("question"):
                turns.append((None, "them", r["question"])); ats.append(r.get("at", ""))
            if r.get("answer"):
                turns.append((None, "you", r["answer"])); ats.append(r.get("at", ""))
        if turns:
            todo.append(("message", who, who, rows[0].get("at", ""), turns, stamp, ats))
        if len(todo) >= limit:
            break
    return todo


def index_once(limit: int = 200) -> int:
    """(Re)index the calls and message threads that are new or changed, up to `limit` of them.
    Returns how many. Nothing while the model may not run (a call is on): they wait."""
    if not ready():
        return 0
    n = 0
    for kind, source, person, at, turns, stamp, ats in _sources(limit):
        parts = pieces(turns)
        vecs = embed([DOC.format(p) for p, _, _ in parts]) if parts else []
        if vecs is None:
            break                                  # a call came on, or the model is gone
        with _db_lock:
            con = _connect()
            try:
                old = con.execute("SELECT id, text FROM pieces WHERE kind = ? AND source = ?",
                                  (kind, source)).fetchall()
                for r in old:
                    con.execute("INSERT INTO pieces_fts(pieces_fts, rowid, text) VALUES ('delete', ?, ?)",
                                (r["id"], r["text"]))
                con.execute("DELETE FROM pieces WHERE kind = ? AND source = ?", (kind, source))
                for i, ((text, turn, t), v) in enumerate(zip(parts, vecs)):
                    cur = con.execute(
                        "INSERT INTO pieces (kind, source, person, at, idx, turn, t, text, vec) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (kind, source, person, ats[turn] if ats else at, i, turn, t, text,
                         v.astype("float16").tobytes()))
                    con.execute("INSERT INTO pieces_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
                con.execute("INSERT OR REPLACE INTO indexed VALUES (?, ?)", (f"{kind}:{source}", stamp))
                con.commit()
            finally:
                con.close()
        n += 1
    if n:
        logger.info("search: indexed %d conversation(s)", n)
    return n


def forget_call(call_id: str) -> int:
    """Remove one call's pieces — the owner deleted it (erase.py). Returns how many went.

    With the DB alone: the embedding model need not be loaded to forget something.
    """
    if not DB.exists():
        return 0
    with _db_lock:
        con = _connect()
        try:
            old = con.execute("SELECT id, text FROM pieces WHERE kind = 'call' AND source = ?",
                              (call_id,)).fetchall()
            for r in old:
                con.execute("INSERT INTO pieces_fts(pieces_fts, rowid, text) VALUES ('delete', ?, ?)",
                            (r["id"], r["text"]))
            con.execute("DELETE FROM pieces WHERE kind = 'call' AND source = ?", (call_id,))
            con.execute("DELETE FROM indexed WHERE key = ?", ("call:" + call_id,))
            con.commit()
        finally:
            con.close()
    return len(old)


# ---- asking ------------------------------------------------------------------------------------

def search(q: str, who: str = "", k: int = 8) -> list[dict]:
    """The pieces that best answer `q`, best first, from calls and messages alike:
    {kind, source, person, at, turn, t, text, how}. `kind` is "call" (source = the call's id, `t`
    seconds into its recording when known) or "message" (source = the person; `at` is the
    message's time). `how` says which half found it: "meaning", "words", or "both". With the model
    unavailable (a call is on, or it is not downloaded) the keyword half answers alone.
    """
    import numpy as np
    q = (q or "").strip()
    if not q:
        return []
    with _db_lock:
        con = _connect()
        try:
            where, args = ("WHERE person = ?", (who,)) if who else ("", ())
            rows = con.execute("SELECT id, kind, source, person, at, turn, t, text, vec "
                               f"FROM pieces {where}", args).fetchall()
            words: list[int] = []
            if len(q) >= 3:                       # the trigram index needs three characters
                phrase = '"' + q.replace('"', '""') + '"'
                try:
                    words = [r[0] for r in con.execute(
                        "SELECT rowid FROM pieces_fts WHERE pieces_fts MATCH ? ORDER BY rank LIMIT 50",
                        (phrase,))]
                except sqlite3.OperationalError:
                    words = []
        finally:
            con.close()
    by_id = {r["id"]: r for r in rows}
    words = [i for i in words if i in by_id]
    meaning: list[int] = []
    qv = embed([QUERY.format(q)])
    if qv is not None and rows:
        have = [r for r in rows if r["vec"]]
        if have:
            m = np.frombuffer(b"".join(r["vec"] for r in have), dtype=np.float16).reshape(len(have), DIMS)
            scores = m.astype(np.float32) @ qv[0]
            meaning = [have[i]["id"] for i in np.argsort(-scores)[:50]
                       if scores[i] >= MIN_SIMILARITY]
    # RECIPROCAL RANK FUSION: each list votes 1/(60 + rank); found by both, it gets both votes.
    score: dict[int, float] = {}
    for ranked in (meaning, words):
        for rank, pid in enumerate(ranked):
            score[pid] = score.get(pid, 0.0) + 1.0 / (60 + rank)
    out = []
    for pid in sorted(score, key=score.get, reverse=True)[:k]:
        r = by_id[pid]
        how = "both" if pid in meaning and pid in words else ("meaning" if pid in meaning else "words")
        out.append({"kind": r["kind"], "source": r["source"], "person": r["person"], "at": r["at"],
                    "turn": r["turn"], "t": r["t"], "text": r["text"], "how": how,
                    # the call's id, as the hub has used it
                    "call_id": r["source"] if r["kind"] == "call" else ""})
    return out


_wake: "asyncio.Event | None" = None


def wake() -> None:
    """A transcript just landed: index it now rather than at the next pass."""
    if _wake is not None:
        _wake.set()


async def worker() -> None:
    """Keep the index up to date, forever, off the event loop."""
    global _wake
    _wake = asyncio.Event()
    while True:
        try:
            await asyncio.to_thread(index_once)
        except Exception as exc:
            logger.error("the search indexer hit %s: %s", type(exc).__name__, exc)
        try:
            await asyncio.wait_for(_wake.wait(), POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wake.clear()
