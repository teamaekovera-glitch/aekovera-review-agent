"""RunManager - BatchRunner threads supervised from the API process.

Division of truth: the SQLite store is the single source of truth for run
state, and every API response is read from the store - never from thread
memory. The manager's own bookkeeping answers exactly one question: whether
THIS process currently has a live worker thread for a run. Anything that
looks like run state (status, counts, decisions) comes from the store.

Workers each open their own ``ReviewStore`` connection (WAL keeps readers
and the writer consistent; ``busy_timeout`` absorbs brief contention) and
rebuild their runner with :meth:`BatchRunner.from_store` on every start -
the same path crash recovery uses, so a paused run, a crashed run, and a
just-pasted manual box all resume through one code path that replays the
persisted ledger instead of re-researching decided records.

Adoption (``adopt``, called at server boot) never opens a browser and never
mutates state: it registers in-progress runs found in the store so the API
can report them truthfully (``adopted: true`` + the recovered ledger state)
and make them resumable. A crashed run is still ``running`` on disk - the
lifecycle module's contract - and resume moves it the same way any park
resumes. Rebuilding the actual runner is deferred to resume because
``from_store`` needs live collaborators (a transport, a session) that must
not be constructed - let alone opened - at boot time.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from review_hub.config import BASE_URL
from review_hub.engine.corrections import CorrectionApplier
from review_hub.engine.prompting import build_research_prompt
from review_hub.engine.research.manual import PasteBoxBackend
from review_hub.engine.research.obvious import ObviousAgentClient
from review_hub.engine.research.openrouter import OpenRouterClient
from review_hub.engine.runner import BatchRunner, PageOps
from review_hub.engine.session import open_review_page, open_session
from review_hub.engine.transport import QAClient
from review_hub.lifecycle import RunLifecycle, RunLifecycleError, RunStatus, StoreManualGate
from review_hub.store.repository import ReviewStore

logger = logging.getLogger("review_hub.server")

# The default research backends, keyed by the per-run switch the API accepts.
# manual: the paste box over the store's gate (zero paid resources, default).
# api: the OpenRouter free-model client. Factories receive the WORKER's own
# store so the manual gate consumes pastes on the connection that runs them.
BackendFactory = Callable[[ReviewStore, str], Any]


def default_backend_factories() -> dict[str, BackendFactory]:
    return {
        "manual": lambda store, run_id: PasteBoxBackend(StoreManualGate(store, run_id)),
        "api": lambda store, run_id: OpenRouterClient(),
        "obvious": lambda store, run_id: ObviousAgentClient(),
    }


@contextmanager
def engine_session(profile_dir: str | None = None):
    """The default Playwright session: yield (context, page) for one worker.

    Mirrors cli.main's wiring - persistent profile, review page opened on the
    first tab - as a context manager the RunManager can enter per worker.
    """
    with open_session(profile_dir) as context:
        page = open_review_page(context)
        yield context, page


def _quiet_log(*args: Any) -> None:
    """Route engine prints to the server log instead of stdout noise."""
    logger.info("engine: %s", " ".join(str(a) for a in args))


class RunManager:
    """Owns the worker threads; never owns run state."""

    def __init__(
        self,
        store: ReviewStore,
        db_path: Any,
        *,
        session_factory: Any = engine_session,
        backend_factories: dict[str, BackendFactory] | None = None,
        clock: Any = None,
    ) -> None:
        self._store = store  # main-thread reads + operator lifecycle writes
        self._db_path = db_path  # workers open their own store here
        self._session_factory = session_factory
        self._backends = backend_factories or default_backend_factories()
        self._default_backend = next(iter(self._backends))
        self._clock = clock
        self._threads: dict[str, threading.Thread] = {}
        self._adopted: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Supervisor bookkeeping (NOT run state)
    # ------------------------------------------------------------------ #
    def thread_alive(self, run_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(run_id)
            return bool(thread and thread.is_alive())

    def adopted(self, run_id: str) -> bool:
        return run_id in self._adopted

    def supervisor_view(self, run_id: str) -> dict[str, bool]:
        return {"thread_alive": self.thread_alive(run_id), "adopted": self.adopted(run_id)}

    def backend_names(self) -> tuple[str, ...]:
        return tuple(self._backends)

    # ------------------------------------------------------------------ #
    # Startup adoption
    # ------------------------------------------------------------------ #
    def adopt(self) -> list[str]:
        """Register in-progress runs found in the store (server boot).

        A run left ``running`` by a dead process, or ``queued`` and never
        started, is adoptable: it is registered so the API reports the
        recovered state from the store and ``resume`` can rebuild it via
        ``BatchRunner.from_store``. No browser is opened, no row is touched.
        """
        adopted = []
        with self._lock:
            for row in self._store.runs():
                run_id = str(row.get("run_id") or "")
                if row.get("status") in (RunStatus.RUNNING.value, RunStatus.QUEUED.value):
                    self._adopted.add(run_id)
                    adopted.append(run_id)
        if adopted:
            logger.info("adopted %d in-progress run(s) from the store", len(adopted))
        return adopted

    # ------------------------------------------------------------------ #
    # Operator actions
    # ------------------------------------------------------------------ #
    def start_run(self, *, mode: str, backend: str, limit: int) -> str:
        """Create the run row (queued) and launch its worker thread."""
        if backend not in self._backends:
            raise ValueError(f"unknown research backend {backend!r}")
        run_id = uuid.uuid4().hex[:12]
        self._store.record_run_start(run_id, mode=mode, run_count=limit)
        self._spawn(run_id, backend=backend, fresh=True)
        return run_id

    def resume(self, run_id: str, *, backend: str | None = None) -> dict[str, Any]:
        """Resume a parked / crashed / never-started run via from_store.

        The backend is an explicit per-resume choice (defaulting to the
        settings' RESEARCH_BACKEND) because the runs table does not carry it
        and the store's schema is not the server's to change.
        """
        status = self._store.run_status(run_id)
        if status in (s.value for s in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)):
            raise RunLifecycleError(f"run {run_id} is terminal ({status}); it cannot resume")
        if self.thread_alive(run_id):
            raise RunLifecycleError(f"run {run_id} already has a live worker")
        name = backend or self._default_backend
        if name not in self._backends:
            raise ValueError(f"unknown research backend {name!r}")
        # The newest operator command wins: a pause queued before an explicit
        # resume must not re-park the run at its first safe boundary.
        self._store.poll_run_command(run_id)
        self._spawn(run_id, backend=name, fresh=False)
        return {"resumed": True, "status": self._store.run_status(run_id)}

    def pause(self, run_id: str) -> dict[str, Any]:
        """Pause at the runner's next safe boundary - or immediately when
        no live worker exists (a crashed/adopted run has no boundary to
        reach, and its lifecycle edge running -> paused is legal)."""
        if self.thread_alive(run_id):
            RunLifecycle(self._store, run_id).request_pause()
        else:
            RunLifecycle(self._store, run_id).pause(reason="paused by operator")
        return {"requested": "pause", "status": self._store.run_status(run_id)}

    def cancel(self, run_id: str) -> dict[str, Any]:
        if self.thread_alive(run_id):
            RunLifecycle(self._store, run_id).request_cancel()
        else:
            RunLifecycle(self._store, run_id).cancel(reason="cancelled by operator")
        return {"requested": "cancel", "status": self._store.run_status(run_id)}

    # ------------------------------------------------------------------ #
    # Worker threads
    # ------------------------------------------------------------------ #
    def _spawn(self, run_id: str, *, backend: str, fresh: bool) -> None:
        with self._lock:
            thread = threading.Thread(
                target=self._worker,
                args=(run_id, backend, fresh),
                name=f"review-run-{run_id}",
                daemon=True,
            )
            self._threads[run_id] = thread
        thread.start()

    def _worker(self, run_id: str, backend_name: str, fresh: bool) -> None:
        store = ReviewStore(self._db_path, clock=self._clock)
        try:
            self._supervise(store, run_id, backend_name, fresh)
        except Exception as exc:
            # The runner marks engine failures itself; a crash OUTSIDE the
            # loop (session, construction) would otherwise strand the run as
            # 'running' with nobody polling a mailbox. Persist the truth.
            self._mark_worker_failure(store, run_id, exc)
        finally:
            store.close()
            with self._lock:
                self._threads.pop(run_id, None)

    def _supervise(self, store: ReviewStore, run_id: str, backend_name: str, fresh: bool) -> None:
        with self._session_factory() as (context, page):
            transport = QAClient(context, base_url=BASE_URL, log=_quiet_log)
            applier = CorrectionApplier(transport=transport, log=_quiet_log)
            ops = PageOps(applier=applier, transport=transport)
            backend = self._backends[backend_name](store, run_id)
            # The prompt builder mirrors cli.main: ChatGPT (and free models
            # reading agent-fetched evidence) get the browsing-variant
            # rulebook. build_prompt is read per construction so settings
            # edits reach new runs.
            runner = BatchRunner.from_store(
                store,
                run_id,
                ops,
                backend,
                build_prompt=lambda record: build_research_prompt(record, browsing=True),
                log=_quiet_log,
            )
            row = store.get_run(run_id) or {}
            if row.get("status") in (s.value for s in (
                RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED
            )):
                return  # terminal before the worker could start - nothing to do
            if fresh:
                summary = runner.run(page, int(row.get("run_count") or 0))
            else:
                summary = runner.resume(page)
            logger.info("run %s worker finished: %s", run_id, summary.get("final_state"))

    def _mark_worker_failure(self, store: ReviewStore, run_id: str, exc: Exception) -> None:
        logger.exception("run %s worker crashed", run_id)
        status = store.run_status(run_id)
        lifecycle = RunLifecycle(store, run_id)
        try:
            if status == RunStatus.QUEUED.value:
                # It never started: QUEUED -> FAILED is not a legal edge, and
                # 'cancelled' with the reason is the honest end state.
                lifecycle.cancel(reason=f"worker could not start: {exc}")
            else:
                lifecycle.fail(reason=f"worker error: {exc}")
        except RunLifecycleError:
            # The runner (or a race) already moved the run to a terminal
            # state - the store is truthful; nothing to add.
            logger.info("run %s already terminal (%s); crash state kept", run_id, status)

    def shutdown(self, *, join_timeout: float = 2.0) -> None:
        """Best-effort stop: join live workers briefly (daemon threads)."""
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=join_timeout)
