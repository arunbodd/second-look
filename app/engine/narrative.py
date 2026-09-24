"""Offline narrative and offline question answering.

These templates run with no model and no network, so the prototype always works. They use
the same evidence IDs and pass through the same quality checks as model-written output.
"""

from __future__ import annotations

import re

from .evidence import ANOMALY_ID, CLAIM_AMOUNT_ID, link_evidence_id, similar_evidence_id
from .signals import FAMILIES, LANES, SIGNAL_BY_KEY, ordinal

NARRATIVE_VERSION = "1.1"

NUM_WORDS = {0: "no", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
             8: "eight", 9: "nine", 10: "ten"}


def num_word(n: int) -> str:
    return NUM_WORDS.get(n, str(n))


def oxford(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def phrase(s: dict) -> str:
    """Short noun phrase for a signal, used inside sentences."""
    v = s["value"]
    k = s["key"]
    if k == "duplicate_service_billed":
        return "an upstream duplicate-service flag"
    if k == "service_overlap_other_provider":
        return "an upstream flag for an overlapping claim from another provider"
    if k == "weekly_visit_frequency":
        return f"{v:.0f} billed visits per week"
    if k == "member_provider_distance_miles":
        return f"a provider {v:.0f} miles from the member"
    if k == "weekend_billing_ratio":
        return f"{s['display']} of services billed on weekends"
    if k == "shared_contact_with_provider":
        return "contact details shared with the provider"
    if k == "amount_vs_peer_avg_pct":
        return f"charges {abs(v):.0f}% {'above' if v >= 0 else 'below'} the peer average"
    if k == "round_dollar_billing_ratio":
        return f"{s['display']} round-dollar line items"
    if k == "recent_policy_change_flag":
        return "a recent policy change"
    if k == "prior_claims_last_12mo":
        return f"{v:.0f} prior claims in 12 months"
    return s["label"].lower()


def _cite(s: dict) -> str:
    return f"[{s['evidence_id']}]"


def _parts(case: dict) -> dict:
    sig = {s["key"]: s for s in case["signals"]}
    triggered = sorted(
        (s for s in case["signals"] if s["level"] in ("elevated", "high")), key=lambda s: -s["strength"]
    )
    normal = [s for s in case["signals"] if s["level"] == "normal"]
    active = [f for f in case["families"] if f["key"] in case["active_families"]]
    clean = [f for f in case["families"] if f["level"] == "normal"]
    return {"sig": sig, "triggered": triggered, "normal": normal, "active": active, "clean": clean}


def _strongest_in(family: dict, case: dict) -> dict:
    members = [s for s in case["signals"] if s["family"] == family["key"] and s["strength"]]
    return max(members, key=lambda s: s["strength"])


CLEAN_PHRASES = {
    "duplicate_service_billed": "no duplicate billing",
    "service_overlap_other_provider": "no overlapping provider",
    "shared_contact_with_provider": "no shared contact with the provider",
    "weekly_visit_frequency": "a normal visit frequency",
    "member_provider_distance_miles": "a normal distance to the provider",
    "amount_vs_peer_avg_pct": "charges in line with peers",
}


def _clean_signals(p: dict, n: int = 3) -> list[dict]:
    normal = {s["key"]: s for s in p["normal"]}
    return [normal[k] for k in CLEAN_PHRASES if k in normal][:n]


def _clean_sentence(p: dict) -> str:
    picks = _clean_signals(p)
    if not picks:
        return ""
    return "There is " + oxford([f"{CLEAN_PHRASES[s['key']]} {_cite(s)}" for s in picks]) + "."


def _clean_cites(p: dict) -> str:
    return "".join(_cite(s) for s in _clean_signals(p, 2))


def offline_narrative(case: dict) -> dict:
    f = case["facts"]
    p = _parts(case)
    lane, base_lane = case["lane"], case["base_lane"]
    amount = f"${f['claim_amount_usd']:,}"
    head = f"This {f['care_type']} claim for {amount} [{CLAIM_AMOUNT_ID}] in {f['state']}"
    codes = {g["code"] for g in case["guardrails"]}
    indicators: list[dict] = []
    mitigating_ids = {"duplicate_service_billed", "service_overlap_other_provider", "shared_contact_with_provider",
                      "weekly_visit_frequency"}

    if lane == "suspicious":
        n = len(p["active"])
        labels = [FAMILIES[fam["key"]].risk_phrase for fam in p["active"]]
        lines = (
            f"shows evidence in all five families: {oxford(labels)}"
            if n == 5
            else f"shows {num_word(n)} independent lines of evidence: {oxford(labels)}"
        )
        top = [_strongest_in(fam, case) for fam in p["active"][:2]]
        summary = (
            f"{head} {lines}. The strongest indicators are {phrase(top[0])} {_cite(top[0])}"
            + (f" and {phrase(top[1])} {_cite(top[1])}" if len(top) > 1 else "")
            + ". Taken together, the pattern looks genuinely suspicious and warrants a full investigation."
        )
        if p["clean"]:
            clean_labels = " or ".join(FAMILIES[c["key"]].clean_phrase for c in p["clean"][:2])
            clean_ids = "".join(f"[{e}]" for c in p["clean"][:2] for e in c["signals"])
            summary += f" For balance, there is no evidence of {clean_labels} {clean_ids}."
        for s in p["triggered"][:4]:
            indicators.append({"evidence_id": s["evidence_id"], "statement": s["text"], "direction": "risk"})
        for s in p["normal"]:
            if s["key"] in mitigating_ids and len(indicators) < 6:
                indicators.append({"evidence_id": s["evidence_id"], "statement": s["text"], "direction": "mitigating"})
        action = "Open a full investigation."
        if f["claim_amount_usd"] >= 25000 and case["risk_score"] >= 80:
            action += " Ask the SIU lead to decide whether to hold payment while the investigation runs."
        steps = []
        for fam in p["active"][:2]:
            steps.extend(fam["steps"])
    elif lane == "review" and base_lane == "likely_fp":
        n_trig = len(p["triggered"])
        clean_bit = (
            "all ten signals are within expected ranges"
            if not n_trig
            else f"{num_word(len(p['normal']))} of ten signals are within expected ranges and the remaining "
            + ("signal is mild" if n_trig == 1 else "signals are mild")
        )
        if "linked_to_suspicious" in codes:
            other = next(l for l in case["linked"] if l["base_lane"] == "suspicious")
            eid = link_evidence_id(case, other["case_id"])
            summary = (
                f"On its own, this {f['care_type']} claim for {amount} [{CLAIM_AMOUNT_ID}] in {f['state']} would look "
                f"like a false positive, because {clean_bit} {_clean_cites(p)}. However, its claim number also appears on "
                f"{other['case_id']} [{eid}], which looks genuinely suspicious. The link needs to be resolved before "
                "this case can be closed."
            )
            article = "an" if other["care_type"][0].lower() in "aeiou" else "a"
            indicators.append({"evidence_id": eid, "statement": f"Claim number {f['claim_number']} is shared with "
                               f"{other['case_id']}, {article} {other['care_type']} claim in {other['state']} with risk score "
                               f"{other['risk_score']}.", "direction": "risk"})
            action = f"Resolve the claim-number link with {other['case_id']} before closing."
            steps = [
                f"Confirm with claims operations whether claim number {f['claim_number']} was keyed in error on one of the two cases.",
                f"If both cases are genuine, check whether the same member or provider is behind {f['case_id']} and {other['case_id']}.",
                "Close this case as a false positive only after the link is resolved.",
            ]
        elif "anomaly_disagrees" in codes:
            summary = (
                f"{head} scores low on the rules, because {clean_bit}. However, the statistical outlier check ranks "
                f"it as unusual for today's queue [{ANOMALY_ID}], so it deserves a second look before closing."
            )
            action = "Review the signal profile against similar cases before closing."
            steps = ["Compare this case with the similar cases listed below.", "Close if nothing stands out."]
        else:
            summary = f"{head} cannot be fast-closed because key signals are missing from the referral."
            action = "Request the missing data from the referral source before deciding."
            steps = ["Ask the fraud engine team to backfill the missing signals.", "Re-run the triage once the data arrives."]
        for s in p["normal"]:
            if s["key"] in mitigating_ids and len(indicators) < 4:
                indicators.append({"evidence_id": s["evidence_id"], "statement": s["text"], "direction": "mitigating"})
    elif lane == "review":
        top_f = p["active"][0]
        top_s = _strongest_in(top_f, case)
        k = len(p["clean"])
        summary = (
            f"{head} shows a mixed picture. The main concern is {FAMILIES[top_f['key']].risk_phrase}, "
            f"driven by {phrase(top_s)} {_cite(top_s)}. {num_word(k).capitalize()} of five evidence families "
            f"{'is' if k == 1 else 'are'} clean, so a targeted check should settle it."
        )
        for s in p["triggered"][:3]:
            indicators.append({"evidence_id": s["evidence_id"], "statement": s["text"], "direction": "risk"})
        for s in p["normal"]:
            if s["key"] in mitigating_ids and len(indicators) < 5:
                indicators.append({"evidence_id": s["evidence_id"], "statement": s["text"], "direction": "mitigating"})
        action = f"Verify before deciding: {top_f['steps'][0][0].lower() + top_f['steps'][0][1:]}"
        steps = list(top_f["steps"]) + ["Close as a false positive if the records check out, or open an investigation if they do not."]
    else:
        if not p["triggered"]:
            clean_bit = "All ten signals are within expected ranges."
        else:
            mild = p["triggered"][0]
            clean_bit = (
                f"{num_word(len(p['normal'])).capitalize()} of ten signals are within expected ranges. The one "
                f"exception is mild: {phrase(mild)} {_cite(mild)}."
            )
        summary = f"{head} looks like a likely false positive. {clean_bit} {_clean_sentence(p)}"
        if case["qa_sample"]:
            summary += " It was randomly selected for quality-assurance sampling, so it needs a full review before closing."
        for s in p["normal"]:
            if s["key"] in mitigating_ids:
                indicators.append({"evidence_id": s["evidence_id"], "statement": s["text"], "direction": "mitigating"})
        for s in p["triggered"][:1]:
            indicators.append({"evidence_id": s["evidence_id"], "statement": s["text"], "direction": "risk"})
        if case["qa_sample"]:
            action = "Complete a full review before closing, because this case is a random quality-assurance sample."
            steps = ["Review every signal and the claim details as you would for a suspicious case.",
                     "Record whether you agree with the likely-false-positive call."]
        else:
            action = "Close as a false positive after a quick check of the summary."
            steps = ["Confirm the claim details match the policy file.",
                     "Close with the reason code \"False positive: no supporting evidence\"."]

    uncertainties = [r["text"] for r in case["confidence"]["reasons"] if not r["positive"]]
    innocent = []
    if base_lane != "likely_fp":
        for fam in p["active"][:2]:
            if (fam["strength"] or 0) >= 0.2:
                innocent.extend(fam["benign"])

    return {
        "summary": summary,
        "key_indicators": indicators,
        "recommended_action": action,
        "next_steps": steps,
        "uncertainties": uncertainties,
        "innocent_explanations": innocent,
        "agrees_with_engine": True,
        "disagreement_reason": "",
    }


# --------------------------------------------------------------------------------------
# Offline question answering
# --------------------------------------------------------------------------------------

SIGNAL_KEYWORDS = {
    "duplicate_service_billed": ["duplicate", "double bill", "billed twice", "double-bill"],
    "service_overlap_other_provider": ["overlap", "another provider", "other provider", "second provider"],
    "weekly_visit_frequency": ["visit", "frequency", "how often"],
    "member_provider_distance_miles": ["distance", "miles", "how far", "far away", "travel"],
    "weekend_billing_ratio": ["weekend", "saturday", "sunday"],
    "shared_contact_with_provider": ["contact", "phone", "address", "email", "shared"],
    "amount_vs_peer_avg_pct": ["peer average", "overcharg", "rates", "above average", "vs peer", "amount vs"],
    "round_dollar_billing_ratio": ["round"],
    "recent_policy_change_flag": ["policy change", "policy", "rider", "benefit increase"],
    "prior_claims_last_12mo": ["prior claim", "previous claim", "past claim", "claim history", "prior"],
}

SUGGESTIONS = [
    "Why is this case rated this way?",
    "Which innocent explanations should I rule out first?",
    "How do the red flags combine?",
    "What should I do next?",
    "What would change this assessment?",
    "What does the distance tell us?",
    "How does this compare with the rest of the queue?",
    "Are there linked or similar cases?",
    "How confident is the assessment?",
    "How much money is at stake?",
]


def _has(q: str, words: list[str]) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(w), q) for w in words)


def _cites(text: str) -> list[str]:
    seen: list[str] = []
    for m in re.findall(r"\[(E\d+)\]", text):
        if m not in seen:
            seen.append(m)
    return seen


def answer_offline(case: dict, question: str) -> dict:
    q = question.lower().strip()
    p = _parts(case)
    f = case["facts"]
    lane_label = case["lane_label"].lower()
    mentioned = [k for k, words in SIGNAL_KEYWORDS.items() if _has(q, words)]

    if _has(q, ["rule out", "innocent", "counter-argument", "counter argument", "counterargument", "explain away"]):
        text = _answer_rule_out(case)
    elif _has(q, ["combine", "together", "contradict", "correlat", "relate to each other", "interact"]):
        text = _answer_combine(case)
    elif _has(q, ["link", "claim number", "related", "same claim", "similar", "precedent", "other cases like",
                "cases like this"]):
        text = _answer_links(case)
    elif _has(q, ["what would change", "change the", "change this", "lower", "downgrade", "innocent", "benign",
                  "legitimate", "explain away", "raise", "upgrade", "what would it take"]):
        text = _answer_counterfactual(case, p)
    elif _has(q, ["next", "should i", "what do i do", "recommend", "action", "steps", "how do i verify",
                  "what to do", "how to proceed"]):
        text = _answer_next(case)
    elif _has(q, ["confiden", "how sure", "certain", "uncertain", "trust", "reliable"]):
        text = _answer_confidence(case)
    elif mentioned and len(mentioned) <= 3:
        text = "\n\n".join(_answer_signal(case, p["sig"][k]) for k in mentioned)
    elif _has(q, ["compare", "peer", "typical", "normal", "percentile", "queue", "others", "rest of"]):
        text = _answer_compare(case, p)
    elif _has(q, ["amount", "dollar", "exposure", "money", "cost", "how much"]):
        text = (
            f"The claim amount is ${f['claim_amount_usd']:,} [{CLAIM_AMOUNT_ID}]. Multiplied by the risk score of "
            f"{case['risk_score']}, that is about ${case['dollars_at_risk']:,.0f} of dollars at risk, which the queue "
            "uses to order cases within each risk rating."
        )
    elif _has(q, ["summary", "summar", "tl;dr", "tldr", "overview", "in short"]):
        text = offline_narrative(case)["summary"]
    elif _has(q, ["why", "risk", "score", "suspicious", "explain", "driver", "reason", "rated", "flag"]):
        text = _answer_why(case, p, lane_label)
    else:
        text = (
            "I'm running in offline mode, so I answer from the case data with fixed reasoning patterns. "
            "I can explain the risk drivers, any single signal, peer comparisons, linked or similar cases, "
            "what would change the assessment, confidence, and next steps. Connect a language model "
            "(see the README) for open-ended questions."
        )
    return {"answer": text, "citations": _cites(text), "mode": "offline"}


def _answer_rule_out(case: dict) -> str:
    from .evidence import build_evidence

    flags = [e for e in build_evidence(case) if e.get("reading") == "red flag"]
    if not flags:
        return "No signal is above its expected range, so there is nothing to explain away. Nothing in this file argues against closing it."
    lines = ["Rule these out first, one per red flag, with the record that settles each:"]
    for e in flags:
        lines.append(f"- [{e['id']}] {e['label']}: {e['counter_arguments'][0]} Settled by: {e['verify']}")
    return "\n".join(lines)


def _answer_combine(case: dict) -> str:
    from .evidence import evidence_pack

    pairs = evidence_pack(case)["engine"]["column_logic_for_red_flag_pairs"]
    if not pairs:
        return "No two red flags on this case are linked by a shared mechanism or a contradiction, so each one is judged on its own."
    clash = [p for p in pairs if "cut against" in p["relation"]]
    agree = [p for p in pairs if p not in clash]
    lines = []
    if clash:
        lines.append("These red flags contradict each other in legitimate care, so each pair is a question to resolve first:")
        lines += [f"- [{a}] and [{b}]: {p['both_high_means']} {p['why']}" for p in clash for a, b in [p["evidence_ids"]]]
    if agree:
        lines.append("These red flags share a mechanism, so together they describe one pattern and count less than separate problems would:")
        lines += [f"- [{a}] and [{b}]: {p['both_high_means']}" for p in agree for a, b in [p["evidence_ids"]]]
    return "\n".join(lines)


def _answer_why(case: dict, p: dict, lane_label: str) -> str:
    if not p["active"]:
        return (
            f"The case is rated {lane_label} with a risk score of {case['risk_score']} out of 100 because none of the "
            "five evidence families is active. The upstream duplicate [E1], overlap [E2], and shared-contact [E6] "
            "flags are not set, and visit frequency [E3] is normal."
            + (" It is held for review only because of a review rule: " + case["guardrails"][0]["text"]
               if case["lane"] != case["base_lane"] else "")
        )
    lines = [f"The risk score is {case['risk_score']} out of 100 ({lane_label}). Each active evidence family "
             "and how much the score would drop if it were ruled out:"]
    for fam in p["active"]:
        top = _strongest_in(fam, case)
        lines.append(f"- **{fam['short']}**: {phrase(top)} [{top['evidence_id']}]; explaining it would lower the "
                     f"score by about {fam['drop_if_explained']:.0f} points.")
    if p["clean"]:
        lines.append(f"Clean families: {oxford([c['short'].lower() for c in p['clean']])}.")
    lines.append("Correlated signals inside one family add only a small bonus, so the score reflects how many "
                 "independent lines of evidence agree.")
    return "\n".join(lines)


def _answer_signal(case: dict, s: dict) -> str:
    base = f"**{s['label']}** [{s['evidence_id']}]: {s['text']}"
    if s["level"] == "missing":
        return base
    pct = s["percentile"]
    extra = f" This is higher than about {pct:.0f}% of cases in today's queue." if s["kind"] == "continuous" else ""
    innocent = ""
    if s["level"] != "normal":
        innocent = f" A possible innocent explanation: {SIGNAL_BY_KEY[s['key']].innocent[0].lower()}{SIGNAL_BY_KEY[s['key']].innocent[1:]}"
    definition = f" {s['meaning']}" if s["level"] != "normal" else ""
    return f"{base}{extra}{definition}{innocent}"


def _answer_compare(case: dict, p: dict) -> str:
    rows = [s for s in case["signals"] if s["kind"] == "continuous" and s["level"] != "missing"]
    rows.sort(key=lambda s: -(s["percentile"] or 0))
    lines = ["Where this case sits in today's queue (percentile 100 is the highest):"]
    for s in rows:
        lines.append(f"- {s['label']}: {s['display']}, higher than about {s['percentile']:.0f}% of cases [{s['evidence_id']}]")
    flags = [s for s in case["signals"] if s["kind"] == "binary" and s["level"] == "high"]
    if flags:
        lines.append("Binary flags raised: " + oxford([f"{s['label'].lower()} [{s['evidence_id']}]" for s in flags]) + ".")
    return "\n".join(lines)


def _answer_links(case: dict) -> str:
    lines = []
    if case["linked"]:
        for link in case["linked"]:
            eid = link_evidence_id(case, link["case_id"])
            lines.append(
                f"{link['reason']}: {link['case_id']} [{eid}] is a {link['care_type']} claim in {link['state']} "
                f"dated {link['claim_date']} with risk score {link['risk_score']}. Claim numbers should be unique, "
                "so confirm whether this is a keying error or a reused number."
            )
    else:
        lines.append("No other case in the queue shares this claim number.")
    if case["similar"]:
        lines.append("Most similar cases by signal profile: " + oxford(
            [f"{s['case_id']} ({s['care_type']}, risk {s['risk_score']}, {LANES[s['base_lane']]['label'].lower()}) "
             f"[{similar_evidence_id(case, s['case_id'])}]" for s in case["similar"]])
            + ". Their decisions appear in the Similar cases panel once investigators record them.")
    return "\n\n".join(lines)


def _answer_counterfactual(case: dict, p: dict) -> str:
    cf = case["counterfactual"]
    lines = []
    fam_ids = {f["key"]: "".join(f"[{e}]" for e in f["signals"] if _sig_active(case, e)) for f in case["families"]}
    sig_ids = {s["key"]: s["evidence_id"] for s in case["signals"]}
    for step in cf["lower"]:
        cites = "".join(fam_ids.get(k, "") for k in step["family_keys"])
        lines.append(
            f"- If {oxford([x.lower() for x in step['families']])} were ruled out {cites}, the score would fall to "
            f"{step['risk_after']} ({LANES[step['lane_after']]['label'].lower()})."
        )
    for r in cf["raise"]:
        lines.append(
            f"- If {r['label'].lower()} [{sig_ids[r['signal']]}] changed to {r['to']}, the score would rise to "
            f"{r['risk_after']} ({LANES[r['lane_after']]['label'].lower()})."
        )
    if not lines:
        lines.append("- No single change in the available signals would move this case to another risk rating.")
    innocent = []
    for fam in p["active"][:2]:
        if (fam["strength"] or 0) >= 0.2:
            innocent.extend(fam["benign"])
    out = "What would change the assessment:\n" + "\n".join(lines)
    if innocent:
        out += "\n\nInnocent explanations worth ruling out:\n" + "\n".join(f"- {i}" for i in innocent)
    return out


def _sig_active(case: dict, evidence_id: str) -> bool:
    return any(s["evidence_id"] == evidence_id and s["level"] in ("elevated", "high") for s in case["signals"])


def _answer_next(case: dict) -> str:
    n = offline_narrative(case)
    basis = "".join(f"[{i['evidence_id']}]" for i in n["key_indicators"][:3])
    return (f"**Recommended:** {n['recommended_action']} This is based on {basis}.\n"
            + "\n".join(f"- {s}" for s in n["next_steps"]))


def _answer_confidence(case: dict) -> str:
    c = case["confidence"]
    lines = [f"Confidence in the risk rating is **{c['level']}**."]
    for r in c["reasons"]:
        lines.append(f"- {'Supports' if r['positive'] else 'Lowers'}: {r['text']}")
    lines.append(f"The independent outlier check places the case at the {ordinal(case['anomaly']['percentile'])} "
                 f"percentile of today's queue [{ANOMALY_ID}].")
    return "\n".join(lines)
