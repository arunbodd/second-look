# Second Look

Second Look triages a queue of long-term care fraud referrals before an investigator opens it. Each case gets a risk score, a risk rating, a short assessment that cites its evidence, and a suggested next step. The investigator accepts or rejects each finding, moves the case through review, and can ask questions about it.

![Architecture](docs/architecture.png)

A rules engine computes every score. A language model is optional: it writes the assessment and answers questions, and every draft is checked against the case's evidence before anyone sees it. Without an API key, the app shows a recorded model run for the 50 sample cases.

## Run it

Needs Python 3.11 or newer.

```bash
git clone https://github.com/arunbodd/second-look.git
cd second-look
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python serve.py
```

Open http://127.0.0.1:8000. The app also serves https on 8443 with a self-signed certificate, so the browser asks you to accept it once. If a port is taken, the next free one is used and printed. Use `python serve.py --http-only` for plain http.

On a Mac, double-click `run_app.command` instead. It creates the virtualenv, installs packages, and opens the browser. If macOS says you lack permission, run `chmod +x run_app.command` once.

Docker: `docker compose up --build`, then open http://127.0.0.1:8000.

Tests: `pip install -r requirements-dev.txt && pytest -q`

## What's in the app

- **Morning brief.** How many referrals look suspicious, what share of claim dollars they hold, and how much of the money at risk today's picks cover.
- **Queue.** Cases sorted by risk and dollars at risk, with the evidence behind each score.
- **Case panel.** The assessment, what points to risk beside what could explain it, the ten signals, the evidence pack, and the decision buttons. Keys: `j`/`k` to move, `a` to accept, `Esc` to close.
- **Rules review and AI review.** The same queue with template text or model-written text.
- **Score drivers.** What each signal does to the score in the loaded file, and which signals move together.
- **Exports.** A Markdown case file per case and an Excel workbook of the queue.

To triage your own data, use **Data → Upload a case sheet** with the same columns as `data/sample_cases_synthetic.csv`.

## Language models (optional)

Add a key to `.env` (copy `.env.example`), start the app, and pick a writer and a judge in the **Models** menu. Nothing is sent to a model until you press **Run**.

| Key | Models |
|---|---|
| `OPENAI_API_KEY` | GPT-6 Astra, Sol, and Luna |
| `ANTHROPIC_API_KEY` | Claude Fable 5.1, Opus 5.5, Sonnet 5, and Haiku 4.5 |
| `GEMINI_API_KEY` | Gemini 3.1 Pro and 3.8 Flash |
| `PERPLEXITY_API_KEY` | GPT, Claude, and Gemini models through Perplexity's Agent API, with web search off |

Use a judge from a different model maker than the writer, because models grade their own writing too kindly. The app warns when they match.

To set a default pair, or to use a local or other OpenAI-compatible server (Ollama, LM Studio, vLLM, OpenRouter), set `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY` in `.env`, and the same `JUDGE_*` variables for the judge. `LLM_PROVIDER=litellm` works with any provider LiteLLM supports after `pip install litellm`.

Model output is cached by evidence, model pair, and prompt version, so the same case is never sent twice. The app counts tokens and cost for every call and shows them in the run bar and on each case.

### Recorded run

`data/recorded_review.json` holds one real run: GPT-6 Luna writing and Gemini 3.1 Flash-Lite judging, both through Perplexity, for all 50 cases, plus answers to the ten starter questions for the suspicious cases. It cost $0.33. The app shows it when no key is set. A case whose evidence has changed since the recording shows "not run".

Re-record with `python -m app.record` (options: `--writer`, `--judge`, `--max-cost`, default $2).

## Security

The app has no sign-in, so it listens on 127.0.0.1 only and cannot be reached from your network or public Wi-Fi. Starting it on another address needs `--allow-network`.

It also refuses requests addressed to an unknown host name, which blocks DNS rebinding, and write requests from other websites, so a page open in your browser cannot start a model run or change data. Docker publishes the port on 127.0.0.1 only.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY` | offline | Default writer model |
| `JUDGE_PROVIDER`, `JUDGE_MODEL`, `JUDGE_BASE_URL`, `JUDGE_API_KEY` | same as writer | Default judge model |
| `JUDGE_ENABLED` | `true` | `false` runs the automated checks only |
| `JUDGE_MAX_REVISIONS` | `1` | Rewrites allowed after the judge flags a draft |
| `LLM_REASONING_EFFORT` | `low` | Reasoning effort for GPT-5 and GPT-6 models |
| `LLM_MAX_CONCURRENCY` | `3` | Model calls run at once |
| `RECORDED_REVIEW` | `data/recorded_review.json` | Recorded run file; `off` disables it |
| `ALLOWED_HOSTS` | (empty) | Extra host names, for a deployment behind a proxy |
| `DATA_PATH` | `data/sample_cases_synthetic.csv` | Case sheet loaded at startup |
| `DB_PATH` | `data/triage.db` | Decision log (SQLite) |

To start over, use **Data → Reset decisions and notes**, or delete `data/triage.db` and `data/assessment_cache.db`.

## Layout

```
app/engine/     signals, scoring, evidence pack, template text
app/llm/        model adapters, prompts, writer, chat, token accounting
app/quality/    automated checks and the model judge
app/static/     the web page (no build step)
app/            service, API, workflow, event store, network checks, Excel export
data/           sample cases and the recorded run
docs/           architecture diagram and its generator
tests/          88 tests
```
