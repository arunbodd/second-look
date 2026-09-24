"""Deterministic triage engine.

The engine owns every number the investigator sees: signal strengths, evidence-family
strengths, the 0-100 risk score, the triage lane, confidence, counterfactuals, and case links.
Language models only write explanations on top of this output (see app/llm).

Scoring in one paragraph
------------------------
Each signal becomes a 0-1 strength (signals.py). Signals are grouped into evidence families
that describe one fraud scheme each. The ten signals are highly correlated in this data
(pairwise r of 0.63 to 0.93), so adding them up would count one underlying pattern many times.
Within a family the engine therefore takes the strongest signal plus a small corroboration
bonus. Across families, evidence combines with a noisy-OR:
    risk = 1 - prod_f (1 - weight_f * strength_f)
so several independent lines of evidence push the score up faster than one very loud line.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .signals import (
    ALWAYS_MISSING,
    ENGINE_VERSION,
    FAMILIES,
    FLAG_ONLY_CAP,
    LANE_HIGH_MIN,
    LANE_LOW_MAX,
    LANES,
    QA_SAMPLE_RATE,
    SIGNAL_BY_KEY,
    SIGNAL_KEYS,
    SIGNALS,
    fmt_threshold,
    fmt_value,
    ordinal,
)

WITHIN_FAMILY_BONUS = 0.15
HIGH_LEVEL_AT = 0.6
ANOMALY_DISAGREE_HIGH = 90  # percentile: a "clean" case this unusual is sent to review
ANOMALY_DISAGREE_LOW = 30  # percentile: a "suspicious" case this ordinary lowers confidence


# --------------------------------------------------------------------------------------
# Primitive scoring functions
# --------------------------------------------------------------------------------------


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def signal_strength(key: str, value: Any, care_type: str) -> float | None:
    """Convert a raw signal value to a 0-1 strength. ``None`` means the value is missing."""
    if _is_missing(value):
        return None
    sig = SIGNAL_BY_KEY[key]
    value = float(value)
    if care_type in sig.unscored_care:
        return 0.0
    if sig.kind == "binary":
        return 1.0 if value >= 1 else 0.0
    lo, hi = sig.thresholds(care_type)
    return float(min(1.0, max(0.0, (value - lo) / (hi - lo))))


def strength_level(strength: float | None) -> str:
    if strength is None:
        return "missing"
    if strength <= 0:
        return "normal"
    if strength < HIGH_LEVEL_AT:
        return "elevated"
    return "high"


def family_strength(strengths: list[float | None]) -> float | None:
    known = sorted((s for s in strengths if s is not None), reverse=True)
    if not known:
        return None
    return float(min(1.0, known[0] + WITHIN_FAMILY_BONUS * sum(known[1:])))


def risk_from_families(fam: dict[str, float | None], exclude: set[str] | None = None) -> float:
    exclude = exclude or set()
    prod = 1.0
    for key, s in fam.items():
        if s is None or key in exclude:
            continue
        prod *= 1.0 - FAMILIES[key].weight * s
    return 100.0 * (1.0 - prod)


def lane_for(score: float) -> str:
    if score >= LANE_HIGH_MIN:
        return "suspicious"
    if score >= LANE_LOW_MAX:
        return "review"
    return "likely_fp"


def score_signals(row: dict[str, Any], overrides: dict[str, float] | None = None) -> tuple[dict, dict, float]:
    """Return (signal strengths, family strengths, risk score) for one case."""
    care_type = row["care_type"]
    values = {k: row.get(k) for k in SIGNAL_KEYS}
    if overrides:
        values.update(overrides)
    strengths = {k: signal_strength(k, values[k], care_type) for k in SIGNAL_KEYS}
    fam = families_from(strengths)
    return strengths, fam, capped_risk(fam, families_from(strengths, measured_only=True))


def families_from(strengths: dict[str, float | None], measured_only: bool = False) -> dict[str, float | None]:
    """Family strengths; with measured_only, the yes/no upstream flags count as zero."""
    st = {k: (0.0 if measured_only and SIGNAL_BY_KEY[k].kind == "binary" and v is not None else v) for k, v in strengths.items()}
    return {fk: family_strength([st[s.key] for s in SIGNALS if s.family == fk]) for fk in FAMILIES}


def capped_risk(fam: dict, measured_fam: dict, exclude: set[str] | None = None) -> float:
    """Noisy-OR risk, capped below the suspicious line when only the unverified flags put it there."""
    raw = risk_from_families(fam, exclude)
    if raw >= LANE_HIGH_MIN and risk_from_families(measured_fam, exclude) < LANE_LOW_MAX:
        return float(min(raw, FLAG_ONLY_CAP))
    return raw


def flag_cap_applied(strengths: dict[str, float | None]) -> bool:
    fam, mfam = families_from(strengths), families_from(strengths, measured_only=True)
    return risk_from_families(fam) >= LANE_HIGH_MIN and risk_from_families(mfam) < LANE_LOW_MAX


# --------------------------------------------------------------------------------------
# Queue-level analysis
# --------------------------------------------------------------------------------------


def _anomaly_percentiles(df: pd.DataFrame) -> dict[str, float]:
    """Second opinion: an unsupervised Isolation Forest over the raw signals.

    It knows nothing about the thresholds or families above, so agreement between the two
    methods is informative. Percentile 100 means the most unusual case in today's queue.
    In production the model would be fit on historical claims, not on one day's queue.
    """
    from sklearn.ensemble import IsolationForest

    x = df[SIGNAL_KEYS].astype(float)
    x = x.fillna(x.median())
    std = x.std(ddof=0).replace(0, 1.0)
    z = (x - x.mean()) / std
    model = IsolationForest(n_estimators=400, random_state=7, contamination="auto")
    model.fit(z.values)
    unusual = -model.score_samples(z.values)
    ranks = pd.Series(unusual, index=df["case_id"]).rank(pct=True) * 100
    return {cid: float(round(p)) for cid, p in ranks.items()}


def _percentile_in(values: pd.Series, v: float) -> float:
    vals = values.dropna().astype(float)
    if len(vals) == 0 or _is_missing(v):
        return float("nan")
    below = (vals < v).sum()
    equal = (vals == v).sum()
    return float(round(100.0 * (below + 0.5 * equal) / len(vals)))


def _qa_sample(case_ids: list[str]) -> set[str]:
    if not case_ids:
        return set()
    n = max(1, math.ceil(QA_SAMPLE_RATE * len(case_ids)))
    ranked = sorted(case_ids, key=lambda c: hashlib.sha256(f"qa-{c}".encode()).hexdigest())
    return set(ranked[:n])


@dataclass
class QueueResult:
    cases: dict[str, dict]
    order: list[str]
    alerts: list[dict]
    triaged_at: str
    engine_version: str = ENGINE_VERSION


def triage_queue(df: pd.DataFrame, triaged_at: str = "") -> QueueResult:
    df = df.copy().reset_index(drop=True)
    rows = df.to_dict(orient="records")
    anomaly = _anomaly_percentiles(df)

    base: dict[str, dict] = {}
    for row in rows:
        strengths, fam, risk = score_signals(row)
        base[row["case_id"]] = {"row": row, "strengths": strengths, "fam": fam, "risk": risk}

    # Links: the same claim number on more than one case.
    by_claim: dict[str, list[str]] = {}
    for row in rows:
        by_claim.setdefault(str(row["claim_number"]), []).append(row["case_id"])
    alerts = []
    for claim, ids in by_claim.items():
        if len(ids) > 1:
            alerts.append(
                {
                    "code": "claim_number_collision",
                    "case_ids": ids,
                    "text": f"Claim number {claim} appears on {len(ids)} cases ({', '.join(ids)}). "
                    "Claim numbers should be unique, so this is either a data error or a reused claim number.",
                }
            )

    base_lanes = {cid: lane_for(b["risk"]) for cid, b in base.items()}
    qa = _qa_sample([cid for cid, lane in base_lanes.items() if lane == "likely_fp"])

    # Similarity uses the strength vector so that "similar" means "similar evidence".
    vec = {cid: np.array([b["strengths"][k] or 0.0 for k in SIGNAL_KEYS]) for cid, b in base.items()}

    cases: dict[str, dict] = {}
    for row in rows:
        cid = row["case_id"]
        b = base[cid]
        cases[cid] = _build_case(
            row,
            b,
            df,
            anomaly_pct=anomaly[cid],
            linked=[
                {
                    "case_id": other,
                    "reason": f"Same claim number {row['claim_number']}",
                    "base_lane": base_lanes[other],
                    "risk_score": round(base[other]["risk"]),
                    "care_type": base[other]["row"]["care_type"],
                    "state": base[other]["row"]["state"],
                    "claim_date": base[other]["row"]["claim_date"],
                }
                for other in by_claim[str(row["claim_number"])]
                if other != cid
            ],
            similar=_similar(cid, vec, base, base_lanes),
            qa_sample=cid in qa,
        )

    order = sorted(
        cases,
        key=lambda c: (LANES[cases[c]["lane"]]["order"], -cases[c]["dollars_at_risk"], c),
    )
    return QueueResult(cases=cases, order=order, alerts=alerts, triaged_at=triaged_at)


def _similar(cid: str, vec: dict, base: dict, base_lanes: dict, k: int = 3) -> list[dict]:
    dists = sorted(
        ((float(np.linalg.norm(vec[cid] - v)), other) for other, v in vec.items() if other != cid)
    )[:k]
    return [
        {
            "case_id": other,
            "distance": round(d, 2),
            "base_lane": base_lanes[other],
            "risk_score": round(base[other]["risk"]),
            "care_type": base[other]["row"]["care_type"],
            "claim_number": str(base[other]["row"]["claim_number"]),
            "flags": [k for k in SIGNAL_KEYS if strength_level(base[other]["strengths"][k]) in ("elevated", "high")],
            "state": base[other]["row"]["state"],
            "amount": int(base[other]["row"]["claim_amount_usd"]),
        }
        for d, other in dists
    ]


# --------------------------------------------------------------------------------------
# Per-case assembly
# --------------------------------------------------------------------------------------


def _counterfactuals(row: dict, fam: dict, risk: float, lane: str) -> dict:
    mfam = families_from(score_signals(row)[0], measured_only=True)
    lower: list[dict] = []
    if lane in ("suspicious", "review"):
        removed: set[str] = set()
        order: list[str] = []  # removal order, so the text is the same in every process
        current_lane = lane
        for _ in range(3):
            candidates = [f for f in fam if f not in removed and fam[f]]
            if not candidates:
                break
            best = max(candidates, key=lambda f: risk - capped_risk(fam, mfam, removed | {f}))
            removed.add(best)
            order.append(best)
            new_risk = min(risk, capped_risk(fam, mfam, removed))
            lower.append(
                {
                    "families": [FAMILIES[f].short for f in order],
                    "family_keys": list(order),
                    "risk_after": round(new_risk),
                    "lane_after": lane_for(new_risk),
                }
            )
            if lane_for(new_risk) != current_lane:
                break

    raise_: list[dict] = []
    if lane in ("likely_fp", "review"):
        for sig in SIGNALS:
            v = row.get(sig.key)
            if _is_missing(v):
                continue
            s = signal_strength(sig.key, v, row["care_type"])
            if (s and s >= HIGH_LEVEL_AT) or row["care_type"] in sig.unscored_care:
                continue
            target = 1.0 if sig.kind == "binary" else sig.thresholds(row["care_type"])[1]
            _, _, new_risk = score_signals(row, {sig.key: target})
            if lane_for(new_risk) != lane:
                raise_.append(
                    {
                        "signal": sig.key,
                        "label": sig.label,
                        "to": fmt_value(sig, target, row["care_type"]),
                        "risk_after": round(new_risk),
                        "lane_after": lane_for(new_risk),
                    }
                )
        raise_.sort(key=lambda r: -r["risk_after"])
        raise_ = raise_[:3]
    return {"lower": lower, "raise": raise_}


def _build_case(
    row: dict,
    b: dict,
    df: pd.DataFrame,
    anomaly_pct: float,
    linked: list[dict],
    similar: list[dict],
    qa_sample: bool,
) -> dict:
    care_type = row["care_type"]
    strengths, fam, risk = b["strengths"], b["fam"], b["risk"]
    mfam = families_from(strengths, measured_only=True)
    base_lane = lane_for(risk)
    lane = base_lane
    guardrails: list[dict] = []

    # ---- signals -------------------------------------------------------------------
    signals = []
    for i, sig in enumerate(SIGNALS, start=1):
        v = row.get(sig.key)
        missing = _is_missing(v)
        s = strengths[sig.key]
        lo_display = fmt_threshold(sig, care_type)
        text_tpl = sig.normal_text if (s is not None and s <= 0) else sig.triggered_text
        unscored = care_type in sig.unscored_care
        text = (
            f"{sig.label} is missing from the referral data."
            if missing
            else f"The address of record is {fmt_value(sig, v, care_type)} from the provider. Not scored for "
            f"{care_type}: the member lives at the facility."
            if unscored
            else text_tpl.format(v=fmt_value(sig, v, care_type), normal=lo_display, care_type=care_type)
        )
        signals.append(
            {
                "evidence_id": f"E{i}",
                "key": sig.key,
                "label": sig.label,
                "family": sig.family,
                "kind": sig.kind,
                "value": None if missing else float(v),
                "display": fmt_value(sig, None if missing else v, care_type),
                "normal_display": lo_display,
                "thresholds": None if sig.kind == "binary" else list(sig.thresholds(care_type)),
                "strength": None if s is None else round(s, 3),
                "level": "not_scored" if (unscored and not missing) else strength_level(s),
                "percentile": None if missing else _percentile_in(df[sig.key], float(v)),
                "text": text,
                "meaning": sig.meaning,
            }
        )
    completeness = sum(1 for s in signals if s["level"] != "missing") / len(signals)

    # ---- families ------------------------------------------------------------------
    families = []
    for fk, f in FAMILIES.items():
        s = fam[fk]
        families.append(
            {
                "key": fk,
                "label": f.label,
                "short": f.short,
                "weight": f.weight,
                "strength": None if s is None else round(s, 3),
                "evidence": 0.0 if s is None else round(f.weight * s, 3),
                "drop_if_explained": round(max(0.0, risk - capped_risk(fam, mfam, {fk})), 1),
                "level": strength_level(s),
                "signals": [x["evidence_id"] for x in signals if x["family"] == fk],
                "question": f.question,
                "steps": list(f.steps),
                "benign": list(f.benign),
            }
        )
    # Share of the score each family carries. The score is 100 * (1 - prod(1 - w_i * s_i)), so
    # -ln(1 - score/100) = sum of -ln(1 - w_i * s_i): each family's term over that sum is its exact
    # share, the shares add up to 100%, and share * score gives points that add up to the score.
    logs = {f["key"]: (-math.log(1.0 - f["evidence"]) if 0 < f["evidence"] < 1 else 0.0) for f in families}
    total_log = sum(logs.values())
    for f in families:
        share = logs[f["key"]] / total_log if total_log > 0 else 0.0
        f["share"] = round(share, 4)
        f["points"] = round(share * risk, 1)
    families.sort(key=lambda f: (-f["share"], -f["drop_if_explained"]))
    active = [f for f in families if f["level"] in ("elevated", "high")]

    # ---- guardrails (safety rules that can only raise a lane, never lower it) ---------
    high_links = [l for l in linked if l["base_lane"] == "suspicious"]
    if high_links and lane == "likely_fp":
        lane = "review"
        guardrails.append(
            {
                "code": "linked_to_suspicious",
                "text": f"Shares claim number {row['claim_number']} with {high_links[0]['case_id']}, "
                "which looks genuinely suspicious. Resolve the link before closing.",
            }
        )
    if linked and not high_links:
        guardrails.append(
            {"code": "linked", "text": f"Shares claim number {row['claim_number']} with {linked[0]['case_id']}."}
        )
    if flag_cap_applied(strengths):
        guardrails.append(
            {
                "code": "flag_only_cap",
                "text": f"Held at {FLAG_ONLY_CAP}: the yes/no upstream flags alone would rate this case suspicious, but its "
                "measured signals stay below the review line. Verify the flags before treating it as suspicious.",
            }
        )
    if lane == "likely_fp" and anomaly_pct >= ANOMALY_DISAGREE_HIGH:
        lane = "review"
        guardrails.append(
            {
                "code": "anomaly_disagrees",
                "text": f"The rules score this case low, but the statistical outlier check ranks it at the "
                f"{ordinal(anomaly_pct)} percentile of today's queue. Sent to review.",
            }
        )
    missing_heavy = [
        s["label"] for s in signals if s["level"] == "missing" and FAMILIES[s["family"]].weight >= 0.45
    ]
    if missing_heavy and lane == "likely_fp":
        lane = "review"
        guardrails.append(
            {
                "code": "data_gap",
                "text": f"Cannot recommend a fast close while {', '.join(missing_heavy)} is missing.",
            }
        )
    if qa_sample and lane == "likely_fp":
        guardrails.append(
            {
                "code": "qa_sample",
                "text": "Randomly selected for a full quality-assurance review (10% of likely false positives) "
                "so the team can measure how often the triage misses real fraud.",
            }
        )

    # ---- anomaly agreement -----------------------------------------------------------
    if base_lane == "suspicious":
        agrees = anomaly_pct >= ANOMALY_DISAGREE_LOW
    elif base_lane == "likely_fp":
        agrees = anomaly_pct < ANOMALY_DISAGREE_HIGH
    else:  # a middle-lane case should at least not be among the most ordinary in the queue
        agrees = anomaly_pct >= 20
    anomaly = {
        "percentile": anomaly_pct,
        "agrees": agrees,
        "text": (
            f"An independent statistical outlier check (Isolation Forest over all ten signals) ranks this case "
            f"more unusual than {anomaly_pct:.0f}% of today's queue"
            + (", which agrees with the rules-based score." if agrees else ", which disagrees with the rules-based score.")
        ),
    }

    # ---- confidence ------------------------------------------------------------------
    confidence, reasons = "High", []
    if lane == "suspicious":
        margin = risk - LANE_HIGH_MIN
    elif lane == "likely_fp":
        margin = LANE_LOW_MAX - risk
    else:
        margin = min(risk - LANE_LOW_MAX, LANE_HIGH_MIN - risk) if base_lane == "review" else 0

    def downgrade(to: str, why: str) -> None:
        nonlocal confidence
        rank = {"High": 2, "Medium": 1, "Low": 0}
        if rank[to] < rank[confidence]:
            confidence = to
        reasons.append({"text": why, "positive": False})

    def support(why: str) -> None:
        reasons.append({"text": why, "positive": True})

    if base_lane == lane:
        if margin < 4:
            downgrade("Low", "The score sits within 4 points of a risk-rating boundary (30 or 60).")
        elif margin < 10:
            downgrade("Medium", "The score sits within 10 points of a risk-rating boundary (30 or 60).")
        else:
            support("The score is well clear of the risk-rating boundaries (30 and 60).")
    else:
        downgrade("Medium", "A safety rule raised the risk rating above what the score alone gives.")
    if not agrees:
        downgrade("Low", "The statistical outlier check disagrees with the rules-based score.")
    else:
        support("The statistical outlier check agrees with the rules-based score.")
    if completeness < 1:
        downgrade("Low" if completeness < 0.8 else "Medium", f"{round((1 - completeness) * 10)} of 10 signals are missing.")
    if lane == "review" and base_lane == "review":
        downgrade("Medium", "The evidence is mixed: some families are active and others are clean.")

    amount = float(row["claim_amount_usd"])
    return {
        "case_id": row["case_id"],
        "facts": {
            "case_id": row["case_id"],
            "claim_number": str(row["claim_number"]),
            "claim_date": str(row["claim_date"]),
            "care_type": care_type,
            "claim_amount_usd": int(amount),
            "state": row["state"],
        },
        "signals": signals,
        "families": families,
        "active_families": [f["key"] for f in active],
        "data_gaps": [n for f in active for n in FAMILIES[f["key"]].needs][:6] + [ALWAYS_MISSING],
        "risk_score": int(round(risk)),
        "risk_exact": round(risk, 2),
        "base_lane": base_lane,
        "lane": lane,
        "lane_label": LANES[lane]["label"],
        "guardrails": guardrails,
        "confidence": {"level": confidence, "reasons": reasons},
        "anomaly": anomaly,
        "linked": linked,
        "similar": similar,
        "counterfactual": _counterfactuals(row, fam, risk, base_lane),
        "dollars_at_risk": round(amount * risk / 100.0, 2),
        "qa_sample": bool(qa_sample and lane == "likely_fp"),
        "completeness": round(completeness, 2),
        "engine_version": ENGINE_VERSION,
    }
