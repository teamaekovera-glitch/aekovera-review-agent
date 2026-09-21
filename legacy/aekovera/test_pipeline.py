"""Tests for the HTTP transport and the v4 decision gate.

Run with:  python test_pipeline.py

No network, no browser, no database: the QA app's behaviour is reproduced
from the handlers in qa_app/app.py and qa_app/db.py, so the tests pin the
contract the agent depends on.
"""

import sys
import types
from urllib.parse import urlparse, parse_qs

import decision_v4
import qa_http
from qa_http import (QAClient, QAHttpError, QASessionExpired, read_card_identity,
                     DECISION_TO_VERDICT)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# A fake QA app that enforces the same rules as db.save_verdict.
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, location=""):
        self.status = status
        self.headers = {"location": location} if location else {}


class FakeQAApp:
    """Reimplements the parts of the real app the agent relies on."""

    def __init__(self):
        # unit_id -> record
        self.cards = {
            "101": {"nonce": "n-101", "status": "pending", "verdict": None},
            "102": {"nonce": "n-102", "status": "pending", "verdict": None},
        }
        self.edits = []
        self.calls = []
        self.fail_next = 0
        self.session_valid = True

    def post(self, url, form=None, max_redirects=None, timeout=None):
        path = urlparse(url).path
        self.calls.append((path, dict(form or {})))

        # The agent must never follow redirects: doing so would GET /review,
        # which leases a card.
        assert max_redirects == 0, "request followed redirects (would lease a card)"

        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("connection reset by peer")

        if not self.session_valid:
            return FakeResponse(303, "/login?message=Please+sign+in")

        if path == "/verdict":
            return self._verdict(form)
        if path == "/edit":
            return self._edit(form)
        if path == "/skip":
            return self._skip(form)
        return FakeResponse(404)

    def _verdict(self, form):
        unit_id, nonce = form.get("unit_id"), form.get("nonce")
        card = self.cards.get(unit_id)
        if not card:
            return FakeResponse(303, "/review?message=Supplier+not+found.")
        if card["verdict"] or card["status"] == "done":
            return FakeResponse(303, "/review?message=Already+recorded.")
        if card["nonce"] != nonce:
            return FakeResponse(
                303, "/review?message=That+review+lease+expired%3B+another+card+is+ready.")
        card["verdict"] = form.get("verdict")
        card["status"] = "done"
        card["nonce"] = None
        return FakeResponse(303, "/review?message=Saved+%E2%80%94+next+supplier+ready.")

    def _edit(self, form):
        self.edits.append((form.get("unit_id"), form.get("field"), form.get("new_value")))
        return FakeResponse(303, "/review?message=Correction+saved")

    def _skip(self, form):
        card = self.cards.get(form.get("unit_id"))
        if not card or card["nonce"] != form.get("nonce"):
            return FakeResponse(303, "/review?message=Lease+lost")
        return FakeResponse(303, "/review?message=Skipped")


def make_client(app, **kw):
    context = types.SimpleNamespace(request=app)
    kw.setdefault("backoff_s", 0.0)
    kw.setdefault("log", lambda *a, **k: None)
    return QAClient(context, base_url="https://qa.example", **kw)


# ---------------------------------------------------------------------------
print("\nVerdict binding — the double-accept bug")
# ---------------------------------------------------------------------------

app = FakeQAApp()
client = make_client(app)

outcome = client.submit_verdict("101", "n-101", "ACCEPT")
check("a valid verdict saves", outcome == "saved", outcome)
check("it marked the right company", app.cards["101"]["verdict"] == "green")
check("the next company was untouched", app.cards["102"]["verdict"] is None)

# The exact scenario described: the action fires a second time.
repeat = client.submit_verdict("101", "n-101", "ACCEPT")
check("replaying the same verdict is a no-op", repeat == "already", repeat)
check("the replay did not touch the next company",
      app.cards["102"]["verdict"] is None)

# A stale nonce (what a keyboard press against a re-rendered page amounts to).
stale = client.submit_verdict("102", "n-101", "ACCEPT")
check("a mismatched nonce is refused", stale == "lease_lost", stale)
check("company 102 stayed pending", app.cards["102"]["status"] == "pending")

try:
    client.submit_verdict("102", "", "ACCEPT")
    check("a missing nonce is refused", False, "no error raised")
except QAHttpError:
    check("a missing nonce is refused", True)

try:
    client.submit_verdict("", "n-102", "ACCEPT")
    check("a missing unit_id is refused", False, "no error raised")
except QAHttpError:
    check("a missing unit_id is refused", True)

try:
    client.submit_verdict("102", "n-102", "MANUAL_REVIEW")
    check("MANUAL_REVIEW is not a verdict", False, "no error raised")
except QAHttpError:
    check("MANUAL_REVIEW is not a verdict", True)

check("all four buttons are mapped",
      DECISION_TO_VERDICT == {"ACCEPT": "green", "PARK": "orange",
                              "RE_ENRICH": "yellow", "REJECT": "red"})

# ---------------------------------------------------------------------------
print("\nA verdict is never retried")
# ---------------------------------------------------------------------------

app = FakeQAApp()
client = make_client(app, max_retries=3)
app.fail_next = 1
before = len(app.calls)
try:
    client.submit_verdict("101", "n-101", "ACCEPT")
    check("an ambiguous verdict failure raises", False, "no error raised")
except QAHttpError as exc:
    check("an ambiguous verdict failure raises", True)
    check("the error tells the operator to check", "check this company" in str(exc).lower(),
          str(exc))
check("the verdict was attempted exactly once", len(app.calls) - before == 1,
      f"{len(app.calls) - before} attempts")

# ---------------------------------------------------------------------------
print("\nEdits: retried, verified, never silently empty")
# ---------------------------------------------------------------------------

app = FakeQAApp()
client = make_client(app, max_retries=3)
ok, msg = client.apply_edit("101", "products", "rice flours, breadcrumbs",
                            "https://example.com/products")
check("an edit saves", ok, msg)
check("it was sent to the right unit", app.edits[-1][0] == "101")

app.fail_next = 2
ok, msg = client.apply_edit("101", "specialty", "rice-based ingredients",
                            "https://example.com")
check("an edit recovers from transient failures", ok, msg)
check("retries were counted", client.stats["retries"] == 2, client.stats["retries"])

ok, msg = client.apply_edit("101", "unit_id", "999")
check("a non-editable field is refused", not ok, msg)

ok, msg = client.apply_edit("101", "products", "")
check("an empty value is refused", not ok, msg)

# ---------------------------------------------------------------------------
print("\nSession expiry is not mistaken for a normal failure")
# ---------------------------------------------------------------------------

app = FakeQAApp()
client = make_client(app)
app.session_valid = False
try:
    client.apply_edit("101", "products", "x", "https://example.com")
    check("a login redirect raises QASessionExpired", False, "no error raised")
except QASessionExpired:
    check("a login redirect raises QASessionExpired", True)

# ---------------------------------------------------------------------------
print("\nReading the card identity from the page")
# ---------------------------------------------------------------------------

class FakeLocator:
    def __init__(self, value):
        self._value = value
        self.first = self
    def count(self):
        return 1 if self._value is not None else 0
    def get_attribute(self, _name):
        return self._value


class FakePage:
    def __init__(self, values):
        self._values = values
    def locator(self, selector):
        name = selector.split("'")[1]
        return FakeLocator(self._values.get(name))


unit_id, nonce = read_card_identity(FakePage({"unit_id": "101", "nonce": "n-101"}))
check("unit_id and nonce are read", (unit_id, nonce) == ("101", "n-101"))

try:
    read_card_identity(FakePage({"unit_id": "101"}))
    check("a missing nonce on the page raises", False, "no error raised")
except QAHttpError:
    check("a missing nonce on the page raises", True)

# ---------------------------------------------------------------------------
print("\nv4 decision gate")
# ---------------------------------------------------------------------------

def base(**over):
    result = {
        "company_name": "Pacific Rice Ingredients",
        "decision": "ACCEPT", "bucket": None, "scope_match": True,
        "site_identity": "confirmed", "supplier_type": "Food Manufacturer / Brand",
        "type_quote": None, "is_us_based": True, "supply_country": "United States",
        "confidence": 0.94, "reason": "Homepage lists rice flours and breadcrumbs.",
        "changes": [], "needs": [],
    }
    result.update(over)
    return result


j = decision_v4.validate(base())
check("a clean ACCEPT survives", j.decision == "ACCEPT", j.describe())

j = decision_v4.validate(base(scope_match=False))
check("scope_match false forces REJECT", j.decision == "REJECT", j.describe())

j = decision_v4.validate(base(scope_match="probably?"))
check("an unreadable scope_match is held", j.decision == "MANUAL_REVIEW", j.describe())

j = decision_v4.validate(base(supplier_type="Equipment / Services"))
check("equipment can never ACCEPT", j.decision == "REJECT", j.describe())

j = decision_v4.validate(base(supplier_type="Co-Packer", type_quote=None))
check("a contract type without its quote is held",
      j.decision == "MANUAL_REVIEW", j.describe())

j = decision_v4.validate(base(supplier_type="Co-Packer",
                              type_quote="we co-pack for brands nationwide"))
check("a contract type with its quote passes", j.decision == "ACCEPT", j.describe())

j = decision_v4.validate(base(site_identity="unverifiable"))
check("unverified identity parks", j.decision == "PARK", j.describe())
check("  with a bucket", j.bucket == "unverified_identity", j.bucket)

j = decision_v4.validate(base(supplier_type="Co-Packer", type_quote="we co-pack",
                              is_us_based=False))
check("a foreign contract manufacturer parks", j.decision == "PARK", j.describe())
check("  as foreign_brand", j.bucket == "foreign_brand", j.bucket)

j = decision_v4.validate(base(supplier_type="Packaging Supplier", is_us_based=False,
                              supply_country="Germany"))
check("a foreign packaging supplier still ACCEPTs", j.decision == "ACCEPT", j.describe())

j = decision_v4.validate(base(decision="PARK", bucket=None))
check("PARK without a bucket gets one", j.bucket == "other", j.bucket)

j = decision_v4.validate(base(), allow_accept=False)
check("approval mode holds every ACCEPT", j.decision == "MANUAL_REVIEW", j.describe())
check("  and drops its changes", j.changes == [])

j = decision_v4.validate(base(decision="WHATEVER"))
check("an unknown decision is held", j.decision == "MANUAL_REVIEW", j.describe())

# -- changes -----------------------------------------------------------------

good = {"field": "products", "new_value": "rice flours", "clear": False,
        "source_url": "https://example.com/p", "quote": "rice flours"}
j = decision_v4.validate(base(changes=[good]))
check("an evidenced change is kept", len(j.changes) == 1, j.changes)

j = decision_v4.validate(base(changes=[dict(good, source_url="")]))
check("a change with no source_url is dropped", j.changes == [], j.changes)

j = decision_v4.validate(base(changes=[dict(good, quote="")]))
check("a change with no quote is dropped", j.changes == [], j.changes)

j = decision_v4.validate(base(changes=[dict(good, new_value=None)]))
check("new_value null means do not touch", j.changes == [], j.changes)

j = decision_v4.validate(base(decision="REJECT", scope_match=False, changes=[good]))
check("REJECT drops all changes", j.changes == [], j.changes)

j = decision_v4.validate(base(decision="PARK", bucket="conflated", changes=[good]))
check("a conflated PARK drops all changes", j.changes == [], j.changes)

clear = {"field": "linkedin_url", "clear": True,
         "clear_reason": "the page of a trucking company"}
j = decision_v4.validate(base(decision="PARK", bucket="thin_data", changes=[clear]))
check("a clear with a reason is kept", len(j.changes) == 1, j.changes)

j = decision_v4.validate(base(decision="PARK", bucket="thin_data",
                              changes=[{"field": "linkedin_url", "clear": True}]))
check("a clear without a reason is dropped", j.changes == [], j.changes)

j = decision_v4.validate(base(
    decision="RE_ENRICH",
    changes=[{"field": "website_url", "new_value": "https://real.example",
              "source_url": "https://search", "quote": "real.example"}]))
check("RE_ENRICH surfaces the suggested URL",
      j.suggested_url == "https://real.example", j.suggested_url)

# -- prompt ------------------------------------------------------------------
prompt = decision_v4.load_prompt()
check("the v4 prompt loads", "Aekovera record judge, v4" in prompt)
check("  and lists all four decisions",
      all(d in prompt for d in ("ACCEPT", "PARK", "RE_ENRICH", "REJECT")))

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
print("\nv13.2: clear / type / skip over HTTP")
# ---------------------------------------------------------------------------

app = FakeQAApp()
client = make_client(app)

ok, msg = client.clear_field("101", "linkedin_url", "trucking company page")
check("a clear posts an empty value", ok and app.edits[-1] == ("101", "linkedin_url", ""), msg)

ok, msg = client.clear_field("101", "company_name", "x")
check("company_name cannot be cleared", not ok, msg)

ok, msg = client.apply_edit("101", "supplier_type", "Co-Packer | Ingredient Supplier",
                            "https://example.com")
check("supplier_type posts the pipe-joined string",
      ok and app.edits[-1][2] == "Co-Packer | Ingredient Supplier", msg)

check("a skip is bound to unit_id+nonce", client.skip_record("102", "n-102") is True)
check("a skip with a stale nonce is refused", client.skip_record("102", "n-999") is False)
check("skip never touched a verdict", app.cards["102"]["verdict"] is None)

app.fail_next = 1
try:
    client.skip_record("101", "n-101")
    check("a skip is never retried", False, "no error raised")
except Exception:
    check("a skip is never retried", True)

# ---------------------------------------------------------------------------
print("\nv13.3: a non-URL source_url must not kill the edit")
# ---------------------------------------------------------------------------
app = FakeQAApp()
client = make_client(app)
ok, msg = client.apply_edit("101", "city", "Pointe-Claire", "maps card")
check("edit saves when source is not a URL", ok, msg)
check("  and the bad source was not sent", "source_url" not in app.calls[-1][1])
ok, msg = client.apply_edit("101", "state", "Quebec", "https://empwr.example/contact")
check("a real source_url is still sent",
      app.calls[-1][1].get("source_url") == "https://empwr.example/contact")
ok, msg = client.apply_edit("101", "zip", "H9R 1A1", "empwr.example/contact")
check("a scheme-less URL is treated as non-URL", "source_url" not in app.calls[-1][1])
check("drops are counted", client.stats["source_url_dropped"] == 2)

# ---------------------------------------------------------------------------
print("\nv13.4: manual ChatGPT backend parked almost everything")
# ---------------------------------------------------------------------------
browsing = decision_v4.load_prompt(browsing=True)
api_prompt = decision_v4.load_prompt(browsing=False)
check("API prompt still says the model has no browsing",
      "You have NO live browsing" in api_prompt)
check("browsing prompt no longer says the model has no browsing",
      "NO live browsing" not in browsing)
check("  and tells it to search and open the site itself",
      "HAVE live web browsing" in browsing and "Open the on-file website_url" in browsing)
check("  and keeps every other section",
      all(h in browsing for h in ("## 2. Decisions", "## 5. Identity", "## 10. Output",
                                   "## 11. Worked examples")))
check("  and section 1 appears exactly once",
      browsing.count("## 1. ") == 1)

good_accept = dict(decision="ACCEPT", scope_match=True, supplier_type="Co-Packer",
                   type_quote="we co-pack for brands", is_us_based=True, changes=[])
for phrasing in ("Confirmed", "confirmed - name and address on contact page",
                 "Confirmed: phone matches", "verified on site"):
    j = decision_v4.validate(dict(good_accept, site_identity=phrasing))
    check(f"site_identity {phrasing!r} keeps ACCEPT", j.decision == "ACCEPT", j.describe())
for phrasing in ("unverifiable", "not confirmed", "unconfirmed", "failed - different company"):
    j = decision_v4.validate(dict(good_accept, site_identity=phrasing))
    check(f"site_identity {phrasing!r} still parks", j.decision == "PARK", j.describe())

j = decision_v4.validate(dict(good_accept, site_identity="confirmed",
                              decision="PARK", bucket="thin_data",
                              needs=["fetch_page https://x.example"]))
check("a PARK with unfulfilled needs is flagged as provisional",
      any("did not browse" in w for w in j.warnings), j.warnings)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("All tests passed (including the v13.1 regression).")
