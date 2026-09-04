# Settings

Parsed BY HEADING by the code — keep the headings. Never retrieved, never quoted to anyone.

## Name
<!-- Your name, as the agent should sign and refer to you. -->

## Pronoun
<!-- How the agent refers to you to OUTSIDE parties: "he/him", "she/her", "they/them".
     Leave empty and it uses your name rather than guessing — it once inferred "he" from a
     name, and you are not in the room to correct a guess made to a third party. -->

## Voice
Warm but brief. Two or three sentences. Plain words, no corporate padding.
Never over-promise; if unsure, say the owner will follow up.

## Phone
<!-- Your own number in E.164 (+6591234567), for the agent to RING YOU on — never given out.
     Leave empty and the agent will not offer a callback: it must not promise what the code
     cannot do. -->

## Calls
<!-- answer  — the agent picks up and speaks for you. Needs a model key.
     carry   — the call is bridged onward to your phone system and BOTH SIDES ARE RECORDED
               to run/recordings/. Nobody is answered by the agent in this mode.

     Only one applies: a call is either answered or carried, never both.

     `carry` IS THE DEFAULT because carrying is the product — see "Two products, one binary"
     in CLAUDE.md. Answering is the second product and needs a model key.

     Know what the default means: you are recording two people talking. Whether they have to be
     told, and by whom, depends on where you and they are. This software does not announce it
     for you, and nobody has answered that question for us either. -->
carry

## Messages
<!-- What happens when someone messages you — on DDUET (a person writing to your business
     account) or WhatsApp.

     carry   — the message is relayed to you. You read it in the app and you answer it.
               The assistant helps: it transcribes, summarises and drafts. It does not send.
     answer  — the agent answers as you, from what it knows. Needs a model key.

     `carry` IS THE DEFAULT, and it is the same decision as Calls above: two humans talk, we are
     the junction, nobody is impersonated.

     This setting did not exist until 2026-08-28, and its absence had a consequence worth
     knowing about: a `carry` install still answered chats as its owner, because the message
     path went straight to the agent with nothing to consult. If you have been running this
     app and someone got a reply you did not write, that is why. -->
carry

## Recordings
<!-- Where call audio and transcripts are written. An ABSOLUTE path.

     Empty means the default, inside this instance:  <AGENTDUET_HOME>/run/recordings

     Changing this does NOT move recordings already on disk. New ones go to the new folder and
     the old ones stay where they are — moving gigabytes of audio is not something a settings
     change should do behind your back. `agentduet-desktop status` always reports the folder
     actually in use. -->


## Record calls
<!-- yes (default) — an answered call is saved as audio under run/recordings/answered/,
     one file for the caller and one for the agent. The written transcript is kept either way.
     no             — keep only the transcript.

     You are recording someone. Whether they must be told, and by whom, depends on where you
     and they are. This software does not announce it for you. -->
yes

## Transcription
<!-- Which speech engine and model run on this machine.

     LEAVE THIS EMPTY for the best available here. On macOS 26 or newer with Apple Silicon that
     is Apple's own on-device engine: measured on a real 222-second call it was 19x faster than
     Whisper for about a fifteen-hundredth of the CPU, which on a laptop is the difference
     between fans and silence. It also writes spoken digits as 91234567 and a spoken domain as
     b3networks.com. Everywhere else, empty means Whisper's default below.

     apple           prefer Apple's engine
     (empty)         the same thing — Apple where it can serve your language, else Whisper

     Neither value can override the Language setting: if Apple has no model for the language
     you asked for, Whisper transcribes that call instead. Your language wins over your engine,
     because a transcript in the wrong language is worse than a slower one.

     Apple's engine has no language detection and only some languages — English everywhere,
     plus Chinese, Japanese, Korean and the major European ones. It has NO Malay, Vietnamese,
     Tamil or Thai, and told the wrong language it returns fluent nonsense rather than an error.
     So if the Language setting below names a language it does not have, Whisper is used for
     that instead, automatically.

     Naming a Whisper model means Whisper — choosing large-v3 is choosing an engine, not asking
     for a faster one that ignores the choice. Whisper's own names, smallest first:

     small           ~464 MB
     medium          ~1.5 GB
     large-v3-turbo  ~1.6 GB   THE DEFAULT. large-v3's accuracy at roughly half its time —
                               same encoder, decoder cut from 32 layers to 4
     large-v3        ~2.9 GB   the most accurate

     No tiny or base: fast, and not accurate enough for a phone call. No distil-* models
     either — faster again, and ENGLISH ONLY, while the Language setting above offers
     Vietnamese, Chinese, Malay and Thai.

     The old names — fast, balanced, accurate, max — still work and mean base, small, medium and
     large-v3.

     Your name from above is used to help it hear names correctly, whichever you pick.

     Transcribing happens after the call, on a queue, so nothing waits for it. If your
     transcripts are missing words, this is the dial. -->


## Thinking

no

<!-- Only some models can do this: a downloaded Qwen3 or DeepSeek-R1, or hosted Qwen. Gemini
     and Claude ignore it — Claude reasons adaptively already — so on those it is not offered.

     OFF by default, and measured rather than assumed. Asked "Hi, are you there?", Qwen3 8B
     spent 454 tokens deliberating and 26 seconds where suppressing it answered in 1.46. On a
     question that invites re-checking it does not converge at all: "what are the last 4 digits
     of 12345678" exhausted 2,048 tokens with NO answer on both models and at both
     temperatures, and given 8,192 the 1.7B needed 6,877 reasoning tokens and 172 seconds — to
     answer what it gets right in 1.0 second with thinking off.

     So this is here to experiment with, not to leave on. `yes` turns it on; anything else
     leaves it off, because a typo must not silently make every answer a hundred times slower. -->

## Language
<!-- The language your calls are in, as a code: en, vi, zh, ms, th. Leave empty to guess.
     Only the on-machine speech engine uses this, and guessing is unreliable on phone audio —
     an English call has been detected as Vietnamese and transcribed as nonsense. If your
     transcripts come back in the wrong language, set this. -->

## Never say
<!-- Topics never to state on your behalf, however readable the source. One per line. -->
