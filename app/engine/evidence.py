"""Evidence pack: the only material any language model is allowed to reason from.

Every item has a stable ID (E1, E2, ...). Narratives, chat answers, and judge feedback must
cite these IDs, and the quality checks reject citations or figures that are not in the pack.
"""

from __future__ import annotations

from .rationale import (
    ANOMALY_RATIONALE,
    CLAIM_AMOUNT_RATIONALE,
    LINK_RATIONALE,
    PROFILE_RATIONALE,
    SIGNAL_RATIONALE,
    SIMILAR_RATIONALE,
    Rationale,
    article,
)
from .readings import amount_reading, anomaly_reading, link_reading, profile_reading, signal_readings, similar_reading
from .crosslogic import E as PAIR_EID, PAIRS, RELATIONS
from .signals import LANES, ordinal

CLAIM_AMOUNT_ID = "E11"
PROFILE_ID = "E12"
ANOMALY_ID = "E13"
FIRST_LINK_NUM = 14


def _reasoning(r: Rationale, care_type: str, context: str | None = None) -> dict:
    assumption, counters = r.for_care(care_type)
    out = {"points_to": r.points_to, "assumption": assumption, "counter_arguments": counters, "verify": r.verify}
    if context:
        out["context"] = context
    return out


def _reading(level: str) -> str:
    if level in ("elevated", "high"):
        return "red flag"
    if level == "missing":
        return "missing"
    if level == "not_scored":
        return "not scored"
    return "clear"


def build_evidence(case: dict) -> list[dict]:
    items: list[dict] = []
    f = case["facts"]
    care = f["care_type"]
    readings = signal_readings(case)
    for s in case["signals"]:
        context = readings.get(s["key"])
        reading = _reading(s["level"])
        item = {
            "id": s["evidence_id"],
            "kind": "signal",
            "label": s["label"],
            "level": s["level"],
            "reading": reading,
            "value": s["display"],
            "expected_max": s["normal_display"],
            "queue_percentile": s["percentile"],
            "text": s["text"],
            "definition": s["meaning"],
            "source": (
                "Yes/no flag from the upstream fraud engine. The file does not show what it matched, so it is an unverified flag "
                "to verify."
                if s["kind"] == "binary"
                else "Value computed by the upstream fraud engine; the normal limit is this prototype's assumption."
            ),
            **_reasoning(SIGNAL_RATIONALE[s["key"]], care, context),
        }
        if reading == "not scored":
            item["points_to"] = ("Not scored for this care type. " + item["points_to"])
        if reading == "clear":
            item["points_to"] = "Inside the expected range, so this counts against: " + item["points_to"][0].lower() + item["points_to"][1:]
        items.append(item)
    items.append(
        {
            "id": CLAIM_AMOUNT_ID,
            "kind": "claim",
            "label": "Claim amount",
            "text": f"The claim amount is ${f['claim_amount_usd']:,}. Dollars at risk (amount multiplied by the "
            f"risk score) are about ${case['dollars_at_risk']:,.0f}.",
            **_reasoning(CLAIM_AMOUNT_RATIONALE, care, amount_reading(case)),
        }
    )
    items.append(
        {
            "id": PROFILE_ID,
            "kind": "claim",
            "label": "Claim profile",
            "text": f"{care} claim {f['claim_number']} dated {f['claim_date']} in {f['state']}.",
            **_reasoning(PROFILE_RATIONALE, care, profile_reading(case)),
        }
    )
    items.append(
        {
            "id": ANOMALY_ID,
            "kind": "model",
            "label": "Statistical outlier check",
            "text": case["anomaly"]["text"],
            **_reasoning(ANOMALY_RATIONALE, care, anomaly_reading(case)),
        }
    )
    for i, link in enumerate(case["linked"]):
        items.append(
            {
                "id": f"E{FIRST_LINK_NUM + i}",
                "kind": "link",
                "label": f"Linked case {link['case_id']} (claim {f['claim_number']})",
                "text": f"{link['reason']}: {link['case_id']} is {article(link['care_type'])} {link['care_type']} "
                f"claim in {link['state']} dated {link['claim_date']} with risk score {link['risk_score']} "
                f"({LANES[link['base_lane']]['label'].lower()}).",
                **_reasoning(LINK_RATIONALE, care, link_reading(case, link)),
            }
        )
    first_similar = FIRST_LINK_NUM + len(case["linked"])
    for i, sim in enumerate(case["similar"]):
        claim = sim.get("claim_number", "")
        rank = "closest" if i == 0 else f"{ordinal(i + 1)} closest"
        items.append(
            {
                "id": f"E{first_similar + i}",
                "kind": "similar",
                "label": f"Comparable case {sim['case_id']} (claim {claim})",
                "text": f"{sim['case_id']} (claim {claim}) is the {rank} signal profile to this case: "
                f"{article(sim['care_type'])} {sim['care_type']} claim in {sim.get('state', '')} with risk score "
                f"{sim['risk_score']} ({LANES[sim['base_lane']]['label'].lower()}).",
                **_reasoning(SIMILAR_RATIONALE, care, similar_reading(case, sim)),
            }
        )
    return items


def link_evidence_id(case: dict, linked_case_id: str) -> str | None:
    for i, link in enumerate(case["linked"]):
        if link["case_id"] == linked_case_id:
            return f"E{FIRST_LINK_NUM + i}"
    return None


def similar_evidence_id(case: dict, similar_case_id: str) -> str | None:
    first_similar = FIRST_LINK_NUM + len(case["linked"])
    for i, sim in enumerate(case["similar"]):
        if sim["case_id"] == similar_case_id:
            return f"E{first_similar + i}"
    return None


def evidence_pack(case: dict) -> dict:
    """Compact, model-facing view of one case."""
    fams = {f["key"]: f for f in case["families"]}
    active = [fams[k] for k in case["active_families"]]
    clean = [f for f in case["families"] if f["level"] == "normal"]
    cf = case["counterfactual"]
    lines = []
    for step in cf["lower"]:
        lines.append(
            f"If {', '.join(step['families'])} were ruled out, the score would fall to {step['risk_after']} "
            f"({LANES[step['lane_after']]['label'].lower()})."
        )
    for r in cf["raise"]:
        lines.append(
            f"If {r['label']} changed to {r['to']}, the score would rise to {r['risk_after']} "
            f"({LANES[r['lane_after']]['label'].lower()})."
        )
    fired = {s["key"] for s in case["signals"] if s["level"] in ("elevated", "high")}
    column_logic = [
        {"evidence_ids": [PAIR_EID[p.a], PAIR_EID[p.b]], "relation": RELATIONS[p.relation]["label"],
         "why": p.why, "both_high_means": p.both}
        for p in PAIRS
        if p.relation in ("together", "tension") and p.a in fired and p.b in fired
    ]
    return {
        "case": case["facts"],
        "engine": {
            "risk_score": case["risk_score"],
            "risk_scale": "0 to 100",
            "risk_rating": case["lane_label"],
            "confidence": case["confidence"]["level"],
            "confidence_reasons": case["confidence"]["reasons"],
            "active_evidence_families": [
                {
                    "family": f["label"],
                    "strength": f["strength"],
                    "share_of_score": f"{f['share'] * 100:.0f}%",
                    "points_of_score": f["points"],
                    "score_drop_if_explained": f["drop_if_explained"],
                    "evidence_ids": f["signals"],
                    "verification_steps": f["steps"],
                    "possible_innocent_explanations": f["benign"],
                }
                for f in active
            ],
            "clean_evidence_families": [f["label"] for f in clean],
            "review_rules_applied": [g["text"] for g in case["guardrails"]],
            "what_would_change_the_assessment": lines,
            "column_logic_for_red_flag_pairs": column_logic,
        },
        "evidence": build_evidence(case),
    }
