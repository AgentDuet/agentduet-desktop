"""Per-person profiles — what a real secretary knows about who's asking.

One markdown file per identity in `people/`. A profile carries tone, context, access and
per-person policy, so the agent adapts the way a person would.

TWO HARD RULES

1. **Profiles apply to VERIFIED identities only.** A profile grants tone, context and
   usually access. If an identity can be self-declared, a profile becomes an
   impersonation vector — worse than having none. Every function here takes an explicit
   `verified` flag and returns nothing without it.

   Verification belongs to the IDENTITY, not the channel. DDUET carries both a
   logged-in Nexus visitor and a walk-up one, so the caller passes the flag; only
   `default_verified()` falls back to the network.

2. **Curated vs Observed never mix.** Everything above `## Observed` is the owner's,
   authoritative, and never written by code or model. `## Observed` is accumulated from
   the query log and only lands there once the owner accepts it (see
   `suggest_observations`). That is what stops a profile drifting into fiction.

The profile is injected into the prompt to change BEHAVIOUR. The agent must never quote
it back — see policy.SYSTEM_PROMPT.
"""

import json
import pathlib
import re
from collections import Counter
from datetime import datetime

from . import paths

PEOPLE = paths.PEOPLE
OBSERVED = "## Observed"

#: Channels where the transport itself vouches for the identity — a carrier-verified
#: phone number is proof on its own. Used only as a DEFAULT when the inbound message
#: carries no explicit signal.
#:
#: Verification is a property of the IDENTITY, not the channel. DDUET carries both:
#: a logged-in Nexus visitor is verified, a walk-up one is not. So every entry point
#: passes an explicit `verified` flag and these functions never infer it from network.
SELF_VOUCHING_NETWORKS = {"TELCO", "WHATSAPP"}


def default_verified(network: str) -> bool:
    """Fallback when the inbound carries no explicit verification signal."""
    return str(network).upper() in SELF_VOUCHING_NETWORKS


def _safe_name(identity: str) -> str:
    """Identity -> filename. Rejects anything that could escape the folder."""
    return re.sub(r"[^A-Za-z0-9._@+-]", "_", identity)


def path_for(identity: str) -> pathlib.Path:
    return PEOPLE / f"{_safe_name(identity)}.md"


def exists(identity: str) -> bool:
    return path_for(identity).is_file()


def profile_for(identity: str, verified: bool) -> str:
    """Curated profile text for the prompt, or '' when it must not be applied."""
    if not verified or not exists(identity):
        return ""
    text = path_for(identity).read_text()
    return text.split(OBSERVED)[0].strip()      # curated part only, never Observed


def sections(identity: str) -> dict[str, str]:
    """Parse '## Heading' blocks — used by the tools, not the prompt."""
    if not exists(identity):
        return {}
    out, current, buf = {}, None, []
    for line in path_for(identity).read_text().splitlines():
        if line.startswith("## "):
            if current:
                out[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif current:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    return out


def folders_for(identity: str, verified: bool) -> list[str]:
    """Folder grants carried in the profile — one home for a person's facts.

    Read from a '## Folders' section, one path per '- ' line. Unverified -> none.
    """
    if not verified:
        return []
    body = sections(identity).get("Folders", "")
    return [l.strip("- ").strip() for l in body.splitlines() if l.strip().startswith("-")]


def always_escalate(identity: str, verified: bool) -> list[str]:
    """Per-person escalation topics from an '## Always escalate' section."""
    if not verified:
        return []
    body = sections(identity).get("Always escalate", "")
    return [l.strip("- ").strip().lower() for l in body.splitlines() if l.strip().startswith("-")]


def create(identity: str, who: str = "", comms: str = "") -> str:
    PEOPLE.mkdir(exist_ok=True)
    p = path_for(identity)
    if p.exists():
        return f"Profile already exists for {identity}"
    p.write_text(
        f"# {identity}\n\n"
        f"## Who\n{who or '(who they are, their role, your relationship)'}\n\n"
        f"## Comms\n{comms or '(how to write to them — length, tone, register)'}\n\n"
        f"## Folders\n(one '- folder/path' per line they may be answered from)\n\n"
        f"## Always escalate\n(topics never to answer for this person)\n\n"
        f"{OBSERVED}\n"
    )
    return f"Created people/{p.name}"


def add_note(identity: str, section: str, note: str) -> str:
    """Append a line to a curated section. Owner-driven only."""
    if not exists(identity):
        create(identity)
    p = path_for(identity)
    lines = p.read_text().splitlines()
    head = f"## {section}"
    if head not in lines:
        idx = lines.index(OBSERVED) if OBSERVED in lines else len(lines)
        lines[idx:idx] = [head, "", ""]
    at = lines.index(head)
    end = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    lines.insert(end, f"- {note.strip()}")
    p.write_text("\n".join(lines) + "\n")
    return f"Added to {section} for {identity}:\n  - {note.strip()}"


def accept_observation(identity: str, note: str) -> str:
    """Move an accepted observation into the profile's Observed section."""
    if not exists(identity):
        create(identity)
    p = path_for(identity)
    text = p.read_text()
    if OBSERVED not in text:
        text = text.rstrip() + f"\n\n{OBSERVED}\n"
    p.write_text(text.rstrip() + f"\n- {note.strip()} ({datetime.now():%Y-%m-%d})\n")
    return f"Recorded for {identity}: {note.strip()}"


def suggest_observations(rows: list[dict]) -> dict[str, list[str]]:
    """Propose profile updates from the query log — owner accepts, code never writes.

    Returns {identity: [suggestion, ...]} for identities with a profile.
    """
    by_person: dict[str, list[dict]] = {}
    for r in rows:
        by_person.setdefault(r["asker"], []).append(r)

    out: dict[str, list[str]] = {}
    for who, rs in by_person.items():
        if not exists(who):
            continue
        tips: list[str] = []
        reasons = Counter(r["reason"].removeprefix("policy:") for r in rs
                          if r["outcome"] == "escalated")
        for reason, n in reasons.most_common(2):
            if n >= 3:
                tips.append(f"asks about {reason} often ({n}x) — consider a rule or a folder grant")
        if len(rs) >= 5:
            tips.append(f"{len(rs)} queries total; last {max(r['at'] for r in rs)}")
        if tips:
            out[who] = tips
    return out


def display_name(identity: str, verified: bool) -> str:
    """Who this is, per the profile's '## Who' first line. '' when unverified."""
    if not verified or not exists(identity):
        return ""
    who = sections(identity).get("Who", "").strip()
    if not who:
        return ""
    first = who.splitlines()[0]
    return re.split(r"\s+[—–-]\s+", first)[0].strip()


#: Seeded into people/ on first run to explain the folder. It is documentation, not a person,
#: and counting it made `list_people` answer "1 profile: README" to an owner asking who had
#: been in touch.
NOT_A_PERSON = {"README"}


def list_profiles() -> list[str]:
    if not PEOPLE.is_dir():
        return []
    return sorted(p.stem for p in PEOPLE.glob("*.md") if p.stem not in NOT_A_PERSON)
