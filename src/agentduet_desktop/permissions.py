"""Folder-scoped access control — what the agent may READ.

Two orthogonal gates in this POC; don't merge them:

  ACCESS    (here)      may the agent read this file?      -> folder allowlist
  AUTHORITY (policy.py) may it say this on your behalf?    -> hard rules

A price inside an allowed folder is readable but still not quotable. Keeping them
separate is what lets the owner widen access without accidentally widening authority.

Enforcement is in code, never in the prompt: the model only ever sees text that
`context_for()` handed it. Roots are resolved with realpath, so symlinks and `..`
cannot escape an allowed folder.

Permissions grow over time: every escalation is a prompt to grant a bit more
(`grant_folder` in secretary_mcp.py). Per-asker scoping is only meaningful because
the inbound identity is verified — that's the whole point.
"""
from __future__ import annotations


import json
import pathlib
import re

from . import folder_index

from . import paths

PERMS = paths.PERMISSIONS

READABLE_SUFFIXES = {".md", ".txt"}
MAX_FILE_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 200 * 1024

DEFAULT_PERMS = {
    "_comment": "Folders the agent may READ. Paths are relative to secretary-sample/ "
                "or absolute. 'default' applies to everyone; 'askers' adds per-identity.",
    # ONE audience for now: knowledge/ is flat and readable by anyone who writes in. A fact
    # only one person may hear belongs in people/<identity>.md, not in a scoped knowledge
    # folder — that is the distinction that replaced public/ vs partners/.
    "default": {"folders": ["knowledge"]},
    "askers": {},
    "grants": [],
}


def load() -> dict:
    if not PERMS.exists():
        PERMS.write_text(json.dumps(DEFAULT_PERMS, indent=2))
    return json.loads(PERMS.read_text())


def save(data: dict) -> None:
    PERMS.write_text(json.dumps(data, indent=2))


def trusted(asker: str) -> bool:
    """Has the owner explicitly vouched for this identity?

    DDUET is a public channel with self-service login, so the channel itself cannot
    vouch for an address (see my-agenda.md). But the owner can: naming an address here
    is a deliberate act, exactly like granting a folder. Everyone else stays unverified.
    """
    if not asker:
        return False
    ids = load().get("trusted_identities", [])
    return asker.strip().lower() in {i.strip().lower() for i in ids}


def folders_for(asker: str, verified: bool = False) -> list[str]:
    """Allowed folders for this identity.

    default + permissions.json grants + the person's profile '## Folders' section.
    Grants apply only to a VERIFIED identity — a self-declared one must never inherit
    someone else's access. Verification is passed in, never inferred from the channel.
    """
    from . import people                # local import: keeps the module dependency-light

    p = load()
    folders = list(p.get("default", {}).get("folders", []))
    if not verified:
        return folders                  # unverified -> public defaults only

    for f in p.get("askers", {}).get(asker, {}).get("folders", []):
        if f not in folders:
            folders.append(f)
    for f in people.folders_for(asker, True):
        if f not in folders:
            folders.append(f)
    return folders


#: What any caller may do before the owner has said anything about them. Every stranger starts
#: here, so it is the only setting that matters at scale.
#:
#: `search_knowledge` because answering is the product, and `escalate` because refusing safely has
#: to remain possible. Never `book` — that acts on the owner's behalf — and never
#: `transfer_to_owner`, which rings their phone.
DEFAULT_TOOLS = ["search_knowledge", "escalate"]

#: NOT REVOCABLE, by anyone, including the owner.
#:
#: Take `escalate` away and an agent facing a question it cannot answer has no legitimate move
#: left — which is exactly the state in which a model invents one. The safety valve cannot live
#: inside the system that can withdraw it, so this is enforced here rather than left to the owner
#: to remember.
ALWAYS_TOOLS = ["escalate"]


def tools_for(asker: str, verified: bool = False) -> list[str]:
    """Which asker-side tools this caller may invoke.

    The second half of that proposal (2026-08-04): disclosure already follows a per-caller grant,
    so authority follows the same one. Same shape as `folders_for`, and the same rule — a grant
    reaches an identity only once it is VERIFIED, because an unverified address is a claim.

    This is NOT the fence. The fence is the compiled registry in `voice.py`, which decides what
    the product offers at all; this decides which of those a given caller gets. Both are checked,
    and neither substitutes for the other.
    """
    p = load()
    granted = list(p.get("default", {}).get("tools", DEFAULT_TOOLS))
    if verified:
        for t in p.get("askers", {}).get(asker, {}).get("tools", []):
            if t not in granted:
                granted.append(t)
    for t in ALWAYS_TOOLS:
        if t not in granted:
            granted.append(t)
    return granted


def _root(folder: str) -> pathlib.Path:
    return folder_index.root_of(folder)


# --- retrieval ---------------------------------------------------------------
# Reading every permitted file breaks on a real folder: a 2.7 MB repo against a
# 200 KB cap silently yields ~7% of it, chosen alphabetically, while looking like it
# worked. So chunk, rank against the question, and send only what's relevant.

MAX_CHUNKS = 12
MAX_RETRIEVED_BYTES = 24 * 1024

_STOP = {"the", "a", "an", "is", "are", "was", "were", "do", "does", "did", "what", "which",
         "who", "how", "why", "when", "where", "can", "could", "would", "should", "i", "you",
         "we", "they", "it", "of", "to", "in", "on", "for", "and", "or", "with", "your", "my",
         "please", "about", "there", "this", "that", "be", "been", "have", "has", "will"}

def _terms(text: str) -> list[str]:
    """Lowercase word stems, stopwords dropped. Crude stemming so 'channels' hits
    'channel' — good enough to rank, and it never decides access."""
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", text.lower()):
        if w in _STOP:
            continue
        for suf in ("ing", "ies", "es", "ed", "s"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)] + ("y" if suf == "ies" else "")
                break
        out.append(w)
    return out


def _folder_chunks(folder: str) -> list[tuple[str, str]]:
    """Chunks for a folder, from the persistent index in ~/.dduet (rebuilt only when
    the folder's files actually changed — see folder_index)."""
    return folder_index.chunks(folder)


def context_for(asker: str, verified: bool = False, query: str = "") -> tuple[str, list[str]]:
    """What this identity's question may be answered from.

    Returns (context_text, source_paths). Empty sources means: answer nothing,
    escalate. With a `query`, only the best-matching chunks are returned, so a large
    permitted folder stays usable.
    """
    folders = folders_for(asker, verified)
    pool: list[tuple[str, str]] = []
    for folder in folders:
        pool.extend(_folder_chunks(folder))
    if not pool:
        return "", []

    q = set(_terms(query))
    if q:
        scored = []
        for label, text in pool:
            t = _terms(text)
            if not t:
                continue
            hits = sum(1 for w in t if w in q)
            if not hits:
                continue
            # coverage of the question matters more than raw frequency
            coverage = len(q & set(t)) / len(q)
            scored.append((coverage * 2 + hits / len(t), label, text))
        scored.sort(key=lambda x: -x[0])
        picked = [(l, t) for _, l, t in scored[:MAX_CHUNKS]]
    else:
        picked = []

    if not picked:
        # Nothing matched (or no query): fall back to the start of the smallest
        # folders, so greetings and general questions still work.
        picked = pool[:MAX_CHUNKS]

    out, sources, total = [], [], 0
    for label, text in picked:
        if total + len(text) > MAX_RETRIEVED_BYTES:
            break
        out.append(f"--- source: {label} ---\n{text}")
        if label not in sources:
            sources.append(label)
        total += len(text)

    return "\n\n".join(out), sources


def grant(asker: str, folder: str, note: str = "") -> str:
    """Let one verified identity read one more folder."""
    root = _root(folder)
    if not root.is_dir():
        return f"No such folder: {folder}"

    p = load()
    entry = p.setdefault("askers", {}).setdefault(asker, {"folders": []})
    if folder in entry["folders"]:
        return f"{asker} already has {folder}"
    entry["folders"].append(folder)
    p.setdefault("grants", []).append({"asker": asker, "folder": folder, "note": note})
    save(p)
    return f"Granted {folder} to {asker}"


def revoke(asker: str, folder: str) -> str:
    p = load()
    entry = p.get("askers", {}).get(asker)
    if not entry or folder not in entry.get("folders", []):
        return f"{asker} does not have {folder}"
    entry["folders"].remove(folder)
    save(p)
    return f"Revoked {folder} from {asker}"
