"""Run-lifecycle tests: parks, resume, crash recovery, and the paste box.

The lifecycle task's acceptance core:
- pause mid-batch -> resume -> identical final state as an uninterrupted run,
- crash-kill simulation -> restart -> no re-research of decided records,
- awaiting_manual end-to-end with a pasted raw response (JSON leak included),
- cancel semantics, repeat guard under resume, paused_for_login parking,
- the run-status state machine's legal edges and terminal enforcement.

A KeyboardInterrupt stands in for a killed process: it is a BaseException, so
the runner's failure handling never runs - the run row is left 'running'
exactly as a real kill would leave it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_runner import FakeBackend, FakeOps, accept_result, make_record

from review_hub.cli import build_persistence
from review_hub.engine.research.manual import PasteBoxBackend
from review_hub.engine.runner import BatchRunner, RunnerState
from review_hub.engine.transport import QASessionExpired
from review_hub.lifecycle import (
    RunLifecycle,
    RunLifecycleError,
    RunStatus,
    StoreManualGate,
)
from review_hub.persistence import FileTransitionSink
from review_hub.store.repository import ReviewStore

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------

def advancing_clock(base: datetime | None = None):
    """Clock returning later timestamps on every call (the store-test twin)."""
    state = {"n": 0}
    base = base or datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    def _clock() -> datetime:
        state["n"] += 1
        return base.replace(second=min(base.second + state["n"], 59))

    return _clock


@pytest.fixture()
def store(tmp_path: Path) -> ReviewStore:
    s = ReviewStore(tmp_path / "review.db", clock=advancing_clock())
    yield s
    s.close()


def silent(*args, **kwargs) -> None:
    return None


def make_runner(store, ops, backend, **kw):
    """A runner fully wired to the store: the store sink IS the persistence
    layer and the RunLifecycle tracks the run's status in it."""
    lifecycle = RunLifecycle(store)
    kw.setdefault("log", silent)
    kw.setdefault("build_prompt", lambda record: "PROMPT")
    ops.lifecycle = lifecycle  # the ops hooks can request pause/cancel
    runner = BatchRunner(
        ops=ops, backend=backend, sink=store.sink(), lifecycle=lifecycle, **kw
    )
    return runner, lifecycle


def paste_runner(store, run_id, records):
    """A runner whose backend is the store-backed paste box (manual default)."""
    lifecycle = RunLifecycle(store, run_id)
    runner = BatchRunner(
        ops=FakeOps(serve=[dict(r) for r in records]),
        backend=PasteBoxBackend(StoreManualGate(store, run_id)),
        sink=store.sink(),
        lifecycle=lifecycle,
        log=silent,
        build_prompt=lambda record: f"PROMPT-{record['record_id']}",
    )
    return runner, lifecycle


def paste_payload(decision: str, name: str) -> str:
    """A complete decision object as ChatGPT would emit it (raw text)."""
    return json.dumps(
        {
            "company_name": name,
            "decision": decision,
            "bucket": None,
            "scope_match": True,
            "site_identity": "confirmed",
            "supplier_type": "Food Manufacturer / Brand",
            "type_quote": None,
            "is_us_based": True,
            "supply_country": "United States",
            "confidence": 0.95,
            "reason": "clean paste",
            "changes": [],
            "needs": [],
        }
    )


LEAKY_PARK = (
    "Sure! Here is my analysis:\n```json\n"
    + paste_payload("PARK", "Supplier MST-1")
    + "\n```\nLet me know if you need anything else."
)


class CommandAfterSettle(FakeOps):
    """Requests a lifecycle command after the Nth settle (a safe boundary).

    The real operator surfaces (control plane, terminal signal) enqueue
    commands between records; this fires at the same boundary.
    """

    def __init__(self, serve, command: str, after: int = 1):
        super().__init__(serve=serve)
        self._command = command
        self._after = after
        self._settles = 0
        self.lifecycle = None  # wired by make_runner

    def wait_settle(self, page):
        self._settles += 1
        if self._settles == self._after and self.lifecycle is not None:
            if self._command == "pause":
                self.lifecycle.request_pause()
            elif self._command == "cancel":
                self.lifecycle.request_cancel()


class KillMidBatch:
    """Serves scripted results, then simulates a hard kill mid-research.

    KeyboardInterrupt is a BaseException: the runner's failure handling never
    runs, so the store is left exactly as a killed process leaves it - run
    row still 'running', decisions persisted up to the kill.
    """

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def research(self, prompt, system):
        self.calls += 1
        if self.calls <= len(self._results):
            return self._results[self.calls - 1]
        raise KeyboardInterrupt("simulated kill -9 mid-batch")


# ---------------------------------------------------------------------------
# The state machine: legal edges, parks, terminal enforcement
# ---------------------------------------------------------------------------

class TestRunStateMachine:
    def test_fresh_run_moves_queued_to_running_and_records_events(self, store):
        lifecycle = RunLifecycle(store, "run-machine-1")
        lifecycle.start(mode="auto", run_count=7)
        assert lifecycle.status() == RunStatus.RUNNING.value
        events = lifecycle.events()
        assert [e["to_status"] for e in events] == [RunStatus.RUNNING.value]
        assert events[0]["from_status"] == RunStatus.QUEUED.value
        assert "7 records" in events[0]["reason"]

    def test_park_states_move_back_to_running_on_resume(self, store):
        lifecycle = RunLifecycle(store, "run-machine-2")
        lifecycle.start()
        for park, move in (
            (RunStatus.PAUSED, lifecycle.pause),
            (RunStatus.PAUSED_FOR_LOGIN, lifecycle.pause_for_login),
            (RunStatus.AWAITING_MANUAL, lambda: lifecycle.await_manual(prompt="p")),
        ):
            move()
            assert lifecycle.status() == park.value
            lifecycle.resume()
            assert lifecycle.status() == RunStatus.RUNNING.value

    def test_terminal_states_have_no_outgoing_edges(self, store):
        lifecycle = RunLifecycle(store, "run-machine-3")
        lifecycle.start()
        lifecycle.complete({"run_id": "run-machine-3", "processed": 0})
        assert lifecycle.status() == RunStatus.COMPLETED.value
        with pytest.raises(RunLifecycleError):
            lifecycle.pause()
        with pytest.raises(RunLifecycleError):
            lifecycle.resume()

    def test_completing_a_paused_run_is_illegal(self, store):
        lifecycle = RunLifecycle(store, "run-machine-4")
        lifecycle.start()
        lifecycle.pause(reason="holding")
        with pytest.raises(RunLifecycleError):
            lifecycle.complete({"run_id": "run-machine-4", "processed": 0})

    def test_failing_a_run_records_the_failure_summary(self, store):
        lifecycle = RunLifecycle(store, "run-machine-5")
        lifecycle.start()
        lifecycle.fail(
            reason="boom", summary={"run_id": "run-machine-5", "processed": 2}
        )
        row = lifecycle.run_row()
        assert lifecycle.status() == RunStatus.FAILED.value
        assert row["final_state"] == "failed"
        assert row["records_processed"] == 2

    def test_resume_over_a_crash_left_stale_running_row_is_safe(self, store):
        # A killed process never writes its exit: the row still says running.
        lifecycle = RunLifecycle(store, "run-machine-6")
        lifecycle.start()
        lifecycle.resume(reason="crash-recovery restart")
        assert lifecycle.status() == RunStatus.RUNNING.value


# ---------------------------------------------------------------------------
# The store is the persistence layer (default); JSONL stays configurable
# ---------------------------------------------------------------------------

class TestStoreAsPersistenceLayer:
    def test_transitions_and_run_state_persist_in_the_store(self, store):
        runner, lifecycle = make_runner(
            store, FakeOps(serve=[make_record("MST-1")]), FakeBackend([accept_result()])
        )
        summary = runner.run(None, 1)
        assert summary["final_state"] == RunnerState.RUN_DONE.value
        assert store.count_transitions(runner.run_id, outcome="run started: 1 records") == 1
        assert store.count_transitions(runner.run_id, outcome="run resumed") == 0
        row = lifecycle.run_row()
        assert row["status"] == RunStatus.COMPLETED.value
        assert row["final_state"] == "run_done"
        assert row["records_processed"] == 1
        assert row["decision_tally"] == {"ACCEPT": 1}

    def test_default_persistence_is_the_store(self, tmp_path, monkeypatch):
        monkeypatch.delenv("REVIEW_HUB_SINK", raising=False)
        monkeypatch.setattr("review_hub.cli.RUN_LOG_DIR", str(tmp_path))
        sink, lifecycle = build_persistence()
        assert isinstance(lifecycle, RunLifecycle)
        assert lifecycle.run_id

    def test_jsonl_sink_still_selectable_for_debug(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REVIEW_HUB_SINK", "jsonl")
        monkeypatch.setattr("review_hub.cli.RUN_LOG_DIR", str(tmp_path))
        sink, lifecycle = build_persistence()
        assert lifecycle is None
        assert isinstance(sink, FileTransitionSink)

    def test_store_fallback_is_loud_not_silent(self, tmp_path, monkeypatch, capsys):
        # A store file that cannot be opened as a database (corrupted) must
        # make the CLI say so and fall back to the JSONL log - never silently.
        (tmp_path / "review.db").write_bytes(b"not a database at all")
        monkeypatch.delenv("REVIEW_HUB_SINK", raising=False)
        monkeypatch.setattr("review_hub.cli.RUN_LOG_DIR", str(tmp_path))
        sink, lifecycle = build_persistence()
        assert lifecycle is None
        assert isinstance(sink, FileTransitionSink)
        assert "SQLite store unavailable" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Pause mid-batch -> resume -> identical final state (the acceptance core)
# ---------------------------------------------------------------------------

class TestPauseResumeMidBatch:
    def test_resume_reaches_the_same_final_state_as_an_uninterrupted_run(self, store):
        records = [make_record(f"MST-{i}") for i in range(1, 4)]
        results = [accept_result(), accept_result(decision="PARK"), accept_result()]

        # Run A: uninterrupted.
        runner_a, _ = make_runner(
            store,
            FakeOps(serve=[dict(r) for r in records]),
            FakeBackend([dict(r) for r in results]),
        )
        summary_a = runner_a.run(None, 3)
        assert summary_a["final_state"] == RunnerState.RUN_DONE.value

        # Run B: pause requested after the first record settles.
        ops_b = CommandAfterSettle([dict(r) for r in records], "pause")
        backend_b = FakeBackend([dict(r) for r in results])
        runner_b, lifecycle_b = make_runner(store, ops_b, backend_b)
        parked = runner_b.run(None, 3)
        assert parked["final_state"] == RunnerState.PAUSED.value
        assert parked["processed"] == 1
        assert lifecycle_b.status() == RunStatus.PAUSED.value
        assert "paused at a safe boundary" in parked["pause_reason"]

        # Resume: the remaining records flow through the same loop.
        resumed = runner_b.resume(None)
        assert resumed["final_state"] == RunnerState.RUN_DONE.value
        for key in ("processed", "repeat_passes_total", "decision_tally"):
            assert resumed[key] == summary_a[key], key
        assert backend_b.calls == 3

        # One continuous history: the park and the resume are both in the log.
        assert [e["to_status"] for e in lifecycle_b.events()] == [
            "running",
            "paused",
            "running",
            "completed",
        ]


# ---------------------------------------------------------------------------
# Crash recovery: a killed process restarts from persisted transitions
# ---------------------------------------------------------------------------

class TestCrashRecovery:
    def test_restart_after_a_kill_never_rerearchs_decided_records(self, store):
        records = [make_record("MST-1"), make_record("MST-2"), make_record("MST-3")]
        ops = FakeOps(serve=[dict(r) for r in records])
        killer = KillMidBatch([accept_result()])  # MST-1 decided, then the kill
        runner, lifecycle = make_runner(store, ops, killer)
        with pytest.raises(KeyboardInterrupt):
            runner.run(None, 3)

        # The killed process never wrote its exit: the run row is still
        # 'running', with MST-1's decision safely persisted.
        assert lifecycle.status() == RunStatus.RUNNING.value
        assert [d["record_id"] for d in lifecycle.decisions()] == ["MST-1"]

        # Restart: the ledger says MST-1 is decided. The page re-serves it
        # (drift) alongside the remaining records - only MST-2 and MST-3 may
        # consume a research round-trip.
        ops2 = FakeOps(
            serve=[make_record("MST-2"), make_record("MST-1"), make_record("MST-3")]
        )
        backend2 = FakeBackend([accept_result(decision="PARK"), accept_result()])
        runner2 = BatchRunner.from_store(
            store,
            runner.run_id,
            ops2,
            backend2,
            log=silent,
            build_prompt=lambda r: "PROMPT",
        )
        resumed = runner2.resume(None)

        assert resumed["final_state"] == RunnerState.RUN_DONE.value
        assert backend2.calls == 2  # MST-1 reused from the ledger, never re-researched
        assert resumed["processed"] == 3  # replayed 1 + MST-2 + MST-3
        # The restart's final accounting matches the uninterrupted run. The
        # PARK arrived without a bucket, so the gate recorded it as "other"
        # and the tally key carries the bucket (the legacy shape).
        assert resumed["decision_tally"] == {"ACCEPT": 2, "PARK [other]": 1}
        # The ledger stays one-row-per-record: MST-1 was not decided twice.
        rows = runner2.lifecycle.decisions()
        assert len([r for r in rows if r["record_id"] == "MST-1"]) == 1
        assert runner2.lifecycle.status() == RunStatus.COMPLETED.value

    def test_repeat_guard_is_honored_on_resume(self, store):
        records = [make_record("MST-1"), make_record("MST-2")]
        ops = FakeOps(serve=[dict(r) for r in records])
        killer = KillMidBatch([accept_result()])
        runner, _ = make_runner(store, ops, killer)
        with pytest.raises(KeyboardInterrupt):
            runner.run(None, 2)

        # Restart with MST-1 re-served FIRST: the restored repeat guard
        # recognizes it and reuses the result without a round-trip.
        ops2 = FakeOps(serve=[make_record("MST-1"), make_record("MST-2")])
        backend2 = FakeBackend([accept_result(decision="PARK")])
        runner2 = BatchRunner.from_store(
            store,
            runner.run_id,
            ops2,
            backend2,
            log=silent,
            build_prompt=lambda r: "PROMPT",
        )
        summary = runner2.resume(None)

        assert summary["final_state"] == RunnerState.RUN_DONE.value
        assert backend2.calls == 1  # only MST-2; MST-1 hit the restored guard
        assert summary["processed"] == 2
        # No double-count: the tally carries the restored ACCEPT plus the
        # restart's PARK (bucketed "other" by the gate - the legacy shape).
        assert summary["decision_tally"] == {"ACCEPT": 1, "PARK [other]": 1}

    def test_research_budget_is_not_replayed_across_restart(self, store):
        # 2-record budget: MST-1 decided (1 processed), then the crash. The
        # restart must have exactly 1 record of budget left, not a fresh 2.
        records = [make_record("MST-1"), make_record("MST-2")]
        ops = FakeOps(serve=[dict(r) for r in records])
        killer = KillMidBatch([accept_result()])
        runner, _ = make_runner(store, ops, killer)
        with pytest.raises(KeyboardInterrupt):
            runner.run(None, 2)

        ops2 = FakeOps(
            serve=[make_record("MST-2"), make_record("MST-1"), make_record("MST-3")]
        )
        backend2 = FakeBackend([accept_result()])
        runner2 = BatchRunner.from_store(
            store,
            runner.run_id,
            ops2,
            backend2,
            log=silent,
            build_prompt=lambda r: "PROMPT",
        )
        summary = runner2.resume(None)
        # processed stops at the 2-record budget even though the page
        # offered a third record.
        assert summary["processed"] == 2
        assert backend2.calls == 1

    def test_from_store_with_an_unknown_run_raises(self, store):
        with pytest.raises(RuntimeError):
            BatchRunner.from_store(
                store, "run-nope", FakeOps(), FakeBackend(), log=silent
            )


# ---------------------------------------------------------------------------
# Awaiting manual: the paste-box flow (manual ChatGPT is the default backend)
# ---------------------------------------------------------------------------

class TestAwaitingManualPasteBox:
    def test_manual_chatgpt_paste_box_end_to_end(self, store):
        run_id = "run-paste-1"
        runner, lifecycle = paste_runner(
            store, run_id, [make_record("MST-1"), make_record("MST-2")]
        )

        # Nothing pasted yet: the run parks with the record's prompt surfaced.
        parked = runner.run(None, 2)
        assert parked["final_state"] == RunnerState.AWAITING_MANUAL.value
        assert parked["processed"] == 0
        assert lifecycle.status() == RunStatus.AWAITING_MANUAL.value
        box = store.pending_manual_request(run_id)
        assert box["record_id"] == "MST-1"
        assert box["prompt"] == "PROMPT-MST-1"
        assert box["status"] == "pending"

        # The operator pastes ChatGPT's raw response: prose and markdown
        # fences around the JSON (the JSON-leak shape) must clean up fine.
        store.submit_manual_response(run_id, LEAKY_PARK)
        # The page still shows MST-1 after the park - resume re-reads it
        # (an exhausted fake would read MST-2 first and steal the paste).
        runner.ops = FakeOps(serve=[dict(r) for r in [make_record("MST-1"), make_record("MST-2")]])
        runner.ops.lifecycle = lifecycle
        resumed = runner.resume(None)

        # MST-1 finalized from the paste; MST-2 then parks for its own paste.
        assert resumed["final_state"] == RunnerState.AWAITING_MANUAL.value
        assert resumed["processed"] == 1
        box2 = store.pending_manual_request(run_id)
        assert box2["record_id"] == "MST-2"  # consume-once: no stale response leak

        store.submit_manual_response(run_id, paste_payload("ACCEPT", "Supplier MST-2"))
        final = runner.resume(None)
        assert final["final_state"] == RunnerState.RUN_DONE.value
        assert final["processed"] == 2
        # The paste's PARK arrived without a bucket; the gate records it as
        # "other" and the tally keys carry the bucket (the legacy shape).
        assert final["decision_tally"] == {"PARK [other]": 1, "ACCEPT": 1}
        assert store.pending_manual_request(run_id) is None
        assert [e["to_status"] for e in lifecycle.events()] == [
            "running",
            "awaiting_manual",
            "running",
            "awaiting_manual",
            "running",
            "completed",
        ]

    def test_unusable_paste_reparks_without_counting_a_failure(self, store):
        run_id = "run-paste-2"
        runner, lifecycle = paste_runner(store, run_id, [make_record("MST-1")])
        runner.run(None, 1)  # parks awaiting MST-1's paste
        assert lifecycle.status() == RunStatus.AWAITING_MANUAL.value

        store.submit_manual_response(run_id, "no decision object in here")
        reparked = runner.resume(None)

        assert reparked["final_state"] == RunnerState.AWAITING_MANUAL.value
        assert reparked["processed"] == 0
        assert runner.failures == 0  # a bad paste is an input error, not a failure
        box = store.pending_manual_request(run_id)
        assert box["record_id"] == "MST-1"
        assert box["error"]  # the rejection reason is attached for the operator

        # A good paste then completes the record normally.
        store.submit_manual_response(run_id, paste_payload("ACCEPT", "Supplier MST-1"))
        final = runner.resume(None)
        assert final["final_state"] == RunnerState.RUN_DONE.value
        assert final["processed"] == 1


# ---------------------------------------------------------------------------
# Cancel semantics
# ---------------------------------------------------------------------------

class TestCancelSemantics:
    def test_cancel_at_a_boundary_marks_the_run_terminal(self, store):
        ops = CommandAfterSettle(
            [make_record("MST-1"), make_record("MST-2")], "cancel"
        )
        runner, lifecycle = make_runner(
            store, ops, FakeBackend([accept_result()])
        )
        summary = runner.run(None, 2)

        assert summary["final_state"] == RunnerState.CANCELLED.value
        assert summary["processed"] == 1  # cancelled after the first record
        assert lifecycle.status() == RunStatus.CANCELLED.value
        assert lifecycle.run_row()["final_state"] == "cancelled"
        # Terminal: the run cannot be revived or finished afterwards.
        with pytest.raises(RunLifecycleError):
            runner.resume(None)
        with pytest.raises(RunLifecycleError):
            lifecycle.pause()

    def test_cancel_releases_a_pending_paste_box(self, store):
        run_id = "run-cancel-2"
        runner, lifecycle = paste_runner(store, run_id, [make_record("MST-1")])
        runner.run(None, 1)  # parked awaiting_manual with a pending box
        assert store.pending_manual_request(run_id) is not None

        lifecycle.cancel(reason="operator cancelled")
        assert store.pending_manual_request(run_id) is None
        row = lifecycle.run_row()
        assert row["status"] == RunStatus.CANCELLED.value
        assert row["final_state"] == "cancelled"


# ---------------------------------------------------------------------------
# paused_for_login: an expired QA session parks, it does not fail
# ---------------------------------------------------------------------------

class TestPausedForLogin:
    def test_login_expiry_parks_the_run_and_resume_completes_it(self, store):
        records = [make_record("MST-1"), make_record("MST-2")]
        backend = FakeBackend(error=QASessionExpired("login required"))
        runner, lifecycle = make_runner(
            store, FakeOps(serve=[dict(r) for r in records]), backend
        )

        parked = runner.run(None, 2)
        assert parked["final_state"] == RunnerState.PAUSED_FOR_LOGIN.value
        assert lifecycle.status() == RunStatus.PAUSED_FOR_LOGIN.value
        assert parked["processed"] == 0
        # An expired login is a park, not a failure: no failure counter moved.
        assert runner.failures == 0

        runner.backend = FakeBackend(
            [accept_result(), accept_result(decision="PARK")]
        )
        # The page still shows MST-1 after the relogin - resume re-reads it.
        runner.ops = FakeOps(serve=[dict(r) for r in records])
        runner.ops.lifecycle = lifecycle
        resumed = runner.resume(None)
        assert resumed["final_state"] == RunnerState.RUN_DONE.value
        assert resumed["processed"] == 2
        assert [e["to_status"] for e in lifecycle.events()] == [
            "running",
            "paused_for_login",
            "running",
            "completed",
        ]
