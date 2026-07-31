"""First-run setup — an interview, not a form.

WHY AN INTERVIEW

A fresh instance is four template files with empty headings. Asking an owner to fill them in
is asking them to guess a schema: what belongs in `## Who`, how a capability's bounds relate to
its document, why a fact should be phrased the way a customer would ask it. Every one of those
is something the model can work out from a conversation, and get more right than a form would.

So `init` collects the few things only the owner knows — their name, what they do, who contacts
them, what must never be said — and the MODEL writes the files, using the same tools it will use
later (`add_knowledge`, `edit_knowledge`, `declare_capability`). The framework supplies the
questions and the destinations; the model supplies the drafting.

WHAT IS NOT LEFT TO THE MODEL

The credential step. `attach_model` verifies a key BEFORE saving it, because an interview that
ends with a broken credential leaves the owner with an agent that appears installed and answers
nothing. And nothing here declares a capability the owner did not ask for: authority is the one
thing an interview must not be helpful about.
"""

import os
import sys

from . import llm, owner, paths, tools

#: Asked in order. (key, prompt, whether an empty answer is allowed)
#:
#: Only what the agent cannot learn by running. Availability, who tends to write in, and the
#: topics never to discuss all emerge from use: the first "can we meet Tuesday?" escalates, the
#: owner answers, and the answer is recorded. Asking for them up front makes setup longer at the
#: moment the owner knows least about what their agent will actually be asked.
#:
#: The never-say list is the one that looks like a safety question. It is not urgent on day one:
#: commitments, pricing and scheduling are refused in CODE regardless (policy.COMMITMENT_RULES),
#: and a fresh instance has almost nothing to disclose. It becomes worth setting when the owner
#: starts adding documents — which is when to prompt for it, not before.
QUESTIONS = [
    ("name", "Your name, as the agent should sign off and refer to you", False),
    ("pronoun", "How should it refer to you to outsiders? he/him, she/her, they/them "
                "(blank = use your name, never a guess)", True),
    ("does", "In a sentence or two: what do you do? Include any other business you run — "
             "the agent has no other way to judge whether a question is your kind of thing", False),
]

INTERVIEW_PROMPT = """You are setting up a personal secretary for its owner, from an interview.

Write the owner's answers into the instance using your tools. Rules that matter:

- SETTINGS vs FACTS. Name, pronoun and never-say are settings the code parses by heading — set
  them with set_setting. What an ASKER may be told (what the owner does, availability) is
  knowledge: write it into owner.md with add_knowledge.
- PUT EACH FACT UNDER THE RIGHT HEADING. add_knowledge takes `section`. owner.md already has
  "Who" and "Availability" — use them. What the owner does, and any business they run, goes
  under Who. Do not pile everything into a new section.
- WRITE IN THE THIRD PERSON, NOT THE OWNER'S WORDS. These sentences are read out to strangers by
  an assistant, so the owner's "I" would be the assistant claiming to be them. Convert:
      the owner said: "I run Tan Legal, a conveyancing practice in Bugis"
      you write:      "Runs Tan Legal, a conveyancing practice in Bugis."
      the owner said: "Weekdays 9-6, no meetings Fridays"
      you write:      "Reachable weekdays 9am-6pm Singapore time." and
                      "Does not take meetings on Fridays."
  Split a sentence that carries two facts into two bullets, so either can be corrected alone.
- Phrase it so it answers the question someone would actually ask. A fact recorded in the
  owner's wording often fails to match how it is asked about.
- One assertion per `- ` bullet, under a `## ` heading that names the subject, so it can be
  corrected later instead of appended to.
- Do NOT declare any capability. The owner has not authorised an action, and authority is not
  something to be helpful about. If their answers suggest one (they take orders, bookings,
  callbacks), SAY so at the end and tell them the command to run — do not act.
- If an answer is too vague to write down, ask ONE short follow-up rather than inventing detail.

THE INTERVIEW
{answers}

Now make the writes. When finished, reply with a short summary of what you recorded, then tell
them in one line that anything else is learned as they go: when a question comes in that you
cannot answer, they answer it once and you remember it — and that they can say "never discuss X"
at any time.
"""


#: Appended on a re-run. Setup is not one-shot: it is the settings screen, so the second visit
#: must CORRECT what is there rather than add a rival version of it — the same discipline the
#: knowledge tools enforce at runtime.
RERUN_NOTE = """

ALREADY RECORDED — this is a RE-RUN, not a first setup
  name:         {name}
  pronoun:      {pronoun}
  what they do: {does}

Reconcile ONLY these three. Everything else in the knowledge base — availability, never-say,
anything learned since — was not asked about here and must be left exactly as it is. Deleting a
fact merely because this short form does not cover it would throw away what the owner has built
up by using the agent.

For the three above, do not duplicate:
- Where the answer above MATCHES what is recorded, change nothing and do not re-add it.
- Where it DIFFERS, correct the existing statement — set_setting for a setting, edit_knowledge
  for a bullet. Never add a second bullet saying something different about the same subject.
- Where something recorded is now ABSENT from the answers, delete it with edit_knowledge and an
  empty replacement. An omission is how the owner removes a fact.
- Report what you changed, what you left alone, and what you deleted.
"""


def _ask(prompt: str, allow_blank: bool) -> str:
    while True:
        try:
            got = input(f"\n  {prompt}\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  cancelled — nothing written.")
            sys.exit(1)
        if got or allow_blank:
            return got
        print("  (needed)")


def ensure_instance() -> list[str]:
    """Create $DDUET_HOME from the templates. Idempotent, and never overwrites."""
    return paths.migrate()


def attach(interactive: bool = True) -> bool:
    """Attach a model. Returns True once one is usable."""
    ok, _ = llm.verify()
    if ok:
        print(f"  model: {llm.describe()}")
        return True
    if not interactive:
        return False
    print("\n  No model attached yet. Paste an API key for Gemini, Claude or Qwen.")
    print("  It is verified before it is saved, and stored only in "
          f"{paths.ENV_FILE} (owner-readable only).")
    model = input("\n  Model name (e.g. gemini-2.5-flash, claude-sonnet-5, qwen3.6-flash)\n  > ").strip()
    key = input("  API key (not echoed back)\n  > ").strip()
    out = tools.attach_model(key, model)
    print("  " + out.replace("\n", "\n  "))
    return not out.lower().startswith(("could not", "that key"))


def interview() -> str:
    """Ask the owner the few things only they know, then let the model do the writing."""
    print("\n  A few questions. The agent will write the files itself.")
    answers = {}
    for key, prompt, blank_ok in QUESTIONS:
        answers[key] = _ask(prompt, blank_ok)

    block = "\n".join(f"{k}: {v or '(not given)'}" for k, v in answers.items())
    # The model does the drafting, through the same tool surface it uses at runtime — so an
    # instance set up by init is indistinguishable from one the owner filled in by hand.
    from .web import OwnerChat          # imported late: it pulls in the model client
    chat = OwnerChat(os.getenv("SECRETARY_MODEL", ""))
    import asyncio
    result = asyncio.run(chat.turn(
        INTERVIEW_PROMPT.format(answers=block),
        label=("(setup: answered the questions — "
               + ", ".join(k for k, v in answers.items() if v) + ")")))
    return result.get("reply", "")


def main(interactive: bool = True) -> int:
    print("\n  DDuet Desktop — setup")
    made = ensure_instance()
    print(f"  instance: {paths.HOME}" + (f"  (created {len(made)} file(s))" if made else "  (already present)"))

    if not attach(interactive):
        print("\n  Setup stopped: the interview needs a working model, because the agent writes "
              "its own configuration.\n  Run `dduet-desktop init` again once you have a key.")
        return 1

    print("\n  " + (interview() or "(no summary returned)").replace("\n", "\n  "))
    print(f"""
  Next:
    dduet-desktop run          start it (owner site on 127.0.0.1)
    examples in {paths.EXAMPLES}
      — copy a folder to give it something it may DO, then declare the capability
""")
    return 0
