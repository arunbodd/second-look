# Second Look

AI case review for fraud investigators (the brief's "Junior AI Investigator").

An AI-first case review tool for fraud investigators. Every morning the tool triages the referral queue before anyone opens it: each case gets a risk score, a risk rating, a plain-language assessment with cited evidence, and a recommended next step. The investigator stays in control: they accept or reject each AI finding, move the case through its status, annotate it, and question it.

![Architecture](docs/architecture.png)

The prototype runs fully offline with no API key, and it ships with one recorded AI review run (GPT-6 Luna writing and Gemini 3.1 Flash-Lite judging, both through Perplexity) so the AI review tab has real model output even without a key. Connecting a language model (any provider) adds model-written narratives, open-ended follow-up questions, and an LLM-as-judge that audits every model output before an investigator sees it.

## Run it locally

Requirements: Python 3.11 or newer. Nothing else is needed: no Node, no Docker, and no API key. On a Mac, double-clicking `run_app.command` in the project folder does all of the steps below (the first time, macOS may ask you to allow it; `chmod +x run_app.command` fixes a "permission" error): it checks for Python 3.11 or newer, creates the virtualenv on first run, reinstalls packages only when `requirements.txt` changes, creates `.env` from `.env.example` if there is none, says which AI review you will get (live models if `.env` has a key, otherwise the recorded run), and opens the browser.

```bash
git clone https://github.com/arunbodd/second-look.git
cd second-look
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python serve.py
```

Open **http://127.0.0.1:8000** or **https://127.0.0.1:8443**. `serve.py` starts both listeners from one process (if another program already uses 8000 or 8443 it takes the next free port, prints it, and `--open` opens the browser there); the https one uses a self-signed certificate created on first run in `data/certs/` (with the `openssl` command, present on macOS and Linux), so the browser shows a one-time warning to accept. `python serve.py --http-only` or `uvicorn app.main:app --port 8000` gives plain http alone. `make install && make run` does the same in two words.

With Docker: `docker compose up --build` serves http://127.0.0.1:8000 from a non-root container with decisions and the model cache on a named volume; put API keys in `.env` first if you want the AI review. `/healthz` and `/readyz` answer for a load balancer, and TLS is expected there. The 50 cases load from `data/sample_cases_synthetic.csv` (the brief calls it `sample_cases.csv`), the queue is triaged at startup, and the **Rules review** tab is ready immediately. (The UI loads three Google Fonts for its look; without internet it falls back to system fonts and works the same.)

To triage your own case sheet, open the **Data** menu (the file name in the top bar), choose **Upload a case sheet**, and pick a file with the same columns as the sample (`case_id`, `claim_number`, `claim_date`, `care_type`, `claim_amount_usd`, `state`, and the ten signal columns). The upload is triaged on the spot, keeps its own decision log, and **Use the sample data** brings the bundled file back. The same menu exports everything to Excel and resets decisions. The same thing works from the command line: `curl -X POST --data-binary @my_cases.csv "http://localhost:8000/api/dataset?filename=my_cases.csv"`.

Run the tests:

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Connect a language model (optional)

Put an API key in `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `PERPLEXITY_API_KEY`), start the app, and open the **Models** menu in the top bar to pick a writer and a judge from the current models of each family (GPT-6 Astra/Sol/Luna, Claude Fable 5.1/Opus 5.5/Sonnet 5/Haiku 4.5, Gemini 3.1 Pro/3.8 Flash). A Perplexity key reaches GPT-6, Claude, and Gemini models through Perplexity's Agent API, so one key is enough for a writer and a judge from different makers; web search is never turned on, so answers come from the evidence pack alone. The choice applies at once, without a restart. To fix a default, or to use a local or other OpenAI-compatible server, set the writer and, optionally, a separate judge in `.env`; the same three variables work for every provider, and whatever `.env` names also appears in the menu.

| Provider | `LLM_PROVIDER` | `LLM_MODEL` | `LLM_BASE_URL` | `LLM_API_KEY` |
|---|---|---|---|---|
| Anthropic | `anthropic` | `claude-sonnet-5` | (empty) | your key |
| OpenAI | `openai` | `gpt-4.1-mini` | (empty) | your key |
| Ollama (local) | `openai` | `qwen2.5:7b-instruct` | `http://localhost:11434/v1` | `ollama` |
| LM Studio, vLLM, llama.cpp | `openai` | the served model name | the server's `/v1` URL | anything |
| OpenRouter, Groq, Together | `openai` | the provider's model name | the provider's `/v1` URL | your key |
| Perplexity (GPT, Claude, Gemini) | `perplexity` | `openai/gpt-6-luna`, `anthropic/claude-sonnet-5`, … | (empty) | your key |
| Anything else | `litellm` | LiteLLM model string | (optional) | (optional) |

`litellm` is an optional adapter: install it with `pip install litellm` to reach 100+ providers through one library.

Recommended: give the judge a different model family from the writer (`JUDGE_PROVIDER`, `JUDGE_MODEL`), because a model rates its own writing too kindly. The UI shows a warning when writer and judge are the same model.

Example `.env` for Anthropic writer plus an Ollama judge:

```
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5
LLM_API_KEY=sk-ant-...
JUDGE_PROVIDER=openai
JUDGE_MODEL=qwen2.5:7b-instruct
JUDGE_BASE_URL=http://localhost:11434/v1
JUDGE_API_KEY=ollama
```

Nothing is sent to a model until you ask. Rate limits (429) and transient server errors are retried with exponential backoff that honours `Retry-After`; a rejected request (a bad key, an unknown model) fails at once. On the **AI review** tab a run bar covers exactly the cases the table shows (set with the View selector: all, today's picks, or a risk rating, plus care type and state), and names the writer and judge with and a rough call and cost estimate; **Run** sends those cases, rows fill in as they finish, and **Stop** halts what has not started. A single case can be sent from its row or from the case panel. Results are cached in `data/assessment_cache.db` by evidence pack, writer, judge, and prompt version, so a reset, a restart, switching models back and forth, or re-uploading the same cases never sends the same evidence twice (`PRECOMPUTE=true` reviews everything at startup instead). If the model is unreachable or returns unusable output, the app keeps working and shows a flag on the affected case.

**Token counter and cost controls.** Every model call's tokens (input, cached input, output, and hidden reasoning) and cost are recorded from the provider's own response; Perplexity reports the exact charge, and for the others it is computed from list prices. The run bar totals them, the quality review shows each case's writer and judge tokens, and each answer carries its own count. The pre-run estimate switches from a budget to measured averages once three cases are reviewed. What keeps the count down: the evidence pack goes to models as compact JSON without the per-item definitions (about 22% fewer tokens than indented JSON), it always opens the first message so a revision or a follow-up question on the same case reuses it through prompt caching (automatic on OpenAI and Perplexity, marked explicitly for Anthropic), a revision continues the conversation instead of resending a new prompt, the judge is not called when the automated checks already failed a draft, and reasoning models run at low reasoning effort (`LLM_REASONING_EFFORT`, `JUDGE_REASONING_EFFORT`). `python -m app.record --max-cost 2` stops a recording before it spends more than two dollars.

**Recorded run.** Without a key, the AI review tab replays `data/recorded_review.json`: one real run with GPT-6 Luna writing and Gemini 3.1 Flash-Lite judging (two makers, both through Perplexity), plus judged answers to the ten starter questions for the suspicious cases. The whole recording, 50 reviews and 100 answers in 217 calls, cost $0.33 as billed by Perplexity. It says so in the run bar, stays selectable as "Recorded run" in the Models menu, and never calls a model. Each entry is tied to the evidence-pack fingerprint it was written from, so after a change to the engine, the prompt, or the data a case reads "not run" instead of showing a stale review. Refresh it with `python -m app.record` (or `make record`; options `--writer`, `--judge`, `--questions-for`, `--max-cost`; an interrupted recording resumes where it stopped), or turn it off with `RECORDED_REVIEW=off`.

## What you will see

1. **Morning brief.** One sentence says how many referrals look suspicious and what share of claim dollars they hold; the next says how much of the open dollars at risk the top cases cover ("reviewing the top 8 cases by dollars at risk covers 80% of the open dollars at risk"), with the number editable in place. A strip under it draws every case as a segment sized by its claim amount and coloured by risk rating, with today's picks marked; clicking a rating filters the page.
2. **Three figures.** What a day of reviews covers (click the curve to set capacity), the risk score distribution by risk rating, and dollars at risk by care type. Clicking a bar filters the queue.
3. **Queue.** Today's picks are numbered, each row says in plain words why it scored (the top evidence families), and one column shows the decision and status. The toolbar has a search box, a decision filter (To decide, Decided, Closed), one View selector (All, Today's picks, or a risk rating), and care type and state. In AI review the run covers whatever the table shows.
4. **Review modes.** **Rules review** (engine score, template narrative, rule checks; no model calls) and **AI review** (model-written narratives audited by the LLM judge) share the same queue; switching is instant.
5. **Case panel.** Score, risk rating, and confidence with the facts that matter; your call on the AI finding (Accept, Reject with a risk rating and reason, or Needs more evidence) and a separate status (New, In review, Documentation requested, Escalated, Closed benign, Closed confirmed concern); "Points to risk" beside "Could explain it"; what this file cannot show; the next step; the ten-signal grid with counterfactuals; and collapsed rows for the evidence pack, verification steps, the quality review, and similar cases. The "Ask about this case" panel names the model answering (or the rules engine in Rules review) and offers ten starter questions. Keyboard: `j`/`k` move between cases, `a` accepts, `Esc` closes.
6. **Score drivers.** For whichever file is loaded: what each signal does to the score (with the rule that flags it), how the ten signals move together (the risk score and claim amount are left out of the matrix: the score is built from the signals, so correlating it with them would be circular), claim amount against the score, a check of the columns that are not signals, and which column correlations have a business reason.
7. **Ask and export.** Grounded follow-up questions with citations and optional judge verdicts; a Markdown case file per case; and an Excel workbook of the whole queue whose summary is live formulas, including an editable capacity plan.

## How it works

```
CSV (50 referrals)
  └─ Deterministic engine (app/engine)
       signals → strengths → 5 evidence families → risk score (noisy-OR) → risk rating
       + Isolation Forest second opinion, links, similar cases, counterfactuals, guardrails
       + evidence pack with stable IDs (E1..En)
            └─ Narrative writer (app/llm)   offline template  |  any LLM via one adapter
                 └─ Quality review (app/quality)
                      layer 1: rule checks (always)      citations, figures, drivers, tone, risk-rating fit
                      layer 2: LLM-as-judge (optional)   5-criterion rubric, revise loop, audit
                           └─ publish policy: pass → show; revise → show with flag; fail → template instead
Investigator ─ accept / reject (risk rating + reason) / needs evidence · status workflow · notes · feedback · questions ─ event log (SQLite)
                                                                  └─ exports: Markdown case file, Excel workbook
```

The engine owns every number. Models only write words on top of the engine's output, and every model output is checked against the evidence pack before it is shown. The diagram at the top (`docs/architecture.svg`, regenerated with `python docs/architecture.py --png`) shows the same flow with the tech stack.

**Local only by default.** The app has no sign-in, so `serve.py` listens on 127.0.0.1 only and refuses any other address without `--allow-network`; nothing on your office network or public Wi-Fi can reach it. Two further checks (`app/netguard.py`) stop a malicious web page open in the same browser: requests must name an allowed host (`localhost`, `127.0.0.1`, `::1`, plus `ALLOWED_HOSTS`), which defeats DNS rebinding, and a write request sent from any other origin is refused with 403, so a page cannot start an AI review, upload data, or record a decision. Docker publishes the port on 127.0.0.1 only, `run_app.command` makes `.env` readable by you alone, and the self-signed certificate's private key is created with the same restriction.

**Production posture.** Security headers and a strict content-security policy on every response, a request id on every log line and error, JSON errors that never leak stack traces, size limits on uploads, notes, and questions, `/healthz` and `/readyz`, pinned dependencies (`requirements.lock`), a non-root Docker image, and CI that runs the tests on Python 3.11 and 3.12 and builds the image. Not included, deliberately: sign-in and roles, and a managed database; the event log is SQLite behind one small module (`app/store.py`) so it can move to Postgres without touching the rest.

## Project layout

```
app/
  engine/    signals.py (thresholds and families), scoring.py, evidence.py, narrative.py (offline text)
  llm/       providers.py (adapters), catalog.py (models menu), prompts.py, assessor.py (generate → check → judge → revise), chat.py
  quality/   checks.py (rule layer), judge.py (LLM judge), flaws.py (test fixtures for the rule checks)
  recorded.py (recorded run replay), record.py (python -m app.record)
  service.py (queue, assessments, case state), workflow.py (decision and status rules), store.py (SQLite)
  main.py (FastAPI, middleware, health), export_xlsx.py (Excel workbook)
  static/    index.html, app.js, app.css (no build step, no JS dependencies; PrimeOmicX styling)
data/        sample_cases_synthetic.csv, recorded_review.json (the recorded AI run)
docs/        architecture diagram (.svg, .png, and its generator)
tests/       88 tests, including mocked LLM providers, the decision workflow, legacy events, the Excel export, and security headers
run_app.command (Mac launcher), Dockerfile, docker-compose.yml, Makefile, requirements.lock, .github/workflows/ci.yml
```

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `offline` | `offline`, `openai`, `anthropic`, `perplexity`, or `litellm` |
| `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY` | (empty) | Writer model settings |
| `LLM_TIMEOUT`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_JSON_MODE` | `90`, `0.2`, `1400`, `auto` | Writer request settings (`JSON_MODE` is for OpenAI-compatible servers: `auto`, `on`, or `off`) |
| `LLM_REASONING_EFFORT`, `JUDGE_REASONING_EFFORT` | `low` | Reasoning effort for GPT-5 and GPT-6 models (`low`, `medium`, `high`, or `none` to send nothing); hidden reasoning is billed as output |
| `JUDGE_PROVIDER`, `JUDGE_MODEL`, `JUDGE_BASE_URL`, `JUDGE_API_KEY` | same as writer | Judge model settings |
| `JUDGE_ENABLED` | `true` | Set `false` for rule checks only |
| `JUDGE_MAX_REVISIONS` | `1` | Revision rounds before a flagged narrative is published or replaced |
| `JUDGE_CHAT` | `true` | Allow judging chat answers |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `PERPLEXITY_API_KEY` | (empty) | Keys the Models menu uses for each family (Perplexity reaches all three makers) |
| `PRECOMPUTE` | `false` | Review every case at startup instead of waiting for **Run** |
| `LLM_MAX_CONCURRENCY` | `3` | Parallel model calls during a run |
| `RECORDED_REVIEW` | `data/recorded_review.json` | Recorded run shown when no model is configured; `off` disables it |
| `ALLOWED_HOSTS` | (empty) | Extra host names the app answers to, comma-separated (for a deployment behind a proxy); `localhost`, `127.0.0.1`, and `::1` are always allowed |
| `DATA_PATH` | `data/sample_cases_synthetic.csv` | Input CSV |
| `LOG_LEVEL` | `INFO` | Log level; each request logs its id, path, status, and time |
| `DB_PATH` | `data/triage.db` | SQLite file for the event log (uploads get their own file under `data/uploads/`); model output is cached beside it in `assessment_cache.db` |

To start clean, use **Data → Reset decisions and notes** in the UI (clears decisions, notes, and questions but keeps cached narratives), or stop the server and delete `data/triage.db` and `data/assessment_cache.db` (clears everything).
