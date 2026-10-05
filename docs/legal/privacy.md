# AgentDuet — Privacy Policy

> **DRAFT for review — not in force.** Written 2026-10-05 by engineering as a starting point for
> B3 legal and GRC. Every statement about what the App does was checked against the code on that
> date. Statements about what B3's platform keeps are marked [ENG]/[GRC] and need confirming.
> Reviewers: the DPO and legal, then security-grc.
> Not legal advice.

Last updated: [date]

This policy explains what personal data AgentDuet, AgentDuet Recorder and AgentDuet AI (the
"App") handle, where it goes, and who is responsible for it. The App is provided by B3 Networks
Pte Ltd ("B3", "we").

## The short version

- **Your recordings, transcripts, summaries and messages are stored on your computer, not by
  us.** You decide what is recorded and you are responsible for it under data protection law.
- **Calls and messages pass through B3's platform** to reach your computer. B3 keeps a record
  of each call (numbers, times, length), but not its audio. [ENG: confirm before publishing.]
- **The AI runs on your computer.** Call and message content is not sent to a cloud AI. The
  models are Qwen3-ASR (speech to text), Google Gemma 4 (summaries, the assistant), Google
  EmbeddingGemma (search) and Strands Decider. Their output can be wrong; see the Terms of Use.
- **We do not collect usage analytics or telemetry** from the App.

## 1. Data stored on your computer

The App creates and keeps on your computer:

- recordings of both sides of each call, and transcripts and summaries of them;
- messages received on your number;
- a list of your calls (numbers, times, length, direction);
- names it reads from your Mac's Contacts, to label callers. Contacts never leave your computer;
- your settings and, if you use the assistant, what you have told it.

This data stays in the folder you choose and in the App's own folder on your computer. **B3 has
no access to it.** It is deleted when you delete it; uninstalling the App does not delete your
recordings.

**For this data, you are responsible** under the PDPA and similar laws: telling the people on
your calls, getting their consent where required, keeping it secure, and answering their
requests about it. See section 2 of the Terms of Use.

## 2. Data that passes through B3

To carry calls and messages, B3's platform handles:

- **your AgentDuet account**: the email address and name from the sign-in you use (for example
  your Google account), and the connector that links the App to your number;
- **call audio and message content**, in transit to your computer. [ENG to confirm with the
  platform team: none is stored on the AgentDuet call path.]
- **call and message records**: numbers, times, length and routing, kept for [3 years, per B3's
  record of processing — confirm once the retention schedule, security-grc #12, is set] for
  [billing, fraud prevention and support — confirm purposes].

**Who is responsible for what.** For your account (sign-in identity and email), B3 is the
controller: we decide how it is used. For call audio and messages carried to you, B3 acts as a
data intermediary on your behalf, and you are the organisation responsible for them. As an
intermediary, B3 still protects that data and keeps it no longer than needed, as the PDPA
requires. See also [b3networks.com/data-policy].

## 3. Other services the App contacts

| Service | When | What it receives |
|---|---|---|
| Hugging Face (huggingface.co) | when an AI model is downloaded, usually once at setup | your IP address and which model is requested |
| GitHub (api.github.com) | to check for a new version, about four times a day | your IP address and the App's version number |
| AgentDuet sign-in (auth.agentduet.com) and your identity provider | when you sign in | what you approve on the sign-in screen |
| Google Calendar, your email app | only when you click a link the App made | the event or email text you chose to open |

None of these receive your recordings, transcripts or messages.

**One exception, off by default:** if you set up the assistant to answer calls for you, those
calls are handled by a hosted AI service, which receives the call audio. [Name the provider and
link its terms before that feature is offered.]

## 4. Permissions the App asks for

- **Documents folder**: to save recordings in the folder you chose.
- **Contacts**: to show callers' names. Read on your computer only.
- **Start at login**: optional, so calls are recorded after a restart.

The App does not ask for the microphone or camera.

## 5. Security

Recordings are ordinary files on your computer and are protected by your computer's own
security, such as your login password and FileVault disk encryption. We recommend you turn
FileVault on. The App's control page is reachable only from your own computer, and only with a
key kept in its folder.

## 6. Children

The App is not intended for anyone under [18].

## 7. Your rights

For data B3 holds (section 2), you can ask us to access, correct or delete it at [privacy email].
For data on your computer (section 1), you hold it, so you can view, export or delete it
yourself. Requests from people on your calls about that data are yours to answer.

## 8. Changes

We will update this policy when the App's handling of data changes, and show you significant
changes in the App.

## 9. Contact

B3 Networks Pte Ltd, [address]. Data Protection Officer: [name/email].

---

**Engineering notes for the reviewer** (remove before publishing)

- The HTML pages load fonts from Google Fonts, but the Mac app does not use those pages. If the
  Windows version ships with them, add Google Fonts to section 3, or bundle the fonts (already
  on the checklist).
- Hosted AI providers (Gemini, Claude, and others) exist in the code but are switched off
  (`llm.CHOICE_QUARANTINED`). If they are switched back on, sections 3 and "The short version"
  must change in the same release.
- "We do not collect telemetry" was checked against the code on 2026-10-05: no analytics,
  crash-reporting or telemetry library, and the only outbound hosts are the ones in section 3
  (plus the quarantined providers above). The GitHub check sends only the version, as its
  User-Agent. Keep it true, or change this policy first.
- B3's RoPA lists S3 call recordings (3 years) for other products. Before publishing, the
  platform team must confirm the AgentDuet carry path writes none, or "B3 keeps no audio" is
  false. Same for WhatsApp message content.
