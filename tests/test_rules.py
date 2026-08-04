"""The deterministic half of the agent — everything that must NOT depend on a model.

Why this exists as its own suite: `test_behaviour.py` drives the real model, so it is slow,
non-repeatable, and it costs money. On 2026-07-28 a day of iterating exhausted the project's
monthly Gemini spend cap, which meant the bounds logic — pure integer and time comparisons —
could not be tested at all. That is backwards. The rules that decide whether the agent may
ACT are exactly the rules that should be testable offline, in a second, forever.

It is also where the bugs actually were. Every failure we hit in the capability work was in
this layer, not in the model's judgement: a slot that ended after closing time, a quantity
compared as a string, a gate that matched phrasing instead of intent.

Run:  python3 test_rules.py        (no venv needed — nothing here imports the model SDK)

ISOLATION: every module store is redirected into a temp directory before anything runs.
Without that this suite would overwrite the real capabilities.json and delete live bookings —
which is precisely the kind of destructive surprise a "safe" unit test should never spring.
"""

import json
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

TMP = pathlib.Path(tempfile.mkdtemp(prefix="secretary-rules-"))

from dduet_desktop import capabilities
from dduet_desktop import memory
from dduet_desktop import paths
from dduet_desktop import permissions
from dduet_desktop import policy
from dduet_desktop import schedule
from dduet_desktop import tools

# Redirect stores BEFORE any test writes. Module-level constants, so this must happen here
# rather than inside a fixture.
schedule.STORE = TMP / "schedule.json"
capabilities.STORE = TMP / "capabilities.json"
memory.STORE = TMP / "conversations.json"
# Knowledge WRITES land on disk, so the root and the permissions file move too. Without this
# the suite would append test facts to the owner's real documents.
paths.KNOWLEDGE = TMP / "knowledge"
paths.SETTINGS = TMP / "settings.md"
permissions.PERMS = TMP / "permissions.json"
tools.EDIT_LOG = TMP / "knowledge-edits.jsonl"
paths.KNOWLEDGE.mkdir(parents=True, exist_ok=True)

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


def eq(name: str, got, want) -> None:
    ok(name, got == want, f"got {got!r}, wanted {want!r}")


# --------------------------------------------------------------------------
# schedule — the booking primitive
# --------------------------------------------------------------------------
def test_no_undefined_names() -> None:
    """Every name the package uses is bound. Caught by pyflakes, not by import.

    WHY THIS EARNS ITS PLACE

    A scripted edit deleted `model = QwenVoice(...)` out of the voice call path while replacing
    the block around it. The file still parsed, every module still imported, and all 140 checks
    still passed — because nothing here opens a call. It would have failed with NameError on the
    first real caller, and it survived four commits.

    The same class of accident happened twice in one day. A linter is the systemic answer; a
    test that only exercises what it remembers to exercise is not.
    """
    print("\n  -- lint: no undefined names --")
    import subprocess
    src = pathlib.Path(__file__).parent.parent / "src" / "dduet_desktop"
    try:
        done = subprocess.run([sys.executable, "-m", "pyflakes", *sorted(map(str, src.glob("*.py")))],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        ok("pyflakes is available", False, f"{exc} — pip install pyflakes")
        return
    if done.returncode not in (0, 1):
        ok("pyflakes ran", False, done.stderr[:200])
        return
    # Only the fatal class. Unused imports and f-string nits are style, and failing the suite on
    # them would train people to ignore it.
    fatal = [l for l in done.stdout.splitlines()
             if "undefined name" in l or "referenced before assignment" in l]
    ok("no undefined names anywhere in the package", not fatal, "\n        ".join(fatal))


def test_prompts() -> None:
    """Prompt templates are checked OFFLINE, because on voice the prompt is the control and a
    hole in it is only otherwise discovered by a stranger on the phone."""
    print("\n  -- prompts: templates render, and refuse holes --")
    from dduet_desktop import prompts

    problems = prompts.check_all()
    ok("every template declares exactly the parameters it uses", not problems, "; ".join(problems))

    text = prompts.render("asker-voice", owner_name="Stanley", pronoun="he/him")
    ok("the owner's name reaches the voice instruction", "Stanley" in text)
    ok("so does the configured pronoun", "he/him" in text)

    # The pronoun line must VANISH rather than render half-written: "Refer to X as ." is worse
    # than saying nothing, and an unset pronoun is the normal case.
    bare = prompts.render("asker-voice", owner_name="Stanley", pronoun="")
    ok("an unset pronoun removes its line entirely", "Refer to" not in bare, bare[:120])

    # The value class that actually shipped: a call answered as "[Owner's Name]'s assistant".
    for bad in ("", "   ", "[Owner's Name]", "TODO"):
        try:
            prompts.render("asker-voice", owner_name=bad)
            ok(f"refused owner_name={bad!r}", False, "rendered anyway")
        except prompts.PromptError:
            ok(f"refused owner_name={bad!r}", True)

    # THE OWNER-FACING PROMPT. Exposed over MCP so a host shows "Get started with DDuet" as
    # something to click — the only surface that answers "installed it, now what?".
    started = prompts.render("owner-getting-started")
    ok("the getting-started prompt renders with no parameters", len(started) > 400)
    # Two refusals it must carry. Both are invariants elsewhere in the product, and this text is
    # read by a model with shell access on the owner's machine, so a hole here is not cosmetic.
    ok("it refuses to accept a credential in chat",
       "API key" in started and "refuse" in started.lower(), started[-200:])
    ok("and it must not declare a capability",
       "Do not declare a capability" in started)


def test_asker_tool_surface() -> None:
    """The asker agent's authority. Read from SOURCE, so this runs with no SDK and no venv.

    This is the fence the whole product rests on: the agent that reads text written by strangers
    can only do these things. A tool that appears here without someone deciding to put it here is
    the failure in docs/tool-surface-risk.md.
    """
    print("\n  -- asker: the five tools, and nothing else --")
    import re
    src = (pathlib.Path(__file__).parent.parent / "src" / "dduet_desktop" / "voice.py").read_text()

    declared = set(re.findall(r'\{"name": "(\w+)",', src))
    handlers = set(re.findall(r"@tool\s*\n\s*async def (\w+)\(", src))

    # THE CANARY. Spelled out, so widening the asker's authority means editing a test that says
    # what this list is for — not just appending a dict and having every check still pass.
    expected = {"search_knowledge", "escalate", "request_callback", "transfer_to_owner", "book"}
    ok("the asker agent declares exactly the five agreed tools", declared == expected,
       f"declared={sorted(declared)}")
    ok("and no tool is offered without a handler", declared - handlers == set(),
       f"unimplemented={sorted(declared - handlers)}")
    ok("and no handler exists that was never declared", handlers - declared == set(),
       f"undeclared={sorted(handlers - declared)}")

    # Nothing that reaches the filesystem, the shell, or the network by name. Not a substitute for
    # reading the list — a tripwire for the specific thing an injected caller asks for.
    forbidden = ("read_file", "write_file", "shell", "exec", "run_command", "http", "fetch")
    ok("none of them can reach the machine",
       not [d for d in declared for f in forbidden if f in d], sorted(declared))

    # The declared list must be the authority. Dispatching off `handlers` would let a handler that
    # was never declared be called, which is how a debugging helper becomes reachable by a caller.
    ok("dispatch checks the declared registry, not the handler table",
       "if name not in ASKER_TOOL_NAMES" in src)
    # Compiled in, never read from the instance directory — see the withdrawn checklist item.
    ok("the registry is not loaded from $DDUET_HOME",
       not re.search(r"ASKER_TOOLS\s*=\s*.*(json\.load|read_text|paths\.)", src))

    # RETURN VALUES ARE CALLER-VISIBLE. A tool result enters the context of a model that is
    # speaking to a stranger, and `say` is only a convention the prompt asks it to respect. So
    # internals must not be in a return at all. Each of these was actually there.
    # Comments stripped: the fix's own comment SAYS `str(exc)` was returned here, and matching
    # prose instead of code is how a check starts failing for being well documented.
    code_only = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    ok("no exception text is returned to the model", "str(exc)" not in code_only,
       "str(exc) is reachable in a return")
    ok("no other system's error code is returned",
       '"reason": code' not in src)
    ok("knowledge filenames are not returned",
       '"sources": sources' not in src)
    ok("an unknown tool name is not echoed back",
       'f"no such tool: {name}"' not in src)


def test_hosts() -> None:
    """Assistant detection and registration. Model-free: it is paths and process calls."""
    print("\n  -- hosts: what an assistant is told to launch --")
    from dduet_desktop import hosts

    # PATH-INDEPENDENCE. A double-clicked app inherits the desktop session's environment, not the
    # shell's, and ~/.local/bin is often missing from it — so `shutil.which` found nothing and
    # step 4 reported "None found" about an installed Claude Code, while registration failed with
    # "could not run `claude`". Invisible from a terminal, which is why it is pinned here.
    import os
    real_path = os.environ.get("PATH", "")
    tmpbin = TMP / "fakebin"
    tmpbin.mkdir(parents=True, exist_ok=True)
    fake = tmpbin / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    real_extra = hosts.EXTRA_BINS
    try:
        os.environ["PATH"] = "/nonexistent"
        hosts.EXTRA_BINS = [tmpbin]
        ok("an assistant is found off PATH, where a GUI launch cannot see it",
           hosts.resolve_bin("claude") == str(fake), hosts.resolve_bin("claude"))
        ok("and something genuinely absent is still absent",
           hosts.resolve_bin("no-such-assistant") is None)
        # The registration path must use the resolved absolute path, not the bare name.
        src = (pathlib.Path(hosts.__file__)).read_text()
        ok("registration invokes the resolved path, not the bare command name",
           '[claude, "mcp", "add"' in src and '["claude", "mcp"' not in src)
    finally:
        os.environ["PATH"] = real_path
        hosts.EXTRA_BINS = real_extra

    cmd = hosts.launch_command()
    # The dev incantation cannot be registered on an installed machine — no python, no module
    # path — so a frozen build must register ITSELF. Getting this wrong produces a config that
    # works on the developer's laptop and nowhere else.
    ok("from source it launches the module", cmd[1:] == ["-m", "dduet_desktop.secretary_mcp"], cmd)

    import sys
    sys.frozen = True                       # pretend to be a PyInstaller build
    try:
        ok("frozen, it launches ITSELF with `mcp`", hosts.launch_command()[1:] == ["mcp"],
           hosts.launch_command())
        # The registered path must be the SYMLINK when one exists, never the versioned file —
        # otherwise every update silently breaks the owner's assistant.
        from dduet_desktop import install
        link = install.installed_path()
        if link.is_symlink() and link.resolve().is_file():
            ok("it registers the stable symlink, not the versioned payload",
               hosts.launch_command()[0] == str(link), hosts.launch_command()[0])
    finally:
        del sys.frozen

    # A dry run that changed something would be the worst possible bug in an installer.
    before = (pathlib.Path.home() / ".claude.json")
    stamp = before.stat().st_mtime if before.exists() else None
    text = hosts.connect(apply=False)
    after = before.stat().st_mtime if before.exists() else None
    ok("--show changes nothing", stamp == after)
    ok("--show says what it WOULD do", "would run" in text or "No AI assistant" in text, text[:90])

    # Real evidence, not a leftover directory: ~/.cursor survived here for months with no
    # cursor binary, and directory-existence reported an assistant that was not installed.
    import shutil
    if shutil.which("cursor") is None and not (hosts.HOME / ".cursor/mcp.json").is_file():
        ok("a stale ~/.cursor alone does not count as Cursor", "Cursor" not in hosts.detect())


def test_setup_mode() -> None:
    """Setup mode: while setup is unfinished the process is the installer, not the daemon.

    The defect this pins is not a wrong answer, it is TWO answers. The site decides which page a
    browser gets and the daemon decides whether to take the connector; when those were separate
    checks a process could serve "finish setting up" while holding the one client the connector
    allows — which is what forces a hand-over to wait on a pid before anyone can be answered.
    """
    print("\n  -- setup mode: the installer must not hold the channel --")
    import os
    import re
    from dduet_desktop import llm, owner

    src = pathlib.Path(__file__).parent.parent / "src" / "dduet_desktop"

    # ONE definition. Checked in the source because the alternative is importing both modules,
    # and this suite must run with no venv: `web` pulls in aiohttp, `secretary_agent` the SDK.
    web_src = (src / "web.py").read_text()
    ok("the site's page choice comes from owner.setup_pending",
       re.search(r"def needs_setup.*?owner\.setup_pending\(", web_src, re.S) is not None)
    agent_src = (src / "secretary_agent.py").read_text()
    # DELIBERATELY A DIFFERENT FUNCTION from the site's. Sharing one definition read well and
    # silently disabled a live secretary: a blank name in settings.md closed the channel on an
    # instance that was answering calls. Only "cannot answer at all" may do that.
    ok("the daemon gates the channel on cannot_answer, not on setup_pending",
       "owner.cannot_answer()" in agent_src and "owner.setup_pending()" not in agent_src)
    # ORDER is the invariant: the gate has to be reached before anything opens the channel.
    ok("and it is reached before the channel is opened",
       agent_src.index("owner.cannot_answer()") < agent_src.index("await run_channel()"))
    ok("it waits rather than exiting, so finishing setup needs no restart",
       re.search(r"while owner\.cannot_answer\(\):\s*\n\s*await asyncio\.sleep", agent_src)
       is not None)

    # A WAY OUT of the setup page. Closing the browser leaves the process running with nothing on
    # screen to say so, and the url that reaches it carries a per-machine token — a closed tab is
    # a lost tab. Checked as text because the page is plain browser JS with no test harness.
    page = (src / "setup.html").read_text()
    ok("the setup page has a cancel button", 'id="doCancel"' in page)
    ok("it stops through the existing /api/quit, not a second path",
       "post('/api/quit'" in page and "/api/quit" in (src / "web.py").read_text())
    ok("it reads the channel state, so it can say whether anything goes off the air",
       "/api/state" in page and "onAir" in page)
    # Both of these have unwired a whole page before: localStorage is a ReferenceError in
    # WebKitGTK, and an unwired form submits natively to `/`, dropping the token.
    ok("the setup page reports JS errors where they can be seen", "window.onerror" in page)
    ok("it touches no localStorage and has no form to submit natively",
       "localStorage" not in page and "<form" not in page)

    # The two facts, one at a time. llm.configured is patched rather than fed a key: a real
    # credential would need a provider SDK, and this suite must run without one.
    real_configured, real_profile = llm.configured, owner.PROFILE
    real_name = os.environ.pop("OWNER_NAME", None)
    owner.PROFILE = TMP / "no-settings.md"          # no name recorded anywhere
    try:
        llm.configured = lambda *a, **k: False
        why = owner.setup_pending()
        ok("no model attached means setup is unfinished", bool(why), why)
        ok("and it says which of the two is missing", "model" in why, why)

        ok("and no model also means it cannot answer anyone", bool(owner.cannot_answer()),
           owner.cannot_answer())

        llm.configured = lambda *a, **k: True
        why = owner.setup_pending()
        # Without this the agent greets strangers as "the owner", which the model has been seen
        # to read as a template and speak aloud.
        ok("a model alone does not finish setup — a name is needed too", bool(why), why)
        ok("and it says so", "owner" in why, why)

        # THE REGRESSION THIS PINS: on 2026-08-03 the daemon gated on setup_pending, so this
        # exact state — a working model, a connector, and a blank name — stopped a secretary
        # that was answering calls. A missing name costs a greeting, not the phone.
        ok("but a model with NO name can still answer, so the channel stays open",
           owner.cannot_answer() == "", owner.cannot_answer())

        os.environ["OWNER_NAME"] = "Tan"
        ok("model plus name finishes setup", owner.setup_pending() == "",
           owner.setup_pending())
    finally:
        llm.configured = real_configured
        owner.PROFILE = real_profile
        os.environ.pop("OWNER_NAME", None)
        if real_name is not None:
            os.environ["OWNER_NAME"] = real_name


def test_schedule() -> None:
    print("\n  -- schedule: conflicts and hours --")
    d = lambda s: datetime.fromisoformat(f"2026-08-01T{s}")

    # Half-open intervals. Closed ones would refuse back-to-back deliveries, which is the
    # normal case, so this is the single most load-bearing line in the module.
    ok("back-to-back slots do not clash",
       not schedule.overlaps(d("19:00"), 30, d("19:30"), 30))
    ok("partial overlap clashes",
       schedule.overlaps(d("19:00"), 30, d("19:15"), 30))
    ok("identical slots clash",
       schedule.overlaps(d("19:00"), 30, d("19:00"), 30))
    ok("a slot inside a longer one clashes",
       schedule.overlaps(d("19:10"), 10, d("19:00"), 60))

    # The bug this pins: 20:50 + 30min ends at 21:20, past a 21:00 close. Checking only the
    # START would have accepted it.
    ok("slot ending after close is refused",
       not schedule.within_hours("2026-08-01T20:50", 30, "11:00-21:00"))
    ok("slot ending exactly at close is allowed",
       schedule.within_hours("2026-08-01T20:30", 30, "11:00-21:00"))
    ok("slot starting exactly at open is allowed",
       schedule.within_hours("2026-08-01T11:00", 30, "11:00-21:00"))
    ok("slot before open is refused",
       not schedule.within_hours("2026-08-01T10:30", 30, "11:00-21:00"))
    ok("slot crossing midnight is refused",
       not schedule.within_hours("2026-08-01T23:50", 30, "11:00-23:59"))
    # Fails OPEN by design: a typo'd bound must not silently refuse every order. The owner
    # still sees the bound listed verbatim, so the mistake is visible.
    ok("malformed hours bound does not block",
       schedule.within_hours("2026-08-01T03:00", 30, "not-a-window"))

    print("\n  -- schedule: booking --")
    row = schedule.book("2026-08-01T19:00", 30, "2 pizzas", "+6591234567")
    eq("book returns the normalised time", row["at"], "2026-08-01T19:00")
    eq("one booking stored", len(schedule.bookings()), 1)
    eq("conflicts finds it", len(schedule.conflicts("2026-08-01T19:15", 30)), 1)
    eq("free slot has no conflict", schedule.conflicts("2026-08-01T19:30", 30), [])

    try:
        schedule.book("2026-08-01T19:10", 30, "clash", "someone")
        ok("double booking raises Conflict", False, "no exception raised")
    except schedule.Conflict as exc:
        ok("double booking raises Conflict", True)
        ok("Conflict names what it clashed with", "2026-08-01T19:00" in str(exc), str(exc))
    eq("failed booking stored nothing", len(schedule.bookings()), 1)

    eq("next_free skips the taken slot",
       schedule.next_free("2026-08-01T19:00", 30, "11:00-21:00"), "2026-08-01T19:30")
    # Nothing fits after close, so it must give up rather than suggest an illegal slot.
    eq("next_free respects closing time",
       schedule.next_free("2026-08-01T20:45", 30, "11:00-21:00"), "")

    eq("day filter matches", len(schedule.bookings("2026-08-01")), 1)
    eq("day filter excludes other days", len(schedule.bookings("2026-08-02")), 0)
    ok("cancel removes it", schedule.cancel(row["id"]))
    eq("cancelled slot is free again", schedule.bookings(), [])
    ok("cancelling an unknown id is a no-op", not schedule.cancel("nope"))


# --------------------------------------------------------------------------
# capabilities — bounded authority
# --------------------------------------------------------------------------
def test_capabilities() -> None:
    print("\n  -- capabilities: declare --")
    out = capabilities.add("test pizza", "taking pizza orders", "book_slot",
                           {"hours": "11:00-21:00", "block_minutes": 30,
                            "max_quantity": 4, "verified_only": True, "radius_km": 5})
    ok("declared", "test_pizza" in capabilities.all_capabilities(), out)
    eq("name is normalised", list(capabilities.all_capabilities()), ["test_pizza"])

    # An unknown action must be refused, not stored: a capability that can never fire is
    # worse than a rejection, because the owner believes the agent gained an ability.
    before = dict(capabilities.all_capabilities())
    msg = capabilities.add("refunds", "issuing refunds", "issue_refund")
    ok("unknown action refused", "Unknown action" in msg, msg)
    eq("unknown action stored nothing", capabilities.all_capabilities(), before)

    ok("checked bound is not marked advisory",
       "advisory" not in capabilities.describe("test_pizza").split("hours")[1].split("\n")[0])
    ok("unknown bound is marked advisory",
       "advisory" in [l for l in capabilities.describe("test_pizza").splitlines()
                      if "radius_km" in l][0])

    print("\n  -- capabilities: bounds are enforced in CODE --")
    B = lambda **kw: capabilities.check_bounds("test_pizza", **kw)
    at_ok = "2026-08-01T19:00"

    ok("inside every bound is allowed", B(verified=True, quantity=2, at=at_ok)[0])
    ok("unverified refused when verified_only",
       not B(verified=False, quantity=2, at=at_ok)[0])
    ok("over max_quantity refused", not B(verified=True, quantity=9, at=at_ok)[0])
    ok("at max_quantity allowed", B(verified=True, quantity=4, at=at_ok)[0])
    ok("outside hours refused",
       not B(verified=True, quantity=1, at="2026-08-01T23:00")[0])
    ok("refusal explains the limit",
       "4" in B(verified=True, quantity=9, at=at_ok)[1],
       B(verified=True, quantity=9, at=at_ok)[1])
    ok("unknown capability refused", not capabilities.check_bounds("nope", verified=True)[0])

    print("\n  -- capabilities: refine --")
    # Numbers arrive as strings over MCP/JSON. Compared as strings, "9" > "4" is True by
    # luck and "10" > "4" is False — silently allowing an over-limit order.
    capabilities.set_bound("test_pizza", "max_quantity", "10")
    eq("string number is coerced to int",
       capabilities.get("test_pizza")["bounds"]["max_quantity"], 10)
    ok("refined limit takes effect", B(verified=True, quantity=9, at=at_ok)[0])
    capabilities.set_bound("test_pizza", "verified_only", "false")
    eq("string bool is coerced",
       capabilities.get("test_pizza")["bounds"]["verified_only"], False)
    ok("unverified now allowed", B(verified=False, quantity=1, at=at_ok)[0])

    capabilities.set_bound("test_pizza", "hours", "")
    ok("bound can be removed",
       "hours" not in capabilities.get("test_pizza")["bounds"])
    ok("removing hours stops the hours check",
       B(verified=False, quantity=1, at="2026-08-01T23:00")[0])

    eq("block_minutes read back", capabilities.block_minutes("test_pizza"), 30)
    eq("block_minutes falls back for unknown", capabilities.block_minutes("nope", 45), 45)

    print("\n  -- capabilities: fails closed --")
    capabilities.add("unbounded", "anything at all", "book_slot", {})
    okk, why = capabilities.check_bounds("unbounded", verified=True, quantity=1, at=at_ok)
    ok("a capability with NO bounds authorises nothing", not okk, why)
    ok("and says why", "no bounds" in why.lower(), why)

    ok("remove withdraws it", "Removed" in capabilities.remove("unbounded"))
    ok("removed capability is gone", capabilities.get("unbounded") is None)


# --------------------------------------------------------------------------
# policy — the regex gates, where phrasing bugs live
# --------------------------------------------------------------------------
def test_capability_disclosure() -> None:
    """A declared capability is a fact the agent may STATE, not only act on."""
    print("\n  -- capabilities as disclosable facts --")
    for name in list(capabilities.all_capabilities()):
        capabilities.remove(name)
    capabilities.add("pizza_delivery", "taking pizza delivery orders", "book_slot",
                     {"hours": "11:00-21:00", "max_quantity": 6, "verified_only": True})
    d = capabilities.disclosable()
    ok("the domain is stated", "taking pizza delivery orders" in d, d)
    # Led with the agent's authority once, and the model answered "No, Stanley does not sell
    # pizza. However, I can arrange a pizza delivery order for you" — in one sentence.
    ok("phrased as a fact about the OWNER, not the agent's authority",
       "owner's business includes" in d, d)
    ok("and closes the inference explicitly", "the answer is YES" in d, d)
    ok("limits ride along, since refusals already state them",
       "11:00-21:00" in d and "up to 6" in d and "verified" in d, d)
    capabilities.remove("pizza_delivery")
    ok("no capabilities -> nothing to disclose", capabilities.disclosable() == "",
       repr(capabilities.disclosable()))


def test_policy() -> None:
    print("\n  -- policy: action gate --")
    gate = lambda q: policy.check(q)

    eq("booking a slot escalates", gate("Can we book a call on Thursday at 3pm?")[1],
       "policy:scheduling")
    eq("let's meet escalates", gate("Shall we meet Thursday?")[1], "policy:scheduling")
    # The other half of the narrowed rule: availability is documented as non-committal
    # precisely so it can be answered. The old rule escalated every phrasing.
    ok("availability question is not an action",
       gate("Are you free Thursday afternoon?")[1] != "policy:scheduling")
    ok("availability question is not gated at all",
       not gate("Are you free Thursday afternoon?")[0])

    # Specific before general: a price ask is a negotiation, not a bare commitment.
    eq("negotiation beats generic commitment",
       gate("Can you agree to a 20% discount?")[1], "policy:negotiation")
    eq("approval is a commitment", gate("Can you approve this for us?")[1],
       "policy:commitment")
    eq("signing is legal binding", gate("Please sign the NDA we sent")[1],
       "policy:legal_binding")

    # Stems, not exact words: `\bprice\b` never matched "pricing", so a whole class of
    # asks sailed through the gate.
    for q in ["Can you give me a discount on that?", "What about your pricing for renewal?"]:
        ok(f"stem matches: {q[:34]}", gate(q)[0] or "pricing" in q,
           f"reason={gate(q)[1]!r}")

    print("\n  -- policy: bare retries resolve to the previous ask --")
    earlier = ["clean up the escalation list", "try again"]
    eq("a bare retry re-asks the original",
       policy.retry_of("try again", earlier), "clean up the escalation list")
    eq("walks past an earlier retry",
       policy.retry_of("again", earlier), "clean up the escalation list")
    for phrasing in ["retry", "once more", "do it again", "Please try again."]:
       ok(f"retry phrasing: {phrasing!r}", bool(policy.retry_of(phrasing, earlier)))
    # A real sentence that happens to contain "again" is not a retry. Getting this wrong
    # would silently replace someone's actual question with an older one.
    for sentence in ["I tried again to reach you last week",
                     "again, what is the price?",
                     "can you check the renewal terms again for the 2026 contract"]:
       ok(f"not a retry: {sentence[:34]!r}", policy.retry_of(sentence, earlier) == "")
    eq("no history means nothing to retry", policy.retry_of("try again", []), "")
    # The real property lives in memory: an owner reply is stored with a placeholder
    # question, and offering that as the thing being retried would re-ask "(owner replied)".
    kr = memory.key("+6500000000", True, "retry")
    memory.append(kr, "what is the price?", "It is $24.", "")
    memory.append(kr, "(owner replied)", "I'll confirm tomorrow.", "owner:delivered")
    eq("recent_questions skips one-sided owner turns",
       memory.recent_questions(kr), ["what is the price?"])
    eq("so a retry resolves past the owner reply",
       policy.retry_of("try again", memory.recent_questions(kr)), "what is the price?")

    print("\n  -- policy: reclassify + TTL --")
    eq("reclassify keeps a stored action reason",
       policy.reclassify("Can you approve this for us?", "policy:commitment"),
       "policy:commitment")
    fresh = datetime.now().isoformat(timespec="seconds")
    old = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    ok("a fresh escalation has not expired",
       not policy.expired("policy:commitment", fresh))
    ok("a very old escalation has expired",
       policy.expired("policy:commitment", old))


# --------------------------------------------------------------------------
# memory — key isolation and the one-sided turn
# --------------------------------------------------------------------------
def test_memory() -> None:
    print("\n  -- memory: keys are isolated --")
    v = memory.key("+6591234567", True, "c1")
    u = memory.key("+6591234567", False, "c1")
    ok("verified and unverified never share a key", v != u, f"{v} vs {u}")
    ok("verified key is marked", v.startswith("v:"))
    ok("unverified key is marked", u.startswith("u:"))
    ok("different conversations differ",
       memory.key("+6591234567", True, "c2") != v)

    print("\n  -- memory: one-sided owner replies --")
    memory.append(v, "what is the price?", "It is $24.", "policy:answered")
    memory.append(v, "(owner replied)", "I'll sign this week.", "owner:delivered")
    turns = memory.turns(v)
    eq("both turns stored", len(turns), 2)
    ok("normal turn is not one-sided", not memory.one_sided(turns[0]))
    ok("delivered reply is one-sided", memory.one_sided(turns[1]))

    prompt = memory.as_prompt(v)
    ok("the agent does not read back a question nobody asked",
       "Them: (owner replied)" not in prompt, prompt)
    ok("but the owner's words are still there",
       "I'll sign this week." in prompt, prompt)
    ok("the normal question is still attributed to them",
       "Them: what is the price?" in prompt, prompt)


def test_knowledge_writes() -> None:
    """Owner-saved facts: where they land, what they may not contradict, where they may not go.

    All three were live defects: one destination for every subject, a fact that could
    contradict an enforced bound, and no boundary on the write path.
    """
    print("\n  -- knowledge writes: destination, bounds, boundary --")
    # Start from ONE capability: the guard only refuses when a bound has a single declared
    # value, because code cannot tell which subject a sentence is about.
    for name in list(capabilities.all_capabilities()):
        capabilities.remove(name)
    capabilities.add("pizza_delivery", "pizza delivery", "book_slot",
                     {"hours": "11:00-21:00", "max_quantity": 6, "radius_km": 5})

    # A fact that AGREES with a bound is documentation, not a conflict.
    for fact in ("We are open on Sunday.", "Last order 20:30.",
                 "We open at 11:00 and close at 21:00.", "Maximum 6 pizzas per order.",
                 "We deliver within 5 km of Tanjong Pagar."):
        out = tools.add_knowledge(fact, file="learned.md")
        ok(f"allowed: {fact}", not out.startswith("NOT saved"), out)

    # A fact that DISAGREES would make the agent say one thing and check_bounds do another.
    for fact, why in (("We are now open till 22:00.", "hours"),
                      ("We now close at 11pm.", "hours on a 12-hour clock"),
                      ("Maximum 8 pizzas per order now.", "max_quantity"),
                      ("We deliver up to 10 km now.", "radius_km")):
        out = tools.add_knowledge(fact, file="learned.md")
        ok(f"refused, conflicts with {why}", out.startswith("NOT saved"), out)
        ok("the refusal names the tool that CAN change it",
           "set_capability_bound" in out, out)

    # Destination routing.
    menu = paths.KNOWLEDGE / "pizza-delivery.md"
    menu.write_text("# Menu\n")
    out = tools.add_knowledge("We now do calzone.", file="pizza-delivery.md")
    ok("writes into the file that owns the subject", "calzone" in menu.read_text(), out)
    out = tools.add_knowledge("A general fact.")
    ok("a blank destination is refused, not defaulted to a catch-all",
       out.startswith("NOT saved"), out)
    ok("and the refusal names the kinds of destination",
       "about the owner" in out and "ONE person" in out, out)

    # The write boundary. Reads may point at real source trees; writes may not.
    outside = TMP / "outside.md"
    outside.write_text("# untouched\n")
    for bad in ("../outside.md", "/etc/evil.md", "newfolder/x.md", "notes.txt"):
        out = tools.add_knowledge("escaped", file=bad)
        ok(f"refused destination {bad}", out.startswith("NOT saved"), out)
    ok("nothing was written outside the knowledge root",
       outside.read_text() == "# untouched\n", outside.read_text())

    # With two capabilities declaring DIFFERENT caps, the fact cannot be attributed, so it is
    # saved with a note instead of refused. Guessing which one the owner meant was worse: it
    # refused a correct fact for disagreeing with an unrelated capability.
    capabilities.add("callback_requests", "arranging callbacks", "book_slot",
                     {"max_quantity": 3})
    # Wording deliberately distinct from the "allowed" fact above: the same sentence is now
    # caught by duplicate detection first, which would pass this check for the wrong reason.
    out = tools.add_knowledge("No more than 6 in a single order.", file="learned.md")
    ok("ambiguous cap: saved, not refused", not out.startswith("NOT saved"), out)
    ok("ambiguous cap: the ambiguity is reported",
       "different max_quantity" in out and "callback_requests" in out, out)
    capabilities.remove("callback_requests")

    # EDITING. Appending a correction left both versions readable, so a fact must be
    # correctable in place — with the exact-and-unique contract that makes that safe.
    doc = paths.KNOWLEDGE / "hours.md"
    doc.write_text("# Hours\n\n- The business is open on Sunday.\n- Deliveries are free.\n")
    out = tools.edit_knowledge("hours.md",
                               "- The business is open on Sunday.",
                               "- The business is closed on Sunday.")
    ok("edit replaces the statement", "closed on Sunday" in doc.read_text(), out)
    ok("and the old version is gone", "open on Sunday" not in doc.read_text(), doc.read_text())
    ok("untouched lines survive", "Deliveries are free." in doc.read_text(), doc.read_text())

    out = tools.edit_knowledge("hours.md", "not present anywhere", "x")
    ok("a snippet that is absent changes nothing", out.startswith("NOT edited"), out)
    ok("and it says to read the file first", "read_knowledge" in out, out)

    doc.write_text("# Hours\n\n- same line\n- same line\n")
    out = tools.edit_knowledge("hours.md", "- same line", "- edited")
    ok("an ambiguous snippet is refused, not guessed",
       out.startswith("NOT edited") and "2 times" in out, out)
    ok("nothing was written on the ambiguous edit",
       doc.read_text().count("- same line") == 2, doc.read_text())

    doc.write_text("# Hours\n\n- delete me\n- keep me\n")
    out = tools.edit_knowledge("hours.md", "- delete me\n", "")
    ok("an empty replacement deletes the fact", "delete me" not in doc.read_text(), out)
    ok("deletion keeps the rest", "keep me" in doc.read_text(), doc.read_text())

    # The bounds guard applies to edits too, or it could be bypassed by editing instead.
    doc.write_text("# Hours\n\n- We close at 21:00.\n")
    out = tools.edit_knowledge("hours.md", "- We close at 21:00.", "- We close at 23:00.")
    ok("an edit cannot contradict a declared bound", out.startswith("NOT edited"), out)

    # Every change to what external parties may be told is recorded, because the edit itself is not.
    doc.write_text("# Hours\n\n- A fact.\n")
    tools.edit_knowledge("hours.md", "- A fact.", "- A corrected fact.")
    logged = [json.loads(l) for l in tools.EDIT_LOG.read_text().splitlines() if l.strip()]
    ok("the edit is journalled with the previous content",
       logged and "- A fact." in logged[-1]["before"], str(logged[-1])[:120] if logged else "no log")

    out = tools.edit_knowledge("../outside.md", "x", "y")
    ok("edits obey the same write boundary", out.startswith("NOT edited"), out)

    # The INDEX has to show a subject stated twice, or the agent corrects one copy and leaves
    # the other — which is exactly what happened before it did.
    permissions.save({"default": {"folders": ["knowledge"]}, "askers": {}})
    (paths.KNOWLEDGE / "a.md").write_text(
        "# A\n\n- AgentDuet supports three channels: voice, WhatsApp and DDUET web chat.\n")
    (paths.KNOWLEDGE / "b.md").write_text(
        "# B\n\n- AgentDuet supports four channels: voice, WhatsApp, DDUET web chat and SMS.\n")
    idx = tools.list_knowledge()
    ok("the index flags a subject stated in two documents", "SAME SUBJECT" in idx, idx[-400:])
    ok("and says to consolidate rather than keep both",
       "consolidate" in idx and "delete the other" in idx, idx[-400:])

    for f in ("a.md", "b.md"):
        (paths.KNOWLEDGE / f).unlink()

    # owner.md is a knowledge document like any other: the instructions/facts split was not a
    # real mechanism (the file is parsed field by field, never injected as prose), so excluding
    # it from retrieval only meant the owner's own facts could not be found.
    from dduet_desktop import folder_index
    (paths.KNOWLEDGE / "owner.md").write_text("# Owner\n\n## Who\n- Runs a bakery.\n")
    indexed = [q.name for q in folder_index.files_under(paths.KNOWLEDGE)]
    ok("owner.md is indexed like any other document", "owner.md" in indexed, str(indexed))

    # Visibility is reported, because it is the disclosure decision.
    permissions.save({"default": {"folders": ["knowledge"]}, "askers": {}})
    pub = tools.add_knowledge("A brand new unrelated subject: kites.", file="about.md")
    ok("a write states who can read it", "anyone who writes in" in pub, pub)


def main() -> None:
    print("\n  Model-free rules — bounds, conflicts, gates. No API calls, no cost.")
    test_no_undefined_names()
    test_prompts()
    test_asker_tool_surface()
    test_hosts()
    test_setup_mode()
    test_schedule()
    test_capabilities()
    test_capability_disclosure()
    test_policy()
    test_memory()
    test_knowledge_writes()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n  {PASS} passed, {FAIL} failed")
    if FAILED:
        print("  failing: " + "; ".join(FAILED))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
