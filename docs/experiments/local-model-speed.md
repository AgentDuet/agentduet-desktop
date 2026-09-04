# Local model speed on Apple Silicon — measured 2026-09-03

Machine: **Apple M5, 10 cores (4P/6E), 16 GB unified memory**, macOS 26.6.2.
Engine: `llama-cpp-python` built from source with `-DGGML_METAL=on`, `n_gpu_layers=-1`.

Prompted by "the local model is slow": answering *"Hi, are you there?"* in the app took
**26 seconds**. It was not the hardware.

## A realistic assistant turn

Three questions a call-recorder owner would actually ask, each with a ~600-token system prompt,
averaged. Wall time is what the owner waits for.

| configuration | wall/turn | CPU/turn | output tokens | `<think>` chars | visible answer |
|---|---|---|---|---|---|
| Qwen3 8B, thinking on (default) | **10.84s** | 0.29s | 237 | 955 | 93 chars |
| Qwen3 8B, `/no_think` | **1.50s** | 0.02s | 18 | 2 | 56 chars |
| Llama 3.2 3B | **0.74s** | 0.02s | 23 | 0 | 106 chars |

**Thinking costs 7.2x.** Qwen3 is a reasoning model and monologues before answering — on a
two-word greeting it wrote 454 tokens of `<think>` and 41 characters of reply, which is where the
26 seconds went. It is not generating a better answer; 91% of the tokens are discarded.

**tok/s is the wrong metric here and is left out on purpose.** `/no_think` shows *fewer* tokens
per second (12 vs 22) while being seven times faster, because a short answer is dominated by
reading the 600-token prompt rather than writing 18 tokens. Wall time per turn is what an owner
experiences.

## Thinking does not just cost time — at these settings it produces nothing

Asked *"What's the last 4 digits of 12345678"*:

| | wall | result |
|---|---|---|
| `/no_think` | **4.0s** | `### Final Answer: 5678` |
| thinking on | **92.2s** | **no answer** — 6,118 characters, all of it an UNCLOSED `<think>` block |

The thinking run spent the whole `max_tokens=2048` budget counting the digits, restating the
number and decomposing 12,345,678 into millions and thousands, then stopped mid-sentence:
"…12,000,000 + 345,000 = 12,345,000. Then add 678 to get 12,". It never emitted `</think>`, so
it never began the answer.

That is the case for the default, and it is stronger than speed: at this app's settings a
reasoning model reliably returns NOTHING on a question a 3B answers instantly. It is also why
`_without_thinking` keeps the words of an unclosed block instead of returning "" — the
alternative here is ninety-two seconds of silence.

A CAUTION ON MEASURING THIS. The first check for correctness was `"5678" in answer`, which is
worthless: the question contains 12345678, so any reply that repeats it passes. Read what the
model concluded, not whether the digits appear.

## Is it using the GPU? Yes.

| | |
|---|---|
| layers offloaded | `37/37` (`qwen3-8b`), `0/37` before the `n_gpu_layers` fix |
| GPU utilisation during inference | 97-99% |
| CPU during an 8.5s generation | **0.21s — 2%** |

The CPU ratio is the load-bearing evidence: CPU inference would burn 30+ CPU-seconds over the
same wall time, the way faster-whisper burns 37 for 14 seconds of audio.

## Why a bigger model cannot be made faster here

Decoding is memory-bandwidth-bound — every token reads the whole model out of memory:

    4.795 GB x 23.9 tok/s ~= 115 GB/s

A base M-series chip has roughly 120 GB/s, so an 8B at Q4 is running at ~95% of everything this
machine has. There is no setting that improves it. The only lever is a smaller model, and the
scaling is close to linear in file size: 4.8 GB -> 1.9 GB measured 2.25x faster, against 2.5x
predicted.

## What follows

1. ~~**A reasoning model needs thinking suppressed for this app**~~ **DONE 2026-09-03.**
   `llm._Local.complete` appends Qwen3's `/no_think` switch unless the caller passed
   `think=True`, and strips `<think>` from what it returns. The same question that took 26
   seconds in the app answers in **1.46s**. The `think` parameter already existed on that
   interface and nobody ever passed True, so thinking had been unintended all along — it was
   simply never implemented for the local provider, whose chat template reasons by default.

   **The switch goes in a SYSTEM message.** Appending `/no_think` to the owner's prompt looked
   equivalent and is not: this GGUF's template does not strip it, so it arrives as literal text.
   Asked *"What's the last 4 digits of 12345678"* the model replied that the question was
   incomplete, quoted the switch back, and took 13.2s. From a system message the same question
   answers in 3.66s, against 92s with thinking left on.
2. **A non-reasoning model is the better default on 16 GB.** Llama 3.2 3B was the fastest here
   AND produced the most visible answer, at 40% of the resident memory.
3. `agentduet-desktop models` marks 8B "fits" on this machine, which is true of memory and
   misleading about experience. Worth saying something about speed there.

## What we are NOT going to chase (decided 2026-09-03)

**Arithmetic and literal-recall accuracy from a local model on a laptop.** With thinking off,
the 8B answered "the last 4 digits of 123455787" as `787`. That is wrong, and it is not a bug to
fix — it is what a 4-bit 8B on 16 GB does today. Chasing it would mean a bigger model this
machine cannot hold, or thinking, and thinking is worse:

- it does not merely cost time, it **fails to terminate**. 92.2s and the whole 2048-token budget
  spent counting digits, then truncated mid-arithmetic with no answer at all.
- it is the same shape as the spin the `repeat_penalty` comment in llm.py already records, where
  glm-4-9b emitted one phrase 339 times until max_tokens stopped it.

So thinking stays off, and the local model is scoped to what it is good at: summarising a
transcript, drafting a reply, answering questions about text that is in front of it. **A number
the owner needs to be right should come from the transcript, not from the model's arithmetic** —
which is also why Apple's speech engine writing `91234567` rather than spelling out the digits
matters more here than any model choice.

If a local model must be relied on for exact recall later, the lever is retrieval — put the
digits in the prompt and ask it to quote them — not a larger model or a longer think.

## Method

`docs/experiments/` holds the scripts. Each configuration loads the model fresh, runs the three
prompts, and averages; `resource.getrusage` gives CPU time around the generation call only, and
GPU utilisation is sampled from `ioreg -c IOAccelerator` while a sentinel file confirms
generation has begun. Model load time is excluded — it is ~0.5s and paid once per process.
