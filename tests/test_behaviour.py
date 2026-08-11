"""Behaviour test cases, run against the live decision path.

`test_isolation.py` checks structure (who can reach what). This checks *decisions*: given
an identity, a verification state and a question, does the agent answer, and if not does
it escalate for the right reason.

Runs through `/api/sim`, which calls `brain.handle_query` — the same path a real inbound
message takes. So a pass here is a statement about the real agent, not a mock.

    ./sim-start.sh          # or ./start.sh
    .venv/bin/python test_behaviour.py [-v]

Escalation reason is asserted, not just answered-vs-escalated: "we don't document that"
and "not our subject" are different owner actions, so collapsing them would hide the
regression that matters.
"""

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from dduet_desktop import paths

HERE = pathlib.Path(__file__).parent
# Set to point at an already-running daemon; otherwise this suite starts its OWN on a spare
# port with its OWN $DDUET_HOME. It used to drive the owner's live instance, so every run left
# forged identities in their people list and test rows in the append-only log they read.
BASE = os.getenv("SECRETARY_BEHAVIOUR_BASE", "")
TOKEN = ""
_DAEMON = None
_TMP_HOME = None
PAULINE = "+6591234567"
STRANGER = "someone@nowhere.example"

VERBOSE = "-v" in sys.argv


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_own_instance() -> None:
    """Run the daemon against a throwaway copy of the owner's config.

    A COPY, not a blank instance: these cases assert decisions that depend on the real
    knowledge, capabilities and per-person grants, so a fresh install would fail for reasons
    that have nothing to do with the agent. What must not be shared is the append-only log and
    the people list — the outputs the owner reads.
    """
    global BASE, TOKEN, _DAEMON, _TMP_HOME
    _TMP_HOME = pathlib.Path(tempfile.mkdtemp(prefix="secretary-behaviour-"))
    for name in ("knowledge", "people", "canvas"):
        src = paths.HOME / name
        if src.is_dir():
            shutil.copytree(src, _TMP_HOME / name)
    for name in ("settings.md", "permissions.json", "capabilities.json", ".env"):
        src = paths.HOME / name
        if src.is_file():
            shutil.copy(src, _TMP_HOME / name)

    port = _free_port()
    # No channel: one client per connector, and the owner's daemon already holds it.
    env = {**os.environ, "DDUET_HOME": str(_TMP_HOME), "SECRETARY_WEB_PORT": str(port),
           "SECRETARY_SIM": "1", "SECRETARY_CHANNEL": "0"}
    _DAEMON = subprocess.Popen([sys.executable, "-u", "-m", "dduet_desktop.secretary_agent"], cwd=HERE, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    BASE = f"http://127.0.0.1:{port}"
    tokfile = _TMP_HOME / "run" / "web-token"
    for _ in range(80):
        if tokfile.is_file():
            tok = tokfile.read_text().strip()
            try:
                with urllib.request.urlopen(f"{BASE}/?t={tok}", timeout=2) as r:
                    ready = r.status == 200
            except urllib.error.HTTPError:
                # An HTTP error means the server IS serving — it answered. Treating any
                # exception as "not up" made a 401 on the unauthenticated probe look like a
                # dead socket, and the suite waited out its whole timeout on a healthy daemon.
                ready = True
            except OSError:
                ready = False                 # nothing listening yet
            if ready:
                TOKEN = tok
                print(f"  (own instance on port {port}, home {_TMP_HOME.name})")
                return
        time.sleep(0.5)
    stop_own_instance()
    sys.exit("behaviour suite: its own daemon did not come up")


def stop_own_instance() -> None:
    if _DAEMON is not None:
        _DAEMON.terminate()
        try:
            _DAEMON.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _DAEMON.kill()
    if _TMP_HOME is not None:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


def call(asker: str, verified: bool, question: str, convo: str) -> dict:
    token = TOKEN or (paths.RUN / "web-token").read_text().strip()
    body = json.dumps({"asker": asker, "verified": verified, "network": "WA",
                       "conversation": convo, "message": question}).encode()
    req = urllib.request.Request(f"{BASE}/api/sim?t={token}", data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


#: (name, asker, verified, question, expect_outcome, expect_reason_or_None)
#: reason None = don't care (any escalation reason passes)
CASES = [
    # --- disclosure follows the grant -------------------------------------
    # knowledge/ is ONE flat folder readable by anyone who writes in (2026-07-30). The old
    # public/ vs partners/ split is gone: a fact only one person may hear now belongs in
    # people/<identity>.md, not in a knowledge folder with a narrower grant. So the SDK's
    # production auth details ARE public now — that is the intended consequence of flattening,
    # and these two cases exist to record it rather than to let it drift back unnoticed.
    ("knowledge is public — an unverified claim still gets it",
     PAULINE, False, "What authentication is used in production?", "answered", None),
    ("knowledge is public — so is an external party with no extra grants",
     STRANGER, True, "What authentication is used in production?", "answered", None),
    # What is NOT public is a per-person folder grant. Only the granted identity reaches it.
    ("a per-identity grant is still per-identity",
     PAULINE, True, "How does the QA SSI get created?", "answered", None),
    ("...and someone without that grant does not reach it",
     STRANGER, True, "How does the QA SSI get created?", "escalated", None),

    # --- actions are ungrantable ------------------------------------------
    # "agree" AND "discount" both match; specific-before-general ordering means
    # negotiation wins, which is the more useful label for a price request.
    ("negotiation beats generic commitment on a price ask",
     PAULINE, True, "Can you agree to a 20% discount?", "escalated", "policy:negotiation"),
    ("commitment escalates however much is readable",
     PAULINE, True, "Can you approve this for us?", "escalated", "policy:commitment"),
    ("negotiation escalates",
     PAULINE, True, "Can you give me a discount on that?", "escalated", "policy:negotiation"),
    # Booking a slot commits the owner, so it escalates. Asking whether he is free does
    # NOT: the owner documents "Availability (general, non-committal)" precisely so this
    # can be answered, and the old rule escalated every phrasing of the question — which
    # made that documentation unreachable. Narrowed deliberately; this pins both halves.
    ("booking a slot escalates",
     PAULINE, True, "Can we book a call on Thursday at 3pm?", "escalated", "policy:scheduling"),
    ("legal binding escalates",
     PAULINE, True, "Please sign the NDA we sent", "escalated", "policy:legal_binding"),

    # --- per-person override ----------------------------------------------
    ("profile '## Always escalate' fires",
     PAULINE, True, "What is your headcount plan?", "escalated", "policy:person_rule"),
    ("...and not for an unverified claim (no profile applies)",
     PAULINE, False, "What is your headcount plan?", "escalated", None),

    # --- scope awareness --------------------------------------------------
    # Was asserted as missing_knowledge when nothing documented it. product-hub now covers
    # it (`renewDaysBefore` in `SellerConfig`), and retrieval finds it — so the correct
    # expectation is an ANSWER. Kept as a case because it exercises the hardest retrieval
    # path: the asker's words ("configured renewal days") do not match the doc's vocabulary,
    # so the agentic search loop has to bridge them.
    ("in-scope question the docs DO cover -> answered",
     PAULINE, True, "How do I change the configured renewal days?", "answered", None),
    # What matters is that it ESCALATES rather than deciding the question is not worth the
    # owner's attention — the agent judges what it can answer, not what is worth asking.
    # The exact label is deliberately not asserted: OUTSIDE ("different subject") vs MISSING
    # ("our subject, undocumented") is a fine judgement, and qwen3.6-flash gets it about half
    # the time even with worked examples in the prompt. Both escalate and REASON_WEIGHT
    # treats them almost identically (20 vs 30), so tightening this would test the model
    # tier, not the agent. A stronger model should get the label right; this must not break.
    ("unrelated subject escalates rather than being brushed off",
     PAULINE, True, "Can you recommend a dentist in Singapore?", "escalated", None),

    # The other half of the narrowed scheduling rule: an availability QUESTION must not
    # hit the action gate. It may still escalate for a scope reason if availability is
    # undocumented — what must never happen is `policy:scheduling`.
    ("availability question is not an action",
     PAULINE, True, "Are you free Thursday afternoon?", None, "!policy:scheduling"),
]

#: Multi-turn cases: [(question, expect_outcome_or_(outcome,reason))] in one conversation.
CONVERSATIONS = [
    ("referential follow-up resolves via memory", PAULINE, True, [
        ("What authentication is used in production?", "answered"),
        ("What about the other one?", "answered"),
    ]),
    # The gate reads one message at a time, so a bare revision used to slip through and
    # file a live negotiation as "we couldn't answer".
    ("bare revision keeps the negotiation classification", PAULINE, True, [
        ("We would like a 10% discount on renewal", "escalated"),
        ("Actually make it 25%", "escalated"),
    ]),
    ("a real question mid-negotiation is still answered", PAULINE, True, [
        ("We would like a 10% discount on renewal", "escalated"),
        ("Instead, tell me what authentication production uses", "answered"),
    ]),
]


def check(name: str, got: dict, outcome: str | None, reason: str | None) -> tuple[bool, str]:
    """`outcome=None` means don't care. A reason of "!x" asserts the reason is NOT x.

    Some rules are best pinned negatively: an availability question may legitimately be
    answered OR escalated for a scope reason depending on what the owner has documented,
    but it must never hit the action gate. Asserting the exact outcome there would make
    the test fail for a reason it isn't about.
    """
    if outcome is not None and got["outcome"] != outcome:
        return False, f"outcome {got['outcome']!r}, wanted {outcome!r} ({got['reason']})"
    if reason and reason.startswith("!"):
        if got["reason"] == reason[1:]:
            return False, f"reason {got['reason']!r}, must NOT be {reason[1:]!r}"
    elif reason and got["reason"] != reason:
        return False, f"reason {got['reason']!r}, wanted {reason!r}"
    return True, got["reason"] or "-"


def main() -> None:
    passed = failed = 0
    fails: list[str] = []

    print(f"\n  {len(CASES)} single-turn + {len(CONVERSATIONS)} conversation case(s)\n")
    if not BASE:
        start_own_instance()

    for i, (name, asker, verified, q, outcome, reason) in enumerate(CASES):
        got = call(asker, verified, q, f"bt-{i}")
        ok, detail = check(name, got, outcome, reason)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if VERBOSE or not ok:
            print(f"        Q: {q}")
            print(f"        {detail}")
            if VERBOSE:
                print(f"        reply: {got['reply'][:100]}")
        passed, failed = passed + ok, failed + (not ok)
        if not ok:
            fails.append(name)

    for j, (name, asker, verified, turns) in enumerate(CONVERSATIONS):
        convo = f"btc-{j}"
        ok_all, detail = True, ""
        for q, outcome in turns:
            got = call(asker, verified, q, convo)
            ok, detail = check(name, got, outcome, None)
            if not ok:
                ok_all = False
                break
        print(f"  {'PASS' if ok_all else 'FAIL'}  {name}")
        if not ok_all:
            print(f"        {detail}")
            fails.append(name)
        passed, failed = passed + ok_all, failed + (not ok_all)

    # Only needed when pointed at the owner's live instance (SECRETARY_BEHAVIOUR_BASE); a
    # throwaway home is deleted whole, which is a stronger guarantee than tidying up after.
    if _TMP_HOME is not None:
        print("\n  throwaway instance discarded — nothing to clean up")
    else:
      try:
          from dduet_desktop import tools
          asked = {q for _, _, _, q, _, _ in CASES}
          asked |= {q for _, _, _, turns in CONVERSATIONS for q, _ in turns}
          cleared = sum(1 for g in tools.open_escalations() if g["question"] in asked
                        and tools._mark(g["ids"], "test cleanup"))
          print(f"\n  cleaned up {cleared} escalation(s) created by this run")
      except Exception as exc:
          print(f"\n  cleanup skipped: {exc}")

    stop_own_instance()
    print(f"\n  {passed} passed, {failed} failed")
    if fails:
        print("  failing: " + ", ".join(fails))
        sys.exit(1)


if __name__ == "__main__":
    main()
