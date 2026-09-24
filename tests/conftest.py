import json
import os
from pathlib import Path

# Keep a developer's .env (API keys, model choices) out of the test run.
os.environ["JAI_NO_DOTENV"] = "1"
os.environ["RECORDED_REVIEW"] = "off"
os.environ["ALLOWED_HOSTS"] = "testserver"  # the host name FastAPI's TestClient sends  # tests that need the recorded run point it at a fixture
for _k in [k for k in os.environ if k.startswith(("LLM_", "JUDGE_")) or k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY", "PRECOMPUTE")]:
    os.environ.pop(_k)

import httpx
import pandas as pd
import pytest

from app.engine.scoring import triage_queue

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "sample_cases_synthetic.csv"


@pytest.fixture(scope="session")
def df() -> pd.DataFrame:
    return pd.read_csv(CSV)


@pytest.fixture(scope="session")
def queue(df):
    return triage_queue(df, "2026-01-01T06:00:00+00:00")


class ScriptedLLM:
    """An httpx transport that plays an OpenAI-compatible or Anthropic server from a script.

    ``replies`` is a list of strings returned in order; the last one repeats. ``calls`` records
    every request body so tests can assert on prompts.
    """

    def __init__(self, replies: list[str], style: str = "openai"):
        self.replies = list(replies)
        self.style = style
        self.calls: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
            text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            if self.style == "openai":
                payload = {"model": body.get("model", "mock"), "choices": [{"message": {"role": "assistant", "content": text}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            else:
                payload = {"model": body.get("model", "mock"), "content": [{"type": "text", "text": text}],
                           "usage": {"input_tokens": 10, "output_tokens": 5}}
            return httpx.Response(200, json=payload)

        return httpx.MockTransport(handler)


def good_narrative(case: dict) -> dict:
    """A narrative that should pass every rule check for the given case."""
    from app.engine.narrative import offline_narrative

    return offline_narrative(case)


def good_judge(verdict: str = "pass", scores: int = 5, issues=None) -> str:
    return json.dumps({
        "scores": {c: scores for c in ("groundedness", "completeness", "calibration", "actionability", "neutrality")},
        "verdict": verdict,
        "issues": issues or [],
        "missed_evidence_ids": [],
        "summary": "Looks fine.",
    })


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Provider retries back off with real sleeps; tests do not wait."""
    from app.llm import providers
    monkeypatch.setattr(providers, "_sleep", lambda s: None)
