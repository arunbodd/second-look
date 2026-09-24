"""Record one AI review run into data/recorded_review.json.

    python -m app.record                          # picks a pair from the keys in .env (below)
    python -m app.record --writer pplx-gpt-6-luna --judge pplx-claude-haiku-4-5

Default pair: with PERPLEXITY_API_KEY, the cheapest pair from two makers, both through Perplexity:
GPT-6 Luna writes ($0.10 / $0.50 per million tokens in / out) and Gemini 3.1 Flash-Lite judges
($0.25 / $1.50). Otherwise GPT-6 Luna writes and GPT-6 Sol judges with OPENAI_API_KEY.

Every case gets an assessment (writer draft, rule checks, judge review, one revision if the
judge sends it back). The suspicious cases also get answers to the ten starter questions in
the Ask panel, each checked and judged. Output already in the assessment cache is reused, so
a rerun only calls the models for cases whose evidence changed.
"""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .engine.narrative import SUGGESTIONS
from .llm import catalog
from .llm import usage as usage_mod
from .llm.chat import answer_question, judge_answer
from .recorded import DEFAULT_PATH
from .service import TriageService
from .settings import load_settings


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--writer", default=None, help="catalogue id (default depends on the keys present)")
    ap.add_argument("--judge", default=None)
    ap.add_argument("--questions-for", default="suspicious", help="risk rating whose cases get starter-question answers (suspicious, review, likely_fp, all, none)")
    ap.add_argument("--out", default=str(DEFAULT_PATH))
    ap.add_argument("--max-cost", type=float, default=2.0,
                    help="stop before spending more than this many US dollars on the recording (default 2.00)")
    args = ap.parse_args()

    settings = load_settings()
    keys = catalog.key_status()
    pplx = keys.get("perplexity")
    args.writer = args.writer or ("pplx-gpt-6-luna" if pplx else "gpt-6-luna")
    args.judge = args.judge or ("pplx-gemini-3-1-flash-lite" if pplx else "gpt-6-sol")
    settings.recorded_path = None  # never replay while recording
    svc = TriageService(settings)
    svc.set_models(args.writer, args.judge)
    w, j = catalog.find(args.writer), catalog.find(args.judge)
    order = svc.queue.order
    print(f"Recording {len(order)} cases with {w['label']} writing and {j['label']} judging", flush=True)

    lock = threading.Lock()
    spent = {"usage": usage_mod.empty()}

    def over_budget() -> bool:
        return spent["usage"]["cost_usd"] >= args.max_cost

    def one(cid):
        if over_budget():
            return None
        a = svc.ensure_assessment(cid, "ai")
        u = a.get("usage")
        with lock:
            spent["usage"] = usage_mod.add(spent["usage"], u)
        print(f"  {cid} {a['source']} {a['quality']['verdict']} · {(u or {}).get('input', 0)} in"
              f" / {(u or {}).get('output', 0)} out · ${(u or {}).get('cost_usd', 0):.4f}", flush=True)
        return a

    with ThreadPoolExecutor(max_workers=settings.max_concurrency) as ex:
        results = dict(zip(order, ex.map(one, order)))
    if any(a is None for a in results.values()):
        raise SystemExit(f"Stopped at the ${args.max_cost:.2f} cap (spent ${spent['usage']['cost_usd']:.2f}). Finished cases are cached; "
                         "raise --max-cost and rerun to continue.")
    failed = [cid for cid, a in results.items() if a["source"] == "template_unavailable"]
    if failed:
        raise SystemExit(f"Model calls failed for {', '.join(failed)}; rerun to retry (finished cases are cached).")

    cases = {cid: {"fingerprint": svc._key(svc.queue.cases[cid], "ai"), "assessment": results[cid]} for cid in order}

    lanes = {"all": None, "none": set()}.get(args.questions_for, {args.questions_for})
    q_cases = [cid for cid in order if lanes is None or svc.queue.cases[cid]["lane"] in lanes]

    def ask(item):
        cid, q = item
        if over_budget():
            return cid, None
        case = svc.queue.cases[cid]
        res = answer_question(case, results[cid], q, [], [], svc.generator)
        report = judge_answer(case, q, res["answer"], res["checks"], svc.judge)
        with lock:
            spent["usage"] = usage_mod.add(spent["usage"], usage_mod.add(res.get("usage"), report.get("usage")))
        return cid, {"question": q, **res, "judge": report}

    # Answers are saved as they finish, so an interrupted recording resumes where it stopped.
    progress_path = Path(args.out).with_suffix(".partial.json")
    fp = {cid: cases[cid]["fingerprint"] for cid in q_cases}
    try:
        saved = json.loads(progress_path.read_text())
    except (OSError, ValueError):
        saved = {}
    questions: dict[str, list] = {cid: [e for e in saved.get(cid, []) if e.get("fingerprint") == fp[cid]] for cid in q_cases}
    done = {(cid, e["question"]) for cid in q_cases for e in questions[cid]}
    items = [(cid, q) for cid in q_cases for q in SUGGESTIONS if (cid, q) not in done]
    print(f"Answering starter questions for {len(q_cases)} cases: {len(done)} saved, {len(items)} to go (each judged)", flush=True)
    for qs in questions.values():  # answers saved by an earlier, interrupted run count toward the cap
        for e in qs:
            spent["usage"] = usage_mod.add(spent["usage"], usage_mod.add(e.get("usage"), (e.get("judge") or {}).get("usage")))
    with ThreadPoolExecutor(max_workers=settings.max_concurrency) as ex:
        for cid, entry in ex.map(ask, items):
            if entry is None:
                continue
            with lock:
                questions[cid].append({**entry, "fingerprint": fp[cid]})
                progress_path.write_text(json.dumps(questions, default=str))
    if over_budget() and sum(len(v) for v in questions.values()) < len(q_cases) * len(SUGGESTIONS):
        raise SystemExit(f"Stopped at the ${args.max_cost:.2f} cap (spent ${spent['usage']['cost_usd']:.2f}). Answers so far are "
                         "saved; raise --max-cost and rerun to continue.")
    for cid in questions:  # keep the starter order
        questions[cid].sort(key=lambda e: SUGGESTIONS.index(e["question"]))

    out = {
        "note": "One recorded AI review run, shown when no API key is configured. Each entry is valid only "
                "while its evidence-pack fingerprint matches the live engine.",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "writer": {"id": w["id"], "label": w["label"], "vendor": w["vendor"], "describe": settings.generator.describe()},
        "judge": {"id": j["id"], "label": j["label"], "vendor": j["vendor"], "describe": settings.judge.describe()},
        "dataset": svc.dataset["name"],
        "engine_version": svc.queue.engine_version,
        "cases": cases,
        "questions": questions,
        "usage": spent["usage"],
    }
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    verdicts = {}
    for a in results.values():
        verdicts[a["quality"]["verdict"]] = verdicts.get(a["quality"]["verdict"], 0) + 1
    chat_v = {}
    for lst in questions.values():
        for e in lst:
            chat_v[e["judge"]["verdict"]] = chat_v.get(e["judge"]["verdict"], 0) + 1
    progress_path.unlink(missing_ok=True)
    u = spent["usage"]
    print(f"Wrote {args.out}: {len(cases)} assessments {verdicts}; {sum(len(v) for v in questions.values())} answers {chat_v}")
    print(f"Tokens: {u['input']:,} in ({u['cached']:,} cached) · {u['output']:,} out ({u['reasoning']:,} reasoning) · "
          f"{u['calls']} calls · ${u['cost_usd']:.4f} ({u['cost_source']})")
    svc.shutdown()


if __name__ == "__main__":
    main()
