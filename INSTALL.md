# Installing DDuet Desktop — alpha 2

A secretary that runs on your own machine. It answers people who call or message you, escalates
anything it should not decide alone, and can only act inside limits you set.

**This is an alpha.** Things listed under "Known rough edges" are known — please don't spend time
reporting them.

---

## Before you start

Two things, and **one of them has to come from Stanley**:

| | what | where from |
|---|---|---|
| A model key | DashScope (Alibaba Qwen), Gemini, or Anthropic | your own account, or ask |
| **A B3 connector** | gives your install a phone number and message channel | **ask Stanley — you need your own** |

Please do not reuse someone else's connector. Only one machine may hold a connector at a time;
a second one fights the first for the same number.

**If you want to test phone calls, use DashScope.** It is the only provider wired for voice
today. Gemini and Anthropic work for messages but the phone will not answer.

---

## macOS

1. Open the `.dmg` and drag **DDuet Desktop** to your Applications folder.
2. **Right-click the app → Open → Open.** Do not double-click.
   The app is not signed yet, so a normal double-click will be refused with *"the developer
   cannot be verified"*. This is expected. You only need to do this once.
3. Your browser opens on the setup page. Continue at **Setting it up** below.

The build is for **Apple Silicon** (M1/M2/M3/M4). Check with  Apple menu → About This Mac →
"Chip". If it says Intel, tell Stanley — you need a different build.

## Linux

The binary is a single file. A file manager will not run it by double-clicking, so the first
launch is one command:

```bash
chmod +x ./dduet-desktop
./dduet-desktop
```

Your browser opens on the setup page. **Step 1 installs it properly** — after that it is on your
applications menu and available as `dduet-desktop` in a terminal.

---

## Setting it up

Four steps in the browser. Two of them are credentials, which is why they are here and not in a
chat with your assistant — a key typed into a chat box is sent to a model provider and stored in
plain text.

1. **Install** — puts the binary somewhere permanent. Worth doing: your AI assistant is told
   where to launch the secretary from, and moving the file later breaks that link.
2. **A model** — paste your key. It is checked with the provider before it is saved, so a wrong
   paste fails here rather than silently later.
3. **Your number** — the B3 connector. Also checked before saving. You can skip it; everything
   local still works, you just never hear from anyone.
4. **Your assistant** — registers the secretary with Claude Code, Goose, or whatever you use.
   **Restart your assistant afterwards** — it starts its tools when it starts.

Then tell your assistant: **"what is my setup status?"** It can do the rest — who you are, what
you do, what the secretary is allowed to act on. That part is a conversation, not a form.

---

## Check it is working

```bash
dduet-desktop status      # what is running, and what this build can do
dduet-desktop --version   # quote this in any bug report
```

Or ask your assistant *"is my secretary running?"* — it has tools for starting and stopping it.

If you have a connector, **call your number**. The agent should answer, introduce itself with
your name, and offer a callback if it cannot help.

---

## Known rough edges

Please don't report these — they are on the list.

- **macOS shows a security warning** on first open. Right-click → Open. Signing is not done yet.
- **No desktop notifications** except on Linux. Escalations reach you when you ask your
  assistant, not before.
- **Nothing starts at login.** If you reboot, start it again.
- **Voice is weaker than text.** On a call, nothing can check a sentence before it is spoken. The
  agent is told to answer only from what it looks up, and the transcript is recorded — but treat
  what it says on the phone as less controlled than what it writes.
- **Calls may fail with silence.** A shared provider limit; the agent hangs up rather than
  leaving you on a dead line. Try again.

---

## When something goes wrong

Send Stanley:

1. `dduet-desktop --version`
2. `dduet-desktop status`
3. `~/.dduet/run/daemon.log`

And say what you expected and what happened — including **"it went quiet"**, which is a real
failure mode on calls and does not always show up anywhere else.

## Where your data lives

Everything is on your machine, in `~/.dduet` — settings, what it knows, who has contacted you,
call transcripts. Nothing is uploaded except what the model provider sees to answer a question.
Deleting that folder resets it completely.
