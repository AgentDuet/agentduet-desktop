"""The owner site's AI half: the assistant, models, speech, summaries, suggestions, the secretary.

Split out of `web.py` on 2026-10-03 for the RECORDER edition (see `edition.py`), which leaves
this module out entirely. `web.make_app` registers `routes(ctx)` only when `edition.ai()`, and
asks the `*_extras` functions below for the AI fields of the routes both editions share — so a
shared route's core answer is written once, in `web.py`, and this file only adds to it.

The handlers are nested in `routes()` exactly as they were nested in `make_app`, so their
closures read the same names; `ctx` hands over the few that `make_app` owns.
"""

import asyncio
import json
import logging
import os

from aiohttp import WSMsgType, web

from . import assistant
from . import capabilities
from . import llm
from . import owner
from . import paths
from . import secretary_tools
from . import tools

logger = logging.getLogger("secretary.web")

#: One background speech-model download at a time, and its outcome. Module-level so the hub's
#: panel can report it as well as the route that starts it.
STT = {"running": False, "error": "", "model": ""}


def _pick_payload() -> dict:
    """The machine's pick, in the shape both pages need — ONE builder, so Settings and the setup
    wizard cannot disagree about which model this machine should run.

    `why` rides along for diagnostics and is NEVER rendered: it is a note about how the pick was
    made, which is our machinery rather than an outcome the owner acted on.
    """
    from . import models
    p = models.pick()
    # THE OVERRIDE WINS, so the card offers exactly what a developer asked for.
    key = llm.intended_model()
    spec = models.spec_of(key) or {}
    return {"model": key, "name": spec.get("name", key), "dl_mb": spec.get("dl_mb", 0),
            "downloaded": bool(key) and models.is_downloaded(key),
            "job": models.jobs().get(key) if key else None,
            "failed": models.failure(key) if key else "",
            "overridden": key == llm.override_model() != "",
            "why": "developer override" if key == llm.override_model() != "" else p["why"]}


def state() -> dict:
    """The full dashboard snapshot — the secretary's, which carries the channel and ui too."""
    return secretary_tools.state()


def setup_current_extras(cur: dict) -> None:
    """The model's part of `/api/setup/current`."""
    from . import models
    # summary(), not describe(): this line is read by the owner, and describe() answers a
    # log's question — provider key, credential kind, client health. /api/state still
    # carries describe() for diagnostics.
    cur["model"] = llm.summary()
    cur["model_name"] = llm.current_model()
    # THE QUARANTINE, and what the card offers instead of a picker: this machine's pick,
    # only while it is not yet here. See llm.CHOICE_QUARANTINED.
    cur["choice_quarantined"] = llm.CHOICE_QUARANTINED
    cur["pick"] = _pick_payload()
    # What the Advanced card's field shows: the repo and file the override resolved to.
    _o = llm.override_model()
    _os = (models.spec_of(_o) or {}) if _o else {}
    cur["model_override"] = f"{_os.get('repo')}:{_os.get('filename')}" if _os else ""
    # Explicit booleans. The pages used to infer "configured" from describe()'s prose, which
    # is a sentence written for a human and not a contract.
    cur["model_configured"] = llm.configured()
    # THE TOGGLE ONLY EXISTS FOR SOME MODELS. Gemini has no dial and Claude reasons
    # adaptively already, so showing a switch there would promise a change it cannot make.
    # The page hides the row rather than disabling it: a switch that does nothing is worse
    # than no switch, and the reason is not visible from the row itself.
    cur["thinking"] = owner.thinking()
    cur["thinking_possible"] = llm.supports_thinking()


def panel_extras(out: dict) -> None:
    """Speech, the model and the decision model, for `/api/panel`."""
    from . import decider, transcribe
    local_ok, _ = transcribe.available()
    out["services"]["transcribe"] = {"on": local_ok, "real": False}
    out["services"]["connect_ai"] = {"on": llm.configured(), "real": False}
    # Whether the Apple Neural Engine option may be OFFERED. It is not built, so this
    # only decides enabled-vs-disabled and the reason shown beside it.
    out["ane"] = dict(zip(("supported", "why"), transcribe.ane_support()))
    # The decision model's download, for the hub's progress bar (decider.py).
    out["decider"] = decider.progress()
    out["stt"] = {"engine": transcribe.engine(), "model": transcribe.local_model(),
                  "quality": owner.transcription_quality() or "balanced",
                  "cached": transcribe.is_cached(),
                  # THE DOWNLOAD, so the hub can show it after setup has finished: the speech
                  # model is 2.4 GB and usually still arriving when the owner reaches the hub.
                  "name": transcribe.display_name(transcribe.local_model()),
                  "mb": transcribe.MODEL_MB.get(transcribe.local_model(), 0),
                  "got_mb": transcribe.size_on_disk(transcribe.local_model()),
                  "running": STT["running"], "error": STT["error"]}
    # `name` so the assistant pane can say WHICH model is answering — a local 135M
    # and a hosted frontier model give very different replies, and the owner cannot
    # otherwise tell which one they are talking to.
    out["model"] = {"configured": llm.configured(),
                    "name": llm.current_model(),
                    "describe": llm.summary(),
                    # So the Assistant tab can show a download the wizard started, which
                    # usually outlasts setup: 4.8 GB takes longer than two screens.
                    "pick": _pick_payload()}


def threads_extras(people: list[dict], open_who: str | None = None) -> None:
    """Messages, suggestions, held replies and summaries, for `/api/threads`."""
    from . import asker_actions, brief, suggest
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
    # WHAT WAS ARRANGED, if anything. A stored verdict only — `for_texts` reads a file and
    # never calls a model, because this is a polled endpoint. The digest is computed from
    # the SAME text the pass judged, which is why the two callers of
    # `carry.transcript_of` have to be one function: judge one text and render another and
    # a suggestion would cite words that are not on the screen.
    for p_ in people:
        keys = {}
        # ONLY CALLS READ IN FULL: the others are the list's light entries, with no transcript
        # (web.api_threads, `open`), and nothing on their cards is shown.
        for c in p_["calls"]:
            if "transcript" in c:
                keys[id(c)] = c["transcript"]
        for m in p_["messages"]:
            keys[id(m)] = "\n".join(x for x in (m["them"], m["us"]) if x)
        found = suggest.for_texts(list(keys.values()))
        for row in (*p_["calls"], *p_["messages"]):
            if id(row) not in keys:
                continue
            key = suggest.digest(keys[id(row)])
            hit = found.get(key)
            row["suggest"] = {**hit, "key": key} if hit else None
    # STILL WAITING, and the QUEUE is what says so. `reply_to` holds an undeliverable
    # reply in the person's own file and flushes it the next time they write, so asking the
    # queue needs no second flag and cannot go stale: a message that has left it has gone
    # out, and the mark disappears on its own without anything having to remember to clear
    # it. Only asked for people the owner has actually written to.
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
    # THE SUMMARY on their page (2026-10-01): the running brief, shown where the owner
    # already looks at them — a file read, so it costs the poll nothing.
    # ONLY THE OPEN PERSON'S when the hub says who is open: one file per person on every poll
    # grew with the list, and only the page that is showing draws one.
    for p in people:
        if open_who is not None and p["who"] != open_who:
            continue
        rec = brief.load(p["who"])
        p["summary"], p["summary_at"] = rec.get("summary", ""), rec.get("updated", "")


def routes(ctx) -> tuple[list, list]:
    """The AI routes, and the background tasks they need: (routes, coroutines taking the app)."""
    authed, _asset, HERE, PORT = ctx.authed, ctx.asset, ctx.here, ctx.port
    _stt = STT

    def _chat():
        # THE SHARED ONE, not a private instance. A WhatsApp message from the owner's own number
        # reaches the same assistant, and both surfaces persist to the same file — two instances
        # would silently overwrite each other's history.
        return assistant.owner_chat()

    def _forget_chat():
        assistant.forget_owner_chat()

    sockets: set[web.WebSocketResponse] = set()          # owner view — full state
    asker_sockets: set[web.WebSocketResponse] = set()    # asker side — pings only

    async def secretary_page(request):
        """The secretary's own view — people, threads, escalations.

        Still here, and still the whole of the second product. `/` is the recorder's hub now
        because that is what a new install IS (see CLAUDE.md, "Two products, one binary"), not
        because this stopped working.
        """
        if not authed(request):
            return web.Response(text="unauthorised", status=401)
        return web.Response(text=_asset(HERE / "secretary.html"), content_type="text/html")

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

    async def api_summary_correct(request):
        """Correct… on a person's Summary card: the owner's words, folded in by the model.

        No approval card: the owner typed it, on the person's own page. The assistant's
        `correct_brief` needs one only because a caller's words may be in its context.
        """
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        body = await request.json()
        who, said = str(body.get("who") or ""), str(body.get("correction") or "")
        from . import brief as _brief
        msg = await asyncio.to_thread(_brief.correct, who, said)
        return web.json_response({"ok": msg.startswith("Corrected"), "message": msg,
                                  "summary": _brief.load(who).get("summary", "")})

    async def api_setup_decider(request):
        """The decision model's download: GET its progress, POST to start it (decider.py)."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import decider as _dec
        if request.method == "POST":
            _dec.start()
        return web.json_response(_dec.progress())


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
                                else transcribe.display_name(model)),
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

    def models_llm_forget(name):
        from . import llm as _l
        return _l.forget_key(name)

    async def api_model_override(request):
        """Set or clear the developer override. An empty name clears it.

        Setting it only REGISTERS the model: nothing downloads here. The Model card then offers
        exactly this model's download, the same one button the automatic pick uses.
        """
        if not authed(request):
            return web.json_response({"ok": False, "message": "unauthorised"}, status=401)
        from . import models
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            tools._forget_env([llm.OVERRIDE])
            return web.json_response({"ok": True, "message": "Override removed."})
        try:
            repo, file = await asyncio.to_thread(models.resolve_hf, name)
            key = await asyncio.to_thread(models.add_custom, repo, file)
        except ValueError as exc:
            return web.json_response({"ok": False, "message": str(exc)})
        except RuntimeError as exc:
            return web.json_response({"ok": False, "message": f"Hugging Face could not be read: {exc}"})
        os.environ[llm.OVERRIDE] = key
        tools._write_env({llm.OVERRIDE: key})
        return web.json_response({"ok": True, "message": f"Override set: {repo} \u00b7 {file}"})

    async def api_stt_override(request):
        """Set or clear the speech-model override, by name. An empty name clears it.

        CHECKED ON THE WAY IN. `transcribe.local_model()` quietly falls back to the default on a
        name it does not know — right for a call that must still be transcribed, wrong for a
        developer who typed `large-v3-trubo` and would otherwise believe it took. So the name is
        refused here, and a legacy tier adjective (`fast`, `accurate`…) is still accepted.
        """
        if not authed(request):
            return web.json_response({"ok": False, "message": "unauthorised"}, status=401)
        from . import transcribe
        body = await request.json()
        name = (body.get("name") or "").strip().lower()
        known = transcribe._known_models() | {transcribe.QWEN} | set(transcribe.QUALITY)
        if name and name not in known:
            return web.json_response({"ok": False, "message": f"Unknown speech model: {name}"})
        tools.set_setting("transcription", name)
        if not name:
            return web.json_response({"ok": True, "message": "Override removed."})
        return web.json_response({"ok": True, "message": "Override set: "
                                  + transcribe.display_name(transcribe.local_model())})

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
            "current": _llm.current_model(),
            # Which of the two branches the owner is on, so the page opens on the right one
            # rather than making them re-declare a choice they already made. EMPTY when the
            # name is not one anything serves — an upgraded install holds an Ollama tag, and
            # `provider()` routes that to gemini, which would open the page on the hosted
            # branch and hide the very list they need.
            "provider": (_llm.provider() if _llm.recognised(_llm.current_model())
                         else ""),
            "configured": _llm.configured(),
            # The setup wizard offers exactly this under the quarantine, rather than the list.
            "choice_quarantined": _llm.CHOICE_QUARANTINED,
            "pick": _pick_payload(),
        })

    async def api_stt_model(request):
        """Delete one downloaded speech model. Downloading is `POST /api/setup/stt`."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import transcribe
        body = await request.json()
        msg = await asyncio.to_thread(transcribe.delete_model, (body.get("model") or "").strip())
        return web.json_response({"ok": msg.startswith("Deleted"), "message": msg})

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
        return web.Response(text=_asset(HERE / "sim.html"), content_type="text/html")

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
        return web.Response(text=_asset(canvas.page_for(rec.get("capability", ""))),
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

    async def api_search(request):
        """What was said on calls that matches `q` — by meaning and by keyword (search.py).
        `who` narrows it to one person. Each hit carries the name the hub shows."""
        if not authed(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        from . import names, search
        q, who = request.query.get("q", ""), request.query.get("who", "")
        hits = await asyncio.to_thread(search.search, q, who)
        for h in hits:
            h["display"] = names.name_for(h["person"])
        return web.json_response({"q": q, "results": hits})

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

    return [
        web.get("/secretary", secretary_page),
        web.get("/api/setup/about", api_setup_about),
        web.post("/api/setup/about", api_setup_about),
        web.get("/api/setup/stt", api_setup_stt),
        web.post("/api/setup/stt", api_setup_stt),
        web.post("/api/summary/correct", api_summary_correct),
        web.get("/api/setup/decider", api_setup_decider),
        web.post("/api/setup/decider", api_setup_decider),
        web.get("/api/setup/questions", api_setup_questions),
        web.post("/api/setup/model", api_setup_model),
        web.post("/api/setup/interview", api_setup_interview),
        web.get("/api/setup/examples", api_setup_examples),
        web.post("/api/setup/example", api_setup_example),
        web.get("/api/models", api_models),
        web.post("/api/provider/key", api_provider_key),
        web.get("/api/models/hf", api_hf),
        web.post("/api/models", api_model_action),
        web.post("/api/model-override", api_model_override),
        web.post("/api/stt-override", api_stt_override),
        web.post("/api/stt-model", api_stt_model),
        web.post("/api/chat", api_chat),
        web.post("/api/resolve", api_resolve),
        web.post("/api/send", api_send),
        web.get("/api/chat_history", api_chat_history),
        web.get("/api/search", api_search),
        web.post("/api/chat_new", api_chat_new),
        web.get("/api/proposals", api_proposals),
        web.post("/api/proposal", api_proposal),
        web.post("/api/suggestion", api_suggestion),
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
    ], [watch_log]
