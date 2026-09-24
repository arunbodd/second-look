"""Application service: owns the queue, the AI assessments, and the case state."""

from __future__ import annotations

import hashlib
import io
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from .engine.analysis import analyse, key_indicator_counts
from .engine.evidence import build_evidence, evidence_pack
from .engine.scoring import QueueResult, triage_queue
from .engine.signals import LANES, REQUIRED_COLUMNS, SIGNAL_KEYS
from .llm import catalog
from .llm import usage as usage_mod
from .llm.assessor import assess_case, cache_key
from .llm.chat import answer_question, judge_answer
from .llm.providers import LLMError, LLMProvider, build_provider
from .quality.checks import run_rule_checks
from .recorded import RecordedRun
from .quality.judge import combine, judge_assessment
from .settings import Settings, reload_api_keys
from .store import Store, now_iso
from . import workflow as wf

OVERRIDE_REASONS = wf.REJECT_REASONS  # kept for callers of the previous name


def load_cases(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")
    for k in SIGNAL_KEYS + ["claim_amount_usd"]:
        df[k] = pd.to_numeric(df[k], errors="coerce")
    df["case_id"] = df["case_id"].astype(str)
    if df["case_id"].duplicated().any():
        raise ValueError("CSV contains duplicate case_id values.")
    if df["claim_amount_usd"].isna().any():
        bad = df.loc[df["claim_amount_usd"].isna(), "case_id"].tolist()
        raise ValueError(f"claim_amount_usd is missing or non-numeric for: {', '.join(bad)}")
    return df


class TriageService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.generator: LLMProvider | None = build_provider(settings.generator)
        self.judge: LLMProvider | None = build_provider(settings.judge)
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=settings.max_concurrency, thread_name_prefix="assess")
        # Models configured in .env that are not in the catalogue stay selectable.
        self._custom_models: list[dict] = []
        for cfg in (settings.generator, settings.judge):
            entry = catalog.custom_entry(cfg)
            if entry and catalog.find(entry["id"], self._custom_models) is None and catalog.find(catalog.entry_id_for(cfg) or "") is None:
                self._custom_models.append(entry)
        self._review: dict | None = None  # the AI review run in progress (or the last one)
        self._review_seq = 0
        # Model output is cached once per (case evidence, writer, judge, prompt version) in a store
        # shared by every dataset, so a reset, a restart, or re-uploading the same cases never
        # sends the same evidence to a model twice.
        self.cache = Store(settings.db_path.parent / "assessment_cache.db")
        # A recorded run ships with the repo. It is shown in the AI review tab when no model is
        # configured, and can be picked again from the Models menu at any time.
        self.recorded: RecordedRun | None = RecordedRun.load(settings.recorded_path) if settings.recorded_path else None
        self.replay = self.recorded is not None and not settings.generator.enabled
        self._load_dataset(load_cases(settings.data_path), settings.data_path.name, settings.db_path, uploaded=False)

    # ---- datasets --------------------------------------------------------------------
    @property
    def ai_available(self) -> bool:
        return self.replay or self.generator is not None or self.judge is not None

    # ---- models ----------------------------------------------------------------------
    def models(self) -> dict:
        """The catalogue the Models menu offers, which keys are present, and the current pair."""
        reload_api_keys()
        entries = []
        keys = catalog.key_status()
        for e in [*catalog.CATALOG, *self._custom_models]:
            entries.append({k: e[k] for k in ("id", "label", "family", "vendor", "model", "in", "out", "roles")}
                           | {"family_label": catalog.FAMILIES[e["family"]]["label"],
                              "key_present": bool(e.get("custom") and e.get("api_key")) or keys[e["family"]]})
        writer, judge = self.settings.generator, self.settings.judge
        return {
            "catalog": entries,
            "keys": keys,
            "current": ({"writer": "recorded", "judge": "recorded"} if self.replay
                        else {"writer": catalog.entry_id_for(writer), "judge": catalog.entry_id_for(judge)}),
            "same_family": writer.enabled and judge.enabled and _family(writer) == _family(judge),
            "recorded": self.recorded.summary(self.queue.cases) if self.recorded else {"available": False},
            "estimate": self._estimate(),
        }

    def _estimate(self) -> dict:
        """Per-case tokens for the pre-run estimate: measured averages once reviews exist."""
        with self._lock:
            done = [a["usage"] for a in self._assessments["ai"].values() if a.get("usage") and a["usage"].get("calls")]
        if len(done) < 3:
            return {"calls_per_case": catalog.CALLS_PER_CASE, "per_case": catalog.TOKENS_PER_CASE, "measured": 0}
        n = len(done)
        per = {role: {"in": round(sum(u[role]["input"] for u in done) / n), "out": round(sum(u[role]["output"] for u in done) / n)}
               for role in ("writer", "judge")}
        return {"calls_per_case": round(sum(u["calls"] for u in done) / n, 2), "per_case": per, "measured": n}

    def usage_summary(self) -> dict:
        """Tokens and cost behind the AI reviews and answers loaded now (live, cached, or recorded)."""
        with self._lock:
            ready = [a for cid, a in self._assessments["ai"].items() if self._status["ai"].get(cid) == "ready"]
        reviews = usage_mod.total(a.get("usage") for a in ready)
        writer = usage_mod.total((a.get("usage") or {}).get("writer") for a in ready)
        judge = usage_mod.total((a.get("usage") or {}).get("judge") for a in ready)
        n = sum(1 for a in ready if (a.get("usage") or {}).get("calls"))
        answers = usage_mod.empty()
        for e in self.store.events():
            p = e["payload"]
            if e["type"] == "chat" and p.get("role") == "assistant" and p.get("mode") == "ai" and p.get("source") != "recorded":
                answers = usage_mod.add(answers, p.get("usage"))
            elif e["type"] == "chat_judge" and not p.get("recorded"):
                answers = usage_mod.add(answers, p["report"].get("usage"))
        recorded_answers = None
        if self.replay and self.recorded:
            recorded_answers = usage_mod.total(
                usage_mod.add(q.get("usage"), (q.get("judge") or {}).get("usage"))
                for qs in self.recorded.questions.values() for q in qs)
        return {"reviews": {**reviews, "cases": n, "writer": writer, "judge": judge},
                "answers": answers, "recorded_answers": recorded_answers,
                "total": usage_mod.add(reviews, answers), "recorded": bool(self.replay)}

    def set_models(self, writer_id: str | None, judge_id: str | None) -> dict:
        """Switch the writer and/or the judge without a restart. Cached assessments for the new
        pair are restored; a review run in progress is stopped first."""
        reload_api_keys()  # a key edited in .env since startup is picked up here
        if writer_id == "recorded":
            if not self.recorded:
                raise ValueError("No recorded run ships with this copy (data/recorded_review.json is missing).")
            self.stop_review()
            with self._lock:
                self.replay = True
                off = self.settings.generator.__class__(provider="offline")
                self.settings.generator, self.settings.judge = off, off
                self.generator = self.judge = None
                self._assessments["ai"] = {}
                self._status["ai"] = {cid: "pending" for cid in self.queue.order}
                self._load_cached()
            return self.models()
        new_cfgs = {}
        for role, model_id, base in (("writer", writer_id, self.settings.generator), ("judge", judge_id, self.settings.judge)):
            if model_id in (None, "", "offline"):
                new_cfgs[role] = base if model_id is None else base.__class__(provider="offline")
                continue
            entry = catalog.find(model_id, self._custom_models)
            if entry is None:
                raise ValueError(f"Unknown model '{model_id}'.")
            cfg = catalog.config_for(entry, base)
            if not cfg.api_key and not (cfg.base_url and ("localhost" in cfg.base_url or "127.0.0.1" in cfg.base_url)):
                raise ValueError(f"No API key for {catalog.FAMILIES[entry['family']]['label']} models "
                                 f"(set {catalog.FAMILIES[entry['family']]['key_env']} in .env).")
            new_cfgs[role] = cfg
        self.stop_review()
        with self._lock:
            self.replay = False
            self.settings.generator, self.settings.judge = new_cfgs["writer"], new_cfgs["judge"]
            self.generator = build_provider(self.settings.generator)
            self.judge = build_provider(self.settings.judge)
            self._assessments["ai"] = {}
            self._status["ai"] = {cid: "pending" for cid in self.queue.order}
            self._load_cached()
        return self.models()

    # ---- AI review runs --------------------------------------------------------------
    def start_review(self, case_ids: list[str] | None = None) -> dict:
        """Queue AI assessments for the given cases (default: every case) that are not ready yet."""
        if self.replay:
            raise ValueError("This is the recorded run, so nothing is sent to a model. Add an API key to .env and pick models to review new cases.")
        if not self.ai_available:
            raise ValueError("No model is configured. Pick a writer in the Models menu first.")
        with self._lock:
            order = self.queue.order
            wanted = [cid for cid in (case_ids if case_ids is not None else order) if cid in self.queue.cases]
            todo = [cid for cid in wanted if self._status["ai"].get(cid) != "ready"]
            if self._review and self._review["running"]:
                todo = [cid for cid in todo if cid not in self._review["queued"]]
                review = self._review
                review["total"] += len(todo)
                review["queued"].update(todo)
            else:
                self._review_seq += 1
                review = self._review = {
                    "id": self._review_seq, "running": bool(todo), "stopped": False, "total": len(todo), "done": 0,
                    "failed": 0, "revised": 0, "skipped": len(wanted) - len(todo), "queued": set(todo),
                    "started_at": now_iso(), "finished_at": None, "writer": self.settings.generator.describe(),
                    "judge": self.settings.judge.describe(),
                }
                if not todo:
                    review["finished_at"] = now_iso()
            review_id = review["id"]
        for cid in todo:
            self._executor.submit(self._review_task, review_id, cid)
        return self.review_status()

    def _review_task(self, review_id: int, cid: str) -> None:
        with self._lock:
            review = self._review
            if not review or review["id"] != review_id or review["stopped"]:
                if review and review["id"] == review_id:  # stopped: skip what is still queued
                    review["queued"].discard(cid)
                    review["total"] -= 1
                    self._finish_review_if_done(review)
                return
        try:
            result = self._run(cid, "ai")
        except Exception:  # _run already falls back; this only guards a KeyError on a swapped dataset
            result = None
        with self._lock:
            review = self._review
            if review and review["id"] == review_id:
                review["queued"].discard(cid)
                review["done"] += 1
                if result is None or result.get("source") == "template_unavailable":
                    review["failed"] += 1
                elif result.get("revisions", 0) > 0:
                    review["revised"] += 1
                self._finish_review_if_done(review)

    def _finish_review_if_done(self, review: dict) -> None:
        if not review["queued"] and review["running"]:
            review["running"] = False
            review["finished_at"] = now_iso()

    def stop_review(self) -> dict:
        with self._lock:
            review = self._review
            if review and review["running"]:
                review["stopped"] = True  # queued cases skip themselves; running ones finish
        return self.review_status()

    def review_status(self) -> dict:
        with self._lock:
            review = self._review
            ready = sum(1 for v in self._status["ai"].values() if v == "ready")
            if not review:
                return {"running": False, "total": 0, "done": 0, "failed": 0, "revised": 0, "ready": ready,
                        "cases": len(self.queue.order)}
            return {k: v for k, v in review.items() if k != "queued"} | {"pending": len(review["queued"]), "ready": ready,
                                                                        "cases": len(self.queue.order)}

    def _providers(self, mode: str) -> tuple[LLMProvider | None, LLMProvider | None]:
        """Two review modes live side by side: 'offline' (template narrative, rule checks) and
        'ai' (model-written narrative reviewed by the judge). The engine's numbers are shared."""
        if mode == "ai" and self.ai_available:
            return self.generator, self.judge
        return None, None

    def _load_dataset(self, df: pd.DataFrame, name: str, db_path: Path, uploaded: bool) -> None:
        """Replace the queue with a new dataset. Each dataset keeps its own SQLite file, so
        decisions on the sample are untouched by an upload and come back when it is reloaded."""
        with self._lock:
            self._review = None  # queued review tasks for the old dataset skip themselves
            self.df = df
            self.dataset = {"name": name, "rows": int(len(df)), "uploaded": uploaded}
            self.triaged_at = now_iso()
            self.queue: QueueResult = triage_queue(self.df, self.triaged_at)
            self.store = Store(db_path)
            self._assessments: dict[str, dict[str, dict]] = {"offline": {}, "ai": {}}
            self._status: dict[str, dict[str, str]] = {m: {cid: "pending" for cid in self.queue.order} for m in ("offline", "ai")}
            self._case_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
            self._load_cached()
            queue = self.queue
        for cid in queue.order:  # offline assessments are instant
            self.ensure_assessment(cid, "offline")
        # AI review is never started by a load or an upload: the investigator runs it from the
        # AI review tab (or sets PRECOMPUTE=true to review every case at startup).
        if self.ai_available and self.settings.precompute:
            self.start_review()

    def upload_dataset(self, csv_text: str, filename: str) -> dict:
        """Triage an uploaded CSV with the same columns as the sample file."""
        try:
            df = load_cases(io.StringIO(csv_text))
        except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise ValueError(f"Could not read the CSV: {exc}") from exc
        if len(df) < 2:
            raise ValueError("The CSV needs at least two cases so that peer comparisons mean something.")
        if len(df) > 5000:
            raise ValueError("The prototype accepts at most 5,000 cases per upload.")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "upload.csv")[:80] or "upload.csv"
        digest = hashlib.sha256(csv_text.encode()).hexdigest()[:10]
        uploads = self.settings.db_path.parent / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        (uploads / f"{digest}_{safe}").write_text(csv_text)
        self._load_dataset(df, safe, uploads / f"{digest}.db", uploaded=True)
        return self.dataset

    def load_sample_dataset(self) -> dict:
        self._load_dataset(load_cases(self.settings.data_path), self.settings.data_path.name, self.settings.db_path, uploaded=False)
        return self.dataset

    # ---- assessments -----------------------------------------------------------------
    def _key(self, case: dict, mode: str) -> str:
        if mode == "ai" and self.ai_available:
            return cache_key(case, self.settings.generator.describe(), self.settings.judge.describe())
        return cache_key(case, "offline", "off")

    def _load_cached(self) -> None:
        for mode in ("offline", "ai"):
            if mode == "ai" and not self.ai_available:
                continue  # without a model the AI lane stays empty rather than mirroring the templates
            if mode == "ai" and self.replay:
                for cid in self.queue.order:
                    rec = self.recorded.assessment(self.queue.cases[cid])
                    if rec is not None:
                        self._assessments[mode][cid] = rec
                        self._status[mode][cid] = "ready"
                    else:
                        self._status[mode][cid] = "not_recorded"
                continue
            for cid in self.queue.order:
                key = self._key(self.queue.cases[cid], mode)
                cached = self.cache.get_assessment(cid, key) or self.store.get_assessment(cid, key)
                if cached and cached.get("source") != "template_unavailable":  # failures are never reused
                    self._assessments[mode][cid] = cached
                    self._status[mode][cid] = "ready"

    def _run(self, cid: str, mode: str = "offline", force: bool = False) -> dict:
        with self._lock:
            queue, locks = self.queue, self._case_locks
            assessments, status = self._assessments[mode], self._status[mode]
        if cid not in queue.cases:
            raise KeyError(cid)
        if mode == "ai" and self.replay:
            # Replay never calls a model: a case outside the recording falls back to the rules review.
            rec = self.recorded.assessment(queue.cases[cid])
            if rec is not None:
                with self._lock:
                    assessments[cid], status[cid] = rec, "ready"
                return rec
            return self.ensure_assessment(cid, "offline")
        gen, judge = self._providers(mode)
        with locks[f"{mode}:{cid}"]:  # one assessment per case and mode at a time
            with self._lock:
                if not force and status.get(cid) == "ready" and cid in assessments:
                    return assessments[cid]
                status[cid] = "running"
            case = queue.cases[cid]
            try:
                result = assess_case(case, gen, judge, self.settings.max_revisions)
            except Exception as exc:  # last-resort guard so the queue never breaks
                result = assess_case(case, None, None, 0)
                detail = str(exc).strip()[:240]
                result["flags"].append({"level": "error", "text": f"Assessment failed ({type(exc).__name__}: {detail or 'no detail'}); showing the template narrative."})
                result["source"] = "template_unavailable"
            result["mode"] = mode
            failed = result["source"] == "template_unavailable"
            with self._lock:
                assessments[cid] = result
                status[cid] = "failed" if failed else "ready"
            if not failed:  # a failed call is shown with its error but never cached, so a rerun retries it
                self.cache.put_assessment(cid, self._key(case, mode), result)
            return result

    def resolve_mode(self, mode: str | None) -> str:
        if mode == "ai" and self.ai_available:
            return "ai"
        return "offline"

    def ensure_assessment(self, cid: str, mode: str = "offline", force: bool = False) -> dict:
        mode = self.resolve_mode(mode)
        if not force:
            with self._lock:
                if cid in self._assessments[mode]:
                    return self._assessments[mode][cid]
        return self._run(cid, mode, force=force)

    def assessment_status(self, cid: str, mode: str = "offline") -> str:
        return self._status[self.resolve_mode(mode)].get(cid, "pending")

    # ---- case state from the event log ----------------------------------------------
    def case_state(self, cid: str) -> dict:
        events = self.store.events(cid)
        flow = wf.empty_state()
        decisions, notes, chat, feedback = [], [], [], {}
        judge_by_msg: dict[int, dict] = {}
        for e in events:
            p = e["payload"]
            if e["type"] in ("decision", "status"):
                wf.apply_event(flow, e["type"], p)
                decisions.append({**p, "type": e["type"], "actor": e["actor"], "ts": e["ts"], "id": e["id"],
                                  "summary": wf.event_summary(e["type"], p, LANES)})
            elif e["type"] == "note":
                notes.append({"text": p["text"], "actor": e["actor"], "ts": e["ts"], "id": e["id"]})
            elif e["type"] == "feedback":
                feedback[p["evidence_id"]] = {**p, "actor": e["actor"], "ts": e["ts"]}
            elif e["type"] == "chat":
                chat.append({**p, "actor": e["actor"], "ts": e["ts"], "id": e["id"]})
            elif e["type"] == "chat_judge":
                judge_by_msg[p["message_id"]] = p["report"]
        for m in chat:
            if m["id"] in judge_by_msg:
                m["judge"] = judge_by_msg[m["id"]]
        timeline = [
            {"id": e["id"], "type": e["type"], "actor": e["actor"], "ts": e["ts"], "summary": _event_summary(e)}
            for e in events if e["type"] != "chat_judge"
        ]
        return {
            "decision": flow["decision"],
            "decision_label": wf.DECISIONS[flow["decision"]],
            "status": flow["status"],
            "status_label": wf.STATUSES[flow["status"]],
            "closed": flow["status"] in wf.CLOSED,
            "final_lane": flow["final_lane"],
            "reason_code": flow["reason_code"],
            "decisions": decisions,
            "notes": notes,
            "feedback": feedback,
            "chat": chat,
            "timeline": timeline,
        }

    # ---- views -----------------------------------------------------------------------
    def meta(self) -> dict:
        s = self.settings
        ready = {m: sum(1 for v in self._status[m].values() if v == "ready") for m in ("offline", "ai")}
        return {
            "app": "Second Look",
            "dataset": self.dataset,
            "required_columns": REQUIRED_COLUMNS,
            "ai_available": self.ai_available,
            "replay": self.recorded.summary(self.queue.cases) if (self.replay and self.recorded) else None,
            "modes": {"offline": {"label": "Rules review", "ready": ready["offline"]},
                      "ai": {"label": "AI review", "ready": ready["ai"], "available": self.ai_available}},
            "generator": f"{self.recorded.writer['label']} (recorded {self.recorded.recorded_at[:10]})" if self.replay else s.generator.describe(),
            "judge": f"{self.recorded.judge['label']} (recorded)" if self.replay else s.judge.describe(),
            "judge_chat": s.judge_chat and s.judge.enabled,
            "same_model_judge": s.generator.enabled and s.generator.describe() == s.judge.describe(),
            "review": self.review_status(),
            "usage": self.usage_summary(),
            "models": self.models()["current"],
            "engine_version": self.queue.engine_version,
            "triaged_at": self.triaged_at,
            "cases": len(self.queue.order),
            "assessments_ready": ready["ai" if self.ai_available else "offline"],
            "warnings": s.warnings,
            "override_reasons": wf.REJECT_REASONS,
            "lanes": LANES,
            "status_labels": wf.STATUSES,
            "decision_labels": wf.DECISIONS,
        }

    def queue_view(self, mode: str = "offline") -> dict:
        mode = self.resolve_mode(mode)
        rows = []
        for cid in self.queue.order:
            case = self.queue.cases[cid]
            state = self.case_state(cid)
            a = self._assessments[mode].get(cid)
            top = [f for f in case["families"] if f["key"] in case["active_families"]][:2]
            rows.append({
                "case_id": cid,
                **case["facts"],
                "risk_score": case["risk_score"],
                "lane": case["lane"],
                "lane_label": case["lane_label"],
                "base_lane": case["base_lane"],
                "confidence": (a["confidence"]["level"] if a else case["confidence"]["level"]),
                "dollars_at_risk": case["dollars_at_risk"],
                "drivers": [f["short"] for f in top],
                "driver_detail": [{"key": f["key"], "short": f["short"], "share": f["share"], "points": f["points"],
                                   "evidence_ids": f["signals"]} for f in case["families"] if f["key"] in case["active_families"]],
                "signals": {x["key"]: x["level"] for x in case["signals"]},
                "families": {f["key"]: f["evidence"] for f in case["families"]},
                "anomaly_pct": case["anomaly"]["percentile"],
                "guardrails": [g["code"] for g in case["guardrails"]],
                "qa_sample": case["qa_sample"],
                "linked": [l["case_id"] for l in case["linked"]],
                "status": state["status"],
                "status_label": state["status_label"],
                "decision": state["decision"],
                "decision_label": state["decision_label"],
                "closed": state["closed"],
                "final_lane": state["final_lane"],
                "data_gaps": case["data_gaps"],
                "assessment_status": self._status[mode][cid],
                "quality_verdict": a["quality"]["verdict"] if a else None,
                "source": a["source"] if a else None,
                "recommended_action": a["narrative"]["recommended_action"] if a else None,
            })
        return {"cases": rows, "briefing": self.briefing(rows), "alerts": self.queue.alerts}

    def analysis(self, mode: str = "offline") -> dict:
        """How the columns drive the risk score, for the loaded dataset (cached per dataset)."""
        mode = self.resolve_mode(mode)
        with self._lock:
            df, queue, triaged_at = self.df, self.queue, self.triaged_at
            cached = getattr(self, "_analysis", None)
            assessments = {cid: a for cid, a in self._assessments[mode].items() if self._status[mode].get(cid) == "ready"}
        if not cached or cached[0] != triaged_at:
            cached = (triaged_at, analyse(df, queue.cases, queue.order))
            with self._lock:
                self._analysis = cached
        return {**cached[1], "dataset": self.dataset, "mode": mode,
                "key_indicators": key_indicator_counts(assessments, queue.cases)}

    def signal_values(self) -> dict[str, list[float | None]]:
        """Raw signal values for the whole queue, for the strip plots in the UI."""
        out: dict[str, list] = {}
        for k in SIGNAL_KEYS:
            out[k] = [None if pd.isna(v) else float(v) for v in self.df[k].tolist()]
        return out

    def briefing(self, rows: list[dict] | None = None) -> dict:
        rows = rows or self.queue_view()["cases"]
        lanes = {}
        for lane in LANES:
            in_lane = [r for r in rows if r["lane"] == lane]
            lanes[lane] = {
                "label": LANES[lane]["label"],
                "count": len(in_lane),
                "amount": int(sum(r["claim_amount_usd"] for r in in_lane)),
                "dollars_at_risk": int(sum(r["dollars_at_risk"] for r in in_lane)),
                "open": sum(1 for r in in_lane if _undecided(r)),
            }
        open_rows = [r for r in rows if _undecided(r)]
        start = sorted(open_rows, key=lambda r: (-r["dollars_at_risk"], r["case_id"]))[:3]
        decided = [r for r in rows if not _undecided(r)]
        return {
            "triaged_at": self.triaged_at,
            "total": len(rows),
            "open": len(open_rows),
            "decided": len(decided),
            "lanes": lanes,
            "start_here": [{"case_id": r["case_id"], "care_type": r["care_type"], "claim_amount_usd": r["claim_amount_usd"],
                            "risk_score": r["risk_score"], "drivers": r["drivers"]} for r in start],
            "qa_samples": [r["case_id"] for r in rows if r["qa_sample"]],
            "alerts": self.queue.alerts,
            "estimated_minutes_saved": _minutes_saved(rows),
        }

    def case_view(self, cid: str, mode: str = "offline") -> dict:
        if cid not in self.queue.cases:
            raise KeyError(cid)
        mode = self.resolve_mode(mode)
        case = self.queue.cases[cid]
        a = self._assessments[mode].get(cid)
        state = self.case_state(cid)
        similar = []
        for s in case["similar"]:
            st = self.case_state(s["case_id"])
            similar.append({**s, "status": st["status"], "status_label": st["status_label"], "decision": st["decision"],
                            "decision_label": st["decision_label"], "final_lane": st["final_lane"]})
        return {
            "case": case,
            "evidence": build_evidence(case),
            "column_logic": evidence_pack(case)["engine"]["column_logic_for_red_flag_pairs"],
            "assessment": a,
            "assessment_status": self._status[mode][cid],
            "mode": mode,
            "state": state,
            "similar": similar,
        }

    # ---- human in the loop -----------------------------------------------------------
    def decide(self, cid: str, decision: str, actor: str, lane: str | None = None, reason_code: str | None = None,
               note: str = "") -> dict:
        """Record the investigator's call on the AI finding: accept, reject, needs_evidence, or reset."""
        if cid not in self.queue.cases:
            raise KeyError(cid)
        case = self.queue.cases[cid]
        plan = wf.plan_decision(self.case_state(cid), decision, case["lane"], lane, reason_code, LANES)
        payload = {**plan, "ai_lane": case["lane"], "note": note.strip(), "ai_risk_score": case["risk_score"]}
        self.store.add_event(cid, "decision", payload, actor)
        if note.strip():
            self.store.add_event(cid, "note", {"text": note.strip(), "context": decision}, actor)
        return self.case_state(cid)

    def set_status(self, cid: str, status: str, actor: str, note: str = "") -> dict:
        """Move the case through the workflow (in review, documentation requested, escalated, closed)."""
        if cid not in self.queue.cases:
            raise KeyError(cid)
        payload = {**wf.plan_status(self.case_state(cid), status), "note": note.strip()}
        self.store.add_event(cid, "status", payload, actor)
        if note.strip():
            self.store.add_event(cid, "note", {"text": note.strip(), "context": status}, actor)
        return self.case_state(cid)

    def add_note(self, cid: str, text: str, actor: str) -> dict:
        if not text.strip():
            raise ValueError("Empty note.")
        self.store.add_event(cid, "note", {"text": text.strip(), "context": "note"}, actor)
        return self.case_state(cid)

    def feedback(self, cid: str, evidence_id: str, verdict: str, actor: str, comment: str = "") -> dict:
        if verdict not in ("agree", "disagree", "clear"):
            raise ValueError("verdict must be agree, disagree, or clear")
        if evidence_id not in {e["id"] for e in build_evidence(self.queue.cases[cid])}:
            raise ValueError(f"Unknown evidence id {evidence_id}.")
        self.store.add_event(cid, "feedback", {"evidence_id": evidence_id, "verdict": verdict, "comment": comment.strip()}, actor)
        return self.case_state(cid)

    def chat(self, cid: str, question: str, actor: str, mode: str = "offline") -> dict:
        mode = self.resolve_mode(mode)
        gen, _ = self._providers(mode)
        case = self.queue.cases[cid]
        assessment = self.ensure_assessment(cid, mode)
        state = self.case_state(cid)
        history = [{"role": m["role"], "content": m["content"]} for m in state["chat"] if m.get("mode", "offline") == mode]
        notes = [n["text"] for n in state["notes"]]
        self.store.add_event(cid, "chat", {"role": "user", "content": question.strip(), "mode": mode}, actor)
        recorded_judge = None
        if mode == "ai" and self.replay:
            rec = self.recorded.answer(case, question)
            if rec is not None:
                res = {k: rec.get(k) for k in ("answer", "citations", "source", "model", "checks", "warnings", "usage")}
                res["source"], res["model"] = "recorded", f"{rec['model']} (recorded {self.recorded.recorded_at[:10]})"
                recorded_judge = rec.get("judge")
            else:
                res = answer_question(case, assessment, question.strip(), history, notes, None)
                res["warnings"] = ["This question is not in the recorded run, so the rules engine answered it. Add an API key to ask the model."] + res["warnings"]
        else:
            res = answer_question(case, assessment, question.strip(), history, notes, gen)
        event = self.store.add_event(cid, "chat", {"role": "assistant", "content": res["answer"], "citations": res["citations"],
                                                   "source": res["source"], "model": res["model"], "checks": res["checks"],
                                                   "warnings": res["warnings"], "mode": mode, "usage": res.get("usage")}, "ai")
        if recorded_judge:
            self.store.add_event(cid, "chat_judge", {"message_id": event["id"], "report": recorded_judge, "recorded": True}, "ai")
        return {**res, "message_id": event["id"], "judge_available": mode == "ai" and bool(self.judge) and self.settings.judge_chat}

    def judge_chat_message(self, cid: str, message_id: int) -> dict:
        case = self.queue.cases[cid]
        state = self.case_state(cid)
        msgs = state["chat"]
        idx = next((i for i, m in enumerate(msgs) if m["id"] == message_id and m["role"] == "assistant"), None)
        if idx is None:
            raise KeyError(message_id)
        question = next((m["content"] for m in reversed(msgs[:idx]) if m["role"] == "user"), "")
        report = judge_answer(case, question, msgs[idx]["content"], msgs[idx].get("checks") or run_rule_checks_chat(case, msgs[idx]["content"]),
                              self.judge if (self.settings.judge_chat and msgs[idx].get("mode") == "ai") else None)
        self.store.add_event(cid, "chat_judge", {"message_id": message_id, "report": report}, "ai")
        return report

    def regenerate(self, cid: str, mode: str = "offline") -> dict:
        return self.ensure_assessment(cid, mode, force=True)

    def metrics(self) -> dict:
        rows = self.queue_view()["cases"]
        events = self.store.events()
        decided = [r for r in rows if r["decision"] in ("accept", "reject")]
        accepts = sum(1 for r in decided if r["decision"] == "accept")
        reasons: dict[str, int] = {}
        for r in decided:
            if r["decision"] == "reject":
                code = self.case_state(r["case_id"])["reason_code"] or "unspecified"
                reasons[code] = reasons.get(code, 0) + 1
        statuses: dict[str, int] = {}
        for r in rows:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        fb = [e for e in events if e["type"] == "feedback"]
        fb_latest: dict[tuple[str, str], str] = {}
        for e in fb:
            fb_latest[(e["case_id"], e["payload"]["evidence_id"])] = e["payload"]["verdict"]
        verdicts: dict[str, int] = {}
        sources: dict[str, int] = {}
        revisions = 0
        for a in self._assessments["ai" if self.ai_available else "offline"].values():
            verdicts[a["quality"]["verdict"]] = verdicts.get(a["quality"]["verdict"], 0) + 1
            sources[a["source"]] = sources.get(a["source"], 0) + 1
            revisions += a.get("revisions", 0)
        chat_judged = [e for e in events if e["type"] == "chat_judge"]
        chat_verdicts: dict[str, int] = {}
        for e in chat_judged:
            v = e["payload"]["report"]["verdict"]
            chat_verdicts[v] = chat_verdicts.get(v, 0) + 1
        return {
            "decided": len(decided),
            "accepted": accepts,
            "overridden": len(decided) - accepts,
            "agreement_rate": round(accepts / len(decided), 3) if decided else None,
            "statuses": statuses,
            "override_reasons": reasons,
            "indicator_feedback": {
                "agree": sum(1 for v in fb_latest.values() if v == "agree"),
                "disagree": sum(1 for v in fb_latest.values() if v == "disagree"),
            },
            "assessment_verdicts": verdicts,
            "assessment_sources": sources,
            "revisions": revisions,
            "chat_verdicts": chat_verdicts,
            "open": sum(1 for r in rows if _undecided(r)),
        }

    def export_markdown(self, cid: str, mode: str = "offline") -> str:
        v = self.case_view(cid, mode)
        case, a, state = v["case"], v["assessment"], v["state"]
        f = case["facts"]
        lines = [f"# Case file {cid}", "",
                 f"Claim {f['claim_number']} | {f['care_type']} | {f['state']} | {f['claim_date']} | ${f['claim_amount_usd']:,}", "",
                 f"**Decision:** {state['decision_label']}" + (f" (risk rating {LANES[state['final_lane']]['label']}; reason: {state['reason_code']})" if state['decision'] == 'reject' else "") + "  ",
                 f"**Status:** {state['status_label']}  ",
                 f"**Risk rating:** {case['lane_label']} (risk score {case['risk_score']}/100, engine confidence {case['confidence']['level']}"
                 + (f"; lowered to {a['confidence']['level']} because the quality review did not pass the narrative" if a and a['confidence']['level'] != case['confidence']['level'] else "")
                 + ")  ",
                 f"**Engine:** v{case['engine_version']}, triaged {self.triaged_at}  ",
                 f"**Narrative source:** {a['source'] if a else 'pending'} ({a['generator'] if a else ''}); judge: {a['judge'] if a else ''}; quality verdict: {a['quality']['verdict'] if a else ''}", ""]
        if a:
            n = a["narrative"]
            lines += [("## AI assessment" if a.get("source") == "llm" else "## Assessment (rules template, no model)"), "", n["summary"], "", f"**Recommended action:** {n['recommended_action']}", ""]
            lines += ["### Key indicators", ""] + [f"- [{i['evidence_id']}] ({i['direction']}) {i['statement']}" for i in n["key_indicators"]] + [""]
            lines += ["### Next steps", ""] + [f"1. {s}" for s in n["next_steps"]] + [""]
            if n["uncertainties"]:
                lines += ["### Uncertainties", ""] + [f"- {u}" for u in n["uncertainties"]] + [""]
        lines += ["## Evidence", ""]
        for e in v["evidence"]:
            lines.append(f"- **{e['id']}** {e['label']}" + (f" ({e['reading']})" if e.get("reading") else "") + f": {e['text']}")
            if e.get("source"):
                lines.append(f"  - Source: {e['source']}")
            if e.get("context"):
                lines.append(f"  - Read together: {e['context']}")
            for k, name in (("points_to", "Points to"), ("assumption", "Assumption"), ("verify", "Settled by")):
                if e.get(k):
                    lines.append(f"  - {name}: {e[k]}")
            for c in e.get("counter_arguments") or []:
                lines.append(f"  - Counter-argument: {c}")
        lines.append("")
        if case["guardrails"]:
            lines += ["## Safety rules applied", ""] + [f"- {g['text']}" for g in case["guardrails"]] + [""]
        if state["decisions"]:
            lines += ["## Decisions and status changes", ""] + [f"- {d['ts']} {d['actor']}: {d['summary']}" for d in state["decisions"]] + [""]
        if case["data_gaps"]:
            lines += ["## What this file cannot show", ""] + [f"- {g}" for g in case["data_gaps"]] + [""]
        if state["notes"]:
            lines += ["## Notes", ""] + [f"- {n['ts']} {n['actor']}: {n['text']}" for n in state["notes"]] + [""]
        if state["feedback"]:
            lines += ["## Indicator feedback", ""] + [f"- {k}: {fb['verdict']}" + (f" ({fb['comment']})" if fb.get("comment") else "") for k, fb in state["feedback"].items()] + [""]
        if state["chat"]:
            lines += ["## Questions and answers", ""]
            for m in state["chat"]:
                who = "Investigator" if m["role"] == "user" else "AI"
                lines.append(f"**{who}:** {m['content']}")
                if m.get("judge"):
                    lines.append(f"_Judge verdict: {m['judge']['verdict']}_")
                lines.append("")
        return "\n".join(lines)

    def reset(self) -> None:
        self.store.clear_events()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def _family(cfg) -> str:
    return catalog.vendor_of(cfg.provider, cfg.model, cfg.base_url)


def run_rule_checks_chat(case: dict, answer: str) -> dict:
    from .quality.checks import run_chat_checks

    return run_chat_checks(answer, case, evidence_pack(case))


def _event_summary(e: dict) -> str:
    p = e["payload"]
    t = e["type"]
    if t in ("decision", "status"):
        return wf.event_summary(t, p, LANES)
    if t == "note":
        return f"Note: {p['text'][:120]}"
    if t == "feedback":
        return f"Marked {p['evidence_id']} as {p['verdict']}"
    if t == "chat":
        return ("Asked: " if p["role"] == "user" else "AI answered: ") + p["content"][:100]
    return t


def _undecided(row: dict) -> bool:
    return row["decision"] in ("pending", "needs_evidence") and not row.get("closed")


def _minutes_saved(rows: list[dict]) -> int:
    """Assumption: manual review takes about 20 minutes per case; a triaged likely-false-positive
    takes about 4 minutes to confirm and close, and a triaged review case about 12 minutes."""
    saved = 0
    for r in rows:
        if r["lane"] == "likely_fp":
            saved += 16
        elif r["lane"] == "review":
            saved += 8
    return saved
