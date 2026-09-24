"""Combined readings: what each evidence item means once it is read together with the columns
it is correlated with.

A single column rarely means much alone. Distance matters because of visit count, weekend share
matters because of visit count and care type, a peer-amount gap matters because of visit volume
and duplicates, and so on. Every evidence item gets one reading that states those combinations in
plain figures, so an investigator (and the AI writer) can see the logic without inferring it.
"""

from __future__ import annotations

from .rationale import DAY_CENTER, IN_HOME, RESIDENTIAL
from .signals import SIGNAL_KEYS, ordinal

EID = {k: f"E{i}" for i, k in enumerate(SIGNAL_KEYS, start=1)}
FLAT_RATE = RESIDENTIAL | DAY_CENTER


def _f(x: float | None) -> float | None:
    return None if x is None else float(x)


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _money(x: float) -> str:
    return f"${x:,.0f}"


def _flag(levels: dict, key: str) -> bool:
    return levels.get(key) in ("elevated", "high")


def signal_readings(case: dict) -> dict[str, str]:
    """Return {signal key: combined reading} for every signal that has a value."""
    care = case["facts"]["care_type"]
    amount = float(case["facts"]["claim_amount_usd"])
    sig = {s["key"]: s for s in case["signals"]}
    val = {k: _f(s["value"]) for k, s in sig.items()}
    lvl = {k: s["level"] for k, s in sig.items()}
    ceil = {k: (s["thresholds"] or [None])[0] for k, s in sig.items()}
    out: dict[str, str] = {}

    d, o = val["duplicate_service_billed"], val["service_overlap_other_provider"]
    v, m = val["weekly_visit_frequency"], val["member_provider_distance_miles"]
    w, c = val["weekend_billing_ratio"], val["shared_contact_with_provider"]
    p, r = val["amount_vs_peer_avg_pct"], val["round_dollar_billing_ratio"]
    pc, n = val["recent_policy_change_flag"], val["prior_claims_last_12mo"]
    e = EID

    # ---- double billing: E1 and E2 read as a pair -------------------------------------
    if d is not None and o is not None:
        if d >= 1 and o >= 1:
            pair = (f"Both upstream flags are set: a duplicate on this claim ({e['duplicate_service_billed']}) and an "
                    f"overlapping claim from another provider ({e['service_overlap_other_provider']}). If both are "
                    "confirmed, this provider billed some care more than once and a second provider billed the same "
                    "dates. Neither flag can be checked in this file, so both are leads: this claim's service lines and "
                    "the other provider's claim come first.")
        elif d >= 1:
            pair = (f"Only the duplicate flag is set; no overlapping claim from another provider was flagged "
                    f"({e['service_overlap_other_provider']}). If confirmed, the repeat is inside this provider's own "
                    "bill, and an unvoided corrected claim is the first explanation to check.")
        elif o >= 1:
            pair = (f"Only the overlap flag is set; no duplicate was flagged on this claim "
                    f"({e['duplicate_service_billed']}). The question is between two providers, and the other "
                    "provider's claim, which is not in this file, is needed to answer it.")
        else:
            pair = (f"Neither upstream flag is set ({e['duplicate_service_billed']}, "
                    f"{e['service_overlap_other_provider']}), so the upstream engine found no sign of double billing.")
        extra = ""
        if d >= 1 and p is not None and p > (ceil["amount_vs_peer_avg_pct"] or 20):
            extra = (f" The claim is also {p:+.0f}% above the peer average ({e['amount_vs_peer_avg_pct']}); a "
                     "confirmed duplicate would explain part of that.")
        out["duplicate_service_billed"] = pair + extra
        if o >= 1 and v is not None and _flag(lvl, "weekly_visit_frequency"):
            out["service_overlap_other_provider"] = pair + (
                f" This provider also billed {v:.0f} visits a week ({e['weekly_visit_frequency']}); if the overlap is "
                "confirmed, the overlapping dates may be where the extra visits come from.")
        else:
            out["service_overlap_other_provider"] = pair

    # ---- visits: E3 with weekend share, distance, and care type -----------------------
    if v is not None:
        parts = []
        if care in DAY_CENTER:
            if v > 5:
                parts.append(f"A center open on weekdays offers 5 attendance days a week, so {v:.0f} billed days means "
                             f"{v - 5:.0f} more than weekdays allow.")
            else:
                parts.append(f"{v:.0f} attendance days fits a center open on weekdays.")
        elif care in RESIDENTIAL:
            parts.append(f"{v:.0f} billed units a week is about {v / 7:.1f} a day for a member who lives at the facility.")
        else:
            parts.append(f"{v:.0f} visits a week is about {v / 7:.1f} visits every day of the week.")
        if w is not None and _flag(lvl, "weekend_billing_ratio"):
            parts.append(f"With {_pct(w)} billed on weekends ({e['weekend_billing_ratio']}), about {v * w:.0f} of them "
                         "fall on Saturday and Sunday.")
        if m is not None and _flag(lvl, "member_provider_distance_miles") and care not in RESIDENTIAL:
            who = "the member's" if care in DAY_CENTER else "a caregiver's"
            parts.append(f"Each one means {who} {2 * m:.0f}-mile round trip ({e['member_provider_distance_miles']}).")
        if not _flag(lvl, "weekly_visit_frequency"):
            parts.append("Visit volume is within range, so on its own it does not support the question of undelivered care.")
        out["weekly_visit_frequency"] = " ".join(parts)

    # ---- distance: E4 with visits and care type ---------------------------------------
    if m is not None:
        normal = ceil["member_provider_distance_miles"] or 30
        if m > normal and v is not None and care not in RESIDENTIAL:
            weekly = int(round(v * m * 2, -2))
            if care in IN_HOME:
                s = (f"At {v:.0f} visits a week ({e['weekly_visit_frequency']}) and {m:.0f} miles each way, a caregiver "
                     f"traveling from the provider would drive about {weekly:,} miles a week.")
            elif care in DAY_CENTER:
                s = (f"At {v:.0f} attendance days a week ({e['weekly_visit_frequency']}) and {m:.0f} miles each way, the "
                     f"member would travel about {weekly:,} miles a week to the center.")
            else:
                s = (f"For residential care the member should live at the facility, yet the address of record is "
                     f"{m:.0f} miles away.")
            if c is not None and c >= 1:
                s += (f" The member and provider also share contact details ({e['shared_contact_with_provider']}), "
                      "which is hard to square with living this far apart.")
            out["member_provider_distance_miles"] = s
        elif care in RESIDENTIAL:
            out["member_provider_distance_miles"] = (
                f"A resident should live at the facility, yet the address of record is {m:.0f} miles away. That usually "
                "means a former home or a relative's address is still on file. Distance is not scored for residential "
                "care, so it adds nothing to the risk score.")
        else:
            out["member_provider_distance_miles"] = (
                f"At {m:.0f} miles, travel does not limit how often care can be delivered, so distance gives no support "
                "to the question of undelivered care.")

    # ---- weekend share: E5 with visits and care type ----------------------------------
    if w is not None:
        if not _flag(lvl, "weekend_billing_ratio"):
            s = (f"{_pct(w)} of services fall on weekends, within the expected {_pct(ceil['weekend_billing_ratio'] or 0.29)} "
                 f"for {care}, so the billing calendar gives no support to the question of undelivered care.")
        elif v is not None:
            wk_end_day = v * w / 2
            wk_day = v * (1 - w) / 5
            s = (f"Of {v:.0f} billed visits a week ({e['weekly_visit_frequency']}), about {v * w:.0f} fall on the weekend, "
                 f"which is about {wk_end_day:.1f} per weekend day against {wk_day:.1f} per weekday.")
            if wk_end_day > wk_day * 1.2:
                s += " Care is billed more heavily on the days offices are closed."
            if care in DAY_CENTER and w > (ceil["weekend_billing_ratio"] or 0.10):
                s += " An adult day center would have to be open on both weekend days to deliver this."
        else:
            s = f"{_pct(w)} of services fall on weekends."
        out["weekend_billing_ratio"] = s

    # ---- shared contact: E6 with distance ----------------------------------------------
    if c is not None:
        if c >= 1 and m is not None and m > (ceil["member_provider_distance_miles"] or 30):
            s = (f"The member and provider share contact details yet are recorded {m:.0f} miles apart "
                 f"({e['member_provider_distance_miles']}). If the shared detail is an address, one of the two addresses "
                 "on file is wrong; if it is a phone or email, one party may be managing the other's records.")
        elif c >= 1:
            s = (f"The provider is {m:.0f} miles from the member ({e['member_provider_distance_miles']}), which fits a "
                 "relative or a small local agency; the question is whether that arrangement is approved.") if m is not None \
                else "The shared detail needs an owner before it means anything."
        else:
            s = "No shared contact details, so this signal gives no sign of a hidden member-provider link."
        if c >= 1 and (d or 0) >= 1:
            s += f" The duplicate flag on this claim ({e['duplicate_service_billed']}) would matter more if that link is confirmed."
        out["shared_contact_with_provider"] = s

    # ---- peer amount: E7 with visit volume and duplicates -------------------------------
    if p is not None:
        peer = amount / (1 + p / 100) if p > -100 else None
        if peer is not None and p > 0:
            s = f"At {p:+.0f}% above peers, about {_money(amount - peer)} of the {_money(amount)} claim is above what a comparable claim would cost."
        elif peer is not None:
            s = f"At {p:+.0f}% against peers, this claim costs about {_money(peer - amount)} less than a comparable claim."
        else:
            s = ""
        if p > (ceil["amount_vs_peer_avg_pct"] or 20):
            if v is not None and _flag(lvl, "weekly_visit_frequency"):
                s += (f" Visit volume is also above range ({v:.0f} a week, {e['weekly_visit_frequency']}), so part of the "
                      "excess may be more care billed.")
            elif v is not None:
                s += f" Visits are within range ({e['weekly_visit_frequency']}), so the excess comes from higher charges per service."
            if (d or 0) >= 1:
                s += f" A duplicate, if the upstream flag ({e['duplicate_service_billed']}) is confirmed, would add to the amount as well."
        out["amount_vs_peer_avg_pct"] = s.strip()

    # ---- round-dollar share: E8 with peer amount and care type -------------------------
    if r is not None:
        rceil = ceil["round_dollar_billing_ratio"] or 0.25
        if r > rceil and p is not None and p > (ceil["amount_vs_peer_avg_pct"] or 20):
            s = (f"{_pct(r)} of lines are round and the claim is {p:+.0f}% above peers ({e['amount_vs_peer_avg_pct']}). "
                 "Charges that are both high and round look typed in; the itemized statement would confirm or clear that.")
        elif r > rceil:
            s = (f"{_pct(r)} of lines are round, but the amount is in line with peers ({e['amount_vs_peer_avg_pct']}), so "
                 "the round amounts are not inflating the bill.")
        else:
            s = "Most line amounts are not round, which fits amounts calculated from real service records."
        if care in FLAT_RATE and r > rceil:
            s += f" For {care}, flat daily rates make round lines expected, so this is the counter-argument to rule out first."
        out["round_dollar_billing_ratio"] = s

    # ---- timing: E9 and E10 read together ----------------------------------------------
    if pc is not None:
        if pc >= 1 and n is not None and n > 3:
            s = (f"The policy changed shortly before this claim, and the member already had {n:.0f} claims in the prior "
                 f"12 months ({e['prior_claims_last_12mo']}), so care was likely underway before the change. The change "
                 "history shows whether it was chosen or automatic.")
        elif pc >= 1:
            s = (f"The policy changed shortly before this claim and there were few prior claims ({n:.0f}, "
                 f"{e['prior_claims_last_12mo']}), so this may be the first claim after the change. The gap between the "
                 "change and the start of care is the key fact.") if n is not None else \
                "The gap between the change and the start of care is the key fact."
        else:
            s = "No recent policy change, so coverage timing raises no question."
        out["recent_policy_change_flag"] = s
    if n is not None:
        word = "claim" if round(n) == 1 else "claims"
        n_ceiling = ceil["prior_claims_last_12mo"] or 3
        if care in RESIDENTIAL:
            s = (f"{n:.0f} {word} in 12 months. Residential care is usually billed monthly, so up to {n_ceiling:.0f} a "
                 "year is normal for this care type.")
        elif n <= 3:
            s = (f"{n:.0f} {word} in 12 months is within the expected 3, so the claim history shows no repeated pattern.")
        elif n <= 12:
            s = (f"{n:.0f} {word} in 12 months. If this member's care is billed monthly, about 12 a year is normal and "
                 f"{n:.0f} fits that pattern; if care is billed per episode, more than 3 is unusual.")
        else:
            s = (f"{n:.0f} {word} in 12 months is more than the 12 a member billed monthly would file, so the extra "
                 "claims need an explanation either way.")
        if (pc or 0) >= 1 and n > 3:
            s += f" Together with a recent policy change ({e['recent_policy_change_flag']}), the order of the change and the claims is what matters."
        out["prior_claims_last_12mo"] = s
    return out


def amount_reading(case: dict) -> str:
    amount = float(case["facts"]["claim_amount_usd"])
    p = next((s["value"] for s in case["signals"] if s["key"] == "amount_vs_peer_avg_pct"), None)
    s = f"This is exposure at a risk score of {case['risk_score']} ({case['lane_label'].lower()})."
    if p is not None and p > -100:
        peer = amount / (1 + p / 100)
        if p > 0:
            s += f" About {_money(amount - peer)} of it sits above the peer level ({EID['amount_vs_peer_avg_pct']})."
    return s


def profile_reading(case: dict) -> str:
    care = case["facts"]["care_type"]
    if care in DAY_CENTER:
        return ("As adult day care: the member travels to the center, visits count attendance days (5 weekdays a week), "
                "the weekend ceiling is 10%, and round amounts carry less weight because of flat daily rates.")
    if care in RESIDENTIAL:
        return (f"As {care.lower()}: the member lives at the facility, so distance should be near zero, visits count "
                "billed units, care is billed every day, and round amounts carry less weight because of flat daily rates.")
    return (f"As {care.lower()}: the caregiver travels to the member for every visit, so distance and visit count are "
            "read together, and weekends are about 29% of an evenly spread week.")


def anomaly_reading(case: dict) -> str:
    flags = [s for s in case["signals"] if s["level"] in ("elevated", "high")]
    ids = ", ".join(EID[s["key"]] for s in flags) or "none"
    a = case["anomaly"]
    verdict = "Both methods point the same way." if a["agrees"] else "The two methods disagree, so a person should look."
    return (f"The rules give a risk score of {case['risk_score']} from {len(flags)} red flags ({ids}); the outlier check "
            f"puts the case at the {ordinal(a['percentile'])} percentile of the queue. {verdict}")


def link_reading(case: dict, link: dict) -> str:
    f = case["facts"]
    diffs = []
    if link["care_type"] != f["care_type"]:
        diffs.append(f"care type ({f['care_type']} here, {link['care_type']} there)")
    if link["state"] != f["state"]:
        diffs.append(f"state ({f['state']} here, {link['state']} there)")
    if link["claim_date"] != f["claim_date"]:
        diffs.append(f"date ({f['claim_date']} here, {link['claim_date']} there)")
    if diffs:
        s = "The two cases differ on " + "; ".join(diffs) + ", which fits a reused or mistyped claim number more than one claim submitted twice."
    else:
        s = "The two cases match on care type, state, and date, which fits one claim submitted twice."
    if abs(link["risk_score"] - case["risk_score"]) >= 30:
        s += f" Their risk scores are far apart ({case['risk_score']} and {link['risk_score']}), so the link has to be resolved before either is closed."
    return s


def similar_reading(case: dict, sim: dict) -> str:
    mine = {s["key"] for s in case["signals"] if s["level"] in ("elevated", "high")}
    theirs = set(sim.get("flags") or [])
    both = [EID[k] for k in SIGNAL_KEYS if k in mine & theirs]
    only_me = [EID[k] for k in SIGNAL_KEYS if k in mine - theirs]
    only_them = [EID[k] for k in SIGNAL_KEYS if k in theirs - mine]
    if not mine and not theirs:
        return "Neither case has any red flag, so the resemblance is only that both look clean."
    if mine == theirs:
        return (f"{sim['case_id']} has exactly the same red flags as this case ({', '.join(both)}), so the comparison "
                "adds nothing beyond E1 to E10.")
    s = f"Red flags in both: {', '.join(both) or 'none'}."
    if only_me:
        s += f" Only in this case: {', '.join(only_me)}."
    if only_them:
        s += f" Only in {sim['case_id']}: {', '.join(only_them)}."
    return s
