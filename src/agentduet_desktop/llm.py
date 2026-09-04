"""The model seam — one place that knows which provider is attached.

AgentDuet Desktop is distributed, and the user attaches their own model. That only works if
the agent's decision code never names a vendor. Before this module, `brain.py` imported
`google.genai` and called `client.models.generate_content` at eight separate call sites,
so "attach Sonnet" meant editing the decision core.

Everything above this line asks for text and gets text back. Provider choice is config.

WHAT DIFFERS BETWEEN PROVIDERS, and why it is handled here rather than pushed upward:

- **Determinism.** The Gemini path pins `temperature=0`, added deliberately after a gate
  flipped between identical runs. Claude Sonnet 5 and Opus 5 REJECT a non-default
  `temperature` with a 400, so that lever does not exist there — see TEMPERATURE below.
- **`max_tokens`.** Optional on Gemini, required on Anthropic. It also caps thinking plus
  visible text together on the Claude 5 family, so a small value truncates the answer
  rather than the reasoning.
- **Response shape.** Gemini hands back `.text`; Anthropic returns a list of blocks, only
  some of which are text. Reading `content[0].text` blind breaks on a thinking block or a
  refusal, so this module walks the blocks.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import re
import pathlib

from . import paths

# This module reads the credentials, so it loads the file they live in. brain used to be
# the only loader, which meant importing tools without brain (the MCP server, a script,
# a test) reported "no credential" on a perfectly configured machine — the same silent
# degradation that once made owner replies close no threads.
#
# Tolerant of python-dotenv being absent: the model-free suites are meant to run on a bare
# interpreter with no venv, and importing this module must not be what stops them. Already
# exported variables still work; only the .env file is skipped.
try:
    from dotenv import load_dotenv
    load_dotenv(paths.ENV_FILE)
except ImportError:                     # pragma: no cover - depends on the environment
    logging.getLogger("secretary.llm").debug("python-dotenv absent; using os.environ only")

logger = logging.getLogger("secretary.llm")

#: Which provider owns a model name. LOCAL IS DECIDED BY THE CATALOGUE, not by a prefix: the
#: ids in models.CATALOGUE are exact, so there is nothing to guess. `qwen` remains a prefix for
#: DashScope because it means two different things — a hosted qwen and a local one — and a local
#: qwen is matched by its full catalogue id (`qwen3-8b`), never by the bare word.
_PROVIDERS = {"gemini": ("gemini",), "anthropic": ("claude",), "dashscope": ("qwen",)}

#: Where an unrecognised name is routed, and whose first catalogue entry is the
#: fallback model. Named once so the two cannot drift apart.
DEFAULT_PROVIDER = "gemini"

#: Gemini honours it; the Claude 5 family rejects any non-default value with a 400, so the
#: Anthropic path cannot send it. Consequence worth knowing rather than hiding: borderline
#: escalate-or-answer decisions are repeatable on Gemini and only mostly repeatable on
#: Claude. The gate's *rules* are still code, so this affects wording and edge calls, not
#: whether an action can slip past the action gate.
TEMPERATURE = float(os.getenv("SECRETARY_TEMPERATURE", "0"))

#: Anthropic requires it. Generous because thinking is on by default on the Claude 5
#: family and shares this budget with the reply.
MAX_TOKENS = int(os.getenv("SECRETARY_MAX_TOKENS", "8192"))


def provider(model: str = "") -> str:
    """Which provider serves `model`. Explicit env wins, then the catalogue, then the name.

    THE CATALOGUE IS CHECKED BEFORE THE PREFIXES, and that order is load-bearing. `qwen3-8b`
    is a local model whose name contains `qwen`, which is DashScope's prefix — inferring from
    the name first sent a downloaded model to a cloud vendor. An exact id beats a substring.
    """
    named = (os.getenv("SECRETARY_PROVIDER") or "").strip().lower()
    if named in _IMPLS:
        return named
    name = (model or os.getenv("SECRETARY_MODEL") or "").lower()
    from . import models
    if name in models.CATALOGUE:
        return "local"
    for prov, prefixes in _PROVIDERS.items():
        if any(p in name for p in prefixes):
            return prov
    return DEFAULT_PROVIDER


class _Gemini:
    KEY = "GEMINI_API_KEY"

    @classmethod
    def credential(cls) -> str | None:
        """The API key, or None. Gemini has no interactive-login path — key or nothing."""
        return os.getenv(cls.KEY) or None

    def __init__(self, model: str, key: str | None):
        from google import genai
        self.client = genai.Client(api_key=key)
        self.model = model

    def complete(self, prompt: str, think: bool = False) -> str:
        # Gemini flash has no thinking dial here; accepted and ignored so callers stay
        # provider-neutral rather than asking "which provider am I on?".
        resp = self.client.models.generate_content(
            model=self.model,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config={"temperature": TEMPERATURE})
        return (resp.text or "").strip()


class _Local:
    """A model running on THIS machine, from weights we shipped for and downloaded ourselves.

    WHY THIS IS VIABLE HERE WHEN IT WAS NOT FOR VOICE. Speech-to-speech was chosen over a local
    cascade on latency: a CPU model cannot hold a live call. The recorder's model job is
    different — summarising a transcript AFTER the call, on a queue where nothing waits. That is
    the same argument that already makes local speech recognition work, and it is why "nothing
    leaves your machine" can be true end to end rather than true until you attach a key.

    IT WAS OLLAMA UNTIL 2026-08-27. Ollama made model management free and cost the thing the
    product sells: download one binary and it works. `models.py` owns the weights now, so there
    is nothing for the owner to install.

    NO API KEY, so `credential()` reports whether local inference is POSSIBLE — the engine is in
    this build and a model is on disk. Everything else asks "is there a credential" and gets a
    truthful answer about whether this provider can actually serve.
    """

    @classmethod
    def credential(cls) -> str | None:
        from . import models
        if not models.available()[0]:
            return None
        return "on-device" if any(models.is_downloaded(m) for m in models.CATALOGUE) else None

    def __init__(self, model: str, key: str | None):
        self.model = model

    def complete(self, prompt: str, think: bool = False) -> str:
        from . import models
        engine, msg = models.load(self.model)
        if engine is None:
            raise RuntimeError(msg)
        # LOADED ONCE AND KEPT. `models.load` returns the resident engine when it is already the
        # one asked for, so a second summary does not re-read gigabytes from disk. Releasing it
        # is the owner's call, through unload — see the three states in models.py.
        # ASK IT NOT TO THINK, IN A SYSTEM MESSAGE — never by appending to the owner's text.
        #
        # Qwen3 and friends monologue before answering, and for this app that is 7.2x of wall
        # time for nothing an owner sees: on "Hi, are you there?" the 8B wrote 454 tokens of
        # <think> and 41 characters of reply. `/no_think` is Qwen3's own switch and this GGUF's
        # template does NOT strip it, so it arrives as literal text — appended to the question
        # it becomes part of the question. Asked "What's the last 4 digits of 12345678", the
        # model answered "the question is incomplete... this seems like a typo", spent 13
        # seconds on it, and quoted the switch back. A system message cannot do that.
        #
        # DeepSeek-R1 does not honour the switch — it always reasons — so the strip below is
        # what makes those models usable at all, at the cost of generating tokens nobody reads.
        msgs = []
        if models.thinks(self.model) and not think:
            msgs.append({"role": "system", "content": "/no_think"})
        msgs.append({"role": "user", "content": prompt})
        try:
            out = self._generate(engine, msgs)
        except Exception as exc:
            raise RuntimeError(_local_failure(exc, self.model)) from exc
        answer = out["choices"][0]["message"]["content"] or ""
        # A caller that ASKED for reasoning gets to keep it; everyone else gets the answer only.
        # Nobody passes think=True today, which is why the monologue was reaching owner_chat.json.
        return answer.strip() if think else _without_thinking(answer)

    def _generate(self, engine, msgs):
        return engine.create_chat_completion(
            messages=msgs,
            temperature=TEMPERATURE,
            # NOT llama.cpp's default. `create_chat_completion` defaults this to 1.0 — no
            # penalty at all — while llama.cpp's own CLI uses 1.1, so leaving it unset silently
            # buys "repetition is free". With TEMPERATURE at 0 that is near-greedy decoding with
            # nothing discouraging a loop, and a context that visibly repeats is all it takes:
            # glm-4-9b answered "Who called me this week?" with that phrase 339 times, 8,525
            # characters holding 7 distinct 40-character windows, stopping only at max_tokens.
            # 1.1 is llama.cpp's own value; high enough to break a loop, low enough to leave a
            # model free to repeat a name or a date that genuinely recurs.
            repeat_penalty=1.1,
            # Room for a transcript summary. A cap small enough to truncate does not error; it
            # returns a confident half-answer, which is worse.
            max_tokens=2048)


def _local_failure(exc: Exception, model: str) -> str:
    """Turn a llama.cpp failure into something an owner can act on.

    `RuntimeError: llama_decode returned -3` reached the page as a bare HTTP 500 — no message,
    no cause, nothing to do about it. It is almost always memory: a second process holding a
    model is enough on 16 GB, where one 8B is already ~6 GB resident. Seen exactly that way here,
    with the daemon and a terminal each loading one.

    The context being full produces the same class of error, so both are named rather than
    guessing between them.
    """
    text = str(exc)
    if "llama_decode" in text or "llama_batch" in text:
        return (f"{model} could not generate a reply. This is almost always memory — another "
                f"process holding a model is enough, since an 8B needs about 6 GB resident. "
                f"Close anything else running a model, or pick a smaller one in Settings. "
                f"(A full context window gives the same error.) [{text}]")
    return f"{model} failed: {text}"


def _without_thinking(text: str) -> str:
    """Drop a reasoning model's <think> monologue, which is for the model and not the owner.

    It was being stored in owner_chat.json and rendered: 1,816 characters of deliberation about
    a two-word greeting, in front of 41 characters of answer. The same class of bug as showing
    model-facing text anywhere else.

    An UNCLOSED block means the model was still thinking when max_tokens ran out. The tag is
    removed and the text kept, because a truncated thought is at least something; returning ""
    would turn a slow answer into a silent one.
    """
    without = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return without.replace("<think>", "").replace("</think>", "").strip()


class _Anthropic:
    KEY = "ANTHROPIC_API_KEY"

    #: Where `ant auth login` stores an OAuth profile. Presence of a credentials file is
    #: the signal that a login exists; the SDK does the actual resolution.
    PROFILES = pathlib.Path(
        os.getenv("ANTHROPIC_CONFIG_DIR", pathlib.Path.home() / ".config" / "anthropic")
    ) / "credentials"

    @classmethod
    def credential(cls) -> str | None:
        """An explicit key if set, else `""` meaning "let the SDK resolve it", else None.

        Anthropic supports an interactive OAuth login (`ant auth login`) whose profile the
        SDK reads on its own from a zero-argument constructor. Requiring ANTHROPIC_API_KEY
        would therefore report "no model attached" on a machine that is perfectly well
        authenticated — which for this agent means silently escalating everything.

        The empty string is deliberately distinct from None: None is "no credential of any
        kind", "" is "credentialed, but not by us".
        """
        key = os.getenv(cls.KEY)
        if key:
            return key
        if os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_PROFILE"):
            return ""
        try:
            if cls.PROFILES.is_dir() and any(cls.PROFILES.glob("*.json")):
                return ""
        except OSError:
            pass
        return None

    def __init__(self, model: str, key: str | None):
        import anthropic
        # Zero-arg when we hold no key of our own, so the SDK walks its own credential
        # chain (env token → active/named OAuth profile → workload identity).
        self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.model = model

    def complete(self, prompt: str, think: bool = False) -> str:
        """Text only, and only from text blocks.

        `think` is accepted and ignored: adaptive thinking is ON by default on the Claude 5
        family, so this path already thinks. Passing `{"type": "disabled"}` to turn it OFF
        would be the risky direction — on Opus 5 that can emit a tool call as plain text or
        leak `<thinking>` tags into the response.

        No `temperature`: the Claude 5 family 400s on a non-default value. A refusal
        (`stop_reason == "refusal"`) returns "" rather than raising — every caller here
        already treats "" as "could not tell" and falls back to escalating, which is the
        behaviour we want when the model declines.
        """
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}])
        if resp.stop_reason == "refusal":
            logger.warning("model declined the request (%s)",
                           getattr(resp.stop_details, "category", "no category"))
            return ""
        parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        return "\n".join(parts).strip()


class _DashScope:
    """Qwen on Alibaba's DashScope, through its OpenAI-compatible endpoint.

    Uses plain HTTP over httpx rather than adding the `openai` SDK — one fewer dependency
    for a distributed app, and the compatible endpoint is a single POST.

    Region and env-var names follow the convention already established by
    `wss-sdk-python/examples/qwen_integration.py`, so one credential serves both projects.

    MODEL CHOICE: the hosted line is `qwen3.7-max` > `qwen3.7-plus` > `qwen3.6-flash`
    (Alibaba's own ordering, flash being "most cost-effective"). `qwen3.6-flash` is the
    default pick — this agent's calls are classification, extraction and short grounded
    answers, which a flash tier handles, and the prompts are input-heavy so cheap input
    pricing is what matters. Step up to `qwen3.7-plus` if the judgement calls degrade;
    that is a config change, not a code one.

    Do NOT assume these are the same artifacts as the `qwen3:8b` published weights that
    run under Ollama. Similar names, different products — the hosted line is not
    documented as open-weight, so it carries no "runs on the user's own machine" promise.
    """

    KEY = "DASHSCOPE_API_KEY"

    #: OPT-IN ONLY. This used to default to ~/.qwen, so a key sitting in the developer's home
    #: silently credentialed every instance on the machine — including throwaway ones created
    #: to test a first run, which reported themselves configured while holding nothing. A
    #: credential belongs to the INSTANCE, in $AGENTDUET_HOME/.env, or the install cannot be
    #: reasoned about and a dev machine stops resembling a new one.
    KEY_FILE = pathlib.Path(os.environ["DASHSCOPE_KEY_FILE"]) \
        if os.getenv("DASHSCOPE_KEY_FILE") else None

    @classmethod
    def credential(cls) -> str | None:
        # The memory note from the voice work applies here too: a DashScope *model-access*
        # key is not the same credential as a coding-plan key. Only the former works.
        key = os.getenv(cls.KEY)
        if key:
            return key
        # KEY_FILE is None unless DASHSCOPE_KEY_FILE is set, and `None.read_text()` raises
        # AttributeError — which the `except OSError` below does NOT catch. So on a machine with
        # neither the variable nor the file, asking whether a DashScope credential exists RAISED
        # instead of answering "no". It stayed hidden because the only callers were on the
        # DashScope path, where a key was present by definition; the first caller that asks the
        # question unconditionally — transcription, deciding whether it can run at all — hit it
        # immediately on a machine with no key.
        if cls.KEY_FILE is None:
            return None
        try:
            # First non-blank line, so both a bare key and a KEY=value line work.
            for line in cls.KEY_FILE.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                return line.split("=", 1)[1].strip() if line.startswith(cls.KEY + "=") else line
        except OSError:
            pass
        return None

    def __init__(self, model: str, key: str | None):
        import httpx
        region = (os.getenv("DASHSCOPE_REGION") or "intl").strip().lower()
        host = "dashscope-intl.aliyuncs.com" if region == "intl" else "dashscope.aliyuncs.com"
        self.url = f"https://{host}/compatible-mode/v1/chat/completions"
        self.key = key
        self.model = model
        self.http = httpx.Client(timeout=120.0)

    def complete(self, prompt: str, think: bool = False) -> str:
        """One completion. `think=True` enables Qwen3 reasoning, which forces streaming.

        DashScope's compatible endpoint only allows thinking on a STREAMED request — a
        non-streaming call with `enable_thinking: true` fails or returns nothing useful.
        That constraint is why thinking was off at first, and why turning it on meant
        implementing SSE here rather than flipping a flag.

        Reasoning arrives in its own `delta.reasoning_content` field, separate from
        `delta.content`, so the answer comes back clean. `_strip_thinking` still runs as
        defence: some builds put a literal `<think>` block in the content instead, and that
        would break `_json` parsing rather than raising.
        """
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "enable_thinking": bool(think),
        }
        if not think:
            return _strip_thinking(self._once(body)).strip()
        body["stream"] = True
        return _strip_thinking(self._streamed(body)).strip()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def _once(self, body: dict) -> str:
        r = self.http.post(self.url, json=body, headers=self._headers())
        if r.status_code != 200:
            # Surface the provider's own message — it distinguishes a bad key from an
            # unknown model name, which are otherwise both "it didn't work".
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        choices = r.json().get("choices") or []
        return (choices[0].get("message", {}).get("content") or "") if choices else ""

    def _streamed(self, body: dict) -> str:
        """Collect an SSE stream, keeping content and discarding reasoning.

        The reasoning is deliberately dropped rather than returned: callers parse this text
        as JSON or send it to an external party, and neither wants the model's working. It is
        logged at debug so it can be inspected when a judgement looks wrong.
        """
        answer, reasoning = [], []
        with self.http.stream("POST", self.url, json=body, headers=self._headers()) as r:
            if r.status_code != 200:
                r.read()
                raise RuntimeError(f"{r.status_code} {r.text[:200]}")
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    delta = ((json.loads(chunk).get("choices") or [{}])[0]).get("delta") or {}
                except json.JSONDecodeError:
                    continue           # keepalive or partial frame; not fatal
                if delta.get("content"):
                    answer.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
        if reasoning:
            logger.debug("reasoning (%d chars) discarded", sum(map(len, reasoning)))
        return "".join(answer)


def _strip_thinking(text: str) -> str:
    """Remove a leading/inline <think>…</think> block. Tolerates an unclosed one."""
    if "<think>" not in text:
        return text
    import re
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return re.sub(r"<think>.*", "", text, flags=re.S)


class _OpenAICompat:
    """Any provider that speaks the OpenAI chat-completions shape over a bearer key.

    A separate small class rather than a refactor of `_DashScope`: that one carries region
    selection, a key file, and an SSE path for Qwen's thinking mode, none of which a plain
    provider needs — and rewriting a working money-path provider to share code with two new
    ones is a poor trade the day before a demo.

    Subclasses set KEY, URL and LABEL. Nothing else differs.
    """

    KEY = ""
    URL = ""
    LABEL = ""

    @classmethod
    def credential(cls) -> str | None:
        return os.getenv(cls.KEY) or None

    def __init__(self, model: str, key: str | None):
        self.model, self.key = model, key or os.getenv(self.KEY, "")

    def complete(self, prompt: str, think: bool = False) -> str:
        import httpx
        r = httpx.post(self.URL,
                       headers={"Authorization": f"Bearer {self.key}",
                                "Content-Type": "application/json"},
                       json={"model": self.model,
                             "messages": [{"role": "user", "content": prompt}],
                             "temperature": TEMPERATURE},
                       timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.LABEL} refused the request ({r.status_code}): "
                               f"{r.text[:200]}")
        return (r.json()["choices"][0]["message"]["content"] or "").strip()


class _XAI(_OpenAICompat):
    """Grok, on xAI's own OpenAI-compatible endpoint."""

    KEY = "XAI_API_KEY"
    URL = "https://api.x.ai/v1/chat/completions"
    LABEL = "xAI"


class _Bedrock(_OpenAICompat):
    """Amazon Nova, through Bedrock's OpenAI-compatible endpoint and a Bedrock API key.

    UNTESTED BY US — we hold no AWS key, so this path has never made a real request. It is
    written from the documented endpoint shape rather than from a run, and the region is
    part of the URL, so a wrong region is a wrong host rather than a wrong parameter.

    That is safer than it sounds because `attach_model` VERIFIES before it saves: a wrong
    endpoint or a key of the wrong kind fails at the moment the owner configures it, with the
    provider's own error, rather than silently at the first call. If it turns out Bedrock
    needs SigV4 rather than a bearer token for this route, that is what the owner will see.
    """

    KEY = "AWS_BEDROCK_API_KEY"
    LABEL = "Amazon Bedrock"

    @property
    def URL(self) -> str:                     # region belongs to the instance, not the class
        region = os.getenv("AWS_REGION") or "us-east-1"
        return f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1/chat/completions"


_IMPLS = {"gemini": _Gemini, "anthropic": _Anthropic, "dashscope": _DashScope,
          "local": _Local, "xai": _XAI, "bedrock": _Bedrock}
_cached: dict[str, object] = {}


def fallback_model() -> str:
    """The model to assume when nothing is configured and a caller needs a name anyway.

    FOUR FUNCTIONS SPELLED THIS OUT AS A LITERAL, and a literal in control flow is a guess
    about someone else's catalogue that nobody will revisit. `gemini-3.1-flash` was that guess
    in `client`, `configured`, `verify` and `summary` — and Google does not serve it, so a
    fresh install with a Gemini key and no explicit choice hit `404 NOT_FOUND` from four
    directions. Now the name lives once, in HOSTED, where `offered()` can also correct it.

    The catalogue's own first entry, because HOSTED lists each provider's models in our
    preferred order — the same order `offered()` keeps at the top of the page.
    """
    picks = HOSTED.get(DEFAULT_PROVIDER, {}).get("models") or []
    return picks[0] if picks else ""


def client(model: str = ""):
    """The live client for `model`, or None when no key is configured.

    Cached per (provider, model). Returning None rather than raising is deliberate: the
    agent must degrade to escalating everything when no model is attached, not crash.
    """
    m = model or os.getenv("SECRETARY_MODEL") or fallback_model()
    prov = provider(m)
    hit = _cached.get(f"{prov}:{m}")
    if hit is not None:
        return hit
    impl = _IMPLS[prov]
    key = impl.credential()
    if key is None:
        return None
    try:
        made = impl(m, key)
    except Exception as exc:               # missing SDK, malformed key
        logger.warning("could not build %s client: %s", prov, exc)
        return None
    _cached[f"{prov}:{m}"] = made
    return made


def key_name(model: str = "") -> str:
    """The env var this model's credential lives in — so callers don't hardcode it."""
    return _IMPLS[provider(model)].KEY


def forget() -> None:
    """Drop cached clients. Needed after a credential changes, or the old one is reused."""
    _cached.clear()


def configured(model: str = "") -> bool:
    """Is a credential present for the configured model? NO network call.

    Distinct from verify(), which really calls the model — right for `init`, which must not save
    a key it has not proven, and wrong for anything asked repeatedly. Status checks run whenever
    an assistant is curious; spending a token and a round-trip on each one is a cost nobody
    agreed to.
    """
    return client(model or os.getenv("SECRETARY_MODEL") or fallback_model()) is not None


def verify(model: str = "") -> tuple[bool, str]:
    """Actually call the model once. Returns (ok, a sentence the owner can act on.)

    Init must not write a credential it has not proven works. A typo'd key is
    indistinguishable at rest from a good one, and the failure mode is silent: the agent
    starts, connects, and escalates every message. Cheaper to spend one token here.

    The message separates the cases that need different actions — a bad key needs a new
    key, a spend cap needs a billing change, and both are otherwise reported as "the model
    isn't working".
    """
    m = model or os.getenv("SECRETARY_MODEL") or fallback_model()
    c = client(m)
    if c is None:
        return False, f"No credential found for {provider(m)}."
    try:
        out = c.complete("Reply with the single word: ok")
    except Exception as exc:
        text = str(exc)
        if "401" in text or "API key not valid" in text or "authentication" in text.lower():
            return False, "The credential was rejected (401) — wrong or revoked."
        if "429" in text or "RESOURCE_EXHAUSTED" in text:
            return False, ("The credential works, but the account is out of quota or over "
                           "its spend cap (429). Nothing to fix in the config.")
        # A MODEL NAME THAT DOES NOT EXIST IS NOT A CREDENTIAL PROBLEM, and it read as one.
        # Our model lists are hardcoded, so a provider renaming or retiring a model turns the
        # whole provider unusable with a raw vendor 404 in the owner's face — "NOT saved: Call
        # failed: 404 NOT_FOUND. models/gemini-3.1-pro is not found for API version v1beta".
        # The key in that case is fine and was proven so by reaching the API at all; only the
        # name is wrong, and the owner cannot tell those apart from what they were shown.
        if "404" in text or "NOT_FOUND" in text or "not found for API version" in text:
            return False, (f"The credential reached {_vendor_of_name(m)}, so "
                           f"the key is good — but they do not offer a model called \"{m}\" on "
                           f"this API. Our model list is built into the app, so it goes stale "
                           f"when a provider renames or retires one. Try another model from the "
                           f"list; if none of them work, the list needs updating.")
        return False, f"Call failed: {text[:160]}"
    if not out:
        return False, "The model returned nothing — it may have declined the request."
    return True, f"Working — {describe(m)}"


#: What the owner calls each provider. `describe()` names the code's provider key, which is
#: right for a log and wrong for a settings page — nobody bought a "dashscope".
_VENDOR = {"gemini": "Google", "anthropic": "Anthropic", "dashscope": "Alibaba",
           "local": "this machine", "xai": "xAI", "bedrock": "Amazon"}


def _vendor_of_name(model: str) -> str:
    """The vendor a model NAME belongs to, ignoring what is currently configured.

    `provider()` deliberately lets `SECRETARY_PROVIDER` win over its own argument, which is
    right for routing a call and wrong for describing a name: asked about `gemini-3.1-pro`
    while a local model is attached, it answers "local", and a message built on that told the
    owner their Google key had reached "this machine".
    """
    name = (model or "").lower()
    for prov, prefixes in _PROVIDERS.items():
        if any(p in name for p in prefixes):
            return _VENDOR.get(prov, prov)
    return "the provider"


def recognised(model: str) -> bool:
    """Is this a name some provider here actually serves?

    `provider()` must always answer, so it defaults to gemini for anything unmatched — which is
    right for routing and wrong for reporting. This is the question routing cannot ask.
    """
    name = (model or "").lower()
    from . import models
    if name in models.CATALOGUE:
        return True
    return any(p in name for prefixes in _PROVIDERS.values() for p in prefixes)


def summary(model: str = "") -> str:
    """One line for the OWNER: which model, and where it runs.

    The twin of describe(), split off because the two audiences want different sentences.
    describe() names the provider key, the credential kind and the client's health — a
    diagnostic. This answers the only question a settings page is asked: what is it set to?
    """
    m = model or os.getenv("SECRETARY_MODEL")
    if not m:
        return "No model attached. Calls are still carried and recorded without one."
    # AN UPGRADE LEAVES A NAME BEHIND. Instances configured before 2026-08-27 hold an Ollama
    # tag like `tulu3:8b`, which no provider serves now — and `provider()` falls through to
    # gemini, so the owner was told their model "has no key yet". It is not a missing key; the
    # model is gone. Say that, because the fix is to choose another one, not to find a key.
    if not recognised(m):
        return (f"{m} is not available in this build — it was managed by Ollama, which is no "
                f"longer required. Choose a model below and it downloads itself.")
    prov = provider(m)
    impl = _IMPLS[prov]
    if impl.credential() is None:
        return f"{m} is chosen, but has no key yet."
    if client(m) is None:
        return f"{m} is chosen, but it would not start. See the log."
    where = "on this machine" if prov == "local" else f"hosted by {_VENDOR.get(prov, prov)}"
    return f"{m}, {where}"


def describe(model: str = "") -> str:
    """For the owner's diagnostics — which provider and model are in use, and how it is
    authenticated. Distinguishing "key" from "signed in" matters: an expired OAuth login
    looks exactly like no model attached (everything escalates), so the owner needs to be
    able to see which one they have."""
    m = model or os.getenv("SECRETARY_MODEL") or fallback_model()
    prov = provider(m)
    impl = _IMPLS[prov]
    cred = impl.credential()
    # `getattr`, not `impl.KEY`: the LOCAL provider has no key to set — its credential is a
    # downloaded model — and reaching for one crashed `status` outright with an AttributeError.
    # The command that exists to report health has to survive reporting on every provider.
    if cred is None:
        how = ("no model downloaded — choose one in Settings" if prov == "local" else
               "no credential — set %s%s" % (getattr(impl, "KEY", "the provider's key"),
                                             ", or run `ant auth login`"
                                             if prov == "anthropic" else ""))
    else:
        how = "on this machine" if prov == "local" else \
            ("api key" if cred else "signed in (OAuth profile / auth token)")
    # Report what WORKS, not just what is configured. A stale ANTHROPIC_PROFILE pointing at
    # a deleted profile looks credentialed and builds nothing — and "no model" means the
    # agent escalates every message, so the owner must not be told it is fine.
    if cred is not None and client(m) is None:
        how += " — but the client failed to build; see the log"
    return f"{prov}/{m} — {how}"


# ---- the hosted providers, as a list rather than a text field -------------------------------
#
# THE SAME THREE STATES AS A LOCAL MODEL, and the middle one was invisible until now. Keys live
# per provider in the instance .env, so an owner who configured Gemini and then switched to
# Claude still HAS their Gemini key — and the page could not say so, made them paste it again to
# switch back, and never offered to remove it. That is `absent / on disk / in use` wearing
# different words:
#
#   no key           -> paste one
#   key stored       -> "use this", no retyping
#   key and selected -> in use, and "forget this key" is the counterpart to Delete
#
# WHAT THIS DELIBERATELY DOES NOT CLAIM: that `models` enumerates what a provider offers. A GGUF
# repository can be listed exactly; a hosted catalogue cannot, not without a key and a different
# API per vendor. These are starting points taken from what this codebase already used, and the
# free-text field beside them is not a fallback — it is the normal way to reach anything newer.
# Listing live models per provider once a key exists IS now done — see `offered()`.
# The lists below became the offline fallback and the preferred ORDER, not the claim.

#: TAG IS THE COMPANY, HEADING IS THE NAME PEOPLE KNOW — the same shape as the local cards,
#: which read META / Llama 3.2 3B. Hosted used to read ANTHROPIC / Anthropic: the API's internal
#: id on the tag and the company printed twice, while the word an owner actually recognises
#: ("Claude") appeared nowhere.
HOSTED = {
    "anthropic": dict(
        brand="ANTHROPIC", family="Claude",
        models=["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"],
        what="Strongest on instruction-following and long transcripts. Also accepts a "
             "`claude` CLI login instead of a key."),
    # THESE TWO NAMES WERE FICTION, and they were also this module's hardcoded default, so a
    # fresh install with a Google key and no explicit choice met `404 NOT_FOUND
    # models/gemini-3.1-flash is not found for API version v1beta` from four call sites at once
    # (2026-09-04). Nothing in the repo but this table ever mentioned `gemini-3.1-*`; where the
    # name came from is unknown. Replaced with the newest Gemini names this codebase can attest
    # to having existed — which is NOT the same as confirming Google still serves them, and
    # cannot be from here, because we hold no Google key.
    #
    # THAT IS WHY `offered()` EXISTS. With a key the list comes from Google and this entry is
    # only the offline fallback; without one, the field beside it is typeable. Both were added
    # the same day, for this.
    "gemini": dict(
        brand="GOOGLE", family="Gemini",
        models=["gemini-2.5-flash", "gemini-2.5-pro"],
        what="Fast and inexpensive. The default this project was built against."),
    "dashscope": dict(
        brand="ALIBABA", family="Qwen",
        models=["qwen3.6-flash", "qwen3.6-plus"],
        what="The same family as the local Qwen models, without the download."),
    "xai": dict(
        brand="XAI", family="Grok",
        models=["grok-4", "grok-4-fast"],
        what="xAI's hosted line, on an OpenAI-compatible endpoint."),
    "bedrock": dict(
        brand="AMAZON", family="Nova",
        models=["amazon.nova-lite-v1:0", "amazon.nova-pro-v1:0"],
        what="Nova through Bedrock. Needs a Bedrock API key and the right AWS_REGION — and "
             "we hold no AWS account, so this is the one provider here we have never run."),
}


#: How each provider names its own models, given a credential. One GET, one JSON body.
#:
#: BEDROCK IS ABSENT ON PURPOSE. We hold no AWS account and have never run it, so a guess at its
#: listing shape would be untested code wearing the same clothes as the tested ones. It keeps its
#: built-in list and says so.
_LISTING = {
    "gemini": dict(
        url="https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        auth="query",
        # Only what can actually answer a prompt. The same endpoint lists embedding and
        # image models, and offering one as a chat model reproduces this bug pointing the
        # other way — a name in our dropdown that 404s the moment it is chosen.
        pluck=lambda d: [m["name"].split("/")[-1] for m in d.get("models", [])
                         if "generateContent" in m.get("supportedGenerationMethods", [])]),
    "anthropic": dict(
        url="https://api.anthropic.com/v1/models?limit=100",
        auth="x-api-key",
        pluck=lambda d: [m["id"] for m in d.get("data", []) if m.get("id")]),
    "dashscope": dict(
        url="",                                  # region-dependent; see `_listing_url`
        auth="bearer",
        pluck=lambda d: [m["id"] for m in d.get("data", []) if m.get("id")]),
    "xai": dict(
        url="https://api.x.ai/v1/models",
        auth="bearer",
        pluck=lambda d: [m["id"] for m in d.get("data", []) if m.get("id")]),
}

#: Short, because a settings page is waiting on it. A provider that does not answer in this long
#: is treated as unreachable and the built-in list is shown — the page must never hang on a
#: vendor, and an install with no network is the normal case for this product.
LISTING_TIMEOUT = 6

#: One listing per provider per five minutes. The page polls, and a fresh HTTP call per poll
#: would be both slow and rude.
LISTING_TTL = 300
_listed: dict[str, tuple[float, list[str]]] = {}

#: Names that are not chat models. The OpenAI-compatible listings return everything an account
#: can reach — embeddings, speech, image and video generation — and none of those can answer a
#: prompt. Substring match, deliberately conservative: `vl` (vision-language) and `omni` DO
#: chat and are not excluded.
_NOT_CHAT = ("embedding", "embed", "rerank", "tts", "asr", "whisper", "speech", "audio",
             "ocr", "moderation", "image", "video", "wan", "imagen", "veo", "aqa")


def _listing_url(name: str) -> str:
    """The listing endpoint, resolving anything region-dependent at use time."""
    spec = _LISTING.get(name) or {}
    if name == "dashscope":
        # Same region convention as the completion endpoint above, read from the environment
        # rather than captured, per the .env rule.
        region = (os.getenv("DASHSCOPE_REGION") or "intl").strip().lower()
        host = "dashscope-intl.aliyuncs.com" if region == "intl" else "dashscope.aliyuncs.com"
        return f"https://{host}/compatible-mode/v1/models"
    return spec.get("url", "")


def _listing_get(name: str, key: str) -> dict:
    """One authenticated GET, returning parsed JSON. Raises on anything unexpected."""
    spec = _LISTING[name]
    url, headers = _listing_url(name), {"User-Agent": "agentduet-desktop"}
    auth = spec["auth"]
    if auth == "query":
        url = f"{url}{'&' if '?' in url else '?'}key={urllib.parse.quote(key)}"
    elif auth == "x-api-key":
        headers |= {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=LISTING_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _live_models(name: str) -> list[str] | None:
    """What the provider says it serves, or None if we could not ask."""
    if name not in _LISTING:
        return None
    impl = _IMPLS.get(name)
    key = impl.credential() if impl else None
    # None means no credential. "" means signed in some other way — an Anthropic CLI login —
    # which is a real credential for COMPLETING but not a key we can put on a GET.
    if not key:
        return None
    now = time.monotonic()
    if (hit := _listed.get(name)) and now - hit[0] < LISTING_TTL:
        return hit[1]
    try:
        got = [m for m in _LISTING[name]["pluck"](_listing_get(name, key))
               if not any(bad in m.lower() for bad in _NOT_CHAT)]
    except Exception as exc:
        # THE EXCEPTION TEXT IS DELIBERATELY DROPPED. Gemini authenticates by query string, so
        # the failing URL carries the owner's key — and urllib's errors can carry the URL. Only
        # the class name is logged; it is enough to tell a timeout from a 401.
        logger.debug("could not list %s models: %s", name, type(exc).__name__)
        return None
    if got:
        _listed[name] = (now, got)
    return got or None


def offered(name: str) -> tuple[list[str], str]:
    """The models to show for a provider, and whether the provider itself said so.

    THE POINT OF THIS IS THE REMOVAL, not the addition. Our lists are compiled into the app, so
    a provider retiring a name leaves it in the dropdown until someone ships a new build — and
    choosing it produced `404 NOT_FOUND models/gemini-3.1-pro is not found`, which reads as the
    owner's key being broken. Asking the provider means a name we cannot serve stops being
    offered at all.

    Built-ins the provider still serves come FIRST, in our order, because that order is a
    recommendation the vendor's own listing does not carry. Everything else follows
    alphabetically. The model in use is always present, even if the provider stopped listing it:
    the page marks the current selection by finding it here, and an owner must be able to see
    what their instance is actually set to.
    """
    spec = HOSTED.get(name)
    if not spec:
        return [], "built-in"
    built = list(spec["models"])
    live = _live_models(name)
    if not live:
        return built, "built-in"
    keep = [m for m in built if m in live]
    out = keep + sorted(m for m in live if m not in keep)
    in_use = os.getenv("SECRETARY_MODEL", "")
    if in_use and _vendor_of_name(in_use) == _VENDOR.get(name) and in_use not in out:
        out.append(in_use)
    return out, "live"


def hosted_listing() -> list[dict]:
    """Every hosted provider, what it costs the owner to use, and whether we already hold a key.

    Alphabetical by vendor, with whatever is in use pinned first — the same order as the local
    list, for the same reason: any other order ranks the vendors.
    """
    live_model = os.getenv("SECRETARY_MODEL", "")
    live_prov = provider(live_model) if recognised(live_model) else ""
    out = []
    for name, spec in HOSTED.items():
        impl = _IMPLS[name]
        cred = impl.credential()
        out.append({
            "id": name, "brand": spec["brand"], "family": spec["family"],
            "what": spec["what"],
            "key_env": getattr(impl, "KEY", ""),
            # None means no credential at all; "" means signed in some other way (an Anthropic
            # CLI profile), which is a real credential and must not read as a missing one.
            "has_key": cred is not None,
            "how": "" if cred is None else ("api key" if cred else "signed in"),
            "in_use": name == live_prov,
            "model": live_model if name == live_prov else "",
            # Asked of the provider when we hold a key, so a retired model stops being offered.
            # Falls back to the built-in list offline, which is the normal case for this product.
            "models": (asked := offered(name))[0],
            "models_from": asked[1],
        })
    return sorted(out, key=lambda h: (not h["in_use"], h["brand"].lower()))


def forget_key(name: str) -> str:
    """Remove a stored credential. Refuses the provider currently in use."""
    spec = HOSTED.get(name)
    if not spec:
        return f"{name} is not a provider we offer."
    live = os.getenv("SECRETARY_MODEL", "")
    if recognised(live) and provider(live) == name:
        return f"{spec['family']} is in use. Choose another model first, then forget the key."
    var = getattr(_IMPLS[name], "KEY", "")
    if not var or os.getenv(var) is None:
        return f"No {spec['family']} key is stored."
    from . import tools
    tools._forget_env([var])
    _cached.clear()          # a cached client outlives the key that built it
    return f"Forgot the {spec['family']} key."
