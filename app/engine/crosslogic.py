"""How the columns should relate to each other, logically, and whether the data agrees.

Correlation in the file says which columns move together. It does not say whether they
*should*. This module states, for every pair of signal columns (and the claim amount), the
business logic that links them or keeps them apart, what it means when both fire in one case,
and what it means when only one does. The analysis page then puts the observed correlation next
to that expectation, so a reviewer can see where the data behaves like real claims and where it
does not.

Relations:
  * together:  the same mechanism or scheme drives both, so they should rise together.
  * support:   different schemes that make each other more likely; a weak positive link at most.
  * independent: no business reason for them to move together.
  * tension:   in legitimate claims they should cut against each other, so seeing both high in one
               case is itself a contradiction to resolve.
"""

from __future__ import annotations

from dataclasses import dataclass

from .signals import SIGNAL_KEYS

E = {k: f"E{i}" for i, k in enumerate(SIGNAL_KEYS, start=1)}
E["claim_amount_usd"] = "E11"

RELATIONS = {
    "together": {"label": "Should rise together", "expect": "r of 0.3 or more"},
    "support": {"label": "Can support each other", "expect": "weak positive r, up to about 0.5"},
    "independent": {"label": "No logical link", "expect": "r near zero (between -0.3 and 0.3)"},
    "tension": {"label": "Should cut against each other", "expect": "r at or below zero"},
}


@dataclass(frozen=True)
class Pair:
    a: str
    b: str
    relation: str
    why: str
    both: str
    one: str


P = Pair
PAIRS: list[Pair] = [
    # ---- duplicate service billed ----------------------------------------------------------
    P("duplicate_service_billed", "service_overlap_other_provider", "together",
      "Both are ways of billing the same care twice: once by the same provider, once by a second provider.",
      "If both upstream flags are confirmed, this provider billed some care more than once and a second provider billed the same dates.",
      "Only one route of double billing is in use; the claim history of that provider is the place to look."),
    P("duplicate_service_billed", "weekly_visit_frequency", "together",
      "A repeated service line adds a visit, so duplicates push the billed visit count up.",
      "Part of the high visit count may be the duplicates themselves.",
      "A duplicate with a normal visit count is a one-off repeat; many visits with no duplicate are distinct visits that need their own proof."),
    P("duplicate_service_billed", "member_provider_distance_miles", "independent",
      "A duplicate is a billing event; distance is geography. Nothing links them mechanically.",
      "Two separate problems, one about billing and one about whether care could be delivered.",
      "Each is judged on its own."),
    P("duplicate_service_billed", "weekend_billing_ratio", "independent",
      "A repeated line can be dated on any day, so duplicates have no reason to fall on weekends.",
      "If the duplicates are the weekend lines, the weekend share is inflated by them; compare the dates.",
      "Each is judged on its own."),
    P("duplicate_service_billed", "shared_contact_with_provider", "support",
      "A member connected to the provider is less likely to dispute a repeated charge.",
      "If the duplicate is confirmed, a repeated charge that no independent member would have questioned.",
      "Expected; most duplicates come from ordinary billing errors."),
    P("duplicate_service_billed", "amount_vs_peer_avg_pct", "together",
      "A repeated service adds its cost to the claim, so the amount rises above peers.",
      "The duplicate may explain part of the excess over peers.",
      "A duplicate with a normal amount is small; an excess with no duplicate comes from price or volume."),
    P("duplicate_service_billed", "round_dollar_billing_ratio", "independent",
      "Copying a line keeps its amount, so duplicates do not make a bill rounder.",
      "Two separate billing problems on one claim.",
      "Each is judged on its own."),
    P("duplicate_service_billed", "recent_policy_change_flag", "independent",
      "The policy change is the policyholder's action; the duplicate is the provider's billing.",
      "Two separate questions, one for the member and one for the provider.",
      "Each is judged on its own."),
    P("duplicate_service_billed", "prior_claims_last_12mo", "support",
      "A provider that repeats services tends to do it on every claim, so more claims give more chances.",
      "A possible repeat pattern; check whether earlier claims had duplicates too.",
      "Each is judged on its own."),
    P("duplicate_service_billed", "claim_amount_usd", "together",
      "A repeated service adds dollars to the claim.",
      "The duplicate is part of the exposure.",
      "Each is judged on its own."),
    # ---- overlap with another provider -----------------------------------------------------
    P("service_overlap_other_provider", "weekly_visit_frequency", "together",
      "If both agencies bill the same days, this provider's count includes days another agency also covered.",
      "The extra visits may sit on the overlapping dates.",
      "Overlap with normal visits suggests a handover; many visits with no overlap are this provider's alone."),
    P("service_overlap_other_provider", "member_provider_distance_miles", "support",
      "When two providers bill the same dates, the distant one is the less likely to have delivered care.",
      "The distant provider is the first to verify.",
      "Each is judged on its own."),
    P("service_overlap_other_provider", "weekend_billing_ratio", "independent",
      "Overlap concerns who billed a date, weekend share concerns which dates were billed.",
      "Check whether the overlapping dates are the weekend dates.",
      "Each is judged on its own."),
    P("service_overlap_other_provider", "shared_contact_with_provider", "support",
      "A relative billing as a provider on top of an agency that actually delivers care produces exactly this pair.",
      "Possible family member billing alongside a real agency.",
      "Each is judged on its own."),
    P("service_overlap_other_provider", "amount_vs_peer_avg_pct", "independent",
      "The other provider's bill is a separate claim, so overlap does not raise this claim's amount by itself.",
      "Two separate problems.",
      "Each is judged on its own."),
    P("service_overlap_other_provider", "round_dollar_billing_ratio", "independent",
      "Overlap is about dates, round amounts are about how lines are priced.",
      "Two separate problems.", "Each is judged on its own."),
    P("service_overlap_other_provider", "recent_policy_change_flag", "independent",
      "Provider billing and the member's policy changes have no mechanical link.",
      "Two separate questions.", "Each is judged on its own."),
    P("service_overlap_other_provider", "prior_claims_last_12mo", "independent",
      "The number of past claims says nothing about who billed this one.",
      "Two separate questions.", "Each is judged on its own."),
    P("service_overlap_other_provider", "claim_amount_usd", "independent",
      "The overlapping bill is paid on another claim.",
      "Exposure is higher than this claim alone shows.", "Each is judged on its own."),
    # ---- weekly visits ---------------------------------------------------------------------
    P("weekly_visit_frequency", "member_provider_distance_miles", "tension",
      "A provider far from the member cannot visit often, so in legitimate care many visits go with short distances.",
      "The combination is physically implausible and is the core of the undelivered-care question.",
      "Either one alone has an ordinary explanation (a local caregiver visiting often, or a distant provider visiting rarely)."),
    P("weekly_visit_frequency", "weekend_billing_ratio", "support",
      "More visits in legitimate care means seven-day care, which keeps weekends near 29%; a share well above that means the extra visits were piled onto weekends.",
      "Visits padded onto the days least likely to be checked.",
      "Many visits at a normal weekend share fits a seven-day plan; a high weekend share with few visits fits weekend respite."),
    P("weekly_visit_frequency", "shared_contact_with_provider", "support",
      "A family caregiver can bill many hours with nobody independent checking them.",
      "Heavy billing by a connected party.", "Each is judged on its own."),
    P("weekly_visit_frequency", "amount_vs_peer_avg_pct", "together",
      "More visits cost more, so volume pushes the amount above peers.",
      "The excess over peers is at least partly volume.",
      "A high amount with normal visits means higher price per visit; many visits at a normal amount means low-priced visits."),
    P("weekly_visit_frequency", "round_dollar_billing_ratio", "independent",
      "How many visits and how each is priced are separate.",
      "Two separate problems.", "Each is judged on its own."),
    P("weekly_visit_frequency", "recent_policy_change_flag", "support",
      "A benefit increase followed by more visits is how a raised benefit gets used.",
      "Use of care rose with the new coverage; check the order of the two.", "Each is judged on its own."),
    P("weekly_visit_frequency", "prior_claims_last_12mo", "independent",
      "Prior claims count claims, visits count care within one claim.",
      "Two separate questions.", "Each is judged on its own."),
    P("weekly_visit_frequency", "claim_amount_usd", "together",
      "More visits mean a larger claim.",
      "Volume drives the exposure.", "A large claim with few visits means expensive visits."),
    # ---- distance --------------------------------------------------------------------------
    P("member_provider_distance_miles", "weekend_billing_ratio", "independent",
      "Distance is geography and weekend share is the calendar.",
      "Long weekend trips add to the travel implausibility.", "Each is judged on its own."),
    P("member_provider_distance_miles", "shared_contact_with_provider", "tension",
      "A shared address means the same household, so the distance should be near zero.",
      "A contradiction: one of the addresses on file is wrong, or the shared detail is a phone or email controlled by one party.",
      "Either alone is ordinary."),
    P("member_provider_distance_miles", "amount_vs_peer_avg_pct", "support",
      "Some providers bill travel, so long distances can add a little cost.",
      "Check whether mileage is billed.", "Each is judged on its own."),
    P("member_provider_distance_miles", "round_dollar_billing_ratio", "independent",
      "No link between geography and how lines are priced.", "Two separate problems.", "Each is judged on its own."),
    P("member_provider_distance_miles", "recent_policy_change_flag", "independent",
      "No direct link, with one exception: if the policy change was an address update, it can explain the distance.",
      "Check whether the policy change was the address of record changing.", "Each is judged on its own."),
    P("member_provider_distance_miles", "prior_claims_last_12mo", "independent",
      "Distance says nothing about how often the member claims.", "Two separate questions.", "Each is judged on its own."),
    P("member_provider_distance_miles", "claim_amount_usd", "independent",
      "Distance does not set the size of a claim.", "Two separate questions.", "Each is judged on its own."),
    # ---- weekend share ---------------------------------------------------------------------
    P("weekend_billing_ratio", "shared_contact_with_provider", "support",
      "Weekend care by a relative is common, and a connected party is not checked by office staff.",
      "Weekend hours billed by a connected party.", "Each is judged on its own."),
    P("weekend_billing_ratio", "amount_vs_peer_avg_pct", "support",
      "Weekend care is often billed at premium rates.",
      "Part of the excess may be weekend premiums.", "Each is judged on its own."),
    P("weekend_billing_ratio", "round_dollar_billing_ratio", "independent",
      "Which days are billed and how lines are priced are separate.", "Two separate problems.", "Each is judged on its own."),
    P("weekend_billing_ratio", "recent_policy_change_flag", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
    P("weekend_billing_ratio", "prior_claims_last_12mo", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
    P("weekend_billing_ratio", "claim_amount_usd", "independent",
      "The calendar does not set the size of the claim.", "Two separate questions.", "Each is judged on its own."),
    # ---- shared contact --------------------------------------------------------------------
    P("shared_contact_with_provider", "amount_vs_peer_avg_pct", "independent",
      "A connection says nothing about price.", "Two separate problems.", "Each is judged on its own."),
    P("shared_contact_with_provider", "round_dollar_billing_ratio", "support",
      "A connected party filling in timesheets by hand tends to write round numbers.",
      "Hand-entered records by a connected party.", "Each is judged on its own."),
    P("shared_contact_with_provider", "recent_policy_change_flag", "support",
      "A relative who manages the member's affairs can raise the benefit and then bill it.",
      "The same person may control both the policy and the billing.", "Each is judged on its own."),
    P("shared_contact_with_provider", "prior_claims_last_12mo", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
    P("shared_contact_with_provider", "claim_amount_usd", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
    # ---- peer amount -----------------------------------------------------------------------
    P("amount_vs_peer_avg_pct", "round_dollar_billing_ratio", "together",
      "Amounts typed in rather than calculated tend to be rounded up, so both point to the same billing scheme.",
      "Charges that are both high and round look entered by hand.",
      "High and precise suggests real but expensive care; round and normal suggests flat rates."),
    P("amount_vs_peer_avg_pct", "recent_policy_change_flag", "support",
      "A higher benefit makes a larger claim payable.",
      "The claim may be sized to the new benefit; check the benefit before and after.", "Each is judged on its own."),
    P("amount_vs_peer_avg_pct", "prior_claims_last_12mo", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
    P("amount_vs_peer_avg_pct", "claim_amount_usd", "together",
      "The peer gap is the claim amount compared with peers, so they move together by definition.",
      "Exposure and overbilling point the same way.", "A large claim close to peers is expensive care, not overbilling."),
    # ---- round dollars, timing -------------------------------------------------------------
    P("round_dollar_billing_ratio", "recent_policy_change_flag", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
    P("round_dollar_billing_ratio", "prior_claims_last_12mo", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
    P("round_dollar_billing_ratio", "claim_amount_usd", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
    P("recent_policy_change_flag", "prior_claims_last_12mo", "support",
      "Both answer the timing question: many claims before a benefit increase means care was already needed when coverage was raised.",
      "Coverage raised after care began; the change history decides whether it was chosen or automatic.",
      "A change with few prior claims means the first claim came soon after the change."),
    P("recent_policy_change_flag", "claim_amount_usd", "independent",
      "No direct link.", "Two separate questions.", "Each is judged on its own."),
    P("prior_claims_last_12mo", "claim_amount_usd", "independent",
      "No link.", "Two separate questions.", "Each is judged on its own."),
]

# The claim amount is not a signal and never enters the score, so its pairs are kept above for
# reference but left out of the column logic, the same as in the correlation matrix.
PAIRS = [p for p in PAIRS if p.a in SIGNAL_KEYS and p.b in SIGNAL_KEYS]
COLUMNS = list(SIGNAL_KEYS)
assert len(PAIRS) == len(COLUMNS) * (len(COLUMNS) - 1) // 2, len(PAIRS)
assert len({frozenset((p.a, p.b)) for p in PAIRS}) == len(PAIRS)


def verdict(relation: str, r: float | None) -> str:
    if r is None:
        return "Not enough data to test."
    if relation == "together":
        return "Matches the logic." if r >= 0.3 else "Weaker than the logic expects."
    if relation == "support":
        return "Matches the logic." if -0.1 <= r <= 0.5 else (
            "Stronger than the logic expects: something else drives both." if r > 0.5 else "Weaker than the logic expects.")
    if relation == "independent":
        return "Matches the logic." if abs(r) < 0.3 else (
            "Stronger than the logic expects: something else drives both." if r > 0 else "Opposite to what the logic expects.")
    return "Matches the logic." if r <= 0.1 else (
        "The contradiction is common here: cases with both high need the counter-argument checked first.")
