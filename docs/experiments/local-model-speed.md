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

1. **A reasoning model needs thinking suppressed for this app**, or it is unusable: 7.2x, and the
   monologue is stored and shown to the owner because nothing strips `<think>`.
2. **A non-reasoning model is the better default on 16 GB.** Llama 3.2 3B was the fastest here
   AND produced the most visible answer, at 40% of the resident memory.
3. `agentduet-desktop models` marks 8B "fits" on this machine, which is true of memory and
   misleading about experience. Worth saying something about speed there.

## Method

`docs/experiments/` holds the scripts. Each configuration loads the model fresh, runs the three
prompts, and averages; `resource.getrusage` gives CPU time around the generation call only, and
GPU utilisation is sampled from `ioreg -c IOAccelerator` while a sentinel file confirms
generation has begun. Model load time is excluded — it is ~0.5s and paid once per process.
