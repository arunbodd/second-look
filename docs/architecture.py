"""Generate docs/architecture.svg (and, with --png, docs/architecture.png via Playwright).

One figure, one claim: the engine owns every number, models only write words through a
two-layer quality gate, and people make every decision into an append-only log.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

W, H = 1600, 900
BG, SURF, SURF2 = "#f8f6f1", "#ffffff", "#efece4"
INK, MUTED, FAINT, RULE, ACCENT = "#17171a", "#5c5a55", "#8f8c85", "#d9d5cb", "#bf3f2c"
SERIF = "Newsreader, Georgia, 'Times New Roman', serif"
SANS = "'IBM Plex Sans', Helvetica, Arial, sans-serif"
MONO = "'IBM Plex Mono', Menlo, Consolas, monospace"

out: list[str] = []


def t(x, y, s, size=13, fam=SANS, fill=INK, weight=400, anchor="start", italic=False, ls=None):
    style = f' font-style="italic"' if italic else ""
    spacing = f' letter-spacing="{ls}"' if ls else ""
    out.append(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
               f'text-anchor="{anchor}"{style}{spacing}>{escape(s)}</text>')


def box(x, y, w, h, fill=SURF, stroke=RULE, sw=1, extra=""):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>')


def arrow(points, label=None, lx=None, ly=None, color=INK, anchor="middle", lsize=11.5, lfill=None):
    pts = " ".join(f"{x},{y}" for x, y in points)
    mid = "arrow-accent" if color == ACCENT else "arrow"
    out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" marker-end="url(#{mid})"/>')
    if label:
        t(lx, ly, label, size=lsize, fam=MONO, fill=lfill or (ACCENT if color == ACCENT else MUTED), anchor=anchor)


def column_head(x, n, title, stack):
    t(x, 128, f"{n}", size=12, fam=MONO, fill=FAINT)
    t(x + 22, 128, title, size=15, fam=SANS, weight=600)
    t(x, 148, stack, size=11.5, fam=MONO, fill=FAINT)


def card(x, y, w, h, title, lines, tech=None, stroke=RULE, title_fill=INK):
    box(x, y, w, h, stroke=stroke)
    t(x + 16, y + 26, title, size=14, weight=600, fill=title_fill)
    for i, line in enumerate(lines):
        t(x + 16, y + 48 + i * 19, line, size=12.5, fill=MUTED)
    if tech:
        t(x + w - 14, y + 26, tech, size=11, fam=MONO, fill=FAINT, anchor="end")


def build() -> str:
    out.clear()
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
               f'aria-label="Architecture of Second Look: a CSV is triaged by a deterministic engine into an evidence pack; '
               f'a writer model or template drafts the narrative, which passes rule checks and an LLM judge before publication; '
               f'investigators decide in the browser and every action is appended to an event log.">')
    out.append(f'''<defs>
<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{INK}"/></marker>
<marker id="arrow-accent" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{ACCENT}"/></marker>
</defs>''')
    box(0, 0, W, H, fill=BG, stroke="none")

    # ---- header
    t(40, 44, "ARCHITECTURE · SECOND LOOK 1.2", size=12, fam=MONO, fill=FAINT, ls="0.06em")
    t(40, 84, "The engine owns every number. Models only write words, through a quality gate. People decide.", size=28, fam=SERIF, weight=500)
    out.append(f'<line x1="40" y1="104" x2="{W - 40}" y2="104" stroke="{INK}" stroke-width="1.5"/>')

    # ---- column 1: ingest
    column_head(40, "1", "Ingest", "FastAPI · pandas")
    card(40, 300, 200, 150, "Case sheet", ["CSV upload or sample", "16 columns, up to 5,000", "Schema and value checks", "Own decision log per file"])

    # ---- column 2: deterministic engine
    ex, ew = 280, 320
    column_head(ex, "2", "Deterministic engine", "pandas · NumPy · scikit-learn · SciPy")
    box(ex, 170, ew, 430)
    steps = [
        ("Signal strengths", "0 to 1, care-type cut-offs"),
        ("Five evidence families", "strongest signal + corroboration"),
        ("Risk score and rating", "noisy-OR · ratings split at 30 and 60"),
        ("Second opinion", "Isolation Forest outlier check"),
        ("Safety rules", "linked claims · data gaps · QA sample"),
        ("Explain", "counterfactuals · similar cases · drivers"),
    ]
    for i, (a_, b_) in enumerate(steps):
        y = 196 + i * 66
        t(ex + 18, y + 4, f"{i + 1}", size=12, fam=MONO, fill=FAINT)
        t(ex + 42, y + 4, a_, size=14, weight=600)
        t(ex + 42, y + 24, b_, size=12.5, fill=MUTED)
        if i < len(steps) - 1:
            out.append(f'<line x1="{ex + 18}" y1="{y + 42}" x2="{ex + ew - 18}" y2="{y + 42}" stroke="{SURF2}" stroke-width="1"/>')
    box(ex, 624, ew, 56, fill=INK, stroke=INK)
    t(ex + 18, 648, "Evidence pack  E1 … En", size=15, fam=SANS, weight=600, fill=BG)
    t(ex + 18, 668, "stable IDs · the only facts a model uses", size=12, fam=MONO, fill="#cfcac0")
    out.append(f'<line x1="{ex + ew / 2}" y1="600" x2="{ex + ew / 2}" y2="620" stroke="{INK}" stroke-width="1.5" marker-end="url(#arrow)"/>')
    arrow([(240, 375), (ex - 4, 375)], "rows", 260, 366)

    # ---- column 3: narrative + quality gate
    gx, gw = 716, 346
    column_head(gx, "3", "Narrative + quality gate", "httpx adapters · any provider")
    out.append(f'<rect x="{gx}" y="196" width="{gw}" height="376" fill="none" stroke="{ACCENT}" stroke-width="1.5"/>')
    t(gx + gw - 12, 214, "QUALITY GATE", size=11, fam=MONO, fill=ACCENT, anchor="end", ls="0.08em")
    cx, cw = gx + 18, gw - 36
    card(cx, 222, cw, 84, "Writer", ["Rules review: template, no model", "AI review: GPT-6, Claude, Gemini, local"])
    card(cx, 330, cw, 64, "Layer 1 · rule checks", ["citations · figures in the pack · tone"])
    card(cx, 418, cw, 64, "Layer 2 · LLM-as-judge", ["5-criterion rubric · other model family"])
    card(cx, 506, cw, 56, "Publish", ["pass shows · flag warns · fail = template"])
    mid = cx + cw / 2
    for y0, lab in ((306, "draft JSON"), (394, "checked draft"), (482, "verdict recomputed")):
        arrow([(mid, y0), (mid, y0 + 20)])
        t(mid + 10, y0 + 15, lab, size=11.5, fam=MONO, fill=MUTED)
    # revise loop: the accent, because it is what makes the words trustworthy
    out.append(f'<polyline points="{cx},450 {gx + 7},450 {gx + 7},262 {cx - 4},262" fill="none" stroke="{ACCENT}" '
               f'stroke-width="1.5" marker-end="url(#arrow-accent)"/>')
    t(gx - 8, 350, "issues:", size=11.5, fam=MONO, fill=ACCENT, anchor="end")
    t(gx - 8, 366, "revise once", size=11.5, fam=MONO, fill=ACCENT, anchor="end")
    # evidence pack into the writer
    vx = ex + ew + 20
    arrow([(ex + ew, 652), (vx, 652), (vx, 240), (cx - 4, 240)])
    t(vx + 8, 620, "cites", size=11.5, fam=MONO, fill=MUTED)
    t(vx + 8, 636, "E-IDs", size=11.5, fam=MONO, fill=MUTED)
    # cache
    card(gx, 606, gw, 74, "Assessment cache", ["SQLite, keyed by pack + writer + judge", "+ prompt: never sent twice"])
    arrow([(mid, 562), (mid, 602)])
    t(mid + 10, 588, "store", size=11.5, fam=MONO, fill=MUTED)

    # ---- column 4: investigator workspace
    x4, w4 = 1150, 410
    column_head(x4, "4", "Investigator workspace", "vanilla JS + SVG · no build · strict CSP")
    box(x4, 170, w4, 380)
    t(x4 + 16, 196, "Browser", size=14, weight=600)
    items = [
        ("Morning brief", "capacity plan: N reviews → share of dollars"),
        ("Queue", "dollar strip · today's picks · plain-language why"),
        ("Case panel", "points to risk vs could explain it · data gaps"),
        ("Ask", "grounded answers with citations, judged"),
        ("Score drivers", "what each column does to the score"),
    ]
    for i, (a_, b_) in enumerate(items):
        y = 228 + i * 62
        t(x4 + 16, y, a_, size=13.5, weight=600)
        t(x4 + 16, y + 19, b_, size=12.5, fill=MUTED)
    card(x4, 572, w4, 60, "Decisions", ["accept · reject (rating, reason) · needs evidence · status"])
    card(x4, 652, w4, 60, "Event log", ["append-only · exports Excel with formulas, Markdown"])
    arrow([(x4 + 205, 550), (x4 + 205, 568)])
    t(x4 + 215, 564, "decides", size=11.5, fam=MONO, fill=MUTED)
    arrow([(x4 + 205, 632), (x4 + 205, 648)])
    t(x4 + 215, 645, "appends", size=11.5, fam=MONO, fill=MUTED)

    # engine -> workspace (no model needed)
    arrow([(ex + ew, 184), (x4 - 4, 184)], "scores · ratings · evidence (works with no model)", (ex + ew + x4) / 2, 176)
    # run review / ask -> writer, narrative -> workspace (straight, labelled in the gap)
    gap = (gx + gw + x4) / 2
    arrow([(x4, 262), (gx + gw - 18 + 4, 262)])
    t(gap, 254, "run review", size=11.5, fam=MONO, fill=MUTED, anchor="middle")
    t(gap, 280, "and ask", size=11.5, fam=MONO, fill=MUTED, anchor="middle")
    arrow([(gx + gw - 18, 534), (x4 - 4, 534)])
    t(gap, 526, "narrative", size=11.5, fam=MONO, fill=MUTED, anchor="middle")

    # ---- platform band
    out.append(f'<line x1="40" y1="736" x2="{W - 40}" y2="736" stroke="{INK}" stroke-width="1.5"/>')
    t(40, 762, "PLATFORM", size=11.5, fam=MONO, fill=FAINT, ls="0.08em")
    cells = [
        ("API", "FastAPI · Uvicorn", "Pydantic request models"),
        ("Transport", "http 8000", "https 8443 (self-signed)"),
        ("Ship", "Docker, non-root", "/healthz · /readyz"),
        ("Protect", "strict CSP · headers", "request IDs · input limits"),
        ("Test", "pytest · 78 tests", "GitHub Actions CI"),
        ("Export", "Excel via openpyxl", "Markdown case file"),
    ]
    cw6 = (W - 80) / len(cells)
    for i, (a_, b_, c_) in enumerate(cells):
        x = 40 + i * cw6
        if i:
            out.append(f'<line x1="{x}" y1="776" x2="{x}" y2="850" stroke="{RULE}" stroke-width="1"/>')
        pad = 16 if i else 0
        t(x + pad, 796, a_, size=14, weight=600)
        t(x + pad, 818, b_, size=12, fam=MONO, fill=MUTED)
        t(x + pad, 837, c_, size=12, fam=MONO, fill=MUTED)
    t(40, 880, "Keys stay in .env on the server; the browser never sees them. Offline by default: with no key the rules review runs end to end.",
      size=12.5, fill=FAINT, italic=True)

    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    here = Path(__file__).parent
    svg = build()
    (here / "architecture.svg").write_text(svg)
    print("wrote", here / "architecture.svg")
    if "--png" in sys.argv:
        import asyncio

        from playwright.async_api import async_playwright

        html = ('<!doctype html><html><head><link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500'
                '&family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400&display=swap" rel="stylesheet">'
                f'<style>body{{margin:0;background:{BG}}}</style></head><body>{svg}</body></html>')

        async def shot():
            async with async_playwright() as p:
                b = await p.chromium.launch()
                pg = await b.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
                await pg.set_content(html, wait_until="networkidle")
                await pg.wait_for_timeout(600)
                await pg.locator("svg").screenshot(path=str(here / "architecture.png"))
                await b.close()

        asyncio.run(shot())
        print("wrote", here / "architecture.png")


if __name__ == "__main__":
    main()
