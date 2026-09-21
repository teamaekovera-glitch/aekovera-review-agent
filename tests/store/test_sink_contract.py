"""Sink-contract tests: SQLite sink satisfies the SAME contract as the file sink.

Every contract case runs against BOTH implementations (parametrized) - the
file-backed JSONL sink is the reference contract, the SQLite sink must behave
identically to anything above ``TransitionSink``. The crash-safety cases are
SQLite-specific (transactional transition writes).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from review_hub.persistence import FileTransitionSink, NullSink, TransitionRecord, TransitionSink
from review_hub.store.sqlite_sink import SqliteTransitionSink


def make_record(run_id: str = "run-1", **overrides) -> TransitionRecord:
    defaults = dict(
        run_id=run_id,
        from_state="reading",
        to_state="researching",
        card_id="card-42",
        record_id="M-1001",
        supplier_name="Alekovera Spice Co",
        outcome="record read",
        error="",
        meta={"decision_tally": {"ACCEPT": 1}, "applied": ["city"]},
    )
    defaults.update(overrides)
    return TransitionRecord(**defaults)


@pytest.fixture()
def sqlite_sink(tmp_path: Path) -> Iterator[SqliteTransitionSink]:
    sink = SqliteTransitionSink(tmp_path / "review.db")
    yield sink
    sink.close()


@pytest.fixture()
def file_sink(tmp_path: Path) -> Iterator[FileTransitionSink]:
    sink = FileTransitionSink(tmp_path / "transitions.jsonl")
    yield sink
    sink.close()


SINKS = ("sqlite_sink", "file_sink")


# --------------------------------------------------------------------------- #
# Contract cases - identical for every TransitionSink implementation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sink_name", SINKS)
def test_satisfies_the_transition_sink_protocol(request, sink_name: str) -> None:
    sink = request.getfixturevalue(sink_name)
    assert isinstance(sink, TransitionSink)


@pytest.mark.parametrize("sink_name", SINKS)
def test_blank_ts_is_stamped_and_provided_ts_is_kept(request, sink_name: str) -> None:
    sink = request.getfixturevalue(sink_name)
    blank = make_record(ts="")
    stamped = make_record(ts="2026-09-21T12:00:00.000+00:00")
    sink.emit(blank)
    sink.emit(stamped)
    assert blank.ts  # the record object itself gets the stamp, like the file sink
    assert stamped.ts == "2026-09-21T12:00:00.000+00:00"

    replayed = list(sink.read_all())
    assert len(replayed) == 2
    assert replayed[0].ts
    assert replayed[1].ts == "2026-09-21T12:00:00.000+00:00"


@pytest.mark.parametrize("sink_name", SINKS)
def test_roundtrip_preserves_every_field_in_order(request, sink_name: str) -> None:
    sink = request.getfixturevalue(sink_name)
    records = [
        make_record(run_id=f"run-{i}", to_state=f"state-{i}", meta={"i": i}) for i in range(5)
    ]
    for record in records:
        sink.emit(record)

    replayed = list(sink.read_all())
    assert [r.run_id for r in replayed] == [f"run-{i}" for i in range(5)]
    for original, back in zip(records, replayed, strict=True):
        assert back.from_state == original.from_state
        assert back.to_state == original.to_state
        assert back.card_id == original.card_id
        assert back.record_id == original.record_id
        assert back.supplier_name == original.supplier_name
        assert back.outcome == original.outcome
        assert back.error == original.error
        assert back.meta == original.meta  # meta dict roundtrips exactly


@pytest.mark.parametrize("sink_name", SINKS)
def test_run_level_transition_has_no_supplier_fields(request, sink_name: str) -> None:
    """Run-level arrows (idle -> reading) emit empty card/record/name."""
    sink = request.getfixturevalue(sink_name)
    sink.emit(make_record(card_id="", record_id="", supplier_name="", meta={}))
    back = list(sink.read_all())[0]
    assert back.card_id == "" and back.record_id == "" and back.supplier_name == ""
    assert back.meta == {}


@pytest.mark.parametrize("sink_name", SINKS)
def test_close_is_idempotent(request, sink_name: str) -> None:
    sink = request.getfixturevalue(sink_name)
    sink.close()
    sink.close()  # must not raise


@pytest.mark.parametrize("sink_name", SINKS)
def test_json_shape_matches_transition_record_serialization(request, sink_name: str) -> None:
    """The wire shape stays TransitionRecord.to_json/from_json compatible."""
    sink = request.getfixturevalue(sink_name)
    record = make_record()
    sink.emit(record)
    back = list(sink.read_all())[0]
    assert json.loads(back.to_json()) == json.loads(record.to_json())
    assert TransitionRecord.from_json(record.to_json()) == record


def test_null_sink_discards_everything() -> None:
    sink = NullSink()
    sink.emit(make_record())  # must not raise into the runner loop
    sink.close()  # no-op close, like the other sinks


# --------------------------------------------------------------------------- #
# SQLite-specific: crash safety for transition writes
# --------------------------------------------------------------------------- #
def test_uncommitted_transition_is_invisible_to_other_connections(tmp_path: Path) -> None:
    """A partially written transition must never be readable as complete:
    a second connection sees either nothing (in-flight) or the full row
    (committed) - never a torn half-state."""
    writer = SqliteTransitionSink(tmp_path / "review.db")
    reader_conn = sqlite3.connect(tmp_path / "review.db")

    def rows_visible() -> int:
        return reader_conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]

    assert rows_visible() == 0

    writer_conn = writer._conn
    writer_conn.execute("BEGIN IMMEDIATE")
    writer.emit(make_record(run_id="run-inflight"))
    # In-flight: the write is buffered inside the transaction...
    assert rows_visible() == 0  # ...and NO other connection can see any of it.

    writer_conn.execute("ROLLBACK")
    assert rows_visible() == 0  # rolled-back transition never existed.

    writer.emit(make_record(run_id="run-committed"))  # autocommit emit
    assert rows_visible() == 1
    writer_conn.close()
    reader_conn.close()


def test_simulated_crash_leaves_only_committed_transitions(tmp_path: Path) -> None:
    """Simulate a crash between emits: a fresh connection reading the store
    after the crash sees exactly the transitions committed before it."""
    sink = SqliteTransitionSink(tmp_path / "review.db")
    sink.emit(make_record(run_id="run-a"))
    sink.emit(make_record(run_id="run-b", to_state="researching"))

    # "Crash": abandon the connection WITHOUT close (OS reclaims; WAL keeps
    # committed data) and open a fresh one, as recovery would.
    fresh = sqlite3.connect(tmp_path / "review.db")
    committed = fresh.execute(
        "SELECT run_id, to_state FROM transitions ORDER BY transition_id"
    ).fetchall()
    fresh.close()
    assert committed == [("run-a", "researching"), ("run-b", "researching")]


def test_meta_with_non_json_safe_values_survives(tmp_path: Path) -> None:
    """meta values that json.dumps cannot handle fall back to str (the file
    sink's json.dumps would raise) - never raise into the runner loop."""
    sink = SqliteTransitionSink(tmp_path / "review.db")
    sink.emit(make_record(meta={"seen_at": datetime(2026, 9, 21, tzinfo=UTC)}))
    back = list(sink.read_all())[0]
    assert back.meta["seen_at"].startswith("2026-09-21")


def test_shared_connection_sink_leaves_closing_to_the_owner(tmp_path: Path) -> None:
    """A sink wrapping someone else's connection must not close it."""
    from review_hub.store.schema import connect, ensure_schema

    conn = connect(tmp_path / "review.db")
    ensure_schema(conn)
    sink = SqliteTransitionSink(connection=conn)
    sink.emit(make_record())
    sink.close()
    assert conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] == 1
    conn.close()
