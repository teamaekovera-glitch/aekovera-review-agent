"""Schema and ReviewStore CRUD tests: versioning, dedupe, routing, lifecycle."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from review_hub.store import schema as store_schema
from review_hub.store.repository import ReviewStore
from review_hub.store.schema import SCHEMA_VERSION, ReviewStoreError, connect

EXPECTED_TABLES = {
    "runs",
    "records",
    "corrections",
    "holds",
    "audit_log",
    "evidence",
    "transitions",
    "history",
    "accepted_companies",
}


def advancing_clock(base: datetime | None = None):
    """Clock that returns later timestamps on every call."""
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


def record(record_id: str = "M-1001", name: str = "Alekovera Spice Co") -> dict:
    return {
        "record_id": record_id,
        "fields": {"company_name": name, "city": "Portland", "website_url": "https://x.test"},
    }


def result(decision: str = "accept", **overrides) -> dict:
    base = {
        "decision": decision,
        "company_name": "Alekovera Spice Co",
        "confidence": "high",
        "reason": "in scope",
        "scope_match": "yes",
        "food_beverage_connection": "ingredients for hot sauce",
        "qualifying_supplier_types": ["ingredients"],
        "supply_country": "USA",
        "is_us_based": True,
        "supply_origin_note": "",
        "manual_review_reason": "",
        "changes": [{"field": "city", "new_value": "Portland, OR", "old_value": ""}],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Schema versioning
# --------------------------------------------------------------------------- #
def test_fresh_store_is_at_current_schema_version(store: ReviewStore) -> None:
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_fresh_store_has_every_lifecycle_table(store: ReviewStore) -> None:
    with sqlite3.connect(store.path) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert EXPECTED_TABLES <= tables


def test_refuses_a_store_written_by_newer_code(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    conn = connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(ReviewStoreError, match="newer than this code"):
        connect(path) and store_schema.ensure_schema(connect(path))


def test_wal_mode_is_on(store: ReviewStore) -> None:
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def test_run_start_and_finish_roundtrip(store: ReviewStore) -> None:
    store.record_run_start("run-1", mode="auto", run_count=5)
    store.record_run_finish(
        {
            "run_id": "run-1",
            "final_state": "idle",
            "processed": 5,
            "repeat_passes_total": 2,
            "decision_tally": {"ACCEPT": 3, "REJECT": 2},
        }
    )
    run = store.get_run("run-1")
    assert run is not None
    assert run["mode"] == "auto"
    assert run["records_processed"] == 5
    assert run["decision_tally"] == {"ACCEPT": 3, "REJECT": 2}
    assert run["summary"]["repeat_passes_total"] == 2
    assert run["started_at"] and run["finished_at"]


def test_run_finish_without_run_id_is_rejected(store: ReviewStore) -> None:
    with pytest.raises(ReviewStoreError):
        store.record_run_finish({})


# --------------------------------------------------------------------------- #
# Records: identity = dedupe key; a repeat updates in place
# --------------------------------------------------------------------------- #
def test_record_sighting_insert_then_repeat_updates_in_place(store: ReviewStore) -> None:
    key = store.record_sighting("M-1001", "Alekovera Spice Co", {"city": "Portland"})
    assert key == "id:m-1001"
    first = store.get_record("M-1001")
    assert first is not None and first["times_seen"] == 1
    assert first["first_seen"] == first["last_seen"]

    store.record_sighting("M-1001", "Alekovera Spice Co", {"city": "Portland, OR"})
    rows = store._conn.execute("SELECT * FROM records").fetchall()
    assert len(rows) == 1
    second = store.get_record("M-1001")
    assert second["times_seen"] == 2
    assert second["first_seen"] == first["first_seen"]  # original sighting preserved
    assert second["last_seen"] >= first["last_seen"]
    assert second["fields"]["city"] == "Portland, OR"


def test_record_sighting_falls_back_to_normalised_name(store: ReviewStore) -> None:
    key = store.record_sighting("", "Clayton's Crab Co.")
    assert key == "name:claytons crab"


# --------------------------------------------------------------------------- #
# Corrections
# --------------------------------------------------------------------------- #
def test_corrections_roundtrip(store: ReviewStore) -> None:
    store.record_correction(
        "run-1",
        "M-1001",
        "Alekovera Spice Co",
        field="city",
        action="update",
        status="applied",
        old_value="Portland",
        new_value="Portland, OR",
    )
    rows = store.corrections_for("M-1001")
    assert len(rows) == 1
    assert rows[0]["field"] == "city"
    assert rows[0]["status"] == "applied"
    assert rows[0]["new_value"] == "Portland, OR"


# --------------------------------------------------------------------------- #
# Holds
# --------------------------------------------------------------------------- #
def test_manual_review_hold_roundtrip(store: ReviewStore) -> None:
    store.record_manual_review(
        "run-1",
        "M-1002",
        "Scope Unknown LLC",
        reason="scope uncertain",
        food_beverage_connection="packaging only",
        qualifying_supplier_types=["packaging"],
    )
    holds = store.holds()
    assert len(holds) == 1
    assert holds[0]["kind"] == "manual_review"
    assert holds[0]["detail"]["manual_review_reason"] == "scope uncertain"
    assert holds[0]["detail"]["qualifying_supplier_types"] == ["packaging"]

    assert store.resolve_hold(holds[0]["hold_id"], note="reviewed by hand")
    resolved = store.holds(status="resolved")
    assert resolved and resolved[0]["resolution_note"] == "reviewed by hand"
    assert resolved[0]["resolved_at"]


def test_field_hold_keeps_structured_lists(store: ReviewStore) -> None:
    store.record_field_hold(
        "run-1",
        "M-1003",
        "Held Foods",
        needs_clear=[("products", "???")],
        needs_review=[("website_url", "https://x.test", "no trace of company")],
        identity_renamed=[("company_name", "Held Foods Inc", "Held Foods")],
    )
    detail = store.holds()[0]["detail"]
    assert detail["needs_clear"] == [["products", "???"]]
    assert detail["needs_review"] == [["website_url", "https://x.test", "no trace of company"]]
    assert detail["identity_renamed"] == [["company_name", "Held Foods Inc", "Held Foods"]]


# --------------------------------------------------------------------------- #
# Audit log: prompts, raw responses, website checks, discovery failures
# --------------------------------------------------------------------------- #
def test_prompts_and_raw_responses_are_persisted_verbatim(store: ReviewStore) -> None:
    prompt = "RESEARCH PROMPT:\n\nLook at this supplier... {json blob}"
    response = '{"decision": "ACCEPT", "confidence": "high"}'
    store.record_prompt("run-1", "M-1001", prompt)
    store.record_response("run-1", "M-1001", response)

    events = store.audit_events("M-1001")
    assert [e["kind"] for e in events] == ["prompt", "response"]
    assert events[0]["detail"] == prompt
    assert events[1]["detail"] == response


def test_website_check_keeps_typed_verdict(store: ReviewStore) -> None:
    store.record_website_check("run-1", "M-1001", "Alekovera Spice Co", "https://x.test", False, "MISMATCH: site sells shoes")
    store.record_website_check("run-1", "M-1001", "Alekovera Spice Co", "https://x.test", None, "inconclusive load")
    store.record_website_check("run-1", "M-1001", "Alekovera Spice Co", "https://x.test", True, "match")

    payload_verdicts = [e["payload"]["verdict"] for e in store.audit_events("M-1001")]
    assert payload_verdicts == [False, None, True]
    assert store.audit_events("M-1001")[0]["payload"]["proposed_url"] == "https://x.test"


def test_discovery_failure_is_logged(store: ReviewStore) -> None:
    store.record_discovery_failure("run-1", "", "no record id found after 3 retries")
    events = store.audit_events()
    assert events[0]["kind"] == "discovery_failure"
    assert "3 retries" in events[0]["detail"]


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
def test_evidence_roundtrip(store: ReviewStore) -> None:
    store.record_evidence("run-1", "M-1001", "https://x.test/about", content="<html>about</html>", label="about page")
    rows = store.evidence_for("M-1001")
    assert len(rows) == 1
    assert rows[0]["content"] == "<html>about</html>"
    assert rows[0]["label"] == "about page"


# --------------------------------------------------------------------------- #
# Supplier history: dedupe, update-in-place, outcome routing
# --------------------------------------------------------------------------- #
def test_history_dedup_repeats_update_in_place(store: ReviewStore) -> None:
    verb1 = store.record_history(
        record(), result(), outcome="accepted", mode="auto", backend="manual", finalized=True
    )
    assert verb1 == "added"
    first = store.history_rows()[0]
    first_seen = first["first_seen"]

    verb2 = store.record_history(
        record(), result(), outcome="accepted", mode="auto", backend="manual", finalized=True
    )
    assert verb2 == "updated"
    rows = store.history_rows()
    assert len(rows) == 1
    assert rows[0]["times_seen"] == 2
    assert rows[0]["first_seen"] == first_seen  # original sighting preserved
    assert rows[0]["decision"] == "ACCEPT"


def test_history_row_moves_between_outcome_views(store: ReviewStore) -> None:
    store.record_history(
        record(), result("accept"), outcome="accepted", mode="auto", backend="manual", finalized=True
    )
    # Same supplier, later run: the decision changed (e.g. held then rejected).
    store.record_history(
        record(), result("reject"), outcome="rejected", mode="auto", backend="manual", finalized=True
    )
    rows = store.history_rows()
    assert len(rows) == 1  # one supplier, one row - never two
    assert rows[0]["outcome_view"] == "Rejected"


def test_history_unfinalized_always_routes_to_manual_review_sheet(store: ReviewStore) -> None:
    store.record_history(
        record(),
        result("accept"),
        outcome="held for field review",
        mode="auto",
        backend="manual",
        finalized=False,
    )
    assert store.history_rows()[0]["outcome_view"] == "Manual review & held"


def test_history_final_view_overlays_applied_changes(store: ReviewStore) -> None:
    store.record_history(
        record(),
        result(),
        outcome="accepted",
        mode="auto",
        backend="manual",
        finalized=True,
        applied=["city"],
    )
    row = store.history_rows()[0]
    assert row["city"] == "Portland, OR"  # applied correction visible in the export row


# --------------------------------------------------------------------------- #
# Accepted companies: pending -> confirm / rollback (v32.1 order)
# --------------------------------------------------------------------------- #
def accepted_inputs() -> tuple[dict, dict, dict]:
    rec = record()
    res = result("accept")
    return rec, {"fields": {"company_name": "Alekovera Spice Co", "city": "Portland, OR"}, "record_id": "M-1001"}, res


def test_accepted_snapshot_added_then_updated_in_place(store: ReviewStore) -> None:
    rec, corrected, res = accepted_inputs()
    store.record_accepted_snapshot(rec, corrected, res, applied=["city"])
    first = store.accepted_rows()[0]
    first_accepted = first["first_accepted"]

    store.record_accepted_snapshot(rec, corrected, res, applied=["city"])
    rows = store.accepted_rows()
    assert len(rows) == 1
    assert rows[0]["times_accepted"] == 2
    assert rows[0]["first_accepted"] == first_accepted
    assert rows[0]["verdict_status"] == "accepted"


def test_accepted_pending_then_confirm(store: ReviewStore) -> None:
    rec, corrected, res = accepted_inputs()
    store.record_accepted_snapshot(
        rec, corrected, res, verdict_status="pending - written before Platform ready"
    )
    assert store.accepted_rows()[0]["verdict_status"].startswith("pending")
    assert store.confirm_accept("M-1001", "Alekovera Spice Co", confirmed_by="auto:verified")
    row = store.accepted_rows()[0]
    assert row["verdict_status"] == "accepted"
    assert row["confirmed_by"] == "auto:verified"


def test_accepted_rollback_restores_previous_snapshot(store: ReviewStore) -> None:
    rec, corrected, res = accepted_inputs()
    first = store.record_accepted_snapshot(rec, corrected, res, applied=["city"])
    assert first["previous"] is None

    # Second acceptance, then the verdict failed: roll back to the first row.
    second = store.record_accepted_snapshot(rec, corrected, res, applied=["city"])
    previous = second["previous"]
    assert previous is not None
    store.rollback_accept("M-1001", "Alekovera Spice Co", previous=previous)

    rows = store.accepted_rows()
    assert len(rows) == 1
    assert rows[0]["times_accepted"] == 1


def test_accepted_rollback_without_previous_removes_pending_row(store: ReviewStore) -> None:
    rec, corrected, res = accepted_inputs()
    store.record_accepted_snapshot(
        rec, corrected, res, verdict_status="pending - written before Platform ready"
    )
    store.rollback_accept("M-1001", "Alekovera Spice Co")
    assert store.accepted_rows() == []
