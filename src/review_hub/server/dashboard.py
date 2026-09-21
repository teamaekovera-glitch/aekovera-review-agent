"""Server-rendered operator dashboard: Jinja2 pages over the JSON API.

The templates adapt to the API, never the reverse: every page renders data
through the same pure serializers the JSON routes use (``run_brief``,
``run_detail``, ``record_view``, ``manual_request_view``, the raw hold rows)
and the vanilla-JS layer (``static/dashboard.js``) polls the same JSON
endpoints for live state. No new API surface is required, so the JSON API is
untouched; the only additive seam is the ``dashboard`` key on the ``/`` index
body so API clients can discover the UI.

Screens: run overview (create + monitor, per-record progress), the
awaiting_manual paste box (the operator's primary loop: prompt surfaced,
paste, invalid-paste re-park shown clearly), queues (manual-review and held,
each hold with its reason, resolvable), accepted companies, supplier history
with export downloads, per-record audit inspection, and settings (read +
safe edit through the API's validated allowlist).

Local-first posture (locked decision): these pages assume the loopback bind
and no login, exactly like the JSON API - a single operator's machine is the
trust boundary. See ``server/auth.py`` for the access-token seam
(REVIEW_HUB_ACCESS_TOKEN) that must precede any non-loopback exposure, and
keep the bind on 127.0.0.1 (``python -m review_hub.server``) until then.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from review_hub.server.routes import manual_request_view, run_brief, run_detail
from review_hub.server.settings import (
    EDITABLE_SETTINGS,
    SECRET_SETTING_NAMES,
    redacted_settings,
)

# Templates and static files ship inside the package (see pyproject
# package-data) so a pip install is self-contained: no CDN, no external
# assets - the dashboard fully works on localhost without internet.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# The four hold surfaces, each with its reason: manual_review carries the
# reviewer-facing reason text, field_hold carries the structured lists of
# fields that never cleared / need review / were renamed (both in detail_json).
QUEUE_SECTIONS = (
    ("manual-review", "Manual review", "manual_review"),
    ("held", "Held for field review", "field_hold"),
)


def _state(request: Request) -> Any:
    return request.app.state


def _open_holds(state: Any) -> dict[str, list[dict[str, Any]]]:
    holds = state.store.holds(status="open")
    return {
        "manual_review": [h for h in holds if h.get("kind") == "manual_review"],
        "field_hold": [h for h in holds if h.get("kind") == "field_hold"],
    }


def _export_links(state: Any) -> list[dict[str, str]]:
    """The v34-parity exports as (name, href) display pairs."""
    specs: dict[str, Any] = state.export_specs
    return [{"name": name, "href": f"/export/{name}"} for name in specs]


def make_dashboard_router() -> APIRouter:
    router = APIRouter(prefix="/dashboard", include_in_schema=False)

    # ---------------------------------------------------------------- #
    # Overview: runs (create + monitor) and queue summary
    # ---------------------------------------------------------------- #
    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def overview(request: Request):
        state = _state(request)
        holds = _open_holds(state)
        context = {
            "runs": [run_brief(row, state.store, state.manager) for row in state.store.runs()],
            "open_manual_review": holds["manual_review"],
            "open_field_holds": holds["field_hold"],
            "backends": list(state.backend_names),
        }
        return templates.TemplateResponse(request, "overview.html", context)

    # ---------------------------------------------------------------- #
    # Run monitor: per-record progress, controls, the paste box
    # ---------------------------------------------------------------- #
    @router.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: str, request: Request):
        state = _state(request)
        row = state.store.get_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        detail = run_detail(row, state.store, state.manager)
        # The paste box is the operator's primary loop: the pending request
        # (prompt + any re-park error) renders server-side; polling keeps it
        # live through static/dashboard.js against the same API payload.
        detail["manual_request"] = manual_request_view(state.store.pending_manual_request(run_id))
        return templates.TemplateResponse(request, "run.html", {"run": detail})

    # ---------------------------------------------------------------- #
    # Queues: manual-review and held, each hold with its reason
    # ---------------------------------------------------------------- #
    @router.get("/queues", response_class=HTMLResponse)
    def queues(request: Request):
        state = _state(request)
        holds = _open_holds(state)
        return templates.TemplateResponse(
            request,
            "queues.html",
            {
                "sections": [
                    {"queue": queue, "title": title, "holds": holds[kind]}
                    for queue, title, kind in QUEUE_SECTIONS
                ]
            },
        )

    # ---------------------------------------------------------------- #
    # Inspection: accepted companies, history + exports, audit
    # ---------------------------------------------------------------- #
    @router.get("/accepted", response_class=HTMLResponse)
    def accepted(request: Request):
        state = _state(request)
        return templates.TemplateResponse(
            request,
            "accepted.html",
            {
                "accepted": state.store.accepted_rows(),
                "exports": _export_links(state),
            },
        )

    @router.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        state = _state(request)
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "history": state.store.history_rows(),
                "exports": _export_links(state),
            },
        )

    @router.get("/audit", response_class=HTMLResponse)
    def audit_lookup(request: Request, record_id: str = ""):
        record_id = (record_id or "").strip()
        if record_id:
            return RedirectResponse(f"/dashboard/audit/{record_id}", status_code=303)
        return templates.TemplateResponse(request, "audit.html", {"record_id": ""})

    @router.get("/audit/{record_id}", response_class=HTMLResponse)
    def audit(record_id: str, request: Request):
        state = _state(request)
        return templates.TemplateResponse(
            request,
            "audit.html",
            {
                "record_id": record_id,
                "events": state.store.audit_events(record_id),
                "evidence": state.store.evidence_for(record_id),
            },
        )

    # ---------------------------------------------------------------- #
    # Settings: read + safe edit (the API validates; the page never
    # invents keys). Secrets render as presence badges, never values.
    # ---------------------------------------------------------------- #
    @router.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        # Settings are process-global (the engine's config module), not
        # per-store state; the API's validated allowlist is the single
        # source of what may be read and edited here.
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "keys": list(EDITABLE_SETTINGS),
                "secrets": sorted(SECRET_SETTING_NAMES),
                "values": redacted_settings(),
            },
        )

    return router


__all__ = ["make_dashboard_router", "templates"]
