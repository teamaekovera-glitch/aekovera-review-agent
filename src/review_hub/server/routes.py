"""The JSON API: routes that speak only to the store and the RunManager.

Reads are store reads - the database is the single source of truth for run
state, never thread memory. Writes go through the lifecycle or the manager.
Serializers below are pure so response shapes are testable and stable.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response as HTTPResponse
from pydantic import BaseModel, Field

from review_hub.lifecycle import RunLifecycleError
from review_hub.server.settings import (
    SettingsError,
    apply_updates,
    effective_settings,
    redacted_settings,
    save_overrides,
    validate_updates,
)


class RunCreate(BaseModel):
    mode: str = Field(default="approval", pattern="^(auto|approval)$")
    backend: str = Field(default="manual")
    limit: int = Field(default=5, ge=1)


class ManualResponse(BaseModel):
    raw_response: str = Field(min_length=1)


class ResolveRequest(BaseModel):
    note: str = ""
    outcome: str = ""


def _state(request: Request) -> Any:
    """The app-state namespace: store, manager, backend_names, overrides path."""
    return request.app.state


def _run_or_404(store: Any, run_id: str) -> dict[str, Any]:
    row = store.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    return row


def _live_processed(store: Any, run_id: str) -> int:
    """Processed count from the ledger, fresher than the run row's finish count."""
    return max(
        (int(row.get("processed_after") or 0) for row in store.ledger_rows(run_id)), default=0
    )


def record_view(row: dict[str, Any]) -> dict[str, Any]:
    """One ledger row as the dashboard's record feed entry."""
    return {
        "record_id": row.get("record_id"),
        "company_name": row.get("company_name"),
        "kind": row.get("kind"),
        "decision": row.get("decision"),
        "finalized": bool(row.get("finalized")),
        "result": row.get("result") or {},
        "processed_after": row.get("processed_after"),
        "recorded_at": row.get("recorded_at"),
        "run_id": row.get("run_id"),
    }


def manual_request_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """The paste box as the operator sees it - never the raw pasted payload."""
    if row is None:
        return None
    return {
        "request_id": row.get("request_id"),
        "run_id": row.get("run_id"),
        "record_id": row.get("record_id"),
        "company_name": row.get("company_name"),
        "prompt": row.get("prompt"),
        "error": row.get("error"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "answered_at": row.get("answered_at"),
        "consumed_at": row.get("consumed_at"),
        "pending": row.get("status") == "pending",
    }


def run_brief(row: dict[str, Any], store: Any, manager: Any) -> dict[str, Any]:
    run_id = str(row.get("run_id"))
    return {
        "run_id": run_id,
        "mode": row.get("mode"),
        "status": row.get("status"),
        "status_reason": row.get("status_reason"),
        "run_count": row.get("run_count"),
        "processed": max(_live_processed(store, run_id), int(row.get("records_processed") or 0)),
        "final_state": row.get("final_state"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        **manager.supervisor_view(run_id),
    }


def run_detail(row: dict[str, Any], store: Any, manager: Any) -> dict[str, Any]:
    run_id = str(row.get("run_id"))
    return {
        **run_brief(row, store, manager),
        "decision_tally": row.get("decision_tally") or {},
        "status_events": store.run_status_events(run_id),
        "transitions": store.transitions(run_id),
        "records": [record_view(r) for r in store.ledger_rows(run_id)],
        "manual_request": manual_request_view(store.pending_manual_request(run_id)),
        "summary": row.get("summary") or {},
    }


def _lifecycle(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return action()
    except RunLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def make_api_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    # ---------------------------------------------------------------- #
    # Run management
    # ---------------------------------------------------------------- #
    @router.post("/runs", status_code=201)
    def create_run(payload: RunCreate, request: Request) -> dict[str, Any]:
        state = _state(request)
        if payload.backend not in state.backend_names:
            expected = ", ".join(sorted(state.backend_names))
            raise HTTPException(
                status_code=422,
                detail=f"unknown research backend {payload.backend!r}; expected one of {expected}",
            )
        cap = int(effective_settings()["MAX_RECORDS"])
        if payload.limit > cap:
            raise HTTPException(
                status_code=422,
                detail=f"limit {payload.limit} exceeds the MAX_RECORDS safety cap ({cap}); "
                "raise it in settings first",
            )
        run_id = state.manager.start_run(
            mode=payload.mode, backend=payload.backend, limit=payload.limit
        )
        return {"run_id": run_id, "status": state.store.run_status(run_id), **payload.model_dump()}

    @router.get("/runs")
    def list_runs(request: Request) -> dict[str, Any]:
        state = _state(request)
        return {"runs": [run_brief(row, state.store, state.manager) for row in state.store.runs()]}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        return run_detail(_run_or_404(state.store, run_id), state.store, state.manager)

    @router.post("/runs/{run_id}/pause")
    def pause_run(run_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        _run_or_404(state.store, run_id)
        return _lifecycle(lambda: state.manager.pause(run_id))

    @router.post("/runs/{run_id}/resume")
    def resume_run(run_id: str, request: Request, backend: str | None = None) -> dict[str, Any]:
        state = _state(request)
        _run_or_404(state.store, run_id)
        return _lifecycle(lambda: state.manager.resume(run_id, backend=backend))

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        _run_or_404(state.store, run_id)
        return _lifecycle(lambda: state.manager.cancel(run_id))

    @router.get("/runs/{run_id}/records")
    def run_records(run_id: str, request: Request, status: str | None = None) -> dict[str, Any]:
        state = _state(request)
        _run_or_404(state.store, run_id)
        rows = state.store.ledger_rows(run_id)
        if status:
            wanted = status.strip().lower()
            rows = [
                r
                for r in rows
                if wanted
                in {str(r.get("decision") or "").lower(), str(r.get("kind") or "").lower()}
            ]
        return {"records": [record_view(r) for r in rows]}

    @router.get("/runs/{run_id}/transitions")
    def run_transitions(run_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        _run_or_404(state.store, run_id)
        return {"transitions": state.store.transitions(run_id)}

    # ---------------------------------------------------------------- #
    # The manual paste box
    # ---------------------------------------------------------------- #
    @router.get("/runs/{run_id}/manual-response")
    def get_manual_response(run_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        _run_or_404(state.store, run_id)
        return {"request": manual_request_view(state.store.pending_manual_request(run_id))}

    @router.post("/runs/{run_id}/manual-response", status_code=202)
    def post_manual_response(
        run_id: str, payload: ManualResponse, request: Request
    ) -> dict[str, Any]:
        """Consume the operator's paste exactly once, then resume the run.

        The paste is persisted BEFORE any thread reads it; an invalid paste
        re-parks the run in awaiting_manual with the reason attached - no
        research failure is recorded and no failure counter advances.
        """
        state = _state(request)
        _run_or_404(state.store, run_id)
        if state.store.pending_manual_request(run_id) is None:
            raise HTTPException(status_code=409, detail=f"run {run_id} has no pending manual request")
        state.store.submit_manual_response(run_id, payload.raw_response)
        resumed = _lifecycle(lambda: state.manager.resume(run_id, backend="manual"))
        return {"status": "answered", "resumed": resumed.get("resumed", False)}

    # ---------------------------------------------------------------- #
    # Queues
    # ---------------------------------------------------------------- #
    @router.get("/queues/decisions")
    def decisions_queue(request: Request, limit: int = 50) -> dict[str, Any]:
        state = _state(request)
        rows = state.store.ledger_rows(finalized=True)[: max(0, min(limit, 500))]
        return {"decisions": [record_view(r) for r in rows]}

    @router.get("/queues/manual-review")
    def manual_review_queue(request: Request) -> dict[str, Any]:
        state = _state(request)
        open_holds = [h for h in state.store.holds(status="open") if h.get("kind") == "manual_review"]
        return {"holds": open_holds}

    @router.get("/queues/held")
    def held_queue(request: Request) -> dict[str, Any]:
        state = _state(request)
        open_holds = [h for h in state.store.holds(status="open") if h.get("kind") == "field_hold"]
        return {"holds": open_holds}

    @router.post("/queues/{queue}/{hold_id}/resolve")
    def resolve_queue_hold(
        queue: str, hold_id: str, payload: ResolveRequest, request: Request
    ) -> dict[str, Any]:
        """Resolve a hold; an explicit outcome is preserved in the note prefix."""
        state = _state(request)
        if queue not in ("manual-review", "held"):
            raise HTTPException(status_code=404, detail=f"unknown queue {queue!r}")
        note = f"[{payload.outcome}] {payload.note}" if payload.outcome else payload.note
        if not state.store.resolve_hold(hold_id, note):
            raise HTTPException(status_code=404, detail=f"unknown hold {hold_id}")
        return {"resolved": hold_id}

    # ---------------------------------------------------------------- #
    # Inspection: history, accepted companies, audit
    # ---------------------------------------------------------------- #
    @router.get("/history")
    def history(request: Request, supplier: str | None = None) -> dict[str, Any]:
        state = _state(request)
        rows = state.store.history_rows()
        if supplier:  # store read has no filter param; narrow here
            needle = supplier.casefold()
            rows = [
                r
                for r in rows
                if needle in str(r.get("company_name") or "").casefold()
            ]
        return {"history": rows}

    @router.get("/accepted-companies")
    def accepted_companies(request: Request) -> dict[str, Any]:
        state = _state(request)
        return {"accepted": state.store.accepted_rows()}

    @router.get("/audit/{record_id}")
    def audit_record(record_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        return {
            "record_id": record_id,
            "events": state.store.audit_events(record_id),
            "evidence": state.store.evidence_for(record_id),
        }

    # ---------------------------------------------------------------- #
    # Settings
    # ---------------------------------------------------------------- #
    @router.get("/settings")
    def get_settings(request: Request) -> dict[str, Any]:
        _ = request  # route signature consistency; settings are process-global
        return redacted_settings()

    @router.put("/settings")
    def put_settings(request: Request, updates: dict[str, Any] = Body(...)) -> dict[str, Any]:
        state = _state(request)
        try:
            validated = validate_updates(updates)
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        apply_updates(validated)
        save_overrides(state.settings_overrides_path, validated)
        return redacted_settings()

    return router


def make_export_router(exports: dict[str, tuple[str, str, Callable[..., Any]]]) -> APIRouter:
    """Serve the v34-parity workbooks and CSV logs with their content types.

    Export builders take ``(store, path)`` and write a file; the route renders
    to a temp file per request and serves the bytes - no caches, no staleness.
    """
    router = APIRouter(prefix="/export")

    def render(builder: Callable[..., Any], store: Any) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export"
            builder(store, path)
            return path.read_bytes()

    @router.get("/{export_name}")
    def download(export_name: str, request: Request) -> HTTPResponse:
        spec = exports.get(export_name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown export {export_name!r}")
        media, filename, builder = spec
        store = _state(request).store
        return HTTPResponse(
            content=render(builder, store),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router
