"""Transition persistence - the interface the BatchRunner emits through.

The runner is a state machine: every state transition (and every terminal
outcome) flows through a :class:`TransitionSink`. This module defines the
typed record, the sink protocol, and a minimal file-backed JSONL
implementation. The storage task replaces the backing with the SQLite store
behind the SAME interface, so nothing above this layer changes.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class TransitionRecord:
    """One durable step of a run: a state change with its context.

    ``card_id``/``record_id``/``supplier_name`` identify the supplier the
    transition was about (empty for run-level transitions). ``outcome`` is
    the human-readable result of the step; ``error`` carries the failure
    detail when a step failed. ``meta`` holds free-form extras (decision
    tallies, applied-field lists) - JSON-safe values only.
    """

    run_id: str
    from_state: str
    to_state: str
    ts: str = ""  # ISO-8601 UTC; defaulted by the sink when blank
    card_id: str = ""
    record_id: str = ""
    supplier_name: str = ""
    outcome: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> TransitionRecord:
        return cls(**json.loads(line))


@runtime_checkable
class TransitionSink(Protocol):
    """Where the runner emits transitions. Storage-agnostic by design."""

    def emit(self, record: TransitionRecord) -> None:
        """Persist one transition. Must never raise into the runner loop."""
        ...

    def close(self) -> None:
        """Flush and release resources (idempotent)."""
        ...


class NullSink:
    """A sink that discards everything (tests, dry introspection)."""

    def emit(self, record: TransitionRecord) -> None:
        return

    def close(self) -> None:
        return


class FileTransitionSink:
    """Append-only JSONL file, one transition per line.

    Each emit rewrites the file atomically (temp file + ``os.replace``), so a
    crash mid-write can lose the newest line but never leaves a torn line
    behind. Fine for the file-backed stopgap; the SQLite store takes over
    for long runs.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, record: TransitionRecord) -> None:
        if not record.ts:
            record.ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        line = record.to_json() + "\n"

        existing = ""
        if self._path.exists() and self._path.stat().st_size > 0:
            existing = self._path.read_text(encoding="utf-8")

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(existing + line)
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def close(self) -> None:
        return

    def read_all(self) -> Iterator[TransitionRecord]:
        """Stream the persisted transitions back (recovery/introspection)."""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield TransitionRecord.from_json(line)
