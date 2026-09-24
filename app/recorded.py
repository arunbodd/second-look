"""A recorded AI review that ships with the repo, so a reviewer without an API key still sees
what the model layer produces.

The file holds one run: the writer and judge it used, the date, every assessment, and answers
to the starter questions for the suspicious cases. Each entry carries the fingerprint of the
evidence pack it was written from (the same key the live cache uses), so an entry is shown
only while the case's evidence is unchanged. Change the engine, the prompt, or the data and
the entry reads "not run" instead of showing a stale review.

Build or refresh it with ``python -m app.record`` (needs the API keys for the chosen models).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .llm.assessor import cache_key

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "recorded_review.json"


def _norm(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", q.lower()).strip()


class RecordedRun:
    def __init__(self, data: dict):
        self.data = data
        self.writer = data["writer"]  # {"id", "label", "describe"}
        self.judge = data["judge"]
        self.recorded_at = data["recorded_at"]
        self.cases: dict = data.get("cases", {})
        self.questions: dict = data.get("questions", {})

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "RecordedRun | None":
        try:
            return cls(json.loads(Path(path).read_text()))
        except (OSError, ValueError, KeyError):
            return None

    @property
    def label(self) -> str:
        return f"Recorded run · {self.writer['label']} + {self.judge['label']} · {self.recorded_at[:10]}"

    def fingerprint(self, case: dict) -> str:
        return cache_key(case, self.writer["describe"], self.judge["describe"])

    def assessment(self, case: dict) -> dict | None:
        entry = self.cases.get(case["case_id"])
        if not entry or entry.get("fingerprint") != self.fingerprint(case):
            return None
        return entry["assessment"]

    def answer(self, case: dict, question: str) -> dict | None:
        if self.assessment(case) is None:  # answers are only valid next to the review they came from
            return None
        for item in self.questions.get(case["case_id"], []):
            if _norm(item["question"]) == _norm(question):
                return item
        return None

    def summary(self, cases: dict) -> dict:
        matched = sum(1 for c in cases.values() if self.assessment(c) is not None)
        return {"available": True, "label": self.label, "writer": self.writer["label"], "judge": self.judge["label"],
                "recorded_at": self.recorded_at, "recorded_cases": len(self.cases), "matched": matched,
                "same_maker": self.writer.get("vendor", "w") == self.judge.get("vendor", "j"),
                "questions": sum(len(v) for v in self.questions.values()),
                "question_cases": sorted(self.questions)}
