# AgentDuet Desktop

Answers your phone, or records it — on your own machine.

Two products ship in one binary, and a new install is the first of them:

- **The recorder.** A call is carried through to your phone, both sides are recorded, and the
  audio is transcribed locally. Two people talk. Nobody is answered, nothing is decided.
- **The secretary.** An agent picks up, speaks for you, and escalates what it should not
  decide. Configured later, by someone who wants it.

They are mutually exclusive per install: one connector has one call handler, so a call is
either carried or answered.

## Install

Download a build from [Releases](../../releases). macOS (Apple Silicon) and Linux x86-64.

```
agentduet-desktop init      # set it up
agentduet-desktop run       # start it
agentduet-desktop status    # what is running, and what this build can do
```

macOS and Windows set up in a browser page; **Linux sets up in the console**, because that is
where someone self-hosting is. Both surfaces exist everywhere — this is about which one is
documented.

## What it does today

| | |
|---|---|
| **Record calls** | both legs, to a folder on your machine |
| **Transcribe** | on-device via faster-whisper, or a hosted model if you attach a key |
| **Answer calls** | speech-to-speech, if you configure the secretary |
| **Messages** | WhatsApp, through the AgentDuet SDK |

**Transcription runs on your machine by default.** With no model key at all, calls are still
transcribed — nothing is sent anywhere. Pick the model size to trade speed for accuracy.

**Set your language.** Left to guess, speech models are unreliable on phone audio: an English
call has been transcribed as fluent Malay, with the meaning reversed. `## Language` in
`settings.md`, or the Transcribe panel.

## Configuration

Everything lives in `$AGENTDUET_HOME` (default `~/.agentduet-desktop`):

```
settings.md        parsed by heading — name, pronoun, call mode, language, transcription
knowledge/         what the agent may tell an outside caller
people/            what it knows about individual contacts
run/recordings/    audio and transcripts
.env               credentials, mode 0600
```

`settings.md` is parsed **by heading**, so keep the headings.

## Building

```
uv build --wheel
pyinstaller --distpath dist-bin packaging/agentduet-desktop.spec
```

`./dev.sh` runs from source in a few seconds against the same instance — the pages are read per
request, so an HTML change needs only a browser refresh.

## Tests

```
python tests/test_rules.py       # no model, no venv — the invariants
python tests/test_isolation.py   # the asker path cannot reach the owner's tools
python tests/test_wasm.py        # the customer-tool sandbox
```

`test_behaviour.py` drives a real model and needs a key.

## How it is built

The agent reads; **code decides**. Every judgement a model makes is checked mechanically before
anything happens: disclosure follows an explicit folder grant, an action must fit declared
bounds, and a document can never grant the right to act. Customer tools run in a WebAssembly
sandbox that cannot choose its own destination.

- [`docs/design.md`](docs/design.md) — the architecture, and what would reverse each decision
- [`docs/tool-surface-risk.md`](docs/tool-surface-risk.md) — the attack class the tool split prevents
- [`docs/thesis.md`](docs/thesis.md) — why an agent is a UI and its tools are APIs

## Known gaps

- **Carrying a call records nothing yet** — the bridge works, but the platform does not hand the
  app the conference audio.
- **Single sign-on is a stub.** Configure the connector by hand in Settings.
- **SMS archiving does not exist**, and the cloud-summary path is not wired.
- **Apple Silicon only** on macOS. No Windows build yet.

## Recording other people

Carrying a call records two people talking. Whether they must be told, and by whom, depends on
where you and they are. **This software does not announce it for you.**
