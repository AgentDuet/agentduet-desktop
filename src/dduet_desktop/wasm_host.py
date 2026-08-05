"""Run a customer-authored JavaScript tool inside a WASM sandbox.

FIRST CUT — deliberately built with the mistake in place, so the denial test is seen to fail
before the shim is written. `inherit_env()` below is the realistic error: it is one method call,
it looks like helpfulness, and it hands the owner's model key and connector credential to code a
stranger's question caused to run. Replaced in the next commit.

See docs/wasm-host-brief.md and the WASM section of docs/design.md.
"""

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


def run_source(js: str, input_data: dict) -> str:
    """Compile and run one tool. A fresh instance every call — never reused across callers."""
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / "in").write_text(json.dumps(input_data))
        (d / "out").touch()

        cfg = WasiConfig()
        cfg.stdin_file = str(d / "in")
        cfg.stdout_file = str(d / "out")
        cfg.stderr_file = str(d / "err")
        # THE MISTAKE, on purpose and briefly. See the module docstring.
        cfg.inherit_env()

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
