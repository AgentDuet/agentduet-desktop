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

import json
import os
import pathlib
import sys
import tempfile

# ISOLATION FIRST, BEFORE ANY IMPORT. A fulfiller test reads knowledge through the real
# permission path, and without this it reads the OWNER'S documents — which is how a "safe" test
# quietly starts depending on one machine's private data.
os.environ["AGENTDUET_HOME"] = tempfile.mkdtemp(prefix="wasm-test-")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

def eq(name, got, want):
    ok(name, got == want, f"got {got!r}, wanted {want!r}")


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
        from agentduet_desktop import wasm_host
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

    # 4. THE SANDBOX ITSELF, not the engine's surface.
    #
    # Every probe above questions the JS environment, and QuickJS exposes almost nothing — so
    # they ALL PASSED while `inherit_env()` was still in the host. They prove the engine is
    # minimal, not that we configured the sandbox correctly. A test that passes when the code is
    # wrong manufactures confidence, so the configuration is asserted directly.
    host = (pathlib.Path(__file__).parent.parent / "src" / "agentduet_desktop" / "wasm_host.py")
    src = "\n".join(l for l in host.read_text().splitlines() if not l.strip().startswith("#"))
    # The CALL, not the word. The docstring explains why inherit_env() was removed, and matching
    # prose instead of code is how a check starts failing for being well documented — the same
    # slip that tripped the str(exc) check in test_rules.py.
    ok("the sandbox does not inherit our environment", "cfg.inherit_env()" not in src,
       "inherit_env() would hand the model key to a stranger's tool")
    ok("and the environment is emptied explicitly", "cfg.env = []" in src)
    ok("no directory is preopened", "preopen" not in src)
    ok("stdin is not wired to the tool", "stdin_file" not in src)

    # 5. It must still be USEFUL, or the denials prove nothing.
    out = wasm_host.run_source("result({greeting: 'hi ' + INPUT.name});", {"name": "Tan"})
    ok("a tool receives its input and returns a result", "hi Tan" in str(out), str(out)[:160])

    # Input is compiled in as a literal, so it must not be escapable.
    out = wasm_host.run_source("result({v: INPUT.name});",
                               {"name": '"); console.log("ESCAPED"); ("'})
    # Breaking out would run a SECOND console.log, so the tell is an extra line — not the
    # presence of the word, which correctly comes back escaped inside the value.
    lines = [l for l in out.strip().splitlines() if l.strip()]
    ok("a hostile input value cannot break out of the literal",
       len(lines) == 1
       and json.loads(lines[0])["result"]["v"].startswith('"); console.log'),
       f"{len(lines)} line(s): {out[:120]}")

    # ---- two-phase: a tool asks, WE decide -------------------------------------------------
    # Javy's plugin imports only WASI, so a tool cannot call us. It states a need and is run
    # again with the answer. Stricter than host functions: it never initiates anything.
    stock = """
    if (ANSWERS.stock === undefined) { need({ kind: 'stock', item: INPUT.item }); }
    else if (ANSWERS.stock === null)  { result({ status: 'unknown' }); }
    else { result({ status: ANSWERS.stock > 0 ? 'in_stock' : 'out_of_stock' }); }"""

    seen = []
    def grant(kind, req):
        seen.append((kind, req.get("item")))
        return 7 if kind == "stock" else None

    out = wasm_host.run_tool(stock, {"item": "SKU-A"}, grant)
    ok("a tool can ask for something and be answered",
       out.get("result", {}).get("status") == "in_stock", str(out))
    ok("and we saw exactly what it asked for", seen == [("stock", "SKU-A")], str(seen))

    # REFUSAL is not an error. The tool must cope with not being given what it wanted.
    out = wasm_host.run_tool(stock, {"item": "SKU-A"}, lambda k, r: None)
    ok("a refused request reaches the tool as no answer",
       out.get("result", {}).get("status") == "unknown", str(out))

    # THE RUNAWAY. An unbounded ask/answer loop would re-run the tool forever while a caller
    # waits — the ring-limit problem in another costume.
    rounds = []
    wasm_host.run_tool("need({kind:'x'});", {}, lambda k, r: rounds.append(1) or 1)
    ok("a tool that never stops asking is capped",
       len(rounds) == wasm_host.MAX_ROUNDS, f"{len(rounds)} rounds")

    # A tool cannot invent a capability by naming one — the fulfiller decides what exists.
    out = wasm_host.run_tool("need({kind:'read_the_disk'});", {}, grant)
    ok("an unknown request kind is refused", out["status"] == "tool_failed", str(out))

    # ---- the fulfiller: where a request meets the owner's grants ---------------------------
    from agentduet_desktop import paths
    (paths.KNOWLEDGE).mkdir(parents=True, exist_ok=True)
    (paths.KNOWLEDGE / "hours.md").write_text("We open at 9am on weekdays.")

    ask = """
    if (ANSWERS.knowledge === undefined) { need({kind:'knowledge', query: INPUT.q}); }
    else { result({ text: ANSWERS.knowledge || 'nothing' }); }"""

    out = wasm_host.run_tool(ask, {"q": "opening hours"}, wasm_host.fulfiller("bob@x", False))
    ok("a tool's request is answered through the owner's permissions",
       "9am" in str(out.get("result", {}).get("text", "")), str(out)[:200])

    # THE POINT of routing through permissions: a tool must not be a way around disclosure.
    host_src = (pathlib.Path(__file__).parent.parent / "src" / "agentduet_desktop"
                / "wasm_host.py").read_text()
    ok("knowledge is fetched through the same gate as the built-in tool",
       "permissions.context_for(caller, verified, query)" in host_src)
    ok("and file paths are not handed to the tool", '"sources"' not in host_src)

    # A closed set, like capabilities.ACTIONS — a tool cannot name a capability into existence.
    out = wasm_host.run_tool("need({kind:'shell', cmd:'ls'});", {},
                             wasm_host.fulfiller("bob@x", False))
    ok("a kind we do not fulfil is refused", out["status"] == "tool_failed", str(out))

    # ---- reaching the outside: the owner names it, the tool asks by name --------------------
    # A tool that could supply a URL is an SSRF: a caller talks it into fetching an internal
    # address. So it names a NAME, and there is no URL for it to express.
    from agentduet_desktop import toolstore
    toolstore.ACTIVE = pathlib.Path(os.environ["AGENTDUET_HOME"]) / "tools"
    toolstore.PENDING = toolstore.ACTIVE / "pending"

    toolstore.propose("weather", "result({ok:1});",
                      endpoints={"forecast": "https://api.open-meteo.com/v1/forecast"})
    toolstore.approve("weather")
    eq("the approved endpoints are stored with the tool",
       toolstore.endpoints("weather"), {"forecast": "https://api.open-meteo.com/v1/forecast"})

    # 1. AN UNAPPROVED NAME. The owner approved `forecast` and nothing else.
    ok("an endpoint the owner did not approve is refused",
       wasm_host.resolve_url("weather", {"endpoint": "somewhere_else"}) is None)

    # 2. A URL, however it is dressed up. Checked through resolve_url so this needs no network:
    # the question is which destination a request MEANS, not what that server replies.
    for attempt in ({"url": "http://169.254.169.254/"},
                    {"endpoint": "http://192.168.1.1/"},
                    {"endpoint": "../../etc/passwd"},
                    {"endpoint": ""}):
        ok(f"a tool cannot name a destination itself: {str(attempt)[:36]}",
           wasm_host.resolve_url("weather", attempt) is None)

    # A `url` field alongside a VALID endpoint must be ignored, not honoured — the tool still
    # reaches only what the owner approved.
    got = wasm_host.resolve_url("weather", {"endpoint": "forecast",
                                            "url": "http://127.0.0.1:8899/"})
    ok("a url field is ignored, not obeyed",
       got is not None and got.startswith("https://api.open-meteo.com"), str(got))

    # Params cannot move the host.
    got = wasm_host.resolve_url("weather", {"endpoint": "forecast",
                                            "params": {"latitude": "1.29", "x": "http://evil/"}})
    ok("params become a query string and cannot change the host",
       got.startswith("https://api.open-meteo.com/v1/forecast?"), str(got)[:90])

    # 3. A tool with no approved endpoints at all cannot fetch anything.
    ok("a tool with no approved endpoints cannot fetch anything",
       wasm_host.resolve_url("nothing", {"endpoint": "forecast"}) is None)
    ok("and neither can a request with no tool at all",
       wasm_host.resolve_url("", {"endpoint": "forecast"}) is None)

    print(f"\n  {PASS} passed, {FAIL} failed")
    if FAILED:
        print("  failing: " + "; ".join(FAILED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
