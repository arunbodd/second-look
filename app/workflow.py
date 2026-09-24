"""Investigator workflow: a decision on the AI finding, and a case status, kept separate.

The decision answers "do I agree with the AI's lane?"; the status answers "where is this case in
my work?". Both are derived from the append-only event log, so every change stays auditable.

Decision: pending -> accept | reject (needs a lane and a reason) | needs_evidence; reset returns
          a decided case to pending.
Status:   new, in_review, documentation_requested, escalated, closed_benign, closed_confirmed.
"""

from __future__ import annotations

DECISIONS = {
    "pending": "Pending review",
    "accept": "Accepted AI finding",
    "reject": "Rejected AI finding",
    "needs_evidence": "Needs more evidence",
}
STATUSES = {
    "new": "New",
    "in_review": "In review",
    "documentation_requested": "Documentation requested",
    "escalated": "Escalated",
    "closed_benign": "Closed (benign)",
    "closed_confirmed": "Closed (confirmed concern)",
}
CLOSED = {"closed_benign", "closed_confirmed"}
REJECT_REASONS = [
    "Benign explanation confirmed",
    "Signal is a data error",
    "Additional evidence found",
    "Pattern known to the team",
    "Other (see note)",
]
DECISION_ACTIONS = {"accept", "reject", "needs_evidence", "reset"}

# Events written before the decision/status split used one "action" field.
_LEGACY_STATUS = {"closed_fp": "closed_benign", "verifying": "in_review", "investigating": "in_review",
                  "escalated": "escalated", "pending_info": "documentation_requested", "open": "new"}


def status_for_lane(lane: str | None) -> str:
    return "closed_benign" if lane == "likely_fp" else "in_review"


def empty_state() -> dict:
    return {"decision": "pending", "status": "new", "final_lane": None, "reason_code": None}


def apply_event(state: dict, etype: str, p: dict) -> None:
    """Fold one decision/status event (current or legacy format) into ``state`` in place."""
    if etype == "status":
        state["status"] = p["status"]
        return
    if etype != "decision":
        return
    if "decision" in p:  # current format
        d = p["decision"]
        if d == "reset":
            state.update(empty_state())
        else:
            state["decision"] = d
            state["final_lane"] = p.get("final_lane")
            state["reason_code"] = p.get("reason_code")
        state["status"] = p.get("status_after", state["status"])
        return
    action = p.get("action")  # legacy format
    status = _LEGACY_STATUS.get(p.get("status_after"), state["status"])
    if action == "accept":
        state.update(decision="accept", final_lane=p.get("final_lane"), reason_code=None, status=status)
    elif action == "override":
        state.update(decision="reject", final_lane=p.get("final_lane"), reason_code=p.get("reason_code"), status=status)
    elif action == "request_info":
        state.update(decision="needs_evidence", status="documentation_requested")
    elif action == "escalate":
        state["status"] = "escalated"
    elif action == "resolve":
        state.update(final_lane=p.get("final_lane") or state["final_lane"], status=status)
    elif action == "reopen":
        state.update(empty_state())


def plan_decision(state: dict, decision: str, ai_lane: str, lane: str | None, reason_code: str | None,
                  lanes: dict) -> dict:
    """Validate a decision and return the event payload fields (raises ValueError when not allowed)."""
    if decision not in DECISION_ACTIONS:
        raise ValueError(f"Unknown decision '{decision}'. Use accept, reject, needs_evidence, or reset.")
    current = state["decision"]
    if decision == "reset":
        if current == "pending" and state["status"] == "new":
            raise ValueError("There is no decision to reset.")
        return {"decision": "reset", "final_lane": None, "reason_code": None, "status_after": "new"}
    if current in ("accept", "reject"):
        raise ValueError(f"The AI finding was already {DECISIONS[current].split()[0].lower()}ed. Reset the decision to change it.")
    if decision == "accept":
        return {"decision": "accept", "final_lane": ai_lane, "reason_code": None, "status_after": status_for_lane(ai_lane)}
    if decision == "reject":
        if lane not in lanes:
            raise ValueError("Rejecting the AI finding needs the risk rating you would give the case.")
        if lane == ai_lane:
            raise ValueError("That is the AI's own risk rating; accept the finding instead.")
        if reason_code not in REJECT_REASONS:
            raise ValueError("Rejecting the AI finding needs one of the listed reasons.")
        return {"decision": "reject", "final_lane": lane, "reason_code": reason_code, "status_after": status_for_lane(lane)}
    return {"decision": "needs_evidence", "final_lane": None, "reason_code": None, "status_after": "documentation_requested"}


def plan_status(state: dict, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"Unknown status '{status}'.")
    if status == state["status"]:
        raise ValueError(f"The case is already '{STATUSES[status]}'.")
    if status in CLOSED and state["decision"] == "pending":
        raise ValueError("Record a decision on the AI finding before closing the case.")
    return {"status": status, "status_before": state["status"]}


def event_summary(etype: str, p: dict, lanes: dict) -> str:
    if etype == "status":
        return f"Status: {STATUSES.get(p['status'], p['status'])}"
    if "decision" in p:
        d = p["decision"]
        if d == "reset":
            return "Decision reset: back to pending review"
        s = DECISIONS[d]
        if d == "reject" and p.get("final_lane"):
            s += f" (risk rating changed from {lanes[p['ai_lane']]['label']} to {lanes[p['final_lane']]['label']}; reason: {p.get('reason_code')})"
        return s + f" · status {STATUSES.get(p.get('status_after'), '')}"
    legacy = {"accept": "Accepted AI finding", "override": "Rejected AI finding", "escalate": "Escalated",
              "request_info": "Needs more evidence", "reopen": "Decision reset", "resolve": "Verification resolved"}
    return legacy.get(p.get("action"), "Decision")
