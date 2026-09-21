"""Regression tests for the final-snapshot fix (reload -> extract -> file -> verdict).

Run with:  python test_snapshot.py

The bug: edits are saved with HTTP POSTs that never refresh the browser tab,
so fetch_corrected_record() read the page as it was BEFORE the corrections
and accepted_companies.xlsx received the original data ("no changes needed").
These tests use a fake page whose fields only change when reload() is called,
which is exactly how the real review tab behaves.
"""

import os
import sys
import tempfile

import main
import accepted_snapshots as snap
import history

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(name)


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


def install_fakes(tab):
    main.read_card_identity = lambda page: (page.unit, "nonce-" + page.unit)
    main.read_record_id = lambda page: page.master
    main.wait_for_page_settle = lambda page, max_ms=None: True
    main.discover_fields = lambda page: [{"field": k} for k in page.rendered]
    main.read_field_values = lambda page, known: {k: v for k, v in page.rendered.items() if k in known}


ORIGINAL = {"company_name": "Vermont Smoke and Cure", "primary_email": "spm@smingredients.com",
            "primary_phone": "714-348-7685", "zip": "12345", "website_url": "vtsmokeandcure.com",
            "supplier_type": "Food Manufacturer / Brand"}
CHANGES = [
    {"field": "primary_email", "new_value": "info@vtsmokeandcure.com"},
    {"field": "primary_phone", "new_value": "+18024824666"},
    {"field": "zip", "new_value": "05461"},
]
APPLIED = ["primary_email", "primary_phone", "zip"]

# ---------------------------------------------------------------------------
print("\nThe bug: without a reload the page still shows the old values")
# ---------------------------------------------------------------------------
tab = FakeReviewTab(ORIGINAL)
tab.server.update({"primary_email": "info@vtsmokeandcure.com", "primary_phone": "8024824666", "zip": "05461"})
install_fakes(tab)
check("the tab still renders the uncorrected email before any reload",
      tab.rendered["primary_email"] == "spm@smingredients.com")

# ---------------------------------------------------------------------------
print("\nThe fix: fetch_corrected_record reloads, then reads the saved values")
# ---------------------------------------------------------------------------
got = main.fetch_corrected_record(tab, "MST-03682", result={"changes": CHANGES},
                                  applied=APPLIED, cleared=[])
check("page was reloaded exactly once", tab.reloads == 1, tab.reloads)
check("snapshot has the corrected email", got["fields"]["primary_email"] == "info@vtsmokeandcure.com", got)
check("snapshot has the corrected zip", got["fields"]["zip"] == "05461")
check("phone saved as +18024824666 matches page 8024824666", not got["not_landed"], got["not_landed"])

# ---------------------------------------------------------------------------
print("\nA correction that did not land is reported")
# ---------------------------------------------------------------------------
tab = FakeReviewTab(ORIGINAL)
tab.server.update({"primary_email": "info@vtsmokeandcure.com", "zip": "05461"})   # phone never saved
install_fakes(tab)
got = main.fetch_corrected_record(tab, "MST-03682", result={"changes": CHANGES},
                                  applied=APPLIED, cleared=[])
check("phone flagged as not landed", [f for f, _e, _p in got["not_landed"]] == ["primary_phone"], got["not_landed"])

tab = FakeReviewTab(dict(ORIGINAL, linkedin_url="https://linkedin.com/company/other"))
install_fakes(tab)
got = main.fetch_corrected_record(tab, "MST-03682", result={"changes": []}, applied=[],
                                  cleared=[("linkedin_url", "belongs to another company")])
check("a clear that did not land is reported", got["not_landed"] and got["not_landed"][0][0] == "linkedin_url")

check("supplier_type order/separator differences are not false alarms",
      not main.find_unlanded_changes({"supplier_type": "Private Label Manufacturer, Co-Packer"},
                                     [{"field": "supplier_type", "new_value": "Co-Packer | Private Label Manufacturer"}],
                                     ["supplier_type"]))

# ---------------------------------------------------------------------------
print("\nSafety: a reload that serves a different card stops everything")
# ---------------------------------------------------------------------------
tab = FakeReviewTab(ORIGINAL, serve_after_reload=("U-2", "MST-99999", {"company_name": "Other Co"}))
install_fakes(tab)
try:
    main.fetch_corrected_record(tab, "MST-03682", result={"changes": CHANGES}, applied=APPLIED)
    check("drift raises ReloadDriftError", False, "no exception")
except main.ReloadDriftError:
    check("drift raises ReloadDriftError", True)

# ---------------------------------------------------------------------------
print("\naccepted_companies.xlsx: written before the verdict, confirmed or rolled back after")
# ---------------------------------------------------------------------------
tmp = tempfile.mkdtemp()
snap.ACCEPTED_SNAPSHOT_FILE = os.path.join(tmp, "accepted.xlsx")
snap.ACCEPTED_SNAPSHOT_PENDING_FILE = os.path.join(tmp, "pending.jsonl")
snap.ENABLE_ACCEPTED_SNAPSHOT = True


def read_rows():
    from openpyxl import load_workbook
    wb = load_workbook(snap.ACCEPTED_SNAPSHOT_FILE)
    _h, rows = snap._read_sheet(wb[snap.ACCEPTED_SNAPSHOT_SHEET])
    return rows


record = {"record_id": "MST-03682", "fields": dict(ORIGINAL)}
tab = FakeReviewTab(ORIGINAL)
tab.server.update({"primary_email": "info@vtsmokeandcure.com", "primary_phone": "8024824666", "zip": "05461"})
install_fakes(tab)
corrected = main.fetch_corrected_record(tab, "MST-03682", result={"changes": CHANGES}, applied=APPLIED)
key = snap.record_pending(record, corrected, {"changes": CHANGES}, applied=APPLIED)
rows = read_rows()
check("row is in the file BEFORE the verdict", len(rows) == 1)
check("...with the corrected (reloaded) email", rows[0]["primary_email"] == "info@vtsmokeandcure.com", rows[0])
check("...marked pending", str(rows[0]["verdict_status"]).startswith("pending"))
check("edit_status is no longer 'no changes needed'", rows[0]["edit_status"] == "complete", rows[0]["edit_status"])
check("snapshot_source says the page was reloaded", "reloaded" in rows[0]["snapshot_source"])

snap.confirm_pending(key, confirmed_by="agent (auto mode)")
rows = read_rows()
check("confirm marks it accepted", rows[0]["verdict_status"] == "accepted" and rows[0]["confirmed_by"] == "agent (auto mode)", rows[0])

# A second company that is then NOT accepted disappears again.
rec2 = {"record_id": "MST-12914", "fields": {"company_name": "Chomps", "city": "Chicago"}}
key2 = snap.record_pending(rec2, {"fields": {"company_name": "Chomps", "city": "Naples"}, "record_id": "MST-12914"}, {})
check("second pending row added", len(read_rows()) == 2)
snap.rollback_pending(key2, "reviewer rejected")
rows = read_rows()
check("rollback removes a new company's pending row", len(rows) == 1 and rows[0]["record_id"] == "MST-03682")

# Re-accepting an existing company, then failing: the previous accepted row comes back.
key3 = snap.record_pending(record, {"fields": dict(corrected["fields"], zip="99999"), "record_id": "MST-03682"}, {})
check("pending overwrite visible", read_rows()[0]["zip"] == "99999")
snap.rollback_pending(key3, "Platform ready did not land")
rows = read_rows()
check("rollback restores the earlier accepted row", rows[0]["zip"] == "05461" and rows[0]["verdict_status"] == "accepted", rows[0])

# ---------------------------------------------------------------------------
print("\nHistory workbook uses the reloaded values too")
# ---------------------------------------------------------------------------
row = history.build_row(record, {"decision": "ACCEPT", "changes": []}, outcome="x", mode="auto",
                        backend="manual", corrected=corrected)
check("history row shows the corrected zip", row.get("zip") == "05461", row.get("zip"))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("All snapshot tests passed.")
