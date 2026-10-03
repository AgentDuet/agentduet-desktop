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
| Transcript search index (PLANNED, not built) | ~15 pieces × ~1.5 KB ≈ 25 KB | Fine to ~1 million pieces (~60,000 calls, ~1.5 GB); past that, a nearest-neighbour index rather than comparing against every piece. It is derived, so it can always be rebuilt or trimmed. |

### Fixed costs per machine

| What | Size | Notes |
|---|---|---|
| Gemma 4 E4B (the assistant, summaries) | 4.8 GB on disk, ~5.6 GB resident | one at a time, `gate.py` |
| Qwen3-ASR 1.7B (speech) | 2.4 GB on disk, ~2.5 GB resident | |
| Decision model | 1.7 GB on disk, ~0.5 GB while it runs | in a child process, freed after |
| Transcription speed | **measured** 9.3 s for a 222 s call (M5) | ~24x realtime |
| Summary per call | **measured** ~8.7 s (Gemma 4 E4B, M5) | |

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
