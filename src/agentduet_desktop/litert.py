"""Gemma 4 E-models in Google's LiteRT-LM, not llama.cpp (2026-10-09).

WHY. Measured on the M5 with the same prompt of five real calls, Gemma 4 E4B warm: 41 tok/s against
llama.cpp's 20, first token 1.7 s against 3.0, and a fraction of the memory — llama.cpp maps the
whole 4.9 GB file onto the GPU (5.7 GB resident), while LiteRT keeps the per-layer embedding
tables on disk and reads the rows each token needs. Google's own card says ~3.2 GB of GPU memory
on an M4 Max. On an 8 GB Mac that is the difference between speech and Gemma fitting together or
not. Answers were of the same quality. It is the file Google AI Edge Foresight runs.

WHAT IT CHANGES, and why each piece is shaped as it is:
  - NO REUSE OF A PROMPT'S START across conversations. llama.cpp re-read only a prompt's changed
    tail; here a new conversation reads everything (3,243 tokens: 5.2 s), while a message added
    to a live one reads only itself (0.3 s). So the assistant's conversation is KEPT (`Chat`) and
    fed only what is new, and rebuilt only when what came before changed.
  - NO SAVE/RESTORE of state (`clone()` is a TODO in the package). Not needed: a background job
    gets a conversation of its own and closes it, and the assistant's is never touched.
  - THE PICKER reads LiteRT's scores, one option per fresh session — the package scores one target
    at a time, and a score moves the session on. The scores are far less sure than llama.cpp's
    next-token probabilities (Thai 0.62 where llama.cpp said 0.997) though they rank the same,
    so a caller's bar is its own (`PICK_SURE`), not llama.cpp's.

An AI module (edition.AI_MODULES). `litert_lm` is an AI library the recorder never contains.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading

from . import paths

logger = logging.getLogger("secretary")

#: The longest conversation, in tokens: the assistant's ~3,000-token start and its chat fit.
CONTEXT = 8192


def available() -> bool:
    return importlib.util.find_spec("litert_lm") is not None


def cache_dir():
    """Where LiteRT keeps the GPU form of the weights it builds on first load (~2 GB for E4B)."""
    d = paths.HOME / "models" / "litert-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(path):
    """An engine for one model file. The first load on a machine builds its GPU cache (~5 s)."""
    import litert_lm as lm
    backend = lm.Backend.GPU() if sys.platform == "darwin" else lm.Backend.CPU()
    lm.set_min_log_severity(lm.LogSeverity.ERROR)
    # FLOAT32 ACTIVATIONS, never the GPU's float16 default (2026-10-09). With float16, once the
    # prompt passed ~3,000 tokens the model could no longer copy a phone number: the assistant
    # listed "+659835262" for +6598352362 from a tool result in front of it, 0 of 7 right, while
    # llama.cpp on the same messages got 7 of 7. It was LENGTH, not the tools — 2,270 tokens of
    # filler did the same. float32: 7 of 7, the same first-token time, 4% slower to write.
    return lm.Engine(str(path), backend=backend, max_num_tokens=CONTEXT, cache_dir=str(cache_dir()),
                     activation_data_type=lm.ActivationDataType.FLOAT32)


def _sampler(think: bool):
    import litert_lm as lm
    # GREEDY unless thinking, as on llama.cpp (llm.TEMPERATURE): a summary wants determinism.
    return lm.SamplerConfig(temperature=0.6, top_p=0.95, top_k=20) if think else \
        lm.SamplerConfig(temperature=0.0, top_k=1)


def _penalty():
    import litert_lm as lm
    # llama.cpp's 1.1, for the same reason (llm._generate): with greedy decoding nothing else
    # discourages a loop. OVER THE LAST 64 TOKENS, as llama.cpp's own default: LiteRT's default
    # window is the whole conversation, prompt included, and penalising every token the prompt
    # holds pushes a model off the very numbers and names it is meant to repeat — on 2026-10-09 it
    # left an invoice out of an answer that the 64-token window kept.
    return lm.RepetitionPenaltyConfig(repetition_penalty=1.1, window_size=64)


def _text(msg) -> str:
    """The words in one streamed chunk or message."""
    c = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type", "text") == "text")
    return c or ""


def _calls(msg) -> list[dict]:
    """The tool calls in one message, as {"name", "arguments"}."""
    out = []
    raw = msg.get("tool_calls") if isinstance(msg, dict) else None
    for c in raw or []:
        f = c.get("function", c)
        out.append({"name": f.get("name", ""), "arguments": f.get("arguments") or {}})
    content = msg.get("content") if isinstance(msg, dict) else None
    for x in content if isinstance(content, list) else []:
        if isinstance(x, dict) and x.get("type") == "tool_call":
            f = x.get("function", x)
            out.append({"name": f.get("name", ""), "arguments": f.get("arguments") or {}})
    return out


def generate(engine, messages: list[dict], *, think: bool = False, max_tokens: int = 2048,
             cancel: threading.Event | None = None) -> str:
    """One answer in a conversation of its own, closed afterwards. `cancel` stops it between chunks.

    `messages` are chat messages; all but the last seed the conversation, the last is sent.
    """
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    conv = engine.create_conversation(messages=_to_litert(rest[:-1]) or None,
                                      system_message=system or None,
                                      sampler_config=_sampler(think), max_output_tokens=max_tokens)
    try:
        out = []
        for chunk in conv.send_message_async(_to_litert(rest[-1:])[0],
                                             repetition_penalty_config=_penalty()):
            if cancel is not None and cancel.is_set():
                conv.cancel_process()
                raise _Cancelled()
            out.append(_text(chunk))
        return "".join(out)
    finally:
        conv.close()


class _Cancelled(Exception):
    pass


# ---- the assistant's conversation, kept between calls ---------------------------------------

def _to_litert(messages: list[dict]) -> list[dict]:
    """OpenAI-shaped messages (what assistant.py builds) as LiteRT's."""
    out = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "content": [{"type": "tool_response", "name": m.get("name", ""),
                                                     "response": m.get("content", "")}]})
        elif m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": [{"type": "function", "function": {
                            "name": c["function"]["name"],
                            "arguments": c["function"].get("arguments") or {}}}
                            for c in m["tool_calls"]]})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _key(m: dict) -> tuple:
    """What makes two messages THE SAME for reuse — not always their whole text, on purpose.

    - A message the caller gave a `key` is matched by it. The assistant keys its system message
      by its fixed instructions (the memory under them is re-folded after every turn) and a
      question by the question (the inbox and screen context joined to it is rebuilt every
      turn). The conversation keeps what it read then; matching by the whole text would re-read
      everything, cold, on every question (3,243 tokens: 5.2 s).
    - A tool call by its NAME: the assistant rebuilds earlier turns' calls without their
      arguments, so comparing arguments would read the whole conversation again every turn.
      (Where code runs a call with other arguments than the model wrote, it says so in the
      result instead — assistant._keep_period.)
    - The model's own reply by its role: the conversation holds what it wrote; a note the
      assistant appended for the owner is not worth a cold re-read.
    """
    if "key" in m:
        return (m["role"], m["key"])
    if m.get("tool_calls"):
        return ("call",) + tuple(c["function"]["name"] for c in m["tool_calls"])
    if m["role"] == "assistant":
        return ("assistant",)
    return (m["role"], m.get("content") or "")


class Chat:
    """The assistant's one live conversation. `ask` sends only what is new since the last call."""

    def __init__(self):
        self._conv = None
        self._fed: list[tuple] = []            # keys of every message the conversation holds
        self._tools_key = None
        self._engine = None

    def holds(self, engine) -> bool:
        return self._engine is engine

    def close(self) -> None:
        if self._conv is not None:
            try:
                self._conv.close()
            except Exception:
                pass
        self._conv, self._fed, self._engine = None, [], None

    def _fresh(self, engine, system_msg: dict | None, seed: list[dict], tools: list[dict]) -> None:
        self.close()
        lm_tools = [_Schema(t) for t in tools or []]
        self._conv = engine.create_conversation(
            messages=_to_litert(seed) or None,
            system_message=(system_msg or {}).get("content") or None,
            tools=lm_tools or None, automatic_tool_calling=False,
            sampler_config=_sampler(False), max_output_tokens=2048)
        self._fed = ([_key(system_msg)] if system_msg else []) + [_key(m) for m in seed]
        self._tools_key = json.dumps(tools or [], sort_keys=True)
        self._engine = engine

    def ask(self, engine, messages: list[dict], tools: list[dict],
            cancel: threading.Event | None = None) -> dict:
        """The model's next message: {"text", "calls"}.

        REUSED when `messages` are exactly what the conversation holds plus ONE new message — a
        new question, or a tool's result. Anything else starts it again from `messages`: history
        trimmed, a conversation broken off, or several new messages at once, which cannot be
        added without the model answering each.
        """
        system = next((m for m in messages if m["role"] == "system"), None)
        rest = [m for m in messages if m["role"] != "system"]
        keys = ([_key(system)] if system else []) + [_key(m) for m in rest]
        if not (self._conv is not None and engine is self._engine
                and self._tools_key == json.dumps(tools or [], sort_keys=True)
                and keys[:-1] == self._fed):
            if self._conv is not None:
                logger.info("assistant conversation read again from the start (%d messages)",
                            len(messages))
            self._fresh(engine, system, rest[:-1], tools)
        text, calls = [], []
        try:
            for chunk in self._conv.send_message_async(_to_litert(rest[-1:])[0],
                                                       repetition_penalty_config=_penalty()):
                if cancel is not None and cancel.is_set():
                    self._conv.cancel_process()
                    self.close()
                    raise _Cancelled()
                text.append(_text(chunk))
                calls += _calls(chunk)
        except _Cancelled:
            raise
        except Exception:
            self.close()                       # a half-fed conversation is not reused
            raise
        self._fed.append(keys[-1])
        reply = {"role": "assistant", "content": "".join(text)}
        if calls:
            reply["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                   for c in calls]
        self._fed.append(_key(reply))
        return {"text": "".join(text), "calls": calls}


#: THE assistant's conversation. One: there is one owner and one chat.
CHAT = Chat()


def _schema_class():
    import litert_lm as lm

    class Schema(lm.interfaces.Tool):
        """One of our OpenAI-shaped tool schemas, declared to LiteRT. Never executed by it:
        automatic tool calling is off, so the assistant's own loop — and its gates — runs every
        call (assistant.NEEDS_OWNER)."""

        def __init__(self, schema: dict):
            self._schema = schema

        def get_tool_description(self) -> dict:
            return self._schema

        def execute(self, param):
            raise RuntimeError("tools run in the assistant's loop, never inside LiteRT")

    return Schema


def _Schema(schema: dict):
    return _schema_class()(schema)


# ---- the picker -------------------------------------------------------------------------------

def pick(engine, prompt: str, names: list[str]) -> str:
    """One of `names`, or "" when unsure. NOT a probability, unlike llama.cpp's (llm._Local.pick).

    LiteRT gives no usable confidence (2026-10-09): `run_text_scoring` scores one option per fresh
    session — ten reads of the transcript for ten languages — and is far less sure than llama.cpp
    (Thai 0.62 where it said 0.997); a decoded token's score is always 0. Its greedy ANSWER is good:
    six languages out of six, Tamil and Spanish included. So it is asked TWICE, the options
    reversed, and only an answer that survives the reordering counts. Deny by default: a model
    guessing does not usually guess the same language from two different numberings.
    """
    def once(order: list[str]) -> str:
        lines = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(order))
        user = f"{prompt.strip()}\n\n{lines}\n\nAnswer with the option number only."
        s = engine.create_session(apply_prompt_template=False, max_output_tokens=2,
                                  sampler_config=_sampler(False))
        try:
            s.run_prefill([f"<|turn>user\n{user}<turn|>\n<|turn>model\n"])
            got = "".join(s.run_decode().texts).strip()
        finally:
            s.close()
        return order[int(got[0]) - 1] if got[:1].isdigit() and 0 < int(got[0]) <= len(order) else ""

    first = once(list(names))
    if not first:
        return ""
    return first if once(list(reversed(names))) == first else ""
