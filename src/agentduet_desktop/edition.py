"""Which product this binary is: the whole of AgentDuet, or the RECORDER alone (2026-10-03).

WHY TWO. A telco partner is launching the recorder under its own name, and its security review
treats anything that is AI — transcription included — as a separate product it does not endorse.
So that build must CONTAIN no AI: not switched off, absent. A reviewer checks it by listing what
is in the bundle, and a flag that keeps the code in the binary answers the wrong question. (The
quarantine flags elsewhere in this package keep code in on purpose; this is the opposite tool.)

ONE CODEBASE, NOT A FORK. The recorder is the core of the full product as well, so every fix to
carrying, recording, playback, contacts or sign-in lands once. The full build is the core plus the
modules in `AI_MODULES`; the recorder build leaves those out (`packaging/agentduet-desktop.spec`).

THE RULE THAT KEEPS IT TRUE: no core module imports an AI module except behind `ai()`.
`tests/test_recorder.py` runs the daemon from a copy of this package with every AI module deleted,
which is exactly what the recorder binary is, so a stray import fails there rather than on a
tester's machine.
"""
from __future__ import annotations

import os

FULL, RECORDER = "full", "recorder"
#: AGENTDUET AI (2026-10-03): the AI half alone, beside AgentDuet Recorder. It has no phone line
#: and takes no calls; it reads the recordings the recorder leaves in the AgentDuet folder, and
#: nothing else, and writes their transcripts into each call's `.txt` (see `ingest.py`).
AI_ONLY = "ai"

#: Everything that is AI, or exists only to serve it. The recorder build excludes each of these,
#: and the core may reach one only behind `ai()`. Kept here, in the core, because the build and
#: the test both read it.
AI_MODULES = (
    # speech, summaries, suggestions and the owner's assistant, and their half of the site
    "transcribe", "live", "brief", "suggest", "assistant", "decider", "recall", "tools",
    "web_ai", "ingest", "slot", "search", "litert", "vocab",
    # the console interview, which hands the owner's answers to the model
    "init",
    # the local model and what sizes, times and schedules it
    "llm", "models", "machine", "speed", "budget", "gate", "jobs", "prompts",
    # the secretary: an agent that answers, discloses and acts
    "brain", "secretary_tools", "secretary_mcp", "voice", "canvas", "capabilities", "policy",
    "wasm_host", "toolstore", "asker_actions", "memory", "people", "permissions",
    "folder_index", "schedule", "identity", "digest", "notify", "run_sim", "hosts",
)

#: Python libraries that are AI runtimes or exist only for one. The recorder build excludes them.
AI_LIBRARIES = (
    "llama_cpp", "onnxruntime", "tokenizers", "pywhispercpp", "faster_whisper", "ctranslate2",
    "huggingface_hub", "google.genai", "anthropic", "openai", "agentduet_adapters", "mcp",
    "wasmtime", "litert_lm",
)


#: Package data the recorder build leaves out, as globs under the package. The prompts, the
#: example capability and the tool sandbox are the secretary's; the HTML pages are the frozen
#: owner site, whose hub holds the assistant (a Mac shows native windows, not these); and
#: `templates/` seeds an agent's instance — the recorder seeds `templates-recorder/` instead.
AI_DATA = ("prompts/**/*", "examples/**/*", "wasm/**/*", "templates/**/*", "*.html",
           # the full Terms and Privacy Policy, which name the AI models; the recorder ships its own
           # (legal/recorder/, legal.py). NAMED, not "legal/*.md": the spec matches with fnmatch,
           # whose * crosses "/", so a glob would strip the recorder's own texts too.
           "legal/terms.md", "legal/privacy.md")


def _built() -> str:
    try:
        from ._edition import NAME            # type: ignore  # written by the recorder build
        return NAME
    except ImportError:
        return FULL


def name() -> str:
    """The edition. The build decides; AGENTDUET_EDITION=recorder (or ai) runs one from source."""
    if _built() in (RECORDER, AI_ONLY):
        return _built()                         # a single-edition binary cannot be talked out of it
    asked = os.getenv("AGENTDUET_EDITION", "").strip().lower()
    return asked if asked in (RECORDER, AI_ONLY) else FULL


def ai() -> bool:
    """Whether the AI half is part of this product."""
    return name() in (FULL, AI_ONLY)


def calls() -> bool:
    """Whether this product has a phone line: it takes and records calls. Not AgentDuet AI."""
    return name() in (FULL, RECORDER)


def home_name() -> str:
    """The instance folder under the home directory. Each edition keeps its own, so two of them
    on one Mac never share one (the shell resolves the same names, Daemon.swift)."""
    return {RECORDER: ".agentduet-recorder", AI_ONLY: ".agentduet-ai"}.get(name(), ".agentduet-desktop")


def port() -> int:
    """The owner site's default port; 0 means any free one, which macOS picks.

    AGENTDUET AI TAKES ANY (2026-10-03): nothing needs its number. The shell finds the daemon by
    the address it writes (`run/site-url`), and a fixed port exists only for sign-in, whose
    callback the AgentDuet server checks — and this app never signs in. So it can never clash
    with the recorder, or with anything else. The two apps with a line keep 8899 until the
    server is confirmed to accept any loopback port.
    """
    return 0 if name() == AI_ONLY else 8899
