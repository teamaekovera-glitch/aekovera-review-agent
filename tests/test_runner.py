"""Invariant tests for the BatchRunner state machine and correction guards.

New coverage required by the engine-port task:
- the four Auto Mode accept holds (pure evaluation + runner-level wiring),
- JSON-leak cleaning guards,
- discovery-failure skip that is loud, not silent,
- the repeat guard (never re-research a decided record),
- REJECT fast path and manual-review routing.
"""


from review_hub.engine import corrections
from review_hub.engine.discovery import FieldDiscoveryError
from review_hub.engine.research import LLMError
from review_hub.engine.runner import BatchRunner, RunnerState

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class ListSink:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def outcomes(self):
        return [r.outcome for r in self.records]


class FakeOps:
    """Duck-typed PageOps serving a scripted sequence of records."""

    def __init__(self, serve=(), website_verdict=(True, "ok")):
        self._serve = list(serve)
        self.current = None
        self.website_verdict = website_verdict
        self.skip_reasons = []
        self.applied_changes = []
        self.cleared_fields = []
        self.flagged = []
        self.final_actions = []
        self.reload_and_verify_calls = 0

    # -- reading ----------------------------------------------------------
    def page_closed(self, page):
        return False

    def extract_record(self, page):
        if self._serve:
            self.current = self._serve.pop(0)
        return self.current

    def read_record_id(self, page):
        return self.current["record_id"] if self.current else ""

    # -- navigation / skipping -------------------------------------------
    def skip_record(self, page, reason):
        self.skip_reasons.append(reason)

    def wait_settle(self, page):
        return None

    # -- field ops ---------------------------------------------------------
    def field_present(self, page, field):
        return True

    def apply_change(self, page, change, newly_created=False):
        self.applied_changes.append(change.get("field"))
        return True

    def clear_field(self, page, field, previous_value):
        self.cleared_fields.append(field)

    def apply_type_fill(self, page, entry, desired):
        return list(desired)

    def verify_website(self, page, company_name, url):
        return self.website_verdict

    def reload_and_verify(self, page, record, result, expected_id, *, applied, cleared):
        self.reload_and_verify_calls += 1
        corrected = {"fields": dict(record["fields"]), "record_id": expected_id}
        return corrected, []

    def flag_manual_review(self, page, result):
        self.flagged.append(result.get("company_name"))

    def final_action(self, page, result, judgement):
        self.final_actions.append(result.get("decision"))
        return "saved"


class FakeBackend:
    """Counts research calls; serves scripted results, then the last one."""

    def __init__(self, results=(), error=None):
        self._results = list(results)
        self._error = error
        self._last = None
        self.calls = 0

    def research(self, prompt, system):
        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._results:
            self._last = self._results.pop(0)
            return self._last
        if self._last is None:
            raise RuntimeError("FakeBackend: no scripted result to serve")
        return self._last


def accept_result(**over):
    result = {
        "company_name": "Acme Rice",
        "decision": "ACCEPT",
        "bucket": None,
        "scope_match": True,
        "site_identity": "confirmed",
        "supplier_type": "Food Manufacturer / Brand",
        "type_quote": None,
        "is_us_based": True,
        "supply_country": "United States",
        "confidence": 0.95,
        "reason": "clean accept",
        "changes": [],
        "needs": [],
    }
    result.update(over)
    return result


def make_record(record_id="MST-1", name=None):
    # Names default from the id: the repeat guard matches id OR company name
    # (legacy semantics), so two distinct records sharing a default name
    # would be indistinguishable from a re-served supplier.
    return {
        "record_id": record_id,
        "fields": {"company_name": name or f"Supplier {record_id}", "website_url": ""},
        "missing_fields": [],
    }


def make_runner(ops, backend, **kw):
    kw.setdefault("log", lambda *a, **k: None)
    # The real CLI injects the prompting builder; these tests exercise the
    # state machine, not prompt formatting.
    kw.setdefault("build_prompt", lambda record: "PROMPT")
    return BatchRunner(ops=ops, backend=backend, sink=ListSink(), **kw)


# ---------------------------------------------------------------------------
# The four Auto Mode accept holds - pure evaluation, pinned exactly
# ---------------------------------------------------------------------------

class TestFourAcceptHolds:
    def test_no_flags_means_no_hold(self):
        reasons = corrections.evaluate_accept_holds(
            needs_clear=[],
            needs_review=[],
            identity_renamed=[],
            snapshot_problems=[],
            corrected_is_none=False,
        )
        assert reasons == []

    def test_hold_1_unresolved_needs_clear(self):
        reasons = corrections.evaluate_accept_holds(
            needs_clear=[("linkedin_url", "belongs to a trucking company")],
            needs_review=[],
            identity_renamed=[],
            snapshot_problems=[],
            corrected_is_none=False,
        )
        assert reasons == ["1 field(s) left uncleared"]

    def test_hold_1_unresolved_needs_review(self):
        reasons = corrections.evaluate_accept_holds(
            needs_clear=[],
            needs_review=[("website_url", "https://x", "no match")],
            identity_renamed=[],
            snapshot_problems=[],
            corrected_is_none=False,
        )
        assert reasons == ["1 field(s) flagged for review"]

    def test_hold_2_identity_rename(self):
        reasons = corrections.evaluate_accept_holds(
            needs_clear=[],
            needs_review=[],
            identity_renamed=[("company_name", "Acme Rice", "Acme Rice LLC")],
            snapshot_problems=[],
            corrected_is_none=False,
        )
        assert reasons == ["1 company-identity field(s) renamed"]

    def test_hold_3_edits_did_not_land_after_reload(self):
        problems = ["primary_phone: saved '+18024824666', page shows '714-348-7685'"]
        reasons = corrections.evaluate_accept_holds(
            needs_clear=[],
            needs_review=[],
            identity_renamed=[],
            snapshot_problems=problems,
            corrected_is_none=False,
        )
        assert reasons == problems

    def test_hold_4_final_snapshot_failed(self):
        problems = ["final snapshot failed: reload served a different card"]
        reasons = corrections.evaluate_accept_holds(
            needs_clear=[],
            needs_review=[],
            identity_renamed=[],
            snapshot_problems=problems,
            corrected_is_none=True,
        )
        assert reasons == problems

    def test_holds_combine(self):
        reasons = corrections.evaluate_accept_holds(
            needs_clear=[("a", "r")],
            needs_review=[("b", "v", "d")],
            identity_renamed=[("company_name", "A", "B")],
            snapshot_problems=["snapshot failed"],
            corrected_is_none=True,
        )
        assert len(reasons) == 4


# ---------------------------------------------------------------------------
# Runner wiring of the holds: a held ACCEPT skips and never submits
# ---------------------------------------------------------------------------

def test_accept_with_flagged_website_is_held_not_submitted():
    ops = FakeOps(serve=[make_record()], website_verdict=(False, "homepage says something else"))
    backend = FakeBackend(
        results=[
            accept_result(
                changes=[{
                    "field": "website_url",
                    "new_value": "https://wrong.example",
                    "source_url": "https://search",
                    "quote": "wrong.example",
                }]
            )
        ]
    )
    runner = make_runner(ops, backend)
    summary = runner.run(page=None, run_count=1)

    assert summary["final_state"] == RunnerState.RUN_DONE.value
    assert summary["processed"] == 1
    assert ops.final_actions == []  # no verdict was submitted
    assert ops.skip_reasons and "auto mode hold" in ops.skip_reasons[0]
    assert any(r.to_state == RunnerState.HELD.value for r in runner.sink.records)
    # A held record is NOT accepted: the research result is remembered for
    # the repeat guard instead of burning another round-trip.
    assert backend.calls == 1


# ---------------------------------------------------------------------------
# JSON-leak cleaning guards
# ---------------------------------------------------------------------------

class TestJsonLeakCleaning:
    def test_leak_markers_are_detected(self):
        leaked = 'MN, [United States](https://site.com%22}, {%22field%22:%22city%22'
        assert corrections.looks_like_json_leak(leaked)
        assert corrections.looks_like_json_leak("clean value") is False

    def test_markdown_link_in_longer_value_is_stripped(self):
        # The Harris Honey case: a citation link spliced INTO the middle of
        # a country/city value, its target corrupted into changes-JSON.
        dirty = 'Madelia [MN](https://x.example%22},{%22field%22:%22city%22,%22new_value%22:%22Madelia%22}, ...)'
        assert corrections.clean_corrected_value("city", dirty) == "Madelia MN"

    def test_full_markdown_links_reduce_to_their_visible_text(self):
        assert (
            corrections.clean_corrected_value("website_url", "[https://example.com](https://example.com)")
            == "https://example.com"
        )

    def test_email_uses_the_mailto_target(self):
        assert (
            corrections.clean_corrected_value("primary_email", "[a@b.example](mailto:a@b.example)")
            == "a@b.example"
        )
        assert corrections.clean_corrected_value("primary_email", "mailto:a@b.example") == "a@b.example"

    def test_phone_is_normalized_for_the_ui(self):
        assert corrections.clean_corrected_value("primary_phone", "tel:+1 (207) 373-4513") == "+12073734513"
        assert corrections.clean_corrected_value("primary_phone", "(207) 373-4513") == "2073734513"

    def test_plain_values_pass_through(self):
        assert corrections.clean_corrected_value("city", "Madelia") == "Madelia"
        assert corrections.clean_corrected_value("city", None) is None


# ---------------------------------------------------------------------------
# Discovery failure: skip loudly, never research a page that was not read
# ---------------------------------------------------------------------------

class ExplodingDiscoveryOps(FakeOps):
    def __init__(self, fail_times, **kw):
        super().__init__(**kw)
        self._fail_times = fail_times
        self.reads = 0

    def extract_record(self, page):
        self.reads += 1
        if self.reads <= self._fail_times:
            raise FieldDiscoveryError("no card content found in the page DOM")
        return super().extract_record(page)


def test_discovery_failure_skips_loudly_then_recovers():
    ops = ExplodingDiscoveryOps(fail_times=1, serve=[make_record()])
    backend = FakeBackend(results=[accept_result()])
    runner = make_runner(ops, backend)
    summary = runner.run(page=None, run_count=1)

    assert summary["processed"] == 1
    assert ops.skip_reasons == ["field discovery failed"]  # loud, with a reason
    assert any(r.to_state == RunnerState.DISCOVERY_FAILED.value for r in runner.sink.records)
    assert any(r.outcome == "research ok: ACCEPT" for r in runner.sink.records)
    assert ops.final_actions == ["ACCEPT"]


def test_consecutive_discovery_failures_stop_the_run():
    ops = ExplodingDiscoveryOps(fail_times=99)
    backend = FakeBackend()
    runner = make_runner(ops, backend, max_consecutive_failures=2)
    summary = runner.run(page=None, run_count=5)

    assert summary["final_state"] == RunnerState.STOPPED.value
    assert summary["processed"] == 0
    assert backend.calls == 0  # never researched a page it could not read
    # The second consecutive failure stops the run WITHOUT a further skip.
    assert len(ops.skip_reasons) == 1


# ---------------------------------------------------------------------------
# Research failure: loud skip, then stop at the configured threshold
# ---------------------------------------------------------------------------

def test_research_failure_skips_loudly_then_recovers():
    # Three served cards, two unique records: the run budget counts unique
    # records, so run_count=2 finalizes MST-2 and MST-3 (MST-1 was skipped).
    ops = FakeOps(
        serve=[make_record("MST-1"), make_record("MST-2"), make_record("MST-3")]
    )
    # First call raises, second call succeeds with a clean ACCEPT.
    backend = FlakyBackend(error=LLMError("rate limited"), then=accept_result())
    runner = make_runner(ops, backend)
    summary = runner.run(page=None, run_count=2)

    assert summary["processed"] == 2
    assert ops.skip_reasons == ["research failure"]
    assert any(
        r.to_state == RunnerState.RESEARCH_FAILED.value and r.error == "rate limited"
        for r in runner.sink.records
    )
    assert ops.final_actions == ["ACCEPT", "ACCEPT"]
    assert backend.calls == 3  # one failed round-trip + two successful ones


def test_consecutive_research_failures_stop_the_run():
    ops = FakeOps(serve=[make_record("MST-1"), make_record("MST-2")])
    backend = AlwaysFailingBackend(LLMError("quota exhausted"))
    runner = make_runner(ops, backend, max_consecutive_failures=2)
    summary = runner.run(page=None, run_count=2)

    assert summary["final_state"] == RunnerState.STOPPED.value
    assert summary["processed"] == 0
    assert ops.final_actions == []


class FlakyBackend:
    """Raises `error` on the first call, then returns `then` forever."""

    def __init__(self, error, then=None):
        self._error = error
        self._then = then if then is not None else accept_result()
        self.calls = 0

    def research(self, prompt, system):
        self.calls += 1
        if self.calls == 1:
            raise self._error
        return self._then


class AlwaysFailingBackend:
    """Raises `error` on EVERY call - for consecutive-failure stop tests."""

    def __init__(self, error):
        self._error = error
        self.calls = 0

    def research(self, prompt, system):
        self.calls += 1
        raise self._error


# ---------------------------------------------------------------------------
# Repeat guard: never re-research a decided record served again
# ---------------------------------------------------------------------------

def test_repeat_guard_reuses_result_without_new_research():
    # Serve 1: MST-1 fresh. Serve 2: the SAME card again (a lost verdict -
    # the page did not advance). The repeat guard reuses the previous
    # result instead of another research round-trip, and the re-served card
    # does not consume the run budget. Serve 3: a new card, budget slot 2.
    record = make_record("MST-1", "Acme Rice")
    next_record = make_record("MST-2", "Beta Foods")
    ops = FakeOps(serve=[record, dict(record), next_record])
    backend = FakeBackend(results=[accept_result()])
    runner = make_runner(ops, backend)
    summary = runner.run(page=None, run_count=2)

    # A decided record is never re-researched within a run.
    assert summary["repeat_passes_total"] == 1
    repeat_transitions = [
        r for r in runner.sink.records if r.outcome == "repeat guard: reusing previous result"
    ]
    assert len(repeat_transitions) == 1
    assert backend.calls == 2  # exactly one round-trip was saved
    assert ops.final_actions == ["ACCEPT", "ACCEPT", "ACCEPT"]
    # Only fresh researches are tallied (legacy): recA and recB - two ACCEPTs.
    assert summary["decision_tally"] == {"ACCEPT": 2}


def test_repeat_guard_is_disabled_by_flag():
    record = make_record("MST-1", "Acme Rice")
    ops = FakeOps(serve=[record, dict(record)])
    backend = FakeBackend(results=[accept_result()])
    runner = make_runner(ops, backend, reuse_result_on_repeat=False)
    summary = runner.run(page=None, run_count=2)

    assert summary["repeat_passes_total"] == 0
    assert not [r for r in runner.sink.records if "repeat guard" in r.outcome]


# ---------------------------------------------------------------------------
# REJECT fast path and manual-review routing
# ---------------------------------------------------------------------------

def test_reject_fast_path_applies_no_changes():
    ops = FakeOps(serve=[make_record()])
    backend = FakeBackend(
        results=[
            accept_result(
                decision="REJECT",
                scope_match=False,
                changes=[{
                    "field": "products",
                    "new_value": "rice flours",
                    "source_url": "https://example.com",
                    "quote": "rice flours",
                }],
            )
        ]
    )
    runner = make_runner(ops, backend)
    summary = runner.run(page=None, run_count=1)

    assert summary["processed"] == 1
    assert ops.applied_changes == []  # no point editing a rejected record
    assert ops.final_actions == ["REJECT"]


def test_manual_review_flags_without_final_action():
    ops = FakeOps(serve=[make_record()])
    backend = FakeBackend(results=[accept_result(scope_match="probably?")])
    runner = make_runner(ops, backend)
    summary = runner.run(page=None, run_count=1)

    assert summary["processed"] == 1
    assert ops.flagged == ["Acme Rice"]
    assert ops.final_actions == []  # left undecided for a human
    assert any(r.to_state == RunnerState.MANUAL_REVIEW.value for r in runner.sink.records)


# ---------------------------------------------------------------------------
# Accept happy path: reload-before-snapshot order is preserved
# ---------------------------------------------------------------------------

def test_accept_happy_path_reloads_before_verdict():
    ops = FakeOps(serve=[make_record()])
    backend = FakeBackend(results=[accept_result()])
    runner = make_runner(ops, backend)
    summary = runner.run(page=None, run_count=1)

    assert summary["final_state"] == RunnerState.RUN_DONE.value
    assert summary["processed"] == 1
    assert ops.reload_and_verify_calls == 1  # reload happened before the verdict
    assert ops.final_actions == ["ACCEPT"]
    assert summary["decision_tally"] == {"ACCEPT": 1}
