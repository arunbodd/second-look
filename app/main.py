"""FastAPI application: JSON API plus the single-page UI in app/static."""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import netguard
from .llm.providers import LLMError
from .service import TriageService
from .settings import load_settings

from . import __version__

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("jai")

# Fonts come from Google Fonts; everything else is served from this origin.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.setLevel(level)


class DecisionIn(BaseModel):
    decision: str | None = Field(None, description="accept | reject | needs_evidence | reset")
    action: str | None = Field(None, description="deprecated alias: accept | override | request_info | reopen | escalate")
    lane: str | None = None
    reason_code: str | None = None
    note: str = Field("", max_length=4000)


class StatusIn(BaseModel):
    status: str = Field(..., description="new | in_review | documentation_requested | escalated | closed_benign | closed_confirmed")
    note: str = Field("", max_length=4000)


LEGACY_ACTIONS = {"accept": "accept", "override": "reject", "request_info": "needs_evidence", "reopen": "reset"}


class NoteIn(BaseModel):
    text: str = Field(..., max_length=4000)


class FeedbackIn(BaseModel):
    evidence_id: str
    verdict: str = Field(..., description="agree | disagree | clear")
    comment: str = ""


class ChatIn(BaseModel):
    question: str = Field(..., max_length=2000)


class ModelsIn(BaseModel):
    writer: str | None = Field(None, description="catalogue id, 'offline', or omitted to keep the current writer")
    judge: str | None = Field(None, description="catalogue id, 'offline', or omitted to keep the current judge")


class ReviewIn(BaseModel):
    case_ids: list[str] | None = Field(None, description="cases to review; omitted = every case")


def create_app(service: TriageService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service or TriageService(load_settings())
        yield
        app.state.service.shutdown()

    configure_logging()
    app = FastAPI(title="Second Look", version=__version__, lifespan=lifespan)
    allowed = netguard.allowed_hosts()  # read once; see app/netguard.py

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Request id, access log line, and security headers on every response."""
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        blocked = netguard.check(request.method, request.headers.get("host"), request.headers.get("origin"),
                                 request.headers.get("sec-fetch-site"), allowed)
        try:
            if blocked:
                log.warning("blocked rid=%s %s %s host=%s origin=%s: %s", rid, request.method, request.url.path,
                            request.headers.get("host"), request.headers.get("origin"), blocked)
                response = JSONResponse({"detail": blocked, "request_id": rid}, status_code=403)
            else:
                response = await call_next(request)
        except Exception:  # noqa: BLE001 - log and return a clean 500 with the request id
            log.exception("unhandled error rid=%s %s %s", rid, request.method, request.url.path)
            response = JSONResponse({"detail": "Internal error. Quote this id when reporting it.", "request_id": rid}, status_code=500)
        ms = (time.perf_counter() - t0) * 1000
        if not request.url.path.startswith("/static"):
            log.info("rid=%s %s %s %s %.0fms", rid, request.method, request.url.path, response.status_code, ms)
        h = response.headers
        h["X-Request-ID"] = rid
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        h.setdefault("Content-Security-Policy", CSP)
        if request.url.scheme == "https":
            h["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """Liveness: the process is up."""
        return {"ok": True, "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        """Readiness: a dataset is triaged and the event store answers."""
        try:
            s = svc()
            s.store.events("__readyz__")
            return {"ok": True, "cases": len(s.queue.order), "dataset": s.dataset["name"], "ai_available": s.ai_available}
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": type(exc).__name__}, status_code=503)

    def svc() -> TriageService:
        return app.state.service

    def actor_from(header: str | None) -> str:
        return (header or "investigator").strip()[:60] or "investigator"

    def case_or_404(cid: str) -> None:
        if cid not in svc().queue.cases:
            raise HTTPException(404, f"Unknown case {cid}")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/meta")
    def meta():
        return svc().meta()

    @app.post("/api/dataset")
    async def upload_dataset(request: Request, filename: str = Query("upload.csv")):
        """Upload a CSV (raw body, text/csv) with the same columns as the sample and triage it."""
        raw = await request.body()
        if len(raw) > 10 * 1024 * 1024:
            raise HTTPException(413, "CSV larger than 10 MB.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(400, "The file is not UTF-8 text.")
        try:
            return svc().upload_dataset(text, filename)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/dataset/sample")
    def load_sample():
        return svc().load_sample_dataset()

    @app.get("/api/models")
    def models():
        return svc().models()

    @app.post("/api/models")
    def set_models(body: ModelsIn):
        """Switch the writer and/or judge model at runtime (keys come from the environment)."""
        try:
            return svc().set_models(body.writer, body.judge)
        except (ValueError, LLMError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/review/run")
    def run_review(body: ReviewIn | None = None):
        """Start the AI review for a set of cases. Nothing is sent to a model before this call."""
        try:
            return svc().start_review(body.case_ids if body else None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/review/status")
    def review_status():
        return svc().review_status()

    @app.post("/api/review/stop")
    def stop_review():
        return svc().stop_review()

    @app.get("/api/queue")
    def queue(mode: str = Query("offline", description="offline | ai")):
        return svc().queue_view(mode)

    @app.get("/api/analysis")
    def analysis(mode: str = Query("offline")):
        """How each column drives the risk score for the loaded dataset."""
        return svc().analysis(mode)

    @app.get("/api/queue/values")
    def queue_values():
        return svc().signal_values()

    @app.get("/api/briefing")
    def briefing():
        return svc().briefing()

    @app.get("/api/metrics")
    def metrics():
        return svc().metrics()

    @app.get("/api/cases/{cid}")
    def case(cid: str, mode: str = Query("offline"), wait: bool = Query(False, description="Block until the assessment is ready")):
        case_or_404(cid)
        if wait:
            svc().ensure_assessment(cid, mode)
        return svc().case_view(cid, mode)

    @app.post("/api/cases/{cid}/assessment")
    def regenerate(cid: str, mode: str = Query("offline")):
        case_or_404(cid)
        return svc().regenerate(cid, mode)

    @app.post("/api/cases/{cid}/decision")
    def decision(cid: str, body: DecisionIn, x_investigator: str | None = Header(default=None)):
        """The investigator's call on the AI finding."""
        case_or_404(cid)
        actor = actor_from(x_investigator)
        try:
            if body.decision is None and body.action == "escalate":
                return svc().set_status(cid, "escalated", actor, body.note)
            decision = body.decision or LEGACY_ACTIONS.get(body.action or "", body.action or "")
            return svc().decide(cid, decision, actor, body.lane, body.reason_code, body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/cases/{cid}/status")
    def status(cid: str, body: StatusIn, x_investigator: str | None = Header(default=None)):
        """Move the case through the workflow."""
        case_or_404(cid)
        try:
            return svc().set_status(cid, body.status, actor_from(x_investigator), body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/cases/{cid}/notes")
    def note(cid: str, body: NoteIn, x_investigator: str | None = Header(default=None)):
        case_or_404(cid)
        try:
            return svc().add_note(cid, body.text, actor_from(x_investigator))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/cases/{cid}/feedback")
    def feedback(cid: str, body: FeedbackIn, x_investigator: str | None = Header(default=None)):
        case_or_404(cid)
        try:
            return svc().feedback(cid, body.evidence_id, body.verdict, actor_from(x_investigator), body.comment)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/cases/{cid}/chat")
    def chat(cid: str, body: ChatIn, mode: str = Query("offline"), x_investigator: str | None = Header(default=None)):
        case_or_404(cid)
        if not body.question.strip():
            raise HTTPException(400, "Empty question.")
        return svc().chat(cid, body.question, actor_from(x_investigator), mode)

    @app.post("/api/cases/{cid}/chat/{message_id}/judge")
    def judge_message(cid: str, message_id: int):
        case_or_404(cid)
        try:
            return svc().judge_chat_message(cid, message_id)
        except KeyError:
            raise HTTPException(404, "Unknown message.")

    @app.get("/api/cases/{cid}/export.md", response_class=PlainTextResponse)
    def export(cid: str, mode: str = Query("offline")):
        case_or_404(cid)
        return svc().export_markdown(cid, mode)

    @app.get("/api/export.xlsx")
    def export_xlsx(mode: str = Query("offline")):
        """Excel workbook: summary with live formulas, queue, assessments, signals, score drivers, activity."""
        from .export_xlsx import build_workbook

        data = build_workbook(svc(), svc().resolve_mode(mode))
        name = svc().dataset["name"].rsplit(".", 1)[0] or "queue"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}_triage.xlsx"'})

    @app.post("/api/reset")
    def reset():
        svc().reset()
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
