"""Layer 1 of quality review: deterministic rule checks.

These run on every AI output (model-written or template), with or without a network. They
catch the failure modes that can be verified mechanically: citations to evidence that does not
exist, figures that do not appear in the case data, a missed top driver, language that
contradicts the triage lane, missing next steps, and accusatory wording.

The LLM judge (judge.py) is layer 2 and covers what rules cannot: whether a claim is actually
supported by the evidence it cites, whether the reasoning is sound, and whether the tone fits.
"""

from __future__ import annotations

import json
import re

CRITERIA = ["groundedness", "completeness", "calibration", "actionability", "neutrality"]
CHAT_CRITERIA = ["groundedness", "relevance", "calibration", "neutrality"]
PENALTY = {"high": 3, "medium": 2, "low": 1}
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
VERDICT_RANK = {"pass": 0, "revise": 1, "fail": 2}

STRONG_RISK = [
    "genuinely suspicious", "highly suspicious", "strong evidence", "full investigation",
    "open an investigation", "fraud scheme", "clear fraud", "very suspicious",
]
BENIGN_LANGUAGE = [
    "false positive", "benign", "close the case", "close as", "no concern", "nothing suspicious",
    "low risk", "low-risk", "safe to close",
]
OVERCONFIDENT = [
    r"\bdefinitely\b", r"\bcertainly\b", r"\bundoubtedly\b", r"\bwithout (?:a )?doubt\b", r"\bproves?\b",
    r"\bproven\b", r"\bconfirmed fraud\b", r"\bclearly fraudulent\b", r"\bis fraud\b", r"\bis fraudulent\b",
    r"\bguaranteed\b", r"\bbeyond doubt\b",
]
ACCUSATORY = [
    r"\bfraudsters?\b", r"\bcriminals?\b", r"\bscam(?:mer|mers|s)?\b", r"\bstole\b", r"\bstealing\b",
    r"\bguilty\b", r"\bliars?\b", r"\blying\b", r"\bcommitted fraud\b", r"\bcommitting fraud\b",
    r"\bcrooks?\b", r"\bcon artists?\b", r"\bthie(?:f|ves)\b", r"\bcheat(?:ing|ed|s)?\b",
]
PROTECTED = [r"\brace\b", r"\bethnic", r"\breligio", r"\bimmigra", r"\bnational origin\b", r"\bgender\b"]
GEO_STEREOTYPE = [r"known for (?:insurance )?fraud", r"high[- ]fraud (?:state|area|region)", r"fraud[- ]prone"]
ACTION_WORDS = ["investigat", "escalat", "verify", "request", "refer", "review", "confirm", "pull", "compare", "check"]

_ID_RE = re.compile(r"\[(E\d+)\]")
_GROUP_RE = re.compile(r"\[\s*(E\d+(?:\s*(?:,|;|and|[-–])\s*E?\d+)+)\s*\]")


def normalize_citations(text: str) -> str:
    """Expand grouped citations that models often write, "[E11, E13]" or "[E1-E10]", into "[E11][E13]"."""
    def expand(m: re.Match) -> str:
        body, ids = m.group(1), []
        for part in re.split(r"\s*(?:,|;|and)\s*", body):
            rng = re.match(r"E(\d+)\s*[-–]\s*E?(\d+)$", part.strip())
            if rng and int(rng.group(2)) - int(rng.group(1)) <= 30:
                ids += [f"E{i}" for i in range(int(rng.group(1)), int(rng.group(2)) + 1)]
            elif re.match(r"E\d+$", part.strip()):
                ids.append(part.strip())
        return "".join(f"[{i}]" for i in ids) if ids else m.group(0)
    return _GROUP_RE.sub(expand, str(text or ""))
_NUM_RE = re.compile(r"(?<![\w.])([-+]?\$?\d(?:[\d,]*\d)?(?:\.\d+)?)(\s?(?:%|[kK]\b))?")


# --------------------------------------------------------------------------------------
# Number grounding
# --------------------------------------------------------------------------------------


def _strip_identifiers(text: str) -> str:
    text = re.sub(r"\[E\d+\]", " ", text)
    text = re.sub(r"\bE\d+\b", " ", text)
    text = re.sub(r"\bC\d{3,}\b", " ", text)
    text = re.sub(r"\bLTC-\d+\b", " ", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)
    return text


def extract_numbers(text: str) -> list[tuple[str, float]]:
    out = []
    for m in _NUM_RE.finditer(_strip_identifiers(text)):
        raw = m.group(1).replace("$", "").replace(",", "")
        if raw in {"", "+", "-"}:
            continue
        try:
            val = abs(float(raw))
        except ValueError:
            continue
        suffix = (m.group(2) or "").strip().lower()
        if suffix == "k":
            val *= 1000
        out.append((m.group(0).strip(), val))
    return out


def allowed_numbers(case: dict, pack: dict) -> list[float]:
    nums: set[float] = {float(n) for n in range(0, 13)}  # counts such as "five families", "12 months"
    nums.update({14.0, 100.0, 50.0})  # scale endpoints and queue size
    for _, v in extract_numbers(json.dumps(pack)):
        nums.add(v)
    f = case["facts"]
    nums.update({float(f["claim_amount_usd"]), round(f["claim_amount_usd"] / 1000, 1), float(case["risk_score"]),
                 float(case["risk_exact"]), float(case["dollars_at_risk"]), round(case["dollars_at_risk"])})
    for part in str(f["claim_date"]).split("-"):
        if part.isdigit():
            nums.add(float(part))
    for s in case["signals"]:
        v = s["value"]
        if v is None:
            continue
        nums.add(abs(v))
        if s["kind"] == "continuous" and s["thresholds"]:
            lo, hi = s["thresholds"]
            share = s["display"].endswith("%") and abs(v) <= 1
            for x in (lo, hi):
                nums.add(abs(x))
                if share:
                    nums.add(round(abs(x) * 100))
            if share:
                nums.add(round(abs(v) * 100))
            if lo > 0:
                nums.add(round(abs(v) / lo, 1))
            nums.add(round(abs(v) - lo, 2))
            if share:
                nums.add(round((abs(v) - lo) * 100))
        if s["percentile"] is not None:
            nums.add(float(s["percentile"]))
    nums.add(float(case["anomaly"]["percentile"]))
    for fam in case["families"]:
        nums.add(abs(fam["drop_if_explained"]))
        nums.add(round(abs(fam["drop_if_explained"])))
    return sorted(nums)


def _supported(v: float, allowed: list[float]) -> bool:
    return any(abs(v - a) <= max(0.51, 0.015 * min(abs(v), abs(a))) for a in allowed)


# --------------------------------------------------------------------------------------
# Report helpers
# --------------------------------------------------------------------------------------


def _issue(criterion: str, severity: str, problem: str, fix: str, evidence_ids=None, quote: str = "") -> dict:
    return {
        "criterion": criterion,
        "severity": severity,
        "problem": problem,
        "fix": fix,
        "evidence_ids": evidence_ids or [],
        "quote": quote,
        "source": "rules",
    }


def score_and_verdict(issues: list[dict], criteria: list[str]) -> tuple[dict, str]:
    scores = {c: 5 for c in criteria}
    for i in issues:
        if i["criterion"] in scores:
            scores[i["criterion"]] = max(1, scores[i["criterion"]] - PENALTY[i["severity"]])
    if any(i["severity"] == "high" for i in issues):
        verdict = "fail"
    elif any(i["severity"] == "medium" for i in issues):
        verdict = "revise"
    else:
        verdict = "pass"
    return scores, verdict


def _find(patterns: list[str], text: str) -> list[str]:
    hits = []
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            hits.append(m.group(0))
    return hits


def _contains_any(words: list[str], text: str) -> list[str]:
    low = text.lower()
    return [w for w in words if w in low]


# --------------------------------------------------------------------------------------
# Assessment checks
# --------------------------------------------------------------------------------------


def run_rule_checks(narr: dict, case: dict, pack: dict) -> dict:
    issues: list[dict] = []
    valid_ids = {e["id"] for e in pack["evidence"]}
    sig_by_id = {s["evidence_id"]: s for s in case["signals"]}
    indicators = narr.get("key_indicators") or []
    parts = {
        "summary": narr.get("summary") or "",
        "action": narr.get("recommended_action") or "",
        "steps": " ".join(narr.get("next_steps") or []),
        "indicators": " ".join(i.get("statement", "") for i in indicators),
        "uncertainties": " ".join(narr.get("uncertainties") or []),
        "innocent": " ".join(narr.get("innocent_explanations") or []),
    }
    full = " ".join(parts.values())
    headline = parts["summary"] + " " + parts["action"]
    lane, base_lane = case["lane"], case["base_lane"]
    codes = {g["code"] for g in case["guardrails"]}

    # ---- groundedness -------------------------------------------------------------
    cited = set(_ID_RE.findall(full)) | {i.get("evidence_id") for i in indicators if i.get("evidence_id")}
    bad = sorted(c for c in cited if c not in valid_ids)
    if bad:
        issues.append(_issue("groundedness", "high", f"Cites evidence that does not exist: {', '.join(bad)}.",
                             "Cite only IDs from the evidence pack.", bad))
    if any(not i.get("evidence_id") for i in indicators):
        issues.append(_issue("groundedness", "high", "A key indicator has no evidence ID.",
                             "Attach an evidence ID to every key indicator."))
    allowed = allowed_numbers(case, pack)
    unsupported = [tok for tok, v in extract_numbers(full) if not _supported(v, allowed)]
    if unsupported:
        issues.append(_issue("groundedness", "high",
                             f"Figures that do not appear in the case data: {', '.join(dict.fromkeys(unsupported))}.",
                             "Copy figures exactly from the evidence pack.", quote=", ".join(unsupported)))
    if not _ID_RE.search(parts["summary"]):
        issues.append(_issue("groundedness", "medium", "The summary cites no evidence IDs.",
                             "Cite the evidence ID after each factual claim in the summary."))
    for ind in indicators:
        s = sig_by_id.get(ind.get("evidence_id", ""))
        if not s:
            continue
        if ind.get("direction") == "risk" and s["level"] == "normal":
            issues.append(_issue("groundedness", "medium",
                                 f"{s['label']} [{s['evidence_id']}] is normal but is presented as a risk indicator.",
                                 "Mark it as mitigating or drop it.", [s["evidence_id"]], ind.get("statement", "")))
        if ind.get("direction") == "mitigating" and s["level"] == "high":
            issues.append(_issue("groundedness", "medium",
                                 f"{s['label']} [{s['evidence_id']}] is high but is presented as mitigating.",
                                 "Mark it as a risk indicator.", [s["evidence_id"]], ind.get("statement", "")))

    # ---- completeness -------------------------------------------------------------
    if lane in ("suspicious", "review") and base_lane != "likely_fp":
        fams = [f for f in case["families"] if f["key"] in case["active_families"]][:2]
        for fam in fams:
            members = [s for s in case["signals"] if s["family"] == fam["key"] and s["strength"]]
            top = max(members, key=lambda s: s["strength"])
            if top["evidence_id"] not in cited:
                issues.append(_issue("completeness", "medium",
                                     f"Does not cite the strongest driver of '{fam['short']}': {top['label']} [{top['evidence_id']}].",
                                     "Cite and explain the top driver of each leading evidence family.", [top["evidence_id"]]))
        clean_ids = {s["evidence_id"] for s in case["signals"] if s["level"] == "normal"}
        if lane == "suspicious" and clean_ids and not (clean_ids & cited) and not parts["innocent"].strip():
            issues.append(_issue("completeness", "low",
                                 "Does not acknowledge any evidence that points toward innocence.",
                                 "Mention clean evidence families or innocent explanations for balance.", sorted(clean_ids)[:3]))
    if lane == "likely_fp":
        normal_cited = [c for c in cited if c in sig_by_id and sig_by_id[c]["level"] == "normal"]
        if len(normal_cited) < 2:
            issues.append(_issue("completeness", "medium",
                                 "A likely-false-positive call should cite at least two normal signals as support.",
                                 "Cite the clean signals that justify closing."))
    if "linked_to_suspicious" in codes:
        link_ids = {e["id"] for e in pack["evidence"] if e["kind"] == "link"}
        if not (link_ids & cited):
            issues.append(_issue("completeness", "high",
                                 "Ignores the review rule: this case shares a claim number with a suspicious case.",
                                 "Cite the linked case and explain that the link must be resolved before closing.", sorted(link_ids)))
    if case.get("qa_sample") and not _contains_any(["quality", "full review", "qa sample"], full):
        issues.append(_issue("completeness", "medium", "Does not mention that this case is a quality-assurance sample.",
                             "State that a full review is required before closing."))

    # ---- calibration --------------------------------------------------------------
    if lane == "likely_fp":
        hits = _contains_any(STRONG_RISK, headline)
        if hits:
            issues.append(_issue("calibration", "high",
                                 f"Uses strong-risk language ('{hits[0]}') for a likely false positive.",
                                 "Match the language to the risk rating.", quote=hits[0]))
    if lane == "suspicious":
        hits = _contains_any(BENIGN_LANGUAGE, headline)
        if hits:
            issues.append(_issue("calibration", "high",
                                 f"Uses benign language ('{hits[0]}') for a genuinely suspicious case.",
                                 "Match the language to the risk rating.", quote=hits[0]))
    over = _find(OVERCONFIDENT, full)
    if over:
        issues.append(_issue("calibration", "medium",
                             f"Overstates certainty ('{over[0]}'). Indicators justify investigation; they do not prove fraud.",
                             "Use calibrated language such as 'consistent with' or 'warrants review'.", quote=over[0]))

    # ---- actionability ------------------------------------------------------------
    if not parts["action"].strip():
        issues.append(_issue("actionability", "high", "No recommended action.", "State one clear next action."))
    elif lane == "suspicious" and not _contains_any(ACTION_WORDS, parts["action"] + parts["steps"]):
        issues.append(_issue("actionability", "medium", "The action for a suspicious case is not an investigative step.",
                             "Recommend investigation, verification, or escalation."))
    if lane != "likely_fp" and not (narr.get("next_steps") or []):
        issues.append(_issue("actionability", "medium", "No next steps for a case that needs follow-up.",
                             "List concrete verification steps."))
    if lane == "likely_fp" and not codes & {"qa_sample"} and not re.search(r"\bclos(e|ed|es|ing|ure)\b", parts["action"].lower()):
        issues.append(_issue("actionability", "low", "A likely false positive should recommend closing.",
                             "Recommend closing after a quick check."))

    # ---- neutrality ---------------------------------------------------------------
    acc = _find(ACCUSATORY, full)
    if acc:
        issues.append(_issue("neutrality", "high", f"Accusatory language about people ('{acc[0]}').",
                             "Describe indicators, not people. Avoid claims about guilt or intent.", quote=acc[0]))
    prot = _find(PROTECTED, full)
    if prot:
        issues.append(_issue("neutrality", "high", f"Refers to a protected characteristic ('{prot[0]}').",
                             "Remove references to protected characteristics.", quote=prot[0]))
    geo = _find(GEO_STEREOTYPE, full)
    if geo:
        issues.append(_issue("neutrality", "medium", f"Relies on a geographic stereotype ('{geo[0]}').",
                             "Judge the claim on its own evidence.", quote=geo[0]))

    scores, verdict = score_and_verdict(issues, CRITERIA)
    return {
        "layer": "rules",
        "status": "ok",
        "scores": scores,
        "verdict": verdict,
        "issues": issues,
    }


# --------------------------------------------------------------------------------------
# Chat answer checks
# --------------------------------------------------------------------------------------


def run_chat_checks(answer: str, case: dict, pack: dict) -> dict:
    issues: list[dict] = []
    valid_ids = {e["id"] for e in pack["evidence"]}
    cited = set(_ID_RE.findall(answer))
    bad = sorted(c for c in cited if c not in valid_ids)
    if bad:
        issues.append(_issue("groundedness", "high", f"Cites evidence that does not exist: {', '.join(bad)}.",
                             "Cite only IDs from the evidence pack.", bad))
    allowed = allowed_numbers(case, pack)
    unsupported = [tok for tok, v in extract_numbers(answer) if not _supported(v, allowed)]
    if unsupported:
        issues.append(_issue("groundedness", "high",
                             f"Figures that do not appear in the case data: {', '.join(dict.fromkeys(unsupported))}.",
                             "Copy figures exactly from the evidence pack.", quote=", ".join(unsupported)))
    if extract_numbers(answer) and not cited and len(answer) > 160:
        issues.append(_issue("groundedness", "medium", "States facts without citing evidence IDs.",
                             "Cite evidence IDs for each factual claim."))
    over = _find(OVERCONFIDENT, answer)
    if over:
        issues.append(_issue("calibration", "medium", f"Overstates certainty ('{over[0]}').",
                             "Use calibrated language.", quote=over[0]))
    acc = _find(ACCUSATORY, answer)
    if acc:
        issues.append(_issue("neutrality", "high", f"Accusatory language about people ('{acc[0]}').",
                             "Describe indicators, not people.", quote=acc[0]))
    prot = _find(PROTECTED, answer)
    if prot:
        issues.append(_issue("neutrality", "high", f"Refers to a protected characteristic ('{prot[0]}').",
                             "Remove references to protected characteristics.", quote=prot[0]))
    scores, verdict = score_and_verdict(issues, CHAT_CRITERIA)
    return {"layer": "rules", "status": "ok", "scores": scores, "verdict": verdict, "issues": issues}


def worst_verdict(*verdicts: str | None) -> str:
    vs = [v for v in verdicts if v in VERDICT_RANK]
    return max(vs, key=lambda v: VERDICT_RANK[v]) if vs else "pass"
