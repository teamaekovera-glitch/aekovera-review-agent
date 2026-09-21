"""BatchRunner - main()'s loop as a callable, injectable state machine.

The legacy v34 loop lived in a 700-line ``main()`` with module globals.
Here the same pipeline is a state machine whose collaborators (page
operations, research backend, transition sink) are injected, so every
invariant can be driven by tests without a browser.

Pipeline states per record (identical decision order to legacy):

    IDLE -> READING -FieldDiscoveryError-> DISCOVERY_FAILED (loud skip)
         |            \\-- stop after MAX_CONSECUTIVE_FAILURES
         +-> RESEARCHING (repeat guard: reuse the last result when the
         |    same supplier is served again; never re-research a decided
         |    record within a run)
         +-> RESEARCH_FAILED (skip loudly, or stop when configured)
         +-> VALIDATING (scope/gate normalization)
         +-> MANUAL_REVIEW  -> flagged, nothing finalized -> next record
         +-> REJECT fast path (no field changes) -> final action
         +-> APPLYING -> VERIFYING -> SNAPSHOT (accept only)
         +-> HOLDS_CHECK -> HELD (skip, no verdict) | DECIDING
         +-> DECIDING (the one POST /verdict - never retried) -> next

Every arrow is emitted to the sink as a TransitionRecord.
"""

from __future__ import annotations

import json
import time
import uuid
from enum import Enum
from typing import Any

from review_hub.config import (
    CONTINUE_ON_RESEARCH_FAILURE,
    MAX_CONSECUTIVE_FAILURES,
    MAX_REPEAT_PASSES,
    REUSE_RESULT_ON_REPEAT,
    REVIEW_URL,
)
from review_hub.engine.corrections import (
    CorrectionApplier,
    evaluate_accept_holds,
    filter_changes,
    skip_current_record,
    wait_for_page_settle,
)
from review_hub.engine.fields import IDENTITY_FIELD_KEYS, is_substantial_identity_change
from review_hub.engine.finalization import perform_final_action, prepare_final_snapshot
from review_hub.engine.research import LLMError, ResearchBackend
from review_hub.engine.transport import QAClient
from review_hub.jsonutil import safe_text


class RunnerState(str, Enum):
    IDLE = "idle"
    READING = "reading"
    DISCOVERY_FAILED = "discovery_failed"
    RESEARCHING = "researching"
    RESEARCH_FAILED = "research_failed"
    VALIDATING = "validating"
    APPLYING = "applying"
    VERIFYING = "verifying"
    SNAPSHOT = "snapshot"
    HOLDS_CHECK = "holds_check"
    HELD = "held"
    DECIDING = "deciding"
    MANUAL_REVIEW = "manual_review"
    RECORD_DONE = "record_done"
    RUN_DONE = "run_done"
    STOPPED = "stopped"


class PageOps:
    """The DOM/transport surface the runner touches, in one seam.

    The default implementation wraps the real ported functions; tests
    subclass or duck-type this to drive the state machine against fakes.
    """

    def __init__(self, applier: CorrectionApplier, transport: QAClient) -> None:
        self.applier = applier
        self.transport = transport

    def page_closed(self, page: Any) -> bool:
        return bool(page.is_closed())

    def extract_record(self, page: Any) -> dict[str, Any]:
        from review_hub.engine.discovery import extract_record

        return extract_record(page)

    def read_record_id(self, page: Any) -> str:
        from review_hub.engine.discovery import read_record_id

        return read_record_id(page)

    def skip_record(self, page: Any, reason: str) -> None:
        skip_current_record(page, self.transport, reason)

    def wait_settle(self, page: Any) -> None:
        wait_for_page_settle(page)

    def field_present(self, page: Any, field: str) -> bool:
        from review_hub.engine.corrections import field_present

        return field_present(page, field)

    def apply_change(self, page: Any, change: dict[str, Any], newly_created: bool) -> bool:
        return self.applier.apply_change(page, change, newly_created=newly_created)

    def clear_field(self, page: Any, field: str, previous_value: str) -> None:
        self.applier.apply_field_clear(page, field, previous_value=previous_value)

    def apply_type_fill(self, page: Any, entry: dict[str, Any], desired: list[str]) -> list[str]:
        from review_hub.engine.corrections import apply_type_missing

        return apply_type_missing(page, entry, desired)

    def verify_website(self, page: Any, company_name: str, url: str) -> tuple[bool | None, str]:
        from review_hub.engine.evidence import website_matches_company

        return website_matches_company(page.context, company_name, url)

    def reload_and_verify(
        self,
        page: Any,
        record: dict[str, Any],
        result: dict[str, Any],
        expected_id: str,
        *,
        applied: list,
        cleared: list,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        return prepare_final_snapshot(
            page,
            record,
            result,
            expected_id,
            applied=applied,
            cleared=cleared,
            failed=[],
            needs_clear=[],
            needs_review=[],
        )

    def flag_manual_review(self, page: Any, result: dict[str, Any]) -> None:
        from review_hub.engine.corrections import flag_for_manual_review

        flag_for_manual_review(page, result, self.transport)

    def final_action(self, page: Any, result: dict[str, Any], judgement: Any | None) -> str:
        return perform_final_action(
            page, result, self.transport, judgement, review_url=REVIEW_URL
        )


class BatchRunner:
    """Drive the review pipeline over records, emitting every transition."""

    def __init__(
        self,
        ops: PageOps,
        backend: ResearchBackend,
        sink: Any,
        *,
        mode: str = "auto",
        build_prompt: Any | None = None,
        continue_on_research_failure: bool = CONTINUE_ON_RESEARCH_FAILURE,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        reuse_result_on_repeat: bool = REUSE_RESULT_ON_REPEAT,
        max_repeat_passes: int = MAX_REPEAT_PASSES,
        log: Any = print,
        clock: Any = time.time,
    ) -> None:
        self.ops = ops
        self.backend = backend
        self.sink = sink
        self.mode = mode
        self.build_prompt = build_prompt  # record -> prompt string (prompting.py)
        self.continue_on_research_failure = continue_on_research_failure
        self.max_consecutive_failures = max_consecutive_failures
        self.reuse_result_on_repeat = reuse_result_on_repeat
        self.max_repeat_passes = max_repeat_passes
        self.log = log
        self.clock = clock

        self.run_id = uuid.uuid4().hex[:12]
        self.state = RunnerState.IDLE
        self.processed = 0
        self.failures = 0
        self.discovery_failures = 0
        self.repeat_passes_total = 0
        self.decision_tally: dict[str, int] = {}
        self.started_at: float | None = None

        # Same-company guard (v12.25): if the page serves the supplier we
        # just finalized (verdict POST lost, or leftover corrections), reuse
        # the previous research instead of another research round-trip.
        self._last_finalized_id = ""
        self._last_finalized_name = ""
        self._last_result: dict[str, Any] | None = None
        self._repeat_passes = 0

    # ------------------------------------------------------------------ #
    # Transition plumbing
    # ------------------------------------------------------------------ #
    def _transition(
        self,
        from_state: RunnerState,
        to_state: RunnerState,
        *,
        card_id: str = "",
        record_id: str = "",
        supplier_name: str = "",
        outcome: str = "",
        error: str = "",
        **meta: Any,
    ) -> None:
        from review_hub.persistence import TransitionRecord

        meta = {k: v for k, v in meta.items() if v is not None}
        self.state = to_state
        self.sink.emit(
            TransitionRecord(
                run_id=self.run_id,
                from_state=from_state.value,
                to_state=to_state.value,
                card_id=str(card_id),
                record_id=str(record_id),
                supplier_name=supplier_name,
                outcome=outcome,
                error=error,
                meta=meta,
            )
        )

    def _tally(self, result: dict[str, Any], downgraded: bool) -> None:
        key = safe_text(result.get("decision")) or "?"
        if result.get("bucket"):
            key += f" [{result['bucket']}]"
        if downgraded:
            key += " (downgraded by gate)"
        self.decision_tally[key] = self.decision_tally.get(key, 0) + 1

    # ------------------------------------------------------------------ #
    # The state machine
    # ------------------------------------------------------------------ #
    def run(self, page: Any, run_count: int) -> dict[str, Any]:
        """Process up to ``run_count`` records. Returns the run summary."""
        self.started_at = self.clock()
        self.run_id = uuid.uuid4().hex[:12]
        self._transition(
            RunnerState.IDLE, RunnerState.READING, outcome=f"run started: {run_count} records"
        )

        while self.processed < run_count:
            self.log("\n" + "#" * 60)
            self.log(f"RECORD {self.processed + 1} / {run_count}")
            self.log("#" * 60)

            if self.ops.page_closed(page):
                self.log(
                    "\nThe review-page browser window is closed. This script "
                    "never navigates that window to ChatGPT itself - paste "
                    "ChatGPT's prompt/response using a SEPARATE browser "
                    "window, and leave this one open and untouched. "
                    f"Progress: {self.processed} of {run_count} records completed."
                )
                self._transition(
                    RunnerState.READING, RunnerState.STOPPED, outcome="browser window closed"
                )
                break

            record = self._read_record(page)
            if record is None:
                break
            if record is False:  # sentinel: record skipped, keep going
                continue

            this_id = self.ops.read_record_id(page)
            this_name = safe_text(record["fields"].get("company_name")).lower()
            is_repeat = (
                self.reuse_result_on_repeat
                and self._last_result is not None
                and (
                    (this_id and this_id == self._last_finalized_id)
                    or (this_name and this_name == self._last_finalized_name)
                )
            )

            result = self._research(page, record, is_repeat)
            if result is None:
                # Research failed (loudly). Skip and maybe stop.
                if self.failures >= self.max_consecutive_failures:
                    self.log(
                        f"\n{self.failures} consecutive research failures. "
                        "Stopping safely - check your API key/quota, model "
                        "list, or the manual copy/paste workflow."
                    )
                    self._transition(
                        RunnerState.RESEARCH_FAILED,
                        RunnerState.STOPPED,
                        card_id=this_id,
                        record_id=this_id,
                        supplier_name=this_name,
                        error="consecutive research failures",
                    )
                    break
                self.ops.skip_record(page, "research failure")
                self.ops.wait_settle(page)
                continue

            # VALIDATING tally + routing -----------------------------------
            judgement = result.get("_judgement")
            downgraded = bool(judgement is not None and getattr(judgement, "downgrades", None))
            if not is_repeat:
                self._tally(result, downgraded)

            if result.get("decision") == "MANUAL_REVIEW":
                self._manual_review(page, record, result)
                # Legacy counts every served card on this path unconditionally
                # (main.py:3298) - a flagged record never consumes research
                # twice, so there is no repeat-loop hazard here.
                self.processed += 1
                if self.processed >= run_count:
                    break
                self.ops.wait_settle(page)
                continue

            if result.get("decision") == "REJECT":
                self._reject(page, result, this_id, this_name, is_repeat)
                if self.state is RunnerState.STOPPED:
                    break
                if self.processed >= run_count:
                    break
                self.ops.wait_settle(page)
                continue

            outcome = self._apply_and_finalize(
                page, record, result, this_id, this_name, is_repeat
            )
            if outcome == "stop":
                break
            if self.processed >= run_count:
                break
            self.ops.wait_settle(page)

        return self._finish()

    # ------------------------------------------------------------------ #
    # Stages
    # ------------------------------------------------------------------ #
    def _read_record(self, page: Any) -> dict[str, Any] | None | bool:
        """READING. Returns the record, False when skipped, None when stopping."""
        from review_hub.engine.discovery import FieldDiscoveryError

        try:
            record = self.ops.extract_record(page)
        except FieldDiscoveryError as exc:
            # Fail loudly and skip rather than silently sending a
            # blank/degraded record for research - see FieldDiscoveryError's
            # docstring for why the old silent fallback made this exact
            # failure invisible.
            self.log(
                f"\n⚠ Field discovery failed for this record: {exc}\n"
                "  Skipping this record instead of researching against a "
                "page it could not actually read."
            )
            self.discovery_failures += 1
            self._transition(
                RunnerState.READING,
                RunnerState.DISCOVERY_FAILED,
                error=str(exc),
                outcome="skipping record (field discovery failed)",
            )
            if self.discovery_failures >= self.max_consecutive_failures:
                self.log(
                    f"\n{self.discovery_failures} consecutive field-discovery "
                    "failures. Stopping safely - this usually means the "
                    "review page's DOM structure changed (a real selector "
                    "break), not a transient timing race."
                )
                self._transition(
                    RunnerState.DISCOVERY_FAILED,
                    RunnerState.STOPPED,
                    error="consecutive field-discovery failures",
                )
                return None
            self.ops.skip_record(page, "field discovery failed")
            self.ops.wait_settle(page)
            return False
        self.discovery_failures = 0

        self.log("\nCurrent fields:")
        self.log(json.dumps(record["fields"], indent=2, ensure_ascii=False))
        self._transition(
            RunnerState.READING,
            RunnerState.RESEARCHING,
            record_id=safe_text(record.get("record_id")),
            supplier_name=safe_text(record["fields"].get("company_name")),
            outcome="record read",
        )
        return record

    def _research(
        self, page: Any, record: dict[str, Any], is_repeat: bool
    ) -> dict[str, Any] | None:
        """RESEARCHING with the repeat guard. None on (loud) failure."""
        this_id = self.ops.read_record_id(page)
        this_name = safe_text(record["fields"].get("company_name")).lower()

        if is_repeat and self._repeat_passes < self.max_repeat_passes:
            self._repeat_passes += 1
            self.repeat_passes_total += 1
            self.log(
                f"\n↻ SAME SUPPLIER SERVED AGAIN ({this_id or this_name}). "
                f"Reusing the previous research result - no research round-trip. "
                f"(repeat pass {self._repeat_passes}/{self.max_repeat_passes})"
            )
            self._transition(
                RunnerState.RESEARCHING,
                RunnerState.VALIDATING,
                record_id=this_id,
                supplier_name=this_name,
                outcome="repeat guard: reusing previous result",
            )
            return dict(self._last_result or {})

        if is_repeat:
            self.log(
                f"\n⚠ {this_id or this_name} served {self._repeat_passes} extra times; "
                "researching afresh in case the record really changed."
            )
        self._repeat_passes = 0

        from review_hub.engine.prompting import SYSTEM_PROMPT, build_research_prompt

        prompt = (
            self.build_prompt(record)
            if self.build_prompt
            else build_research_prompt(record, browsing=True)
        )
        try:
            result = self.backend.research(prompt, SYSTEM_PROMPT)
        except LLMError as exc:
            # Any backend failure is loud: report it, count it, and let the
            # loop decide skip vs stop. A silent None would strand the
            # record with no correction applied and no trace.
            self.log(f"\n✗ Research failed: {exc}")
            self.failures += 1
            self._transition(
                RunnerState.RESEARCHING,
                RunnerState.RESEARCH_FAILED,
                record_id=this_id,
                supplier_name=this_name,
                error=str(exc),
                outcome="research failure",
            )
            if not self.continue_on_research_failure:
                raise
            return None

        from review_hub.engine.finalization import validate_scope_result

        try:
            result = validate_scope_result(result)
        except Exception as exc:  # gate rejections (ValueError contract)
            self.log(f"\n✗ Invalid research result ({exc})")
            self.failures += 1
            self._transition(
                RunnerState.RESEARCHING,
                RunnerState.RESEARCH_FAILED,
                record_id=this_id,
                supplier_name=this_name,
                error=str(exc),
                outcome="invalid research result",
            )
            if not self.continue_on_research_failure:
                raise
            return None

        self.failures = 0
        self._transition(
            RunnerState.RESEARCHING,
            RunnerState.VALIDATING,
            record_id=this_id,
            supplier_name=this_name,
            outcome=f"research ok: {result.get('decision')}",
        )
        return result

    def _manual_review(self, page: Any, record: dict[str, Any], result: dict[str, Any]) -> None:
        """Genuinely uncertain after real research - route to a human.

        No field corrections are applied (the gate already forced
        changes=[]), and neither Platform ready nor Reject is ever clicked
        for this record.
        """
        self.ops.flag_manual_review(page, result)
        # finalized=False: no verdict was written, so this goes to the
        # undecided bucket rather than Accepted/Rejected.
        self._transition(
            RunnerState.VALIDATING,
            RunnerState.MANUAL_REVIEW,
            record_id=safe_text(record.get("record_id")),
            supplier_name=safe_text(result.get("company_name")),
            outcome="flagged for manual review; left undecided",
            reason=safe_text(result.get("manual_review_reason")),
        )
        self._last_finalized_id, self._last_finalized_name, self._last_result = "", "", None

    def _reject(
        self,
        page: Any,
        result: dict[str, Any],
        this_id: str,
        this_name: str,
        is_repeat: bool,
    ) -> None:
        """FAST PATH: REJECT decisions skip all field changes.

        There is no point editing, clearing, or creating fields on a record
        that is about to be rejected - it wastes time on clicks,
        verification loops, and website fetches for data that will never be
        used.
        """
        try:
            outcome_name = self.ops.final_action(page, result, result.get("_judgement"))
            # Recorded only after the verdict actually landed: if the final
            # action raised, nothing was rejected, and persistence must not
            # claim otherwise.
            self._transition(
                RunnerState.DECIDING,
                RunnerState.RECORD_DONE,
                card_id=this_id,
                record_id=this_id,
                supplier_name=this_name,
                outcome=f"rejected ({outcome_name})",
            )
            self._remember_finalized(this_id, this_name, result)
            if not is_repeat:
                self.processed += 1
        except Exception as exc:
            self._transition(
                RunnerState.DECIDING,
                RunnerState.STOPPED,
                card_id=this_id,
                record_id=this_id,
                supplier_name=this_name,
                error=f"final action failed on REJECT: {exc}",
            )
            raise

    def _apply_and_finalize(
        self,
        page: Any,
        record: dict[str, Any],
        result: dict[str, Any],
        this_id: str,
        this_name: str,
        is_repeat: bool,
    ) -> str:
        """APPLYING -> VERIFYING -> SNAPSHOT -> HOLDS_CHECK -> DECIDING.

        Returns "stop" when the run must stop (safety stop, reload drift).
        """
        changes = result.get("changes", [])
        if isinstance(changes, dict):
            # A lone change object instead of a one-item list - same shape
            # drift as scope_match/decision. Wrap it rather than silently
            # dropping every proposed correction on the record.
            changes = [changes]
        elif not isinstance(changes, list):
            self.log(f"↷ Ignoring malformed 'changes' value: {changes!r}")
            changes = []

        filtered = filter_changes(record, changes, log=self.log)

        applied: list[str] = []
        skipped: list[str] = []
        cleared: list[tuple[str, str]] = []
        identity_renamed: list[tuple[str, str, str]] = []
        created_fields: list[str] = []

        self._transition(
            RunnerState.VALIDATING,
            RunnerState.APPLYING,
            record_id=this_id,
            supplier_name=this_name,
            outcome=f"applying {len(filtered.allowed)} change(s)",
            applied_count=len(filtered.allowed),
        )

        for change in filtered.allowed:
            field = change.get("field")

            # EXPLICIT CLEAR path: remove a contaminated value. Gated in the
            # filter above (flag present + reason given + field non-empty).
            if change.get("_clear"):
                reason = safe_text(change.get("clear_reason"))
                if not self.ops.field_present(page, field):
                    self.log(f"↷ Clear skipped for {field}: not present in the current UI")
                    continue
                try:
                    self.ops.clear_field(page, field, safe_text(record["fields"].get(field)))
                    cleared.append((field, reason))
                    self.log(f"⌫ Cleared contaminated {field} (reason: {reason})")
                except Exception as exc:  # noqa: BLE001 - field-level problem
                    self.log(f"✗ Could not clear {field}: {exc}")
                continue

            # TYPE FILL path: the record had no type and the page offered to
            # add one. Tick the proposed qualifying categories via the
            # checkbox group, then verify (handled in apply_type_missing).
            if change.get("_type_fill"):
                desired = [safe_text(v) for v in (change.get("new_value_multi") or [])]
                try:
                    written = self.ops.apply_type_fill(
                        page, change.get("_type_entry"), desired
                    )
                    applied.append("supplier_type")
                    created_fields.append("Type")
                    self.log(f"✓ Set type (was missing): {', '.join(written)}")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"✗ Could not set type: {exc}")
                    skipped.append("supplier_type")
                continue

            new_value = safe_text(change.get("new_value"))
            newly_created = False

            # If the field does not exist yet, create it from the page's
            # "ADD MISSING:" list before giving up. A field is only ever
            # created when a verified value is about to be written.
            if not self.ops.field_present(page, field):
                from review_hub.engine.addmissing import match_missing_field

                entry = match_missing_field(field, record.get("missing_fields") or [])
                if entry is None:
                    self.log(
                        f"↷ Skipped {field}: not present in the database/UI "
                        "and not offered under ADD MISSING"
                    )
                    continue
                if not new_value:
                    self.log(f"↷ Skipped {field}: refusing to create a field with no value")
                    continue
                # v13.2: db.save_edit() uses jsonb_set(..., TRUE), which
                # creates the key when absent. A missing field is just an
                # edit; the "+ Field" click was only ever a UI affordance.
                self.log(f"+ Adding missing field '{entry['label']}' as {field} (direct edit)")
                created_fields.append(entry["label"])
                newly_created = True

            # Independent sanity check for a proposed website: fetch it and
            # confirm the company's own name actually appears there before
            # writing it. A failed FETCH (network, timeout, blocked domain)
            # never blocks the write - only a fetch that succeeds and finds
            # no match does, and every outcome is logged either way.
            if field == "website_url" and new_value:
                verdict, detail = self.ops.verify_website(
                    page, safe_text(result.get("company_name")), new_value
                )
                if verdict is False:
                    self.log(
                        f"⚠ HOLDING website_url — independent check found no match: {detail}\n"
                        f"  NOT writing {new_value!r} automatically. Verify by hand."
                    )
                    filtered.needs_review.append((field, new_value, detail))
                    continue
                if verdict is None:
                    self.log(f"↷ website_url sanity check inconclusive ({detail}); applying anyway.")
                else:
                    self.log(f"✓ website_url sanity check passed: {detail}")

            try:
                did_apply = self.ops.apply_change(page, change, newly_created=newly_created)
                if did_apply:
                    applied.append(field)
                    self.log(f"✓ Applied: {field} → {new_value!r}")
                    if safe_text(field).lower() in IDENTITY_FIELD_KEYS:
                        old_value = safe_text(record["fields"].get(field))
                        if is_substantial_identity_change(old_value, new_value):
                            identity_renamed.append((field, old_value, new_value))
                            self.log(
                                f"  ⚠ Substantial company-identity change. "
                                f"{old_value!r} → {new_value!r} - held for confirmation "
                                "before the record is finalized."
                            )
                        else:
                            self.log(
                                f"  (minor name cleanup only — not held: "
                                f"{old_value!r} → {new_value!r})"
                            )
            except Exception as exc:  # noqa: BLE001 - field-level problem
                self.log(f"✗ Could not apply {field}: {exc}")
                # A failed field is a field-level problem. Record it and move
                # on to the next proposed change ON THIS SAME RECORD. The
                # previous version called the Skip action here, but Skip on
                # this UI abandons the ENTIRE supplier and advances to the
                # next one - so a single missing edit button silently moved
                # the page to a different company, and every later action was
                # applied to that wrong company. Abandoning one field just
                # means not submitting that field's inline form.
                skipped.append(field)
                continue

        self._transition(
            RunnerState.APPLYING,
            RunnerState.VERIFYING,
            record_id=this_id,
            supplier_name=this_name,
            outcome=f"applied {len(applied)}, skipped {len(skipped)}, cleared {len(cleared)}",
            applied=applied,
            created=created_fields,
        )

        # Last line of defence before an irreversible verdict: confirm the
        # page still shows the supplier this research is about. If the page
        # drifted (a stray record-level action, a manual keypress in the
        # automated window, a UI auto-advance), applying Platform ready /
        # Reject here would mark the WRONG company - a silent, incorrect
        # write into the production database.
        expected_id = safe_text(record.get("record_id"))
        current_id = self.ops.read_record_id(page)
        if expected_id and current_id and expected_id != current_id:
            self.log(
                f"\n✗ SAFETY STOP: the review page moved to a different supplier.\n"
                f"  Research was for {expected_id}; the page now shows {current_id}.\n"
                f"  No final action was performed, so no verdict was written to the\n"
                f"  wrong company. Re-run this supplier and report this message."
            )
            self._transition(
                RunnerState.VERIFYING,
                RunnerState.STOPPED,
                card_id=this_id,
                record_id=expected_id,
                supplier_name=this_name,
                error=f"page drifted: expected {expected_id}, saw {current_id}",
                outcome="safety stop before verdict",
            )
            return "stop"

        # v32.1 - FINAL SNAPSHOT ORDER: corrections saved -> RELOAD the page
        # -> extract every field -> THEN press the verdict.
        is_accept = result.get("decision") == "ACCEPT"
        corrected = None
        snapshot_problems: list[str] = []
        if is_accept:
            self._transition(
                RunnerState.VERIFYING,
                RunnerState.SNAPSHOT,
                record_id=expected_id,
                supplier_name=this_name,
                outcome="reloading before final snapshot",
            )
            try:
                corrected, snapshot_problems = self.ops.reload_and_verify(
                    page,
                    record,
                    result,
                    expected_id,
                    applied=applied,
                    cleared=cleared,
                )
            except Exception as exc:  # includes ReloadDriftError
                self.log(
                    f"\n✗ SAFETY STOP: {exc}.\n"
                    "  No verdict was pressed and nothing was written to the accepted\n"
                    "  record store. Check both companies in the review app before resuming."
                )
                self._transition(
                    RunnerState.SNAPSHOT,
                    RunnerState.STOPPED,
                    card_id=this_id,
                    record_id=expected_id,
                    supplier_name=this_name,
                    error=str(exc),
                    outcome="reload drift before snapshot",
                )
                return "stop"

        # HOLDS_CHECK: the four Auto Mode accept holds, evaluated in one
        # pure function so tests can pin them exactly as legacy did.
        self._transition(
            RunnerState.SNAPSHOT,
            RunnerState.HOLDS_CHECK,
            record_id=expected_id,
            supplier_name=this_name,
            outcome="evaluating accept holds",
        )
        holds = evaluate_accept_holds(
            needs_clear=filtered.needs_clear,
            needs_review=filtered.needs_review,
            identity_renamed=identity_renamed,
            snapshot_problems=snapshot_problems,
            corrected_is_none=corrected is None,
        )
        if holds:
            self.log(
                f"\n⚠ AUTO MODE HOLD: {', '.join(holds)} on an ACCEPT decision. NOT "
                "clicking Platform ready - skipping instead so a human decides."
            )
            self.ops.skip_record(page, "unresolved flagged fields on an ACCEPT (auto mode hold)")
            self.ops.wait_settle(page)
            self._transition(
                RunnerState.HOLDS_CHECK,
                RunnerState.HELD,
                card_id=this_id,
                record_id=this_id,
                supplier_name=this_name,
                outcome="HELD, left undecided",
                error="; ".join(holds),
            )
            # A held record is NOT accepted; remember the research result so
            # a same-supplier re-serve does not burn another round-trip.
            self._remember_finalized(this_id, this_name, result)
            if not is_repeat:
                self.processed += 1
            return "continue"

        # DECIDING: the one verdict POST - never retried (see
        # finalization.perform_final_action for why a retry could accept the
        # NEXT company with no review).
        self._transition(
            RunnerState.HOLDS_CHECK,
            RunnerState.DECIDING,
            card_id=this_id,
            record_id=expected_id,
            supplier_name=this_name,
            outcome=f"submitting verdict: {result.get('decision')}",
        )
        try:
            self.ops.final_action(page, result, result.get("_judgement"))
        except Exception as exc:
            self._transition(
                RunnerState.DECIDING,
                RunnerState.STOPPED,
                card_id=this_id,
                record_id=expected_id,
                supplier_name=this_name,
                error=f"final action failed: {exc}",
            )
            raise

        self._transition(
            RunnerState.DECIDING,
            RunnerState.RECORD_DONE,
            card_id=this_id,
            record_id=expected_id,
            supplier_name=this_name,
            outcome=f"{safe_text(result.get('decision')).lower()} - verdict submitted",
            skipped=skipped,
        )
        self._remember_finalized(this_id, this_name, result)
        if not is_repeat:
            self.processed += 1
        return "continue"

    # ------------------------------------------------------------------ #
    def _remember_finalized(self, this_id: str, this_name: str, result: dict[str, Any]) -> None:
        self._last_finalized_id = this_id
        self._last_finalized_name = this_name
        self._last_result = result

    def _finish(self) -> dict[str, Any]:
        elapsed = (self.clock() - self.started_at) if self.started_at else 0.0
        # RUN_DONE is the durable log's end marker; the summary reports how
        # the run actually ended: an early safety stop, or the full budget.
        terminal = "stopped" if self.state is RunnerState.STOPPED else RunnerState.RUN_DONE.value
        self._transition(
            self.state,
            RunnerState.RUN_DONE,
            outcome=f"records processed: {self.processed}",
            decision_tally=dict(self.decision_tally),
        )
        return {
            "run_id": self.run_id,
            "processed": self.processed,
            "repeat_passes_total": self.repeat_passes_total,
            "decision_tally": dict(self.decision_tally),
            "elapsed_s": elapsed,
            "final_state": terminal,
        }
