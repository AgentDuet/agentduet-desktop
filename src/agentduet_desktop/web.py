"""Local owner site — the second face over tools.py.

Served BY the daemon, so it is up exactly when the agent is (unlike MCP, which only
exists while a host app is open). Gives you what chat is bad at — visible state — plus
a prompt window, plus live push when something escalates.

SECURITY
- Binds 127.0.0.1 only. This site can grant folder access and send messages as the
  owner; exposed on 0.0.0.0 it is a privilege-escalation target on any shared network.
- Token generated per run, written to .run/web-token, required on every request.
- Uses tools.OWNER_TOOLS. The external-facing path never reaches this module.

The owner assistant is a DIFFERENT agent from the external-facing one: different system
prompt, full access, and the owner tool registry. Never share that code path.
"""
from __future__ import annotations


import asyncio
import json
import logging
import os
import signal
import pathlib
import re
import secrets
from datetime import datetime

from aiohttp import WSMsgType, web

from . import capabilities
from . import llm
from . import owner
from . import tools

from . import paths

logger = logging.getLogger("secretary.web")

HERE = pathlib.Path(__file__).parent      # install dir: web.html / sim.html
RUN = paths.RUN
TOKEN_FILE = RUN / "web-token"
HOST, PORT = "127.0.0.1", int(os.getenv("SECRETARY_WEB_PORT", "8899"))

OWNER_PROMPT = """You are the owner's assistant for their desktop secretary.

%s

You help the OWNER manage an agent that answers external parties on their behalf. You are NOT
the external-facing agent: you have full access and may grant permissions.

You have tools. To use one, reply with ONLY this JSON and nothing else:
  {"tool": "<name>", "args": {...}}
After you see the result, either call another tool or answer the owner in plain text.
To answer directly, just write the answer — no JSON.

Be brief. When you show escalations, suggest the concrete next step (grant_folder for a
recurring topic, add_knowledge for a fact, draft_reply to compose an answer).

DRAFTING IS NOT SENDING. If the owner says draft, write, compose, suggest or "what would you
say" — use draft_reply. It cannot send. Use reply_to ONLY when they ask you to send, reply,
tell them or answer them. Sending is outward-facing and closes their thread, so it needs to
have been asked for. When a draft is already waiting, send THAT text rather than a new one.

KNOWLEDGE — you have READ and WRITE. The external-facing agent has READ ONLY: it answers from
these documents and can never change them. You read to know the context and write to improve
it, so keeping the documents correct is your job, not a side effect. Do not accumulate versions
of the truth.

  1. list_knowledge — the index: what each document asserts, and any subject stated in two of
     them. Find the document that already owns the subject.
  2. read_knowledge — look at what it currently says. Never write blind.
  3. Then EITHER edit_knowledge to correct or delete the existing statement,
     OR add_knowledge(fact, file=...) if the subject is genuinely new.
  4. Never describe what you are about to do — do it, then report what you did. A plan is not
     an action, and the owner reads it as one.
  5. If the index flags the subject in another document, deal with BOTH in the same turn —
     follow the verdict on that line. Fixing one copy and leaving the other is how the
     knowledge base comes to hold two answers to one question.

HOW TO STRUCTURE A DOCUMENT — the shape decides whether a fact can be corrected later:
  - ONE assertion per `- ` bullet. A fact buried in a paragraph can only be appended to or
    rewritten wholesale; a bullet can be replaced exactly.
  - Group bullets under a `## ` heading naming the subject, and put a new fact under the
    heading that owns it — never at the end of the file, which lands it under whatever
    section happens to be last.
  - One subject, one document. Detail that only some askers may see belongs in a document
    only they can read, not in a public one hedged with conditions.

When the owner corrects, reverses or updates something ("actually", "no", "we changed"), the
right move is almost always EDIT, not add. Appending leaves both versions readable and the
agent will answer with whichever one retrieval happens to surface — which is how a knowledge
base ends up asserting a fact and its opposite. If a statement is simply wrong, delete it by
passing an empty `new`.

Write the ANSWER, never the question, and phrase it in the words an asker would use: a fact
recorded in the owner's wording often fails to match how it is asked.

ASK WHEN THE SUBJECT IS UNCLEAR. One sentence can belong to more than one of the subjects below,
and a wrong guess gets written down and then answered to external parties. If you cannot tell which
subject, which person, or which open thread the owner means, ask ONE short question instead.

THIS OWNER'S SUBJECTS:
%s

TOOLS:
%s
"""


def _subjects() -> str:
    """What this install's knowledge is ABOUT, read from the documents themselves.

    Derived, not written down: naming the sample's domains ("a pizzeria, a software product")
    in the framework prompt would be sample content in framework code, and wrong for every
    other install.
    """
    root = paths.KNOWLEDGE
    if not root.is_dir():
        return "- (no knowledge documents yet)"
    out = []
    for f in sorted(root.rglob("*.md")):
        head = next((l.lstrip("# ").strip() for l in f.read_text().splitlines()
                     if l.startswith("#")), f.stem)
        out.append(f"- {head}  ({f.relative_to(root.parent).as_posix()})")
    return "\n".join(out) or "- (no knowledge documents yet)"


def _tool_docs() -> str:
    lines = []
    for name, (fn, params) in tools.OWNER_TOOLS.items():
        args = ", ".join(f"{k} ({v})" for k, v in params.items()) or "no arguments"
        # A tool with no docstring used to raise IndexError here, and the whole owner site
        # failed to bind over it — reported as one warning line while the channel connected
        # normally, so the daemon looked healthy. Prompt assembly must not be able to do that.
        doc = ((fn.__doc__ or "").strip().splitlines() or ["(undocumented)"])[0]
        lines.append(f"- {name}: {doc}\n    args: {args}")
    return "\n".join(lines)


class OwnerChat:
    """Minimal tool-calling loop.

    Deliberately provider-neutral: the model returns a JSON action rather than using a
    vendor function-calling API, so swapping models doesn't rewrite this. A production
    build would use native function calling.
    """

    # How many earlier lines of the owner's own conversation to carry. The point of this
    # surface is to think a reply through — "who is waiting?", "draft something", "send
    # that" — and each `turn` used to start from nothing, so "that" referred to nothing and
    # the third step failed. Trimmed rather than unbounded: tool results are verbose and
    # the whole thing is re-sent every turn.
    KEEP = 30

    #: The owner's own chat, persisted. It used to live only in this object, so a page reload
    #: showed an empty panel and a daemon restart genuinely lost it — while the asker side has
    #: had a restorable transcript all along. The owner's thinking is worth at least as much.
    STORE = paths.RUN / "owner_chat.json"

    def __init__(self, model: str):
        # Through the same seam brain uses, so the owner surface follows whatever provider
        # is attached. The JSON-action protocol below was already provider-neutral; this
        # was the last line in the file that named a vendor.
        self.client = llm.client(model)
        self.model = model
        self.system = OWNER_PROMPT % (owner.identity_block(), _subjects(), _tool_docs())
        self.history: list[str] = []
        self.shown: list[dict] = self._load()      # what the page renders, oldest first
        # Reconstruct the model's own history from the visible turns, so a restart does not
        # also lose the thread of the conversation ("send that" still resolves).
        for turn in self.shown[-self.KEEP // 2:]:
            self.history += [f"OWNER: {turn['q']}", f"ASSISTANT: {turn['a']}"]
        self.history = self.history[-self.KEEP:]

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.STORE.read_text())
        except (OSError, json.JSONDecodeError):
            return []

    def _record(self, question: str, answer: str, used: list[str], full: str = "") -> None:
        """Append one visible turn. Tool results are deliberately NOT stored — they are
        diagnostics, they are large, and they are stale the moment the queue changes.

        `full` is the real prompt when `question` is a stand-in for it (setup). Kept so debug
        can still show what was actually sent, but never rendered by default: an internal
        instruction block displayed as if the owner typed it is not their conversation.
        """
        turn = {"q": question, "a": answer, "tools": used,
                "at": datetime.now().isoformat(timespec="seconds")}
        if full and full != question:
            turn["q_full"] = full
        self.shown = (self.shown + [turn])[-60:]
        try:
            self.STORE.parent.mkdir(parents=True, exist_ok=True)
            self.STORE.write_text(json.dumps(self.shown, indent=2))
        except OSError as exc:
            logger.warning("could not persist owner chat: %s", exc)

    async def _ask(self, history: list[str], context: str = "") -> str:
        # Context rides on the SYSTEM side, not in history: it is regenerated per turn from
        # live state, so storing it would accumulate stale copies of the same queue.
        text = self.system + ("\n\n" + context if context else "") + "\n\n" + "\n".join(history)
        return await asyncio.to_thread(self.client.complete, text)

    # A completed action claimed in prose. The assistant answered "I have updated the
    # knowledge base to reflect that you are closed next Monday" having called no tool at all —
    # nothing was written, and the owner had every reason to believe it was. Worse than a
    # refusal: the owner stops thinking about it. So the claim is checked against what actually
    # ran. Past tense only, so "I would add X" and "shall I save it?" do not trip it.
    # The inverse failure: a plan instead of an action. Shown the conflicting documents, the
    # assistant replied "I need to: 1. correct learned.md 2. check about.md" and called nothing,
    # so the contradiction it had just found survived. A clarifying question is NOT this — those
    # end in a question mark and are allowed, since asking is sometimes the right move.
    INTENT = re.compile(r"\bI (?:need to|will|am going to|should|plan to)\b|"
                        r"\bnext,?\s+I\b|\bLet me\b", re.I)

    CLAIMED = re.compile(
        r"\bI(?:'ve| have)?\s+(?:just\s+)?"
        r"(added|updated|saved|stored|recorded|noted|written|sent|replied|granted|revoked|"
        r"resolved|closed|booked|cancelled|removed|deleted)\b", re.I)

    async def turn(self, message: str, viewing: str = "", focus: str = "",
                   label: str = "") -> dict:
        """One owner turn. `label` is what gets REMEMBERED in place of `message`.

        Setup drives this with a 3 KB instruction block. Recording that verbatim put the whole
        internal prompt in the owner's visible chat history AND in the rolling history fed back
        to the model, so later questions were answered with setup instructions still in context.
        The model still receives `message`; only what is stored is replaced.
        """
        shown_as = label or message
        # Hand over what the assistant would otherwise have to ask for — the person's open
        # threads, their briefings (including the draft the agent already wrote), and the
        # recent exchange. Drafting used to run with `tools: []` and invent figures.
        context = ""
        if viewing:
            context = tools.owner_context(viewing, focus=focus)
            if context:
                context += (f"\n\nIf the owner says \"her\", \"him\", \"them\" or \"this "
                            f"person\" without naming anyone, they mean {viewing}.")
        history = self.history + [f"OWNER: {message}"]
        used: list[str] = []
        nudged = False

        def remember(h):
            # What the model SAW is not what we keep. A 3 KB setup instruction block would
            # otherwise ride along in every later turn's context.
            kept = [f"OWNER: {shown_as}" if x == f"OWNER: {message}" else x for x in h]
            self.history = kept[-self.KEEP:]

        for _ in range(8):                       # bounded tool loop — list+read+edit twice is 5
            out = await self._ask(history, context)
            action = self._parse(out)
            if not action:
                if (not nudged and self.INTENT.search(out) and "?" not in out[-200:]):
                    nudged = True
                    history += [f"ASSISTANT: {out}",
                                "TOOL_RESULT: You described what you intend to do but called no "
                                "tool, so nothing happened. Do it now — emit the tool JSON. "
                                "Afterwards report only what you actually did."]
                    continue
                if not used and self.CLAIMED.search(out):
                    out += ("\n\n[nothing actually happened — no tool ran this turn, so nothing "
                            "was saved, sent or changed. Ask again to have it done.]")
                remember(history + [f"ASSISTANT: {out}"])
                self._record(shown_as, out, used, full=message)
                return {"reply": out, "tools": used}

            # Every call in the reply, in the order given. One-at-a-time silently discarded
            # the rest of a batched reply.
            for name, args in action:
                entry = tools.OWNER_TOOLS.get(name)
                if not entry:
                    history.append(f"TOOL_RESULT: no such tool '{name}'")
                    continue
                try:
                    result = entry[0](**args)
                except Exception as exc:         # surface, don't crash the page
                    result = f"tool error: {exc}"
                used.append(name)
                history.append(f"ASSISTANT: called {name}")
                history.append(f"TOOL_RESULT: {result}")
            continue

        final = await self._ask(history + ["(answer the owner now)"], context)
        remember(history + [f"ASSISTANT: {final}"])
        self._record(shown_as, final, used, full=message)
        return {"reply": final, "tools": used}

    @staticmethod
    def _parse(text: str):
        """Every tool call in one reply, in order — or [] if it is prose.

        Was one-call-only: a reply containing several calls parsed as None and came back to the
        owner as prose, so NOTHING ran. The setup interview exposed it because writing a name, a
        pronoun, a never-say list and two facts is naturally five calls, and the model emitted
        them together. Any multi-step owner request has the same shape.
        """
        t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out, dec = [], json.JSONDecoder()
        i = 0
        while i < len(t):
            j = t.find("{", i)
            if j < 0:
                break
            try:
                obj, end = dec.raw_decode(t, j)
            except json.JSONDecodeError:
                i = j + 1
                continue
            if isinstance(obj, dict) and "tool" in obj:
                out.append((obj["tool"], obj.get("args", {}) or {}))
            i = end
        return out


def make_app(chat: "OwnerChat | None", token: str) -> web.Application:
    # Built once at startup, `chat` stayed None for the life of a FIRST RUN — no model exists
    # yet, so the setup interview could never run in the session that attached one. Everything
    # below goes through _chat(), built on demand and reset when the credential changes (which
    # also makes switching models later take effect without a restart).
    state = {"chat": chat}

    def _chat():
        if state["chat"] is None:
            model = os.getenv("SECRETARY_MODEL", "")
            if model and llm.client(model):
                state["chat"] = OwnerChat(model)
        return state["chat"]

    def _forget_chat():
        state["chat"] = None

    sockets: set[web.WebSocketResponse] = set()          # owner view — full state
    asker_sockets: set[web.WebSocketResponse] = set()    # asker side — pings only

    def authed(request) -> bool:
        return secrets.compare_digest(
            request.query.get("t", "") or request.headers.get("X-Token", ""), token)

    def needs_setup() -> bool:
        """True until the owner has both a working model and a name.

        Checked on every load rather than from a flag file, so a half-finished setup resumes
        instead of stranding the owner on a dashboard that cannot work.

        `setup_pending`, which is deliberately a WIDER test than the daemon's `cannot_answer`:
        showing a setup page to someone who did not need it costs a click, whereas closing the
        channel costs every call, so only the daemon's narrower question may do that. They were
        one function until a blank name took a live secretary off the air — see
        `owner.cannot_answer`.

        `deep=True` because a page load can afford one real call to the model, and a key that is
        present but rejected must not be shown a dashboard.

        THE MARKER EXISTS BECAUSE CARRYING NEEDS NOTHING. `setup_pending` answers "is anything
        missing that would stop this working", and for the recorder the honest answer on a brand
        new install is "no" — carrying needs no model, and since the name became answer-only it
        needs no name either. So a fresh install went straight to the dashboard and the welcome
        screen was unreachable. Finishing setup is now a thing the owner DID, not a state we
        infer from configuration.

        Still not a flag file alone: an instance that predates the marker, or one whose marker is
        lost, falls back to the old question so an already-configured owner is not sent through
        setup again. A connector is the test there, being the one thing nobody has by accident.
        """
        if (paths.RUN / "setup-done").exists():
            return False
        from . import connector
        return bool(owner.setup_pending(deep=True)) or not connector.configured()

    async def index(request):
        if not authed(request):
            return web.Response(status=401, text="bad or missing token")
        page = "setup.html" if needs_setup() else "web.html"
        return web.Response(text=(HERE / page).read_text(), content_type="text/html")

    async def setup_page(request):
        """The first-run WIZARD. Reachable later too — it reconciles rather than duplicating."""
        if not authed(request):
            return web.Response(status=401, text="bad or missing token")
        return web.Response(text=(HERE / "setup.html").read_text(), content_type="text/html")

    async def settings_page(request):
        """Changing things afterwards: direct fields, no steps, no welcome.

        Separate from the wizard because they are different jobs. The wizard is an interview
        that lets the MODEL write prose; settings is a form where CODE writes exactly what was
        typed. Merging them made one page apologise for being both.
        """
        if not authed(request):
            return web.Response(status=401, text="bad or missing token")
        return web.Response(text=(HERE / "settings.html").read_text(), content_type="text/html")

    async def app_css(request):
        """The house style, shared by every page.

        NO TOKEN. It is a stylesheet with nothing in it worth protecting, and requiring one
        would mean the browser could not cache it across the pages that link it. The site binds
        loopback only regardless.
        """
        return web.Response(text=(HERE / "app.css").read_text(), content_type="text/css",
                            headers={"Cache-Control": "no-cache"})

    async def secretary_page(request):
        """The secretary's own view — people, threads, escalations.

        Still here, and still the whole of the second product. `/` is the recorder's hub now
        because that is what a new install IS (see CLAUDE.md, "Two products, one binary"), not
        because this stopped working.
        """
        if not authed(request):
            return web.Response(text="unauthorised", status=401)
        return web.Response(text=(HERE / "secretary.html").read_text(), content_type="text/html")

    async def api_setup_about(request):
        """Who the owner is — WITHOUT needing a model.

        GET returns what is recorded, so a second run edits rather than duplicates. POST writes
        name/pronoun/phone through set_setting (settings.md, parsed by heading) and the one
        free-text answer into knowledge/owner.md under `## Who`.

        THE POINT IS THAT NO MODEL IS INVOLVED. `/api/setup/interview` exists and phrases this
        better, but it runs the answers through the LLM — so on an install with no credential it
        cannot run at all, and this step is exactly the one an owner recording calls still needs.
        A sentence written verbatim is worth more than a better sentence they cannot reach.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import owner as owner_settings, paths as _paths
        if request.method == "GET":
            import re as _re
            who = ""
            if _paths.KNOWLEDGE.joinpath("owner.md").is_file():
                m = _re.search(r"^##\s+Who\s*$(.*?)(?=^##\s|\Z)",
                               _paths.KNOWLEDGE.joinpath("owner.md").read_text(), _re.S | _re.M)
                # Strip the template's guidance comment, or the box prefills with instructions.
                who = _re.sub(r"<!--.*?-->", "", m.group(1) if m else "", flags=_re.S).strip()
                # WITHOUT THE BULLETS. They are storage, not what the owner typed — handing
                # "- I run …" back to the textarea makes the next save write "- - I run …".
                who = "\n".join(l.strip().lstrip("-•").strip() for l in who.splitlines()
                                if l.strip())
            name = owner_settings.name()
            return web.json_response({
                # DEFAULT_NAME is a fallback, not an answer — prefilling "the owner" would look
                # like a recorded choice and get saved back as one.
                "name": "" if name == owner_settings.DEFAULT_NAME else name,
                "pronoun": owner_settings.pronoun_raw(), "phone": owner_settings.phone(),
                "does": who, "calls": owner_settings.calls()})

        body = await request.json()
        done, problems = [], []
        for field in ("name", "pronoun", "phone"):
            value = (body.get(field) or "").strip()
            if not value:
                continue          # blank means "leave it", never "clear it"
            out = tools.set_setting(field, value)
            (problems if out.lower().startswith("unknown") else done).append(out)
        # UNCHANGED MEANS UNTOUCHED. add_knowledge APPENDS, so re-saving a prefilled form
        # would file the same sentence twice and the agent would answer from whichever surfaced
        # first. Comparing against what is recorded makes reopening setup and pressing Save a
        # no-op, which is what anyone would expect it to be.
        #
        # A CHANGED answer still appends rather than replacing. Reconciling a rewrite is what
        # /api/setup/interview does, and it needs a model — so on this path the honest behaviour
        # is to add, and to say so here rather than imply an edit that does not happen.
        current = ""
        if _paths.KNOWLEDGE.joinpath("owner.md").is_file():
            import re as _re2
            m2 = _re2.search(r"^##\s+Who\s*$(.*?)(?=^##\s|\Z)",
                             _paths.KNOWLEDGE.joinpath("owner.md").read_text(), _re2.S | _re2.M)
            current = _re2.sub(r"<!--.*?-->", "", m2.group(1) if m2 else "", flags=_re2.S)
            current = " ".join(l.strip().lstrip("-•").strip() for l in current.splitlines()
                               if l.strip())
        does = (body.get("does") or "").strip()
        if does and does not in current:
            # Positional order is (fact, file, section) — and the section matters: without it the
            # bullet lands under whatever heading happens to be last, which for owner.md is
            # Availability. A sentence about what you do, filed as when you are free, is worse
            # than not filing it.
            out = tools.add_knowledge(does, "owner.md", "Who")   # the FILENAME, extension and all
            # add_knowledge signals refusal with a "NOT saved." prefix, which is the whole
            # failure vocabulary here — matching anything looser would swallow a real refusal.
            (problems if out.startswith("NOT saved") else done).append(out)
        if problems:
            return web.json_response({"ok": False, "message": "; ".join(problems)})
        return web.json_response({"ok": True, "message": "Saved. It knows who it works for now."})

    #: What a page calls each setting. `tools.set_setting` answers the ASSISTANT — its message
    #: names the file and warns that settings are never quoted to anyone, which is guidance the
    #: model needs and a person reading a form does not. Shown on screen it reads as debug
    #: output leaking through, so the page gets its own sentence.
    _SETTING_LABEL = {"name": "Your name", "pronoun": "Your pronoun", "phone": "Your phone",
                      "never_say": "The never-say list", "calls": "What happens to a call",
                      "record_calls": "Call recording", "language": "Language",
                      "transcription": "Transcription quality"}

    async def api_setup_setting(request):
        """Set one owner setting directly — no model involved."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        field = (body.get("field") or "").strip()
        value = body.get("value") or ""

        # Hardware capability gating check for transcription quality
        if field.lower().replace(" ", "_").replace("-", "_") == "transcription":
            from . import hardware
            val_str = str(value).strip().lower()
            model_id = hardware.TIER_TO_MODEL.get(val_str, val_str)
            cap = hardware.check_capability(model_id)
            if not cap["can_load"]:
                return web.json_response({
                    "ok": False,
                    "message": f"Hardware capability exceeded: {cap['reason']}"
                })

        out = tools.set_setting(field, value)
        if out.lower().startswith("unknown"):
            return web.json_response({"ok": False, "message": out})
        label = _SETTING_LABEL.get(field.lower().replace(" ", "_").replace("-", "_"), field)
        shown = value.strip().splitlines()[0][:60] if value.strip() else ""
        return web.json_response({"ok": True, "message":
                                  f"{label} saved" + (f" — {shown}." if shown else ".")})

    async def api_setup_connector(request):
        """Verify the B3 connector, then save it. Never reachable from the assistant."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import connector
        body = await request.json()
        key, uuid = (body.get("key") or "").strip(), (body.get("uuid") or "").strip()
        if not key or not uuid:
            return web.json_response({"ok": False, "message": "Both fields are needed."})
        if connector.in_use(uuid):
            # Live-testing the connector the daemon is already holding would be a second client
            # on it — the documented race. Save and say when it takes effect.
            out = tools.save_connector(key, uuid)
            return web.json_response({"ok": True, "message": out + "\n(Not re-tested: the "
                                      "running daemon already holds this connector.)"})
        ok, why = await connector.verify(key, uuid)
        if not ok:
            return web.json_response({"ok": False, "message": f"NOT saved — {why}"})
        return web.json_response({"ok": True, "message": tools.save_connector(key, uuid)
                                  + f"\nChecked: {why}"})

    async def api_quit(request):
        """Stop the daemon from the owner's own view.

        Double-clicked from a file manager there is no terminal, so without this the only ways
        to stop it are the CLI or a process manager — neither of which the person who just
        clicked an icon is holding.

        The SETUP page cancels through here too, deliberately not through a second endpoint. On a
        fresh install the process serving that page holds no channel (the daemon gates that on
        `owner.cannot_answer`), so cancelling costs nothing; on a configured one it is the same
        stop the owner's view offers, and the page says so before asking.

        Exits with os._exit AFTER the response is flushed. SIGTERM is caught somewhere in the
        async stack and does not reliably exit (the reason `stop` escalates to SIGKILL), and
        there is no unsaved state to lose: every store writes synchronously as it changes.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)

        async def _bye():
            await asyncio.sleep(0.4)          # let the response reach the browser
            (paths.RUN / "secretary.pid").unlink(missing_ok=True)
            logger.info("stopped from the local site")
            os._exit(0)

        asyncio.get_running_loop().create_task(_bye())
        return web.json_response({"ok": True, "message": "Stopping."})

    #: One background download at a time, and its outcome. A module-level dict rather than a
    #: task per request: the page POSTs once and then POLLS, so the state has to outlive the
    #: request that started it.
    _stt = {"running": False, "error": "", "model": ""}

    async def api_setup_stt(request):
        """The on-machine speech model: whether it is needed, whether it is here, and fetching it.

        GET reports; POST starts a download and returns immediately. It is NOT a blocking POST
        because this can be 2.9 GB — a request held open that long is a request that times out
        on someone's slow connection, and then the page cannot tell a failure from a slow link.

        ONLY RELEVANT WITH NO MODEL KEY. With one attached the hosted engine transcribes and
        nothing is ever downloaded, so the whole step is hidden rather than shown-and-skipped.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import hardware, owner as _own, transcribe

        model = transcribe.local_model()
        needed = transcribe.engine() == "local" and _own.record_calls()
        cap = hardware.check_capability(model)
        loaded = transcribe.loaded_info()

        if request.method == "GET":
            return web.json_response({
                "needed": needed, "model": model,
                "mb": transcribe.MODEL_MB.get(model, 0),
                "cached": transcribe.is_cached(model),
                "running": _stt["running"], "error": _stt["error"],
                "installed": transcribe._local_available(),
                "capability": cap,
                "loaded_info": loaded,
                "hardware": hardware.recommend_models(),
            })

        if not cap["can_download"]:
            return web.json_response({
                "ok": False,
                "error": cap["reason"],
                "message": cap["reason"],
                "blocked": True,
            }, status=400)

        if _stt["running"]:
            return web.json_response({"ok": True, "message": "Already downloading."})
        _stt.update(running=True, error="", model=model)

        async def _go():
            try:
                await asyncio.to_thread(transcribe.fetch, model)
            except Exception as exc:
                # Kept, not raised: the page is polling and this is the only way it learns why.
                _stt["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("speech model download failed: %s", _stt["error"])
            finally:
                _stt["running"] = False

        asyncio.create_task(_go())
        return web.json_response({"ok": True, "message": f"Downloading {model}…"})

    async def api_hardware(request):
        """Hardware capabilities profile, dynamic model recommendations, and loaded model status."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import hardware, llm, transcribe
        rec = hardware.recommend_models()
        rec["loaded_model"] = transcribe.loaded_info()
        rec["loaded_llm"] = llm.loaded_info()
        return web.json_response(rec)

    async def api_model_unload(request):
        """Unload resident local models (STT, LLM clients) and collect garbage to release RAM."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import hardware, llm, transcribe
        stt_was_loaded, stt_model, stt_freed_mb = transcribe.unload()
        llm_was_loaded, llm_model, llm_freed_mb = llm.unload()
        _forget_chat()
        hw = hardware.get_hardware_profile()
        freed_mb = stt_freed_mb + llm_freed_mb
        unloaded_names = [m for m in (stt_model, llm_model) if m]
        msg = f"Unloaded {', '.join(unloaded_names)} from memory (~{freed_mb} MB RAM freed)." if (stt_was_loaded or llm_was_loaded) else "No model was loaded in memory."
        return web.json_response({
            "ok": True,
            "message": msg,
            "stt_unloaded": stt_was_loaded,
            "llm_unloaded": llm_was_loaded,
            "model_unloaded": ", ".join(unloaded_names),
            "freed_ram_mb": freed_mb,
            "hardware": hw,
        })

    async def api_model_load(request):
        """Explicitly pre-load the configured or requested local speech model into memory."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import hardware, transcribe
        body = await request.json() if request.can_read_body else {}
        want = (body.get("model") or "").strip() or transcribe.local_model()

        # Check capability first
        cap = hardware.check_capability(want)
        if not cap["can_load"]:
            return web.json_response({"ok": False, "error": cap["reason"], "blocked": True}, status=400)

        try:
            await asyncio.to_thread(transcribe._load, want)
            transcribe._loaded_name = want
            return web.json_response({
                "ok": True,
                "message": f"Successfully loaded {cap['name']} into memory (~{cap['ram_mb']} MB RAM resident).",
                "loaded_info": transcribe.loaded_info(),
            })
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Failed to load: {exc}"}, status=500)

    async def api_llm_load(request):
        """Explicitly pre-load the configured or requested local LLM into memory."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import hardware, llm
        body = await request.json() if request.can_read_body else {}
        want = (body.get("model") or "").strip() or os.getenv("SECRETARY_MODEL", "llama-3.2-1b")

        cap = hardware.check_llm_capability(want)
        if not cap.get("can_load", True):
            return web.json_response({"ok": False, "error": cap["reason"], "blocked": True}, status=400)

        try:
            info = await asyncio.to_thread(llm.load, want)
            return web.json_response({
                "ok": True,
                "message": f"Successfully loaded {info['name']} into memory (~{info['ram_mb']} MB RAM resident).",
                "loaded_info": info,
            })
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Failed to load: {exc}"}, status=500)

    async def api_llm_unload(request):
        """Explicitly unload the local LLM from memory."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import hardware, llm
        was_loaded, model_name, freed_mb = llm.unload()
        return web.json_response({
            "ok": True,
            "message": msg,
            "llm_unloaded": was_loaded,
            "model_unloaded": model_name,
            "freed_ram_mb": freed_mb,
            "hardware": hw,
        })

    async def api_llm_download(request):
        """Explicitly download the requested local LLM after capability verification."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import hardware, llm
        body = await request.json() if request.can_read_body else {}
        want = (body.get("model") or "").strip() or "qwen-2.5-1.5b"

        cap = hardware.check_llm_capability(want)
        if not cap.get("can_download", True):
            return web.json_response({"ok": False, "error": cap["reason"], "blocked": True}, status=400)

        try:
            res = await asyncio.to_thread(llm.download, want)
            auto_load = body.get("auto_load", False)
            if auto_load and cap.get("can_load", True):
                load_res = await asyncio.to_thread(llm.load, want)
                res["loaded_info"] = load_res
                res["message"] += f" Loaded {load_res['name']} into RAM."
            return web.json_response(res)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Failed to download: {exc}"}, status=500)

    async def api_llm_delete(request):
        """Delete a downloaded local LLM from disk."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import hardware, llm
        body = await request.json() if request.can_read_body else {}
        want = (body.get("model") or "").strip()
        if not want:
            return web.json_response({"ok": False, "error": "No model specified."}, status=400)

        try:
            res = await asyncio.to_thread(llm.delete, want)
            _forget_chat()
            return web.json_response(res)
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Failed to delete: {exc}"}, status=500)


    async def api_setup_current(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import connector
        cur = tools.current_setup()
        cur["model"] = llm.describe()
        cur["model_name"] = os.getenv("SECRETARY_MODEL", "")
        # Explicit booleans. The pages used to infer "configured" from describe()'s prose, which
        # is a sentence written for a human and not a contract.
        cur["model_configured"] = llm.configured()
        cur["connector_configured"] = connector.configured()
        # No longer "live on the next restart" — the channel loop polls the environment, so a
        # saved connector connects within seconds.
        cur["connector"] = "configured" if connector.configured() else "not configured"
        cur["connector_uuid"] = os.getenv(connector.UUID, "")
        # WHERE THE RECORDINGS GO, resolved on THIS machine. Never a path written into the page:
        # $AGENTDUET_HOME differs by platform and by install, and a Mac owner told to look in
        # /home/... would reasonably conclude the feature had not run. Sent as an absolute path
        # so it can be pasted into a file manager.
        from . import carry, owner as _own, voice as _voice
        cur["calls"] = _own.calls()
        cur["transcription"] = _own.transcription_quality()
        cur["language"] = _own.language()
        cur["record_calls"] = _own.record_calls()
        cur["recordings_dir"] = str(carry.RECORDINGS / _voice.ANSWERED)
        cur["carried_dir"] = str(carry.RECORDINGS)
        # False until the backend has a sign-in endpoint. The page uses it to decide whether to
        # lead with "Sign in" or with the manual fields — see connector.OAUTH_URL.
        # Whether setup has been FINISHED before. The page uses it to decide that "Complete"
        # is a re-run and must not hand over: handover is the installer's last act, and on an
        # already-installed copy it would spawn a replacement and stand this one down — a
        # daemon restart, triggered from a Settings button, for no reason.
        cur["setup_done"] = (paths.RUN / "setup-done").exists()
        cur["oauth_available"] = connector.oauth_available()
        return web.json_response(cur)

    async def api_setup_questions(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from .init import QUESTIONS
        return web.json_response({"questions": [
            {"key": k, "prompt": p, "optional": opt, "long": k in ("does", "contacts", "never")}
            for k, p, opt in QUESTIONS]})

    async def api_setup_model(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        # attach_model verifies BEFORE saving; the message it returns names the length and last
        # four characters only, never the key.
        out = tools.attach_model((body.get("key") or "").strip(),
                                 (body.get("model") or "").strip())
        # Allow-list, not deny-list: a failed attach begins "NOT saved — …", which a list of
        # failure prefixes missed, so a bad key was reported as success. Only the message
        # attach_model emits on success counts as success.
        bad = not out.lower().startswith("attached")
        if not bad:
            llm.forget()          # drop any client cached under the old credential
            _forget_chat()        # rebuild the owner's assistant against the new one
        return web.json_response({"ok": not bad, "message": out})

    async def api_setup_interview(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        answers = body.get("answers") or {}
        from .init import INTERVIEW_PROMPT, RERUN_NOTE
        block = "\n".join(f"{k}: {v or '(not given)'}" for k, v in answers.items())
        # Second and later runs: show what is already recorded and require reconciliation.
        # Without this a changed answer produced a SECOND bullet beside the old one, and the
        # agent would then answer with whichever retrieval surfaced.
        cur = tools.current_setup()
        prompt = INTERVIEW_PROMPT.format(answers=block)
        if cur.get("configured"):
            # Only the fields this form asks about. Passing availability and never-say here
            # would invite the reconcile rule to delete them for being "absent from the
            # answers" — they are learned in use, not set in setup.
            prompt += RERUN_NOTE.format(
                name=cur["name"], pronoun=cur["pronoun"] or "(none set)",
                does=cur["does"] or "(nothing recorded)")
        if _chat() is None:
            return web.json_response({"ok": False,
                                      "message": "No model attached — go back to step 1."})
        # The SAME prompt the terminal init uses: one interview, two front ends. Recorded under
        # a label — the owner's chat history is a record of their conversation, not of the
        # instructions we sent on their behalf.
        result = await _chat().turn(
            prompt,
            label=("(setup: answered the questions — "
                   + ", ".join(k for k, v in answers.items() if v) + ")"))
        # Reporting ok on a reply that called nothing is how the first attempt looked like it
        # worked while settings.md stayed a template. Success means writes actually happened.
        wrote = result.get("tools") or []
        return web.json_response({
            "ok": bool(wrote),
            "message": (result.get("reply") or "Done.") if wrote else
                       ("Nothing was written — the model described the changes instead of making "
                        "them. Press it again."),
            "wrote": wrote})

    # Not wired to setup any more: choosing what the agent may DO is not a first-run decision.
    # Kept because the registry operations behind them (list_examples / install_example) are what
    # a later, generic surface for managing capabilities and their forms will call.
    async def api_setup_examples(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        import json as _json
        out = []
        root = paths.EXAMPLES
        for d in sorted(p for p in (root.iterdir() if root.is_dir() else []) if p.is_dir()):
            try:
                spec = _json.loads((d / "capability.json").read_text())
            except (OSError, ValueError):
                continue
            for name, cap in spec.items():
                bounds = cap.get("bounds") or {}
                action = cap.get("action", "book_slot")
                out.append({
                    "name": name,
                    "label": cap.get("canvas_label") or name.replace("_", " ").capitalize(),
                    "what": cap.get("what", ""),
                    # In plain terms, what granting this actually lets the agent do — taken from
                    # the framework's own description of the action, so it cannot overstate it.
                    "grants": capabilities.ACTIONS.get(action, action),
                    "limits": ", ".join(f"{k}={v}" for k, v in bounds.items()) or "no limits declared",
                    # Editable so the owner sets THEIR limits at the moment of granting, rather
                    # than inheriting the example's and having to notice.
                    "bounds": {k: v for k, v in bounds.items()
                               if k in capabilities.CHECKED or k == "radius_km"},
                    "installed": bool(capabilities.get(name)),
                })
        return web.json_response({"examples": out})

    async def api_setup_example(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        # Turning one on grants authority, so it is only ever done by this explicit call from a
        # click — never by the model during the interview.
        out = tools.install_example((body.get("name") or "").strip(),
                                    (body.get("bounds") or "").strip())
        return web.json_response({"ok": out.startswith("Installed"), "message": out})

    async def api_state(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        return web.json_response(tools.state())

    async def api_panel(request):
        """Everything the hub renders, in ONE call.

        Deliberately one endpoint rather than four. The panel shows the same few facts in
        several places — the sidebar's number, the overview's four rows, each panel's own
        header — and fetching them separately is how two parts of one screen come to disagree.

        The file lists are BOUNDED and newest-first. A machine that has been carrying calls for
        a year has thousands of files, and a page that lists them all is a page that stops
        opening at exactly the point the owner most wants it.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import carry, hardware, llm as _llm, owner as _own, transcribe, voice as _voice

        def _listing(folder, limit=25):
            if not folder.is_dir():
                return []
            out = []
            for f in sorted(folder.glob("*.*"), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.suffix.lower() not in (".wav", ".json", ".txt"):
                    continue
                out.append({"name": f.name, "kind": f.suffix.lstrip(".").upper(),
                            "bytes": f.stat().st_size})
                if len(out) >= limit:
                    break
            return out

        local_ok, _ = transcribe.available()
        return web.json_response({
            "name": _own.name() if _own.name() != _own.DEFAULT_NAME else "",
            "phone": _own.phone(),
            "calls": _own.calls(),
            "channel": (tools.state().get("channel") or {}).get("channel", ""),
            "storage": str(carry.RECORDINGS),
            "dirs": {"calls": str(carry.RECORDINGS),
                     "answered": str(carry.RECORDINGS / _voice.ANSWERED)},
            # WHAT IS ACTUALLY ON, not what the design shows switched on.
            "services": {
                "record_call": {"on": _own.record_calls(), "real": True},
                "transcribe": {"on": local_ok, "real": False},
                "record_message": {"on": False, "real": False},
                "connect_ai": {"on": _own.connect_ai(), "real": True},
            },
            # Whether the Apple Neural Engine option may be OFFERED. It is not built, so this
            # only decides enabled-vs-disabled and the reason shown beside it.
            "ane": dict(zip(("supported", "why"), transcribe.ane_support())),
            "stt": {"engine": transcribe.engine(), "model": transcribe.local_model(),
                    "quality": _own.transcription_quality() or "balanced",
                    "cached": transcribe.is_cached(),
                    "loaded_info": transcribe.loaded_info()},
            "hardware": hardware.recommend_models(),
            "model": {
                "configured": _llm.configured(),
                "describe": _llm.describe(),
                "provider": _llm.provider(),
                "model": os.getenv("SECRETARY_MODEL", ""),
                "loaded_info": _llm.loaded_info(),
                "is_local": _llm.provider() == "local",
            },
            "files": {"calls": _listing(carry.RECORDINGS),
                      "answered": _listing(carry.RECORDINGS / _voice.ANSWERED),
                      "messages": []},
        })

    async def api_ui(request):
        """View preferences. Server-side because the window has no localStorage — see
        tools.UI_PREFS."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        if request.method == "GET":
            return web.json_response(tools.ui_prefs())
        body = await request.json()
        out = [tools.set_ui_pref(k, v) for k, v in body.items()]
        return web.json_response({"ok": True, "message": "; ".join(out)})

    async def api_install(request):
        """Install status, and the install itself. Setup only — not day-to-day operation."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import install
        if request.method == "GET":
            return web.json_response(install.status())
        return web.json_response({"ok": True, "message": install.install()})

    #: One sign-in attempt in flight: the state and verifier that began it. In memory only —
    #: a PKCE verifier is single-use and worthless after the callback, so persisting it would
    #: store a secret with no purpose past the next few seconds.
    _pending_signin: dict = {}

    async def api_connector_signin(request):
        """Begin an OAuth sign-in, and send the browser to the provider.

        A GET that redirects, rather than an API call returning a URL: the browser has to end up
        at the provider either way, and a redirect keeps the whole flow in the address bar where
        the owner can see who is asking for their credentials.
        """
        if not authed(request):
            return web.Response(status=401, text="bad or missing token")
        from . import oauth
        if not oauth.available():
            return web.json_response(
                {"ok": False, "message": "Sign-in is not available yet. Enter the key manually."})

        provider = (request.query.get("provider") or "").lower()
        # ONLY GOOGLE WORKS UPSTREAM. Microsoft is refused because Entra does not issue
        # `email_verified`, and that flag is the identity-linking key — accepting an unverified
        # address is an account-takeover path. Apple was never in v1. Say which, rather than
        # letting the provider return an error the owner cannot act on.
        if provider and provider != oauth.PROVIDER:
            return web.json_response({"ok": False, "message":
                f"{provider.title()} sign-in is not available yet — only Google is. "
                "Use 'Set it up by hand' for now."})

        url, state, verifier = oauth.begin(PORT)
        _pending_signin.clear()
        _pending_signin.update(state=state, verifier=verifier)
        raise web.HTTPFound(url)

    async def oauth_callback(request):
        """Where the provider sends the browser back. Exchanges the code and stores the tokens.

        NO TOKEN ON THIS ROUTE, and it cannot have one: the redirect is built by us but performed
        by the provider, which will not carry our site token. What authenticates it instead is
        `state` — a value we generated moments ago and kept in memory, which an attacker inducing
        this request cannot know. The path and host are pinned upstream too: the server only
        accepts `http://127.0.0.1:{port}/callback`.
        """
        from . import oauth
        err = request.query.get("error")
        if err:
            return web.Response(content_type="text/html", text=_signin_page(
                "Sign-in was refused", request.query.get("error_description") or err))

        state, code = request.query.get("state", ""), request.query.get("code", "")
        want = _pending_signin.get("state")
        if not want or not secrets.compare_digest(state, want):
            # Either nothing is in flight, or this redirect belongs to a different attempt.
            return web.Response(status=400, content_type="text/html", text=_signin_page(
                "That sign-in did not match", "Start again from the setup page."))
        verifier = _pending_signin.get("verifier", "")
        _pending_signin.clear()          # single use, whatever happens next

        try:
            who = await asyncio.to_thread(oauth.complete, code, verifier, PORT)
        except Exception as exc:
            logger.warning("sign-in exchange failed: %s", exc)
            return web.Response(status=400, content_type="text/html", text=_signin_page(
                "Sign-in could not be completed", str(exc)))
        return web.Response(content_type="text/html", text=_signin_page(
            f"Signed in as {who}", "You can close this tab and go back to setup.", ok=True))

    def _signin_page(title: str, detail: str, ok: bool = False) -> str:
        """A plain result page. Deliberately not one of the app pages: this route has no site
        token, so it must not render anything that would try to call the API with one."""
        colour = "#34d399" if ok else "#fca5a5"
        return (f'<!doctype html><meta charset="utf-8"><title>{title}</title>'
                f'<body style="background:#020617;color:#f1f5f9;font-family:system-ui;'
                f'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
                f'<div style="text-align:center;max-width:28rem;padding:2rem">'
                f'<h1 style="font-size:1.25rem;color:{colour}">{title}</h1>'
                f'<p style="font-size:.85rem;color:#94a3b8;line-height:1.6">{detail}</p></div>')

    def _mark_setup_done():
        """Record that the owner pressed the button. See needs_setup."""
        try:
            paths.RUN.mkdir(parents=True, exist_ok=True)
            (paths.RUN / "setup-done").write_text("")
        except OSError:
            pass          # a dashboard the owner can reach matters more than the marker

    async def api_handover(request):
        """Start the installed daemon and stand down. The last act of the installer."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import service
        # BEFORE handing over, not after: on the installed path this process is about to exit,
        # and the marker has to be on disk for the copy that takes over — which reads it on its
        # first page load. Written on both paths because pressing the button is what "finished"
        # means, whether or not there was a second copy to promote.
        _mark_setup_done()
        msg = service.handover()
        # NOTHING TO HAND OVER TO IS NOT A FAILURE. Handover promotes the INSTALLED copy and
        # stands this one down; with no install there is simply no second copy to promote, and
        # the daemon the owner is talking to keeps answering either way. Reporting that in red
        # on a step called "Finish" tells someone their setup broke when it did not — and it is
        # the normal state for anyone running from source, or who used Skip on step 1.
        if msg.startswith("Not installed"):
            return web.json_response({"ok": True, "message":
                "Setup is complete and this secretary is answering now. It was not installed to "
                "this machine, so it stops when you close it — run step 1 if you want it to "
                "start again by itself after a reboot."})
        ok = msg.startswith("Handing over")
        if ok:
            # Answer FIRST, exit after. Exiting inside the handler would drop the response and
            # the page would report a network error instead of what actually happened.
            async def _stand_down():
                await asyncio.sleep(1.5)
                os.kill(os.getpid(), signal.SIGTERM)
            asyncio.create_task(_stand_down())
        return web.json_response({"ok": ok, "message": msg})

    async def api_chat(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        if _chat() is None:
            return web.json_response({"reply": "No model attached — chat disabled. "
                                               f"{llm.describe()}",
                                      "tools": []})
        body = await request.json()
        # Who the owner is looking at. Without it, "reply to her that I'll call tomorrow" has
        # no "her" — the assistant sits beside a conversation it cannot see, and the owner has
        # to retype an address the screen is already showing.
        # `focus` is the open item the owner has navigated to in the queue. Pointing at
        # something on screen is a way of saying it; without this the assistant had to guess
        # which of several open threads "this one" meant.
        return web.json_response(await _chat().turn(body.get("message", ""),
                                                 (body.get("viewing") or "").strip(),
                                                 (body.get("focus") or "").strip()))

    # ---- simulator -------------------------------------------------------
    # Stands in for the DDUET backend so the POC is testable now. It calls
    # brain.handle_query — the SAME path a real inbound message takes — so what you
    # see here is what a real message would get.
    #
    # OFF BY DEFAULT. It can forge a *verified* identity, which is a bypass of the
    # entire identity model; enabling it in anything but local testing would let
    # anyone claim any profile. Requires SECRETARY_SIM=1 and the owner token.
    sim_on = os.getenv("SECRETARY_SIM") == "1"

    async def sim_page(request):
        if not authed(request):
            return web.Response(status=401, text="bad or missing token")
        if not sim_on:
            return web.Response(status=404, text="simulator disabled (set SECRETARY_SIM=1)")
        return web.Response(text=(HERE / "sim.html").read_text(), content_type="text/html")

    async def api_sim(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        if not sim_on:
            return web.json_response({"error": "simulator disabled"}, status=404)
        from . import brain
        body = await request.json()
        v = body.get("verified")
        result = await brain.handle_query(
            (body.get("asker") or "").strip(),
            (body.get("message") or "").strip(),
            (body.get("network") or "WA").upper(),
            verified=bool(v) if v is not None else None,
            conversation=(body.get("conversation") or "").strip() or None,
        )
        return web.json_response(result)

    async def api_people(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import people
        return web.json_response({
            "profiles": [{"identity": i, "name": people.display_name(i, True)}
                         for i in people.list_profiles()],
            "self_vouching": sorted(people.SELF_VOUCHING_NETWORKS)})

    async def api_pending(request):
        """Asker-side view, used by the simulator. Narrow by construction — see
        tools.pending_for_asker."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        # asker_actions.open_asks is the ONE implementation of "what this asker has
        # open" — it honours their own withdrawals and merges. A second copy in tools.py
        # drifted immediately: it showed 18 items after a cleanup had left 8.
        from . import asker_actions
        q = request.query
        asks = asker_actions.open_asks(q.get("asker", ""), q.get("verified") == "1")
        # Project deliberately: no `reason` (it reveals what we do or don't document) and
        # no ids — the asker gets what they asked and when, nothing about our handling.
        return web.json_response({"pending": [
            {"question": a["question"], "at": a["at"], "merged": a.get("merged", False)}
            for a in asks]})

    async def api_deliver(request):
        """Hand over any replies the owner sent while this person had no live channel.

        `take_` clears them, so a reply is delivered exactly once — and if nobody has a
        page open they stay held and flush on the next inbound message instead. Also
        recorded into the person's conversation, so the owner's view shows the reply as
        delivered rather than still waiting.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import asker_actions
        body = await request.json()
        who = (body.get("asker") or "").strip()
        held = asker_actions.take_pending_replies(who) if who else []
        for m in held:
            tools.record_delivery(who, m["text"], body.get("conversation") or "")
        return web.json_response({"delivered": held})

    async def api_history(request):
        """Stored turns for one conversation, so the simulator can restore its transcript
        after a refresh instead of looking like the exchange never happened."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import memory
        q = request.query
        k = memory.key(q.get("asker", ""), q.get("verified") == "1", q.get("conversation"))
        return web.json_response({"turns": memory.turns(k)})

    async def api_conversation(request):
        """One person's full history, for the site's conversation view."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        who = request.query.get("asker", "")
        return web.json_response({"asker": who, "rows": tools.conversation_rows(who)})

    # ---- asker-facing canvas ---------------------------------------------
    # A SEPARATE surface from the owner site: no owner token, no OWNER_TOOLS, read-only
    # projections plus the capability path. `canvas.py` deliberately does not import tools.
    # Access is the per-identity canvas token in `c=`, never the owner token.
    async def canvas_page(request):
        from . import canvas
        if not canvas.holder(request.match_info.get("token", "")):
            return web.Response(status=404, text="not found")
        rec = canvas.holder(request.match_info.get("token", "")) or {}
        return web.Response(text=canvas.page_for(rec.get("capability", "")).read_text(),
                            content_type="text/html")

    async def api_canvas_available(request):
        """Standing surfaces for this identity, so the asker can find them without asking."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import canvas
        q = request.query
        return web.json_response({"canvases": canvas.advertised(
            (q.get("asker") or "").strip(), q.get("verified") == "1")})

    async def api_canvas_info(request):
        """Label for one canvas, so the asker's page can title a tab it did not author."""
        from . import canvas
        d = canvas.describe(request.query.get("c", ""))
        if not d:
            return web.json_response({"error": "invalid link"}, status=404)
        return web.json_response(d)

    async def api_canvas_menu(request):
        from . import canvas
        rec = canvas.holder(request.query.get("c", ""))
        if not rec:
            return web.json_response({"error": "invalid link"}, status=404)
        cap = rec.get("capability", "")
        return web.json_response({"menu": canvas.menu(cap), "slots": canvas.slots(cap),
                                  "label": (canvas.describe(request.query.get("c", "")) or {}
                                            ).get("label", ""),
                                  "for": rec.get("asker", "")})

    async def api_canvas_order(request):
        from . import canvas
        body = await request.json()
        from . import brain
        from . import capabilities
        rec = canvas.holder(request.query.get("c", "")) or {}
        out = canvas.submit(request.query.get("c", ""), body.get("lines") or [],
                            (body.get("at") or "").strip())
        # Record it in the SAME append-only log the chat path writes to. Without this a canvas
        # order existed only in schedule.json: the owner's conversation view, the digest and
        # every other projection are built over this log, so an action taken on the owner's
        # behalf left no trace in the record of what happened. Written here rather than in
        # canvas.py because that module must not import the owner side (`brain` is clean, but
        # keeping the write at the route keeps canvas.py's imports minimal by construction).
        if out.get("ok"):
            b = out.get("booking") or {}
            brain.record(
                asker=rec.get("asker", ""),
                question=f"[ordering page] {b.get('what', '')}",
                outcome="acted",
                reason=f"capability:{rec.get('capability', '')}:canvas",
                answer=out.get("message", ""),
                verified=bool(rec.get("verified")),
                briefing={"topic": (capabilities.get(rec.get("capability", "")) or {})
                                   .get("canvas_label", "Order")},
            )
        if out.get("ok"):
            for ws in list(sockets):
                try:
                    await ws.send_str(json.dumps({"type": "state", "state": tools.state()}))
                except Exception:
                    sockets.discard(ws)
        return web.json_response(out)

    async def api_chat_history(request):
        """The owner's own chat, so a reload does not look like it never happened."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        return web.json_response({"turns": _chat().shown if _chat() else []})

    async def api_send(request):
        """Owner sends a message straight to a person — the composer in the middle column.

        Goes through `tools.reply_to`, the same implementation MCP uses, rather than a second
        send path: it records the message, closes whatever the reply actually answers, and
        either delivers live or holds it until the person next writes (DDUET is passive).
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        who = (body.get("asker") or "").strip()
        text = (body.get("text") or "").strip()
        if not who or not text:
            return web.json_response({"error": "need an asker and text"}, status=400)
        return web.json_response({"result": tools.reply_to(who, text),
                                  "state": tools.state()})

    async def api_resolve(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        msg = tools.resolve_escalation((body.get("id") or "").strip(),
                                       body.get("note") or "cleared from the panel")
        return web.json_response({"result": msg, "state": tools.state()})

    async def ws_asker(request):
        """Asker-side live channel.

        Deliberately carries NO payload — just "something changed". The owner socket
        pushes tools.state(), which contains every escalation, briefing and permission;
        the asker side must never receive that. It gets a ping and re-fetches
        /api/pending, which is scoped to their own items.
        """
        if not authed(request):
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        asker_sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            asker_sockets.discard(ws)
        return ws

    async def ws_handler(request):
        if not authed(request):
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            sockets.discard(ws)
        return ws

    async def watch_log(app):
        """Push state to open pages when the query log changes — this is the alert
        channel a desktop notification only gestures at."""
        last = None
        while True:
            await asyncio.sleep(2)
            try:
                stamp = tools.LOG.stat().st_mtime if tools.LOG.exists() else 0
            except OSError:
                continue
            if stamp != last:
                last = stamp
                payload = json.dumps({"type": "state", "state": tools.state()})
                for ws in list(sockets):
                    try:
                        await ws.send_str(payload)
                    except Exception:
                        sockets.discard(ws)
                ping = json.dumps({"type": "changed"})     # no data, see ws_asker
                for ws in list(asker_sockets):
                    try:
                        await ws.send_str(ping)
                    except Exception:
                        asker_sockets.discard(ws)

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/setup", setup_page),
        web.get("/app.css", app_css),
        web.get("/secretary", secretary_page),
        web.get("/settings", settings_page),
        web.post("/api/setup/setting", api_setup_setting),
        web.get("/api/setup/about", api_setup_about),
        web.get("/api/setup/stt", api_setup_stt),
        web.post("/api/setup/stt", api_setup_stt),
        web.get("/api/hardware", api_hardware),
        web.post("/api/model/unload", api_model_unload),
        web.post("/api/model/load", api_model_load),
        web.post("/api/llm/download", api_llm_download),
        web.post("/api/llm/load", api_llm_load),
        web.post("/api/llm/unload", api_llm_unload),
        web.post("/api/llm/delete", api_llm_delete),
        web.post("/api/setup/about", api_setup_about),
        web.post("/api/setup/connector", api_setup_connector),
        web.post("/api/quit", api_quit),
        web.get("/api/setup/current", api_setup_current),
        web.get("/api/setup/questions", api_setup_questions),
        web.post("/api/setup/model", api_setup_model),
        web.post("/api/setup/interview", api_setup_interview),
        web.get("/api/setup/examples", api_setup_examples),
        web.post("/api/setup/example", api_setup_example),
        web.get("/api/state", api_state),
        web.get("/api/panel", api_panel),
        web.post("/api/handover", api_handover),
        web.get("/api/install", api_install),
        web.post("/api/install", api_install),
        web.get("/api/connector/signin", api_connector_signin),
        web.post("/api/connector/signin", api_connector_signin),
        web.get("/callback", oauth_callback),
        web.get("/api/ui", api_ui),
        web.post("/api/ui", api_ui),
        web.post("/api/chat", api_chat),
        web.post("/api/resolve", api_resolve),
        web.post("/api/send", api_send),
        web.get("/api/chat_history", api_chat_history),
        web.get("/c/{token}", canvas_page),
        web.get("/api/canvas/available", api_canvas_available),
        web.get("/api/canvas/info", api_canvas_info),
        web.get("/api/canvas/menu", api_canvas_menu),
        web.post("/api/canvas/order", api_canvas_order),
        web.get("/api/pending", api_pending),
        web.post("/api/deliver", api_deliver),
        web.get("/api/history", api_history),
        web.get("/api/conversation", api_conversation),
        web.get("/sim", sim_page),
        web.post("/api/sim", api_sim),
        web.get("/api/people", api_people),
        web.get("/ws", ws_handler),
        web.get("/ws/asker", ws_asker),
    ])
    app.cleanup_ctx.append(lambda a: _background(a, watch_log))
    return app


async def _background(app, coro):
    task = asyncio.create_task(coro(app))
    yield
    task.cancel()


def _token() -> str:
    """Stable per-machine token, reused across restarts.

    Minting a fresh one each launch invalidated every open tab and bookmarked link on
    every restart. It bought nothing: the token file already sits on the same machine
    as the server, so anyone who can read it can reach localhost anyway. Rotate on
    demand with SECRETARY_ROTATE_TOKEN=1 (or just delete .run/web-token).
    """
    RUN.mkdir(exist_ok=True)
    if os.getenv("SECRETARY_ROTATE_TOKEN") == "1":
        TOKEN_FILE.unlink(missing_ok=True)
    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text().strip()
        if existing:
            TOKEN_FILE.chmod(0o600)
            return existing
    token = secrets.token_urlsafe(16)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token


async def start() -> str:
    """Start the site inside the daemon's loop. Returns the URL to open."""
    token = _token()

    # One model serves both surfaces. The OWNER_MODEL split was removed 2026-07-29:
    # never justified on architecture (its reason was free-tier quota management), and
    # it cost real debugging time when the two surfaces silently ran different models.
    model = os.getenv("SECRETARY_MODEL", "gemini-3.1-flash")
    chat = OwnerChat(model) if llm.client(model) else None

    runner = web.AppRunner(make_app(chat, token))
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()

    url = f"http://{HOST}:{PORT}/?t={token}"
    # Record the URL that was actually bound. A second launch reads this rather than rebuilding
    # it from the environment, which would guess the wrong port if the running instance was
    # started with a different SECRETARY_WEB_PORT.
    try:
        (paths.RUN / "site-url").write_text(url)
    except OSError:
        pass
    if os.getenv("SECRETARY_SIM") == "1":
        logger.warning("SIMULATOR ENABLED — forged identities accepted at "
                       "http://%s:%s/sim?t=%s", HOST, PORT, token)
    return url
