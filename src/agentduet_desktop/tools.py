"""Owner tools — the single implementation behind BOTH owner surfaces.

`secretary_mcp.py` (MCP) and `web.py` (local site) are thin wrappers over this module.
Implementing a command twice is how the two faces drift apart, so they don't.

SECURITY: this module is the OWNER's tool registry — it can grant folder access and
send messages as the owner. The external-facing path (`secretary_agent.on_message`)
must never import or reach it. `test_isolation.py` asserts that.
"""

import json
import os
import pathlib
import re
from collections import Counter
from datetime import date, datetime, timedelta

from . import llm
from . import folder_index
from . import people

from . import connector
from . import owner
from . import paths


RUN = paths.RUN
LOG = RUN / "queries.jsonl"


def _capabilities():
    """The secretary's capability machinery, or None when no capability is declared.

    Knowledge is genuinely shared — the owner's Personal Assistant edits the same documents the
    answering agent reads — but two of its guards ask a question only the secretary can answer:
    does this prose contradict a bound the agent is held to, and where does a capability's
    document live. Asking costs an import of the answering agent and the five modules behind it.

    So it is asked ONLY when a capability actually exists. On a recorder install
    `capabilities.json` is not there, the answer is nothing, and nothing is imported. This is the
    single place `tools` may reach toward `secretary_tools`, and it must stay a lazy call inside
    a function — a module-level import here reverses the arrow and tests/test_boundary.py fails.
    """
    if not paths.CAPABILITIES.is_file():
        return None
    from . import secretary_tools
    return secretary_tools
#: How many escalations the last read hid as aged-out. Reported in the UI so nothing
#: disappears without the owner being able to see that it did.
_aged_count = [0]
def rows() -> list[dict]:
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
# Only the numeric bounds are compared. A boolean like verified_only cannot be judged from a
# sentence without guessing, and guessing here would block legitimate facts.
_TIME24 = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_TIME12 = re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.I)
def _knowledge_target(file: str) -> tuple[pathlib.Path | None, str]:
    """Resolve a requested destination inside the knowledge root, or explain the refusal."""
    root = paths.KNOWLEDGE.resolve()
    if not file.strip():
        return None, ("Name the destination. There is deliberately no general file: a catch-all "
                      "becomes a second home for subjects that already have one, and the two "
                      "then disagree. Use list_knowledge, then pick by KIND of fact:\n"
                      "  - about a domain the agent can also ACT in -> that capability's "
                      "document, named after it (see the index)\n"
                      "  - about the owner -> about.md\n"
                      "  - about how the secretary itself works -> secretary.md\n"
                      "  - about ONE person, or anything not everyone should hear -> "
                      "note_person, NOT a knowledge file: knowledge/ is readable by anyone "
                      "who writes in\n"
                      "  - a subject with no capability -> the document for that subject\n"
                      "If none fits, ask the owner where it belongs rather than inventing a "
                      "general file.")
    want = file.strip().replace("\\", "/")
    if want.startswith("/"):
        # Stripping the slash would silently reinterpret /etc/x.md as knowledge/etc/x.md —
        # safe, but the refusal then blames a missing folder instead of the boundary.
        return None, (f"'{file}' is an absolute path. Give a path inside the knowledge "
                      f"folder, e.g. public/learned.md.")
    rel = want.removeprefix("knowledge/")
    if not rel.endswith(".md"):
        return None, f"Knowledge files are markdown; '{file}' is not a .md file."
    target = (paths.KNOWLEDGE / rel).resolve()
    if not target.is_relative_to(root):
        # Covers ../ and symlink escapes. Reads may range wider than writes on purpose.
        return None, (f"'{file}' is outside {root}. Knowledge writes stay inside the "
                      f"knowledge folder — granted folders can be real source trees.")
    if not target.parent.is_dir():
        have = ", ".join(sorted(d.name for d in paths.KNOWLEDGE.iterdir() if d.is_dir()))
        return None, (f"There is no '{rel.rsplit('/', 1)[0]}' folder. A new folder would also "
                      f"be readable by nobody until it is granted. Existing: {have}.")
    return target, ""
def _statements(f: pathlib.Path) -> list[str]:
    """The individually editable assertions in a document: bullets and headings.

    The unit matters. If a document is one prose blob, the agent can only append to it or
    rewrite it wholesale; a bullet is a thing that can be corrected in place.
    """
    out = []
    for line in f.read_text().splitlines():
        s = line.strip()
        if s.startswith("- ") and len(s) > 4:
            out.append(s[2:].strip())
        elif s.startswith("#"):
            out.append(s.lstrip("# ").strip())
    return out
def _duplicated() -> list[str]:
    """Subjects asserted in more than one document — the shape a contradiction takes here.

    Found by term overlap, not meaning, so it over-reports rather than missing: two documents
    both saying something about "channels" is worth the agent's attention even when they agree.
    Written because the agent corrected a channel count in one file and left the other one
    asserting the old number, leaving the knowledge base holding both.
    """
    root = paths.KNOWLEDGE
    seen = []
    for f in sorted(root.rglob("*.md")):
        for s in _statements(f):
            terms = {w for w in folder_index._terms(s) if len(w) > 3}
            if terms:
                seen.append((f, s, terms))
    out = []
    for i, (f1, s1, t1) in enumerate(seen):
        for f2, s2, t2 in seen[i + 1:]:
            if f1 == f2:
                continue
            shared = t1 & t2
            if len(shared) < 2 or len(shared) < 0.5 * min(len(t1), len(t2)):
                continue
            out.append(f"  '{', '.join(sorted(shared))}' [consolidate: correct one, "
                       f"delete the other]\n"
                       f"      {f1.relative_to(root.parent).as_posix()}: \"{s1[:70]}\"\n"
                       f"      {f2.relative_to(root.parent).as_posix()}: \"{s2[:70]}\"")
    return out
def _readers_of(target: pathlib.Path) -> str:
    """Who can be answered from this file.

    One audience: a fact only one person may hear belongs in people/<identity>.md, not in a
    knowledge folder with a narrower grant.
    """
    return "anyone who writes in"
def list_knowledge() -> str:
    """Index of the knowledge documents — what each asserts, and any subject in two of them."""
    root = paths.KNOWLEDGE
    if not root.is_dir():
        return "No knowledge folder yet."
    files = sorted(p for p in root.rglob("*.md") if p.is_file())
    if not files:
        return "No knowledge files yet."
    out = ["KNOWLEDGE INDEX — one subject belongs in ONE document. Correct in place; do not "
           "add a second version of something already stated."]
    for f in files:
        sts = _statements(f)
        out.append(f"\n{f.relative_to(root.parent).as_posix()}  "
                   f"(readable by: {_readers_of(f)})")
        for s in sts[:12]:
            out.append(f"    - {s[:96]}")
        if len(sts) > 12:
            out.append(f"    … +{len(sts) - 12} more (read_knowledge for the rest)")
    st = _capabilities()
    caps = (st.capabilities.all_capabilities() or {}) if st else {}
    if caps:
        out.append("\nCAPABILITY DOCUMENTS — what may be SAID about a domain the agent may ACT "
                   "in. Same name on both sides, so they cannot drift apart unnoticed.")
        for name in caps:
            d = st.doc_for(name)
            out.append(f"    {name}  ->  {d.relative_to(paths.KNOWLEDGE.parent).as_posix()}"
                       + ("" if d.exists() else "   MISSING — the agent can act here but has "
                                                "nothing documented to say about it"))
    dupes = _duplicated()
    if dupes:
        out.append("\nSAME SUBJECT IN MORE THAN ONE DOCUMENT — each line says what to do. "
                   "Overlap is flagged by wording, so some pairs will already agree.")
        out.extend(dupes[:8])
    return "\n".join(out)
def _overlapping(text: str, skip: pathlib.Path | None) -> list[tuple[pathlib.Path, str]]:
    """Existing statements about the same subject, anywhere in knowledge/."""
    terms = {w for w in folder_index._terms(text) if len(w) > 3}
    if not terms:
        return []
    out = []
    for f in sorted(paths.KNOWLEDGE.rglob("*.md")):
        if skip and f.resolve() == skip.resolve():
            continue
        for s in _statements(f):
            st = {w for w in folder_index._terms(s) if len(w) > 3}
            shared = terms & st
            if len(shared) >= 2 and len(shared) >= 0.5 * min(len(terms), len(st)):
                out.append((f, s))
    return out
def _replaces(new_fact: str, candidates: list[tuple[pathlib.Path, str]]) -> list[tuple]:
    """Which existing statements the new one would CONTRADICT or supersede.

    Term overlap alone cannot refuse a write — adding a second item to a menu overlaps with
    the first — so code narrows and the model judges. Cleaning at write time is the point: a
    contradiction that reaches the documents gets answered to an external party before anyone notices.
    """
    if not candidates:
        return []
    try:
        from . import brain
        c = brain.client()
    except Exception:                  # no model SDK installed (the model-free test suite)
        c = None
    if c is None:
        return []                      # no model: allow the write, do not silently guess
    listed = "\n".join(f"{i + 1}. {s}" for i, (_, s) in enumerate(candidates))
    prompt = (
        "A new fact is being saved to a knowledge base. Below are existing statements about a "
        "similar subject, visible to the same readers.\n\n"
        f"NEW FACT: {new_fact}\n\nEXISTING:\n{listed}\n\n"
        "Which existing statements does the new fact CONTRADICT or REPLACE — meaning both "
        "cannot be true at once, or the new one is an updated version of the old one? "
        "Statements that are simply about the same topic and can both stand are NOT replaced.\n"
        'Reply with only JSON: {"replaces": [<numbers>]}')
    try:
        raw = c.complete(prompt).strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        nums = json.loads(raw).get("replaces") or []
        return [candidates[int(n) - 1] for n in nums
                if str(n).isdigit() and 1 <= int(n) <= len(candidates)]
    except Exception:
        return []
def _write_time_check(fact: str, target: pathlib.Path) -> str:
    """"" if the fact can be saved as-is, else what must be resolved first."""
    hits = _replaces(fact, _overlapping(fact, skip=None))
    if not hits:
        return ""
    lines = [f"  - {f.relative_to(paths.KNOWLEDGE.parent).as_posix()}: \"{s[:100]}\""
             for f, s in hits]
    return ("This contradicts or replaces what is already recorded:\n" + "\n".join(lines)
            + "\nFix the existing statement with edit_knowledge instead of adding a second "
              "version. Two answers to one question means the agent will sometimes give the "
              "wrong one.")
EDIT_LOG = RUN / "knowledge-edits.jsonl"
def read_knowledge(file: str) -> str:
    """Show a knowledge document, so it can be corrected instead of appended to."""
    target, why = _knowledge_target(file)
    if target is None:
        return f"Cannot read: {why}"
    if not target.exists():
        return f"{file} does not exist yet. list_knowledge shows what does."
    where = target.resolve().relative_to(paths.KNOWLEDGE.resolve().parent).as_posix()
    return (f"{where} — readable by: {_readers_of(target)}\n"
            f"Edit it with edit_knowledge(file, old, new); `old` must appear exactly once.\n"
            f"-----\n{target.read_text()}")
def edit_knowledge(file: str, old: str, new: str = "") -> str:
    """Replace an exact snippet in a knowledge document — correct or delete a stale fact.

    Exact-and-unique on purpose. A fuzzy edit to the disclosure surface is a silent change to
    what external parties get told, and appending a correction instead (the only option before this)
    left both versions readable — the agent then answered with whichever one retrieval
    happened to surface.
    """
    if not old.strip():
        return "Give the exact text to replace. Use read_knowledge first."
    target, why = _knowledge_target(file)
    if target is None:
        return f"NOT edited. {why}"
    if not target.exists():
        return f"NOT edited. {file} does not exist. list_knowledge shows what does."
    text = target.read_text()
    hits = text.count(old)
    if hits == 0:
        return (f"NOT edited. That text is not in {file} — it may be worded differently. "
                f"Call read_knowledge('{file}') and copy the line exactly.")
    if hits > 1:
        return (f"NOT edited. That text appears {hits} times in {file}. Include enough "
                f"surrounding text to identify one of them.")
    if new.strip():
        st = _capabilities()
        clash = st._bound_conflict(new)[0] if st else ""
        if clash:
            return f"NOT edited. {clash}"

    after = text.replace(old, new, 1)
    if not new.strip():                       # a deletion should not leave a blank gap
        after = re.sub(r"\n{3,}", "\n\n", after)
    # Append-only record of every change to what external parties may be told. The edit itself is
    # destructive; this is what makes it recoverable and auditable.
    EDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EDIT_LOG.open("a") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                            "file": str(target), "before": text, "after": after}) + "\n")
    target.write_text(after)
    where = target.resolve().relative_to(paths.KNOWLEDGE.resolve().parent).as_posix()
    verb = "Removed from" if not new.strip() else "Edited"
    return (f"{verb} {where}\n  - was: {old.strip()[:160]}\n"
            + (f"  - now: {new.strip()[:160]}\n" if new.strip() else "")
            + f"Readable by: {_readers_of(target)}")
#: The headings owner.py parses out of settings.md. Only these may be set, because a typo would
#: write a section the code never reads — the same silent failure as the heading rename that
#: emptied the never-say list.
SETTING_FIELDS = {"name": "Name", "pronoun": "Pronoun", "voice": "Voice",
                  "never_say": "Never say", "phone": "Phone",
                  # What happens to a call: `answer` or `carry`. Settable like any other
                  # heading, but note it is the one whose WRONG value is not merely unhelpful —
                  # `carry` starts recording two people. owner.calls() only accepts an exact
                  # match and treats everything else as `answer`, so a typo cannot switch
                  # recording on by accident.
                  "calls": "Calls", "record_calls": "Record calls", "language": "Language",
                  "transcription": "Transcription",
                  # Where the audio goes. An absolute path; anything else falls back to the
                  # default rather than raising — see owner.recordings_dir().
                  "recordings": "Recordings"}
def _section_bullets(doc: pathlib.Path, heading: str) -> list[str]:
    """The `- ` bullets under one `## ` heading."""
    if not doc.is_file():
        return []
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)",
                  doc.read_text(), re.S | re.M)
    if not m:
        return []
    return [l.strip()[2:].strip() for l in m.group(1).splitlines() if l.strip().startswith("- ")]
def current_setup() -> dict:
    """What setup would prefill: the CURRENT state, not the answers that produced it.

    Re-running setup is how an owner changes their mind, so it has to open on what is true now.
    The free-text answers were never stored — the model turned them into settings and bullets —
    so the bullets ARE the answer, and showing them is more honest than showing a stale
    transcript of what was once typed.
    """
    doc = paths.KNOWLEDGE / "owner.md"
    return {
        "name": owner.name() if owner.name() != owner.DEFAULT_NAME else "",
        "pronoun": owner.pronoun_raw(),
        "does": "\n".join(_section_bullets(doc, "Who")),
        "contacts": "\n".join(_section_bullets(doc, "Contacts")),
        "available": "\n".join(_section_bullets(doc, "Availability")),
        "never": "\n".join(owner.never_say()),
        "phone": owner.phone(),
        "configured": owner.name() != owner.DEFAULT_NAME,
    }
def setup_status() -> str:
    """What is configured and what is still missing, and what only a terminal can do.

    Purpose-built for the owner's assistant. It can run the whole interview itself — set_setting,
    add_knowledge, declare_capability and grant_folder are all in this registry, and a
    conversation is a better interview than a list of CLI prompts ever was. What it CANNOT do is
    the two secrets: a model key or a connector credential typed into a chat box is sent to the
    model provider and written to run/owner_chat.json in plaintext.

    So this reports state and names the one command the assistant cannot run for them. It
    deliberately echoes NO values — not the key, not the connector uuid, not the owner's own
    number. Whether something is set is all that setup guidance needs, and anything returned
    here travels to a model provider.
    """
    from . import connector, llm
    ok = llm.configured()       # not verify(): that calls the model, and this is asked often
    cur = current_setup()
    st = _capabilities()
    caps = st.capabilities.all_capabilities() if st else {}
    docs = len(list(paths.KNOWLEDGE.glob("*.md"))) if paths.KNOWLEDGE.is_dir() else 0

    lines = [
        f"  name          {cur['name'] or 'NOT SET'}",
        f"  pronoun       {cur['pronoun'] or 'not set (it will use the name)'}",
        f"  what you do   {len(cur['does'].splitlines()) if cur['does'] else 0} fact(s) recorded",
        f"  knowledge     {docs} document(s)",
        f"  capabilities  {len(caps)} declared" + (f": {', '.join(caps)}" if caps else
                                                   " — it can answer, but may not DO anything"),
        f"  your number   {'set' if cur.get('phone') else 'not set — no callback can be offered'}",
        f"  model         {llm.describe() if ok else 'NOT ATTACHED — the agent cannot answer'}",
        f"  connector     {'configured' if connector.configured() else 'NOT SET — nobody outside can reach it'}",
    ]
    missing = []
    if not ok:
        missing.append("a model key")
    if not connector.configured():
        missing.append("a B3 connector")
    if missing:
        lines += ["", f"  {' and '.join(missing)} must be set at a TERMINAL, not here — a",
                  "  credential typed into chat is sent to the model provider and stored in",
                  "  plaintext. Tell the owner to run:  agentduet-desktop init"]
    if not cur["name"]:
        lines += ["", "  No name yet. Ask them, then set it with set_setting — you can do the",
                  "  whole interview here; only the two credentials need a terminal."]
    return "\n".join(lines)
def set_setting(field: str, value: str) -> str:
    """Set one owner setting: name, pronoun, voice or never_say. Not knowledge — never quoted."""
    key = field.strip().lower().replace(" ", "_").replace("-", "_")
    heading = SETTING_FIELDS.get(key)
    if not heading:
        return f"Unknown setting {field!r}. One of: {', '.join(sorted(SETTING_FIELDS))}."
    path = paths.SETTINGS
    text = path.read_text() if path.is_file() else "# Settings\n"
    body = value.strip()
    if key == "never_say":
        # A list, one topic per line — stored as bullets so owner.never_say() reads it back.
        items = [l.strip("-• ").strip() for l in body.splitlines() if l.strip()]
        body = "\n".join(f"- {i}" for i in items)
    block = f"## {heading}\n{body}\n"
    pattern = re.compile(rf"^## {re.escape(heading)}\b.*?(?=^## |\Z)", re.S | re.M)
    text = pattern.sub(block + "\n", text, count=1) if pattern.search(text) \
        else text.rstrip() + f"\n\n{block}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    # WRITTEN FOR A PERSON. It said "Set Recordings in settings.md (not knowledge — never
    # quoted to anyone):" — the storage file, an internal distinction, and a trailing colon,
    # shown verbatim in the settings page. A person-facing message reads fine to a model; the
    # reverse does not, so this direction is the one that cannot leak. The disclosure fact the
    # parenthetical carried lives in this function's docstring, where the model reads it.
    shown = body.strip().splitlines()[0][:120] if body.strip() else ""
    return f"{heading} saved — {shown}." if shown else f"{heading} cleared."
def add_knowledge(fact: str, file: str = "", section: str = "") -> str:
    """Teach the secretary a fact. Name the file that already owns the subject."""
    fact = fact.strip()
    if not fact:
        return "Nothing to add."
    st = _capabilities()
    clash, note = st._bound_conflict(fact) if st else ("", "")
    if clash:
        return f"NOT saved. {clash}"
    target, why = _knowledge_target(file)
    if target is None:
        return f"NOT saved. {why}"
    stale = _write_time_check(fact, target)
    if stale:
        return f"NOT saved. {stale}"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = target.read_text() if target.exists() else f"# {target.stem.replace('-', ' ').title()}\n"
    # WHERE the bullet lands. A structured document's last section is not a safe default — a
    # calzone price once landed under "## Not covered here", a heading that says the opposite of
    # what the bullet claims. So: append under the named section when one is given, and fall back
    # to a section of our own rather than to whatever happens to be last.
    #
    # `section` exists because without it the caller could not comply with the instruction to put
    # a fact under the heading that owns its subject: every fact went to "## Added since",
    # including the ones belonging in the "## Who" and "## Availability" headings sitting empty
    # above it.
    want = (section or "").strip().lstrip("#").strip()
    placed = False
    if want:
        pat = re.compile(rf"^##\s+{re.escape(want)}\s*$(.*?)(?=^##\s|\Z)", re.S | re.M)
        m = pat.search(text)
        if m:
            body = m.group(1).rstrip()
            text = text[:m.start(1)] + f"{body}\n- {fact}\n\n" + text[m.end(1):]
            placed = True
        else:
            text = text.rstrip() + f"\n\n## {want}\n\n- {fact}\n"
            placed = True
    if not placed:
        SECTION = "## Added since"
        if "\n## " in text and SECTION not in text:
            text = text.rstrip() + f"\n\n{SECTION}\n\nFacts the owner added later, newest last.\n"
        text = text.rstrip() + f"\n- {fact}\n"
    target.write_text(text)
    where = target.resolve().relative_to(paths.KNOWLEDGE.resolve().parent).as_posix()
    return (f"Added to {where}\n  - {fact}\nReadable by: {_readers_of(target)}"
            + (f"\n{note}" if note else ""))
def who_is(asker: str) -> str:
    """Show what the secretary knows about a person."""
    if not people.exists(asker):
        return (f"No profile for {asker}. Create one with add_person so the secretary "
                f"adapts its tone and access for them.")
    secs = people.sections(asker)
    out = [f"{asker}"]
    for name in ("Who", "Comms", "Folders", "Always escalate", "Observed"):
        if secs.get(name):
            out.append(f"\n{name}:\n{secs[name]}")
    return "\n".join(out)
def list_people() -> str:
    """Everyone who has been in touch, and whether the secretary has a profile for them.

    It used to list PROFILE FILES only. That answers a question nobody asks: an owner saying
    "who has contacted me?" wants the two people who called, not the subset someone happened to
    write notes about — and with no profiles yet, the honest answer looked like "none". The
    site's people list was already derived from traffic, so the two faces disagreed about who
    existed.
    """
    seen = {row["asker"] for row in rows() if row.get("asker")}
    profiled = set(people.list_profiles())
    if not seen and not profiled:
        return "Nobody has been in touch yet."
    lines = []
    for who in sorted(seen | profiled):
        mark = "" if who in profiled else "   (no profile yet)"
        lines.append(f"- {who}{mark}")
    return f"{len(lines)} in touch:\n" + "\n".join(lines)
def add_person(asker: str, who: str = "", comms: str = "") -> str:
    """Start a profile for a VERIFIED person — who they are and how to write to them."""
    return people.create(asker, who, comms)
def note_person(asker: str, section: str, note: str) -> str:
    """Add a curated note. section: Who | Comms | Folders | Always escalate.

    'Folders' grants a readable folder to this person; 'Always escalate' adds a topic
    the secretary must never answer for them.
    """
    valid = {"Who", "Comms", "Folders", "Always escalate"}
    if section not in valid:
        return f"section must be one of: {', '.join(sorted(valid))}"
    return people.add_note(asker, section, note)
def profile_suggestions() -> str:
    """Proposed profile updates from the query log. Code never writes these itself —
    accept one with accept_observation."""
    sug = people.suggest_observations(rows())
    if not sug:
        return "Nothing to suggest yet."
    out = []
    for who, tips in sug.items():
        out.append(f"{who}:")
        out += [f"  - {t}" for t in tips]
    return "\n".join(out) + "\n\nAccept one with accept_observation(asker, note)."
def accept_observation(asker: str, note: str) -> str:
    """Record an observation into a person's profile."""
    return people.accept_observation(asker, note)
def model_status() -> str:
    """Which model is attached and whether it actually works right now."""
    ok, why = llm.verify()
    return ("OK   " if ok else "FAIL ") + why
def attach_model(key: str, model: str = "", provider: str = "") -> str:
    """Attach a model by API key: verify it works, then save it to this instance.

    Removes the need for any external CLI — the framework owns the credential step, which
    is what makes `init` possible on a machine with nothing installed but AgentDuet Desktop.

    Verify BEFORE writing. A credential that is saved and broken produces the worst failure
    this agent has: it starts, connects, and silently escalates every single message,
    because "no working model" and "nothing to answer" look the same from outside.

    The key is written to $AGENTDUET_HOME/.env at 0600 and is never echoed, logged, or returned
    — the confirmation reports its length and last four characters only.
    """
    key = (key or "").strip()
    m = (model or "").strip() or os.getenv("SECRETARY_MODEL") or ""
    if not m:
        return "Say which model to attach (e.g. claude-sonnet-5, gemini-3.6-flash)."

    # A LOCAL MODEL HAS NO KEY, so "give the API key" is the wrong question to ask about one.
    # What stands in for verification is the same thing that matters for a hosted key: prove it
    # answers before saving, because a model that is configured and broken makes every message
    # escalate for a reason nobody can see.
    # CLEAR THE STICKY OVERRIDE FIRST, before anything asks which provider this is.
    # SECRETARY_PROVIDER is an explicit choice that beats inference, and picking a local model
    # sets it — so a hosted key attached afterwards was still routed to Ollama and rejected for
    # not being a pulled model. It has to go before the question is asked, not after: an
    # override outlives the decision that set it, which is the whole hazard of having one.
    # The `provider` argument survives because it is a parameter, not the environment.
    os.environ.pop("SECRETARY_PROVIDER", None)

    # EXPLICIT BEATS INFERENCE. Provider is guessed from the model name, and the guess is wrong
    # for exactly the case this feature adds: "qwen2.5:3b" running locally matches DashScope's
    # "qwen" prefix and gets sent to the cloud with no key. The caller who picked a row from the
    # local list knows which provider it is; let it say so.
    if (provider or "").lower() == "local" or llm.provider(m) == "local":
        from . import models
        ok, why = models.available()
        if not ok:
            return why
        if not models.is_downloaded(m):
            return f"{m} is not downloaded yet."
        os.environ["SECRETARY_MODEL"], os.environ["SECRETARY_PROVIDER"] = m, "local"
        _write_env({"SECRETARY_MODEL": m, "SECRETARY_PROVIDER": "local"})
        return (f"Attached {models.CATALOGUE.get(m, {}).get('name', m)}, running on this "
                "machine. Transcripts stay here — nothing is sent to a provider.")

    var = llm.key_name(m)

    # A KEY WE ALREADY HOLD NEEDS NO RETYPING. Switching from Claude back to Gemini used to
    # demand the Gemini key again, because this refused an empty one — while the key was
    # sitting in .env the whole time, unmentioned. An empty key now means "use the stored
    # one", and only a provider with nothing stored is asked for it.
    #
    # "" is a real credential for Anthropic (a CLI profile), so the test is `is None`, not
    # falsiness. Verification still runs either way: an attach that skips it is how an
    # instance reports itself configured while holding a stale key.
    if not key:
        if llm._IMPLS[llm.provider(m)].credential() is None:
            return "Give the API key to attach."
        key = os.getenv(var) or ""

    before, before_model = os.environ.get(var), os.environ.get("SECRETARY_MODEL")
    os.environ[var], os.environ["SECRETARY_MODEL"] = key, m
    llm.forget()                       # drop any client cached under the old credential
    ok, why = llm.verify(m)
    if not ok:
        # Put the environment back: a failed attach must change nothing. SECRETARY_MODEL was
        # being left behind, so a rejected key produced an instance that reported itself
        # configured while holding no working credential — the precise state this function
        # exists to prevent, reached through its own error path.
        if before is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = before
        if before_model is None:
            os.environ.pop("SECRETARY_MODEL", None)
        else:
            os.environ["SECRETARY_MODEL"] = before_model
        llm.forget()
        return f"NOT saved — {why}"

    # Only write a key we were GIVEN. Re-writing a stored one is a no-op, and writing a blank
    # one over an Anthropic CLI login would replace a working credential with an empty string.
    _write_env(({var: key} if key else {}) | {"SECRETARY_MODEL": m,
                                              "SECRETARY_PROVIDER": llm.provider(m)})
    if not key:
        return f"Attached {m}. {why}"
    # The last four characters confirm WHICH key without echoing it. The file it went to and
    # its mode are ours to get right, not facts the owner acts on.
    return f"Attached {m}. {why}\n  Key ending {key[-4:]} saved."
def save_connector(api_key: str, connector_uuid: str) -> str:
    """Write the B3 connector credential to this instance. Verify FIRST (see connector.verify).

    Deliberately not in OWNER_TOOLS: handing a secret to the assistant means typing it into a
    chat box, which sends it to the model provider and writes it to run/owner_chat.json in
    plaintext. Credentials are entered on a page.
    """
    api_key, connector_uuid = api_key.strip(), connector_uuid.strip()
    if not api_key or not connector_uuid:
        return "Give both the API key and the connector uuid."
    _write_env({connector.API_KEY: api_key, connector.UUID: connector_uuid})
    # Visible to this process immediately — and the channel loop polls the environment, so it
    # picks this up within seconds without a restart.
    os.environ[connector.API_KEY] = api_key
    os.environ[connector.UUID] = connector_uuid
    return (f"Saved. Key ending {api_key[-4:]}, connector {connector_uuid}.\n"
            "The channel picks this up within a few seconds — no restart needed.")
def _write_env(values: dict) -> None:
    """Upsert keys in the instance .env, preserving everything else and the file mode."""
    path = paths.ENV_FILE
    lines = path.read_text().splitlines() if path.is_file() else []
    for var, val in values.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == var:
                lines[i] = f"{var}={val}"
                break
        else:
            lines.append(f"{var}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def _forget_env(names: list[str]) -> None:
    """Drop these keys from the instance .env, and from this process.

    The counterpart to _write_env. Setting a credential to "" would leave a line that reads
    like a configured-but-empty key, which is the state `credential()` cannot tell from a
    typo — so the line goes rather than being blanked.
    """
    path = paths.ENV_FILE
    if path.is_file():
        keep = [l for l in path.read_text().splitlines()
                if l.split("=", 1)[0].strip() not in names]
        path.write_text("\n".join(keep) + ("\n" if keep else ""))
        path.chmod(0o600)
    for n in names:
        os.environ.pop(n, None)


def list_calls(days: str = "7") -> str:
    """Calls that were carried and recorded — who, when, and whether a transcript exists."""
    from . import calls as _calls, carry
    from datetime import datetime, timedelta
    try:
        cut = datetime.now() - timedelta(days=max(1, int(str(days) or 7)))
    except ValueError:
        cut = datetime.now() - timedelta(days=7)
    folder = carry.recordings()
    out = []
    for r in _calls.recent():
        at = r.get("at", "")
        try:
            if datetime.fromisoformat(at) < cut:
                continue
        except ValueError:
            pass
        names = [n for n in r.get("recordings", []) if (folder / n).is_file()]
        done = any((folder / n).with_suffix(".txt").is_file() for n in names)
        out.append(f"- {at}  {r.get('caller') or '?'}  "
                   f"({'transcript ready' if done else 'no transcript yet'})")
    return "\n".join(out) if out else f"No calls recorded in the last {days} days."
#: MARK TEXT A STRANGER WROTE, wherever it is about to reach a model.
#:
#: Two paths need this and they are not the same shape. On the secretary side an asker types at
#: the agent directly. On the RECORDER side nobody types at anything — a caller simply talks, and
#: `read_call` hands the transcription to the owner's assistant, which holds `add_knowledge`.
#: That is the same class of exposure arriving through a channel with no signup, no account and
#: no way to refuse the input: anyone who can dial a number can author text that a tool-using
#: model will read. Marking is not a defence on its own; it is what lets everything downstream
#: tell content from instruction.
#:
#: The delimiter is STRIPPED from the content first. Otherwise the author closes it themselves and
#: continues outside the quote — the oldest escape in the book, and the reason naive quoting fails.
UNTRUSTED_MARK = "⟦asker-said⟧"
def untrusted(text: str) -> str:
    """Mark text a STRANGER wrote. See UNTRUSTED_MARK."""
    if not text:
        return ""
    return f"{UNTRUSTED_MARK} {str(text).replace(UNTRUSTED_MARK, '')} {UNTRUSTED_MARK}"
def read_call(who: str = "", when: str = "") -> str:
    """The transcript of a recorded call. `who` is the caller; `when` narrows to one date."""
    from . import calls as _calls, carry
    folder = carry.recordings()
    hits = []
    for r in _calls.recent():
        if who and who.strip().lower() not in (r.get("caller") or "").lower():
            continue
        if when and not (r.get("at") or "").startswith(when.strip()):
            continue
        for n in r.get("recordings", []):
            t = (folder / n).with_suffix(".txt")
            if t.is_file():
                try:
                    # The header is OURS — the timestamp and the caller come from call metadata, not
                    # from anything said. Only the body is the stranger's, so only the body is marked;
                    # marking the header too would let a transcript forge a plausible one.
                    hits.append(f"--- {r.get('at','')} with {r.get('caller') or '?'} ---\n"
                                + untrusted(t.read_text()[:4000]))
                except OSError:
                    pass
                break
        if len(hits) >= 5:
            break
    if not hits:
        return ("No transcript matches that. Transcription runs after a call, on a queue, so a "
                "recent call may not have one yet.")
    return "\n\n".join(hits)
def read_messages(who: str = "", limit: int = 20) -> str:
    """The message conversation with someone — DDUET or WhatsApp, oldest first.

    Marked as untrusted for the same reason `read_call` is, and it is the same risk wearing
    different clothes: the words were written by whoever is on the other end, and they reach a
    model that holds knowledge writes. Anyone who can message a public business slug can put
    text in front of this assistant.
    """
    rows_ = [r for r in rows()
             if r.get("network") in ("WA", "DDUET")
             and (not who or who.strip().lower() in (r.get("asker") or "").lower())]
    if not rows_:
        return ("No messages with them. Messages are carried to the owner and not answered, so "
                "a thread exists only once someone has written.")
    out = []
    for r in rows_[-max(1, limit):]:
        when = (r.get("at") or "")[:16].replace("T", " ")
        if r.get("outcome") == "owner_reply":
            out.append(f"[{when}] the owner replied: {r.get('answer', '')}")
            continue
        out.append(f"[{when}] them: {untrusted(r.get('question', ''))}")
        if r.get("answer"):
            out.append(f"[{when}] the agent answered: {r.get('answer')}")
        else:
            out.append("           (not answered — waiting for the owner)")
    return "\n".join(out)


#: What the owner's Personal Assistant may call. Two registries rather than one so the recorder's
#: own tools stay out of the stdio mcp's surface — the assistant needing a tool is not a reason
#: to widen what an external assistant can call.
#:
#: THESE WERE A NAMED SUBSET OF `OWNER_TOOLS` until the split, which made the recorder's surface
#: a derivative of the secretary's. That is the wrong direction, and it is why the assistant
#: could reach `pending_escalations` at all. Declared here now, on its own terms.
#:
#: `attach_model` and `save_connector` are deliberately absent: using them means pasting a
#: credential into a chat box, which sends it to the model provider and writes it to
#: run/owner_chat.json in plaintext. Keys are entered on the settings page.
def note_about(who: str, note: str) -> str:
    """Remember something about a person, attributed to them.

    This is the assistant's own accumulation, and it is deliberately NOT `note_person`. That one
    also writes 'Folders' (which GRANTS a readable folder) and 'Always escalate' — authority,
    reachable from a tool that reads transcripts. This writes observations only.

    Attribution is what makes autonomous writing safe here. "Pauline said the policy is 90 days"
    stays true whoever said it, so a hostile sentence recorded this way is an accurate note
    rather than a poisoned fact. Stripping the attribution — promoting a claim into the agent's
    own voice — is `add_knowledge`, and that is the step the owner has to approve.
    """
    note = (note or "").strip()
    if not note:
        return "Nothing to note."
    if not (who or "").strip():
        return "Say who this is about — a note with no one attached is a claim, not an observation."
    return people.add_note(who.strip(), "Who", note)
ASSISTANT_SHARED = {
    "list_people": (list_people, {}),
    "who_is": (who_is, {"asker": "their email or number"}),
    "list_knowledge": (list_knowledge, {}),
    "read_knowledge": (read_knowledge, {"file": "e.g. pizza-delivery.md"}),
    "add_knowledge": (add_knowledge, {
        "fact": "the fact to remember, phrased the way someone would ASK about it",
        "file": "destination from list_knowledge — the file that already owns the subject",
        "section": "the '## ' heading to put it under. Use one the document already has; "
                   "a new one is created if it does not"}),
    "edit_knowledge": (edit_knowledge, {
        "file": "the document to correct, e.g. about.md",
        "old": "the exact existing text to replace — must appear exactly once",
        "new": "its replacement; leave empty to delete the text"}),
    "note_about": (note_about, {
        "who": "the person the note is about — their name, email or number",
        "note": "what to remember about them, in your own words"}),
    "setup_status": (setup_status, {}),
    "model_status": (model_status, {}),
}

RECORDER_TOOLS = {
    "list_calls": (list_calls, {"days": "how many days back (default 7)"}),
    "read_messages": (read_messages, {
        "who": "the person, or empty for everyone",
        "limit": "how many of the most recent lines (default 20)"}),
    "read_call": (read_call, {"who": "the caller, or empty for any",
                              "when": "a date like 2026-08-26, or empty for the most recent"}),
}
#: View preferences. NOT settings.md: that file is parsed by heading and holds what the AGENT
#: is (name, pronoun, never-say) — a knowledge edit that renamed a heading once silently emptied
#: the never-say list, so it is not a place to put unrelated keys. This is derived instance
#: state, which is what run/ is for.
#:
#: Server-side rather than localStorage because the owner site is rendered by THREE engines now
#: (browser, WebKitGTK in the pywebview window, WebKit in the macOS .app) and the window has no
#: localStorage at all — referencing it there raises ReferenceError.
UI_PREFS = paths.RUN / "ui.json"
def ui_prefs() -> dict:
    try:
        return json.loads(UI_PREFS.read_text())
    except (OSError, ValueError):
        return {}
def set_ui_pref(key: str, value) -> str:
    prefs = ui_prefs()
    prefs[key] = value
    try:
        UI_PREFS.parent.mkdir(parents=True, exist_ok=True)
        UI_PREFS.write_text(json.dumps(prefs, indent=2))
    except OSError as exc:
        return f"could not save: {exc}"
    return f"{key} = {value}"
