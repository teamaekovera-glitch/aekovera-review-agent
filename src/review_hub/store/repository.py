"""ReviewStore - data access for the lifecycle store.

One class, one transaction per write, and the v34 semantics ported exactly:
history de-duplicates on the MASTER id (falling back to the normalised name),
a repeat updates its row in place with ``first_seen`` preserved, and a row
whose outcome changed MOVES between outcome views (delete + append, so the
export order matches the legacy workbook's). The wiring task will call these
methods from the runner; nothing here touches the engine.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from review_hub.store.rows import (
    ACCEPTED_LEAD_COLUMNS,
    ACCEPTED_STATUS,
    ACCEPTED_TAIL_COLUMNS,
    HISTORY_META_COLUMNS,
    build_accepted_row,
    build_history_row,
    dedupe_key,
    flatten_text,
    sheet_for,
)
from review_hub.store.schema import (
    ReviewStoreError,
    connect,
    ensure_schema,
    json_dumps,
    transaction,
)

# Meta-block columns stored as real history columns; the rest of the
# HISTORY_META_COLUMNS block (first_seen/last_seen/times_seen/record_id/
# company_name) has its own dedicated column.
_HISTORY_META_DB_COLUMNS = tuple(
    c
    for c in HISTORY_META_COLUMNS
    if c not in {"first_seen", "last_seen", "times_seen", "record_id", "company_name"}
)

# Row keys that NEVER land in a fields_json detail block: the meta block
# itself, plus the store's own bookkeeping keys.
_HISTORY_DB_KEYS = frozenset(HISTORY_META_COLUMNS) | {"_decision"}

# Columns kept out of the accepted detail block: identity + edit-summary
# lead/tail and the store's own bookkeeping keys (legacy accepted build_row
# excluded only _FIXED - a detail field named e.g. 'decision' still lands).
ACCEPTED_FIXED_KEYS = (
    frozenset(ACCEPTED_LEAD_COLUMNS)
    | frozenset(ACCEPTED_TAIL_COLUMNS)
    | {"_decision", "_fields_json"}
)

ACCEPTED_DB_COLUMNS = (
    "dedupe_key",
    "record_id",
    "company_name",
    "accepted_at",
    "verdict_status",
    "edit_status",
    "edits_made",
    "fields_failed",
    "fields_unresolved",
    "fields_not_landed",
    "confirmed_by",
    "snapshot_source",
    "first_accepted",
    "times_accepted",
    "fields_json",
    "decision_json",
)


class ReviewStore:
    """SQLite-backed lifecycle store. The single source of truth."""

    def __init__(
        self,
        path: str | Path,
        *,
        connection_factory: type[sqlite3.Connection] | None = None,
        clock: Any = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._conn = connect(self._path, factory=connection_factory)
        ensure_schema(self._conn)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ #
    # Runs
    # ------------------------------------------------------------------ #
    def record_run_start(self, run_id: str, *, mode: str = "", run_count: int = 0) -> None:
        self._conn.execute(
            "INSERT INTO runs (run_id, mode, run_count, started_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET mode = excluded.mode, "
            "run_count = excluded.run_count",
            (flatten_text(run_id), flatten_text(mode), int(run_count), self._now()),
        )

    def record_run_finish(self, summary: Mapping[str, Any]) -> None:
        """Store the runner's ``_finish()`` summary as the run's durable end."""
        run_id = flatten_text(summary.get("run_id"))
        if not run_id:
            raise ReviewStoreError("run summary has no run_id")
        self._conn.execute(
            """
            UPDATE runs SET finished_at = ?, final_state = ?, records_processed = ?,
                            repeat_passes_total = ?, decision_tally_json = ?,
                            summary_json = ?
            WHERE run_id = ?
            """,
            (
                self._now(),
                flatten_text(summary.get("final_state")),
                int(summary.get("processed") or 0),
                int(summary.get("repeat_passes_total") or 0),
                json_dumps(summary.get("decision_tally") or {}),
                json_dumps(dict(summary)),
                run_id,
            ),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (flatten_text(run_id),)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["decision_tally"] = json.loads(item.pop("decision_tally_json") or "{}")
        item["summary"] = json.loads(item.pop("summary_json") or "{}")
        return item

    # ------------------------------------------------------------------ #
    # Records (identity: dedupe key; a repeat updates the row in place)
    # ------------------------------------------------------------------ #
    def record_sighting(
        self,
        record_id: str,
        supplier_name: str,
        fields: Mapping[str, Any] | None = None,
    ) -> str:
        """Upsert one supplier sighting. Returns the record key."""
        key = dedupe_key(record_id, supplier_name)
        if not key:
            return ""
        now = self._now()
        existing = self._conn.execute(
            "SELECT record_key FROM records WHERE record_key = ?", (key,)
        ).fetchone()
        if existing:
            self._conn.execute(
                """
                UPDATE records SET record_id = ?, supplier_name = ?, last_seen = ?,
                                   times_seen = times_seen + 1, fields_json = ?
                WHERE record_key = ?
                """,
                (
                    flatten_text(record_id),
                    flatten_text(supplier_name),
                    now,
                    json_dumps(dict(fields or {})),
                    key,
                ),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO records (record_key, record_id, supplier_name, first_seen,
                                     last_seen, times_seen, fields_json)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    key,
                    flatten_text(record_id),
                    flatten_text(supplier_name),
                    now,
                    now,
                    json_dumps(dict(fields or {})),
                ),
            )
        return key

    def get_record(self, record_id: str, supplier_name: str = "") -> dict[str, Any] | None:
        key = dedupe_key(record_id, supplier_name)
        if not key:
            return None
        row = self._conn.execute(
            "SELECT * FROM records WHERE record_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["fields"] = json.loads(item.pop("fields_json") or "{}")
        return item

    # ------------------------------------------------------------------ #
    # Corrections
    # ------------------------------------------------------------------ #
    def record_correction(
        self,
        run_id: str,
        record_id: str,
        company_name: str,
        *,
        field: str,
        action: str,
        status: str,
        old_value: str = "",
        new_value: str = "",
        detail: str = "",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO corrections (run_id, record_id, company_name, field, action,
                                     status, old_value, new_value, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                flatten_text(run_id),
                flatten_text(record_id),
                flatten_text(company_name),
                flatten_text(field),
                flatten_text(action),
                flatten_text(status),
                flatten_text(old_value),
                flatten_text(new_value),
                flatten_text(detail),
                self._now(),
            ),
        )

    def corrections_for(self, record_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM corrections WHERE record_id = ? ORDER BY correction_id",
            (flatten_text(record_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Holds (manual reviews, field holds) + resolution
    # ------------------------------------------------------------------ #
    def record_manual_review(
        self,
        run_id: str,
        record_id: str,
        company_name: str,
        *,
        reason: str = "",
        food_beverage_connection: str = "",
        qualifying_supplier_types: Sequence[str] = (),
    ) -> int:
        """A MANUAL_REVIEW decision: never auto-decided, queued for a human."""
        cursor = self._conn.execute(
            """
            INSERT INTO holds (run_id, record_id, company_name, kind, detail,
                               detail_json, created_at)
            VALUES (?, ?, ?, 'manual_review', ?, ?, ?)
            """,
            (
                flatten_text(run_id),
                flatten_text(record_id),
                flatten_text(company_name),
                flatten_text(reason),
                json_dumps(
                    {
                        "manual_review_reason": flatten_text(reason),
                        "food_beverage_connection": flatten_text(food_beverage_connection),
                        "qualifying_supplier_types": [
                            flatten_text(t) for t in qualifying_supplier_types or ()
                        ],
                    }
                ),
                self._now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def record_field_hold(
        self,
        run_id: str,
        record_id: str,
        company_name: str,
        *,
        needs_clear: Iterable[Any] = (),
        needs_review: Iterable[Any] = (),
        identity_renamed: Iterable[Any] = (),
    ) -> int:
        """An ACCEPT that Auto Mode held back from Platform ready.

        The structured lists are kept as-is (field/value/detail items) so the
        held-for-field-review export can format the exact v34 text blocks.
        """
        cursor = self._conn.execute(
            """
            INSERT INTO holds (run_id, record_id, company_name, kind, detail,
                               detail_json, created_at)
            VALUES (?, ?, ?, 'field_hold', '', ?, ?)
            """,
            (
                flatten_text(run_id),
                flatten_text(record_id),
                flatten_text(company_name),
                json_dumps(
                    {
                        "needs_clear": [list(x) for x in needs_clear or ()],
                        "needs_review": [list(x) for x in needs_review or ()],
                        "identity_renamed": [list(x) for x in identity_renamed or ()],
                    }
                ),
                self._now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def holds(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM holds WHERE status = ? ORDER BY hold_id",
                (flatten_text(status),),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM holds ORDER BY hold_id").fetchall()
        return [self._hold_dict(r) for r in rows]

    def resolve_hold(self, hold_id: int, note: str = "") -> bool:
        cursor = self._conn.execute(
            "UPDATE holds SET status = 'resolved', resolution_note = ?, resolved_at = ? "
            "WHERE hold_id = ?",
            (flatten_text(note), self._now(), int(hold_id)),
        )
        return cursor.rowcount > 0

    @staticmethod
    def _hold_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
        return item

    # ------------------------------------------------------------------ #
    # Audit log: prompts, raw responses, website checks, discovery failures
    # ------------------------------------------------------------------ #
    def record_prompt(self, run_id: str, record_id: str, prompt_text: str) -> None:
        self._audit(run_id, record_id, "prompt", str(prompt_text))

    def record_response(self, run_id: str, record_id: str, response_text: str) -> None:
        self._audit(run_id, record_id, "response", str(response_text))

    def record_website_check(
        self,
        run_id: str,
        record_id: str,
        company_name: str,
        proposed_url: str,
        verdict: bool | None,
        detail: str,
    ) -> None:
        """The independent website sanity check - logged whatever the outcome."""
        self._audit(
            run_id,
            record_id,
            "website_check",
            flatten_text(detail),
            {
                "company_name": flatten_text(company_name),
                "proposed_url": flatten_text(proposed_url),
                # True/False/None kept typed; the export renders the legacy
                # verdict text (match / MISMATCH / inconclusive).
                "verdict": verdict,
            },
        )

    def record_discovery_failure(self, run_id: str, record_id: str, detail: str) -> None:
        self._audit(run_id, record_id, "discovery_failure", flatten_text(detail))

    def audit_events(self, record_id: str | None = None) -> list[dict[str, Any]]:
        if record_id is None:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY event_id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE record_id = ? ORDER BY event_id",
                (flatten_text(record_id),),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            out.append(item)
        return out

    def _audit(
        self,
        run_id: str,
        record_id: str,
        kind: str,
        detail: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO audit_log (run_id, record_id, kind, detail, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                flatten_text(run_id),
                flatten_text(record_id),
                kind,
                detail,
                json_dumps(dict(payload or {})),
                self._now(),
            ),
        )

    # ------------------------------------------------------------------ #
    # Evidence
    # ------------------------------------------------------------------ #
    def record_evidence(
        self, run_id: str, record_id: str, url: str, content: str = "", label: str = ""
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO evidence (run_id, record_id, url, label, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                flatten_text(run_id),
                flatten_text(record_id),
                flatten_text(url),
                flatten_text(label),
                str(content or ""),
                self._now(),
            ),
        )

    def evidence_for(self, record_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM evidence WHERE record_id = ? ORDER BY evidence_id",
            (flatten_text(record_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Supplier history (dedup view behind supplier_history.xlsx)
    # ------------------------------------------------------------------ #
    def record_history(
        self,
        record: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        outcome: str,
        mode: str,
        backend: str,
        finalized: bool,
        applied: Sequence[Any] = (),
        created: Sequence[Any] = (),
        cleared: Iterable[Any] = (),
        failed: Sequence[Any] = (),
        needs_clear: Iterable[Any] = (),
        needs_review: Iterable[Any] = (),
        identity_renamed: Iterable[Any] = (),
        corrected: Mapping[str, Any] | None = None,
    ) -> str:
        """Write one supplier's outcome (legacy history.record_decision).

        De-duplicates on the v34 key: a repeat updates its row in place with
        first_seen preserved and times_seen advanced; an outcome change moves
        the row between outcome views. Returns the verb (added/updated/moved).
        """
        row = build_history_row(
            record,
            result,
            outcome=outcome,
            mode=mode,
            backend=backend,
            applied=applied,
            created=created,
            cleared=cleared,
            failed=failed,
            needs_clear=needs_clear,
            needs_review=needs_review,
            identity_renamed=identity_renamed,
            corrected=corrected,
            now=self._now(),
        )
        sheet = sheet_for(result.get("decision"), finalized)
        with transaction(self._conn):
            return self._apply_history(row, sheet)

    def _apply_history(self, row: dict[str, Any], sheet: str) -> str:
        """Insert/update/move one history row (legacy history._apply port)."""
        key = dedupe_key(row.get("record_id"), row.get("company_name"))
        previous = None
        if key:
            found = self._conn.execute(
                "SELECT history_id, outcome_view, first_seen, times_seen "
                "FROM history WHERE dedupe_key = ? ORDER BY history_id LIMIT 1",
                (key,),
            ).fetchone()
            if found:
                previous = dict(found)

        if previous:
            # Carry the original sighting forward and count this pass.
            row["first_seen"] = flatten_text(previous["first_seen"]) or row["first_seen"]
            try:
                row["times_seen"] = int(previous["times_seen"] or 0) + 1
            except (TypeError, ValueError):
                row["times_seen"] = 2
            if previous["outcome_view"] == sheet:
                self._write_history_row(row, sheet, history_id=int(previous["history_id"]))
                return "updated"
            # Decision changed since last time (a held record resolved, a
            # reject revisited): remove the stale row so the supplier appears
            # once, in the view that now reflects reality - appended at the
            # end of it, exactly as the legacy workbook did.
            self._conn.execute(
                "DELETE FROM history WHERE history_id = ?", (previous["history_id"],)
            )
            verb = "moved"
        else:
            verb = "added"

        self._write_history_row(row, sheet)
        return verb

    def _write_history_row(
        self, row: dict[str, Any], sheet: str, history_id: int | None = None
    ) -> None:
        meta = [flatten_text(row.get(c)) for c in _HISTORY_META_DB_COLUMNS]
        detail = {k: v for k, v in row.items() if k not in _HISTORY_DB_KEYS}
        values = (
            dedupe_key(row.get("record_id"), row.get("company_name")),
            flatten_text(row.get("record_id")),
            flatten_text(row.get("company_name")),
            sheet,
            flatten_text(row.get("first_seen")),
            flatten_text(row.get("last_seen")),
            int(row.get("times_seen") or 1),
            *meta,
            json_dumps(detail),
            self._now(),
        )
        if history_id is None:
            columns = self._history_columns()
            self._conn.execute(
                f"INSERT INTO history ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
        else:
            assignments = ", ".join(f"{c} = ?" for c in self._history_columns())
            self._conn.execute(
                f"UPDATE history SET {assignments} WHERE history_id = ?",
                (*values, history_id),
            )

    @staticmethod
    def _history_columns() -> tuple[str, ...]:
        return (
            "dedupe_key",
            "record_id",
            "company_name",
            "outcome_view",
            "first_seen",
            "last_seen",
            "times_seen",
        ) + _HISTORY_META_DB_COLUMNS + ("fields_json", "updated_at")

    def history_rows(self) -> list[dict[str, Any]]:
        """All history rows in legacy row-dict shape (insertion order).

        Each row carries ``outcome_view`` - the sheet routing the store
        decided at write time (exports consume it and never print it).
        """
        meta_names = ("outcome_view", "first_seen", "last_seen", "times_seen", "record_id", "company_name") + (
            _HISTORY_META_DB_COLUMNS
        )
        # Name-based reads: record_id/company_name live in the identity
        # prefix of the SELECT, so positional slicing here would misalign.
        rows = self._conn.execute(
            f"SELECT {', '.join(self._history_columns())} FROM history ORDER BY history_id"
        ).fetchall()
        out = []
        for row in rows:
            item = {name: row[name] for name in meta_names}
            item.update(json.loads(row["fields_json"] or "{}"))
            out.append(item)
        return out

    # ------------------------------------------------------------------ #
    # Accepted companies (accepted_companies.xlsx)
    # ------------------------------------------------------------------ #
    def record_accepted_snapshot(
        self,
        record: Mapping[str, Any],
        corrected: Mapping[str, Any] | None,
        result: Mapping[str, Any],
        *,
        applied: Sequence[Any] = (),
        cleared: Iterable[Any] = (),
        failed: Sequence[Any] = (),
        needs_clear: Iterable[Any] = (),
        needs_review: Iterable[Any] = (),
        confirmed_by: str = "",
        verdict_status: str = ACCEPTED_STATUS,
    ) -> dict[str, Any]:
        """File one accepted company (legacy accepted_snapshots paths).

        Called for the reloaded record BEFORE the verdict with
        ``verdict_status=PENDING_STATUS`` (v32.1 order), then confirmed or
        rolled back. Returns ``{"previous": row_or_None}`` - the previously
        stored row, which :meth:`rollback_accept` can restore.
        """
        row = build_accepted_row(
            record,
            corrected,
            result,
            applied=applied,
            cleared=cleared,
            failed=failed,
            needs_clear=needs_clear,
            needs_review=needs_review,
            confirmed_by=confirmed_by,
            now=self._now(),
        )
        if verdict_status != ACCEPTED_STATUS:
            row["verdict_status"] = verdict_status
        decision = {
            k: result.get(k)
            for k in (
                "is_us_based",
                "supply_country",
                "supply_origin_note",
                "qualifying_supplier_types",
            )
        }
        with transaction(self._conn):
            previous = self._accepted_row_dict(
                dedupe_key(row.get("record_id"), row.get("company_name"))
            )
            verb = self._upsert_accepted(row, decision)
        return {"previous": previous, "verb": verb}

    def _accepted_row_dict(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        found = self._conn.execute(
            "SELECT * FROM accepted_companies WHERE dedupe_key = ? ORDER BY accepted_id LIMIT 1",
            (key,),
        ).fetchone()
        return self._accepted_assembled(found) if found else None

    def _upsert_accepted(self, row: dict[str, Any], decision: Mapping[str, Any]) -> str:
        """Upsert keyed on the dedupe key (legacy accepted _upsert port)."""
        key = dedupe_key(row.get("record_id"), row.get("company_name"))
        previous = self._accepted_row_dict(key)
        if previous is None:
            self._write_accepted_row(row, decision)
            return "added"
        # Keep any column the old row had that this read did not expose
        # (e.g. a note typed into the sheet by hand) instead of blanking it.
        row["first_accepted"] = (
            flatten_text(previous.get("first_accepted")) or row["first_accepted"]
        )
        try:
            row["times_accepted"] = int(previous.get("times_accepted") or 0) + 1
        except (TypeError, ValueError):
            row["times_accepted"] = 2
        previous_fields = json.loads(previous.get("_fields_json") or "{}")
        for k, v in previous_fields.items():
            row.setdefault(k, v)
        self._delete_accepted(key)
        self._write_accepted_row(row, decision)
        return "updated"

    def _delete_accepted(self, key: str) -> None:
        self._conn.execute("DELETE FROM accepted_companies WHERE dedupe_key = ?", (key,))

    def _write_accepted_row(self, row: dict[str, Any], decision: Mapping[str, Any]) -> None:
        detail = {
            k: v
            for k, v in row.items()
            if k not in ACCEPTED_FIXED_KEYS and not k.startswith("_")
        }
        self._conn.execute(
            f"INSERT INTO accepted_companies ({', '.join(ACCEPTED_DB_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in ACCEPTED_DB_COLUMNS)})",
            (
                dedupe_key(row.get("record_id"), row.get("company_name")),
                flatten_text(row.get("record_id")),
                flatten_text(row.get("company_name")),
                flatten_text(row.get("accepted_at")),
                flatten_text(row.get("verdict_status")),
                flatten_text(row.get("edit_status")),
                flatten_text(row.get("edits_made")),
                flatten_text(row.get("fields_failed")),
                flatten_text(row.get("fields_unresolved")),
                flatten_text(row.get("fields_not_landed")),
                flatten_text(row.get("confirmed_by")),
                flatten_text(row.get("snapshot_source")),
                flatten_text(row.get("first_accepted")),
                int(row.get("times_accepted") or 1),
                json_dumps(detail),
                json_dumps(dict(decision)),
            ),
        )

    def confirm_accept(self, record_id: str, company_name: str, confirmed_by: str = "") -> bool:
        """Platform ready landed: mark the pending row accepted (v32.1)."""
        key = dedupe_key(record_id, company_name)
        if not key:
            return False
        cursor = self._conn.execute(
            "UPDATE accepted_companies SET verdict_status = ?, confirmed_by = ? "
            "WHERE dedupe_key = ?",
            (ACCEPTED_STATUS, flatten_text(confirmed_by), key),
        )
        return cursor.rowcount > 0

    def rollback_accept(
        self,
        record_id: str,
        company_name: str,
        *,
        previous: Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> None:
        """The company was NOT accepted: remove the pending row, or restore
        the company's previous accepted snapshot."""
        del reason  # the caller logs the reason; the store only restores state
        key = dedupe_key(record_id, company_name)
        if not key:
            return
        with transaction(self._conn):
            if previous:
                self._delete_accepted(key)
                self._write_accepted_row(
                    dict(previous), dict(previous.get("_decision") or {})
                )
            else:
                self._delete_accepted(key)

    def accepted_rows(self) -> list[dict[str, Any]]:
        """All accepted rows in legacy row-dict shape (insertion order)."""
        rows = self._conn.execute(
            f"SELECT {', '.join(ACCEPTED_DB_COLUMNS)} FROM accepted_companies "
            "ORDER BY accepted_id"
        ).fetchall()
        return [self._accepted_assembled(row) for row in rows]

    def _accepted_assembled(self, row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "accepted_at": row["accepted_at"],
            "record_id": row["record_id"],
            "company_name": row["company_name"],
            "verdict_status": row["verdict_status"],
            "edit_status": row["edit_status"],
            "edits_made": row["edits_made"],
            "fields_failed": row["fields_failed"],
            "fields_unresolved": row["fields_unresolved"],
            "fields_not_landed": row["fields_not_landed"],
            "confirmed_by": row["confirmed_by"],
            "snapshot_source": row["snapshot_source"],
            "first_accepted": row["first_accepted"],
            "times_accepted": row["times_accepted"],
            "_decision": json.loads(row["decision_json"] or "{}"),
            "_fields_json": row["fields_json"],
        }
        item.update(json.loads(row["fields_json"] or "{}"))
        return item
