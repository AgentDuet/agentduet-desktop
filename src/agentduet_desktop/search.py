"""Search what was said on calls — by meaning and by keyword, on this machine (2026-10-03).

TWO HALVES, MERGED:
  - MEANING. EmbeddingGemma 300M turns each piece of a transcript into 256 numbers; a question is
    turned the same way, and the closest pieces are its answers. It matches across languages and
    through speech-recognition slips: "who hasn't paid" finds a Vietnamese "hóa đơn … chưa thanh
    toán" and "the in voice is over due". Chosen by a bake-off on call-shaped passages
    (2026-10-03): 14/14 first-place answers, against 12/14 for Qwen3-Embedding 0.6B at twice the
    size — and still 14/14 at 256 numbers instead of 768 (EmbeddingGemma is trained for that).
  - KEYWORDS. SQLite's full-text index, with the TRIGRAM tokenizer: exact names, numbers and
    amounts, and Chinese, which has no spaces between words to split on.
A result found by both ranks above one found by either (reciprocal rank fusion).

THE INDEX (`run/search.db`) is derived from the transcripts: a call is (re)indexed whenever its
`.txt` changes, and the whole file can be deleted and rebuilt. It never holds anything the
transcripts do not, and it lives in this instance, never in the recorder's folder.

THE MODEL is one of the occasional ones (slot.py): loaded to index or to answer a question, then
unloaded when idle or when speech or the decision model needs the room. During a call it waits —
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

REPO = "ggml-org/embeddinggemma-300m-qat-q8_0-GGUF"
#: PINNED: the file is checked by size, so a new upload must be a deliberate bump.
REVISION = "66f974f8cd48cc3b9c41c516b95508e75b4bee64"
FILE, SIZE = "embeddinggemma-300m-qat-Q8_0.gguf", 328577056
#: The first DIMS of the model's 768 numbers, renormalised (Matryoshka). Measured: no loss at 256.
DIMS = 256
#: What the model is told each text is, in its own prompt format.
QUERY, DOC = "task: search result | query: {}", "title: none | text: {}"

#: A piece is up to this many turns, the next one starting a turn before this one ends — so an
#: answer split across turns is whole in at least one piece.
TURNS, OVERLAP = 3, 1
#: A single turn longer than this is cut, so one monologue is not one piece.
MAX_CHARS = 600

#: A MEANING HIT BELOW THIS IS NO ANSWER. Without it the closest piece always "matches", and
#: "invoice" on calls that never mentioned one returned three "Okay. Bye" pieces. Measured
#: 2026-10-03: right answers scored 0.457-0.697 (bake-off) and 0.48-0.59 (real calls); topics with
#: no answer 0.399-0.472 and 0.40-0.42. They overlap a little — no line is perfect — so this keeps
#: every right answer seen and drops most non-answers. Keyword hits are never cut by it.
MIN_SIMILARITY = 0.45

#: How often the indexer looks for calls whose transcripts changed. Woken sooner after a transcript.
POLL_SECONDS = 300

DB = paths.RUN / "search.db"
_db_lock = threading.Lock()
_model = None
_model_lock = threading.Lock()


def folder():
    return paths.HOME / "models" / "embed"


def ready() -> bool:
    f = folder() / FILE
    return f.is_file() and f.stat().st_size == SIZE


_fetching = threading.Lock()


def fetch() -> bool:
    from . import models
    if not _fetching.acquire(blocking=False):
        return False
    try:
        folder().mkdir(parents=True, exist_ok=True)
        models.fetch_file(f"https://huggingface.co/{REPO}/resolve/{REVISION}/{FILE}", folder() / FILE, SIZE)
        return True
    except Exception as exc:
        logger.warning("search model download stopped: %s", exc)
        return False
    finally:
        _fetching.release()


def fetch_in_background() -> None:
    if ready() or _fetching.locked():
        return
    logger.info("fetching the search model (%d MB) in the background", SIZE // 2**20)
    threading.Thread(target=fetch, name="search-fetch", daemon=True).start()


# ---- the model, in the slot --------------------------------------------------------------------

def _unload() -> None:
    global _model
    with _model_lock:
        if _model is None:
            return
        try:
            _model.close()
        except Exception:
            pass
        _model = None
    logger.info("search model unloaded")


def _register_slot() -> None:
    from . import slot
    slot.register(slot.EMBED, _unload)


_register_slot()


def embed(texts: list[str]) -> "list[list[float]] | None":
    """Each text as DIMS numbers, unit length. None when the model may not run now (a call is on —
    slot.py) or is not here."""
    from . import slot
    global _model
    if not texts or not ready() or not slot.claim(slot.EMBED):
        return None
    import numpy as np
    with _model_lock:
        if _model is None:
            from llama_cpp import Llama
            from . import machine
            _model = Llama(model_path=str(folder() / FILE), embedding=True, n_ctx=2048,
                           n_gpu_layers=-1 if machine.can_offload() else 0, verbose=False)
            logger.info("search model loaded")
        v = np.array(_model.embed(texts, normalize=True), dtype=np.float32)[:, :DIMS]
    slot.touch(slot.EMBED)
    v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
    return v


# ---- the index ---------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pieces (
    id INTEGER PRIMARY KEY,
    call_id TEXT NOT NULL,
    person TEXT NOT NULL,
    at TEXT NOT NULL,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    vec BLOB                    -- DIMS float16; NULL until embedded
);
CREATE INDEX IF NOT EXISTS pieces_call ON pieces (call_id);
CREATE INDEX IF NOT EXISTS pieces_person ON pieces (person);
CREATE VIRTUAL TABLE IF NOT EXISTS pieces_fts USING fts5(text, content='pieces', content_rowid='id',
                                                        tokenize='trigram');
-- What each call was indexed from: the transcript's size and time, so a changed one is redone.
CREATE TABLE IF NOT EXISTS indexed (call_id TEXT PRIMARY KEY, stamp TEXT NOT NULL);
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    return con


def pieces(body: str) -> list[str]:
    """A transcript's body as overlapping pieces of up to TURNS turns."""
    turns = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if turns and not re.match(r"^(them|you):", line):
            turns[-1] += " " + line                 # a continuation of the turn before
        else:
            turns.append(line)
    cut = []
    for t in turns:                                # one long turn becomes several
        while len(t) > MAX_CHARS:
            at = t.rfind(" ", 0, MAX_CHARS)
            at = at if at > MAX_CHARS // 2 else MAX_CHARS
            cut.append(t[:at])
            t = t[at:].strip()
        cut.append(t)
    if len(cut) <= TURNS:
        return ["\n".join(cut)] if cut else []
    step = TURNS - OVERLAP
    return ["\n".join(cut[i:i + TURNS]) for i in range(0, len(cut) - OVERLAP, step)]


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


def index_once(limit: int = 200) -> int:
    """(Re)index calls whose transcripts are new or changed, up to `limit` of them. Returns how
    many. Nothing while the model may not run (a call is on): they wait for the next pass."""
    from . import calls
    if not ready():
        return 0
    with _db_lock:
        con = _connect()
        try:
            done = {r["call_id"]: r["stamp"] for r in con.execute("SELECT call_id, stamp FROM indexed")}
        finally:
            con.close()
    from . import carry
    todo = []
    for row in calls.recent(None):
        cid = row.get("call_id", "")
        if not cid:
            continue
        path, stamp = _transcript_file(row)
        if path is None or done.get(cid) == stamp:
            continue
        body = carry.read_body(path)
        if body:
            todo.append((row, body, stamp))
        if len(todo) >= limit:
            break
    n = 0
    for row, body, stamp in todo:
        parts = pieces(body)
        vecs = embed([DOC.format(p) for p in parts]) if parts else []
        if vecs is None:
            break                                  # a call came on, or the model is gone
        cid = row["call_id"]
        with _db_lock:
            con = _connect()
            try:
                for (pid,) in con.execute("SELECT id FROM pieces WHERE call_id = ?", (cid,)).fetchall():
                    con.execute("INSERT INTO pieces_fts(pieces_fts, rowid, text) "
                                "SELECT 'delete', id, text FROM pieces WHERE id = ?", (pid,))
                con.execute("DELETE FROM pieces WHERE call_id = ?", (cid,))
                for i, (text, v) in enumerate(zip(parts, vecs)):
                    cur = con.execute(
                        "INSERT INTO pieces (call_id, person, at, idx, text, vec) VALUES (?,?,?,?,?,?)",
                        (cid, calls.person_of(row), row.get("at", ""), i, text,
                         v.astype("float16").tobytes()))
                    con.execute("INSERT INTO pieces_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
                con.execute("INSERT OR REPLACE INTO indexed VALUES (?, ?)", (cid, stamp))
                con.commit()
            finally:
                con.close()
        n += 1
    if n:
        logger.info("search: indexed %d call(s)", n)
    return n


# ---- asking ------------------------------------------------------------------------------------

def search(q: str, who: str = "", k: int = 8) -> list[dict]:
    """The pieces that best answer `q`, best first: {call_id, person, at, text, how}.

    `how` says which half found it: "meaning", "words", or "both". With the model unavailable
    (a call is on, or it is not downloaded) the keyword half answers alone.
    """
    import numpy as np
    q = (q or "").strip()
    if not q:
        return []
    with _db_lock:
        con = _connect()
        try:
            where, args = ("WHERE person = ?", (who,)) if who else ("", ())
            rows = con.execute(f"SELECT id, call_id, person, at, text, vec FROM pieces {where}",
                               args).fetchall()
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
        out.append({"call_id": r["call_id"], "person": r["person"], "at": r["at"],
                    "text": r["text"], "how": how})
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
