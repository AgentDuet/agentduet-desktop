"""What a customer-authored tool must NOT be able to reach.

Separate from `test_rules.py` because that suite runs with no venv and nothing imported from the
model SDK, while these need the WASM runtime. Run:  .venv-build/bin/python tests/test_wasm.py

WHY THESE ARE WRITTEN FIRST

The tool is JavaScript somebody else wrote, invoked because a stranger asked a question. The
sandbox is what stands between that and the machine. Every other test here would pass with a
sandbox that leaks — the tool would still compute, still return a status, still look correct —
so the denial has to be asserted directly, and asserted before the thing it constrains exists.

THE ONE THAT MATTERS is the environment. Our process holds DASHSCOPE_API_KEY and
AGENTDUET_API_KEY. wasmtime's default WasiConfig inherits the parent environment, and the JS
engine REQUIRES `environ_get` to load at all — so "grant nothing" is not available, and a shim
that quietly passes the real environment through is the realistic mistake. It would leak the
owner's credentials to whoever asked a question, and no other check would notice.
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

PASS = FAIL = 0
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


def main() -> int:
    print("\n  -- wasm: what a tool cannot reach --")

    # A sentinel that is unmistakable if it ever comes back out of the sandbox.
    os.environ["DASHSCOPE_API_KEY"] = "sk-LEAKED-CANARY-0001"
    os.environ["AGENTDUET_API_KEY"] = "adk-LEAKED-CANARY-0002"

    try:
        from dduet_desktop import wasm_host
    except ImportError as exc:
        ok("the wasm host module exists", False, f"{exc}")
        print(f"\n  {PASS} passed, {FAIL} failed")
        return 1

    # 1. THE ENVIRONMENT. The canary must not appear in what the tool can see.
    out = wasm_host.run_source(
        "console.log(JSON.stringify(Object.keys(globalThis)));", {})
    ok("a tool cannot read our environment",
       "LEAKED-CANARY" not in str(out), f"leaked: {str(out)[:160]}")

    env_probe = """
    var found = [];
    try { for (var k in process.env) found.push(k + '=' + process.env[k]); } catch (e) {}
    try { found.push(String(Deno.env.toObject())); } catch (e) {}
    console.log(JSON.stringify(found));"""
    out = wasm_host.run_source(env_probe, {})
    ok("and cannot reach it through a JS environment shim",
       "LEAKED-CANARY" not in str(out), f"leaked: {str(out)[:160]}")

    # 2. THE FILESYSTEM. No mounts, so there is nothing to open.
    fs_probe = """
    try { var fs = require('fs'); console.log(fs.readFileSync('/etc/passwd', 'utf8')); }
    catch (e) { console.log('DENIED: ' + e.message); }"""
    out = wasm_host.run_source(fs_probe, {})
    ok("a tool cannot read a file", "root:" not in str(out), str(out)[:160])

    # 3. THE NETWORK. Egress is out of scope for this build, so it must be absent, not merely
    #    unused — a tool that can reach any host is an SSRF before the allowlist is written.
    net_probe = """
    try { console.log(String(typeof fetch)); } catch (e) { console.log('DENIED'); }"""
    out = wasm_host.run_source(net_probe, {})
    ok("a tool has no fetch", "function" not in str(out).lower(), str(out)[:160])

    # 4. It must still be USEFUL, or the denials prove nothing.
    out = wasm_host.run_source("console.log('hi ' + 'Tan');", {"name": "Tan"})
    ok("a tool that asks for nothing still runs", "hi Tan" in str(out), str(out)[:160])

    print(f"\n  {PASS} passed, {FAIL} failed")
    if FAILED:
        print("  failing: " + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
