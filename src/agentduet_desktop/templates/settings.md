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

## Record calls
<!-- yes (default) — an answered call is saved as audio under run/recordings/answered/,
     one file for the caller and one for the agent. The written transcript is kept either way.
     no             — keep only the transcript.

     You are recording someone. Whether they must be told, and by whom, depends on where you
     and they are. This software does not announce it for you. -->
yes

## Transcription
<!-- How hard the on-machine speech engine tries. Only used when no model key is attached.
     fast      quickest, smallest download (~145 MB), least accurate
     balanced  the default (~484 MB)
     accurate  better (~1.5 GB)
     max       best (~3 GB) — still transcribes a call in about a quarter of its length

     Your name from above is used to help it hear names correctly, whichever you pick.

     Transcribing happens after the call, on a queue, so nothing waits for it. If your
     transcripts are missing words, this is the dial. -->
balanced

## Language
<!-- The language your calls are in, as a code: en, vi, zh, ms, th. Leave empty to guess.
     Only the on-machine speech engine uses this, and guessing is unreliable on phone audio —
     an English call has been detected as Vietnamese and transcribed as nonsense. If your
     transcripts come back in the wrong language, set this. -->

## Never say
<!-- Topics never to state on your behalf, however readable the source. One per line. -->
