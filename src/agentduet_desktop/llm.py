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

#: Set explicitly, or inferred from the model name — a user who writes
#: SECRETARY_MODEL=claude-sonnet-5 should not also have to name the provider.
#:
#: `qwen` maps to DashScope (hosted). The same weights can also run locally through Ollama;
#: that is a different transport, not a different model, so it will need
#: SECRETARY_PROVIDER=ollama rather than a name prefix to tell them apart.
_PROVIDERS = {"gemini": ("gemini",), "anthropic": ("claude",), "dashscope": ("qwen",)}

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
    """Which provider serves `model`. Explicit env wins; otherwise inferred from the name."""
    named = (os.getenv("SECRETARY_PROVIDER") or "").strip().lower()
    if named in _PROVIDERS:
        return named
    name = (model or os.getenv("SECRETARY_MODEL") or "").lower()
    for prov, prefixes in _PROVIDERS.items():
        if any(p in name for p in prefixes):
            return prov
    return "gemini"


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


_IMPLS = {"gemini": _Gemini, "anthropic": _Anthropic, "dashscope": _DashScope}
_cached: dict[str, object] = {}


def client(model: str = ""):
    """The live client for `model`, or None when no key is configured.

    Cached per (provider, model). Returning None rather than raising is deliberate: the
    agent must degrade to escalating everything when no model is attached, not crash.
    """
    m = model or os.getenv("SECRETARY_MODEL") or "gemini-3.1-flash"
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
    return client(model or os.getenv("SECRETARY_MODEL") or "gemini-3.1-flash") is not None


def verify(model: str = "") -> tuple[bool, str]:
    """Actually call the model once. Returns (ok, a sentence the owner can act on.)

    Init must not write a credential it has not proven works. A typo'd key is
    indistinguishable at rest from a good one, and the failure mode is silent: the agent
    starts, connects, and escalates every message. Cheaper to spend one token here.

    The message separates the cases that need different actions — a bad key needs a new
    key, a spend cap needs a billing change, and both are otherwise reported as "the model
    isn't working".
    """
    m = model or os.getenv("SECRETARY_MODEL") or "gemini-3.1-flash"
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
        return False, f"Call failed: {text[:160]}"
    if not out:
        return False, "The model returned nothing — it may have declined the request."
    return True, f"Working — {describe(m)}"


def describe(model: str = "") -> str:
    """For the owner's diagnostics — which provider and model are in use, and how it is
    authenticated. Distinguishing "key" from "signed in" matters: an expired OAuth login
    looks exactly like no model attached (everything escalates), so the owner needs to be
    able to see which one they have."""
    m = model or os.getenv("SECRETARY_MODEL") or "gemini-3.1-flash"
    prov = provider(m)
    impl = _IMPLS[prov]
    cred = impl.credential()
    how = ("no credential — set %s%s" % (impl.KEY,
           ", or run `ant auth login`" if prov == "anthropic" else "")) if cred is None \
        else ("api key" if cred else "signed in (OAuth profile / auth token)")
    # Report what WORKS, not just what is configured. A stale ANTHROPIC_PROFILE pointing at
    # a deleted profile looks credentialed and builds nothing — and "no model" means the
    # agent escalates every message, so the owner must not be told it is fine.
    if cred is not None and client(m) is None:
        how += " — but the client failed to build; see the log"
    return f"{prov}/{m} — {how}"
