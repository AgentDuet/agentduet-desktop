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

#: Everything that is AI, or exists only to serve it. The recorder build excludes each of these,
#: and the core may reach one only behind `ai()`. Kept here, in the core, because the build and
#: the test both read it.
AI_MODULES = (
    # speech, summaries, suggestions and the owner's assistant, and their half of the site
    "transcribe", "live", "brief", "suggest", "assistant", "decider", "recall", "tools",
    "web_ai",
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
    "wasmtime",
)


#: Package data the recorder build leaves out, as globs under the package. The prompts, the
#: example capability and the tool sandbox are the secretary's; the HTML pages are the frozen
#: owner site, whose hub holds the assistant (a Mac shows native windows, not these); and
#: `templates/` seeds an agent's instance — the recorder seeds `templates-recorder/` instead.
AI_DATA = ("prompts/**/*", "examples/**/*", "wasm/**/*", "templates/**/*", "*.html")


def _built() -> str:
    try:
        from ._edition import NAME            # type: ignore  # written by the recorder build
        return NAME
    except ImportError:
        return FULL


def name() -> str:
    """The edition. The build decides; AGENTDUET_EDITION=recorder runs the recorder from source."""
    if _built() == RECORDER:
        return RECORDER                         # a recorder binary cannot be talked out of it
    return RECORDER if os.getenv("AGENTDUET_EDITION", "").strip().lower() == RECORDER else FULL


def ai() -> bool:
    """Whether the AI half is part of this product."""
    return name() == FULL
