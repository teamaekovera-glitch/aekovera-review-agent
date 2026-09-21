"""Server surface tests: config, queues, inspection, exports, auth.

These run against a live app (lifespan on) with the fake browser session and
default backends, but never launch a run worker - the run-execution flows
live in test_server_runs.py.
"""

from __future__ import annotations

import json

from conftest import reject_result

from review_hub.server.app import EXPORT_SPECS
from review_hub.store.repository import ReviewStore

SECRET = "OPENROUTER_API_KEY"


# --------------------------------------------------------------------- #
# Index / posture
# --------------------------------------------------------------------- #
def test_index_reports_local_posture(make_hub):
    with make_hub() as (client, _factory, _path):
        body = client.get("/").json()
        assert body["service"] == "aekovera-review-hub"
        assert "localhost-only" in body["posture"]


def test_access_token_required_when_configured(make_hub):
    with make_hub(access_token="t0ken") as (client, _factory, _path):
        assert client.get("/api/settings").status_code == 401
        ok = client.get("/api/settings", headers={"X-Access-Token": "t0ken"})
        assert ok.status_code == 200


def test_no_token_needed_by_default(make_hub):
    with make_hub() as (client, _factory, _path):
        assert client.get("/api/settings").status_code == 200


# --------------------------------------------------------------------- #
# Settings: read redacted, edit validated
# --------------------------------------------------------------------- #
def test_settings_read_never_echoes_secrets(make_hub):
    with make_hub() as (client, _factory, _path):
        body = client.get("/api/settings").json()
        assert isinstance(body, dict) and body
        # The secret is reported as configured/not-configured - the knob name
        # is public, but its value never appears in a response.
        entry = body.get(SECRET)
        assert entry is None or (isinstance(entry, dict) and set(entry) <= {"configured"})


def test_settings_edit_rejects_unknown_key(make_hub):
    with make_hub() as (client, _factory, _path):
        response = client.put("/api/settings", json={"NO_SUCH_KNOB": "1"})
        assert response.status_code == 422


def test_settings_edit_rejects_secret_and_bad_value(make_hub):
    with make_hub() as (client, _factory, _path):
        assert client.put("/api/settings", json={SECRET: "x"}).status_code == 422
        response = client.put("/api/settings", json={"MAX_REPEAT_PASSES": "many"})
        assert response.status_code == 422


def test_settings_edit_roundtrip(make_hub):
    with make_hub() as (client, _factory, _path):
        before = client.put("/api/settings", json={"MAX_REPEAT_PASSES": 2}).json()
        assert before["MAX_REPEAT_PASSES"] == 2
        after = client.get("/api/settings").json()
        assert after["MAX_REPEAT_PASSES"] == 2
        # The override persists next to the database for the next boot.
        overrides = _path.parent / "settings_overrides.json"
        assert json.loads(overrides.read_text())["MAX_REPEAT_PASSES"] == 2


# --------------------------------------------------------------------- #
# Queues: manual-review and held, split by kind, resolvable
# --------------------------------------------------------------------- #
def test_queues_split_by_kind_and_resolve(make_hub):
    with make_hub() as (client, _factory, path):
        seed = ReviewStore(path)
        manual_id = seed.record_manual_review("r-1", "MST-1", "Acme Rice", reason="needs eyes")
        held_id = seed.record_field_hold("r-1", "MST-2", "Beacon", needs_clear=[["w", "v"]])
        seed.close()

        manual = client.get("/api/queues/manual-review").json()["holds"]
        held = client.get("/api/queues/held").json()["holds"]
        assert [h["hold_id"] for h in manual] == [manual_id]
        assert [h["hold_id"] for h in held] == [held_id]

        resolved = client.post(
            f"/api/queues/manual-review/{manual_id}/resolve",
            json={"outcome": "rejected", "note": "trading company"},
        )
        assert resolved.status_code == 200
        assert client.get("/api/queues/manual-review").json()["holds"] == []
        remaining = ReviewStore(path)
        row = remaining.holds(status="resolved")[0]
        assert "rejected" in row["resolution_note"] and "trading company" in row["resolution_note"]
        remaining.close()


def test_resolve_unknown_hold_404(make_hub):
    with make_hub() as (client, _factory, _path):
        response = client.post("/api/queues/manual-review/999/resolve", json={})
        assert response.status_code == 404


# --------------------------------------------------------------------- #
# Inspection: history, accepted companies, audit/evidence
# --------------------------------------------------------------------- #
def test_history_listing_and_supplier_filter(make_hub):
    with make_hub() as (client, _factory, path):
        seed = ReviewStore(path)
        seed.record_history(
            {"record_id": "MST-1", "fields": {"company_name": "Acme Rice"}, "missing_fields": []},
            reject_result(),
            outcome="rejected",
            mode="auto",
            backend="manual",
            finalized=True,
        )
        seed.close()

        rows = client.get("/api/history").json()["history"]
        assert [r["company_name"] for r in rows] == ["Acme Rice"]
        miss = client.get("/api/history", params={"supplier": "nobody"}).json()["history"]
        assert miss == []


def test_accepted_companies_endpoint(make_hub):
    with make_hub() as (client, _factory, path):
        seed = ReviewStore(path)
        seed.record_accepted_snapshot(
            {"record_id": "MST-1", "fields": {"company_name": "Acme Rice"}, "missing_fields": []},
            None,
            reject_result(),
        )
        seed.close()
        rows = client.get("/api/accepted-companies").json()["accepted"]
        assert [r["company_name"] for r in rows] == ["Acme Rice"]


def test_audit_endpoint_returns_events_and_evidence(make_hub):
    with make_hub() as (client, _factory, path):
        seed = ReviewStore(path)
        seed.record_prompt("r-1", "MST-1", "the prompt")
        seed.record_evidence(
            "r-1", "MST-1", "https://acmerice.example", content="hello", label="home"
        )
        seed.close()

        body = client.get("/api/audit/MST-1").json()
        assert [e["kind"] for e in body["events"]] == ["prompt"]
        assert [e["url"] for e in body["evidence"]] == ["https://acmerice.example"]
        assert body["record_id"] == "MST-1"


# --------------------------------------------------------------------- #
# Exports: the seven v34-parity artifacts with their content types
# --------------------------------------------------------------------- #
def test_every_registered_export_serves_with_content_type(make_hub):
    with make_hub() as (client, _factory, _path):
        for name, (media, _filename, _builder) in EXPORT_SPECS.items():
            response = client.get(f"/export/{name}")
            assert response.status_code == 200, name
            assert response.headers["content-type"].startswith(media), name
            assert len(response.content) > 0, name


def test_unknown_export_404(make_hub):
    with make_hub() as (client, _factory, _path):
        assert client.get("/export/not-a-real-export.csv").status_code == 404


def test_decision_feed_serves_finalized_ledger_rows(make_hub):
    with make_hub() as (client, _factory, path):
        seed = ReviewStore(path)
        seed.record_research(
            "r-1",
            "MST-1",
            "Acme Rice",
            kind="supplier",
            decision="REJECT",
            result=reject_result(),
            finalized=True,
        )
        seed.close()
        body = client.get("/api/queues/decisions").json()["decisions"]
        assert [d["record_id"] for d in body] == ["MST-1"]
        assert body[0]["finalized"] is True


def test_manual_request_404_without_run(make_hub):
    with make_hub() as (client, _factory, _path):
        assert client.get("/api/runs/nope/manual-response").status_code == 404
        paste = client.post("/api/runs/nope/manual-response", json={"raw_response": "{}"})
        assert paste.status_code == 404
