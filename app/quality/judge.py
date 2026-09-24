"""Layer 2 of quality review: the LLM-as-judge.

The judge is a separately configured model (JUDGE_PROVIDER / JUDGE_MODEL) that audits what the
generator wrote against the same evidence pack. Its output is validated like any other model
output: scores are clamped, evidence IDs are checked, and the verdict is recomputed from the
rubric so a lenient or inconsistent judge cannot wave a bad narrative through.
"""

from __future__ import annotations

from typing import Any

from ..llm.prompts import CHAT_JUDGE_SYSTEM, JUDGE_SYSTEM, chat_judge_user, judge_user
from ..llm.providers import LLMError, LLMProvider, extract_json
from ..llm import usage as usage_mod
from .checks import CHAT_CRITERIA, CRITERIA, SEVERITY_RANK, VERDICT_RANK, worst_verdict


def _clamp(v: Any) -> int:
    try:
        return max(1, min(5, int(round(float(v)))))
    except (TypeError, ValueError):
        return 3


def _normalise(raw: dict, criteria: list[str], valid_ids: set[str]) -> dict:
    scores_in = raw.get("scores") or {}
    if not isinstance(scores_in, dict) or not any(c in scores_in for c in criteria):
        raise LLMError("The judge response has no scores.")
    scores = {c: _clamp(scores_in.get(c, 3)) for c in criteria}
    issues = []
    for i in raw.get("issues") or []:
        if not isinstance(i, dict):
            continue
        sev = str(i.get("severity", "medium")).lower()
        sev = sev if sev in SEVERITY_RANK else "medium"
        crit = str(i.get("criterion", "groundedness")).lower()
        ids = [e for e in (i.get("evidence_ids") or []) if isinstance(e, str) and e in valid_ids]
        issues.append({
            "criterion": crit if crit in criteria else criteria[0],
            "severity": sev,
            "problem": str(i.get("problem", "")).strip()[:500],
            "fix": str(i.get("fix", "")).strip()[:400],
            "quote": str(i.get("quote", "")).strip()[:300],
            "evidence_ids": ids,
            "source": "llm_judge",
        })
    # A low score with no stated issue still needs actionable feedback for the revision round.
    for c, sc in scores.items():
        if sc <= 3 and not any(i["criterion"] == c for i in issues):
            issues.append({"criterion": c, "severity": "medium",
                           "problem": f"The judge scored {c} {sc}/5 without naming a specific problem.",
                           "fix": f"Re-check every claim for {c} against the evidence pack and cite evidence IDs.",
                           "quote": "", "evidence_ids": [], "source": "llm_judge"})
    claimed = str(raw.get("verdict", "")).lower()
    # Recompute the verdict from the rubric; never trust the judge's label alone.
    if any(i["severity"] == "high" for i in issues) or scores.get("groundedness", 5) <= 2 or scores.get("neutrality", 5) <= 2:
        verdict = "fail"
    elif any(s <= 3 for s in scores.values()) or any(i["severity"] == "medium" for i in issues):
        verdict = "revise"
    else:
        verdict = "pass"
    adjusted = claimed in VERDICT_RANK and claimed != verdict
    if claimed in VERDICT_RANK and VERDICT_RANK[claimed] > VERDICT_RANK[verdict]:
        verdict = claimed  # a stricter judge label is kept
        adjusted = False
    missed = [e for e in (raw.get("missed_evidence_ids") or []) if isinstance(e, str) and e in valid_ids]
    return {
        "layer": "llm_judge",
        "status": "ok",
        "scores": scores,
        "verdict": verdict,
        "claimed_verdict": claimed or None,
        "verdict_adjusted": adjusted,
        "issues": issues,
        "missed_evidence_ids": missed,
        "summary": str(raw.get("summary", "")).strip()[:600],
    }


def _call_judge(provider: LLMProvider, system: str, user: str, criteria: list[str], valid_ids: set[str]) -> dict:
    last_error = ""
    spent = usage_mod.empty()  # a retried call is billed too, so every attempt is counted
    for attempt in range(2):
        try:
            comp = provider.complete(system, [{"role": "user", "content": user}], json_output=True, temperature=0.0)
            spent = usage_mod.add(spent, comp.usage)
            report = _normalise(extract_json(comp.text), criteria, valid_ids)
            report.update({"model": comp.model, "latency_ms": comp.latency_ms, "attempts": attempt + 1, "usage": spent})
            return report
        except (LLMError, ValueError, TypeError) as exc:
            last_error = str(exc)
        except Exception as exc:  # network errors, timeouts
            last_error = f"{type(exc).__name__}: {exc}"
            break
    return {"layer": "llm_judge", "status": "error", "error": last_error[:400], "verdict": None, "scores": {},
            "issues": [], "usage": spent}


def judge_assessment(provider: LLMProvider | None, pack: dict, narrative: dict) -> dict:
    if provider is None:
        return {"layer": "llm_judge", "status": "off", "verdict": None, "scores": {}, "issues": []}
    valid = {e["id"] for e in pack["evidence"]}
    return _call_judge(provider, JUDGE_SYSTEM, judge_user(pack, narrative), CRITERIA, valid)


def judge_chat(provider: LLMProvider | None, pack: dict, question: str, answer: str) -> dict:
    if provider is None:
        return {"layer": "llm_judge", "status": "off", "verdict": None, "scores": {}, "issues": []}
    valid = {e["id"] for e in pack["evidence"]}
    return _call_judge(provider, CHAT_JUDGE_SYSTEM, chat_judge_user(pack, question, answer), CHAT_CRITERIA, valid)


def combine(rules: dict, judge: dict, criteria: list[str] | None = None) -> dict:
    """Merge the rule layer and the judge layer into one quality report."""
    criteria = criteria or CRITERIA
    judge_ok = judge.get("status") == "ok"
    scores = {}
    for c in criteria:
        vals = [rules["scores"].get(c)]
        if judge_ok:
            vals.append(judge["scores"].get(c))
        vals = [v for v in vals if v is not None]
        scores[c] = min(vals) if vals else None
    issues = sorted(rules["issues"] + (judge["issues"] if judge_ok else []),
                    key=lambda i: -SEVERITY_RANK[i["severity"]])
    verdict = worst_verdict(rules["verdict"], judge.get("verdict") if judge_ok else None)
    return {
        "verdict": verdict,
        "scores": scores,
        "issues": issues,
        "rules": rules,
        "judge": judge,
        "judge_status": judge.get("status"),
        "summary": judge.get("summary") if judge_ok else _rules_summary(rules),
    }


def _rules_summary(rules: dict) -> str:
    n = len(rules["issues"])
    if rules["verdict"] == "pass":
        return "All rule-based checks passed: citations resolve, figures match the case data, and the wording fits the risk rating."
    return f"Rule-based checks found {n} issue{'s' if n != 1 else ''}."
