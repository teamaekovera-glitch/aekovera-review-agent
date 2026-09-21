"""SQLite schema for the lifecycle store - the system of record.

Every entity the productization spec pins lives in one local file: runs,
records, corrections, holds, audit log (prompts, raw responses, website
checks, discovery failures), evidence, transitions, the deduplicated
supplier-history view, and accepted-company snapshots. Stdlib ``sqlite3``
only - no paid dependencies or services.

Versioning uses ``PRAGMA user_version``: ``ensure_schema`` applies each
migration step in order inside one transaction, refuses to touch a store
written by a NEWER version of this code, and is idempotent for an already
current one. Write-ahead logging keeps the store readable while a batch
writes to it (the dashboard watches runs live).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class ReviewStoreError(RuntimeError):
    """Raised when a store cannot be opened or migrated safely."""


_V1_STATEMENTS = (
    # Batch runs. The runner's `_finish()` summary is stored wholesale so a
    # finished run can always be re-reported from the store alone.
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL DEFAULT '',
        run_count INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL DEFAULT '',
        finished_at TEXT NOT NULL DEFAULT '',
        final_state TEXT NOT NULL DEFAULT '',
        records_processed INTEGER NOT NULL DEFAULT 0,
        repeat_passes_total INTEGER NOT NULL DEFAULT 0,
        decision_tally_json TEXT NOT NULL DEFAULT '{}',
        summary_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # Supplier records ever seen (identity keyed on the v34 dedupe key:
    # MASTER id, falling back to the normalised company name).
    """
    CREATE TABLE IF NOT EXISTS records (
        record_key TEXT PRIMARY KEY,
        record_id TEXT NOT NULL DEFAULT '',
        supplier_name TEXT NOT NULL DEFAULT '',
        first_seen TEXT NOT NULL DEFAULT '',
        last_seen TEXT NOT NULL DEFAULT '',
        times_seen INTEGER NOT NULL DEFAULT 0,
        fields_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # Structured field corrections (proposed, applied, skipped, cleared...).
    """
    CREATE TABLE IF NOT EXISTS corrections (
        correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL DEFAULT '',
        record_id TEXT NOT NULL DEFAULT '',
        company_name TEXT NOT NULL DEFAULT '',
        field TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        old_value TEXT NOT NULL DEFAULT '',
        new_value TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    # Held / flagged records awaiting a human: manual reviews, field holds.
    # detail_json is kind-specific structured data (see repository).
    """
    CREATE TABLE IF NOT EXISTS holds (
        hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL DEFAULT '',
        record_id TEXT NOT NULL DEFAULT '',
        company_name TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'open',
        resolution_note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        resolved_at TEXT NOT NULL DEFAULT ''
    )
    """,
    # Audit trail: prompts and raw responses (the llm_logs replacement),
    # website sanity checks, field-discovery failures.
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL DEFAULT '',
        record_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    # Fetched web evidence pages, kept with the run/record they served.
    """
    CREATE TABLE IF NOT EXISTS evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL DEFAULT '',
        record_id TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    # One row per BatchRunner state transition - the same stream the
    # file-backed JSONL sink persists, in the same shape.
    """
    CREATE TABLE IF NOT EXISTS transitions (
        transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL DEFAULT '',
        from_state TEXT NOT NULL DEFAULT '',
        to_state TEXT NOT NULL DEFAULT '',
        ts TEXT NOT NULL DEFAULT '',
        card_id TEXT NOT NULL DEFAULT '',
        record_id TEXT NOT NULL DEFAULT '',
        supplier_name TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        meta_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # Deduplicated supplier history (the supplier_history.xlsx sheets): one
    # row per supplier, updated in place, moving between outcome views.
    # The v34 meta block is stored as real columns (queryable state); the
    # discovered company-detail fields stay in fields_json.
    """
    CREATE TABLE IF NOT EXISTS history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT NOT NULL DEFAULT '',
        record_id TEXT NOT NULL DEFAULT '',
        company_name TEXT NOT NULL DEFAULT '',
        outcome_view TEXT NOT NULL DEFAULT '',
        first_seen TEXT NOT NULL DEFAULT '',
        last_seen TEXT NOT NULL DEFAULT '',
        times_seen INTEGER NOT NULL DEFAULT 1,
        decision TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT '',
        run_mode TEXT NOT NULL DEFAULT '',
        backend TEXT NOT NULL DEFAULT '',
        confidence TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        scope_match TEXT NOT NULL DEFAULT '',
        food_beverage_connection TEXT NOT NULL DEFAULT '',
        qualifying_supplier_types TEXT NOT NULL DEFAULT '',
        supply_country TEXT NOT NULL DEFAULT '',
        is_us_based TEXT NOT NULL DEFAULT '',
        supply_origin_note TEXT NOT NULL DEFAULT '',
        manual_review_reason TEXT NOT NULL DEFAULT '',
        fields_applied TEXT NOT NULL DEFAULT '',
        fields_created TEXT NOT NULL DEFAULT '',
        fields_cleared TEXT NOT NULL DEFAULT '',
        fields_failed TEXT NOT NULL DEFAULT '',
        fields_left_uncleared TEXT NOT NULL DEFAULT '',
        fields_held_for_review TEXT NOT NULL DEFAULT '',
        identity_renamed TEXT NOT NULL DEFAULT '',
        fields_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    # Accepted-company snapshots (accepted_companies.xlsx): the corrected
    # record read back from the page before the verdict, confirmed or rolled
    # back after it (v32.1 order). decision_json keeps the research scope
    # block (is_us_based, supply origin, types) with the snapshot.
    """
    CREATE TABLE IF NOT EXISTS accepted_companies (
        accepted_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT NOT NULL DEFAULT '',
        record_id TEXT NOT NULL DEFAULT '',
        company_name TEXT NOT NULL DEFAULT '',
        accepted_at TEXT NOT NULL DEFAULT '',
        verdict_status TEXT NOT NULL DEFAULT '',
        edit_status TEXT NOT NULL DEFAULT '',
        edits_made TEXT NOT NULL DEFAULT '',
        fields_failed TEXT NOT NULL DEFAULT '',
        fields_unresolved TEXT NOT NULL DEFAULT '',
        fields_not_landed TEXT NOT NULL DEFAULT '',
        confirmed_by TEXT NOT NULL DEFAULT '',
        snapshot_source TEXT NOT NULL DEFAULT '',
        first_accepted TEXT NOT NULL DEFAULT '',
        times_accepted INTEGER NOT NULL DEFAULT 1,
        fields_json TEXT NOT NULL DEFAULT '{}',
        decision_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # Query paths the dashboard and exports lean on.
    "CREATE INDEX IF NOT EXISTS idx_transitions_run ON transitions(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_transitions_record ON transitions(record_id)",
    "CREATE INDEX IF NOT EXISTS idx_corrections_record ON corrections(record_id)",
    "CREATE INDEX IF NOT EXISTS idx_holds_status ON holds(status)",
    "CREATE INDEX IF NOT EXISTS idx_holds_record ON holds(record_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_log(record_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit_log(kind)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_record ON evidence(record_id)",
    "CREATE INDEX IF NOT EXISTS idx_history_key ON history(dedupe_key)",
    "CREATE INDEX IF NOT EXISTS idx_accepted_key ON accepted_companies(dedupe_key)",
)

# (target_version, statements): migrating to target_version from target-1.
_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, _V1_STATEMENTS),)


def connect(
    path: str | Path, *, factory: type[sqlite3.Connection] | None = None
) -> sqlite3.Connection:
    """Open the store with the lifecycle settings every caller needs.

    ``isolation_level=None`` puts the driver in autocommit mode so
    :func:`transaction` controls transactions explicitly - partial writes
    stay impossible without implicit-transaction surprises. ``busy_timeout``
    lets a reader wait out an in-flight commit instead of erroring.
    """
    conn = sqlite3.connect(str(path), isolation_level=None, **({"factory": factory} if factory else {}))
    conn.row_factory = sqlite3.Row  # repository reads do dict(row) - rows must name columns
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One all-or-nothing unit of work: BEGIN IMMEDIATE / COMMIT / ROLLBACK."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Migrate the store to the current schema version. Returns the version.

    Refuses a store written by newer code rather than opening it with a
    schema it does not understand - a silent downgrade would be data loss.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ReviewStoreError(
            f"store schema v{version} is newer than this code (v{SCHEMA_VERSION}); "
            "upgrade review-hub before opening this store"
        )
    for target, statements in _MIGRATIONS:
        if target <= version:
            continue
        with transaction(conn):
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")
    return SCHEMA_VERSION


def json_dumps(value: Any) -> str:
    """Compact, deterministic JSON for store payload columns."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
