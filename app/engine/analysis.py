"""Queue-level analysis of how the input columns drive the risk score.

Everything here is computed from the loaded dataset, so an uploaded file gets its own figures:

* per signal: how often it fires, how closely it moves with the score, how many points it adds
  (the case's score minus its score with that one signal set to normal), how many score-only
  lanes would change without it, how much of it the other nine signals already explain, and
  how often it is cited as a key indicator;
* the Spearman correlation matrix of the signals, the risk score, and the claim amount;
* the share of variance carried by one shared factor;
* the non-signal columns (amount, care type, state, date) against the score.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .crosslogic import E as EID, PAIRS, RELATIONS, verdict
from .scoring import lane_for, score_signals
from .signals import FAMILIES, SIGNAL_KEYS, SIGNALS, fmt_value

NEUTRAL = 0.0  # every signal has zero strength at 0 (binary flags off, continuous below their normal limit)


def _num(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _spearman(a: pd.Series, b: pd.Series) -> float | None:
    ok = a.notna() & b.notna()
    if ok.sum() < 5 or a[ok].nunique() < 2 or b[ok].nunique() < 2:
        return None
    return _num(a[ok].rank().corr(b[ok].rank()))


def _kruskal_p(groups: list[np.ndarray]) -> float | None:
    groups = [g for g in groups if len(g) >= 3]
    if len(groups) < 2:
        return None
    try:
        from scipy.stats import kruskal

        return _num(kruskal(*groups).pvalue)
    except Exception:  # identical values in every group, or scipy missing
        return None


def _cutoff(sig) -> str:
    """The rule that makes a signal fire, in words."""
    if sig.kind == "binary":
        return "Flag set to 1"
    lo, hi = sig.thresholds("")
    text = f"Above {fmt_value(sig, lo)}; full weight at {fmt_value(sig, hi)}"
    groups: dict[tuple, list[str]] = {}
    for care, th in sig.overrides.items():
        groups.setdefault(tuple(th), []).append(care)
    for (clo, chi), cares in groups.items():
        text += f" ({' and '.join(cares)}: above {fmt_value(sig, clo)})"
    if sig.unscored_care:
        text += f" (not scored for {' and '.join(sig.unscored_care)})"
    return text


def analyse(df: pd.DataFrame, cases: dict[str, dict], order: list[str]) -> dict:
    rows = df.set_index("case_id").loc[order].reset_index()
    risk = pd.Series([cases[c]["risk_score"] for c in order], dtype=float)
    base_scores = {}
    for _, r in rows.iterrows():
        base_scores[r["case_id"]] = score_signals(r.to_dict())[2]

    # ---- per-signal effect on the score (drop one signal, re-score with the engine) -------------
    signals = []
    ranks = rows[SIGNAL_KEYS].astype(float).rank()
    for sig in SIGNALS:
        k = sig.key
        drops, fired_drops, lane_changes, fired = [], [], [], 0
        for _, r in rows.iterrows():
            row = r.to_dict()
            if _num(row.get(k)) is None:
                continue
            strengths, _, full = score_signals(row)
            if strengths[k] and strengths[k] > 0:
                fired += 1
            without = score_signals(row, {k: NEUTRAL})[2]
            d = full - without
            drops.append(d)
            if strengths[k] and strengths[k] > 0:
                fired_drops.append(d)
            if lane_for(full) != lane_for(without):
                lane_changes.append(row["case_id"])
        known = len(drops)
        # how much of this signal the other nine explain (rank-linear R^2); needs enough rows
        unique = None
        others = [o for o in SIGNAL_KEYS if o != k]
        sub = ranks[[k] + others].dropna()
        if len(sub) > len(others) + 5 and sub[k].nunique() > 1:
            A = np.c_[np.ones(len(sub)), sub[others].values]
            y = sub[k].values
            b, *_ = np.linalg.lstsq(A, y, rcond=None)
            ss = ((y - y.mean()) ** 2).sum()
            unique = _num(((y - A @ b) ** 2).sum() / ss) if ss else None
        signals.append({
            "key": k,
            "label": sig.label,
            "family": sig.family,
            "kind": sig.kind,
            "fired_share": fired / known if known else None,
            "fired": fired,
            "known": known,
            "corr_with_score": _spearman(rows[k].astype(float), risk),
            "avg_points_all": float(np.mean(drops)) if drops else None,
            "avg_points_when_fired": float(np.mean(fired_drops)) if fired_drops else None,
            "max_points": float(max(drops)) if drops else None,
            "lane_changes": len(lane_changes),
            "lane_change_cases": lane_changes[:12],
            "unique_share": unique,
            "cutoff": _cutoff(sig),
        })

    # ---- families -------------------------------------------------------------------------------
    families = []
    flagged = [c for c in order if cases[c]["base_lane"] != "likely_fp"]
    for fk, fam in FAMILIES.items():
        pts = [next(f["drop_if_explained"] for f in cases[c]["families"] if f["key"] == fk) for c in flagged]
        active = sum(1 for c in order if fk in cases[c]["active_families"])
        families.append({"key": fk, "label": fam.short, "weight": fam.weight,
                         "active": active, "avg_points_flagged": float(np.mean(pts)) if pts else None})

    # ---- correlation matrix (signals, score, amount) -------------------------------------------
    # Signals only: the risk score is built from these columns (correlating them with it would be
    # circular) and the claim amount is not a signal; both are examined separately.
    mat_cols = list(SIGNAL_KEYS)
    mdf = rows[SIGNAL_KEYS].astype(float).copy()
    corr = []
    for a in mat_cols:
        corr.append([_spearman(mdf[a], mdf[b]) for b in mat_cols])
    off = [v for i, r in enumerate(corr[:10]) for j, v in enumerate(r[:10]) if i != j and v is not None]

    # ---- one shared factor? ----------------------------------------------------------------------
    pc1 = None
    z = ranks.dropna()
    if len(z) > 3:
        zs = (z - z.mean()) / z.std(ddof=0).replace(0, np.nan)
        zs = zs.dropna(axis=1)
        if zs.shape[1] >= 2:
            ev = np.linalg.eigvalsh(np.cov(zs.T.values))
            pc1 = _num(ev[-1] / ev.sum())

    # ---- columns that are not signals ------------------------------------------------------------
    amount = rows["claim_amount_usd"].astype(float)
    lanes = pd.Series([cases[c]["base_lane"] for c in order])
    by_care = rows.assign(risk=risk.values).groupby("care_type")["risk"]
    by_state = rows.assign(risk=risk.values).groupby("state")["risk"]
    dates = pd.to_datetime(rows["claim_date"], errors="coerce")
    claim_counts = rows["claim_number"].value_counts()
    context = [
        {"column": "claim_amount_usd", "in_score": "No. Sets dollars at risk and the order within a risk rating.",
         "stat": "Spearman r", "value": _spearman(amount, risk),
         "detail": {lane: _num(amount[lanes == lane].median()) for lane in ("likely_fp", "review", "suspicious")}},
        {"column": "care_type", "in_score": "Only to set care-specific limits (weekend share for adult day care; visits, prior claims, and distance for facility care).",
         "stat": "Kruskal–Wallis p", "value": _kruskal_p([g.values for _, g in by_care]),
         "detail": {k: _num(v) for k, v in by_care.median().items()}},
        {"column": "state", "in_score": "No. Filter and display only.",
         "stat": "Kruskal–Wallis p", "value": _kruskal_p([g.values for _, g in by_state]),
         "detail": {"states": int(rows["state"].nunique())}},
        {"column": "claim_date", "in_score": "No. Display only.",
         "stat": "Spearman r", "value": _spearman(dates.map(lambda d: d.toordinal() if pd.notna(d) else np.nan), risk),
         "detail": {"from": str(dates.min().date()) if dates.notna().any() else None,
                    "to": str(dates.max().date()) if dates.notna().any() else None}},
        {"column": "claim_number", "in_score": "Only to detect a number used on more than one case.",
         "stat": "Reused numbers", "value": int((claim_counts > 1).sum()),
         "detail": {"unique": int(claim_counts.size)}},
    ]

    # ---- logical cross-correlation: what each pair should do, and what it does here -------------
    labels = {s.key: s.label for s in SIGNALS} | {"claim_amount_usd": "Claim amount"}
    num = rows[SIGNAL_KEYS + ["claim_amount_usd"]].astype(float)
    flags = {k: [(cases[c]["signals"][i]["level"] in ("elevated", "high")) for c in order] for i, k in enumerate(SIGNAL_KEYS)}
    crosslogic = []
    for pr in [p for p in PAIRS if p.a in SIGNAL_KEYS and p.b in SIGNAL_KEYS]:
        r = _spearman(num[pr.a], num[pr.b])
        both = sum(1 for x, y in zip(flags.get(pr.a, []), flags.get(pr.b, [])) if x and y) if pr.b in flags else None
        crosslogic.append({
            "a": pr.a, "b": pr.b, "a_label": labels[pr.a], "b_label": labels[pr.b],
            "a_id": EID[pr.a], "b_id": EID[pr.b],
            "relation": pr.relation, "relation_label": RELATIONS[pr.relation]["label"],
            "expect": RELATIONS[pr.relation]["expect"],
            "why": pr.why, "both": pr.both, "one": pr.one,
            "r": r, "both_fired": both, "verdict": verdict(pr.relation, r),
        })

    scatter = [{"case_id": c, "amount": float(cases[c]["facts"]["claim_amount_usd"]), "risk": cases[c]["risk_score"],
                "lane": cases[c]["base_lane"]} for c in order]
    return {
        "cases": len(order),
        "signals": signals,
        "families": families,
        "matrix": {"columns": mat_cols, "values": corr},
        "signal_corr_range": [min(off), max(off)] if off else None,
        "pc1_share": pc1,
        "context": context,
        "scatter": scatter,
        "crosslogic": crosslogic,
    }


def key_indicator_counts(assessments: dict[str, dict], cases: dict[str, dict]) -> dict:
    """How often each signal is cited as a risk key indicator in the published assessments."""
    counts = {k: 0 for k in SIGNAL_KEYS}
    id_to_key = {}
    total = 0
    for cid, a in assessments.items():
        if cid not in cases or a.get("source") == "template_unavailable":
            continue
        total += 1
        for s in cases[cid]["signals"]:
            id_to_key[(cid, s["evidence_id"])] = s["key"]
        seen = set()
        for ind in (a.get("narrative") or {}).get("key_indicators") or []:
            key = id_to_key.get((cid, ind.get("evidence_id")))
            if key and ind.get("direction", "risk") == "risk" and key not in seen:
                counts[key] += 1
                seen.add(key)
    return {"assessments": total, "counts": counts}
