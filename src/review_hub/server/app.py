"""App factory: wiring, lifespan (adoption + shutdown), and the index route.

Local-first posture (locked decision): ``python -m review_hub.server`` binds
127.0.0.1 - a single operator's machine is the trust boundary and there is
no login. See server/auth.py for the access-token seam and server/__main__.py
for the bind/host contract.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from review_hub.server import settings
from review_hub.server.auth import AccessTokenMiddleware, configured_token
from review_hub.server.dashboard import STATIC_DIR, make_dashboard_router
from review_hub.server.routes import make_api_router, make_export_router
from review_hub.server.runmanager import RunManager, engine_session
from review_hub.store.exports import (
    export_discovery_failures,
    export_field_holds,
    export_manual_reviews,
    export_non_us_origins,
    export_website_checks,
    write_accepted_workbook,
    write_history_workbook,
)
from review_hub.store.repository import ReviewStore

logger = logging.getLogger("review_hub.server")

_CSV = "text/csv"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# name -> (media type, download filename, builder(store, path))
EXPORT_SPECS: dict[str, tuple[str, str, Any]] = {
    "supplier-history.xlsx": (_XLSX, "supplier_history.xlsx", write_history_workbook),
    "accepted-companies.xlsx": (_XLSX, "accepted_companies.xlsx", write_accepted_workbook),
    "website-verify-log.csv": (_CSV, "website_verify_log.csv", export_website_checks),
    "manual-review-queue.csv": (_CSV, "manual_review_queue.csv", export_manual_reviews),
    "held-for-field-review.csv": (_CSV, "held_for_field_review.csv", export_field_holds),
    "field-discovery-failures.csv": (
        _CSV,
        "field_discovery_failures.csv",
        export_discovery_failures,
    ),
    "accepted-non-us.csv": (_CSV, "accepted_non_us.csv", export_non_us_origins),
}

DEFAULT_DB_PATH = Path("review_hub.db")


def default_db_path() -> Path:
    return Path(os.environ.get("REVIEW_HUB_DB", "") or DEFAULT_DB_PATH)


def create_app(
    db_path: str | Path | None = None,
    *,
    session_factory: Any = engine_session,
    backend_factories: dict[str, Any] | None = None,
    access_token: str | None = None,
) -> FastAPI:
    """Build the control-plane app.

    ``session_factory`` and ``backend_factories`` exist for tests (a fake
    browser session, a scripted research backend); production callers leave
    them defaulted. ``access_token`` overrides the environment token for
    tests; ``None`` reads REVIEW_HUB_ACCESS_TOKEN.
    """
    db_path = Path(db_path) if db_path else default_db_path()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Persisted settings overrides apply before anything builds a runner,
        # so adopted/resumed runs see the operator's edited configuration.
        overrides_path = settings.overrides_path(db_path)
        applied = settings.load_and_apply_overrides(overrides_path)
        if applied:
            logger.info("applied %d persisted settings override(s)", applied)
        store = ReviewStore(db_path, check_same_thread=False)
        manager = RunManager(
            store,
            db_path,
            session_factory=session_factory,
            backend_factories=backend_factories,
        )
        adopted = manager.adopt()
        app.state.store = store
        app.state.manager = manager
        app.state.backend_names = tuple(manager.backend_names())
        app.state.settings_overrides_path = overrides_path
        app.state.export_specs = EXPORT_SPECS
        logger.info("review hub ready (db=%s, adopted=%d)", db_path, len(adopted))
        yield
        manager.shutdown()
        store.close()

    app = FastAPI(title="Aekovera Review Hub", lifespan=lifespan)
    app.add_middleware(
        AccessTokenMiddleware,
        token=configured_token() if access_token is None else access_token,
    )
    app.include_router(make_api_router())
    app.include_router(make_export_router(EXPORT_SPECS))
    # The operator dashboard (additive): Jinja2 pages over the same JSON API.
    # The API itself is untouched - see server/dashboard.py.
    app.include_router(make_dashboard_router())
    app.mount("/dashboard/static", StaticFiles(directory=STATIC_DIR), name="dashboard-static")

    @app.get("/")
    def index() -> dict[str, str]:
        return {
            "service": "aekovera-review-hub",
            "api": "/api",
            "dashboard": "/dashboard",
            "exports": "/export/{name}",
            "posture": "localhost-only by default; set REVIEW_HUB_ACCESS_TOKEN "
            "and a non-loopback bind before exposing beyond this machine",
        }

    return app
