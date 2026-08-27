"""The owner's Personal Assistant — the model they talk to about their own calls.

Split out of `web.py` on 2026-08-27. It had nothing to do with serving HTTP: a prompt, a
tool-calling loop and a transcript, sitting in the middle of a module about routes and sockets.
`web.py` is the site; this is the thing the site happens to render.

IT IS NOT THE SECRETARY'S CONSOLE, and the difference is the whole reason this module has its
own registry. `OWNER_TOOLS` is for an owner running an agent that ANSWERS people — escalations,
drafting, sending, permission grants. On the recorder nobody is answered, so those tools have no
subject; handed them, the assistant used them anyway and reported that "all escalations have
been addressed" on a product with no such object. It gets `tools.RECORDER_TOOLS` plus
`tools.ASSISTANT_SHARED`, and cannot reach the secretary's surface at all.

PROVIDER-NEUTRAL BY DESIGN: the model returns a JSON action rather than using a vendor
function-calling API, so swapping models does not rewrite the loop.
"""

import asyncio
import json
import logging
import re
from datetime import datetime

from . import llm
from . import owner
from . import paths
from . import tools

logger = logging.getLogger("secretary.assistant")


#: THE PERSONAL ASSISTANT IS NOT THE SECRETARY'S CONSOLE.
#:
#: `OWNER_TOOLS` is the registry for an owner running an agent that ANSWERS people: escalations,
#: drafting, sending, permission grants, capability bounds. On the recorder nobody is answered
#: and nothing is escalated, so those tools have no subject — and handed them, the assistant
#: used them anyway. Asked "who called me today?" it called `pending_escalations` and reported
#: that "all escalations have been addressed", on a product with no such object.
#:
#: So the assistant gets its own, smaller registry: the calls, the people on them, and the
#: owner's own notes. Whatever the secretary needs stays in OWNER_TOOLS for the mcp, untouched.
ASSISTANT_PROMPT = """You are %s's personal assistant, running on their own computer.

%%s

Their phone calls are carried to them and recorded here, and the recordings are transcribed on
this machine. Your subject is those calls: who rang, when, what was said, and what the owner
should do about it. Summarise, find things, and remember what they tell you to remember.

You have tools. To use one, reply with ONLY this JSON and nothing else:
  {"tool": "<name>", "args": {...}}
After you see the result, either call another tool or answer in plain text.
To answer directly, just write the answer — no JSON.

ANSWER FROM THE CALLS, NOT FROM MEMORY. A question about who called, or what someone said, is
answered by calling list_calls or read_call first. Never guess at a name, a time or a quote.
If there is no transcript yet, say so — transcription runs after a call, on a queue.

You do not speak to anyone but the owner. You cannot send a message, answer a caller, or act on
their behalf. If they ask you to reply to someone, say that this assistant only reads.

Their notes are yours to keep correct. read_knowledge before you write, edit_knowledge to
correct something that is already there, add_knowledge only when the subject is genuinely new.
Do not leave two versions of one fact.

Be brief. Do not describe what you are about to do — do it, then say what you did.

THEIR NOTES:
%%s

TOOLS:
%%s
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
def assistant_tools() -> dict:
    """The Personal Assistant's registry: the recorder's own tools, plus a named subset of the
    owner registry. `OWNER_TOOLS` itself is untouched — it is also the stdio mcp's surface."""
    return {**tools.RECORDER_TOOLS, **tools.ASSISTANT_SHARED}
def _tool_docs(registry: dict | None = None) -> str:
    lines = []
    for name, (fn, params) in (registry or assistant_tools()).items():
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
        self.registry = assistant_tools()
        self.system = ASSISTANT_PROMPT % owner.name() % (
            owner.identity_block(), _subjects(), _tool_docs(self.registry))
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

    async def turn(self, message: str, viewing: str = "", label: str = "") -> dict:
        """One owner turn. `label` is what gets REMEMBERED in place of `message`.

        Setup drives this with a 3 KB instruction block. Recording that verbatim put the whole
        internal prompt in the owner's visible chat history AND in the rolling history fed back
        to the model, so later questions were answered with setup instructions still in context.
        The model still receives `message`; only what is stored is replaced.
        """
        shown_as = label or message
        # Hand over what the assistant would otherwise have to ask for, so it does not answer a
        # question about someone from nothing. It used to call `owner_context`, which describes
        # the person's OPEN THREADS and the draft the answering agent wrote for them — objects
        # that do not exist on a machine where nobody is answered. What the owner is looking at
        # here is a person and their calls, so that is what it is handed.
        context = ""
        if viewing:
            calls = tools.read_call(who=viewing)
            context = (f"CONTEXT — the owner is looking at {viewing}.\n\n{calls}"
                       f"\n\nIf the owner says \"her\", \"him\", \"them\" or \"this "
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
                entry = self.registry.get(name)
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
        # A weak model can loop on the tool call and hand the same JSON back as its "answer".
        # Rendering `{"tool": ...}` to the owner is never right — it is the machinery, not a
        # reply. The last tool result usually IS the answer, so show that instead.
        if self._parse(final):
            last = next((h[len("TOOL_RESULT: "):] for h in reversed(history)
                         if h.startswith("TOOL_RESULT: ")), "")
            final = last or "No answer this turn — the model kept asking for the same tool."
        final = self._unprefix(final)
        remember(history + [f"ASSISTANT: {final}"])
        self._record(shown_as, final, used, full=message)
        return {"reply": final, "tools": used}

    #: History is handed to the model as plain `OWNER:` / `ASSISTANT:` lines, so a weak model
    #: sometimes CONTINUES the transcript instead of answering — the reply comes back with the
    #: speaker labels in it, and the owner sees their own question quoted back. Cheap to strip,
    #: and never legitimate: the model is asked for the answer, not for the next line.
    @staticmethod
    def _unprefix(text: str) -> str:
        out = []
        for line in text.splitlines():
            if line.startswith("OWNER:"):
                continue
            out.append(line[len("ASSISTANT:"):].lstrip() if line.startswith("ASSISTANT:") else line)
        return "\n".join(out).strip() or text.strip()

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
