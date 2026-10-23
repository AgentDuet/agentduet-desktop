# Limits — what grows, and where it stops (2026-10-03)

Written because a shared system with ~500 users is being discussed (not firmed up), and the
product was built for one owner on one Mac. **Measured** numbers say how; **estimate** numbers
say what they are built from. Revisit when either changes.

## One owner, one Mac

### Fixed 2026-10-03: the newest-200 cap

The hub, the summaries and the assistant used to read only the newest 200 calls (about ten days
for a busy owner), and anyone quieter silently dropped off the list. The call log now has a
SQLite index (`run/calls.db`, rebuilt from `calls.jsonl` whenever it is missing), and every
reader asks it for what it needs: one person's calls, everyone by their newest call, the calls
since a date. Only `suggest` still looks at the newest 200, on purpose: a suggestion is about
what is coming.

### Grows with every call

| What | Per call | Where it starts to matter |
|---|---|---|
| Recording (24 kHz, 16-bit, stereo = 96 KB/s) | ~17 MB per 3 minutes | **Disk, first.** 10,000 calls ≈ 170 GB. Needs an owner-chosen retention setting, never a silent one. |
| Transcript `.txt` | a few KB | never |
| `run/calls.jsonl` + its index `run/calls.db` | ~250 bytes + index | **Measured** on an M5. The hub's list, every 5 s, reads one row per PERSON: 0.5 ms at 10,000 calls · 2 ms at 100,000 · 6 ms at 365,000 (9,125 people). One person's calls: 0.5 ms at any size. Building the index from an existing log, once: 0.1 s · 1.7 s · 8 s. (The whole-file read it replaced: 27 ms · 167 ms · 0.7 s, every poll.) The list's payload grows with people: ~1 MB at 9,000 people. |
| `run/queries.jsonl` (messages), read whole by `tools.rows()` | one line per message | the same shape as above |
| Person summaries (`run/briefs/`) | one file per person | never |
| Transcript search index (`run/search.db`, built 2026-10-03) | ~15 pieces × (512-byte vector + text) | **Measured** on 14 real calls (36 pieces): indexed in 0.7 s; a search 5–6 ms; a pass with nothing new 3 ms. Every search compares against every piece, so it grows with pieces: fine to ~1 million (~60,000 calls); past that, a nearest-neighbour index. Derived from the transcripts, so it can always be rebuilt or trimmed. |

### Fixed costs per machine

| What | Size | Notes |
|---|---|---|
| Gemma 4 E4B (the assistant, summaries) | 4.8 GB on disk, ~5.6 GB resident | one at a time, `gate.py` |
| Qwen3-ASR 1.7B (speech) | 2.4 GB on disk, ~2.5 GB resident | |
| Search model (EmbeddingGemma 300M) | 313 MB on disk, ~0.6 GB while loaded | in the budget; unloaded after 5 idle minutes |
| Decision model | 1.7 GB on disk, ~0.5 GB while it runs | in a child process; in the budget, ended when idle 2 min or room is needed |
| **Peak** | what the budget allows: two thirds of RAM | `slot.py` (2026-10-05): a MEMORY BUDGET. A model loads beside the others if it fits; if not, others are unloaded — never speech during a call or a more important model used in the last minute, a partner (Gemma and the decision model) last, the less important first (search, decision, Gemma, speech), then least recently used. 16 GB holds all four (~9.4 of 10.6 GB). 8 GB cannot hold Gemma 4 E2B and speech together, so they take turns; the decision model still sits beside Gemma. A model that runs out of memory anyway shrinks the budget. |
| Transcription speed | **measured** 9.3 s for a 222 s call (M5) | ~24x realtime |
| Summary per call | **measured** ~8.7 s (Gemma 4 E4B, M5) | |

## One company, one Mac: calls at once (AgentDuet Team, 2026-10-23)

AgentDuet Team records a whole company over one trunk connector, so calls arrive **at the same
time**, which every earlier call through this app never did. The design target is **10 at once**.

**Measured** on an M5 by `tests/test_concurrency.py`: N calls of 30 s through the real
`carry.handle` at once, on fake SDK objects streaming 24 kHz audio in real time, then the queue
saving them. Three runs, with other work on the machine, so a range:

| Calls at once | Recording CPU (share of one core) | Peak memory | Queue: all N saved as MP3 | Recordings correct |
|---|---|---|---|---|
| 1 | 0.8–1.2% | 155 MB | 0.2 s | 1/1 |
| 10 | 4–9% | ~210 MB | 1.7–2.0 s | 10/10 |
| 25 | 11–12% | 270–290 MB | 4.3–6.3 s | 25/25 |
| 50 | 17–23% | 410–430 MB | 8.6–10.7 s | 50/50 |

"Correct": the right name and length, and each side holding its own call's audio, so nothing
crossed between calls.

**Per call:**
- **Real-time recording:** ~0.35–0.45% of one core, about **0.9%** with the SDK receiving the
  audio, which is not in the test (binary frames, about as cheap as our side; doubled to be safe).
- **Memory:** ~5.5 MB on top of ~155 MB.
- **Network and disk:** 96 KB/s.
- **The queue** saves one call at a time after it ends, so it is not real time: MP3 encoding costs
  ~0.7% of a call's length, and one core keeps up with ~150 minutes of calls ending per minute.

**Where it stops:** recording runs on one Python event loop, so ONE core is the ceiling: **~100
calls at once** before it nears 90% (estimate, from the per-call figure). An older Mac mini at half
the speed: ~50. Memory at 100 calls is ~700 MB, and network ~10 MB/s; neither comes first.

**The platform:** wss-edge caps a connector at `maxActiveSessions`, default **100**
(`ChannelConfig.DEFAULT_MAX_ACTIVE_SESSIONS`), settable per connector. One carried call takes
one session.

**Not covered:** transcription (off by default in the team app; with it on, speech is one queue
on the GPU at ~24x realtime, the row above), and the real network. Re-run the test after any
change to `carry.py` or the merge.

## A shared system with ~500 users — what would break

This is a **different architecture**, not more of the same app. AgentDuet Desktop runs one
daemon per owner, each with its own models, its own phone line and a site on loopback. Hosting
was moved to a separate team on a separate project (CLAUDE.md, 2026-09-23); this section is the
input that team would need from us.

Estimates assume 20 calls of 3 minutes per user per working day: **10,000 calls and 500 hours
of audio a day.**

| What | Per-owner design | At 500 users |
|---|---|---|
| **Models in memory** | each daemon loads its own (~8 GB) | 500 × 8 GB is impossible. Needs shared model servers: one resident Gemma and one speech model serving many users, with a queue. |
| **Transcription** | ~0.042 s of GPU per second of audio | 1.8 M s of audio × 0.042 ≈ **21 GPU-hours a day** on M5-class hardware, peaking several times the average in business hours. Several GPUs, batched. |
| **Summaries** | ~8.7 s per call | 10,000 × 8.7 s ≈ **24 GPU-hours a day**. Together with transcription, ~45 a day before the assistant. |
| **Storage** | ~17 MB per call | ~170 GB a day, **~60 TB a year**. Retention becomes a policy, not a setting. |
| **Ports and processes** | 8899 fixed for the editions with a line | 500 daemons on one host cannot share a fixed port. AgentDuet AI already takes any free port; the line editions need sign-in confirmed to accept any loopback port. |
| **Access control** | loopback + a per-machine token in a file only the owner can read | On a shared host every user can reach every loopback port; the token file's permissions are the only wall. A shared service needs per-user authentication, not a machine token. |
| **Phone lines** | one connector per install | 500 connectors, or a multi-tenant one; provisioning is already a release blocker for one (CLAUDE.md). |
| **Data separation** | one instance folder per owner | must hold per user in every store: recordings, transcripts, summaries, the assistant's memory, any search index. |
| **The call log** | one file, indexed in SQLite per instance | per user it is the table above; one shared store would be 3.6 M rows a year → a server database. |

**What carries over** if that system is built: the editions' split (recorder vs AI), the `.txt`
contract between them, the daemon's `/api/*` as the boundary to any UI, and the measurements
above as the sizing inputs.
