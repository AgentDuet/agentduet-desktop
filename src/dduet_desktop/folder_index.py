"""Persistent chunk index for granted folders.

Lives in the USER's home (`~/.dduet` by default, `DDUET_HOME` to override), not in the
checkout — the folders it indexes don't live here either (`~/ext-projects/product-hub`
has nothing to do with this sample), it is derived data about the *user's* files, three
processes need it (daemon, MCP, sim), and it should survive moving the code.

What this buys over the previous in-memory cache:

- **No cold start.** Chunks are rebuilt only when files actually change, so a restart
  costs a stat-scan instead of re-reading megabytes.
- **Incremental.** Touching one file re-chunks that file, not the whole folder.
- **Shared + inspectable.** `status()` tells the owner what a grant actually contains,
  which matters now that the grant is the security boundary — granting a repo root
  should be an informed act.

Staleness is tracked two ways, deliberately:
  `indexed_at`   — when chunks were last (re)built.
  `last_scanned` — when we last *checked* for changes (cheap stat walk).
  `files`        — per-file mtime+size, so we know exactly what moved.
A folder can be freshly scanned but long-since indexed; that's the healthy case and the
two timestamps make it legible.
"""

import hashlib
import json
import os
import pathlib
from datetime import datetime

from . import paths

INDEX_VERSION = 1

READABLE_SUFFIXES = {".md", ".txt"}
MAX_FILE_BYTES = 64 * 1024
CHUNK_CHARS = 1400

_mem: dict[str, dict] = {}          # path -> loaded index, avoids re-reading JSON


def home() -> pathlib.Path:
    return paths.home()


def index_dir() -> pathlib.Path:
    d = home() / "index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def root_of(folder: str) -> pathlib.Path:
    """Resolve a grant to a real path. Relative paths are relative to the INSTANCE.

    Framework documents (how the secretary works) are shipped as a template in the install and
    COPIED into the instance on first run, like owner.md and permissions.json — so the owner can
    tune what their secretary says about itself. Nothing is read from the install at runtime.
    """
    p = pathlib.Path(folder)
    return (p if p.is_absolute() else paths.HOME / p).resolve()


def files_under(root: pathlib.Path):
    """Readable files genuinely inside `root`. Symlinks pointing out are rejected —
    the allowlist is only a boundary if it cannot be walked out of."""
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        real = p.resolve()
        if not real.is_relative_to(root):
            continue
        if real.suffix.lower() not in READABLE_SUFFIXES:
            continue
        try:
            if real.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield real


def split(text: str) -> list[str]:
    """Chunk on markdown headings, then hard-wrap anything still oversized."""
    parts, cur = [], []
    for line in text.splitlines():
        if line.startswith("#") and cur and sum(len(x) for x in cur) > 200:
            parts.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        parts.append("\n".join(cur))

    out = []
    for p in parts:
        while len(p) > CHUNK_CHARS:
            out.append(p[:CHUNK_CHARS])
            p = p[CHUNK_CHARS:]
        if p.strip():
            out.append(p)
    return out


def _path_for(folder: str) -> pathlib.Path:
    root = root_of(folder)
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return index_dir() / f"{root.name or 'root'}-{digest}.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _scan(root: pathlib.Path) -> dict[str, list]:
    """Current [mtime, size] per file — the staleness fingerprint."""
    out = {}
    for f in files_under(root):
        st = f.stat()
        out[str(f)] = [st.st_mtime, st.st_size]
    return out


def load(folder: str) -> dict | None:
    p = _path_for(folder)
    key = str(p)
    if key in _mem:
        return _mem[key]
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("version") != INDEX_VERSION:
        return None
    _mem[key] = data
    return data


def staleness(folder: str) -> dict:
    """What changed since the last build. Cheap: stats only, never reads content."""
    root = root_of(folder)
    idx = load(folder)
    current = _scan(root)
    if idx is None:
        return {"indexed": False, "stale": True, "added": len(current),
                "changed": 0, "removed": 0}
    known = idx.get("files", {})
    added = [f for f in current if f not in known]
    removed = [f for f in known if f not in current]
    changed = [f for f in current if f in known and current[f] != known[f]]
    return {
        "indexed": True,
        "stale": bool(added or removed or changed),
        "added": len(added), "changed": len(changed), "removed": len(removed),
        "indexed_at": idx.get("indexed_at"),
        "last_scanned": idx.get("last_scanned"),
    }


def build(folder: str, force: bool = False) -> dict:
    """(Re)build incrementally — only files whose mtime/size moved are re-read."""
    root = root_of(folder)
    p = _path_for(folder)
    idx = None if force else load(folder)
    current = _scan(root)

    old_files = (idx or {}).get("files", {})
    old_chunks = (idx or {}).get("chunks", {})       # path -> [chunk, ...]
    chunks: dict[str, list[str]] = {}
    reread = 0

    for path, stamp in current.items():
        if not force and path in old_files and old_files[path] == stamp and path in old_chunks:
            chunks[path] = old_chunks[path]          # unchanged — keep as-is
            continue
        try:
            chunks[path] = split(pathlib.Path(path).read_text(errors="replace"))
            reread += 1
        except OSError:
            continue

    data = {
        "version": INDEX_VERSION,
        "folder": folder,
        "root": str(root),
        "indexed_at": _now(),
        "last_scanned": _now(),
        "files": current,
        "chunks": chunks,
        "file_count": len(chunks),
        "chunk_count": sum(len(c) for c in chunks.values()),
        "bytes": sum(s[1] for s in current.values()),
        "reread": reread,
    }
    p.write_text(json.dumps(data))
    _mem[str(p)] = data
    return data


def chunks(folder: str) -> list[tuple[str, str]]:
    """[(source_label, chunk_text)] — rebuilds only if the folder actually changed."""
    root = root_of(folder)
    if not root.is_dir():
        return []

    idx = load(folder)
    if idx is None or staleness(folder)["stale"]:
        idx = build(folder)
    else:
        idx["last_scanned"] = _now()                 # cheap: record the check itself
        _path_for(folder).write_text(json.dumps(idx))

    out = []
    for path, cs in idx.get("chunks", {}).items():
        try:
            label = f"{folder}/{pathlib.Path(path).relative_to(root)}"
        except ValueError:
            label = path
        out.extend((label, c) for c in cs)
    return out


def warm(folders: list[str]) -> list[dict]:
    """Build everything up front so the first real question isn't the slowest one."""
    return [build(f) for f in folders if root_of(f).is_dir()]


def status(folders: list[str]) -> list[dict]:
    """Owner-facing view: what a grant actually contains, and how fresh it is."""
    out = []
    for f in folders:
        root = root_of(f)
        if not root.is_dir():
            out.append({"folder": f, "missing": True})
            continue
        idx = load(f)
        s = staleness(f)
        out.append({
            "folder": f,
            "files": (idx or {}).get("file_count", 0),
            "chunks": (idx or {}).get("chunk_count", 0),
            "bytes": (idx or {}).get("bytes", 0),
            "indexed_at": (idx or {}).get("indexed_at"),
            "last_scanned": (idx or {}).get("last_scanned"),
            "stale": s["stale"],
            "changes": {k: s[k] for k in ("added", "changed", "removed")},
        })
    return out


# --- scope ---------------------------------------------------------------------
# The agent used to see 12 retrieved chunks and nothing else, so it could not tell
# "we don't document that" from "I didn't find it". Both came out as not_grounded.
#
# The index already knows every file and heading in a granted folder, so a cheap
# summary of *what a folder is about* gives the agent a sense of the whole. That lets
# it judge scope, and tell the asker what it CAN help with.

MAX_TOPICS_PER_FOLDER = 12


def topics(folder: str) -> list[str]:
    """What a folder is ABOUT — subject areas, not document structure.

    First attempt ranked headings by frequency, which surfaced the template
    ("Overview", "Specifications", "Description") because those repeat across every
    document. The signal for subject matter is instead:

      1. top-level directory names — how the owner themselves filed it, and
      2. distinct document titles (each file's first H1), which name one thing each.
    """
    idx = load(folder)
    if idx is None:
        return []
    root = root_of(folder)

    dirs, titles = [], []
    for path, cs in idx.get("chunks", {}).items():
        try:
            rel = pathlib.Path(path).relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > 1:
            d = rel.parts[0].replace("-", " ").replace("_", " ")
            if d not in dirs and not d.startswith("."):
                dirs.append(d)
        for c in cs[:1]:                       # the title lives in the first chunk
            for line in c.splitlines():
                if line.startswith("# "):
                    t = line[2:].strip().strip("*")
                    if 3 <= len(t) <= 60:
                        titles.append(t)
                    break

    # Directories first (the owner's own filing), then titles that add something new.
    seen = {d.lower() for d in dirs}
    picked = list(dirs)
    for t in titles:
        if len(picked) >= MAX_TOPICS_PER_FOLDER:
            break
        if t.lower() not in seen:
            seen.add(t.lower())
            picked.append(t)
    return picked[:MAX_TOPICS_PER_FOLDER]


def scope_digest(folders: list[str]) -> str:
    """One line per granted folder: what it covers. '' when nothing is granted."""
    lines = []
    for f in folders:
        idx = load(f)
        if idx is None or not idx.get("file_count"):
            continue
        ts = topics(f)
        label = f.rstrip("/").split("/")[-1] or f
        lines.append(f"- {label} ({idx['file_count']} documents)"
                     + (f": {', '.join(ts)}" if ts else ""))
    return "\n".join(lines)
