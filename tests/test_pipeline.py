"""Tests for the HTTP transport and the v4 decision gate.

Pytest port of legacy/aekovera/test_pipeline.py - same cases, same
expectations; the self-rolled runner is gone. No network, no browser, no
database: the QA app's behaviour is reproduced from the handlers in
qa_app/app.py and qa_app/db.py, so the tests pin the contract the agent
depends on.
"""

import types
from urllib.parse import urlparse

import pytest

from review_hub.engine import decision
from review_hub.engine.transport import (
    DECISION_TO_VERDICT,
    QAClient,
    QAHttpError,
    QASessionExpired,
    read_card_identity,
)

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
                303, "/review?message=That+review+lease+expired%3B+another+card+is+ready."
            )
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
# Verdict binding - the double-accept bug
# ---------------------------------------------------------------------------

def test_verdict_binding_and_double_accept():
    app = FakeQAApp()
    client = make_client(app)

    outcome = client.submit_verdict("101", "n-101", "ACCEPT")
    assert outcome == "saved"
    assert app.cards["101"]["verdict"] == "green"
    assert app.cards["102"]["verdict"] is None

    # The exact scenario described: the action fires a second time.
    repeat = client.submit_verdict("101", "n-101", "ACCEPT")
    assert repeat == "already"
    assert app.cards["102"]["verdict"] is None

    # A stale nonce (what a keyboard press against a re-rendered page amounts to).
    stale = client.submit_verdict("102", "n-101", "ACCEPT")
    assert stale == "lease_lost"
    assert app.cards["102"]["status"] == "pending"


def test_verdict_refusals():
    app = FakeQAApp()
    client = make_client(app)

    with pytest.raises(QAHttpError):
        client.submit_verdict("102", "", "ACCEPT")  # missing nonce
    with pytest.raises(QAHttpError):
        client.submit_verdict("", "n-102", "ACCEPT")  # missing unit_id
    with pytest.raises(QAHttpError):
        client.submit_verdict("102", "n-102", "MANUAL_REVIEW")  # not a verdict

    assert DECISION_TO_VERDICT == {
        "ACCEPT": "green",
        "PARK": "orange",
        "RE_ENRICH": "yellow",
        "REJECT": "red",
    }
    assert app.cards["102"]["verdict"] is None


# ---------------------------------------------------------------------------
# A verdict is never retried
# ---------------------------------------------------------------------------

def test_verdict_is_never_retried():
    app = FakeQAApp()
    client = make_client(app, max_retries=3)
    app.fail_next = 1
    before = len(app.calls)

    with pytest.raises(QAHttpError) as excinfo:
        client.submit_verdict("101", "n-101", "ACCEPT")
    assert "check this company" in str(excinfo.value).lower()
    assert len(app.calls) - before == 1  # attempted exactly once


# ---------------------------------------------------------------------------
# Edits: retried, verified, never silently empty
# ---------------------------------------------------------------------------

def test_edits_are_retried_and_verified():
    app = FakeQAApp()
    client = make_client(app, max_retries=3)

    ok, msg = client.apply_edit(
        "101", "products", "rice flours, breadcrumbs", "https://example.com/products"
    )
    assert ok, msg
    assert app.edits[-1][0] == "101"

    app.fail_next = 2
    ok, msg = client.apply_edit("101", "specialty", "rice-based ingredients", "https://example.com")
    assert ok, msg
    assert client.stats["retries"] == 2

    ok, _msg = client.apply_edit("101", "unit_id", "999")
    assert not ok  # a non-editable field is refused

    ok, _msg = client.apply_edit("101", "products", "")
    assert not ok  # an empty value is refused


# ---------------------------------------------------------------------------
# Session expiry is not mistaken for a normal failure
# ---------------------------------------------------------------------------

def test_session_expiry_raises_qasessionexpired():
    app = FakeQAApp()
    client = make_client(app)
    app.session_valid = False
    with pytest.raises(QASessionExpired):
        client.apply_edit("101", "products", "x", "https://example.com")


# ---------------------------------------------------------------------------
# Reading the card identity from the page
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


def test_read_card_identity():
    unit_id, nonce = read_card_identity(FakePage({"unit_id": "101", "nonce": "n-101"}))
    assert (unit_id, nonce) == ("101", "n-101")

    with pytest.raises(QAHttpError):
        read_card_identity(FakePage({"unit_id": "101"}))  # missing nonce


# ---------------------------------------------------------------------------
# v4 decision gate
# ---------------------------------------------------------------------------

def base(**over):
    result = {
        "company_name": "Pacific Rice Ingredients",
        "decision": "ACCEPT",
        "bucket": None,
        "scope_match": True,
        "site_identity": "confirmed",
        "supplier_type": "Food Manufacturer / Brand",
        "type_quote": None,
        "is_us_based": True,
        "supply_country": "United States",
        "confidence": 0.94,
        "reason": "Homepage lists rice flours and breadcrumbs.",
        "changes": [],
        "needs": [],
    }
    result.update(over)
    return result


def test_gate_clean_accept_survives():
    j = decision.validate(base())
    assert j.decision == "ACCEPT"


def test_gate_scope_mismatch_forces_reject():
    j = decision.validate(base(scope_match=False))
    assert j.decision == "REJECT"


def test_gate_unreadable_scope_is_held():
    j = decision.validate(base(scope_match="probably?"))
    assert j.decision == "MANUAL_REVIEW"


def test_gate_equipment_can_never_accept():
    j = decision.validate(base(supplier_type="Equipment / Services"))
    assert j.decision == "REJECT"


def test_gate_contract_type_requires_its_quote():
    j = decision.validate(base(supplier_type="Co-Packer", type_quote=None))
    assert j.decision == "MANUAL_REVIEW"

    j = decision.validate(
        base(supplier_type="Co-Packer", type_quote="we co-pack for brands nationwide")
    )
    assert j.decision == "ACCEPT"


def test_gate_identity_and_foreign_routing():
    j = decision.validate(base(site_identity="unverifiable"))
    assert j.decision == "PARK"
    assert j.bucket == "unverified_identity"

    j = decision.validate(
        base(supplier_type="Co-Packer", type_quote="we co-pack", is_us_based=False)
    )
    assert j.decision == "PARK"
    assert j.bucket == "foreign_brand"

    j = decision.validate(
        base(supplier_type="Packaging Supplier", is_us_based=False, supply_country="Germany")
    )
    assert j.decision == "ACCEPT"


def test_gate_bucket_and_approval_mode():
    j = decision.validate(base(decision="PARK", bucket=None))
    assert j.bucket == "other"

    j = decision.validate(base(), allow_accept=False)
    assert j.decision == "MANUAL_REVIEW"  # approval mode holds every ACCEPT
    assert j.changes == []  # and drops its changes

    j = decision.validate(base(decision="WHATEVER"))
    assert j.decision == "MANUAL_REVIEW"


def test_gate_change_evidence_rules():
    good = {
        "field": "products",
        "new_value": "rice flours",
        "clear": False,
        "source_url": "https://example.com/p",
        "quote": "rice flours",
    }
    j = decision.validate(base(changes=[good]))
    assert len(j.changes) == 1

    j = decision.validate(base(changes=[dict(good, source_url="")]))
    assert j.changes == []  # no source_url -> dropped

    j = decision.validate(base(changes=[dict(good, quote="")]))
    assert j.changes == []  # no quote -> dropped

    j = decision.validate(base(changes=[dict(good, new_value=None)]))
    assert j.changes == []  # new_value null means do not touch

    j = decision.validate(base(decision="REJECT", scope_match=False, changes=[good]))
    assert j.changes == []  # REJECT drops all changes

    j = decision.validate(base(decision="PARK", bucket="conflated", changes=[good]))
    assert j.changes == []  # a conflated PARK drops all changes

    clear = {"field": "linkedin_url", "clear": True, "clear_reason": "the page of a trucking company"}
    j = decision.validate(base(decision="PARK", bucket="thin_data", changes=[clear]))
    assert len(j.changes) == 1  # a clear with a reason is kept

    j = decision.validate(
        base(
            decision="PARK",
            bucket="thin_data",
            changes=[{"field": "linkedin_url", "clear": True}],
        )
    )
    assert j.changes == []  # a clear without a reason is dropped

    j = decision.validate(
        base(
            decision="RE_ENRICH",
            changes=[{
                "field": "website_url",
                "new_value": "https://real.example",
                "source_url": "https://search",
                "quote": "real.example",
            }],
        )
    )
    assert j.suggested_url == "https://real.example"


def test_v4_prompt_loads():
    prompt = decision.load_prompt()
    assert "Aekovera record judge, v4" in prompt
    assert all(d in prompt for d in ("ACCEPT", "PARK", "RE_ENRICH", "REJECT"))


# ---------------------------------------------------------------------------
# v13.2: clear / type / skip over HTTP
# ---------------------------------------------------------------------------

def test_clear_type_skip_over_http():
    app = FakeQAApp()
    client = make_client(app)

    ok, msg = client.clear_field("101", "linkedin_url", "trucking company page")
    assert ok, msg
    assert app.edits[-1] == ("101", "linkedin_url", "")  # a clear posts an empty value

    ok, _msg = client.clear_field("101", "company_name", "x")
    assert not ok  # company_name cannot be cleared

    ok, msg = client.apply_edit(
        "101", "supplier_type", "Co-Packer | Ingredient Supplier", "https://example.com"
    )
    assert ok, msg
    assert app.edits[-1][2] == "Co-Packer | Ingredient Supplier"

    assert client.skip_record("102", "n-102") is True  # bound to unit_id+nonce
    assert client.skip_record("102", "n-999") is False  # stale nonce refused
    assert app.cards["102"]["verdict"] is None  # skip never touched a verdict

    app.fail_next = 1
    before = len(app.calls)
    with pytest.raises(Exception):
        client.skip_record("101", "n-101")
    assert len(app.calls) - before == 1  # a skip is never retried


# ---------------------------------------------------------------------------
# v13.3: a non-URL source_url must not kill the edit
# ---------------------------------------------------------------------------

def test_non_url_source_url_is_dropped_not_fatal():
    app = FakeQAApp()
    client = make_client(app)

    ok, msg = client.apply_edit("101", "city", "Pointe-Claire", "maps card")
    assert ok, msg
    assert "source_url" not in app.calls[-1][1]  # the bad source was not sent

    ok, msg = client.apply_edit("101", "state", "Quebec", "https://empwr.example/contact")
    assert ok, msg
    assert app.calls[-1][1].get("source_url") == "https://empwr.example/contact"

    ok, msg = client.apply_edit("101", "zip", "H9R 1A1", "empwr.example/contact")
    assert ok, msg
    assert "source_url" not in app.calls[-1][1]  # scheme-less treated as non-URL
    assert client.stats["source_url_dropped"] == 2


# ---------------------------------------------------------------------------
# v13.4: manual ChatGPT backend parked almost everything
# ---------------------------------------------------------------------------

def test_browsing_prompt_substitution():
    browsing = decision.load_prompt(browsing=True)
    api_prompt = decision.load_prompt(browsing=False)

    assert "You have NO live browsing" in api_prompt
    assert "NO live browsing" not in browsing
    assert "HAVE live web browsing" in browsing
    assert "Open the on-file website_url" in browsing
    assert all(
        h in browsing
        for h in ("## 2. Decisions", "## 5. Identity", "## 10. Output", "## 11. Worked examples")
    )
    assert browsing.count("## 1. ") == 1


def test_site_identity_phrasings():
    good_accept = dict(
        decision="ACCEPT",
        scope_match=True,
        supplier_type="Co-Packer",
        type_quote="we co-pack for brands",
        is_us_based=True,
        changes=[],
    )
    for phrasing in (
        "Confirmed",
        "confirmed - name and address on contact page",
        "Confirmed: phone matches",
        "verified on site",
    ):
        j = decision.validate(dict(good_accept, site_identity=phrasing))
        assert j.decision == "ACCEPT", phrasing

    for phrasing in ("unverifiable", "not confirmed", "unconfirmed", "failed - different company"):
        j = decision.validate(dict(good_accept, site_identity=phrasing))
        assert j.decision == "PARK", phrasing


def test_park_with_unfulfilled_needs_is_provisional():
    good_accept = dict(
        decision="ACCEPT",
        scope_match=True,
        supplier_type="Co-Packer",
        type_quote="we co-pack for brands",
        is_us_based=True,
        changes=[],
    )
    j = decision.validate(
        dict(
            good_accept,
            site_identity="confirmed",
            decision="PARK",
            bucket="thin_data",
            needs=["fetch_page https://x.example"],
        )
    )
    assert any("did not browse" in w for w in j.warnings)
