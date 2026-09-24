"""Per-case assessment pipeline: generate, check, judge, revise.

    evidence pack
        -> generator (LLM, or the offline template)
        -> layer 1: rule checks (always)
        -> layer 2: LLM judge (when configured)
        -> if the verdict is "revise" or "fail": feed the issues back and regenerate (bounded)
        -> publish policy:
             pass    show the model narrative
             revise  show it with a visible quality flag and lowered confidence
             fail    never shown; the engine's template narrative is shown instead
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..engine.evidence import evidence_pack
from ..engine.narrative import NARRATIVE_VERSION, offline_narrative
from ..engine.signals import ENGINE_VERSION
from ..quality.checks import normalize_citations
from ..quality.checks import run_rule_checks
from ..quality.judge import combine, judge_assessment
from . import usage as usage_mod
from .prompts import GENERATOR_SYSTEM, PROMPT_VERSION, generator_messages
from .providers import LLMError, LLMProvider, extract_json

NARRATIVE_KEYS = ("summary", "key_indicators", "recommended_action", "next_steps", "uncertainties",
                  "innocent_explanations", "agrees_with_engine", "disagreement_reason")


def cache_key(case: dict, generator: str, judge: str) -> str:
    material = json.dumps({"pack": evidence_pack(case), "gen": generator, "judge": judge, "prompt": PROMPT_VERSION,
                           "engine": ENGINE_VERSION, "narrative": NARRATIVE_VERSION}, sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def normalise_narrative(raw: dict) -> dict:
    """Coerce a model's JSON into the narrative schema, dropping anything unexpected."""
    if not isinstance(raw, dict):
        raise LLMError("Narrative is not a JSON object.")

    def fix_cites(text: Any) -> str:
        return normalize_citations(re.sub(r"\((E\d+)\)", r"[\1]", str(text or ""))).strip()

    def as_list(v: Any) -> list[str]:
        if isinstance(v, str):
            return [fix_cites(v)] if v.strip() else []
        return [fix_cites(x) for x in (v or []) if str(x).strip()]

    indicators = []
    for i in raw.get("key_indicators") or []:
        if isinstance(i, dict):
            direction = str(i.get("direction", "risk")).lower()
            indicators.append({
                "evidence_id": str(i.get("evidence_id", "")).strip().strip("[]()"),
                "statement": fix_cites(i.get("statement", "")),
                "direction": direction if direction in ("risk", "mitigating") else "risk",
            })
    summary = fix_cites(raw.get("summary", ""))
    if not summary:
        raise LLMError("Narrative has no summary.")
    agrees = raw.get("agrees_with_engine", True)
    if isinstance(agrees, str):
        agrees = agrees.strip().lower() not in ("false", "no", "0")
    return {
        "summary": summary,
        "key_indicators": indicators,
        "recommended_action": fix_cites(raw.get("recommended_action", "")),
        "next_steps": as_list(raw.get("next_steps")),
        "uncertainties": as_list(raw.get("uncertainties")),
        "innocent_explanations": as_list(raw.get("innocent_explanations")),
        "agrees_with_engine": bool(agrees),
        "disagreement_reason": str(raw.get("disagreement_reason", "") or "").strip(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def assess_case(case: dict, generator: LLMProvider | None, judge: LLMProvider | None,
                max_revisions: int = 1) -> dict:
    pack = evidence_pack(case)
    gen_name = generator.config.describe() if generator else "offline"
    judge_name = judge.config.describe() if judge else "off"
    rounds: list[dict] = []

    if generator is None:
        narrative = offline_narrative(case)
        rules = run_rule_checks(narrative, case, pack)
        quality = combine(rules, judge_assessment(judge, pack, narrative))
        rounds.append({"round": 1, "verdict": quality["verdict"], "issues": len(quality["issues"]),
                       "judge_status": quality["judge_status"]})
        return _result(case, narrative, "template", gen_name, judge_name, quality, rounds, None)

    feedback: list[dict] | None = None
    previous: dict | None = None
    best: tuple[dict, dict] | None = None
    spent = {"writer": usage_mod.empty(), "judge": usage_mod.empty()}
    for attempt in range(1 + max_revisions):
        try:
            comp = generator.complete(GENERATOR_SYSTEM, generator_messages(pack, feedback, previous), json_output=True)
            spent["writer"] = usage_mod.add(spent["writer"], comp.usage)
            narrative = normalise_narrative(extract_json(comp.text))
        except Exception as exc:  # provider errors, invalid JSON, timeouts
            rounds.append({"round": attempt + 1, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            feedback = [{"criterion": "format", "severity": "high",
                         "problem": f"Your previous response could not be used ({str(exc)[:160]}).",
                         "fix": "Return one valid JSON object that matches the schema.", "evidence_ids": []}]
            if isinstance(exc, LLMError) and "HTTP 4" in str(exc):
                break  # a rejected request (credentials, unknown model, bad parameters) will not fix itself
            continue
        rules = run_rule_checks(narrative, case, pack)
        if rules["verdict"] == "fail" and judge is not None:
            # The judge cannot lift a failed rule check (the verdict is the worse of the two), so
            # the draft goes straight back for revision and the judge call is saved.
            report = {"layer": "llm_judge", "status": "skipped", "verdict": None, "scores": {}, "issues": [],
                      "reason": "Not called: the automated checks already failed this draft."}
        else:
            report = judge_assessment(judge, pack, narrative)
        spent["judge"] = usage_mod.add(spent["judge"], report.get("usage"))
        quality = combine(rules, report)
        rounds.append({
            "round": attempt + 1,
            "verdict": quality["verdict"],
            "issues": len(quality["issues"]),
            "top_issues": [i["problem"] for i in quality["issues"][:3]],
            "judge_status": quality["judge_status"],
            "generator_latency_ms": comp.latency_ms,
            "judge_latency_ms": quality["judge"].get("latency_ms"),
            "writer_tokens": comp.usage,
            "judge_tokens": report.get("usage"),
        })
        if best is None or _rank(quality) <= _rank(best[1]):
            best = (narrative, quality)
        if quality["verdict"] == "pass":
            break
        feedback, previous = quality["issues"], narrative

    if best is None:
        narrative = offline_narrative(case)
        quality = combine(run_rule_checks(narrative, case, pack), {"status": "skipped", "verdict": None, "scores": {}, "issues": []})
        errors = [r["error"] for r in rounds if r.get("error")]
        detail = f" Last error: {errors[-1]}" if errors else ""
        return _result(case, narrative, "template_unavailable", gen_name, judge_name, quality, rounds, None, spent,
                       fallback_reason="The language model was unavailable or did not return usable JSON, so the engine's template narrative is shown instead." + detail)

    narrative, quality = best
    if quality["verdict"] == "fail":
        template = offline_narrative(case)
        t_quality = combine(run_rule_checks(template, case, pack), {"status": "skipped", "verdict": None, "scores": {}, "issues": []})
        return _result(case, template, "template_fallback", gen_name, judge_name, t_quality, rounds,
                       {"narrative": narrative, "quality": quality}, spent,
                       fallback_reason="The model's narrative failed quality review, so the engine's template narrative is shown instead.")
    return _result(case, narrative, "llm", gen_name, judge_name, quality, rounds, None, spent)


def _rank(quality: dict) -> tuple[int, int]:
    order = {"pass": 0, "revise": 1, "fail": 2}
    return order[quality["verdict"]], len(quality["issues"])


def _result(case: dict, narrative: dict, source: str, gen_name: str, judge_name: str, quality: dict,
            rounds: list[dict], rejected: dict | None, spent: dict | None = None, fallback_reason: str = "") -> dict:
    flags = []
    confidence = dict(case["confidence"])
    confidence["reasons"] = list(confidence["reasons"])

    def lower(reason: str) -> None:
        confidence["level"] = "Low"
        confidence["reasons"].append({"text": reason, "positive": False})

    if source == "llm" and quality["verdict"] == "revise":
        flags.append({"level": "warn", "text": "The quality judge flagged issues that remained after revision. Read the quality report before relying on this narrative."})
        lower("The quality review flagged unresolved issues in the AI narrative.")
    if source in ("template_fallback", "template_unavailable"):
        flags.append({"level": "error", "text": fallback_reason})
    if source == "llm" and not narrative.get("agrees_with_engine", True):
        flags.append({"level": "warn", "text": f"The AI narrative disagrees with the engine's risk rating. {narrative.get('disagreement_reason') or 'no reason given'}."})
        lower("The AI narrative disagrees with the engine's risk rating.")
    return {
        "case_id": case["case_id"],
        "narrative": {k: narrative.get(k) for k in NARRATIVE_KEYS},
        "source": source,
        "generator": gen_name,
        "judge": judge_name,
        "same_model_judge": gen_name != "offline" and gen_name == judge_name,
        "quality": quality,
        "rounds": rounds,
        "revisions": max(0, len([r for r in rounds if "verdict" in r]) - 1),
        "rejected": rejected,
        "flags": flags,
        "confidence": confidence,
        "prompt_version": PROMPT_VERSION,
        "created_at": _now(),
        "usage": ({**usage_mod.add(spent["writer"], spent["judge"]), "writer": spent["writer"], "judge": spent["judge"]}
                  if spent else None),
    }
