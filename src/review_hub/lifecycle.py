"""Run-level lifecycle - the spec's run state machine, persisted in the store.

Per the productization spec: ``queued -> running -> completed``, with exits
to ``paused`` (operator hold, or a runner safety stop), ``paused_for_login``
(the review session needs a human login) and ``awaiting_manual`` (the manual
ChatGPT paste box is waiting for the operator's response), plus the terminal
``failed`` / ``cancelled``. A parked run is resumable in place; a crashed run
(died without a clean exit) is still ``running`` on disk and resumes the same
way - both pick up from the persisted transition log and research ledger.

Every status change appends a row to ``run_status_events`` (the per-step
history the dashboard replays) inside the same transaction that moves the
run's current status, so the two can never disagree.

The BatchRunner consumes this through a narrow collaborator surface (start /
resume / parks / poll / ledger); the store does the persistence. Allowed
edges are validated here - an illegal transition (e.g. completing an already
cancelled run) raises :class:`RunLifecycleError` instead of writing a lie.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from review_hub.store.repository import ReviewStore


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    PAUSED_FOR_LOGIN = "paused_for_login"
    AWAITING_MANUAL = "awaiting_manual"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# The park states: not terminal, resumable in place.
PARKED_STATUSES = (
    RunStatus.PAUSED,
    RunStatus.PAUSED_FOR_LOGIN,
    RunStatus.AWAITING_MANUAL,
)

# Terminal states: no exits.
TERMINAL_STATUSES = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)

# Legal edges of the run state machine. A same->same move is always allowed
# for non-terminal statuses (a resume re-enters `running`; a re-park on an
# invalid paste stays `awaiting_manual`) so the event log can carry the
# resumption/error reason without inventing a fake state change.
_ALLOWED_EDGES: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.PAUSED,
            RunStatus.PAUSED_FOR_LOGIN,
            RunStatus.AWAITING_MANUAL,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.PAUSED_FOR_LOGIN: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.AWAITING_MANUAL: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class RunLifecycleError(RuntimeError):
    """An illegal run-status transition was refused."""


class RunLifecycle:
    """One run's status machine, backed by the store.

    Holds the run id the whole pipeline shares (transitions, ledger, manual
    requests, status events). Construct with ``run_id=None`` to mint a fresh
    id for a new run; pass a stored id to resume or inspect one.
    """

    def __init__(self, store: ReviewStore, run_id: str | None = None) -> None:
        self.store = store
        self.run_id = run_id or uuid.uuid4().hex[:12]

    # ------------------------------------------------------------------ #
    # Status plumbing
    # ------------------------------------------------------------------ #
    def status(self) -> str:
        return self.store.run_status(self.run_id)

    def events(self) -> list[dict[str, Any]]:
        return self.store.run_status_events(self.run_id)

    def _move(self, to_status: RunStatus, *, reason: str = "") -> str:
        if to_status in TERMINAL_STATUSES and self.status() in (
            s.value for s in TERMINAL_STATUSES
        ):
            raise RunLifecycleError(
                f"run {self.run_id} is already terminal ({self.status()}); "
                f"cannot move to {to_status.value}"
            )
        allowed = _ALLOWED_EDGES.get(
            RunStatus(self.status()) if self.status() else RunStatus.QUEUED, frozenset()
        )
        if self.status() and to_status.value != self.status() and to_status not in allowed:
            raise RunLifecycleError(
                f"illegal run transition {self.status()} -> {to_status.value} "
                f"for run {self.run_id}"
            )
        return self.store.record_run_status(self.run_id, to_status.value, reason=reason)

    # ------------------------------------------------------------------ #
    # Runner-facing surface
    # ------------------------------------------------------------------ #
    def start(self, *, mode: str = "", run_count: int = 0) -> None:
        """A fresh run: create the row and move queued -> running."""
        self.store.record_run_start(self.run_id, mode=mode, run_count=run_count)
        self._move(RunStatus.RUNNING, reason=f"run started: {run_count} records")

    def resume(self, *, reason: str = "resumed") -> None:
        """Parked (or crashed) -> running. A safe no-op when already running."""
        self._move(RunStatus.RUNNING, reason=reason)

    def pause(self, *, reason: str = "") -> None:
        self._move(RunStatus.PAUSED, reason=reason)

    def pause_for_login(self, *, reason: str = "") -> None:
        self._move(RunStatus.PAUSED_FOR_LOGIN, reason=reason)

    def await_manual(
        self, *, record_id: str = "", company_name: str = "", prompt: str = "", error: str = ""
    ) -> int:
        """Park in awaiting_manual and surface the prompt as the paste box."""
        request_id = self.store.open_manual_request(
            self.run_id, record_id, company_name, prompt, error=error
        )
        self._move(
            RunStatus.AWAITING_MANUAL,
            reason=error or f"waiting for the pasted ChatGPT response ({record_id or 'record'})",
        )
        return request_id

    def complete(self, summary: dict[str, Any]) -> None:
        self._move(RunStatus.COMPLETED, reason="run completed")
        self.store.record_run_finish(
            {"run_id": self.run_id, "final_state": "run_done", **summary}
        )

    def fail(self, *, reason: str = "", summary: dict[str, Any] | None = None) -> None:
        self._move(RunStatus.FAILED, reason=reason)
        self.store.record_run_finish(
            {"run_id": self.run_id, "final_state": "failed", **(summary or {})}
        )

    def cancel(self, *, reason: str = "cancelled", summary: dict[str, Any] | None = None) -> None:
        """Mark the run terminal and release in-flight state cleanly."""
        self.store.cancel_manual_requests(self.run_id)
        self._move(RunStatus.CANCELLED, reason=reason)
        self.store.record_run_finish(
            {"run_id": self.run_id, "final_state": "cancelled", **(summary or {})}
        )

    # ------------------------------------------------------------------ #
    # Operator commands (polled by the runner at safe boundaries)
    # ------------------------------------------------------------------ #
    def request_pause(self) -> None:
        self.store.request_run_command(self.run_id, "pause")

    def request_cancel(self) -> None:
        self.store.request_run_command(self.run_id, "cancel")

    def poll_command(self) -> str | None:
        return self.store.poll_run_command(self.run_id)

    # ------------------------------------------------------------------ #
    # Research ledger (never re-research a decided record)
    # ------------------------------------------------------------------ #
    def record_decision(
        self,
        *,
        record_id: str,
        company_name: str,
        kind: str,
        result: dict[str, Any],
        tally: dict[str, Any] | None = None,
        finalized: bool = True,
        processed_after: int = 0,
    ) -> None:
        self.store.record_research(
            self.run_id,
            record_id,
            company_name,
            kind=kind,
            decision=str(result.get("decision") or ""),
            result=result,
            tally=tally,
            finalized=finalized,
            processed_after=processed_after,
        )

    def decided_result(self, record_id: str, company_name: str) -> dict[str, Any] | None:
        """The result this run already decided for a record (None if undecided)."""
        row = self.store.research_decision(self.run_id, record_id, company_name)
        return dict(row["result"]) if row else None

    def decisions(self) -> list[dict[str, Any]]:
        return self.store.research_rows(self.run_id)

    def repeat_total(self, repeat_outcome: str) -> int:
        return self.store.count_transitions(self.run_id, outcome=repeat_outcome)

    def run_row(self) -> dict[str, Any] | None:
        return self.store.get_run(self.run_id)


class StoreManualGate:
    """The store-backed paste-box gate: the run's answered manual response.

    The :class:`~review_hub.engine.research.manual.PasteBoxBackend` calls
    ``take_response()`` each time research runs. Before the operator pastes
    there is nothing to take (the backend raises ManualPauseRequested and the
    run parks); after they paste, the answered request's raw response is
    returned exactly once and marked consumed, so the NEXT record's research
    can never mistake an old response for its own.
    """

    def __init__(self, store: ReviewStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    def take_response(self) -> str | None:
        return self.store.consume_manual_response(self.run_id)
