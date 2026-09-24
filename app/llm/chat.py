"""Follow-up questions about one case, grounded in the evidence pack."""

from __future__ import annotations

import re

from ..engine.evidence import evidence_pack
from ..engine.narrative import answer_offline
from ..quality.checks import CHAT_CRITERIA, run_chat_checks
from ..quality.judge import combine, judge_chat
from ..quality.checks import normalize_citations
from .prompts import CHAT_SYSTEM, chat_context
from .providers import LLMProvider
from . import usage as usage_mod

_ID_RE = re.compile(r"\[(E\d+)\]")


def _citations(text: str) -> list[str]:
    seen: list[str] = []
    for m in _ID_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


def answer_question(case: dict, assessment: dict, question: str, history: list[dict], notes: list[str],
                    generator: LLMProvider | None) -> dict:
    pack = evidence_pack(case)
    if generator is None:
        res = answer_offline(case, question)
        checks = run_chat_checks(res["answer"], case, pack)
        return {"answer": res["answer"], "citations": res["citations"], "source": "offline", "model": "offline",
                "checks": checks, "warnings": _warnings(checks)}

    context = chat_context(pack, assessment["narrative"], notes)
    messages = [
        {"role": "user", "content": context},
        {"role": "assistant", "content": "Understood. I will answer only from this evidence and cite evidence IDs."},
    ]
    for turn in history[-8:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    answer, checks, model = "", None, generator.config.describe()
    spent = usage_mod.empty()
    for attempt in range(2):
        try:
            comp = generator.complete(CHAT_SYSTEM, messages)
        except Exception as exc:
            fallback = answer_offline(case, question)
            checks = run_chat_checks(fallback["answer"], case, pack)
            return {"answer": fallback["answer"], "citations": fallback["citations"], "source": "offline_fallback",
                    "model": model, "checks": checks, "usage": spent,
                    "warnings": [f"The language model was unavailable ({type(exc).__name__}); this answer comes from the offline responder."]}
        spent = usage_mod.add(spent, comp.usage)
        answer = normalize_citations(comp.text).strip()
        model = comp.model
        checks = run_chat_checks(answer, case, pack)
        if checks["verdict"] != "fail" or attempt == 1:
            break
        problems = "; ".join(i["problem"] for i in checks["issues"])
        messages += [{"role": "assistant", "content": answer},
                     {"role": "user", "content": f"Your answer failed verification: {problems} Rewrite it using only figures and evidence IDs from the evidence pack."}]
    return {"answer": answer, "citations": _citations(answer), "source": "llm", "model": model,
            "checks": checks, "warnings": _warnings(checks), "usage": spent}


def _warnings(checks: dict) -> list[str]:
    return [i["problem"] for i in checks["issues"] if i["severity"] in ("high", "medium")]


def judge_answer(case: dict, question: str, answer: str, checks: dict, judge: LLMProvider | None) -> dict:
    pack = evidence_pack(case)
    report = judge_chat(judge, pack, question, answer)
    return combine(checks, report, CHAT_CRITERIA)
