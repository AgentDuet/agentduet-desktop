"""Prompts that gate behaviour, kept as reviewable files rather than assembled in code.

WHY THIS EXISTS

On voice the prompt IS the disclosure control. Speech-to-speech gives nothing a chance to
inspect a sentence before it is spoken, so the instruction handed to the realtime model at
session open is the only thing between a caller and an ungrounded answer. A control deserves to
be a versioned artifact you can diff and review — not a string concatenated inline, which is
what it was, and which broke twice in one afternoon.

WHAT THIS CATCHES, AND WHAT IT DOES NOT

Be honest about the limit. `.format()` already raises on a missing key, so presence checking is
not the win. The defects that actually shipped were:

  - a MISSING CONCEPT: SYSTEM_PROMPT had no pronoun slot at all, so a configured "she/her" owner
    was called "they". No parameter checker finds a parameter nobody declared. Only a behaviour
    test does, and that is still open on the checklist.
  - a PLAUSIBLE BUT WRONG VALUE: owner_name was "the owner" — non-empty, valid, and the model
    rendered it as a template, answering calls as "[Owner's Name]'s assistant".

So this checks VALUES, not just presence: blank, bracketed, or still-unrendered text is refused
at render time. That is the class of bug that reached a real caller.
"""
from __future__ import annotations


import pathlib
import re

HERE = pathlib.Path(__file__).parent
DIR = HERE / "prompts"

#: A value that is almost certainly a placeholder rather than a real one. `[Owner's Name]` is
#: the literal string a caller heard; "TODO"/"TBD" are the same mistake typed by a human.
_PLACEHOLDER = re.compile(r"\[[^\]]+\]|^\s*(TODO|TBD|FIXME|xxx+)\s*$", re.I)

#: Any `{name}` left after rendering — a parameter the template wanted and nobody passed.
_UNRENDERED = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.I)


class PromptError(RuntimeError):
    """Raised at render time. Deliberately fatal: a prompt with a hole in it is not a prompt
    that should reach a stranger."""


def _split_front_matter(text: str) -> tuple[dict, str]:
    """`--- key: value --- body`. Hand-parsed because a YAML dependency for four keys is not
    worth adding to a binary that has to stay small."""
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---")
    head, sep, body = rest.partition("\n---")
    if not sep:
        return {}, text
    meta: dict = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, body.lstrip("\n")


def load(name: str) -> tuple[dict, str]:
    path = DIR / f"{name}.md"
    if not path.is_file():
        raise PromptError(f"no prompt template {name!r} at {path}")
    return _split_front_matter(path.read_text(encoding="utf-8"))


def render(name: str, **values) -> str:
    """Render a template, or raise.

    Optional parameters are supported by declaring them in `optional:`; an optional value that
    is absent renders the line it sits on away entirely, which is how the pronoun sentence
    disappears for an owner who has not set one — rather than emitting "Refer to X as ."
    """
    meta, body = load(name)
    required = meta.get("params", [])
    optional = meta.get("optional", [])

    missing = [p for p in required if not str(values.get(p, "")).strip()]
    if missing:
        raise PromptError(
            f"prompt {name!r} needs {', '.join(missing)} and got nothing usable. "
            f"An empty required value is how a caller ends up hearing a placeholder.")

    for key, value in values.items():
        if key in required and _PLACEHOLDER.search(str(value)):
            raise PromptError(
                f"prompt {name!r}: {key}={value!r} looks like a placeholder, not a real value.")

    # Optional lines vanish when their value is absent, so nothing renders half-written.
    out_lines = []
    for line in body.splitlines():
        refs = set(_UNRENDERED.findall(line))
        if refs & set(optional) and not all(str(values.get(r, "")).strip() for r in refs & set(optional)):
            continue
        out_lines.append(line)
    body = "\n".join(out_lines)

    try:
        text = body.format(**values)
    except KeyError as exc:
        raise PromptError(f"prompt {name!r} refers to {exc} which was not supplied") from None

    left = _UNRENDERED.findall(text)
    if left:
        raise PromptError(f"prompt {name!r} still contains {left} after rendering")
    return text.strip()


def check_all() -> list[str]:
    """Every template parses and declares its parameters. Called by the test suite, so a
    malformed prompt fails offline rather than on a call."""
    problems = []
    for path in sorted(DIR.glob("*.md")):
        meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
        declared = set(meta.get("params", [])) | set(meta.get("optional", []))
        used = set(_UNRENDERED.findall(body))
        if not meta.get("params") and not meta.get("optional") and used:
            problems.append(f"{path.name}: uses {sorted(used)} but declares no params")
        for undeclared in sorted(used - declared):
            problems.append(f"{path.name}: uses {{{undeclared}}} but does not declare it")
        for unused in sorted(declared - used):
            problems.append(f"{path.name}: declares {unused!r} but never uses it")
    return problems
