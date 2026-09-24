"""Reasoning attached to every evidence item.

A figure on its own ("84 mi") is not evidence of anything. It becomes evidence only under an
assumption about what legitimate care looks like, and it stays evidence only while no
counter-argument explains it. So every item in the evidence pack carries:

  * points_to:        what a red flag here would be evidence toward (the scheme or question)
  * assumption:       the assumption under which the value is read as normal or unusual
  * counter_arguments: innocent explanations an investigator must rule out before relying on it
  * verify:           the record that would confirm or clear it
  * context:          optional, a reading that combines this value with related values

All of these are business assumptions, stated so a reviewer can disagree with them. They are
care-type aware where care is delivered differently (at home, at a day center, or in a facility).
"""

from __future__ import annotations

from dataclasses import dataclass, field

IN_HOME = {"In-Home Care", "Home Health Aide"}
DAY_CENTER = {"Adult Day Care"}
RESIDENTIAL = {"Assisted Living", "Skilled Nursing"}


@dataclass(frozen=True)
class Rationale:
    points_to: str
    assumption: str
    counters: tuple[str, ...]
    verify: str
    # care type -> (extra assumption sentence, extra counter-arguments)
    by_care: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)

    def for_care(self, care_type: str) -> tuple[str, list[str]]:
        extra, more = self.by_care.get(care_type, ("", ()))
        assumption = f"{self.assumption} {extra}".strip()
        return assumption, [*more, *self.counters]


def _each(care_types: set[str], extra: str, counters: tuple[str, ...] = ()) -> dict:
    return {ct: (extra, counters) for ct in care_types}


SIGNAL_RATIONALE: dict[str, Rationale] = {
    "duplicate_service_billed": Rationale(
        points_to="If confirmed, double billing by this provider: the insurer would pay twice for one unit of care. "
        "Until the service lines are pulled it is an unverified flag to check.",
        assumption="The flag counts as a duplicate only if the upstream engine matched the same member, provider, "
        "service, and date of service. Similar services for different members, or the same service on different "
        "days, are normal billing and would make the flag a false positive. The file does not show which rule the "
        "engine used or which lines it matched.",
        counters=(
            "The upstream rule matched similar services across different members or different dates, which is normal billing.",
            "A corrected claim was resubmitted without voiding the original.",
            "Two separate services on the same day share one billing code, for example a morning and an evening visit.",
        ),
        verify="The service lines of this claim (member, provider, service code, date of service) and the claim's "
        "adjustment and void history.",
    ),
    "service_overlap_other_provider": Rationale(
        points_to="If confirmed, the same care billed by two providers, or care billed by one of them without being "
        "delivered. Until the other claim is pulled it is an unverified flag to check.",
        assumption="The flag is read as a different provider billing this same member for dates that overlap this "
        "claim. The other claim is outside this file, and the file has no member or provider identifiers, so "
        "nothing in the 50 cases shows which claim overlaps.",
        counters=(
            "The overlapping claim is for a different kind of care on the same dates, for example a nurse visit alongside an aide.",
            "Two agencies split shifts during a documented care transition.",
            "The upstream match was on a different person with similar details.",
        ),
        verify="The other provider's claim for this member: its dates, times, and services, compared line by line "
        "with this claim.",
    ),
    "weekly_visit_frequency": Rationale(
        points_to="Care billed but not delivered: inflated or phantom visits.",
        assumption="Home-based long-term care is usually at most one visit a day, so more than 6 visits a week is "
        "unusual and 14 or more means two visits every day, weekends included. Visit counts are the easiest item "
        "to inflate on a timesheet.",
        counters=(
            "A care plan that calls for more than one visit a day, for example after a hospital discharge.",
            "Split shifts (morning and evening) billed as separate visits.",
        ),
        verify="The plan of care, the service authorization, and electronic visit verification (EVV) records.",
        by_care={
            **_each(DAY_CENTER, "Adult day care is attended on weekdays, so more than 5 attendance days a week already "
                    "implies weekend or double billing."),
            **_each(RESIDENTIAL, "For residential care the member lives at the facility and is billed every day, so "
                    "'visits' is read as billed service units: up to 14 a week (two units a day, such as nursing plus aide "
                    "care) is normal and 28 or more is strongly unusual.",
                    ("The facility bills separate units for distinct services (for example, nursing and therapy) on the same day.",)),
        },
    ),
    "member_provider_distance_miles": Rationale(
        points_to="Care billed but not delivered, because frequent visits across a long distance are physically "
        "implausible. A large distance can also indicate an agency recruiting members far outside its service area, "
        "or a provider billing under a member's policy without providing care.",
        assumption="The member receives care at the address of record, and the provider's location is where care is "
        "delivered from. Up to 30 miles (a typical agency service radius) is treated as normal and 150 miles or more "
        "as strongly unusual. Distance is only meaningful together with the weekly visit count.",
        counters=(
            "The member lives or stays closer to the care, for example with family, and the address of record was never updated.",
            "The provider bills from a head office while the caregiver lives near the member.",
            "The address of record is a mailing address, such as a PO box or a relative's address.",
        ),
        verify="The member's current residence, the caregiver's home base, and the EVV check-in locations for the billed visits.",
        by_care={
            **_each(IN_HOME, "For in-home care the caregiver travels to the member for every visit."),
            **_each(DAY_CENTER, "For adult day care the member travels to the center for every attendance day."),
            **_each(RESIDENTIAL, "For residential care the member lives at the facility, so distance is not scored: "
                    "it cannot show whether care was delivered. A large distance only means the address of record "
                    "differs from where the member lives.",
                    ("The member moved into the facility and the address of record still shows the former home.",)),
        },
    ),
    "weekend_billing_ratio": Rationale(
        points_to="Care billed but not delivered, concentrated on days when offices are closed and visits are least "
        "likely to be checked.",
        assumption="Care spread evenly across the week puts about 29% of services on weekends (two days of seven), "
        "so a clearly higher share is unusual.",
        counters=(
            "Scheduled weekend respite care while family caregivers are away.",
            "A seven-day care plan with extra weekend hours.",
        ),
        verify="The provider's operating calendar and the EVV records for the weekend dates.",
        by_care={
            **_each(DAY_CENTER, "Adult day centers mostly operate on weekdays, so the expected weekend share is at most "
                    "10%, and weekend billing can mean billing for days the center was closed.",
                    ("The center runs a weekend program, which its published operating calendar would show.",)),
            **_each(RESIDENTIAL, "Residential care is billed every day, so the weekend share should sit close to 29%; "
                    "a higher share means weekend days were billed more heavily than weekdays."),
        },
    ),
    "shared_contact_with_provider": Rationale(
        points_to="An undisclosed relationship between the member and the provider, such as a relative billing as an "
        "agency or a provider controlling the member's claims.",
        assumption="Independent parties rarely share a phone number, address, or email, so a match suggests the same "
        "household or one party managing the other's paperwork. The flag comes from the upstream engine, and the "
        "file does not say which detail matched.",
        counters=(
            "An approved paid family caregiver arrangement.",
            "A small agency whose owner's details appear on both records.",
            "A relative or care manager listed as the contact for both the member and the provider.",
        ),
        verify="Who owns the shared phone, address, or email, and whether a paid family caregiver agreement is on file.",
    ),
    "amount_vs_peer_avg_pct": Rationale(
        points_to="Overbilling, such as inflated rates or billing for a higher level of care than was given.",
        assumption="Claims for the same care type should cost roughly the same, so up to 20% above the peer average "
        "is normal and double the average is strongly unusual. The peer group is defined upstream and taken as correct.",
        counters=(
            "The member needs more intensive care than the peer group.",
            "Regional price differences, if the peer group spans several states.",
        ),
        verify="Contracted or usual-and-customary rates for this care type and state, and the level of care in the plan of care.",
    ),
    "round_dollar_billing_ratio": Rationale(
        points_to="Estimated or fabricated billing records.",
        assumption="Amounts calculated from real hours, rates, and supplies rarely come out to whole dollars, so a bill "
        "where most lines are round suggests amounts were typed in rather than calculated. Up to 25% round lines is "
        "normal and 75% or more is strongly unusual.",
        counters=("Flat daily or monthly rates, which produce round amounts by design.",),
        verify="An itemized statement with service times, and the provider's rate sheet.",
        by_care={
            **_each(RESIDENTIAL | DAY_CENTER, "This care type is often billed at a flat daily rate, which produces "
                    "round amounts by design, so this signal carries less weight here."),
        },
    ),
    "recent_policy_change_flag": Rationale(
        points_to="Opportunistic claiming: coverage was increased because a claim was expected.",
        assumption="A benefit increase shortly before a claim can mean the policyholder knew care was coming. The flag "
        "is assumed to mark a change the policyholder chose. The file does not say what changed or when.",
        counters=(
            "An automatic inflation-protection increase, which many long-term care policies apply every year.",
            "A change bought before any diagnosis that later required care.",
            "An administrative change, such as an address or payment update.",
        ),
        verify="The policy change history (what changed, who initiated it, and when) compared with the date care started.",
    ),
    "prior_claims_last_12mo": Rationale(
        points_to="A repeated pattern across claims, which makes a one-off billing error less likely.",
        assumption="More than 3 claims in 12 months is treated as unusual. This is the weakest assumption in the set, "
        "because members in ongoing care often claim monthly.",
        counters=(
            "Ongoing care billed monthly, which alone produces about 12 claims a year.",
            "A chronic condition that produces recurring legitimate claims.",
        ),
        verify="The outcomes of the prior claims, and whether this member's care is billed monthly, per visit, or per episode.",
        by_care={
            **_each(RESIDENTIAL, "Residential care is usually billed monthly, so for this care type up to 12 claims a "
                    "year is normal and 24 or more is strongly unusual."),
        },
    ),
}

CLAIM_AMOUNT_RATIONALE = Rationale(
    points_to="How much money is at stake. This is exposure, and it is never read as a sign of fraud.",
    assumption="The claim amount sets priority within a risk rating. It never makes a case more suspicious.",
    counters=("A large claim is consistent with legitimate intensive care.",),
    verify="The payment status and amount already paid on this claim.",
)

PROFILE_RATIONALE = Rationale(
    points_to="Context. The care type decides which expected ranges and assumptions apply to E1 to E10.",
    assumption="The care type describes how care is delivered (at home, at a day center, or in a facility). "
    "Claim numbers are assumed to be unique.",
    counters=("The care type may be coded differently from the care actually delivered.",),
    verify="The service authorization and the plan of care.",
)

ANOMALY_RATIONALE = Rationale(
    points_to="Whether this case is unusual compared with the rest of today's queue. It is a second opinion on the "
    "rules and never adds to the risk score.",
    assumption="Cases unlike the rest of the queue deserve a second look. The check does not use the expected ranges "
    "or the evidence questions, so agreement with the rules suggests the flag does not depend on those assumptions.",
    counters=(
        "A rare but legitimate care pattern also ranks as unusual.",
        "The baseline is one queue of cases; in production it would be fit on historical claims.",
    ),
    verify="Nothing to verify directly; it points the investigator back to E1 to E10.",
)

LINK_RATIONALE = Rationale(
    points_to="The same claim submitted more than once, possibly across states, or a data-quality error.",
    assumption="Claim numbers are unique, so two cases with one claim number are either one claim billed twice or a "
    "data error.",
    counters=("A data entry error or a reused claim number in the source system.",),
    verify="The claim system's record for this claim number, including member and provider on each submission.",
)

SIMILAR_RATIONALE = Rationale(
    points_to="Precedent: how comparable cases were decided, once investigators record decisions. It adds no new "
    "evidence about this case.",
    assumption="Cases with a similar pattern across the ten signals may be resolved in similar ways.",
    counters=(
        "Similarity uses the ten signal scores only; the cases share no member, provider, claim number, or date.",
        "Everything the resemblance shows is already in E1 to E10.",
        "Cases whose signals are all at the top of the scale, or all clear, look identical, so the same few cases recur as neighbors.",
    ),
    verify="The recorded decision on the comparable case, if any.",
)


def article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def travel_context(care_type: str, visits: float | None, miles: float | None, normal_miles: float) -> str | None:
    """Combine distance with visit count so the pack states what the distance implies."""
    if visits is None or miles is None or miles <= normal_miles:
        return None
    v, m = int(round(visits)), int(round(miles))
    weekly = int(round(v * m * 2, -2))
    if care_type in IN_HOME:
        return (f"At {v} visits a week and {m} miles each way, a caregiver traveling from the provider would drive "
                f"about {weekly:,} miles a week.")
    if care_type in DAY_CENTER:
        return (f"At {v} attendance days a week and {m} miles each way, the member would travel about "
                f"{weekly:,} miles a week to the center.")
    if care_type in RESIDENTIAL:
        return (f"For residential care the member should live at the facility, yet the address of record is {m} miles away.")
    return None
