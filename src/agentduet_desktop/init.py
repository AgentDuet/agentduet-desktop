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

import asyncio
import os
import pathlib
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


def _prompt(text: str) -> str:
    """`input()` that leaves politely instead of dumping a traceback.

    Ctrl-C during setup, or a closed stdin, is not an error worth a stack trace — it is somebody
    changing their mind, or a script piping fewer answers than there are questions. Sixteen
    prompts each raising a bare EOFError made the second look like a crash in the product.
    """
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  cancelled — nothing further was written.")
        sys.exit(1)


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
    """Create $AGENTDUET_HOME from the templates. Idempotent, and never overwrites."""
    return paths.migrate()


def attach(interactive: bool = True) -> bool:
    """Attach a model. Returns True once one is usable."""
    ok, _ = llm.verify()
    if ok:
        print(f"  model: {llm.describe()}")
        return True
    if not interactive:
        return False

    # TWO WAYS AND A DOOR OUT. The settings page has offered local models since 2026-08-27 and
    # this console path still asked only for an API key — so the owner without a key, who is
    # exactly the owner this path exists for, was told to go and get one. Parity, in the same
    # sense CLAUDE.md means it: a model reachable from one surface only is half the owners
    # unable to choose it.
    print("\n  No model attached yet. Two ways to fix that, and skipping is fine:")
    print("    1  paste an API key — Gemini, Claude or Qwen")
    print("    2  download a model that runs on this machine, no key and no account")
    print("       (gigabytes, and it downloads in the background)")
    choice = _prompt("\n  1, 2, or Enter to skip\n  > ").strip()
    if choice == "2":
        return _attach_local()
    if choice != "1":
        print("  Skipped. Attach one later in Settings, or run `init` again.")
        return False

    print("  It is verified before it is saved, and stored only in "
          f"{paths.ENV_FILE} (owner-readable only).")
    model = _prompt("\n  Model name (e.g. gemini-2.5-flash, claude-sonnet-5, qwen3.6-flash)\n  > ").strip()
    key = _prompt("  API key (not echoed back)\n  > ").strip()
    out = tools.attach_model(key, model)
    print("  " + out.replace("\n", "\n  "))
    return not out.lower().startswith(("could not", "that key"))


def _models_coming() -> bool:
    from . import models
    return models.downloading() is not None


def _self_command() -> list[str]:
    """How to launch this program again as a child.

    The INSTALLED symlink when there is one, so the download outlives the owner tidying up the
    file they downloaded — the same reasoning as `service.handover`.
    """
    import sys
    from . import install
    link = install.installed_path()
    if link.is_symlink() and link.resolve().is_file():
        return [str(link)]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "agentduet_desktop.cli"]


def _attach_local() -> bool:
    """Offer the models this machine can hold, and fetch the chosen one in the background.

    DETACHED, NOT A THREAD. `init` is a short-lived process: a daemon thread would die when it
    exits and a normal one would stop it exiting at all, so neither is "download and proceed".
    A child in its own session survives, and `models.downloading()` reads its progress off the
    `.part` file from anywhere — including `status`.

    Returns False either way, because no model is USABLE yet and callers ask exactly that. The
    answer-mode branch in `main` treats a fetch in flight as reason enough to carry on.
    """
    import subprocess
    from . import models
    choices = []
    for name in models.families():
        spec = models.spec_of(name)
        if not spec:
            continue
        verdict, why = models.can_run(name)
        if verdict == "no":          # do not offer what this machine cannot hold
            continue
        choices.append((name, spec, verdict, why))
    if not choices:
        print("\n  No local model fits this machine's memory. An API key is the way here.")
        return False

    print("\n  Models that fit this machine:")
    for i, (name, spec, verdict, why) in enumerate(choices, 1):
        tight = "  (tight)" if verdict == "tight" else ""
        print(f"    {i}  {spec['name']}  —  {spec['dl_mb'] / 1024:.1f} GB download{tight}")
    print(f"    free space: {models.disk_free_mb() / 1024:.1f} GB")
    picked = _prompt("\n  Which one, or Enter to skip\n  > ").strip()
    if not picked.isdigit() or not (1 <= int(picked) <= len(choices)):
        print("  Skipped.")
        return False
    name, spec, _, _ = choices[int(picked) - 1]

    ok, why = models.can_download(name)
    if not ok:
        print(f"  Cannot download {spec['name']}: {why}.")
        return False
    log = paths.RUN / "model-download.log"
    try:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as out:
            subprocess.Popen(_self_command() + ["models", "download", name],
                             stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  Could not start the download: {exc}")
        return False
    print(f"\n  Downloading {spec['name']} ({spec['dl_mb'] / 1024:.1f} GB) in the background.")
    print("  Setup carries on — the agent cannot answer with it until it lands.")
    print(f"  Watch it with `agentduet-desktop status`, or read {log}.")
    return False


def sign_in(interactive: bool = True) -> bool:
    """Offer to sign in. True if signed in by the time this returns.

    HEADLESS FAILS CLEANLY RATHER THAN DEGRADING. A box with no display cannot show a consent
    screen, and the workarounds — pasting a code back from a page that failed to load, or an ssh
    tunnel — are either confusing or assume the reader forwards ports for fun. The connector key
    still works, so such an owner has a route; it is simply the other one. Saying so plainly
    beats offering a flow that cannot finish.
    """
    from . import oauth
    if oauth.signed_in():
        print(f"\n  Signed in as {oauth.email()}.")
        return True
    if not oauth.available() or not interactive:
        return False
    if not oauth.browser_available():
        print("\n  Signing in needs a browser, and this machine has no display.")
        print("  Use a connector key below instead — it does the same job.")
        return False

    print("\n  You can sign in with Google instead of typing a connector.")
    print("  Signing in creates your connector for you; there is nothing to copy.")
    if _prompt("  Sign in now? [Y/n] ").strip().lower().startswith("n"):
        return False
    try:
        who = oauth.sign_in_interactive()
    except Exception as exc:
        print(f"  Sign-in did not finish: {exc}")
        print("  You can use a connector key below instead.")
        return False
    print(f"  Signed in as {who}. Connector {oauth.connector_uuid()} is yours.")
    return True


def connect(interactive: bool = True) -> bool:
    """Attach the B3 connector. Returns True if one is configured.

    HERE, NOT IN THE MCP, AND NOT IN CHAT. Two secrets go in, and handing a secret to an
    assistant means typing it into a chat box — which sends it to the model provider and writes
    it to run/owner_chat.json in plaintext. `save_connector` is deliberately outside
    OWNER_TOOLS for that reason, so a terminal is the only safe place left.

    Skippable, and saying so matters: everything local works without it. The owner just never
    hears from anyone, which looks identical to nobody having called.
    """
    from . import connector
    if connector.configured():
        print(f"  connector: {os.getenv(connector.UUID)}")
        return True
    if not interactive:
        return False

    print("\n  A B3 connector gives this install its phone number and message channel.")
    print("  Without one everything local still works — it just never hears from anyone.")
    print("  Ask B3 for your OWN connector: only one machine may hold a connector at a time,")
    print("  and a second one fights the first for the same number.")
    uuid_in = _prompt("\n  Connector uuid (blank to skip)\n  > ").strip()
    if not uuid_in:
        print("  Skipped. Add one later with `agentduet-desktop init` again.")
        return False
    key = _prompt("  Connector API key\n  > ").strip()

    print("  checking with B3…")
    ok, why = asyncio.run(connector.verify(key, uuid_in))
    if not ok:
        # Same rule as the model key: a saved-but-wrong credential produces the worst failure —
        # an install that looks configured and silently answers nothing.
        print(f"  NOT saved — {why}")
        return False
    print(f"  {why}")
    print("  " + tools.save_connector(key, uuid_in).replace("\n", "\n  "))
    return True


def interview() -> str:
    """Ask the owner the few things only they know, then let the model do the writing."""
    print("\n  A few questions. The agent will write the files itself.")
    answers = {}
    for key, prompt, blank_ok in QUESTIONS:
        answers[key] = _ask(prompt, blank_ok)

    block = "\n".join(f"{k}: {v or '(not given)'}" for k, v in answers.items())
    # The model does the drafting, through the same tool surface it uses at runtime — so an
    # instance set up by init is indistinguishable from one the owner filled in by hand.
    from .assistant import OwnerChat    # imported late: it pulls in the model client
    chat = OwnerChat(os.getenv("SECRETARY_MODEL", ""))
    import asyncio
    result = asyncio.run(chat.turn(
        INTERVIEW_PROMPT.format(answers=block),
        label=("(setup: answered the questions — "
               + ", ".join(k for k, v in answers.items() if v) + ")")))
    return result.get("reply", "")


def choose_mode(interactive: bool = True) -> str:
    """The call mode in force. NO LONGER ASKED, on either surface.

    The console used to offer "answer it / put it through", and Settings had a card for the
    same choice. Both are gone: the recorder is the product, `carry` is what the seeded
    settings.md says, and offering the secretary as a first-run question put a half-built
    second product in front of everyone installing the first one.

    It is still a real setting and still read here — an owner who wants the agent to answer
    edits `## Calls` in settings.md, which is the same place every other rarely-changed thing
    lives. Removing the question did not remove the mode.
    """
    return owner.calls()


def where_recordings_go(interactive: bool = True) -> None:
    """Ask where call audio should be written. Enter keeps the default.

    On the console path especially: a self-hosted box is the install most likely to want this
    somewhere other than the home directory, and it is the one that never sees the settings
    page. Skipping it here would make the folder settable by macOS owners only.
    """
    if not interactive:
        return
    from . import owner as _o
    current = _o.recordings_set()
    print("\n  Where should call recordings go?")
    print(f"    default: {_o.recordings_dir()}")
    ans = _prompt(f"\n  absolute path, or Enter to keep [{current or 'default'}]: ").strip()
    if not ans:
        return
    print("  " + tools.set_setting("recordings", ans).splitlines()[0])
    print(f"  -> {_o.recordings_dir()}")


def who_you_are(interactive: bool = True) -> None:
    """The owner's name, WITHOUT a model.

    The interview below does this better — but it drives the model, and the owner this whole
    console path exists for is the one carrying calls with no key at all. For them the interview
    cannot run, and until this existed their name stayed unset.

    ONLY THE NAME. Pronoun used to be asked here too, and it is answering-agent configuration:
    how the agent refers to the owner to a CALLER. Nobody is answered on the recorder path, so
    it has no subject — and it came off the settings page for the same reason. Both surfaces
    agree that this product needs a name, which is what keeps them from drifting apart.

    That is not cosmetic. transcribe.py primes the speech engine with "A call for <name>.", and
    on a real recording that turned "Spandy Leong" into "Stanley Leong" — it beat moving to a
    bigger model. A nameless install quietly gets worse transcripts and nothing says so.
    """
    if not interactive:
        return
    from . import owner, tools
    current = owner.name()
    have = current and current != owner.DEFAULT_NAME
    print("\n  Who you are.")
    if have:
        print(f"  name: {current}")
    name = _prompt(f"\n  Your name{' [' + current + ']' if have else ''}\n  > ").strip()
    if name:
        tools.set_setting("name", name)
        print(f"  -> {name}")
    elif not have:
        print("  -> left blank. Transcripts will hear names less well; set it later in settings.md.")




#: The languages the speech engine is told about. Blank means guess, and guessing is the thing
#: this exists to discourage.
LANGUAGES = [("en", "English"), ("vi", "Vietnamese"), ("zh", "Chinese"),
             ("ms", "Malay"), ("th", "Thai")]


def choose_language(interactive: bool = True) -> None:
    """Pin the transcription language.

    ASKED, not defaulted, and asked with the reason. Left to guess, `medium` called a Singapore
    English call MALAY at 0.95 confidence and `large-v3` at 0.87 — and neither garbled it. They
    TRANSLATED, fluently, and the meaning inverted: "can I waive my credit card bill" came back
    as "can I pay my bill". A wrong transcript that reads perfectly is worse than a broken one,
    because nothing about it looks wrong.
    """
    if not interactive:
        return
    from . import owner, tools
    current = (owner.language() or "").strip()
    print("\n  What language are your calls in?")
    print("  Leaving this blank lets the engine guess, and on phone audio it guesses badly:")
    print("  an English call has been transcribed as fluent Malay, with the meaning reversed.")
    for i, (code, label) in enumerate(LANGUAGES, 1):
        print(f"    {i}  {label} ({code})")
    print("    6  something else — type the code")
    pick = _prompt(f"\n  1-6{f' [{current}]' if current else ''}: ").strip()
    if not pick:
        return
    if pick == "6":
        code = _prompt("  Language code (e.g. id, ta, hi)\n  > ").strip().lower()
    elif pick.isdigit() and 1 <= int(pick) <= len(LANGUAGES):
        code = LANGUAGES[int(pick) - 1][0]
    else:
        code = pick.lower()
    if code:
        tools.set_setting("language", code)
        print(f"  -> transcripts will be read as {code}")


def choose_quality(interactive: bool = True) -> None:
    """Which local speech model, BEFORE anything is downloaded.

    Asked first so the download that follows is the one they chose. The web hub shows the same
    four with their sizes; this is its console half.
    """
    if not interactive:
        return
    from . import owner, tools, transcribe
    if transcribe.engine() != "local":
        return                      # a key is attached: the hosted engine transcribes
    current = transcribe.local_model()
    print("\n  Which speech model? Bigger is more accurate and slower.")
    for i, model in enumerate(transcribe.TIERS, 1):
        mb = transcribe.MODEL_MB.get(model, 0)
        here = " (downloaded)" if transcribe.model_dir(model) else ""
        mark = " <- current" if model == current else ""
        print(f"    {i}  {model:16} {mb:>5} MB{here}{mark}")
    pick = _prompt(f"\n  1-{len(transcribe.TIERS)} [{current}]: ").strip()
    if pick.isdigit() and 1 <= int(pick) <= len(transcribe.TIERS):
        chosen = transcribe.TIERS[int(pick) - 1]
        tools.set_setting("transcription", chosen)
        print(f"  -> {chosen}")


def offer_speech_model(interactive: bool = True) -> None:
    """Fetch the on-machine speech model now, rather than on the first call.

    The console half of the web wizard's download step, and it matters more here: a Linux owner
    is likelier to be self-hosting on a box nobody is watching, so a silent several-hundred-MB
    download during the first real call is worse, not better.
    """
    from . import transcribe
    if not interactive or transcribe.engine() != "local" or not owner.record_calls():
        return                      # a model key means hosted, and nothing is ever downloaded
    model = transcribe.local_model()
    if transcribe.is_cached(model):
        return
    mb = transcribe.MODEL_MB.get(model, 0)
    print("\n  Calls are transcribed on this machine, so nothing is sent anywhere.")
    print(f"  That needs a one-off download of about {mb} MB ({model}).")
    if _prompt("  Download it now? [Y/n] ").strip().lower().startswith("n"):
        print("  Skipped — it downloads by itself the first time a call is transcribed.")
        return
    try:
        print("  Downloading… this can take a few minutes.")
        transcribe.fetch(model)
        print(f"  -> {model} is ready.")
    except Exception as exc:
        # NOT fatal. The model still downloads on first use; failing setup over it would be
        # refusing to finish for something that fixes itself.
        print(f"  Could not download it ({type(exc).__name__}). It will try again on "
              "the first call.")


def offer_install(interactive: bool = True) -> bool:
    """Put this build on the PATH, so `agentduet-desktop` is a command that exists.

    WHY THIS IS IN INIT AT ALL. The console is the documented path on Linux, and the last thing
    init prints is `agentduet-desktop run`. Without installing, that name is not on the PATH and
    the instruction is simply wrong — the owner has just been told to type something that
    returns "command not found". It also means the copy they are running is wherever they
    downloaded it, so tidying that folder later removes their agent.

    Running from source (not frozen) there is nothing to install: the answer is the module
    invocation, and saying so is more useful than offering a step that cannot work.
    """
    from . import install
    st = install.status()
    if not st.get("frozen"):
        return False
    if st.get("installed") and st.get("current"):
        return True
    if not interactive:
        return False
    print(f"\n  Install this build to {st.get('target')}?")
    print("  Without it, `agentduet-desktop` is not a command, and the copy you are running is")
    print("  wherever you downloaded it — moving or deleting that folder stops your agent.")
    if _prompt("  Install? [Y/n] ").strip().lower().startswith("n"):
        return False
    print("  " + install.install().replace("\n", "\n  "))
    return install.status().get("installed", False)


def main(interactive: bool = True) -> int:
    print("\n  AgentDuet Desktop — setup")
    ensure_instance()
    # paths.SEEDED, not a second migrate() call: the import already did the work, so calling it
    # again always returns nothing and reported "already present" on an instance created a
    # second earlier.
    made = paths.SEEDED
    print(f"  instance: {paths.HOME}" +
          (f"  (created {len(made)} item(s))" if made else "  (already present)"))

    # THE TWO SECRETS, TOGETHER AND FIRST. This is the part that needs a terminal: a credential
    # typed into a chat box is sent to the model provider and written to run/owner_chat.json in
    # plaintext. Everything else in setup is a conversation the owner's own assistant can have
    # (set_setting, add_knowledge, declare_capability, grant_folder are all in the registry) —
    # and a conversation is a better interview than a list of prompts.
    # WHAT IT DOES WITH A CALL, before asking for a model — because the answer decides whether
    # a model is needed at all. This is the console counterpart of the web wizard's step 2, and
    # Linux is console-first (see CLAUDE.md), so leaving it out meant a Linux owner could not
    # choose the mode at all without hand-editing settings.md.
    mode = choose_mode(interactive)

    # THE TWO SECRETS. Carrying needs neither: nobody is answered, and with the local speech
    # engine the transcript needs no credential either. This used to `return 1` on a missing
    # model regardless, so an owner who only wanted calls recorded could not finish setup — the
    # same bug cannot_answer() had, in the other surface, found the day Linux became
    # console-first.
    if mode == owner.CALLS_CARRY:
        # DO NOT EVEN ASK. Carrying answers nobody and the transcript runs on this machine, so a
        # key is not merely optional here — demanding one is the thing that sent an owner off to
        # sign up for a model they will never call. Offered, because they may want to answer
        # calls later and this is the moment they have the terminal open.
        if not llm.configured():
            print("\n  Carrying a call needs no model, so this step is optional.")
            if _prompt("  Attach one anyway, to answer calls later? [y/N] ").strip().lower()\
                    .startswith("y"):
                attach(interactive)
        else:
            print(f"  model: {llm.describe()}")
    # A FETCH IN FLIGHT COUNTS. attach() answers "is a model usable NOW", which is False while
    # gigabytes are still arriving — stopping setup there would punish the owner for choosing
    # the option we just offered them.
    elif not attach(interactive) and not _models_coming():
        print("\n  Setup stopped: to ANSWER calls the agent needs a model."
              "\n  Run `agentduet-desktop init` again once you have a key,"
              "\n  or choose to have calls put through to you instead.")
        return 1
    # SIGN-IN FIRST, because it makes the manual step unnecessary rather than
    # additional: it provisions the connector server-side.
    connected = sign_in(interactive) or connect(interactive)

    # WHO, then HOW THEY ARE HEARD. Both work with no model, which is the point: this is the
    # path a Linux owner self-hosting a recorder walks, and every step above may have been
    # declined without stopping them.
    who_you_are(interactive)
    choose_language(interactive)
    choose_quality(interactive)
    where_recordings_go(interactive)
    offer_speech_model(interactive)
    installed = offer_install(interactive)

    # THE INTERVIEW IS THE DEFAULT NOW (2026-08-11). It used to be opt-in — "your AI assistant
    # can do this over the mcp, better than these prompts" with a (y/N) that defaulted to NO —
    # which is correct only for an owner who HAS an assistant. This is being packaged for small
    # vendors handed a binary, and for them the default path ended setup with the agent not
    # knowing their name, having declined a step whose alternative they do not own. Skipping is
    # still one keystroke, so nobody with an assistant is forced through it.
    # ONLY WITH A MODEL. The interview hands the answers to the model and lets it write the
    # files, so with no key it fails at the first question — and the owner most likely to have
    # no key is the one this console path serves. who_you_are() above covers the settings it
    # would have set; this adds what the owner DOES, which needs the drafting.
    # "does" is the Who section of owner.md — the thing the interview writes and the only one
    # of its outputs who_you_are() does not already cover.
    knows_what_they_do = bool(tools.current_setup().get("does", "").strip())
    # ANSWER MODE ONLY. The interview asks what the owner does and writes it into knowledge —
    # material for an agent explaining them to a stranger. Nobody is explained to anybody on the
    # recorder path, so for a carrying owner it is three questions with no consumer.
    #
    # This used to be gated on a model alone, which was nearly the same thing while a model
    # meant a paid key. Local models changed that: a recorder owner can now attach qwen2.5:3b in
    # two clicks and would have been interviewed about their business for an agent that never
    # speaks.
    if (interactive and mode != owner.CALLS_CARRY and llm.configured()
            and not knows_what_they_do):
        print("\n  One more, and this one uses your model: what do you do?")
        print("  It writes the answer into your knowledge, in the third person.")
        print("  Press Enter to answer, or type s to skip.")
        if not _prompt("\n  > ").strip().lower().startswith("s"):
            print("\n  " + (interview() or "(no summary returned)").replace("\n", "\n  "))

    # NAME THE COMMAND THAT ACTUALLY WORKS. `agentduet-desktop` is only on the PATH once this
    # build is installed; from source it never is. Printing it unconditionally told the owner
    # to type something that returns "command not found" as the last thing setup ever said.
    how = "agentduet-desktop" if installed else (
        sys.executable + " -m agentduet_desktop.cli" if not getattr(sys, "frozen", False)
        else str(pathlib.Path(sys.argv[0]).resolve()))
    # ONE COMMAND PER BLOCK, description above it. From source `how` is a full interpreter
    # path and a trailing aligned column runs off the terminal, wrapping every line.
    print(f"""
  Next — start it, and see what it can do:

    {how} run
    {how} status
""" + ("" if not installed else
       "  Installed, so it comes back by itself after a reboot.\n") + f"""
  Optional, if you use a coding assistant (Claude Code, Goose):

    {how} connect
""" + ("" if connected else
       "\n  No connector yet, so nobody outside can reach it. Run init again when you have one.\n"))
    return 0
