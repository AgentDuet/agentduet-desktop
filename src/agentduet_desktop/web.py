"""Local owner site — the second face over the owner's settings and records.

Served BY the daemon, so it is up exactly when the agent is (unlike MCP, which only
exists while a host app is open). Gives you what chat is bad at — visible state — plus
a prompt window, plus live push when something escalates.

SECURITY
- Binds 127.0.0.1 only. This site can grant folder access and send messages as the
  owner; exposed on 0.0.0.0 it is a privilege-escalation target on any shared network.
- Token generated per run, written to .run/web-token, required on every request.
- The owner tool registry (secretary_tools.OWNER_TOOLS) is reached only from web_ai.py. The
  external path never reaches either module.

TWO EDITIONS. This file is the CORE, and serves the recorder on its own: carrying and recording
calls, playback, names, sign-in, settings. Everything AI — the assistant, models, speech,
summaries, the secretary — is `web_ai.py`, registered only when `edition.ai()`. A shared route
answers its core fields here and asks `web_ai` for the rest.

The owner assistant is a DIFFERENT agent from the external-facing one: different system
prompt, full access, and the owner tool registry. Never share that code path.
"""

import asyncio
import html
import json
import logging
import os
import signal
import pathlib
import secrets
from datetime import datetime

from aiohttp import WSMsgType, web

from . import edition
from . import legal
from . import owner
from . import paths
from . import settings

logger = logging.getLogger("secretary.web")

HERE = pathlib.Path(__file__).parent      # install dir: web.html / sim.html
RUN = paths.RUN
TOKEN_FILE = RUN / "web-token"
HOST, PORT = "127.0.0.1", int(os.getenv("SECRETARY_WEB_PORT") or edition.port())


def _phone_mic() -> str:
    from . import phone
    return phone.mic_state()


def macperms_sandboxed() -> bool:
    from . import macperms
    return macperms.sandboxed()


def _playable(folder, names) -> dict:
    """{"audio": path, "seconds": length} for a call whose merge is done, else {}.

    The merge only: both sides in one file. A single leg is half a conversation.
    """
    from . import carry
    if len(names) != 1 or folder == carry.legs():
        return {}
    path = folder / names[0]
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            seconds = w.getnframes() / float(w.getframerate() or 1)
    except (OSError, EOFError, wave.Error):
        return {}
    return {"audio": str(path), "seconds": round(seconds, 1)}


def make_app(token: str) -> web.Application:
    # The assistant is built on demand by web_ai (assistant.owner_chat), never here: built once
    # at startup it stayed None for the life of a first run, before any model existed.
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
        # AGENTDUET AI has nothing to infer from: no line, no connector. Setup is done when the
        # owner finishes it, and not before.
        if not edition.calls():
            return True
        from . import connector
        return bool(owner.setup_pending(deep=True)) or not connector.configured()

    def _asset(path) -> str:
        """An asset's text, ALWAYS decoded as UTF-8 — never with the machine's locale.

        `Path.read_text()` with no `encoding=` uses `locale.getencoding()`. That is UTF-8 on
        Linux and macOS, which is why every one of these calls looked fine for a year, and
        **cp1252 on Windows**, which cannot decode a UTF-8 continuation byte. `web.html` holds
        72 em-dashes, so the very first Windows build anyone ran reached the wizard (setup.html
        happens to be cp1252-clean, so it rendered and the build looked healthy) and then 500'd
        the instant setup completed, with the hub unreachable — issue #5.

        These files are UTF-8 on disk whatever the machine believes, so it is STATED here rather
        than inferred per call site. One function because seven call sites drift: the eighth page
        someone adds would be written the way the other seven read, and this bug only shows up on
        a platform most of us never build on.
        """
        return pathlib.Path(path).read_text(encoding="utf-8")

    def _page(name: str) -> web.Response:
        """One of the HTML pages — or 404 where this build has none. The recorder edition ships
        no pages (edition.AI_DATA): its windows are native, and the frozen hub holds the
        assistant."""
        if not (HERE / name).is_file():
            return web.Response(status=404, text="not found")
        return web.Response(text=_asset(HERE / name), content_type="text/html")

    async def index(request):
        if not authed(request):
            return web.Response(status=401, text="bad or missing token")
        return _page("setup.html" if needs_setup() else "web.html")

    async def setup_page(request):
        """The first-run WIZARD. Reachable later too — it reconciles rather than duplicating."""
        if not authed(request):
            return web.Response(status=401, text="bad or missing token")
        return _page("setup.html")

    async def settings_page(request):
        """Changing things afterwards: direct fields, no steps, no welcome.

        Separate from the wizard because they are different jobs. The wizard is an interview
        that lets the MODEL write prose; settings is a form where CODE writes exactly what was
        typed. Merging them made one page apologise for being both.
        """
        if not authed(request):
            return web.Response(status=401, text="bad or missing token")
        return _page("settings.html")

    async def app_css(request):
        """The house style, shared by every page.

        NO TOKEN. It is a stylesheet with nothing in it worth protecting, and requiring one
        would mean the browser could not cache it across the pages that link it. The site binds
        loopback only regardless.
        """
        return web.Response(text=_asset(HERE / "app.css"), content_type="text/css",
                            headers={"Cache-Control": "no-cache"})

    async def icon_font(request):
        """The icon font, from the binary rather than from Google.

        NO TOKEN, like `app.css` and for the same reasons: there is nothing in a font worth
        protecting, and a token would defeat browser caching across the three pages that use it.

        `no-cache`, LIKE THE STYLESHEET — which is a correction. This shipped as
        `max-age=31536000, immutable` on the reasoning that a font changes only when the icon
        list does. True, and it misses what happens WHEN it changes: the owner's browser would
        keep the old file for a year, so adding an icon would ship a build whose new glyph
        renders as its ligature name on every machine that had ever loaded the old one. The same
        staleness bit the logo the day it was made transparent — the window went on drawing the
        white version from cache while the daemon served the new one.
        `no-cache` means revalidate, not "do not store". Over loopback that is a conditional
        request for 22 KB; the correctness is worth more than the microseconds.
        """
        path = HERE / "fonts" / "material-symbols-rounded.woff2"
        if not path.is_file():
            # SAY SO IN THE LOG. A missing font renders the ligature NAMES as text — the very
            # failure this file exists to end — and a 404 in a page's network tab is not
            # somewhere anyone looks. If this fires, the packaging dropped the file.
            logger.error("the icon font is missing from this build (%s) — icons will render "
                         "as their names", path)
            return web.Response(status=404, text="not found")
        return web.Response(body=path.read_bytes(), content_type="font/woff2",
                            headers={"Cache-Control": "no-cache"})

    #: What a page calls each setting. `settings.set_setting` answers the ASSISTANT — its message
    #: names the file and warns that settings are never quoted to anyone, which is guidance the
    #: model needs and a person reading a form does not. Shown on screen it reads as debug
    #: output leaking through, so the page gets its own sentence.
    _SETTING_LABEL = {"name": "Your name", "pronoun": "Your pronoun", "phone": "Your phone",
                      "never_say": "The never-say list", "calls": "What happens to a call",
                      "record_calls": "Call recording", "language": "Language",
                      "transcription": "Transcription quality",
                      "recordings": "Recordings folder", "thinking": "Thinking",
                      "answer_here": "Answer calls here"}

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
        from . import owner as _own
        if field == "answer_here" and _own.ANSWER_HERE_QUARANTINED:
            return web.json_response({"ok": False, "message": "Answering calls in the app is off."})
        out = settings.set_setting(field, value)
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
                            # NOT a day. The mark is part of the brand and changes with a
                            # build; a day-long cache means an upgraded install keeps drawing
                            # the old one, which is exactly what happened on 2026-09-18.
                            headers={"Cache-Control": "no-cache"})

    async def api_permissions(request):
        """The macOS permissions setup asks for. GET reports; POST acts, with a fixed verb.

        `documents` asks for the Documents folder in the background — the macOS prompt blocks the
        call that triggers it, so the page polls GET for the answer. `privacy` opens System
        Settings at Files and Folders, for an owner who refused and changed their mind: macOS
        asks only once, and after a refusal that pane is the only way back.

        The MICROPHONE is ASKED by the page itself, through the same `getUserMedia` the in-app
        phone uses, so the grant it gets is the one that phone will need; its state is reported
        here from the shell. CONTACTS are asked by the shell (`askContacts`), for the same reason
        the Documents panel is: only the app can raise the prompt.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import macperms
        if request.method == "POST":
            act = ((await request.json()) or {}).get("action", "")
            logger.info("permissions: the owner pressed %r", act)
            if act == "documents":
                macperms.request_documents()
            elif act in macperms.PRIVACY:
                macperms.open_privacy_settings(act)
            else:
                return web.json_response({"ok": False, "message": "Unknown action."})
            return web.json_response({"ok": True})
        from . import names as _names, phone as _phone
        # MICROPHONE AND CONTACTS ARE THE SHELL'S TO REPORT — each is asked of macOS by the app
        # itself (MicWatch, ContactsWatch). "" where no shell reports: a browser, or off macOS.
        mic = _phone.mic_report().get("access", "")
        book = _names.contacts_state()
        out = {
            "applies": macperms.applies(),
            # In the sandbox the Documents row asks the SHELL for the system panel.
            "sandboxed": macperms.sandboxed(),
            "documents": await asyncio.to_thread(macperms.documents_state),
            "folder": str(macperms.documents_folder()),
            "mic": mic,
            "contacts": book.get("access", ""),
            "contacts_named": len(_names.from_contacts()),
        }
        # START AT LOGIN only when asked for: it asks the shell binary, which is a subprocess,
        # and setup polls this endpoint every second while a permission is outstanding.
        if request.query.get("login"):
            from . import loginitem
            out["login"] = await asyncio.to_thread(loginitem.registered)
        return web.json_response(out)

    async def api_logs(request):
        """The logs as one zip, for Help › Export Logs… — see `logbundle` for what is in it."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import logbundle
        data = await asyncio.to_thread(logbundle.bundle)
        name = f"AgentDuet Logs {datetime.now():%Y-%m-%d %H%M}.zip"
        return web.Response(body=data, content_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def api_contact_add(request):
        """The header's Add to Contacts: a card with this person's number, and the name the hub
        shows them by if it is not just the number. Contacts asks the owner before adding it."""
        body = await request.json()
        who = str(body.get("who") or "")
        from . import links, names as _names
        if not _names.is_number(who):
            return web.json_response({"ok": False, "message": "Only a phone number can be added."})
        name = _names.name_for(who)
        msg = await asyncio.to_thread(links.add_contact, who, "" if name == who else name)
        return web.json_response({"ok": msg.startswith("Opened"), "message": msg})

    async def api_name(request):
        """The owner's name for a person. POST {who, name}; an empty name removes it, so the
        Contacts name or the number shows again. A label only — nothing is keyed on it."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import names as _names
        body = await request.json()
        who = str(body.get("who") or "").strip()
        if not who:
            return web.json_response({"error": "who"}, status=400)
        kept = _names.set_typed(who, str(body.get("name") or ""))
        return web.json_response({"ok": True, "name": _names.name_for(who),
                                  "typed": kept})

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
        settings.set_setting("start_at_login", "yes" if want else "no")
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
            out = settings.save_connector(key, uuid)
            return web.json_response({"ok": True, "message": out + "\n(Not re-tested: the "
                                      "running daemon already holds this connector.)"})
        ok, why = await connector.verify(key, uuid)
        if not ok:
            return web.json_response({"ok": False, "message": f"NOT saved — {why}"})
        return web.json_response({"ok": True, "message": settings.save_connector(key, uuid)
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

    async def api_setup_current(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import connector
        cur = settings.current_setup()
        from . import owner as _ownr
        # A SUGGESTION for setup's name field, never saved by itself — see owner.os_full_name.
        cur["os_name"] = _ownr.os_full_name()
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
        from . import carry, owner as _own, status
        cur["calls"] = _own.calls()
        cur["transcription"] = _own.transcription_quality()
        cur["language"] = _own.language()
        cur["record_calls"] = _own.record_calls()
        # As above: the badge on the setup screen means the LINE, not the owner's own number.
        cur["line"] = status.snapshot().get("number", "")
        # PREFILL, WITHOUT THE SECRET. The uuid is an identifier and typing it again is the
        # friction this removes; the key stays on disk and the endpoint reads it when the field
        # is left blank, so it never enters the page.
        from . import connector as _conn
        cur["offer_uuid"], cur["offer_key"] = _conn.offered_pair()
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
        # THE SAME QUESTION `index` asks to choose setup.html over the hub, for a UI that is not
        # a page: the native wizard is shown exactly when the HTML one would have been.
        cur["needs_setup"] = needs_setup()
        cur["oauth_available"] = connector.oauth_available()
        cur["edition"] = edition.name()
        # The native wizard asks this BESIDE needs_setup, not inside it — see legal.py.
        cur["terms_agreed"] = legal.agreed()
        if edition.ai():
            from . import web_ai
            web_ai.setup_current_extras(cur)
        return web.json_response(cur)

    async def api_terms(request):
        """GET: the Terms of Use and Privacy Policy, and whether the owner has agreed.
        POST {version}: agree — to the version that was shown, never to another (legal.agree)."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        if request.method == "POST":
            body = (await request.json()) or {}
            ok = legal.agree(str(body.get("version", "")))
            return web.json_response({"ok": ok, **({} if ok else {
                "message": "These terms have changed. Please read them again."})})
        return web.json_response(legal.state())

    async def api_state(request):
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        if edition.ai():
            from . import web_ai
            return web.json_response(web_ai.state())
        # The recorder's share of the secretary's snapshot: the channel and the view prefs.
        from . import status
        return web.json_response({"channel": status.snapshot(), "ui": settings.ui_prefs()})

    async def api_phone(request):
        """The page's phone socket: ringing and call state out, answer/hang-up in, audio both ways.

        Same token as every other route — a WebSocket carries the query string like any GET.
        Everything else is in `phone.on_page`.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import phone
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        await phone.on_page(ws)
        return ws

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
        from . import carry, owner as _own, reveal as _reveal, status
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

        out = {
            "name": _own.name() if _own.name() != _own.DEFAULT_NAME else "",
            "phone": _own.phone(),
            # THE LINE CALLS ARRIVE ON, shown beside the connection light when the owner's own
            # number is not set. The old title-bar line badge never showed it: both pages rendered `phone`, the OWNER'S OWN mobile, whose
            # docstring says it is never disclosed to anyone. Invisible while `## Phone` was
            # empty, which is why it survived. The real value is learned from an inbound call —
            # see status.py — so it is blank until one arrives, and the badge stays hidden.
            "line": status.snapshot().get("number", ""),
            "calls": _own.calls(),
            "channel": status.snapshot().get("channel", ""),
            "storage": str(carry.recordings()),
            # The setting as WRITTEN, not as resolved — the field must show what the owner
            # typed, or an empty box would read as "no folder set" when the default is in use.
            "recordings_set": _own.recordings_set(),
            "can_reveal": _reveal.available()[0],
            "can_pick": _reveal.can_pick()[0],
            # In the sandbox the chooser is the shell's system panel, not osascript.
            "sandboxed": macperms_sandboxed(),
            "dirs": {"calls": str(carry.recordings()),
                     "answered": str(carry.recordings() / carry.ANSWERED)},
            # WHAT IS ACTUALLY ON, not what the design shows switched on. Two of these have
            # nothing behind them yet and say so rather than rendering a lit switch.
            "services": {
                "record_call": {"on": _own.record_calls(), "real": True},
                "record_message": {"on": False, "real": False},
            },
            "edition": edition.name(),
            "files": {"calls": _listing(carry.recordings()),
                      "answered": _listing(carry.recordings() / carry.ANSWERED),
                      "messages": []},
            # WHETHER A NEWER BUILD IS OUT, read from the cache a worker writes every few
            # hours. Never a network call from here: this endpoint is polled by an open page,
            # and a GitHub round trip on the request path would put the hub's responsiveness
            # at the mercy of a host the whole product is supposed to work without.
            "update": _update.state(),
            # THE IN-APP PHONE. `carry` because the toggle only means something there: in
            # answer mode the agent takes every call and there is nothing to pass through.
            # NOT "phone" — that key is the owner's own number, above.
            # QUARANTINED, `carry` reads false, which is what hides the switch on the native hub
            # and on the frozen page alike — neither page needed an edit.
            "answer_here": {"on": _own.answer_here(),
                            "carry": (_own.calls() == _own.CALLS_CARRY
                                      and not _own.ANSWER_HERE_QUARANTINED),
                            "quarantined": _own.ANSWER_HERE_QUARANTINED,
                            # Whether the owner could be heard now — the shell's report. The page
                            # says why in red while the switch is on and it is not "ok".
                            "mic": _phone_mic()},
        }
        if edition.ai():
            from . import web_ai
            web_ai.panel_extras(out)
        return web.json_response(out)

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
        from . import calls, carry

        folder = carry.recordings()

        def _call(r: dict) -> dict:
            """One call in full: its audio and transcript, joined to the files on disk."""
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
            return {
                "at": r.get("at", ""), "call_id": r.get("call_id", ""),
                "mode": r.get("mode", ""), "files": len(names), "bytes": audio,
                # THE ONE FILE TO PLAY: the merge, both sides in one — never a single leg,
                # which is half a conversation. Empty until the merge is done.
                **_playable(af, names),
                "transcript": text,
                # GIVEN UP ON, said as that: "pending" for a transcript that failed is a promise
                # nothing is keeping. A memory failure goes back in the queue (transcribe.py).
                "transcript_failed": not text and carry.transcript_failed(names),
                # Empty WAVs are what an unbridged call leaves behind; saying so beats
                # showing a call that looks recorded and plays nothing.
                "silent": bool(names) and audio <= len(names) * carry.EMPTY_WAV_BYTES,
                # NOTHING WAS CAPTURED, which is not the same as "not transcribed yet".
                # `silent` requires files, so a call with none fell through to the page's
                # "Transcript pending." — promising a transcript that can never arrive. That
                # is the state a carried call is in whenever the platform hands us no audio,
                # so it would have said "pending" for ever.
                "norecording": not names,
                # NOBODY PICKED UP, said as that rather than as a missing file. Recorded
                # explicitly since 2026-09-25 ("missed"); for rows before that, a CARRIED call
                # with no audio from either side is the same fact structurally — carried calls
                # are always recorded, so empty legs mean no one was ever connected.
                "missed": (r.get("note", "").startswith("missed")
                           or (not names and r.get("mode") == "carried")),
                "outgoing": bool(r.get("outgoing")),
                # When it BEGAN, where recorded — see calls.record. The page orders by it.
                "started": r.get("started", ""),
            }

        # ONE PERSON IN FULL, EVERYONE ELSE COUNTED (2026-10-03). `open` names the person whose
        # page is showing: their calls are read in full — audio found, transcript read. Everyone
        # else is a line of the list: how many calls, the newest, how many unread — counted by the
        # database (`calls.summary`), so the poll costs the same however many calls there are.
        # Reading every call in full on every poll grew with every call ever made, which is why
        # this used to show only the newest 200 (docs/limits.md).
        # A CALLER WITHOUT `open` — the frozen HTML pages — still gets every call of the newest
        # 200 in full, exactly as before.
        open_who = request.query.get("open")
        people = []
        if open_who is None:
            for who, rows in calls.by_person().items():
                items = [_call(r) for r in rows]
                people.append({"who": who, "calls": items, "messages": [],
                               "last": items[0]["at"] if items else ""})
        else:
            for who, s in calls.summary(_seen() or {}).items():
                if who == open_who:
                    items = [_call(r) for r in calls.for_person(who)]
                    people.append({"who": who, "calls": items, "messages": [], "last": s["last"]})
                else:
                    people.append({"who": who, "calls": [], "messages": [], "last": s["last"],
                                   "call_count": s["calls"], "calls_unread": s["unread"],
                                   "last_in": s["last_in"]})

        # MESSAGES, SUGGESTIONS, HELD REPLIES AND SUMMARIES are the AI half's (web_ai). It may
        # add people the call log has never heard of — someone who only wrote — so it runs
        # before names are joined.
        if edition.ai():
            from . import web_ai
            web_ai.threads_extras(people, open_who)

        # A READABLE NAME where one arrived with the message. Joined here rather than stored on
        # the row, so it follows whatever the last message said the person is called.
        try:
            seen = json.loads((paths.RUN / "sessions.json").read_text())
        except (OSError, json.JSONDecodeError):
            seen = {}
        from . import names as _names
        # WHICH NAME is names.py's to decide: typed, then Contacts, then the message's own.
        # `name_from` lets the page say where it came from; asking the shell for Contacts names
        # happens here because this is the list of everyone who has called or written.
        _names.want(p["who"] for p in people)
        _names.prune_typed()
        for p in people:
            p["display"] = _names.name_for(p["who"], seen)
            p["name_from"] = _names.source_of(p["who"]) if p["display"] else ""
            p["contact_id"] = _names.contact(p["who"]).get("id") or ""
            p["messages"].sort(key=lambda m: m["at"])
            latest = [p["last"]] + [m["at"] for m in p["messages"]]
            p["last"] = max([x for x in latest if x] or [""])
        _mark_unread(people)
        people.sort(key=lambda p: p["last"], reverse=True)
        return web.json_response({"people": people, "folder": str(folder)})

    #: WHAT THE OWNER HAS SEEN, per person: the newest item's time when they last opened that
    #: person's history. Kept by the daemon, not the page, so the badge survives a restart and
    #: reads the same in the window and in a browser.
    SEEN = paths.RUN / "seen.json"

    def _incoming(p: dict) -> list[str]:
        """The items that can be NEW to the owner: calls in, and messages from the person.
        A call the owner placed, or a reply they sent, is not news to them. A person in the list
        only (not open) brings their newest incoming call as `last_in`, for the first look."""
        return ([c["at"] for c in p["calls"] if not c.get("outgoing") and c.get("at")]
                + [m["at"] for m in p["messages"] if m.get("them") and m.get("at")]
                + ([p["last_in"]] if p.get("last_in") else []))

    def _seen() -> dict | None:
        try:
            return json.loads(SEEN.read_text())
        except (OSError, ValueError):
            return None

    def _mark_unread(people: list[dict]) -> None:
        """Set `unread` on each person: incoming items newer than what the owner last saw.

        THE FIRST TIME, everyone starts as seen — otherwise every existing conversation would
        light up at once the day this shipped. After that a new person has no mark, so their
        first call counts.
        """
        seen = _seen()
        first = seen is None
        if first:
            seen = {p["who"]: max(_incoming(p) or [""]) for p in people}
            try:
                SEEN.parent.mkdir(parents=True, exist_ok=True)
                SEEN.write_text(json.dumps(seen))
            except OSError:
                pass
        for p in people:
            mark = seen.get(p["who"], "")
            # A LIST LINE's calls were counted by the database (`calls_unread`); its `last_in` is
            # only for the first look, which marks everything seen.
            unread_calls = 0 if first else p.pop("calls_unread", 0)
            p.pop("calls_unread", None)
            p["unread"] = unread_calls + sum(
                1 for at in _incoming(p) if at > mark and at != p.get("last_in"))

    async def api_seen(request):
        """The owner opened this person's history: everything in it up to now is seen."""
        if not authed(request):
            return web.json_response({"ok": False, "message": "unauthorised"}, status=401)
        body = (await request.json()) or {}
        # UP TO WHAT WAS ON SCREEN, sent by the page — not "now", which would also mark an item
        # that arrived between the page's last fetch and this request.
        who, at = (body.get("who") or "").strip(), str(body.get("at") or "")
        if not who:
            return web.json_response({"ok": False, "message": "No person."})
        try:
            seen = json.loads(SEEN.read_text())
        except (OSError, ValueError):
            seen = {}
        if at > seen.get(who, ""):
            seen[who] = at
            SEEN.parent.mkdir(parents=True, exist_ok=True)
            SEEN.write_text(json.dumps(seen))
        return web.json_response({"ok": True})

    async def api_ui(request):
        """View preferences. Server-side because the window has no localStorage — see
        settings.UI_PREFS."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        if request.method == "GET":
            return web.json_response(settings.ui_prefs())
        body = await request.json()
        out = [settings.set_ui_pref(k, v) for k, v in body.items()]
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

    async def api_signin_open(request):
        """Begin sign-in in the owner's OWN browser, and let the page wait for it.

        The sibling GET redirects the caller, which is right in a browser tab and wrong in the
        native window — see `oauth.open_consent`. This one opens the system browser and returns,
        so the page can poll `signed_in` instead of navigating away.

        The state and verifier land in the same `_pending_signin` either way, so `/callback`
        cannot tell the two entry points apart and needs no branch of its own.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import oauth
        if not oauth.available():
            return web.json_response({"ok": False, "message":
                "Sign-in is not available yet. Enter the key manually."})
        provider = (request.query.get("provider") or "").lower()
        if provider and provider != oauth.PROVIDER:
            return web.json_response({"ok": False, "message":
                f"{provider.title()} sign-in is not available yet — only Google is."})
        url, state, verifier = oauth.begin(PORT)
        _pending_signin.clear()
        _pending_signin.update(state=state, verifier=verifier)
        # ON A THREAD: launching a browser spawns a process, and this handler is on the loop
        # that also carries call audio.
        opened = await asyncio.to_thread(oauth.open_consent, url)
        # THE URL COMES BACK EITHER WAY, so a page whose browser did not open can offer it to
        # paste — the same fallback the console flow has always printed.
        return web.json_response({"ok": opened, "url": url})

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
        await asyncio.to_thread(settings.set_setting, "recordings", chosen)
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
        token, so it must not render anything that would try to call the API with one.

        ESCAPED (CodeQL py/reflective-xss, 2026-10-06): `detail` can be the provider's
        `error_description`, which is whatever the address says — and this route takes no token,
        on a known port, so any page could send the owner's browser here with a script in it.
        The token never reaches a script (it is not in a cookie or in storage), which kept the
        harm small; the escaping removes it."""
        title, detail = html.escape(title), html.escape(detail)
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
            # An App Store build is updated by the store: no check, no "newer version" row.
            "store_updates": macperms_sandboxed(),
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
        if macperms_sandboxed():
            return web.json_response({"update": _upd.state()})
        row = await asyncio.to_thread(_upd.check)
        return web.json_response({"update": row})

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/setup", setup_page),
        web.get("/app.css", app_css),
        web.get("/fonts/material-symbols-rounded.woff2", icon_font),
        web.get("/settings", settings_page),
        web.post("/api/setup/setting", api_setup_setting),
        web.post("/api/setup/connector", api_setup_connector),
        web.post("/api/setup/login-item", api_setup_login_item),
        web.post("/api/name", api_name),
        web.post("/api/contacts/add", api_contact_add),
        web.get("/api/logs", api_logs),
        web.get("/logo.png", logo),
        # Browsers ask for this unprompted, and the console filled with a 404 on every page load.
        web.get("/favicon.ico", logo),
        web.post("/api/quit", api_quit),
        web.get("/api/setup/current", api_setup_current),
        web.get("/api/terms", api_terms),
        web.post("/api/terms", api_terms),
        web.get("/api/state", api_state),
        web.get("/api/panel", api_panel),
        web.get("/api/phone", api_phone),
        web.get("/api/threads", api_threads),
        web.get("/api/permissions", api_permissions),
        web.post("/api/seen", api_seen),
        web.post("/api/permissions", api_permissions),
        web.post("/api/handover", api_handover),
        web.get("/api/install", api_install),
        web.post("/api/install", api_install),
        web.get("/api/connector/signin", api_connector_signin),
        web.post("/api/connector/signin/open", api_signin_open),
        web.post("/api/connector/signin", api_connector_signin),
        web.post("/api/connector/signout", api_connector_signout),
        web.post("/api/reveal", api_reveal),
        web.post("/api/pick-folder", api_pick_folder),
        web.get("/callback", oauth_callback),
        web.get("/api/ui", api_ui),
        web.post("/api/ui", api_ui),
        web.get("/api/about", api_about),
        web.post("/api/about/check", api_about_check),
    ])
    if edition.ai():
        # THE AI HALF, only in the full edition — the recorder build does not contain web_ai.
        from types import SimpleNamespace
        from . import web_ai
        extra, background = web_ai.routes(SimpleNamespace(authed=authed, asset=_asset, here=HERE,
                                                          port=PORT))
        app.add_routes(extra)
        for coro in background:
            app.cleanup_ctx.append(lambda a, c=coro: _background(a, c))
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
    global PORT
    token = _token()

    # A STALE ADDRESS MUST NOT OUTLIVE THE RUN that wrote it: with a port macOS picks, the last
    # run's number is wrong, and a reader would wait on it.
    (paths.RUN / "site-url").unlink(missing_ok=True)
    runner = web.AppRunner(make_app(token))
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    # THE PORT ACTUALLY BOUND, which is not PORT when that was 0 (edition.port()). Written back
    # so anything that reads web.PORT later — the sign-in redirect, a log line — sees the real one.
    PORT = site._server.sockets[0].getsockname()[1]

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
