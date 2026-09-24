import math

import numpy as np
import pandas as pd

from app.engine.narrative import answer_offline, offline_narrative
from app.engine.scoring import lane_for, score_signals, signal_strength, triage_queue
from app.engine.signals import LANE_HIGH_MIN, SIGNAL_KEYS


def test_signal_strength_ramps_and_clips():
    assert signal_strength("weekly_visit_frequency", 4, "In-Home Care") == 0.0
    assert signal_strength("weekly_visit_frequency", 14, "In-Home Care") == 1.0
    assert 0 < signal_strength("weekly_visit_frequency", 8, "In-Home Care") < 1
    assert signal_strength("duplicate_service_billed", 1, "In-Home Care") == 1.0
    assert signal_strength("duplicate_service_billed", 0, "In-Home Care") == 0.0
    assert signal_strength("weekly_visit_frequency", float("nan"), "In-Home Care") is None


def test_care_type_override_applies_to_adult_day_care():
    assert signal_strength("weekend_billing_ratio", 0.2, "Assisted Living") == 0.0
    assert signal_strength("weekend_billing_ratio", 0.2, "Adult Day Care") > 0.0


def test_clean_row_scores_zero_and_maxed_row_is_high():
    clean = {"care_type": "In-Home Care", **{k: 0 for k in SIGNAL_KEYS}}
    _, _, risk = score_signals(clean)
    assert risk == 0
    maxed = {"care_type": "In-Home Care", "duplicate_service_billed": 1, "service_overlap_other_provider": 1,
             "weekly_visit_frequency": 20, "member_provider_distance_miles": 300, "prior_claims_last_12mo": 12,
             "shared_contact_with_provider": 1, "weekend_billing_ratio": 0.9, "amount_vs_peer_avg_pct": 200,
             "round_dollar_billing_ratio": 0.95, "recent_policy_change_flag": 1}
    _, _, risk = score_signals(maxed)
    assert risk >= LANE_HIGH_MIN
    assert lane_for(risk) == "suspicious"


def test_correlated_signals_do_not_stack_like_independent_ones():
    base = {"care_type": "In-Home Care", **{k: 0 for k in SIGNAL_KEYS}}
    one_family = dict(base, weekly_visit_frequency=14, weekend_billing_ratio=0.9, member_provider_distance_miles=300)
    two_families = dict(base, weekly_visit_frequency=14, duplicate_service_billed=1)
    _, _, r1 = score_signals(one_family)
    _, _, r2 = score_signals(two_families)
    assert r2 > r1, "two independent families should outweigh three correlated signals in one family"


def test_queue_lanes_and_ordering(queue):
    lanes = [queue.cases[c]["lane"] for c in queue.order]
    assert lanes == sorted(lanes, key=lambda l: {"suspicious": 0, "review": 1, "likely_fp": 2}[l])
    counts = {l: lanes.count(l) for l in set(lanes)}
    assert counts["suspicious"] >= 10 and counts["likely_fp"] >= 25 and counts["review"] >= 3
    for c in queue.cases.values():
        assert 0 <= c["risk_score"] <= 100
        assert len(c["signals"]) == 10 and len(c["families"]) == 5


def test_claim_number_collision_creates_link_and_guardrail(queue):
    assert any(a["code"] == "claim_number_collision" for a in queue.alerts)
    c1001, c1031 = queue.cases["C1001"], queue.cases["C1031"]
    assert c1001["base_lane"] == "likely_fp" and c1001["lane"] == "review"
    assert {g["code"] for g in c1001["guardrails"]} == {"linked_to_suspicious"}
    assert c1031["linked"][0]["case_id"] == "C1001"


def test_missing_signal_is_unknown_not_normal(df):
    d = df.copy()
    row = d.index[d["case_id"] == "C1002"][0]
    d.loc[row, "duplicate_service_billed"] = np.nan
    q = triage_queue(d, "t")
    c = q.cases["C1002"]
    dup = next(s for s in c["signals"] if s["key"] == "duplicate_service_billed")
    assert dup["level"] == "missing" and dup["strength"] is None
    assert c["completeness"] < 1
    assert c["lane"] == "review" and any(g["code"] == "data_gap" for g in c["guardrails"])
    assert c["confidence"]["level"] != "High"


def test_counterfactuals_are_consistent(queue):
    for c in queue.cases.values():
        cf = c["counterfactual"]
        for step in cf["lower"]:
            assert step["risk_after"] <= c["risk_score"] + 1
        for r in cf["raise"]:
            assert r["risk_after"] > c["risk_score"]
            assert r["lane_after"] != c["base_lane"]


def test_qa_sample_is_deterministic_and_small(queue):
    qa = [c for c in queue.cases.values() if c["qa_sample"]]
    n_low = sum(1 for c in queue.cases.values() if c["base_lane"] == "likely_fp")
    assert 1 <= len(qa) <= math.ceil(0.1 * n_low) + 1
    again = triage_queue(pd.read_csv("data/sample_cases_synthetic.csv"), "t")
    assert sorted(c["case_id"] for c in again.cases.values() if c["qa_sample"]) == sorted(c["case_id"] for c in qa)


def test_offline_narrative_covers_every_lane(queue):
    for c in queue.cases.values():
        n = offline_narrative(c)
        assert n["summary"] and n["recommended_action"] and n["key_indicators"]
        assert all(i["evidence_id"].startswith("E") for i in n["key_indicators"])
        if c["lane"] == "likely_fp":
            assert "false positive" in n["summary"]
        if c["lane"] == "suspicious":
            assert "investigation" in n["recommended_action"].lower()


def test_offline_answers_cite_evidence(queue):
    c = queue.cases["C1019"]
    for q in ["why is this suspicious", "what would change this", "weekend billing?", "linked cases", "next steps"]:
        a = answer_offline(c, q)
        assert a["citations"], q
    fallback = answer_offline(c, "who is the provider's CEO?")
    assert "offline mode" in fallback["answer"].lower()


def test_every_evidence_item_states_its_reasoning(queue):
    from app.engine.evidence import build_evidence
    for cid, c in queue.cases.items():
        for e in build_evidence(c):
            assert e["points_to"] and e["assumption"] and e["verify"], (cid, e["id"])
            assert e["counter_arguments"], (cid, e["id"])


def test_reasoning_is_care_type_aware(queue):
    from app.engine.evidence import build_evidence
    ev = {e["id"]: e for e in build_evidence(queue.cases["C1037"])}  # adult day care, 236 mi, 16 days/wk
    assert "closed" in ev["E5"]["assumption"] and "10%" in ev["E5"]["assumption"]
    assert "member travels to the center" in ev["E4"]["assumption"]
    assert "miles a week to the center" in ev["E4"]["context"]
    assert any("address of record was never updated" in c for c in ev["E4"]["counter_arguments"])
    sim = [e for e in build_evidence(queue.cases["C1024"]) if e["kind"] == "similar"]
    assert all("claim LTC-" in e["text"] for e in sim) and "rank" not in sim[1]["text"]


def test_every_evidence_item_has_a_combined_reading(queue):
    from app.engine.evidence import build_evidence
    for cid, c in queue.cases.items():
        for e in build_evidence(c):
            assert e.get("context"), (cid, e["id"])
    ev = {e["id"]: e for e in build_evidence(queue.cases["C1037"])}
    assert "(E3)" in ev["E5"]["context"] and "(E4)" in ev["E6"]["context"] and "(E7)" in ev["E8"]["context"]
    assert "(E10)" in ev["E9"]["context"] and "(E1)" in ev["E7"]["context"]


def test_column_logic_covers_every_pair_and_reaches_the_pack(queue):
    import pandas as pd
    from app.engine.analysis import analyse
    from app.engine.crosslogic import COLUMNS, PAIRS
    from app.engine.evidence import evidence_pack
    assert len(PAIRS) == len(COLUMNS) * (len(COLUMNS) - 1) // 2
    df = pd.read_csv("data/sample_cases_synthetic.csv")
    a = analyse(df, queue.cases, queue.order)
    assert len(a["crosslogic"]) == len(PAIRS) and all(x["verdict"] for x in a["crosslogic"])
    pairs = evidence_pack(queue.cases["C1037"])["engine"]["column_logic_for_red_flag_pairs"]
    assert any(p["evidence_ids"] == ["E3", "E4"] and "cut against" in p["relation"] for p in pairs)
    assert evidence_pack(queue.cases["C1002"])["engine"]["column_logic_for_red_flag_pairs"] == []


def test_family_shares_add_up_to_the_score(queue):
    for cid, c in queue.cases.items():
        shares = [f["share"] for f in c["families"]]
        if c["risk_exact"] > 0:
            assert abs(sum(shares) - 1) < 0.01, cid
            assert abs(sum(f["points"] for f in c["families"]) - c["risk_exact"]) < 0.6, cid
        else:
            assert sum(shares) == 0, cid


def test_flags_alone_cannot_make_a_case_suspicious(queue):
    from app.engine.signals import FLAG_ONLY_CAP
    capped = [cid for cid, c in queue.cases.items() if any(g["code"] == "flag_only_cap" for g in c["guardrails"])]
    assert capped and all(queue.cases[cid]["risk_exact"] <= FLAG_ONLY_CAP for cid in capped)
    assert all(queue.cases[cid]["base_lane"] != "suspicious" for cid in capped)


def test_residential_care_does_not_score_distance(queue):
    c = queue.cases["C1024"]  # skilled nursing, 114 mi
    dist = next(s for s in c["signals"] if s["key"] == "member_provider_distance_miles")
    assert dist["level"] == "not_scored" and dist["strength"] == 0
    prior = next(s for s in c["signals"] if s["key"] == "prior_claims_last_12mo")
    assert prior["level"] == "normal"  # 9 claims a year is monthly billing for a resident


def test_evidence_pack_is_identical_across_processes():
    """The cache and the recorded run key on the evidence pack, so it must not depend on hash order."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = ("import json,pandas as pd;from app.engine.scoring import triage_queue;from app.llm.assessor import cache_key;"
            "q=triage_queue(pd.read_csv('data/sample_cases_synthetic.csv'),'2026-01-01T06:00:00+00:00');"
            "print(json.dumps({c:cache_key(q.cases[c],'w','j') for c in q.order}))")
    root = Path(__file__).resolve().parent.parent
    outs = {subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True,
                           env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(root)}).stdout for seed in ("1", "2", "3")}
    assert len(outs) == 1
