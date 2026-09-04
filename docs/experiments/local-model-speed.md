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

Decoding is memory-bandwidth-bound — every token reads the whole model out of memory. That was
asserted from one data point and has since been **checked**, on 2026-09-04, because one number in
it was wrong (below).

| model | file | decode | prefill | file x decode |
|---|---|---|---|---|
| llama-3.2-3b Q4_K_M | 1.88 GB | 54.5 tok/s | 528 tok/s | **102 GB/s** |
| qwen3-8b Q4_K_M | 4.68 GB | 24.4 tok/s | 227 tok/s | **114 GB/s** |

Three things there, and each is a separate argument:

1. **The product is near-constant across a 2.5x difference in model size** — 102 against 114
   GB/s, 11% apart. That is the signature of a fixed ceiling being hit. If decode were
   compute-bound the product would fall with size, not hold.
2. **tok/s scales as 1/size** — 2.23x measured for a 2.49x size ratio.
3. **The same weights, batched, go ~10x faster per token.** Prefill reads the identical model to
   process 1,200 tokens at once and reaches 528 / 227 tok/s. So the arithmetic on those weights
   is not what costs the time; reading them is. This is the control, and it is the one that rules
   out the alternative explanation.

### The test that does not need a bandwidth figure at all

Everything above multiplies a file size by a token rate. That product is arithmetic, not a
counter reading — it would come out the same if something else were the limit, so on its own it
does not answer a challenge. This does. **One pass over the weights, N tokens wide, and how the
cost grows with N:**

| tokens per step | 3b: ms | x cost of 1 | 8b: ms | x cost of 1 |
|---|---|---|---|---|
| 1 | 18.1 | 1.00x | 40.7 | 1.00x |
| 2 | 18.8 | **1.04x** | 42.5 | **1.04x** |
| 4 | 25.1 | 1.39x | 58.1 | 1.43x |
| 16 | 64.2 | 3.55x | 151.2 | 3.71x |
| 32 | 66.1 | 3.66x | 153.9 | 3.78x |
| 128 | 245.0 | 13.6x | 577.1 | 14.2x |
| 256 | 509.3 | 28.2x | 1235.0 | 30.3x |

**Doubling the arithmetic costs 4% more time.** That cannot happen if the arithmetic is the
bottleneck — two tokens is twice the multiply-adds, and it is free. What is not free is the pass
over the weights, and there is exactly one of those either way. Past about 32 the curve turns
linear (128 -> 256 doubles the cost), which is the arithmetic finally becoming the constraint.
A stopwatch, no bandwidth number, either measured or quoted.

**Why the number is large is not mysterious, and it is not waste.** Generating one token needs
every weight exactly once, so 4.68 GB moves per token by construction — there is no shorter
path. Each of those bytes carries about 3.5 floating-point operations at Q4. A machine like this
needs roughly two orders of magnitude more arithmetic per byte before compute becomes the
constraint, so the GPU spends most of a token waiting for weights to arrive. Which is also why
**"GPU 99% busy" is not evidence of compute-bound** — busy means work is scheduled, not that the
ALUs are doing anything. And it is why batching is the whole economics of hosted inference: read
the weights once, serve 32 users' tokens. It does nothing for one person typing one question.

**Two wrong harnesses before this one**, both worth knowing about because each looked fine:
a single-token `eval` against an EMPTY cache reported 4 ms for a step that takes 41, and timing
the FIRST step after `reset()` reported 191 ms, because llama.cpp re-plans its compute graph when
the batch shape changes. The check that catches both: the batch=1 row must equal the rate the
model actually generates at. It does — 18.1 ms and 40.7 ms against 55 and 24 tok/s.

**The correction: the ceiling was compared against the wrong chip.** This said "a base M-series
chip has roughly 120 GB/s, so an 8B is at ~95% of everything this machine has". 120 GB/s is
**M4's** figure and this is an **M5**, which Apple rates at 153 GB/s (their number, not measured
here). So the honest version is ~75% of rated peak, not 95%. A single-threaded streaming loop on
this machine measures 113 GB/s (numpy triad, 4.8 GB read+write) — itself a floor rather than the
ceiling, since it is one thread.

What survives unchanged is the conclusion, because it never depended on the last 25%: **no
setting improves this**, and the only lever is a smaller model. Even reaching 100% of rated peak
would buy a third more tokens per second, against 2.2x for dropping from 8B to 3B.

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
