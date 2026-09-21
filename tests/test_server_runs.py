"""Run-execution tests over HTTP: paste e2e, pause/resume/cancel, adoption.

Only the Playwright session is faked (and, where noted, the research
backend); the runner, lifecycle, manual paste box, store, and decision gate
are all real. Every status assertion goes through the API, and the API reads
the SQLite store - the single source of truth.
"""

from __future__ import annotations

import json
import threading

from conftest import reject_result, wait_for_pending_request, wait_for_status
from test_runner import accept_result

from review_hub.store.repository import ReviewStore


def paste_for(result: dict) -> str:
    """The operator pastes ChatGPT's raw response: prose around the JSON."""
    return "Here is the review you asked for:\n" + json.dumps(result) + "\nHope that helps!"


class BlockingBackend:
    """Research that blocks until the test releases it, then answers REJECT."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.calls = 0

    def release(self) -> None:
        self._event.set()

    def research(self, _prompt: str, _system: str, *, extra_fields: dict | None = None) -> dict:
        self.calls += 1
        self._event.wait(timeout=15)
        return accept_result(decision="REJECT", scope_match=False)


# --------------------------------------------------------------------- #
# Manual paste end-to-end
# --------------------------------------------------------------------- #
def test_manual_paste_finalizes_end_to_end(make_hub):
    with make_hub() as (client, factory, _path):
        created = client.post("/api/runs", json={"backend": "manual", "limit": 1})
        assert created.status_code == 201
        run_id = created.json()["run_id"]
        assert created.json()["status"] in ("queued", "running")

        run = wait_for_status(client, run_id, "awaiting_manual")
        assert run["processed"] == 0

        request = client.get(f"/api/runs/{run_id}/manual-response").json()["request"]
        assert request is not None and request["pending"] is True
        assert request["record_id"] == "MST-2001"
        assert "Acme Rice" in (request["prompt"] or "")

        answered = client.post(
            f"/api/runs/{run_id}/manual-response",
            json={"raw_response": paste_for(reject_result())},
        )
        assert answered.status_code == 202

        run = wait_for_status(client, run_id, "completed")
        records = client.get(f"/api/runs/{run_id}/records", params={"status": "reject"}).json()
        assert [r["record_id"] for r in records["records"]] == ["MST-2001"]
        assert records["records"][0]["finalized"] is True
        assert records["records"][0]["decision"] == "REJECT"
        # The engine really executed: the QA app holds the submitted verdict
        # (DECISION_TO_VERDICT maps REJECT to the app's 'red' verdict).
        assert factory.qa_app.cards["101"]["verdict"] == "red"


def test_invalid_paste_reparks_without_counting_failure(make_hub):
    with make_hub() as (client, _factory, _path):
        run_id = client.post("/api/runs", json={"backend": "manual", "limit": 1}).json()["run_id"]
        wait_for_status(client, run_id, "awaiting_manual")

        reparked = client.post(
            f"/api/runs/{run_id}/manual-response",
            json={"raw_response": "this is not json at all"},
        )
        assert reparked.status_code == 202

        # The run re-parks with a fresh pending request (the operator can
        # simply re-paste); the reason is surfaced, not swallowed.
        request = wait_for_pending_request(client, run_id, timeout=15.0)
        assert "not usable" in (request["error"] or "")
        run = client.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "awaiting_manual"
        assert run["status_reason"] != "research_failure"
        records = client.get(f"/api/runs/{run_id}/records").json()["records"]
        assert records == []  # nothing finalized by the bad paste

        # The operator can simply re-paste and the run completes.
        client.post(
            f"/api/runs/{run_id}/manual-response",
            json={"raw_response": paste_for(reject_result())},
        )
        wait_for_status(client, run_id, "completed")


def test_paste_endpoint_409_without_pending_request(make_hub):
    with make_hub() as (client, _factory, _path):
        run_id = client.post("/api/runs", json={"backend": "manual", "limit": 1}).json()["run_id"]
        # The run parks in awaiting_manual; after a valid paste there is no
        # pending request left, so an immediate second paste is refused.
        wait_for_status(client, run_id, "awaiting_manual")
        client.post(
            f"/api/runs/{run_id}/manual-response",
            json={"raw_response": paste_for(reject_result())},
        )
        wait_for_status(client, run_id, "completed")
        second = client.post(
            f"/api/runs/{run_id}/manual-response",
            json={"raw_response": paste_for(reject_result())},
        )
        assert second.status_code == 409


# --------------------------------------------------------------------- #
# Pause / resume / cancel over HTTP, state read back from the store
# --------------------------------------------------------------------- #
def test_pause_then_resume_over_http(make_hub):
    shared = BlockingBackend()
    with make_hub(backend_factories={"blocking": lambda store, run_id: shared}) as (
        client,
        _factory,
        path,
    ):
        # limit=2: the command applies at the boundary after record 1, the
        # same deterministic sequencing the engine's own lifecycle tests use.
        run_id = client.post("/api/runs", json={"backend": "blocking", "limit": 2}).json()["run_id"]
        wait_for_status(client, run_id, "running")  # research #1 is in flight
        assert client.post(f"/api/runs/{run_id}/pause").status_code == 200

        shared.release()  # research settles; the pause lands at the boundary
        paused = wait_for_status(client, run_id, "paused")
        assert paused["processed"] == 1  # the record finalized before the park

        store = ReviewStore(path)  # read the truth back independently
        assert store.run_status(run_id) == "paused"
        store.close()

        assert client.post(f"/api/runs/{run_id}/resume").status_code == 200
        completed = wait_for_status(client, run_id, "completed")
        assert completed["processed"] == 2  # the second record ran after resume


def test_cancel_over_http_stops_at_the_boundary(make_hub):
    shared = BlockingBackend()
    with make_hub(backend_factories={"blocking": lambda store, run_id: shared}) as (
        client,
        _factory,
        path,
    ):
        run_id = client.post("/api/runs", json={"backend": "blocking", "limit": 2}).json()["run_id"]
        wait_for_status(client, run_id, "running")
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200

        shared.release()  # cancel applies at the boundary after record 1 settles
        cancelled = wait_for_status(client, run_id, "cancelled")
        assert cancelled["processed"] == 1  # matching the engine's cancel semantics

        store = ReviewStore(path)
        assert store.run_status(run_id) == "cancelled"
        store.close()
        records = client.get(f"/api/runs/{run_id}/records", params={"status": "reject"}).json()
        assert [r["record_id"] for r in records["records"]] == ["MST-2001"]


# --------------------------------------------------------------------- #
# Startup adoption: a crashed run is reported truthfully and recovers
# without re-researching decided records
# --------------------------------------------------------------------- #
def test_startup_adoption_recovers_crashed_run_without_re_research(make_hub, tmp_path):
    db = tmp_path / "crashed.db"
    seed = ReviewStore(db)
    seed.record_run_start("adopt-1", mode="auto", run_count=5)
    seed.record_run_status("adopt-1", "running")
    seed.record_research(
        "adopt-1",
        "MST-2001",
        "Acme Rice",
        kind="supplier",
        decision="REJECT",
        result=reject_result(),
        finalized=True,
    )  # the crash happened right after this ledger row
    seed.close()  # ...and the process died here

    with make_hub(db_path=db) as (client, _factory, _path):
        # Boot reports the crashed run's persisted state, with no live thread.
        body = client.get("/api/runs/adopt-1")
        assert body.status_code == 200
        run = body.json()
        assert run["status"] == "running"
        assert run["thread_alive"] is False and run["adopted"] is True

        # Explicit recovery: resume rebuilds through from_store.
        assert client.post("/api/runs/adopt-1/resume").status_code == 200
        wait_for_status(client, "adopt-1", "awaiting_manual")

        # The decided record was NOT re-researched: still exactly one
        # finalized reject, and no prompt was ever recorded for it - the
        # worker skipped it (repeat guard) and parked on the next record.
        records = client.get("/api/runs/adopt-1/records", params={"status": "reject"}).json()
        assert [r["record_id"] for r in records["records"]] == ["MST-2001"]
        audit = client.get("/api/audit/MST-2001").json()
        assert [e for e in audit["events"] if e["kind"] == "prompt"] == []
