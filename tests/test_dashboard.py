"""Dashboard tests: server-rendered pages over the existing API.

The dashboard adapts to the JSON API, so these tests drive the same store and
fake session the API tests use and assert on rendered HTML: every screen
renders, queues surface the right states with their reasons, the paste flow
works end-to-end through the UI endpoints' API counterparts, export links are
present, and secrets never appear.
"""

from __future__ import annotations

import json

from conftest import reject_result, wait_for_pending_request, wait_for_status

from review_hub.store.repository import ReviewStore


def paste_for(result: dict) -> str:
    """The operator pastes ChatGPT's raw response: prose around the JSON."""
    return "Here is the review you asked for:\n" + json.dumps(result) + "\nHope that helps!"


def reject_paste() -> str:
    return paste_for(reject_result())


def test_index_advertises_dashboard(make_hub):
    with make_hub() as (client, _factory, _path):
        body = client.get("/").json()
        assert body["dashboard"] == "/dashboard"
        assert "localhost-only" in body["posture"]  # the API contract is intact


# --------------------------------------------------------------------- #
# Pages render
# --------------------------------------------------------------------- #
def test_dashboard_overview_renders_runs_and_form(make_hub):
    with make_hub() as (client, _factory, _path):
        run_id = client.post("/api/runs", json={"backend": "manual", "limit": 1}).json()["run_id"]
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert run_id in response.text
        assert "New run" in response.text


def test_dashboard_run_page_unknown_run_404(make_hub):
    with make_hub() as (client, _factory, _path):
        assert client.get("/dashboard/runs/nope").status_code == 404


def test_dashboard_static_assets_served_offline(make_hub):
    with make_hub() as (client, _factory, _path):
        css = client.get("/dashboard/static/dashboard.css")
        js = client.get("/dashboard/static/dashboard.js")
        assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        # Zero-cost / offline-first: no external fetches in the assets.
        for text in (css.text, js.text):
            assert "http://" not in text and "https://" not in text


def test_dashboard_pages_require_token_when_configured(make_hub):
    with make_hub(access_token="t0ken") as (client, _factory, _path):
        assert client.get("/dashboard").status_code == 401
        ok = client.get("/dashboard", headers={"X-Access-Token": "t0ken"})
        assert ok.status_code == 200


# --------------------------------------------------------------------- #
# The operator's primary loop: paste box on the run page
# --------------------------------------------------------------------- #
def test_run_page_renders_paste_box_then_finalized_record(make_hub):
    with make_hub() as (client, _factory, _path):
        run_id = client.post("/api/runs", json={"backend": "manual", "limit": 1}).json()["run_id"]
        wait_for_status(client, run_id, "awaiting_manual")

        page = client.get(f"/dashboard/runs/{run_id}")
        assert page.status_code == 200
        assert "awaiting_manual" in page.text
        assert "Acme Rice" in page.text  # the prompt is surfaced
        assert "MST-2001" in page.text

        assert (
            client.post(
                f"/api/runs/{run_id}/manual-response", json={"raw_response": reject_paste()}
            ).status_code
            == 202
        )
        wait_for_status(client, run_id, "completed")

        page = client.get(f"/dashboard/runs/{run_id}")
        assert "MST-2001" in page.text and "REJECT" in page.text
        # The paste box closed: the section renders hidden when nothing is pending.
        paste_section = page.text.split('id="paste-box"', 1)[1].split(">", 1)[0]
        assert "hidden" in paste_section


def test_run_page_shows_invalid_paste_repark_reason(make_hub):
    with make_hub() as (client, _factory, _path):
        run_id = client.post("/api/runs", json={"backend": "manual", "limit": 1}).json()["run_id"]
        wait_for_status(client, run_id, "awaiting_manual")
        client.post(
            f"/api/runs/{run_id}/manual-response",
            json={"raw_response": "this is not json at all"},
        )
        request = wait_for_pending_request(client, run_id, timeout=15.0)
        assert request["error"]

        page = client.get(f"/dashboard/runs/{run_id}")
        assert "not usable" in page.text  # the re-park reason is shown, not swallowed
        assert "re-parked" in page.text
        records = client.get(f"/api/runs/{run_id}/records").json()["records"]
        assert records == []


# --------------------------------------------------------------------- #
# Queues: the four holds surface with their reasons, resolvable
# --------------------------------------------------------------------- #
def test_queues_page_surfaces_holds_with_reasons_and_resolve(make_hub):
    with make_hub() as (client, _factory, path):
        seed = ReviewStore(path)
        manual_id = seed.record_manual_review("r-1", "MST-1", "Acme Rice", reason="needs eyes")
        held_id = seed.record_field_hold(
            "r-1",
            "MST-2",
            "Beacon",
            needs_clear=[["website_url", "https://beacon.example"]],
            identity_renamed=[["company_name", "Beacon Packaging Co"]],
        )
        seed.close()

        page = client.get("/dashboard/queues")
        assert page.status_code == 200
        assert "Acme Rice" in page.text and "needs eyes" in page.text
        assert "Beacon" in page.text
        assert "website_url" in page.text and "(needs clear)" in page.text
        assert "(renamed)" in page.text

        # Resolving through the same API the forms post to clears the screen.
        client.post(
            f"/api/queues/manual-review/{manual_id}/resolve",
            json={"outcome": "rejected", "note": "trading company"},
        )
        client.post(
            f"/api/queues/held/{held_id}/resolve",
            json={"outcome": "accepted", "note": "verified"},
        )
        page = client.get("/dashboard/queues")
        assert "needs eyes" not in page.text
        assert "this queue is clear" in page.text


# --------------------------------------------------------------------- #
# Inspection: history, accepted, exports, audit
# --------------------------------------------------------------------- #
def test_history_page_lists_rows_and_export_links(make_hub):
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

        page = client.get("/dashboard/history")
        assert page.status_code == 200
        assert "Acme Rice" in page.text and "rejected" in page.text
        assert 'href="/export/supplier-history.xlsx"' in page.text
        assert 'href="/export/website-verify-log.csv"' in page.text


def test_accepted_page_lists_companies_and_export_links(make_hub):
    with make_hub() as (client, _factory, path):
        seed = ReviewStore(path)
        seed.record_accepted_snapshot(
            {"record_id": "MST-1", "fields": {"company_name": "Acme Rice"}, "missing_fields": []},
            None,
            reject_result(),
        )
        seed.close()

        page = client.get("/dashboard/accepted")
        assert page.status_code == 200
        assert "Acme Rice" in page.text
        assert 'href="/export/accepted-companies.xlsx"' in page.text


def test_audit_page_renders_events_and_evidence(make_hub):
    with make_hub() as (client, _factory, path):
        seed = ReviewStore(path)
        seed.record_prompt("r-1", "MST-1", "the research prompt text")
        seed.record_evidence(
            "r-1", "MST-1", "https://acmerice.example", content="hello", label="home"
        )
        seed.close()

        page = client.get("/dashboard/audit/MST-1")
        assert page.status_code == 200
        assert "the research prompt text" in page.text
        assert "https://acmerice.example" in page.text

        lookup = client.get(
            "/dashboard/audit", params={"record_id": "MST-1"}, follow_redirects=False
        )
        assert lookup.status_code == 303
        assert lookup.headers["location"] == "/dashboard/audit/MST-1"


# --------------------------------------------------------------------- #
# Settings: read-only view of the allowlist, secret never echoed
# --------------------------------------------------------------------- #
def test_settings_page_lists_knobs_without_secrets(make_hub):
    with make_hub() as (client, _factory, _path):
        page = client.get("/dashboard/settings")
        assert page.status_code == 200
        assert "MAX_RECORDS" in page.text and "RESEARCH_BACKEND" in page.text
        # The secret's knob name is public; its value never reaches the UI -
        # the page renders a configured/not-configured badge only, no input.
        assert "OPENROUTER_API_KEY" in page.text
        assert 'name="OPENROUTER_API_KEY"' not in page.text
        assert "set it in the environment, never via the API" in page.text


def test_settings_edit_via_api_reflects_on_page(make_hub):
    with make_hub() as (client, _factory, _path):
        assert client.put("/api/settings", json={"MAX_REPEAT_PASSES": 2}).status_code == 200
        page = client.get("/dashboard/settings")
        assert 'value="2"' in page.text  # the edited value renders (typed input)
