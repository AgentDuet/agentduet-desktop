"""How much the assistant's prompt may hold, from how fast THIS machine reads.

THE RULE. A prompt read cold — after a restart, or a background job that replaced the engine's
memory without a saved state — should take at most COLD_SECONDS. So the total is that times the
measured reading speed (`speed.py`): about 3,900 tokens on the M5, half on a Mac with half the
GPU. Most questions only read their new tail (see `gate`), so the total mostly bounds the cold
case, how much writing slows at long lengths, and how much a small model has to keep track of.

THE SPLIT, of what is left after the fixed instructions and a reserve for the inbox, the question
and the answer:
- the assistant memory (`recall`), capped at MEMORY_SHARE;
- the person brief (`brief`), capped at PERSON_SHARE;
- recent chat, word for word, gets the rest.

The summaries' caps are the sizes their prompts ask for, so a slow machine gets shorter
summaries rather than a slow prompt. Until the machine has been measured, DEFAULT_READ_TPS
stands in — deliberately below the M5's, so an unmeasured Mac errs towards fast.
"""
from __future__ import annotations

COLD_SECONDS = 10
DEFAULT_READ_TPS = 250
RESERVE_TOKENS = 300
#: Never squeeze the three parts below this, however slow the machine: below it the assistant
#: cannot hold a conversation at all, and a slow answer is better than a lost thread.
MIN_REST_TOKENS = 900
#: The engine's window, minus room for the answer. Nothing may be budgeted past it.
MAX_TOTAL_TOKENS = 8192 - 2048
MEMORY_SHARE, PERSON_SHARE = 0.15, 0.25
#: Gemma's tokenizer on our prompts: 1,159 words were 1,677 tokens.
TOKENS_PER_WORD = 1.45


def _read_tps() -> int:
    from . import llm, speed
    try:
        rec = speed.of(llm.current_model())
    except Exception:
        rec = {}
    return int(rec.get("read_tps") or DEFAULT_READ_TPS)


def split(instruction_words: int = 1200) -> dict:
    """{"total", "memory_words", "person_words", "chat_words", "read_tps"} for this machine."""
    tps = _read_tps()
    total = min(MAX_TOTAL_TOKENS, tps * COLD_SECONDS)
    rest = max(MIN_REST_TOKENS,
               total - int(instruction_words * TOKENS_PER_WORD) - RESERVE_TOKENS)
    words = lambda tokens: max(60, int(tokens / TOKENS_PER_WORD))
    memory, person = int(rest * MEMORY_SHARE), int(rest * PERSON_SHARE)
    return {"total": total, "read_tps": tps,
            "memory_words": words(memory), "person_words": words(person),
            "chat_words": words(rest - memory - person),
            # WITH NOBODY ON SCREEN the person's share goes to the chat.
            "chat_words_alone": words(rest - memory)}
