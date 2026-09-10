"""Local owner site — the second face over tools.py.

Served BY the daemon, so it is up exactly when the agent is (unlike MCP, which only
exists while a host app is open). Gives you what chat is bad at — visible state — plus
a prompt window, plus live push when something escalates.

SECURITY
- Binds 127.0.0.1 only. This site can grant folder access and send messages as the
  owner; exposed on 0.0.0.0 it is a privilege-escalation target on any shared network.
- Token generated per run, written to .run/web-token, required on every request.
- Uses secretary_tools.OWNER_TOOLS. The external path never reaches this module.

The owner assistant is a DIFFERENT agent from the external-facing one: different system
prompt, full access, and the owner tool registry. Never share that code path.
"""

import asyncio
import json
import logging
import os
import signal
import pathlib
import secrets
from datetime import datetime

from aiohttp import WSMsgType, web

from . import capabilities
from . import llm
from . import owner
from . import secretary_tools
from . import tools
from . import assistant
from .assistant import OwnerChat

from . import paths

logger = logging.getLogger("secretary.web")

HERE = pathlib.Path(__file__).parent      # install dir: web.html / sim.html
RUN = paths.RUN
TOKEN_FILE = RUN / "web-token"
HOST, PORT = "127.0.0.1", int(os.getenv("SECRETARY_WEB_PORT", "8899"))










def make_app(chat: "OwnerChat | None", token: str) -> web.Application:
    # Built once at startup, `chat` stayed None for the life of a FIRST RUN — no model exists
    # yet, so the setup interview could never run in the session that attached one. Everything
    # below goes through _chat(), built on demand and reset when the credential changes (which
    # also makes switching models later take effect without a restart).
    state = {"chat": chat}

    def _chat():
        # THE SHARED ONE, not a private instance. A WhatsApp message from the owner's own number
        # reaches the same assistant, and both surfaces persist to the same file — two instances
        # would silently overwrite each other's history.
        return assistant.owner_chat()

    def _forget_chat():
        assistant.forget_owner_chat()

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
                      "transcription": "Transcription quality",
                      "recordings": "Recordings folder", "thinking": "Thinking"}

    def _saved(field: str, value: str) -> str:
        """How the page says a setting was stored. The ONLY phrasing for it."""
        label = _SETTING_LABEL.get(field.lower().replace(" ", "_").replace("-", "_"), field)
        shown = value.strip().splitlines()[0][:60] if value.strip() else ""
        # Cleared, not "saved" with nothing after it — and the state line under it shows what
        # the value fell back to, so the two together say what happened and what is in force.
        return f"{label} saved — {shown}." if shown else f"{label} cleared."

    async def api_setup_setting(request):
        """Set one owner setting directly — no model involved."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        field = (body.get("field") or "").strip()
        value = body.get("value") or ""
        out = tools.set_setting(field, value)
        if out.lower().startswith("unknown"):
            return web.json_response({"ok": False, "message": out})
        return web.json_response({"ok": True, "message": _saved(field, value)})

    async def logo(request):
        """The brand mark. NO TOKEN, deliberately.

        Everything else on this site is behind the per-machine token, because everything else is
        the owner's data. This is a logo: it is in the binary, identical for every install, and
        reveals nothing. Requiring the token would mean the favicon 404s — browsers do not send
        query strings they were not given — which is the noise this route exists to stop.
        """
        path = pathlib.Path(__file__).parent / "logo.png"
        if not path.is_file():
            return web.Response(status=404)
        return web.Response(body=path.read_bytes(), content_type="image/png",
                            headers={"Cache-Control": "max-age=86400"})

    async def api_setup_login_item(request):
        """Record whether this machine should start the app at login, and make it so.

        RECORDED AS WELL AS APPLIED, because the two answer different questions. The system
        registration is the truth about what happens at login; the setting is what the OWNER
        asked for, which is what `status` should report and what an owner re-running setup
        should see already ticked.

        Applied through `loginitem.apply`, which picks the mechanism: the app bundle registers
        itself with SMAppService, and everything else writes the plist, systemd unit or Startup
        shortcut. Never both — see that module for why two registrations is worse than none.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import loginitem
        body = await request.json()
        want = bool(body.get("want"))
        tools.set_setting("start_at_login", "yes" if want else "no")
        said = await asyncio.to_thread(loginitem.apply, want)
        logger.info("start at login -> %s: %s", want, said)
        return web.json_response({"ok": True, "want": want, "message": said})

    async def api_setup_connector(request):
        """Verify the B3 connector, then save it. Never reachable from the assistant."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import connector
        body = await request.json()
        key, uuid = (body.get("key") or "").strip(), (body.get("uuid") or "").strip()
        # A BLANK FIELD FALLS BACK TO THE FILE, so a reset instance is one click. `~/.agentduet`
        # and `~/.connector` live outside $AGENTDUET_HOME on purpose — wiping the instance to
        # simulate a fresh install must not wipe the credential. Anything typed still wins.
        key, uuid = connector.fill_from_files(key, uuid)
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
        from . import owner as _own, transcribe

        model = transcribe.local_model()
        needed = _own.record_calls()
        if request.method == "GET":
            return web.json_response({
                # NAME THE ENGINE. The page used to describe the situation in a sentence,
                # which buried the two facts that matter: which engine, and where it runs.
                # NAME THE ENGINE THAT WILL RUN, which is not always Whisper. This said
                # f"Whisper {model}" unconditionally, so the card read "Transcription engine:
                # Whisper" on a Mac transcribing every call with Apple's on-device engine —
                # while `status` said "Apple on-device" and every log line said `apple`. The
                # comment above it even said a second engine returning must not make the page
                # quietly untrue, and then the line below hardcoded the first one.
                "engine": transcribe.engine(),
                "engine_name": ("Apple on-device" if transcribe.engine() == "apple"
                                else f"Whisper {model}"),
                # THE FOUR TIERS, with what each costs and whether it is here. The page offered
                # them by adjective alone, so "balanced" and the engine line's "Whisper small"
                # were the same model under two names and read as a contradiction.
                "tiers": transcribe.catalogue(),
                "needed": needed, "model": model,
                "mb": transcribe.MODEL_MB.get(model, 0),
                "cached": transcribe.is_cached(model),
                # WHICH model is coming down, not just that one is — the page has a row per
                # model and needs to know where to put the progress.
                "running": _stt["running"], "downloading": _stt.get("model", ""),
                "error": _stt["error"],
                "installed": transcribe._local_available()})

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

    async def api_setup_current(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import connector
        cur = tools.current_setup()
        # summary(), not describe(): this line is read by the owner, and describe() answers a
        # log's question — provider key, credential kind, client health. /api/state still
        # carries describe() for diagnostics.
        cur["model"] = llm.summary()
        cur["model_name"] = os.getenv("SECRETARY_MODEL", "")
        # Explicit booleans. The pages used to infer "configured" from describe()'s prose, which
        # is a sentence written for a human and not a contract.
        cur["model_configured"] = llm.configured()
        cur["connector_configured"] = connector.configured()
        # No longer "live on the next restart" — the channel loop polls the environment, so a
        # saved connector connects within seconds.
        cur["connector"] = ("Connected. Calls and messages can reach you."
                            if connector.configured()
                            else "Not connected, so nothing can reach you yet.")
        cur["connector_uuid"] = os.getenv(connector.UUID, "")
        cur["connected"] = connector.configured()
        cur["backend"] = connector.environment()
        # EVIDENCE THAT A KEY IS SET, without echoing it. The card said "Connected" above an
        # empty password field, which reads as a contradiction — the field was empty because a
        # password input is never populated, not because nothing was configured. Last four
        # characters only, on a loopback page behind a per-machine token.
        _k = os.getenv(connector.API_KEY, "")
        cur["key_hint"] = f"····{_k[-4:]}" if len(_k) >= 4 else ("set" if _k else "")
        # SIGN-IN STATE. The card showed a key field and a uuid field and nothing else, so an
        # owner who had signed in could not see it, could not sign out, and could not tell that
        # the key sitting in .env was being ignored.
        from . import oauth
        cur["oauth"] = {
            "available": oauth.available(),
            "signed_in": oauth.signed_in(),
            "email": oauth.email(),
            "connector": oauth.connector_uuid(),
            # Sign-in needs a browser to send the owner to. On a headless box it cannot work,
            # and offering it there is a button that goes nowhere.
            "browser": oauth.browser_available(),
        }
        # WHERE THE RECORDINGS GO, resolved on THIS machine. Never a path written into the page:
        # $AGENTDUET_HOME differs by platform and by install, and a Mac owner told to look in
        # /home/... would reasonably conclude the feature had not run. Sent as an absolute path
        # so it can be pasted into a file manager.
        from . import carry, llm as _llm, owner as _own
        cur["calls"] = _own.calls()
        cur["transcription"] = _own.transcription_quality()
        cur["language"] = _own.language()
        cur["record_calls"] = _own.record_calls()
        # As above: the badge on the setup screen means the LINE, not the owner's own number.
        cur["line"] = (secretary_tools.state().get("channel") or {}).get("number", "")
        # PREFILL, WITHOUT THE SECRET. The uuid is an identifier and typing it again is the
        # friction this removes; the key stays on disk and the endpoint reads it when the field
        # is left blank, so it never enters the page.
        from . import connector as _conn
        cur["offer_uuid"], cur["offer_key"] = _conn.offered_pair()
        # THE TOGGLE ONLY EXISTS FOR SOME MODELS. Gemini has no dial and Claude reasons
        # adaptively already, so showing a switch there would promise a change it cannot make.
        # The page hides the row rather than disabling it: a switch that does nothing is worse
        # than no switch, and the reason is not visible from the row itself.
        cur["thinking"] = _own.thinking()
        cur["thinking_possible"] = _llm.supports_thinking()
        # THE FACT WHERE IT CAN BE ASKED, the preference where it cannot — and they are
        # different questions. `owner.start_at_login()` is what the owner ASKED FOR; the macOS
        # bundle can report the real SMAppService registration, which the menu bar already showed
        # while this page showed the setting. A restored instance predating the setting therefore
        # read "off" beside a menu bar reading "on".
        from . import loginitem as _li
        _actual = _li.registered()
        cur["start_at_login"] = (_actual in ("on", "pending") if _actual is not None
                                 else _own.start_at_login())
        # "pending" is its own outcome: registered, waiting for the owner in System Settings.
        cur["start_at_login_state"] = _actual or ""
        cur["recordings_dir"] = str(carry.recordings() / carry.ANSWERED)
        cur["carried_dir"] = str(carry.recordings())
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

    async def api_provider_key(request):
        """Check a hosted provider's key and hand back the models it can actually reach.

        Separate from `api_setup_model` on purpose: that endpoint takes a key AND a model and
        proves the pair by completing, which cannot work before the owner knows what models
        exist. This one needs no model name.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        ok, msg, models = tools.save_provider_key((body.get("provider") or "").strip(),
                                                  (body.get("key") or "").strip())
        if ok:
            _forget_chat()        # rebuild the owner's assistant against the new credential
        return web.json_response({"ok": ok, "message": msg, "models": models})

    async def api_setup_model(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        # attach_model verifies BEFORE saving; the message it returns names the length and last
        # four characters only, never the key.
        out = tools.attach_model((body.get("key") or "").strip(),
                                 (body.get("model") or "").strip(),
                                 (body.get("provider") or "").strip())
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
        out = secretary_tools.install_example((body.get("name") or "").strip(),
                                    (body.get("bounds") or "").strip())
        return web.json_response({"ok": out.startswith("Installed"), "message": out})

    async def api_state(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        return web.json_response(secretary_tools.state())

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
        from . import carry, llm as _llm, owner as _own, reveal as _reveal, transcribe
        from . import update as _update

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
            # THE LINE CALLS ARRIVE ON, which is what the "Power Mobile Line" badge means and
            # never what it showed: both pages rendered `phone`, the OWNER'S OWN mobile, whose
            # docstring says it is never disclosed to anyone. Invisible while `## Phone` was
            # empty, which is why it survived. The real value is learned from an inbound call —
            # see status.py — so it is blank until one arrives, and the badge stays hidden.
            "line": (secretary_tools.state().get("channel") or {}).get("number", ""),
            "calls": _own.calls(),
            "channel": (secretary_tools.state().get("channel") or {}).get("channel", ""),
            "storage": str(carry.recordings()),
            # The setting as WRITTEN, not as resolved — the field must show what the owner
            # typed, or an empty box would read as "no folder set" when the default is in use.
            "recordings_set": _own.recordings_set(),
            "can_reveal": _reveal.available()[0],
            "can_pick": _reveal.can_pick()[0],
            "dirs": {"calls": str(carry.recordings()),
                     "answered": str(carry.recordings() / carry.ANSWERED)},
            # WHAT IS ACTUALLY ON, not what the design shows switched on. Two of these have
            # nothing behind them yet and say so rather than rendering a lit switch.
            "services": {
                "record_call": {"on": _own.record_calls(), "real": True},
                "transcribe": {"on": local_ok, "real": False},
                "record_message": {"on": False, "real": False},
                "connect_ai": {"on": _llm.configured(), "real": False},
            },
            # Whether the Apple Neural Engine option may be OFFERED. It is not built, so this
            # only decides enabled-vs-disabled and the reason shown beside it.
            "ane": dict(zip(("supported", "why"), transcribe.ane_support())),
            "stt": {"engine": transcribe.engine(), "model": transcribe.local_model(),
                    "quality": _own.transcription_quality() or "balanced",
                    "cached": transcribe.is_cached()},
            # `name` so the assistant pane can say WHICH model is answering — a local 135M
            # and a hosted frontier model give very different replies, and the owner cannot
            # otherwise tell which one they are talking to.
            "model": {"configured": _llm.configured(),
                      "name": os.getenv("SECRETARY_MODEL", ""),
                      "describe": _llm.summary()},
            "files": {"calls": _listing(carry.recordings()),
                      "answered": _listing(carry.recordings() / carry.ANSWERED),
                      "messages": []},
            # WHETHER A NEWER BUILD IS OUT, read from the cache a worker writes every few
            # hours. Never a network call from here: this endpoint is polled by an open page,
            # and a GitHub round trip on the request path would put the hub's responsiveness
            # at the mercy of a host the whole product is supposed to work without.
            "update": _update.state(),
        })

    async def api_threads(request):
        """People, and what happened with each of them.

        The recorder's view of the world: someone rang, we carried it, and there is audio and a
        transcript. No escalations, no grants, no agent conversation — those are objects that
        only exist when an agent is answering, and nobody is answered here.

        Built from `calls.jsonl` rather than the query log, and joined to the files on disk so a
        row can only claim a recording that is actually there.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import asker_actions, calls, carry, tools, transcribe

        folder = carry.recordings()
        people = []
        for who, rows in calls.by_person().items():
            items = []
            for r in rows:
                # THE MERGE IF IT IS DONE, ELSE THE LEGS. The index row is written when the
                # call ends and the merge happens later on the transcription queue, so a
                # just-finished call legitimately has legs and no merged file. Asking only for
                # the merged name would report it as "No recording." while its audio sat on
                # disk — a false claim, and the exact shape of failure this file keeps finding.
                af, names = carry.call_audio(r.get("recordings", []), r.get("call_id", ""))
                # A .wav with no sibling .txt is still in the transcription queue — that is the
                # queue, so the UI can say "pending" without a second source of truth.
                # BOTH LEGS, LABELLED. This broke out of the loop on the first transcript it
                # found, and since `names` is sorted that was the CALLEE — this line's own side.
                # Half a conversation, and the half the owner already knows. The files are
                # deliberately unmixed (see carry._Recorder: the two sides are not aligned, so
                # summing them compresses one), which is exactly why each needs saying whose it
                # is. `-caller` is always the other party and `-callee` always this line,
                # whichever way the call was set up.
                text = carry.transcript_of(names, af)[:4000]
                audio = sum((af / n).stat().st_size for n in names) if names else 0
                items.append({
                    "at": r.get("at", ""), "call_id": r.get("call_id", ""),
                    "mode": r.get("mode", ""), "files": len(names), "bytes": audio,
                    "transcript": text,
                    # Empty WAVs are what an unbridged call leaves behind; saying so beats
                    # showing a call that looks recorded and plays nothing.
                    "silent": bool(names) and audio <= len(names) * transcribe.EMPTY_WAV_BYTES,
                    # NOTHING WAS CAPTURED, which is not the same as "not transcribed yet".
                    # `silent` requires files, so a call with none fell through to the page's
                    # "Transcript pending." — promising a transcript that can never arrive. That
                    # is the state a carried call is in whenever the platform hands us no audio,
                    # so it would have said "pending" for ever.
                    "norecording": not names,
                })
            people.append({"who": who, "calls": items, "messages": [],
                           "last": items[0]["at"] if items else ""})

        # MESSAGES, from the query log. A person can be here with no call at all — someone who
        # wrote to the business account and never rang — so this both fills in threads for
        # people already listed and adds people the call log has never heard of.
        #
        # `question` is theirs and `answer` is ours, which is enough to render a thread. Who
        # SENT ours matters to the reader, so it is carried: on a carried message there is no
        # answer at all, on an owner reply the owner wrote it, and otherwise the agent did.
        by_who = {p["who"]: p for p in people}
        for r in tools.rows():
            who = r.get("asker") or ""
            # MESSAGING NETWORKS ONLY. A TELCO row is a turn inside an ANSWERED call — the
            # agent's own transcript — and those already appear as a call with its recording.
            # Listing them here would show one phone conversation twice, once as a call and
            # once as a chat that never happened.
            # AN OWNER REPLY PASSES WHATEVER THE NETWORK SAYS. It is logged with the network
            # of their stored session, and someone who has never written in HAS no session — so
            # the owner's own message was recorded with network "" and then discarded right
            # here. The same condition that makes a reply undeliverable (there is no
            # conversation to reply into) was also making it invisible, so the composer looked
            # like it did nothing at all. Ours is ours: we know it happened, whatever channel
            # it is still waiting for.
            owner_sent = r.get("outcome") == "owner_reply"
            if not who or (not owner_sent
                           and r.get("network") not in ("WA", "DDUET")):
                continue
            p = by_who.get(who)
            if p is None:
                p = {"who": who, "calls": [], "messages": [], "last": ""}
                by_who[who] = p
                people.append(p)
            p["messages"].append({
                "at": r.get("at", ""),
                "network": r.get("network", ""),
                # An owner reply has no inbound half — its `question` is the placeholder
                # "(owner reply)", which is machinery and must never render as something the
                # other person said.
                "them": "" if owner_sent else r.get("question", ""),
                "us": r.get("answer", ""),
                # WHO SPOKE FOR US. "owner" is a reply typed here; "agent" is the secretary
                # answering as them; "" means nobody answered, which is what carrying looks like
                # and is the normal case now.
                "by": ("owner" if owner_sent else ("agent" if r.get("answer") else "")),
            })
        # STILL WAITING, and the QUEUE is what says so. `reply_to` holds an undeliverable
        # reply in the person's own file and flushes it the next time they write, so asking the
        # queue needs no second flag and cannot go stale: a message that has left it has gone
        # out, and the mark disappears on its own without anything having to remember to clear
        # it. Only asked for people the owner has actually written to.
        # WHAT WAS ARRANGED, if anything. A stored verdict only — `for_texts` reads a file and
        # never calls a model, because this is a polled endpoint. The digest is computed from
        # the SAME text the pass judged, which is why the two callers of
        # `carry.transcript_of` have to be one function: judge one text and render another and
        # a suggestion would cite words that are not on the screen.
        from . import suggest as _sg
        for p_ in people:
            keys = {}
            for c in p_["calls"]:
                keys[id(c)] = c["transcript"]
            for m in p_["messages"]:
                keys[id(m)] = "\n".join(x for x in (m["them"], m["us"]) if x)
            found = _sg.for_texts(list(keys.values()))
            for row in (*p_["calls"], *p_["messages"]):
                key = _sg.digest(keys[id(row)])
                hit = found.get(key)
                row["suggest"] = {**hit, "key": key} if hit else None

        for p_ in people:
            if not any(m["by"] == "owner" for m in p_["messages"]):
                continue
            try:
                waiting = {h.get("text", "")
                           for h in asker_actions.pending_replies(p_["who"])}
            except (OSError, json.JSONDecodeError):
                waiting = set()
            for m in p_["messages"]:
                if m["by"] == "owner" and m["us"] in waiting:
                    m["held"] = True

        # A READABLE NAME where one arrived with the message. Joined here rather than stored on
        # the row, so it follows whatever the last message said the person is called.
        try:
            seen = json.loads((paths.RUN / "sessions.json").read_text())
        except (OSError, json.JSONDecodeError):
            seen = {}
        for p in people:
            p["display"] = (seen.get(p["who"]) or {}).get("display", "")
            p["messages"].sort(key=lambda m: m["at"])
            latest = [p["last"]] + [m["at"] for m in p["messages"]]
            p["last"] = max([x for x in latest if x] or [""])
        people.sort(key=lambda p: p["last"], reverse=True)
        return web.json_response({"people": people, "folder": str(folder)})

    def models_llm_forget(name):
        from . import llm as _l
        return _l.forget_key(name)

    async def api_model_action(request):
        """download | cancel | load | unload | delete — one verb per state change.

        FIVE VERBS BECAUSE THERE ARE THREE STATES. A model is absent, on disk, or resident, and
        collapsing that into "get it" and "remove it" is what left a laptop holding five
        gigabytes for a model nobody was using.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import models
        body = await request.json()
        name = (body.get("name") or "").strip()
        act = (body.get("action") or "").strip()

        async def _fetch(target: str, then_use: bool) -> dict:
            """Start a download, and REPORT WHAT HAPPENS TO IT.

            The result of the background task used to be discarded, which is how a9's failures
            presented as "the progress bar never appeared": every fetch died on a missing CA
            bundle, `models.download` returned the reason, and nothing read it.
            """
            models.forget_failure(target)
            if then_use:
                models.want(target)

            async def _go():
                try:
                    out = await asyncio.to_thread(models.download, target)
                except Exception:
                    logger.exception("download of %s raised", target)
                    models.note_failure(target, "the download stopped unexpectedly")
                    return
                logger.info("download of %s: %s", target, out)
                if not models.is_downloaded(target):
                    if not models.failure(target):
                        models.note_failure(target, out)
                    return
                # CLAIMED AT COMPLETION, not captured at the start: a "Use this" pressed while
                # the bytes were arriving still counts, and one replaced since does not.
                if models.claim_wanted(target):
                    await asyncio.to_thread(models.load, target)
                    if models.loaded() == target:
                        await asyncio.to_thread(tools.attach_model, "local", target, "local")

            # ASKED BEFORE STARTING, not after. Checking `queued()` once the task exists reads
            # the state before the task has run — `create_task` only schedules it — so a model
            # about to wait reported "Downloading". The slots as they are NOW decide what
            # happens to this request, so they are what the sentence can honestly claim.
            running, _waiting = models.slots()
            at_capacity = running >= models.MAX_CONCURRENT_DOWNLOADS

            asyncio.get_running_loop().create_task(_go())
            label = models.CATALOGUE.get(target, {}).get("name", target)
            # A QUEUE NOBODY CAN SEE is the same failure as a silent download, which is the
            # thing this whole area exists to stop.
            where = (f"Queued {label} behind {running} download"
                     f"{'s' if running != 1 else ''}") if at_capacity else f"Downloading {label}"
            return {"ok": True, "message":
                    where + "." + (" It will be used when it finishes." if then_use else "")}

        if act == "cancel":
            return web.json_response({"ok": True, "message": models.cancel(name)})
        if not name:
            return web.json_response({"ok": False, "message": "Which model?"})

        if act == "delete":
            return web.json_response({"ok": True,
                                      "message": await asyncio.to_thread(models.delete, name)})
        if act == "unload":
            return web.json_response({"ok": True, "message": models.unload()})
        if act == "download":
            # CONCURRENT, CAPPED, AND IT DOES NOT SWITCH THE MODEL IN USE. Fetching a file and
            # choosing which model answers are two decisions; welding them meant an owner who
            # wanted to compare three models had to adopt each one as it landed. `use` is the
            # other verb.
            return web.json_response(await _fetch(name, then_use=False))

        if act in ("use", "load"):
            # Downloaded → load and attach now, because loading IS selecting. Still arriving, or
            # not started → record the intent and switch when it lands. Either way the press has
            # a visible effect, which "Use this" on a 4.6 GB model did not have before.
            #
            # `load` is the older name for the same thing and stays as an alias: one
            # implementation rather than two that drift.
            if models.is_downloaded(name):
                _, msg = await asyncio.to_thread(models.load, name)
                if models.loaded() == name:
                    msg += " " + await asyncio.to_thread(tools.attach_model, "local", name,
                                                         "local")
                return web.json_response({"ok": models.loaded() == name, "message": msg})
            return web.json_response(await _fetch(name, then_use=True))

        if act == "use_hosted":
            # A key we ALREADY HOLD needs no retyping — the same "Use this" a downloaded model
            # gets. attach_model verifies against the provider before saving either way.
            msg = await asyncio.to_thread(tools.attach_model, "", name,
                                          (body.get("provider") or "").strip())
            return web.json_response({"ok": "Attached" in msg, "message": msg})

        if act == "forget_key":
            return web.json_response({"ok": True,
                                      "message": await asyncio.to_thread(models_llm_forget, name)})

        if act == "add":
            # THE ESCAPE HATCH. The owner names a repository and one file in it; the URL is
            # built here from those two, never accepted from the page — a downloader that takes
            # a URL from its caller is a request-forgery tool with a progress bar.
            try:
                key = await asyncio.to_thread(models.add_custom,
                                              body.get("repo", ""), body.get("file", ""))
            except (ValueError, RuntimeError) as exc:
                return web.json_response({"ok": False, "message": str(exc)})

            async def _go():
                await asyncio.to_thread(models.download, key)
                if models.is_downloaded(key):
                    await asyncio.to_thread(models.load, key)
                    if models.loaded() == key:
                        await asyncio.to_thread(tools.attach_model, "local", key, "local")

            asyncio.get_running_loop().create_task(_go())
            return web.json_response({"ok": True, "message":
                                      f"Downloading {body.get('file', '')}."})

        return web.json_response({"ok": False, "message": f"Unknown action {act!r}."})

    async def api_hf(request):
        """Search Hugging Face, or list one repository's GGUF files.

        Two questions on one route because they are one flow: nobody searches without then
        picking a file, and a repository is a shelf of quantisations rather than a model.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import models
        repo = (request.query.get("repo") or "").strip()
        query = (request.query.get("q") or "").strip()
        try:
            if repo:
                return web.json_response({"ok": True, "repo": repo,
                                          "files": await asyncio.to_thread(models.files, repo)})
            return web.json_response({"ok": True,
                                      "results": await asyncio.to_thread(models.search, query)})
        except (RuntimeError, ValueError) as exc:
            return web.json_response({"ok": False, "message": str(exc)})

    async def api_models(request):
        """Every model we offer, sized against this machine, with what it is FOR.

        A weight in GB is not a decision; a weight next to what this computer has is — and even
        that is not enough. The picker used to say a 3B model `fits` and an 8B was `tight`, and
        the 8B was the one that could actually do the job. So a row also carries what the model
        is good at, how fast it runs, and whether it is the one we would pick.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import llm as _llm, machine, models

        engine_ok, engine_why = models.available()
        return web.json_response({
            "machine": machine.describe(),
            "disk_free_gb": round(models.disk_free_mb() / 1024, 1),
            "engine": engine_ok,
            "engine_why": engine_why,
            "models": models.listing(),
            "hosted": _llm.hosted_listing(),
            "loaded": models.loaded(),
            # jobs_seen, not jobs: a download started by the CLI or by init's detached child is
            # not in THIS process's memory, and the page showing "not downloaded" while the file
            # grows is how an owner concludes the app cannot see its own models.
            "jobs": models.jobs_seen(),
            # Waiting for a slot, in turn order, and what "Use this" is holding for.
            "queued": models.queued(),
            "wanted": models.wanted(),
            "max_downloads": models.MAX_CONCURRENT_DOWNLOADS,
            # MB already on disk per unfinished model, so a button can say Resume rather than
            # offering to download 4.4 GB when 3.2 GB of it is already there.
            "partial": {n: got for n, got, _t in models.downloading()},
            # WHY A DOWNLOAD FAILED, per model. The page shows it on the row; without it the
            # only symptom is a button that appears to do nothing.
            "failed": {m["id"]: models.failure(m["id"]) for m in models.listing()
                       if models.failure(m["id"])},
            "current": os.getenv("SECRETARY_MODEL", ""),
            # Which of the two branches the owner is on, so the page opens on the right one
            # rather than making them re-declare a choice they already made. EMPTY when the
            # name is not one anything serves — an upgraded install holds an Ollama tag, and
            # `provider()` routes that to gemini, which would open the page on the hosted
            # branch and hide the very list they need.
            "provider": (_llm.provider() if _llm.recognised(os.getenv("SECRETARY_MODEL", ""))
                         else ""),
            "configured": _llm.configured(),
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

    async def api_stt_model(request):
        """Delete one downloaded speech model. Downloading is `POST /api/setup/stt`."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import transcribe
        body = await request.json()
        msg = await asyncio.to_thread(transcribe.delete_model, (body.get("model") or "").strip())
        return web.json_response({"ok": msg.startswith("Deleted"), "message": msg})

    async def api_reveal(request):
        """Show a folder in the desktop's file manager.

        Takes a KEY, never a path. A route that opens whatever it is handed is a way to launch
        a file manager on anything readable, from a page that is only as private as its token.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import reveal
        body = await request.json()
        msg = await asyncio.to_thread(reveal.open_folder, (body.get("folder") or "").strip())
        return web.json_response({"ok": msg.startswith("Opened"), "message": msg})

    async def api_pick_folder(request):
        """Ask the desktop for a folder, and save it as the recordings location.

        Cancelling returns ok with no change — a person closing a dialog has not failed at
        anything, and reporting it in red is how a UI teaches people to distrust its messages.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import owner as _own, reveal
        try:
            chosen = await asyncio.to_thread(reveal.pick_folder, str(_own.recordings_dir()))
        except RuntimeError as exc:
            return web.json_response({"ok": False, "message": f"Cannot show a folder chooser: {exc}"})
        if not chosen:
            return web.json_response({"ok": True, "changed": False, "message": ""})
        await asyncio.to_thread(tools.set_setting, "recordings", chosen)
        return web.json_response({"ok": True, "changed": True,
                                  "message": _saved("recordings", chosen)})

    async def api_connector_signout(request):
        """Forget the tokens.

        This is ALSO how an owner switches to an API key, and the card says so. The SDK refuses
        a config carrying both — "token_provider is a standalone auth mode: remove api_key /
        connector_uuid" — so a key entered while signed in is not a fallback, it is ignored.
        Making that a real sign-out is the difference between switching and appearing to.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import oauth
        if not oauth.signed_in():
            return web.json_response({"ok": False, "message": "Not signed in."})
        who = oauth.email() or "this account"
        oauth.sign_out()
        return web.json_response({"ok": True, "message":
            f"Signed out of {who}. Calls stop arriving until you sign in again or enter a key."})

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
        message = body.get("message", "")
        viewing = (body.get("viewing") or "").strip()

        # "SEND IT" IS CODE, NOT A MODEL TURN — see `assistant.send_if_asked`, which both owner
        # surfaces now call. It used to live here, which is why the owner asking from WhatsApp
        # got a model turn and was told the assistant can only read.
        sent = assistant.send_if_asked(_chat(), message, viewing)
        if sent is not None:
            return web.json_response({"reply": sent, "tools":
                                      ["reply_to"] if sent.startswith("Sent") else [],
                                      "proposals": []})

        # Who the owner is looking at. Without it, "what did she want?" has no "her" — the
        # assistant sits beside a conversation it cannot see, and the owner retypes a name the
        # screen is already showing.
        try:
            return web.json_response(await _chat().turn(message, viewing))
        except Exception as exc:
            # A MODEL FAILURE IS NOT A SERVER ERROR, and it used to be reported as one.
            # `RuntimeError: llama_decode returned -3` — two processes each holding a 6 GB model
            # on a 16 GB machine — arrived as a bare HTTP 500: an empty balloon, no cause, and
            # on reload a conversation in which the question had never been asked. The owner is
            # the one who can act on it, so it is answered rather than swallowed.
            logger.exception("chat turn failed")
            reply = f"That did not go through — {exc}".strip()
            chat = _chat()
            if chat is not None:
                chat.note_failure(message, reply)
            return web.json_response({"reply": reply, "tools": [], "proposals": []})

    async def api_chat_new(request):
        """Start a new conversation: drop the model's context, keep the owner's record."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        c = _chat()
        if c is not None:
            c.new_conversation()
        return web.json_response({"turns": c.shown if c else []})

    async def api_proposals(request):
        """What the assistant wants to add to the shared notes, waiting on the owner."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import assistant as _a
        return web.json_response({"proposals": _a.pending()})

    async def api_proposal(request):
        """Approve or discard one. The knowledge write happens HERE, on a click — which is the
        whole point: a model that has read a stranger's words cannot publish on its own say-so."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import assistant as _a
        body = await request.json()
        msg = _a.resolve(str(body.get("id") or ""), bool(body.get("approve")))
        return web.json_response({"message": msg, "proposals": _a.pending()})

    async def api_about(request):
        """Which build this is, where it keeps its files, and what it talks to.

        EXISTS BECAUSE A REMOTE TESTER COULD NOT ANSWER "WHICH BUILD ARE YOU ON?". Two reports
        on 2026-09-10 both turned on that question and both needed a terminal to settle: an
        update notice naming a stale version, and a feature "not working" that was simply not
        in the build being run. Neither is diagnosable from the app, and asking a tester to run
        a CLI is how a round trip becomes a day.

        `build_id()` rather than `__version__`, because during an alpha the version alone names
        a dozen binaries — the commit and the build stamp are what identify one.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import __version__, build_id, connector as _conn, paths as _paths
        from . import update as _upd, version_string
        return web.json_response({
            # `number` is the bare version for the titlebar; `version` is the full sentence for
            # the About card. Two fields rather than one, so neither surface parses the other's
            # string — the titlebar wants "0.1.0b1" and the card wants the build and where it
            # came from.
            "number": __version__,
            "version": version_string(),
            "build": build_id(),
            "instance": str(_paths.HOME),
            "backend": _conn.environment(),
            "update": _upd.state(),
        })

    async def api_about_check(request):
        """Ask GitHub now, instead of waiting for the next scheduled pass.

        ON A THREAD, like the worker: `check()` opens a socket and this is a request handler.
        Manual because the schedule is deliberately slow — up to six hours — and a tester who
        has just been told a new build exists should not have to wait for it or restart.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import update as _upd
        row = await asyncio.to_thread(_upd.check)
        return web.json_response({"update": row})

    async def api_suggestion(request):
        """Add the suggested event, or dismiss it. THE CLICK IS THE APPROVAL.

        No proposal card and no second confirmation, and the reason is that the click already
        carries everything one would: the owner is reading the words that produced the offer,
        the offer states the event and its time, and what happens next is a PREFILLED PAGE they
        still have to press Save on. A confirm step here would be asking the same person the
        same question twice.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import suggest as _sg
        body = await request.json()
        msg = _sg.resolve(str(body.get("key") or ""), str(body.get("action") or ""))
        return web.json_response({"message": msg})

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
            secretary_tools.record_delivery(who, m["text"], body.get("conversation") or "")
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
        return web.json_response({"asker": who, "rows": secretary_tools.conversation_rows(who)})

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
                    await ws.send_str(json.dumps({"type": "state", "state": secretary_tools.state()}))
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
        # DID IT GO OUT? ASK THE QUEUE, do not read the prose. `reply_to` answers in a
        # sentence written for the assistant, which is the right thing for it to return and the
        # wrong thing to pattern-match: it explains the gap and what the owner will see next,
        # neither of which belongs on screen, and any rewording breaks the match silently.
        # The delivery queue growing by one IS the fact, so the page gets told rather than
        # guessing.
        from . import asker_actions
        key, why = secretary_tools.resolve_asker(who)
        if why:
            return web.json_response({"result": why, "note": why, "held": True,
                                      "state": secretary_tools.state()})
        before = len(asker_actions.pending_replies(key))
        result = secretary_tools.reply_to(who, text)
        held = len(asker_actions.pending_replies(key)) > before
        return web.json_response({
            "result": result,
            # The OUTCOME, and nothing else. Not what we will do about it, and not a
            # description of the message the owner can see sitting there unsent.
            "note": ("Not delivered — nothing has arrived from them, so there is no "
                     "conversation to send into." if held else "Sent."),
            "held": held,
            "state": secretary_tools.state()})

    async def api_resolve(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        msg = secretary_tools.resolve_escalation((body.get("id") or "").strip(),
                                       body.get("note") or "cleared from the panel")
        return web.json_response({"result": msg, "state": secretary_tools.state()})

    async def ws_asker(request):
        """Asker-side live channel.

        Deliberately carries NO payload — just "something changed". The owner socket
        pushes secretary_tools.state(), which contains every escalation, briefing and permission;
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
                payload = json.dumps({"type": "state", "state": secretary_tools.state()})
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
        web.post("/api/setup/about", api_setup_about),
        web.post("/api/setup/connector", api_setup_connector),
        web.post("/api/setup/login-item", api_setup_login_item),
        web.get("/logo.png", logo),
        # Browsers ask for this unprompted, and the console filled with a 404 on every page load.
        web.get("/favicon.ico", logo),
        web.post("/api/quit", api_quit),
        web.get("/api/setup/current", api_setup_current),
        web.get("/api/setup/questions", api_setup_questions),
        web.post("/api/setup/model", api_setup_model),
        web.post("/api/setup/interview", api_setup_interview),
        web.get("/api/setup/examples", api_setup_examples),
        web.post("/api/setup/example", api_setup_example),
        web.get("/api/state", api_state),
        web.get("/api/panel", api_panel),
        web.get("/api/threads", api_threads),
        web.get("/api/models", api_models),
        web.post("/api/provider/key", api_provider_key),
        web.get("/api/models/hf", api_hf),
        web.post("/api/models", api_model_action),
        web.post("/api/handover", api_handover),
        web.get("/api/install", api_install),
        web.post("/api/install", api_install),
        web.get("/api/connector/signin", api_connector_signin),
        web.post("/api/connector/signin", api_connector_signin),
        web.post("/api/connector/signout", api_connector_signout),
        web.post("/api/stt-model", api_stt_model),
        web.post("/api/reveal", api_reveal),
        web.post("/api/pick-folder", api_pick_folder),
        web.get("/callback", oauth_callback),
        web.get("/api/ui", api_ui),
        web.post("/api/ui", api_ui),
        web.post("/api/chat", api_chat),
        web.post("/api/resolve", api_resolve),
        web.post("/api/send", api_send),
        web.get("/api/chat_history", api_chat_history),
        web.post("/api/chat_new", api_chat_new),
        web.get("/api/proposals", api_proposals),
        web.post("/api/proposal", api_proposal),
        web.post("/api/suggestion", api_suggestion),
        web.get("/api/about", api_about),
        web.post("/api/about/check", api_about_check),
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
