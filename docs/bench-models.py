"""Speed test for candidate local models, loaded exactly as the app loads them.

THE EVIDENCE BEHIND THE TABLE in docs/design.md, "The model: local, and the machine picks it" —
kept in the repo so every number there can be re-run rather than trusted. First run 2026-09-23 on a
16 GB M5 under ordinary use, llama-cpp-python 0.3.35.

    .venv-build/bin/python docs/bench-models.py <path/to/model.gguf>

One model per process, so memory is fully released between models on a 16 GB machine. Prints one
JSON line. Timings come from llama.cpp's own perf counters, so prefill and decode are measured
separately rather than inferred from wall clock. Watch `sysctl vm.swapusage` around a run: a model
that fits the budget can still push the machine into swap, and that is a result, not noise.
"""
import ctypes, json, os, resource, statistics, sys, time

import llama_cpp

path = sys.argv[1]
name = os.path.basename(path)

# A realistic workload: a carried call's transcript, then "summarise and draft a reply".
# ~1,300 tokens of conversation, which is a long call for this product.
turns = [
    ("them", "Hi, I'm calling about the office renovation quote you sent last week, reference R-2291."),
    ("you", "Yes, I have it here. The quote was for the pantry and the two meeting rooms, right?"),
    ("them", "That's right. We'd like to go ahead, but the timeline is a problem. We need the pantry done before our client visit on the fourteenth."),
    ("you", "The quote assumed a start on the second, finishing around the twentieth. The pantry alone could be done first if we reorder the work."),
    ("them", "Could you do the pantry first and the meeting rooms after? The meeting rooms can wait until the end of the month."),
    ("you", "I think so. I would need to check with the carpenter, because the pantry cabinets are custom and take about eight working days to make."),
    ("them", "Eight working days from when? If we confirm today, is the fourteenth realistic?"),
    ("you", "If you confirm today and pay the deposit this week, the cabinets could arrive around the tenth, and installation takes two days. It is tight but possible."),
    ("them", "What is the deposit? The quote said thirty percent."),
    ("you", "Thirty percent of the pantry portion only, if we split it. That would be about four thousand two hundred dollars instead of the full thirty percent."),
    ("them", "That helps. Can you send a revised quote that splits the two phases, with the pantry deposit separately?"),
    ("you", "Yes. I'll also add the revised timeline so your finance team can see the dates."),
    ("them", "Please also confirm whether the electrical work in the pantry is included. Last time there was a surprise charge for extra power points."),
    ("you", "It includes four power points and moving the existing sink. Anything beyond that would be quoted separately before we do it."),
    ("them", "Good. And the noise — the building management only allows drilling before ten in the morning and after four."),
    ("you", "Noted. We'll schedule the noisy work in those windows. It may add a day to the meeting rooms, but not to the pantry."),
    ("them", "Fine. One more thing: who do I contact on site? Last project we never knew who was in charge."),
    ("you", "Our site supervisor will be Marcus. I'll put his number on the revised quote."),
    ("them", "Great. When can I expect the revised quote?"),
    ("you", "By tomorrow afternoon. If you can confirm by Thursday, we can hold the carpenter's slot."),
]
transcript = "\n".join(f"{who}: {what}" for who, what in turns * 3)
messages = [
    {"role": "system", "content": "You help a small business owner. Be brief and accurate. "
                                  "Only state what the transcript supports."},
    {"role": "user", "content": "Here is a call transcript.\n\n" + transcript +
                                "\n\nSummarise the call in three bullet points, then draft a "
                                "short reply the owner could send to the caller."},
]
if "qwen" in name.lower():
    # Qwen's own switch, in the ONE system message. Qwen3.5's template refuses a second system
    # message ("System message must be at the beginning"); Qwen3's tolerated it. Rates are
    # unaffected by thinking, but a thinking model spends far more tokens before the answer.
    messages[0]["content"] = "/no_think\n" + messages[0]["content"]

VIETNAMESE = ("Chào anh, em gọi để hỏi về đơn hàng số 4821 đặt hôm thứ Hai. Em chưa nhận được "
              "hàng và cũng không thấy thông báo giao hàng. Anh kiểm tra giúp em xem đơn đang ở "
              "đâu, và nếu bị trễ thì khi nào em có thể nhận được. Em cần hàng trước thứ Sáu vì "
              "có buổi họp với khách. Cảm ơn anh nhiều.")
ENGLISH = ("Hi, I'm calling to ask about order number 4821 placed on Monday. I haven't received "
           "it and haven't seen any delivery notice. Could you check where the order is, and if "
           "it's delayed, when I can expect it? I need it before Friday because I have a meeting "
           "with a client. Thank you very much.")

t0 = time.perf_counter()
llm = llama_cpp.Llama(model_path=path, n_ctx=8192, n_gpu_layers=-1, verbose=False)
load_s = time.perf_counter() - t0


class Perf(ctypes.Structure):
    _fields_ = [("t_start_ms", ctypes.c_double), ("t_load_ms", ctypes.c_double),
                ("t_p_eval_ms", ctypes.c_double), ("t_eval_ms", ctypes.c_double),
                ("n_p_eval", ctypes.c_int32), ("n_eval", ctypes.c_int32)]


def perf():
    fn = llama_cpp.llama_cpp._lib.llama_perf_context
    fn.restype = Perf
    fn.argtypes = [ctypes.c_void_p]
    return fn(llm._ctx.ctx)


def reset_perf():
    fn = llama_cpp.llama_cpp._lib.llama_perf_context_reset
    fn.argtypes = [ctypes.c_void_p]
    fn(llm._ctx.ctx)


llm.create_chat_completion(messages=[{"role": "user", "content": "Say ok."}], max_tokens=4)  # warm

runs, text = [], ""
for _ in range(3):
    llm.reset()                         # no KV reuse: every run pays the full prefill
    reset_perf()
    out = llm.create_chat_completion(messages=messages, max_tokens=192, temperature=0)
    p = perf()
    text = out["choices"][0]["message"]["content"] or ""
    runs.append({"prefill_tps": p.n_p_eval / (p.t_p_eval_ms / 1000),
                 "decode_tps": p.n_eval / (p.t_eval_ms / 1000),
                 "n_prompt": p.n_p_eval, "n_gen": p.n_eval,
                 "prefill_s": p.t_p_eval_ms / 1000, "decode_s": p.t_eval_ms / 1000})

med = lambda k: statistics.median(r[k] for r in runs)
print(json.dumps({
    "model": name,
    "file_gb": round(os.path.getsize(path) / 1024**3, 2),
    "load_s": round(load_s, 1),
    "prefill_tps": round(med("prefill_tps"), 1),
    "decode_tps": round(med("decode_tps"), 1),
    "n_prompt": runs[-1]["n_prompt"],
    "n_gen": runs[-1]["n_gen"],
    "prefill_s": round(med("prefill_s"), 2),
    "reply_150_s": round(150 / med("decode_tps"), 1),
    "peak_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3, 2),
    "tokens_vi": len(llm.tokenize(VIETNAMESE.encode(), add_bos=False)),
    "tokens_en": len(llm.tokenize(ENGLISH.encode(), add_bos=False)),
    "thinks": text.lstrip().startswith("<think>"),
    "sample": text[:240],
}))
