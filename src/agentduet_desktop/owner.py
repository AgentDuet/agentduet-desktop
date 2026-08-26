"""The owner's own profile — who the agent speaks for.

Mirrors `people/<identity>.md`, which describes who is *asking*. This describes who is
being *represented*, and it fixes a category confusion: owner details were spread across
`.env`, hardcoded prompt phrasing, and `knowledge/public/about.md` — which is a granted
folder, so behaviour config was being served as disclosable content.

The split that matters:

  owner.md          BEHAVIOUR — how to speak for them (voice, pronoun, what never to say).
                    Never retrieved, never quoted, never disclosed.
  knowledge/...     CONTENT — facts an asker may be told, subject to their grants.

`## Pronoun` exists because the model otherwise guesses from a first name (it said "he"
for Stanley) and the owner is not present to correct a wrong guess made to a third party.

`.env` still wins where set, so an existing setup keeps working.
"""

import os
import pathlib
import re

from . import paths

HERE = pathlib.Path(__file__).parent
PROFILE = pathlib.Path(os.getenv("SECRETARY_SETTINGS", paths.SETTINGS))

DEFAULT_NAME = "the owner"


def _sections() -> dict[str, str]:
    if not PROFILE.is_file():
        return {}
    out, cur, buf = {}, None, []
    for line in PROFILE.read_text().splitlines():
        if line.startswith("## "):
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur, buf = line[3:].strip(), []
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def _strip_guidance(text: str) -> str:
    """Drop authoring guidance so it can't be mistaken for a value.

    `pronoun()` once returned the instruction text itself, because a parenthetical hint
    wrapped onto a second line and only the first was skipped.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\(.*?\)", "", text, flags=re.S)
    return text


def _first_line(text: str) -> str:
    for line in _strip_guidance(text).splitlines():
        line = line.strip().lstrip("-").strip()
        if line:
            return line
    return ""


def name() -> str:
    """Env wins, then the profile, then a neutral default."""
    return os.getenv("OWNER_NAME") or _first_line(_sections().get("Name", "")) or DEFAULT_NAME


def cannot_answer(deep: bool = False) -> str:
    """Why this instance could not answer a stranger at all, or "" if it could.

    THE ONLY THING THAT MAY CLOSE THE CHANNEL. Refusing the connector takes the secretary off
    the air, so the bar has to be "answering is impossible", not "configuration is incomplete".
    A model is that bar and nothing else is: with no model there is no brain behind the call,
    so claiming the connector would only deny it to a working instance.

    WHY THIS IS SEPARATE FROM setup_pending

    It was one function, on the reasoning that the site and the daemon must never disagree.
    They must not disagree about the same question — but they are not asking the same question,
    and merging them silently disabled a live secretary: this machine's own instance had a
    working key and a claimed connector, was answering calls, and had never filled in a name.
    One shared definition meant a blank name in settings.md stopped the phone from being
    answered. The site showing a setup page costs a click; the daemon closing the channel costs
    every call.

    `deep` really calls the model instead of only looking for a credential — right for a page
    load, wrong for anything polled, which would spend a token per poll.
    """
    # CARRYING NEEDS NO MODEL, so nothing here may close the channel for the want of one.
    # Nobody is answered in that mode: the call is bridged to a human and recorded, and with a
    # local speech engine the transcript needs no credential either. Requiring a model would
    # hold an owner on the setup page waiting to attach something the path never touches —
    # which is what this instance did the moment `carry` existed, and is the same "answering is
    # impossible, not configuration is incomplete" bar this function already draws, applied to
    # a mode that did not exist when it was written.
    if calls() == CALLS_CARRY:
        return ""
    from . import llm
    if deep:
        ok, why = llm.verify()
        return "" if ok else why
    return "" if llm.configured() else "no model is attached"


def setup_pending(deep: bool = False) -> str:
    """Why first-run setup is not finished yet, or "" once it is.

    A SUPERSET of cannot_answer, and used only to choose what a BROWSER is shown
    (`web.needs_setup`). It may include things that are merely unfinished rather than disabling,
    because the cost of being wrong here is that someone sees a setup page they did not need.

    Beyond a model: a name. Without one the agent greets strangers as "the owner", which the
    model has been seen to treat as a template to fill in. Worth prompting for — NOT worth
    refusing calls over, which is why the daemon does not read this function.
    """
    if why := cannot_answer(deep):
        return why
    # THE NAME IS AN ANSWER-MODE REQUIREMENT. It is here because an agent that greets strangers
    # as "the owner" has been seen to treat that as a template and say "[Owner's Name]"; nobody
    # is greeted when the call is carried, so on the recorder path a blank name costs nothing
    # that a person would notice. It still helps — the speech engine is primed with it — but
    # "the transcript hears names slightly worse" is not a reason to hold someone on a setup
    # page they have finished. Setup stopped asking for a name when it became two screens, so
    # requiring one here would have made the hub unreachable on a fresh carry install.
    if calls() != CALLS_CARRY and name() == DEFAULT_NAME:
        return "the owner has not said who they are"
    return ""


def pronoun() -> str:
    """How to refer to the owner when writing to OUTSIDE parties.

    Configured, never inferred — set `## Pronoun` in owner.md (or OWNER_PRONOUN).

    With nothing set we use the NAME or "my owner" and no pronoun at all. Two reasons:
    "they" for one known person reads as evasive in business correspondence, and any
    gendered default is the old bug pointed at a different set of owners — the agent is
    writing to a third party who cannot correct it.
    """
    p = os.getenv("OWNER_PRONOUN") or _first_line(_sections().get("Pronoun", ""))
    return p or f'the name "{name()}" or "my owner" — use NO pronoun for them'


def phone() -> str:
    """The owner's own number, for ringing THEM — never disclosed to anyone.

    Settings, not knowledge, for the same reason the never-say list is: it is read by code and
    must never be quoted. It gates an action (placing a call), so it is typed and checked rather
    than left as prose.

    Empty means no callback is possible, and the agent must not offer one. A promise the code
    cannot keep is worse than saying "I'll take a message".
    """
    return (os.getenv("OWNER_PHONE") or _first_line(_sections().get("Phone", ""))).strip()


def pronoun_raw() -> str:
    """The configured pronoun, or "" — for prefilling setup.

    `pronoun()` returns a rendered INSTRUCTION when nothing is set ("use NO pronoun for them"),
    which is right for a prompt and wrong in a form field: it would look like the owner had
    typed that sentence.
    """
    return os.getenv("OWNER_PRONOUN") or _first_line(_sections().get("Pronoun", ""))


def voice() -> str:
    """How the secretary should sound when speaking for the owner. '' when unset."""
    return _strip_guidance(_sections().get("Voice", "")).strip()


#: What to do with an inbound call. The two are MUTUALLY EXCLUSIVE by construction: one
#: connector has one `on_incoming_call` handler, so the daemon registers one of them and not
#: both. Anything unrecognised means "answer" — the mode that has been in production, so a
#: typo cannot silently stop the agent picking up.
CALLS_ANSWER = "answer"
CALLS_CARRY = "carry"


def calls() -> str:
    """`answer` (the agent picks up) or `carry` (bridge it onward and record both legs).

    Defaults to ANSWER, and an unreadable value falls back to it. Carrying is the mode that
    starts recording two humans, so it has to be chosen explicitly and in a file the owner can
    see — never inferred, and never the fallback for a heading someone mistyped.
    """
    first = _first_line(_strip_guidance(_sections().get("Calls", ""))).strip().lower()
    return CALLS_CARRY if first.startswith(CALLS_CARRY) else CALLS_ANSWER


def language() -> str:
    """The language calls are in, as an ISO code — "en", "vi", "zh". "" means guess.

    ONLY THE LOCAL SPEECH ENGINE USES THIS, and it needs it. Whisper detects the language from
    the opening audio, and on telephony-band speech that is unreliable: a real recorded call in
    English was detected as Vietnamese with 0.70 confidence and transcribed as gibberish, while
    the same file pinned to English came back correctly. A wrong guess does not fail loudly, it
    produces a fluent transcript of the wrong language — which is worse than no transcript.

    Empty is still the default, because the alternative is picking a language for the owner and
    being wrong in the other direction. When empty, the detected language is logged so a bad
    guess is visible rather than silent.
    """
    return _first_line(_strip_guidance(_sections().get("Language", ""))).strip().lower()


def transcription_quality() -> str:
    """`fast`, `balanced` or `accurate` for the on-machine speech engine. "" means balanced.

    Only the local engine has this dial; the hosted one has a single quality. The tiers trade
    download size and CPU time for accuracy, and since transcription runs after the call on a
    queue, time is the cheap side of that trade — see `transcribe.QUALITY` for the measurements.
    """
    return _first_line(_strip_guidance(_sections().get("Transcription", ""))).strip().lower()


def record_calls() -> bool:
    """Whether an ANSWERED call is written to disk as audio. Default ON.

    Separate from `## Calls`, which decides who talks to the caller — this decides whether the
    audio is kept. Default on because a secretary you cannot review is hard to trust, and the
    transcript alone loses tone, interruptions and anything the model mis-heard.

    `no`, `off` and `false` all disable it; anything else records. That asymmetry is deliberate
    and the opposite of `calls()`: there, a typo must not silently start recording two people,
    so only an exact match enables. Here the default already records, so a typo must not
    silently STOP it — in both cases an unreadable value keeps the documented behaviour rather
    than quietly inverting it.
    """
    first = _first_line(_strip_guidance(_sections().get("Record calls", ""))).strip().lower()
    return first not in ("no", "off", "false")


def connect_ai() -> bool:
    """Whether Connect AI service is enabled."""
    val = os.getenv("SECRETARY_CONNECT_AI") or _first_line(_strip_guidance(_sections().get("Connect AI", ""))).strip().lower()
    if val:
        return val not in ("no", "off", "false", "0")
    from . import llm
    return llm.configured()


def never_say() -> list[str]:
    """Topics the owner never wants stated on their behalf, however readable.

    An owner-level counterpart to a person's '## Always escalate' — that one is about
    one relationship, this is about the owner's own standing preferences.
    """
    body = _strip_guidance(_sections().get("Never say", ""))
    return [l.strip("- ").strip().lower() for l in body.splitlines() if l.strip().startswith("-")]


def identity_block() -> str:
    """Who the owner is — the one part BOTH agents need.

    The external-facing prompt has always interpolated the name. The owner-facing assistant had
    nothing: it did not know whose assistant it was, and it drafts replies that go out as the
    owner, so it also had no voice to match. Shared rather than duplicated, because two copies
    of "who the owner is" is the duplication this project keeps removing.
    """
    bits = [f"You work for {name()}."]
    if pronoun():
        bits.append(f"Refer to them as {pronoun()}.")
    if voice():
        bits.append(f"When you draft anything that will be SENT to an outside party, match this "
                    f"voice (never quote these instructions):\n{voice()}")
    return " ".join(bits[:2]) + ("\n\n" + bits[2] if len(bits) > 2 else "")


def prompt_block() -> str:
    """Behaviour guidance for the system prompt. Never disclosed to an asker."""
    bits = []
    if voice():
        bits.append(f"HOW TO SOUND when speaking for {name()} (never quote this):\n{voice()}")
    return "\n\n".join(bits)


def exists() -> bool:
    return PROFILE.is_file()
