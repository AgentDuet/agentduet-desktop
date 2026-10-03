"""The owner's settings, credentials and view preferences — written by code, never by a model.

Split out of `tools.py` on 2026-10-03 so the RECORDER edition can keep them: `tools.py` is the
assistant's tool registry and is not part of that build (see `edition.py`). `tools` re-exports
every name here, so its callers did not change.
"""

import json
import os
import pathlib
import re

from . import connector
from . import owner
from . import paths


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
                  # In carry mode, whether an inbound call rings in the app before it passes
                  # through. Off unless an explicit yes — see owner.answer_here().
                  "answer_here": "Answer here",
                  "transcription": "Transcription",
                  # Where the audio goes. An absolute path; anything else falls back to the
                  # default rather than raising — see owner.recordings_dir().
                  "recordings": "Recordings",
                  # Whether a reasoning model monologues first. Only meaningful on some models
                  # (llm.supports_thinking), and off unless the value is an explicit yes —
                  # measured at ~100x slower on a question it answers correctly without.
                  "thinking": "Thinking",
                  # Whether the machine launches this app at login. Off unless an explicit yes —
                  # see owner.start_at_login() for why the useful default is not the safe one.
                  "start_at_login": "Start at login"}
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
