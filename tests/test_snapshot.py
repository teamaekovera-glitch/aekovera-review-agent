"""Regression tests for the final-snapshot fix (reload -> extract -> compare).

Pytest port of legacy/aekovera/test_snapshot.py. The bug: edits are saved
with HTTP POSTs that never refresh the browser tab, so the final snapshot
read the page as it was BEFORE the corrections and accepted_companies.xlsx
received the original data ("no changes needed"). These tests use a fake
page whose fields only change when reload() is called, which is exactly how
the real review tab behaves.

The accepted_companies.xlsx workbook round-trip and the history-row checks
from the legacy file target the Excel/CSV output modules, which are
deliberately deferred to the storage task (they will be replaced by the
SQLite system of record); everything pinning fetch_corrected_record,
find_unlanded_changes, and ReloadDriftError is ported here.
"""

import pytest

from review_hub.engine import corrections


class FakeReviewTab:
    """Shows `rendered` until reload() re-renders it from `server`."""

    def __init__(self, server, unit="U-1", master="MST-03682", serve_after_reload=None):
        self.server = dict(server)
        self.rendered = dict(server)
        self.unit = unit
        self.master = master
        self.reloads = 0
        self.serve_after_reload = serve_after_reload

    def reload(self, **_kw):
        self.reloads += 1
        if self.serve_after_reload:
            self.unit, self.master, self.server = self.serve_after_reload
        self.rendered = dict(self.server)


@pytest.fixture()
def install_fakes(monkeypatch):
    def _install(tab):
        monkeypatch.setattr(
            corrections, "read_card_identity", lambda page: (page.unit, "nonce-" + page.unit)
        )
        monkeypatch.setattr(corrections, "read_record_id", lambda page: page.master)
        monkeypatch.setattr(corrections, "wait_for_page_settle", lambda page, max_ms=None: True)
        monkeypatch.setattr(
            corrections, "discover_fields", lambda page: [{"field": k} for k in page.rendered]
        )
        monkeypatch.setattr(
            corrections,
            "read_field_values",
            lambda page, known: {k: v for k, v in page.rendered.items() if k in known},
        )

    return _install


ORIGINAL = {
    "company_name": "Vermont Smoke and Cure",
    "primary_email": "spm@smingredients.com",
    "primary_phone": "714-348-7685",
    "zip": "12345",
    "website_url": "vtsmokeandcure.com",
    "supplier_type": "Food Manufacturer / Brand",
}
CHANGES = [
    {"field": "primary_email", "new_value": "info@vtsmokeandcure.com"},
    {"field": "primary_phone", "new_value": "+18024824666"},
    {"field": "zip", "new_value": "05461"},
]
APPLIED = ["primary_email", "primary_phone", "zip"]


# ---------------------------------------------------------------------------
# The bug: without a reload the page still shows the old values
# ---------------------------------------------------------------------------

def test_without_reload_the_page_still_shows_old_values():
    tab = FakeReviewTab(ORIGINAL)
    tab.server.update(
        {"primary_email": "info@vtsmokeandcure.com", "primary_phone": "8024824666", "zip": "05461"}
    )
    assert tab.rendered["primary_email"] == "spm@smingredients.com"


# ---------------------------------------------------------------------------
# The fix: fetch_corrected_record reloads, then reads the saved values
# ---------------------------------------------------------------------------

def test_snapshot_reloads_then_reads_saved_values(install_fakes):
    tab = FakeReviewTab(ORIGINAL)
    tab.server.update(
        {"primary_email": "info@vtsmokeandcure.com", "primary_phone": "8024824666", "zip": "05461"}
    )
    install_fakes(tab)

    got = corrections.fetch_corrected_record(
        tab, "MST-03682", result={"changes": CHANGES}, applied=APPLIED, cleared=[]
    )
    assert tab.reloads == 1  # reloaded exactly once
    assert got["fields"]["primary_email"] == "info@vtsmokeandcure.com"
    assert got["fields"]["zip"] == "05461"
    # phone saved as +18024824666 matches page 8024824666
    assert not got["not_landed"]


# ---------------------------------------------------------------------------
# A correction that did not land is reported
# ---------------------------------------------------------------------------

def test_unlanded_correction_is_reported(install_fakes):
    tab = FakeReviewTab(ORIGINAL)
    tab.server.update({"primary_email": "info@vtsmokeandcure.com", "zip": "05461"})
    install_fakes(tab)

    got = corrections.fetch_corrected_record(
        tab, "MST-03682", result={"changes": CHANGES}, applied=APPLIED, cleared=[]
    )
    assert [f for f, _e, _p in got["not_landed"]] == ["primary_phone"]


def test_unlanded_clear_is_reported(install_fakes):
    tab = FakeReviewTab(dict(ORIGINAL, linkedin_url="https://linkedin.com/company/other"))
    install_fakes(tab)

    got = corrections.fetch_corrected_record(
        tab,
        "MST-03682",
        result={"changes": []},
        applied=[],
        cleared=[("linkedin_url", "belongs to another company")],
    )
    assert got["not_landed"] and got["not_landed"][0][0] == "linkedin_url"


def test_supplier_type_separator_differences_are_not_false_alarms():
    assert not corrections.find_unlanded_changes(
        {"supplier_type": "Private Label Manufacturer, Co-Packer"},
        [{"field": "supplier_type", "new_value": "Co-Packer | Private Label Manufacturer"}],
        ["supplier_type"],
    )


# ---------------------------------------------------------------------------
# Safety: a reload that serves a different card stops everything
# ---------------------------------------------------------------------------

def test_reload_drift_raises(install_fakes):
    tab = FakeReviewTab(
        ORIGINAL, serve_after_reload=("U-2", "MST-99999", {"company_name": "Other Co"})
    )
    install_fakes(tab)
    with pytest.raises(corrections.ReloadDriftError):
        corrections.fetch_corrected_record(
            tab, "MST-03682", result={"changes": CHANGES}, applied=APPLIED
        )
