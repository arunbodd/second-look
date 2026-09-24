"""Deliberately flawed narratives, used only by the unit tests.

Each flaw corrupts a known-good narrative in one specific way, and the tests check that the
rule-based quality checks catch it.
"""

from __future__ import annotations

import copy
import re

FLAWS = {
    "invented_figure": {
        "label": "Invented figure",
        "expect": "groundedness",
        "description": "A statistic that does not exist in the case data is added to the summary.",
    },
    "bad_citation": {
        "label": "Citation to missing evidence",
        "expect": "groundedness",
        "description": "A claim cites an evidence ID that is not in the evidence pack.",
    },
    "missed_driver": {
        "label": "Missed top driver",
        "expect": "completeness",
        "description": "Every mention of the strongest risk driver is removed (for a clean case, the supporting evidence is removed instead).",
    },
    "overconfident": {
        "label": "Overstated certainty",
        "expect": "calibration",
        "description": "The narrative states fraud as a proven fact, or uses strong-risk language for a likely false positive.",
    },
    "accusatory": {
        "label": "Accusatory language",
        "expect": "neutrality",
        "description": "The narrative calls the provider a fraudster.",
    },
}


def inject_flaw(narrative: dict, case: dict, flaw: str) -> dict:
    n = copy.deepcopy(narrative)
    facts = case["facts"]
    if flaw == "invented_figure":
        from ..engine.evidence import evidence_pack
        from .checks import _supported, allowed_numbers

        allowed = allowed_numbers(case, evidence_pack(case))
        fake_visits = next(v for v in range(37, 400) if not _supported(v, allowed))
        n["summary"] += f" The provider also billed {fake_visits} visits per week and charged ${facts['claim_amount_usd'] + 4321:,} in total [E3]."
    elif flaw == "bad_citation":
        n["summary"] += " The provider's license lapsed last year [E42]."
        n["key_indicators"].append({"evidence_id": "E42", "statement": "The provider's license lapsed last year.",
                                    "direction": "risk"})
    elif flaw == "missed_driver":
        top_ids = _top_driver_ids(case)
        if not top_ids:  # a clean case: the "driver" is the clean evidence that justifies closing
            n["summary"] = re.sub(r"\s?\[E\d+\]", "", n["summary"])
            n["key_indicators"] = [i for i in n["key_indicators"] if i.get("direction") != "mitigating"]
        for eid in top_ids:
            n["summary"] = _drop_sentences_with(n["summary"], eid)
            n["key_indicators"] = [i for i in n["key_indicators"] if i.get("evidence_id") != eid]
        if not n["summary"].strip():
            n["summary"] = f"This {facts['care_type']} claim for ${facts['claim_amount_usd']:,} [E11] in {facts['state']} needs attention."
    elif flaw == "overconfident":
        if case["lane"] == "likely_fp":
            n["summary"] = (f"This {facts['care_type']} claim for ${facts['claim_amount_usd']:,} [E11] in {facts['state']} is "
                            "highly suspicious and needs a full investigation. " + n["summary"])
            n["recommended_action"] = "Open a full investigation immediately."
        else:
            n["summary"] += " This is definitely fraud, and the evidence proves the provider billed for care that never happened."
    elif flaw == "accusatory":
        n["summary"] += " The provider is clearly a fraudster who stole from the member."
    else:
        raise ValueError(f"Unknown flaw '{flaw}'. Choose from {', '.join(FLAWS)}.")
    return n


def _top_driver_ids(case: dict) -> list[str]:
    ids = []
    if case["base_lane"] == "likely_fp":
        if case["linked"] and case["lane"] == "review":
            from ..engine.evidence import link_evidence_id

            return [link_evidence_id(case, case["linked"][0]["case_id"])]
        return []  # a likely false positive has no risk driver; its evidence is the clean signals
    fams = [f for f in case["families"] if f["key"] in case["active_families"]][:1]
    for fam in fams:
        members = [s for s in case["signals"] if s["family"] == fam["key"] and s["strength"]]
        if members:
            ids.append(max(members, key=lambda s: s["strength"])["evidence_id"])
    return ids


def _drop_sentences_with(text: str, evidence_id: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if f"[{evidence_id}]" not in s]
    out = " ".join(kept).strip()
    return re.sub(rf"\[{evidence_id}\]", "", out)
