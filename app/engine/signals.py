"""Signal catalog: the single place where business assumptions about each signal live.

Every threshold here is an explicit, documented assumption. In production these would be
calibrated against investigator outcomes (see WRITEUP.md, "Assumptions").

Strength convention
-------------------
Each signal is converted to a strength between 0 and 1:
  * binary flags: 0 -> 0.0, 1 -> 1.0
  * continuous signals: linear ramp from ``normal_max`` (strength 0) to ``high_at`` (strength 1)
Values at or below ``normal_max`` are treated as normal. The ramp keeps mild deviations mild.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ENGINE_VERSION = "1.1.0"


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    kind: str  # "binary" | "continuous"
    family: str
    unit: str = ""
    normal_max: float | None = None
    high_at: float | None = None
    # Care-type specific overrides of (normal_max, high_at)
    overrides: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Care types for which the signal is shown but not scored (it does not measure what it claims there)
    unscored_care: tuple[str, ...] = ()
    triggered_text: str = ""
    normal_text: str = ""
    meaning: str = ""
    innocent: str = ""

    def thresholds(self, care_type: str) -> tuple[float, float]:
        if care_type in self.overrides:
            return self.overrides[care_type]
        return float(self.normal_max), float(self.high_at)


@dataclass(frozen=True)
class Family:
    key: str
    label: str
    short: str
    weight: float
    question: str
    steps: tuple[str, ...]
    benign: tuple[str, ...]
    risk_phrase: str = ""
    clean_phrase: str = ""
    needs: tuple[str, ...] = ()  # records that would confirm or clear this family, absent from the file


# Evidence families group correlated signals that point at the same underlying fraud scheme.
# Within a family, signals corroborate each other only weakly (they are redundant views of one
# pattern). Across families, evidence is treated as independent and combines more strongly.
FAMILIES: dict[str, Family] = {
    f.key: f
    for f in [
        Family(
            key="not_rendered",
            risk_phrase="signs that care was not delivered as billed",
            clean_phrase="undelivered care",
            label="Care may not have been delivered as billed",
            short="Care not delivered",
            needs=("Visit verification (EVV) or caregiver timesheets for the billed dates", "The plan of care and the service authorization"),
            weight=0.50,
            question="Was the billed care physically possible and actually delivered?",
            steps=(
                "Request electronic visit verification (EVV) records or caregiver timesheets for the billing period and compare them with the plan of care.",
                "Confirm how the caregiver covers the distance to the member for each billed visit.",
            ),
            benign=(
                "A care plan that legitimately requires more than one visit per day, for example after a hospital discharge.",
                "The member is temporarily staying with family far from the address of record.",
                "Scheduled weekend respite care.",
            ),
        ),
        Family(
            key="double_billing",
            risk_phrase="duplicate or overlapping billing",
            clean_phrase="duplicate or overlapping billing",
            label="Duplicate or overlapping billing",
            short="Double billing",
            needs=("Line-level dates of service for both claims", "Adjustment and void history for the claim"),
            weight=0.55,
            question="Was the same care billed twice, by this provider or by two providers?",
            steps=(
                "Pull the duplicate and overlapping claims and compare dates of service line by line.",
                "Ask each provider for an itemized bill covering the overlapping dates.",
            ),
            benign=(
                "A corrected claim was resubmitted without voiding the original.",
                "Two agencies split shifts during a documented care transition.",
            ),
        ),
        Family(
            key="relationship",
            risk_phrase="a possible member-provider connection",
            clean_phrase="a member-provider connection",
            label="Member and provider may be connected",
            short="Relationship conflict",
            needs=("Who owns the shared phone number, address, or email", "Whether a paid family caregiver arrangement is on file"),
            weight=0.45,
            question="Is there an undisclosed relationship between the member and the provider?",
            steps=(
                "Verify who owns the shared phone number, address, or email using public registries (NPI registry, state licensing lookup).",
                "Check the policy file for an approved paid family caregiver arrangement.",
            ),
            benign=(
                "An approved paid family caregiver arrangement.",
                "A small agency where the owner's contact details are listed for both parties.",
            ),
        ),
        Family(
            key="billing_anomaly",
            risk_phrase="unusual charges and billing records",
            clean_phrase="unusual charges",
            label="Charges and billing records look unusual",
            short="Billing anomalies",
            needs=("Contracted or usual-and-customary rates for this care type and state", "An itemized statement with service times"),
            weight=0.45,
            question="Are the charges consistent with peers and with real service records?",
            steps=(
                "Compare billed rates with contracted or usual-and-customary rates for this care type and state.",
                "Request an itemized statement and check whether round-dollar line items match actual service times.",
            ),
            benign=(
                "Higher-acuity care that justifies higher rates.",
                "Flat daily-rate billing, which produces round amounts by design.",
            ),
        ),
        Family(
            key="timing_history",
            risk_phrase="a concerning claim history or policy timing",
            clean_phrase="a concerning claim history or policy timing",
            label="Claim timing and history",
            short="Timing and history",
            needs=("What changed in the policy and when, against the claim onset date", "The outcomes of the prior claims"),
            weight=0.30,
            question="Does the claim history or a recent policy change suggest opportunistic claiming?",
            steps=(
                "Review the policy change history (benefit increases, rider changes) against the claim onset date.",
                "Review the member's prior claims from the last 12 months for the same pattern.",
            ),
            benign=(
                "A benefit increase purchased before a diagnosis that later required care.",
                "A chronic condition that produces recurring legitimate claims.",
            ),
        ),
    ]
}


SIGNALS: list[Signal] = [
    Signal(
        key="duplicate_service_billed",
        innocent="A corrected claim was resubmitted without voiding the original.",
        label="Duplicate service billed",
        kind="binary",
        family="double_billing",
        triggered_text="The upstream fraud engine flagged a duplicate service on this claim. The file does not show which lines it matched.",
        normal_text="The upstream fraud engine did not flag a duplicate service on this claim.",
        meaning="A yes/no flag set by the upstream fraud engine. A true duplicate is the same service, for the same member, from the same provider, on the same date of service, billed more than once. The file includes neither the service lines nor the rule the engine used, so the flag cannot be checked here.",
    ),
    Signal(
        key="service_overlap_other_provider",
        innocent="Two agencies split shifts during a documented care transition.",
        label="Overlap with another provider",
        kind="binary",
        family="double_billing",
        triggered_text="The upstream fraud engine flagged another provider's claim for this member with overlapping dates of service. That other claim is not in this file.",
        normal_text="The upstream fraud engine did not flag an overlapping claim from another provider.",
        meaning="A yes/no flag set by the upstream fraud engine: a different provider billed this same member for dates that overlap this claim. The file has no member or provider identifiers and does not include the other claim, so the overlap cannot be seen or checked here.",
    ),
    Signal(
        key="weekly_visit_frequency",
        innocent="A care plan that requires more than one visit per day, for example after a hospital discharge.",
        label="Weekly visit frequency",
        kind="continuous",
        family="not_rendered",
        unit="visits/wk",
        normal_max=6,
        high_at=14,
        # A resident is billed every day, often for more than one unit (nursing, therapy, aide care):
        # up to two billed units a day is normal, four or more a day is strongly unusual.
        overrides={"Assisted Living": (14, 28), "Skilled Nursing": (14, 28)},
        triggered_text="{v} visits per week were billed; the normal limit is {normal}.",
        normal_text="{v} visits per week is within the expected range (up to {normal}).",
        meaning="Billed visits per week. Fourteen or more means at least two visits every day of the week.",
    ),
    Signal(
        key="member_provider_distance_miles",
        innocent="The member is staying with family away from the address of record, or the provider bills from a head office.",
        label="Member to provider distance",
        kind="continuous",
        family="not_rendered",
        unit="mi",
        normal_max=30,
        high_at=150,
        # A resident lives at the facility, so distance says nothing about whether care was delivered.
        unscored_care=("Assisted Living", "Skilled Nursing"),
        triggered_text="The provider is {v} from the member; the normal limit is {normal} miles.",
        normal_text="The provider is {v} from the member, within the expected {normal} miles.",
        meaning="Distance between the member's address of record and the provider. Long distances make frequent visits implausible.",
    ),
    Signal(
        key="weekend_billing_ratio",
        innocent="Scheduled weekend respite care or a seven-day care plan.",
        label="Weekend billing ratio",
        kind="continuous",
        family="not_rendered",
        unit="share",
        normal_max=0.29,
        high_at=0.60,
        overrides={"Adult Day Care": (0.10, 0.45)},
        triggered_text="{v} of billed services fall on weekends; the normal limit for {care_type} is {normal}.",
        normal_text="{v} of billed services fall on weekends, within the expected {normal} for {care_type}.",
        meaning="Share of billed services dated on a Saturday or Sunday. Two of seven days is about 29%, so higher shares are unusual. Adult day care centers mostly operate on weekdays, so the normal limit is lower for that care type.",
    ),
    Signal(
        key="shared_contact_with_provider",
        innocent="An approved paid family caregiver, or a small agency that lists the owner's details for both parties.",
        label="Shared contact details",
        kind="binary",
        family="relationship",
        triggered_text="The member and provider share contact details (phone, address, or email).",
        normal_text="The member and provider do not share contact details.",
        meaning="The member and the provider list the same phone number, address, or email.",
    ),
    Signal(
        key="amount_vs_peer_avg_pct",
        innocent="Higher-acuity care that justifies higher rates.",
        label="Amount vs peer average",
        kind="continuous",
        family="billing_anomaly",
        unit="%",
        normal_max=20,
        high_at=100,
        triggered_text="The billed amount is {v} above the peer average; the normal limit is {normal}.",
        normal_text="The billed amount is {v} relative to the peer average, within the expected {normal}.",
        meaning="How far the claim amount sits above or below the average for comparable claims, as computed upstream.",
    ),
    Signal(
        key="round_dollar_billing_ratio",
        innocent="Flat daily-rate billing, which produces round amounts by design.",
        label="Round-dollar billing ratio",
        kind="continuous",
        family="billing_anomaly",
        unit="share",
        normal_max=0.25,
        high_at=0.75,
        triggered_text="{v} of line items are round-dollar amounts; the normal limit is {normal}.",
        normal_text="{v} of line items are round-dollar amounts, within the expected {normal}.",
        meaning="Share of line items billed in whole round amounts. Amounts derived from real timesheets are rarely round, so a high share can indicate estimated or fabricated records.",
    ),
    Signal(
        key="recent_policy_change_flag",
        innocent="A benefit increase purchased before a diagnosis that later required care.",
        label="Recent policy change",
        kind="binary",
        family="timing_history",
        triggered_text="The policy was changed shortly before this claim.",
        normal_text="There was no recent policy change before this claim.",
        meaning="The policy was modified (for example, a benefit increase) shortly before the claim.",
    ),
    Signal(
        key="prior_claims_last_12mo",
        innocent="A chronic condition that produces recurring legitimate claims.",
        label="Prior claims (12 months)",
        kind="continuous",
        family="timing_history",
        unit="claims",
        normal_max=3,
        high_at=10,
        # Residential care is usually billed monthly, so about 12 claims a year is normal.
        overrides={"Assisted Living": (12, 24), "Skilled Nursing": (12, 24)},
        triggered_text="{v} prior claims in the last 12 months; the normal limit is {normal}.",
        normal_text="{v} prior claims in the last 12 months, within the expected {normal}.",
        meaning="Number of claims filed by this member in the previous 12 months.",
    ),
]

SIGNAL_BY_KEY: dict[str, Signal] = {s.key: s for s in SIGNALS}
SIGNAL_KEYS: list[str] = [s.key for s in SIGNALS]

# What no case in this file can show, whatever its signals (stated once per case file).
ALWAYS_MISSING = "Member and provider identities, line items, and payment status are not in this file."

FACT_COLUMNS = ["case_id", "claim_number", "claim_date", "care_type", "claim_amount_usd", "state"]
REQUIRED_COLUMNS = FACT_COLUMNS + SIGNAL_KEYS

# Lane thresholds on the 0-100 risk score (see scoring.py)
LANE_LOW_MAX = 30  # below this: likely false positive
LANE_HIGH_MIN = 60  # at or above this: suspicious
# The four yes/no columns are upstream flags this file cannot verify. A case whose measured signals
# alone stay below the review line cannot be rated suspicious on those flags: its score is capped here.
FLAG_ONLY_CAP = 59

LANES = {
    "suspicious": {"label": "Suspicious", "order": 0},
    "review": {"label": "Needs review", "order": 1},
    "likely_fp": {"label": "Likely false positive", "order": 2},
}

# Fraction of likely-false-positive cases randomly held for full review to measure the
# false-negative rate of the triage itself (quality-assurance sampling).
QA_SAMPLE_RATE = 0.10


def ordinal(n: float) -> str:
    n = int(round(n))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fmt_value(signal: Signal, value: float | None, care_type: str = "") -> str:
    if value is None:
        return "missing"
    if signal.kind == "binary":
        return "Yes" if value >= 1 else "No"
    if signal.unit == "share":
        return f"{value * 100:.0f}%"
    if signal.unit == "%":
        return f"{value:+.0f}%"
    if signal.unit == "mi":
        return f"{value:.0f} mi"
    return f"{value:.0f}"


def fmt_threshold(signal: Signal, care_type: str) -> str:
    if signal.kind == "binary":
        return "No"
    normal, _ = signal.thresholds(care_type)
    if signal.unit == "share":
        return f"{normal * 100:.0f}%"
    if signal.unit == "%":
        return f"+{normal:.0f}%"
    return f"{normal:.0f}"
