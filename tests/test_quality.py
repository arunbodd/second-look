
import pytest

from app.engine.evidence import evidence_pack
from app.engine.narrative import answer_offline, offline_narrative
from app.quality.checks import extract_numbers, run_chat_checks, run_rule_checks
from app.quality.flaws import FLAWS, inject_flaw
from app.quality.judge import _normalise, combine


def test_every_template_narrative_passes_rule_checks(queue):
    for c in queue.cases.values():
        r = run_rule_checks(offline_narrative(c), c, evidence_pack(c))
        assert r["verdict"] == "pass", (c["case_id"], r["issues"])


def test_every_offline_answer_passes_chat_checks(queue):
    for c in queue.cases.values():
        for q in ["why", "what would change", "next steps", "compare", "similar cases", "confidence", "weekend"]:
            a = answer_offline(c, q)
            r = run_chat_checks(a["answer"], c, evidence_pack(c))
            assert r["verdict"] == "pass", (c["case_id"], q, r["issues"])


@pytest.mark.parametrize("flaw", list(FLAWS))
@pytest.mark.parametrize("cid", ["C1024", "C1019", "C1026", "C1001", "C1002"])
def test_rule_checks_catch_every_injected_flaw(queue, flaw, cid):
    c = queue.cases[cid]
    pack = evidence_pack(c)
    flawed = inject_flaw(offline_narrative(c), c, flaw)
    r = run_rule_checks(flawed, c, pack)
    assert r["verdict"] != "pass"
    assert any(i["criterion"] == FLAWS[flaw]["expect"] for i in r["issues"]), r["issues"]


def test_number_extraction_ignores_identifiers():
    nums = extract_numbers("Case C1024, claim LTC-2027698 dated 2025-08-08 cites [E3] and $66,655 at 53% and 12.5k")
    assert [v for _, v in nums] == [66655.0, 53.0, 12500.0]


def test_unsupported_figures_and_bad_citations_fail(queue):
    c = queue.cases["C1024"]
    pack = evidence_pack(c)
    n = offline_narrative(c)
    n["summary"] += " The provider billed 999 visits [E77]."
    r = run_rule_checks(n, c, pack)
    problems = " ".join(i["problem"] for i in r["issues"])
    assert r["verdict"] == "fail" and "E77" in problems and "999" in problems


def test_judge_output_is_normalised_and_verdict_recomputed():
    raw = {"scores": {"groundedness": 2, "completeness": 9, "calibration": "4", "actionability": 5, "neutrality": 5},
           "verdict": "pass", "issues": [{"criterion": "groundedness", "severity": "HIGH", "problem": "made up", "evidence_ids": ["E1", "E99"]}],
           "summary": "x"}
    r = _normalise(raw, ["groundedness", "completeness", "calibration", "actionability", "neutrality"], {"E1", "E2"})
    assert r["scores"]["completeness"] == 5 and r["scores"]["calibration"] == 4
    assert r["issues"][0]["severity"] == "high" and r["issues"][0]["evidence_ids"] == ["E1"]
    assert r["verdict"] == "fail" and r["verdict_adjusted"] is True


def test_low_score_without_issue_gets_synthesised_feedback():
    raw = {"scores": {"groundedness": 3, "completeness": 5, "calibration": 5, "actionability": 5, "neutrality": 5}, "verdict": "pass", "issues": []}
    r = _normalise(raw, ["groundedness", "completeness", "calibration", "actionability", "neutrality"], set())
    assert r["verdict"] == "revise" and r["issues"][0]["criterion"] == "groundedness"


def test_combine_takes_worst_of_both_layers():
    rules = {"verdict": "pass", "scores": {"groundedness": 5, "completeness": 5, "calibration": 5, "actionability": 5, "neutrality": 5}, "issues": []}
    judge = {"status": "ok", "verdict": "revise", "scores": {"groundedness": 5, "completeness": 3, "calibration": 5, "actionability": 5, "neutrality": 5},
             "issues": [{"criterion": "completeness", "severity": "medium", "problem": "p", "fix": "f", "evidence_ids": [], "quote": "", "source": "llm_judge"}], "summary": "s"}
    q = combine(rules, judge)
    assert q["verdict"] == "revise" and q["scores"]["completeness"] == 3 and q["issues"][0]["source"] == "llm_judge"
    off = combine(rules, {"status": "off", "verdict": None, "scores": {}, "issues": []})
    assert off["verdict"] == "pass" and off["judge_status"] == "off"


def test_rule_checks_catch_flaws_on_every_case(queue):
    for c in queue.cases.values():
        pack = evidence_pack(c)
        for flaw, spec in FLAWS.items():
            r = run_rule_checks(inject_flaw(offline_narrative(c), c, flaw), c, pack)
            assert any(i["criterion"] == spec["expect"] for i in r["issues"]), (c["case_id"], flaw)


def test_offline_router_prefers_intents_over_signal_keywords(queue):
    c = queue.cases["C1019"]
    assert "risk score is" in answer_offline(c, "Why is this case rated this way?")["answer"]
    assert "Weekend billing ratio" in answer_offline(c, "what about the weekend billing?")["answer"]
    normal = answer_offline(c, "tell me about the policy change")["answer"]
    assert "no recent policy change" in normal and "modified" not in normal


def test_judge_without_scores_is_rejected():
    from app.llm.providers import LLMError

    with pytest.raises(LLMError):
        _normalise({"verdict": "pass", "issues": []}, ["groundedness", "completeness", "calibration", "actionability", "neutrality"], set())


def test_grouped_citations_are_expanded_and_closing_counts_as_close():
    from app.quality.checks import normalize_citations
    assert normalize_citations("a [E11, E13] b [E3-E5]") == "a [E11][E13] b [E3][E4][E5]"
    assert normalize_citations("[E1][E2]") == "[E1][E2]"


def test_recommend_closing_passes_the_close_check(queue):
    from app.engine.evidence import evidence_pack
    from app.engine.narrative import offline_narrative
    from app.quality.checks import run_rule_checks
    c = queue.cases["C1002"]
    n = offline_narrative(c)
    n["recommended_action"] = "Recommend closing the referral after a quick residence check [E4]."
    r = run_rule_checks(n, c, evidence_pack(c))
    assert not any("should recommend closing" in i["problem"] for i in r["issues"])
