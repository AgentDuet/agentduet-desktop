"""Run a customer-authored JavaScript tool inside a WASM sandbox.

THREE LAYERS, AND THE FIRST IS NOT THE SANDBOX

A tool cannot read a file or reach the network for two independent reasons, and it is worth
knowing which is doing the work:

 1. **The JS engine has no such functions.** `globalThis` is five names — `console`, `Javy`,
    `TextEncoder`, `TextDecoder`, and one internal. There is no `require`, no `fetch`, no
    `process`. Most attempts fail here, before the sandbox is involved at all.
 2. **The sandbox would refuse anyway.** The module gets only the capabilities we grant.
 3. **Host functions are the only doors**, and each applies the caller's permissions.

Two independent reasons is stronger than one — but it also means a test that probes from JS
proves layer 1, not layer 2. The environment denial passed while `inherit_env()` was still in
this file, because QuickJS does not expose the environment to JS whatever WASI holds. That is
exactly the test that manufactures confidence, so the shim is pinned by a source check as well.

See docs/wasm-host-brief.md and the WASM section of docs/design.md.
"""
from __future__ import annotations

import json
import logging
import pathlib
import tempfile

from wasmtime import Engine, Linker, Module, Store, WasiConfig

logger = logging.getLogger("dduet.wasm")

#: What a tool that broke returns. The caller hears a technical problem; never the reason.
TRAPPED = ""

#: Javy's QuickJS engine, compiled to WASM. One artifact for every platform. It exports
#: `compile-src` as well as `invoke`, so JS SOURCE compiles inside the sandbox and no compiler
#: ships with the product.
PLUGIN = pathlib.Path(__file__).parent / "wasm" / "javy-plugin.wasm"

#: Javy's contract is stdio: the tool reads its input as JSON on stdin and writes its result as
#: JSON on stdout. So the sandbox needs stdin and stdout — but ours, not the process's.
_engine = Engine()

#: COMPILED ONCE, INSTANTIATED PER CALL. Compiling the 1.3 MB engine on every call is not just
#: wasteful — it accumulates in the shared Engine and the fifth call traps inside
#: `initialize-runtime`. An instance is the cheap part and the part that must be fresh; the
#: module is immutable and shared safely.
_module: "Module | None" = None


def _plugin() -> Module:
    global _module
    if _module is None:
        _module = Module.from_file(_engine, str(PLUGIN))
    return _module


#: How many times a tool may ask for something before we stop running it.
#:
#: An unbounded ask/answer loop is the ring-limit problem in another costume: a tool that always
#: asks would be re-run forever, on the call path, while a caller waits. Three is enough for a
#: lookup that depends on a lookup, and small enough that a runaway costs milliseconds.
MAX_ROUNDS = 3


def _wrap(js: str, input_data: dict, answers: dict) -> str:
    """The tool's source, with its input compiled in and a way to return a result.

    Input is a LITERAL rather than stdin, because stdin traps the engine (see run_source). It is
    serialised with `json.dumps`, which cannot be escaped out of: a hostile value arrives as an
    escaped string, verified with `"); console.log("ESCAPED"); ("` — it came back as data.

    `ensure_ascii` matters and is the default: U+2028 and U+2029 are legal in JSON and terminate
    a line in JavaScript, so an un-escaped one would break out of the literal.
    """
    return (f"const INPUT = {json.dumps(input_data, ensure_ascii=True)};\n"
            f"const ANSWERS = {json.dumps(answers, ensure_ascii=True)};\n"
            f"function result(o) {{ console.log(JSON.stringify({{result: o}})); }}\n"
            f"function need(o) {{ console.log(JSON.stringify({{need: o}})); }}\n"
            f"{js}")


def run_source(js: str, input_data: dict, answers: dict | None = None) -> str:
    """Compile and run one tool ONCE. A fresh instance every call — never reused across callers."""
    js = _wrap(js, input_data, answers or {})
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / "out").touch()

        cfg = WasiConfig()
        # NO stdin. Javy reads stdin during `initialize-runtime`, and handing it input the tool
        # never asked for traps the engine before any JS runs. Input arrives as a literal compiled
        # into the source instead — see `_wrap`.
        cfg.stdout_file = str(d / "out")
        cfg.stderr_file = str(d / "err")

        # THE ENVIRONMENT IS EMPTY, EXPLICITLY.
        #
        # Not `inherit_env()`, which is one method call away and looks like helpfulness — it would
        # hand our DASHSCOPE_API_KEY and AGENTDUET_API_KEY to code that runs because a stranger
        # asked a question. It is set to an empty list rather than left at the default so that the
        # intent is legible and a source check can assert it.
        #
        # No preopened directories, so there is no filesystem to reach even if the engine grew an
        # API for one.
        cfg.env = []

        linker = Linker(_engine)
        linker.define_wasi()
        store = Store(_engine)
        store.set_wasi(cfg)
        instance = linker.instantiate(store, _plugin())

        exports = instance.exports(store)
        src = js.encode()
        # Javy's plugin ABI: hand the source into the module's memory, compile, then invoke.
        memory = exports["memory"]
        realloc = exports["cabi_realloc"]
        ptr = realloc(store, 0, 0, 1, len(src))
        memory.write(store, src, ptr)
        exports["initialize-runtime"](store)

        # `compile-src` returns a pointer to a RESULT, not to a bare fat pointer:
        #
        #     [0] discriminant   0 = ok
        #     [1] pointer        to the compiled bytecode
        #     [2] length
        #
        # Reading it as (ptr, len) takes the discriminant for the pointer and produces bytecode
        # starting at address 0 — which fails as "invalid version (0 expected=26)", where 26 is
        # 0x1a, the first byte of the real bytecode. An oddly helpful error, once located.
        res = exports["compile-src"](store, ptr, len(src))
        head = memory.read(store, res, res + 12)
        if int.from_bytes(head[0:4], "little") != 0:
            raise RuntimeError("the tool did not compile")
        bc_ptr = int.from_bytes(head[4:8], "little")
        bc_len = int.from_bytes(head[8:12], "little")

        # invoke(bytecode, Option<fn_name>). An Option in the canonical ABI is three values —
        # discriminant, pointer, length — which is why this takes five and not three.
        #
        # A TRAP IS CONTAINED HERE. A tool that divides by zero, overflows the stack or calls
        # something absent raises a Trap out of wasmtime, and it must not travel any further: the
        # tool ran because a stranger asked a question, so an uncaught trap would let any caller
        # break the answering path. The caller is told there was a technical problem; the owner
        # gets the detail in the log.
        #
        # NOT the same as a wasmtime PANIC, which aborts the process and cannot be caught at all
        # (see design.md). This catches misbehaving JS, not a bug in the runtime.
        try:
            exports["invoke"](store, bc_ptr, bc_len, 0, 0, 0)
        except Exception as exc:
            logger.info("tool trapped: %s", str(exc).splitlines()[0][:120])
            return TRAPPED

        return (d / "out").read_text()


def run_tool(js: str, input_data: dict, fulfil, max_rounds: int = MAX_ROUNDS) -> dict:
    """Run a tool to a result, fulfilling what it asks for along the way.

    `fulfil(kind, request) -> value | None` is OURS. It decides whether a request is allowed at
    all, applies the caller's permissions, and does the work — the tool only ever states a need.
    Returning None refuses; the tool sees the refusal as an absent answer and must cope.

    Returns the tool's result dict, or a status the framework can render. Never raises: a tool runs
    because a stranger asked a question, so nothing it does may reach the caller's call path.
    """
    answers: dict = {}
    for _ in range(max_rounds):
        raw = run_source(js, input_data, answers)
        if raw is TRAPPED or not raw.strip():
            return {"status": "tool_failed"}
        try:
            # The LAST line. A tool may console.log freely for its own debugging, and only the
            # final line is the one it meant as its answer.
            msg = json.loads([l for l in raw.strip().splitlines() if l.strip()][-1])
        except (ValueError, IndexError):
            logger.info("tool produced no usable output")
            return {"status": "tool_failed"}

        if "result" in msg:
            return {"status": "ok", "result": msg["result"]}

        if "need" in msg:
            req = msg["need"] if isinstance(msg["need"], dict) else {}
            kind = str(req.get("kind", ""))
            # WE decide. The kind comes from a closed set the fulfiller implements, so a tool
            # cannot invent a capability by naming one.
            value = fulfil(kind, req)
            if value is None:
                logger.info("refused a tool's request for %r", kind[:40])
            answers[kind or "_"] = value
            continue

        logger.info("tool returned neither a result nor a need")
        return {"status": "tool_failed"}

    # Asked too many times. Not an error on the caller's part, and not something they hear about.
    logger.info("tool still asking after %d rounds — giving up", max_rounds)
    return {"status": "tool_failed"}


#: WHAT A TOOL MAY ASK FOR. A closed set, like `capabilities.ACTIONS`, and for the same reason:
#: a tool cannot invent a capability by naming one. Anything not here is refused, and the tool
#: sees the refusal as an absent answer.
KINDS = ("knowledge", "fetch")

#: How long we wait on someone else's server, and how much of its answer we accept. A caller is
#: on the line, and a slow or enormous response is the same problem as a slow tool.
FETCH_TIMEOUT = 8.0
FETCH_MAX_BYTES = 64 * 1024


def fulfiller(caller: str, verified: bool, tool: str = ""):
    """Build the `fulfil` for ONE caller. The permissions travel with it, not with the tool.

    This is where a tool's request meets the owner's grants. The tool names a `kind` and passes
    arguments; we decide, and we do the work. A tool asking for knowledge gets exactly what THIS
    caller is allowed to be told — the same `permissions.context_for` that governs the built-in
    `search_knowledge`, so a tool cannot become a way around the disclosure rules.
    """
    from . import permissions

    def fulfil(kind: str, req: dict):
        if kind not in KINDS:
            logger.info("tool asked for %r, which is not a kind we fulfil", str(kind)[:40])
            return None
        if kind == "fetch":
            return _fetch(tool, req)
        if kind == "knowledge":
            query = str(req.get("query") or "")[:400]
            text, sources = permissions.context_for(caller, verified, query)
            logger.info("tool looked up %r for %s -> %s", query[:60], caller, sources)
            # Text, never paths. Same rule as the built-in tool: the owner's file layout is not
            # the caller's business, and a model handed filenames will cite them.
            return text[:4000]
        return None

    return fulfil


def resolve_url(tool: str, req: dict) -> str | None:
    """Which URL a request means — or None if the owner did not approve it.

    THE SECURITY PROPERTY LIVES HERE, and it is kept apart from the network call so it can be
    tested without one. A suite proving a tool cannot reach 169.254.169.254 should not itself need
    the internet to run.

    There is NO FIELD A TOOL CAN USE AS A DESTINATION. It passes a label; we look that label up in
    the manifest the owner approved beside the code. A `url` field, if the tool sends one, is
    IGNORED — not validated, ignored. Validating a URL supplied by something a stranger can talk to
    is how SSRF filters get fooled: redirects, encodings, hostnames that resolve internally. A tool
    cannot express 192.168.1.1 because there is no field in which a URL means anything.

    Params become a query string on the approved URL, so they cannot move the host.
    """
    import urllib.parse

    from . import toolstore

    approved = toolstore.endpoints(tool) if tool else {}
    label = str(req.get("endpoint", ""))
    url = approved.get(label)
    if not url:
        logger.info("tool %r asked for endpoint %r, which the owner did not approve (has: %s)",
                    tool, label[:40], ", ".join(approved) or "none")
        return None
    params = req.get("params")
    if isinstance(params, dict) and params:
        sep = "&" if urllib.parse.urlsplit(url).query else "?"
        url = url + sep + urllib.parse.urlencode({str(k): str(v) for k, v in params.items()})
    return url


def _fetch(tool: str, req: dict):
    """Reach an endpoint the OWNER approved for THIS tool. By name, never by URL.

    THERE IS NO FIELD A TOOL CAN USE AS A DESTINATION. It passes a label; we look that label up in
    the manifest the owner approved alongside the code. `url`, if present, is ignored — not
    validated, ignored, because validating a URL supplied by something a stranger can talk to is
    how SSRF filters get fooled (redirects, encodings, hostnames that resolve internally). A tool
    cannot express 192.168.1.1 because it cannot express a URL.

    Which URL a request means is decided by `resolve_url`, deliberately apart from this: the
    security property is entirely there, and keeping it separate means it can be tested without a
    network. This function only goes and gets it.
    """
    import urllib.request

    label = str(req.get("endpoint", ""))
    url = resolve_url(tool, req)
    if url is None:
        return None
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as r:
            body = r.read(FETCH_MAX_BYTES + 1)
    except Exception as exc:
        # Someone else's server, so failure is normal. The tool sees no answer and must cope.
        logger.info("fetch %s failed: %s: %s", label, type(exc).__name__, str(exc)[:90])
        return None
    if len(body) > FETCH_MAX_BYTES:
        logger.info("fetch %s returned more than %d bytes — refused", label, FETCH_MAX_BYTES)
        return None
    logger.info("fetch %s for tool %r -> %d bytes", label, tool, len(body))
    # UNTRUSTED. It is a third party's response, and it reaches a model that talks to a stranger.
    # Returned as text for the tool to interpret; we make no claim about what is in it.
    return body.decode("utf-8", "replace")
