"""Shared fixtures for the server test files."""

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from fake_session import FakeSessionFactory
from fastapi.testclient import TestClient
from test_runner import accept_result

from review_hub.server import create_app


# The one valid paste shape the decision gate accepts, flipped to REJECT so
# no snapshot/website verification is exercised on the finalize path.
def reject_result(**over):
    return accept_result(decision="REJECT", scope_match=False, reason="trading company", **over)


@pytest.fixture()
def make_hub(tmp_path):
    """A live server + client + fake session factory over a throwaway db.

    Returns a context manager yielding (client, factory, db_path); lifespan
    startup (adoption) and shutdown (join/close) run inside the ``with``.
    """

    @contextmanager
    def _make(db_path=None, **create_kwargs):
        factory = FakeSessionFactory()
        path = db_path if db_path is not None else tmp_path / "hub.db"
        app = create_app(path, session_factory=factory, **create_kwargs)
        with TestClient(app) as client:
            yield client, factory, path

    return _make


def wait_for_status(client: TestClient, run_id: str, status: str, timeout: float = 10.0) -> dict:
    """Poll the API (never thread memory) until the run reports ``status``."""
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        body = response.json()
        if response.status_code == 200 and body.get("status") == status:
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {status!r}; last: {body}")


def wait_for_pending_request(client: TestClient, run_id: str, timeout: float = 10.0) -> dict:
    """Poll until a pending manual request exists.

    A run can sit in ``awaiting_manual`` from the PREVIOUS park while the
    resumed worker has not yet re-parked - status alone is stale evidence;
    the pending request is the paste box's actual readiness.
    """
    deadline = time.monotonic() + timeout
    request = None
    while time.monotonic() < deadline:
        request = client.get(f"/api/runs/{run_id}/manual-response").json()["request"]
        if request is not None and request["pending"] is True:
            return request
        time.sleep(0.05)
    raise AssertionError(f"no pending manual request appeared for {run_id}; last: {request}")
