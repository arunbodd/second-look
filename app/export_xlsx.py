"""Excel export of the queue, assessments, decisions, and score drivers.

Totals on the Summary sheet are formulas over the Queue sheet, so the workbook recalculates if an
analyst edits it. The capacity block has two editable inputs (blue text on yellow) and every
assumption is labelled where it is used.
"""

from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .engine.signals import LANES, SIGNAL_BY_KEY, SIGNAL_KEYS

FONT = "Arial"
INK = "17171A"
MUTED = "5C5A55"
RULE = "D9D5CB"
LANE_FILL = {"suspicious": "F6E4DF", "review": "F7ECD2", "likely_fp": "EFECE4"}

f_base = Font(name=FONT, size=10, color=INK)
f_bold = Font(name=FONT, size=10, bold=True, color=INK)
f_head = Font(name=FONT, size=10, bold=True, color="FFFFFF")
f_title = Font(name=FONT, size=16, bold=True, color=INK)
f_muted = Font(name=FONT, size=9, color=MUTED, italic=True)
f_input = Font(name=FONT, size=10, color="0000FF", bold=True)
fill_head = PatternFill("solid", fgColor=INK)
fill_input = PatternFill("solid", fgColor="FFFF00")
thin = Side(style="thin", color=RULE)
wrap = Alignment(wrap_text=True, vertical="top")


def _header(ws, row: int, labels: list[str]) -> None:
    for i, label in enumerate(labels, start=1):
        c = ws.cell(row, i, label)
        c.font, c.fill = f_head, fill_head
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _table(ws, name: str, ref: str) -> None:
    t = Table(displayName=name, ref=ref)
    t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=False)
    ws.add_table(t)


def build_workbook(svc, mode: str) -> bytes:
    view = svc.queue_view(mode)
    rows = view["cases"]
    analysis = svc.analysis(mode)
    wb = Workbook()
    wb.calculation.fullCalcOnLoad = True

    # ---- Queue ---------------------------------------------------------------------------------
    q = wb.active
    q.title = "Queue"
    undecided = sorted((r for r in rows if r["decision"] in ("pending", "needs_evidence") and not r["closed"]),
                       key=lambda r: (-r["dollars_at_risk"], r["case_id"]))
    priority = {r["case_id"]: i + 1 for i, r in enumerate(undecided)}
    order = sorted(rows, key=lambda r: (priority.get(r["case_id"], 10_000), -r["risk_score"], r["case_id"]))
    heads = ["Priority", "Case ID", "Claim number", "Claim date", "Care type", "State", "Claim amount ($)",
             "Risk score", "Risk rating", "Confidence", "Dollars at risk ($)", "Why (top drivers)", "Decision", "Status",
             "Final risk rating", "AI review", "Linked cases", "Review rules"]
    _header(q, 1, heads)
    for i, r in enumerate(order, start=2):
        vals = [priority.get(r["case_id"]), r["case_id"], r["claim_number"], r["claim_date"], r["care_type"], r["state"],
                r["claim_amount_usd"], r["risk_score"], LANES[r["lane"]]["label"], r["confidence"], None,
                ", ".join(r["drivers"]) or "No family active", r["decision_label"], r["status_label"],
                LANES[r["final_lane"]]["label"] if r["final_lane"] else "", _ai_label(r), ", ".join(r["linked"]),
                ", ".join(g.replace("_", " ") for g in r["guardrails"])]
        for j, v in enumerate(vals, start=1):
            c = q.cell(i, j, v)
            c.font = f_base
        q.cell(i, 11, f"=G{i}*H{i}/100").font = f_base
        q.cell(i, 7).number_format = q.cell(i, 11).number_format = "$#,##0;($#,##0);-"
    last = len(order) + 1
    for lane, color in LANE_FILL.items():
        q.conditional_formatting.add(f"A2:R{last}", FormulaRule(formula=[f'$I2="{LANES[lane]["label"]}"'],
                                                                 fill=PatternFill("solid", fgColor=color)))
    q.freeze_panes = "C2"
    _widths(q, [9, 9, 14, 11, 17, 7, 13, 9, 20, 11, 14, 34, 20, 24, 18, 12, 12, 26])
    _table(q, "Queue", f"A1:R{last}")

    # ---- Summary (formulas over Queue) ----------------------------------------------------------
    s = wb.create_sheet("Summary", 0)
    ds = analysis["dataset"]
    s["A1"] = "Second Look · queue summary"
    s["A1"].font = f_title
    s["A2"] = f"{ds['name']} · {len(rows)} cases · triaged {svc.triaged_at} · review mode: {'AI review' if mode == 'ai' else 'Without AI'} · exported {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    s["A2"].font = f_muted
    _header(s, 4, ["Risk rating", "Cases", "Claim amount ($)", "Dollars at risk ($)", "Share of claim dollars"])
    for i, lane in enumerate(LANES, start=5):
        lab = LANES[lane]["label"]
        s.cell(i, 1, lab).font = f_bold
        s.cell(i, 2, f'=COUNTIF(Queue!$I$2:$I${last},A{i})')
        s.cell(i, 3, f'=SUMIF(Queue!$I$2:$I${last},A{i},Queue!$G$2:$G${last})')
        s.cell(i, 4, f'=SUMIF(Queue!$I$2:$I${last},A{i},Queue!$K$2:$K${last})')
        s.cell(i, 5, f'=IFERROR(C{i}/$C$8,0)')
    s.cell(8, 1, "All cases").font = f_bold
    for col in "BCD":
        s[f"{col}8"] = f"=SUM({col}5:{col}7)"
    s["E8"] = "=IFERROR(C8/$C$8,0)"
    for r in range(5, 9):
        for col in "BCDE":
            s[f"{col}{r}"].font = f_bold if r == 8 else f_base
        s[f"C{r}"].number_format = s[f"D{r}"].number_format = "$#,##0;($#,##0);-"
        s[f"E{r}"].number_format = "0.0%"
    for col in "ABCDE":
        s[f"{col}8"].border = Border(top=thin)

    s["A11"] = "Today's capacity"
    s["A11"].font = f_bold
    s["A12"], s["B12"] = "Reviews planned (edit)", 8
    s["B12"].font, s["B12"].fill = f_input, fill_input
    s["A13"], s["B13"] = "Dollars at risk covered", f'=SUMIFS(Queue!$K$2:$K${last},Queue!$A$2:$A${last},"<="&B12,Queue!$A$2:$A${last},">0")'
    s["A14"], s["B14"] = "Share of flagged dollars at risk", f'=IFERROR(B13/(D5+D6),0)'
    s["B13"].number_format = "$#,##0;($#,##0);-"
    s["B14"].number_format = "0.0%"
    for r in range(12, 15):
        s[f"A{r}"].font = f_base
        if r >= 13:
            s[f"B{r}"].font = f_bold
    s["A15"] = "Cases are taken in Priority order: open cases by dollars at risk (claim amount multiplied by the risk score)."
    s["A15"].font = f_muted
    s["A15"].alignment = wrap
    s.merge_cells("A15:E16")

    s["A21"] = "Decisions and status"
    s["A21"].font = f_bold
    _header(s, 22, ["Decision", "Cases", "", "Status", "Cases"])
    for i, lab in enumerate(svc.meta()["decision_labels"].values(), start=23):
        s.cell(i, 1, lab).font = f_base
        s.cell(i, 2, f'=COUNTIF(Queue!$M$2:$M${last},A{i})').font = f_base
    for i, lab in enumerate(svc.meta()["status_labels"].values(), start=23):
        s.cell(i, 4, lab).font = f_base
        s.cell(i, 5, f'=COUNTIF(Queue!$N$2:$N${last},D{i})').font = f_base
    _widths(s, [44, 14, 18, 26, 20])

    # ---- Assessments ---------------------------------------------------------------------------
    a = wb.create_sheet("Assessments")
    _header(a, 1, ["Case ID", "Risk rating", "Written by", "Quality", "Summary", "Points to risk", "Could explain it",
                   "Can't tell from this file", "Recommended next action", "Verification steps"])
    for i, r in enumerate(order, start=2):
        v = svc.case_view(r["case_id"], mode)
        asm = v["assessment"]
        n = asm["narrative"] if asm else {}
        risk = [f"[{k['evidence_id']}] {k['statement']}" for k in n.get("key_indicators", []) if k.get("direction", "risk") == "risk"]
        mit = [f"[{k['evidence_id']}] {k['statement']}" for k in n.get("key_indicators", []) if k.get("direction") == "mitigating"]
        vals = [r["case_id"], LANES[r["lane"]]["label"], _writer(asm), asm["quality"]["verdict"] if asm else "not run",
                n.get("summary", ""), "\n".join(risk), "\n".join(mit + n.get("innocent_explanations", [])),
                "\n".join(v["case"]["data_gaps"]), n.get("recommended_action", ""),
                "\n".join(f"{k}. {step}" for k, step in enumerate(n.get("next_steps", []), start=1))]
        for j, val in enumerate(vals, start=1):
            c = a.cell(i, j, val)
            c.font, c.alignment = f_base, wrap
    a.freeze_panes = "B2"
    _widths(a, [9, 18, 18, 10, 60, 50, 50, 44, 44, 60])
    _table(a, "Assessments", f"A1:J{last}")

    # ---- Signals -------------------------------------------------------------------------------
    g = wb.create_sheet("Signals")
    _header(g, 1, ["Case ID", "Care type"] + [SIGNAL_BY_KEY[k].label for k in SIGNAL_KEYS] + ["Signals flagged"])
    for i, r in enumerate(order, start=2):
        case = svc.queue.cases[r["case_id"]]
        by_key = {x["key"]: x for x in case["signals"]}
        g.cell(i, 1, r["case_id"]).font = f_base
        g.cell(i, 2, r["care_type"]).font = f_base
        for j, k in enumerate(SIGNAL_KEYS, start=3):
            x = by_key[k]
            c = g.cell(i, j, x["value"])
            c.font = Font(name=FONT, size=10, color=INK, bold=x["level"] == "high")
            if x["level"] == "high":
                c.fill = PatternFill("solid", fgColor=LANE_FILL["suspicious"])
            elif x["level"] == "elevated":
                c.fill = PatternFill("solid", fgColor=LANE_FILL["review"])
        g.cell(i, 13, sum(1 for x in case["signals"] if x["level"] in ("elevated", "high"))).font = f_base
    g.cell(last + 2, 1, "Shading: red = strong, ochre = moderate, none = within the expected range for the care type.").font = f_muted
    g.freeze_panes = "C2"
    _widths(g, [9, 17] + [13] * 10 + [10])

    # ---- Score drivers -------------------------------------------------------------------------
    d = wb.create_sheet("Score drivers")
    _header(d, 1, ["Signal", "Family", "Flagged when", "Flagged in (cases)", "Moves with score (Spearman r)",
                   "Points added when flagged", "Ratings that change without it", "Not explained by the other nine",
                   "Key indicator in (cases)"])
    ki = analysis["key_indicators"]["counts"]
    for i, x in enumerate(analysis["signals"], start=2):
        vals = [x["label"], x["family"].replace("_", " "), x["cutoff"], x["fired"], x["corr_with_score"],
                x["avg_points_when_fired"], x["lane_changes"], x["unique_share"], ki.get(x["key"], 0)]
        for j, val in enumerate(vals, start=1):
            d.cell(i, j, val).font = f_base
        d.cell(i, 5).number_format = "0.00"
        d.cell(i, 6).number_format = "0.0"
        d.cell(i, 8).number_format = "0%"
    d.cell(13, 1, "Points added when flagged: the case's score minus its score with that one signal set to normal, averaged over "
                  "the cases where it was flagged. Ratings that change: the risk rating (split at 30 and 60) differs without the signal.").font = f_muted
    _widths(d, [30, 18, 44, 12, 14, 14, 14, 16, 14])

    # ---- Activity ------------------------------------------------------------------------------
    act = wb.create_sheet("Activity")
    _header(act, 1, ["Time (UTC)", "Case ID", "By", "Type", "What happened"])
    events = svc.store.events()
    rowi = 2
    for e in events:
        if e["type"] == "chat_judge":
            continue
        from .service import _event_summary

        for j, val in enumerate([e["ts"], e["case_id"], e["actor"], e["type"], _event_summary(e)], start=1):
            act.cell(rowi, j, val).font = f_base
        rowi += 1
    if rowi == 2:
        act.cell(2, 1, "No decisions, notes, or questions recorded yet.").font = f_muted
    act.freeze_panes = "A2"
    _widths(act, [22, 9, 18, 10, 90])

    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = ws.title not in ("Summary",)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _ai_label(r: dict) -> str:
    if r["assessment_status"] == "failed":
        return "failed"
    if r["assessment_status"] != "ready":
        return "not run"
    return {"llm": "model", "template": "template"}.get(r["source"], r["source"] or "") + (
        f" · {r['quality_verdict']}" if r.get("quality_verdict") else "")


def _writer(asm: dict | None) -> str:
    if not asm:
        return "not run"
    return asm["generator"] if asm["source"] == "llm" else "template"
