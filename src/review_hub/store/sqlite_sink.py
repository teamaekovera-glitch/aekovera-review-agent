"""SQLite transition sink - a drop-in for :class:`FileTransitionSink`.

Implements the SAME ``TransitionSink`` protocol the BatchRunner emits
through (``persistence.TransitionSink``): ``emit(TransitionRecord)`` and an
idempotent ``close()``. Emits land in the versioned store's ``transitions``
table (one row per state change), so recovery/introspection reads the same
history the file-backed JSONL sink would have written - without rewriting
the whole file on every emit.

Deliberately NOT wired as the runner's default here: that wiring is the next
task's scope. This module only proves the store can sit behind the identical
interface.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from review_hub.persistence import TransitionRecord
from review_hub.store.schema import connect, ensure_schema, json_dumps

_INSERT = (
    "INSERT INTO transitions (run_id, from_state, to_state, ts, card_id, record_id, "
    "supplier_name, outcome, error, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT = (
    "SELECT run_id, from_state, to_state, ts, card_id, record_id, supplier_name, "
    "outcome, error, meta_json FROM transitions ORDER BY transition_id"
)


class SqliteTransitionSink:
    """Append transitions to the ``transitions`` table.

    ``path`` accepts any sqlite3 path, including ``:memory:``. Pass
    ``connection=`` to share an already-open store connection (the wiring
    task's ``ReviewStore.sink()`` will do exactly that); the sink then never
    closes the connection it does not own.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        clock: Any = None,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owns_connection = connection is None
        self._conn = connection or connect(self._path)
        ensure_schema(self._conn)
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, record: TransitionRecord) -> None:
        """Persist one transition. A blank ``ts`` is stamped on the record
        itself, matching the file sink so the caller's object reads back the
        same either way."""
        if self._closed:
            raise RuntimeError("sink is closed")
        if not record.ts:
            record.ts = self._clock().isoformat(timespec="milliseconds")
        self._conn.execute(
            _INSERT,
            (
                record.run_id,
                record.from_state,
                record.to_state,
                record.ts,
                record.card_id,
                record.record_id,
                record.supplier_name,
                record.outcome,
                record.error,
                json_dumps(dict(record.meta)),
            ),
        )

    def read_all(self) -> Iterator[TransitionRecord]:
        """Stream the persisted transitions back in emission order."""
        for row in self._conn.execute(_SELECT).fetchall():
            yield TransitionRecord(
                run_id=row[0],
                from_state=row[1],
                to_state=row[2],
                ts=row[3],
                card_id=row[4],
                record_id=row[5],
                supplier_name=row[6],
                outcome=row[7],
                error=row[8],
                meta=dict(json.loads(row[9] or "{}")),
            )

    def close(self) -> None:
        """Flush and release resources (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._conn.close()
