"""Final action + scope validation - the verdict half of legacy main().

Ports ``validate_scope_result``, ``perform_final_action``, and
``prepare_final_snapshot`` as functions over injected collaborators so the
BatchRunner can drive them without module globals.

The verdict invariant from legacy v13 is preserved verbatim: POST /verdict is
matched by unit_id AND nonce, so the write either lands on the intended
company or is refused as already/lease_lost. It is NEVER retried - a saved
verdict followed by a slow redirect looks identical to a lost one, and the
old retry path accepted the NEXT company with no review.
"""

from __future__ import annotations

from typing import Any

from review_hub.engine import decision
from review_hub.engine.transport import (
    DECISION_TO_VERDICT,
    VERDICT_ALREADY,
    VERDICT_LEASE_LOST,
    VERDICT_SAVED,
    QAClient,
    read_card_identity,
)
from review_hub.jsonutil import safe_text

VERDICT_LABELS = {
    "ACCEPT": "Platform ready",
    "PARK": "Outreach first",
    "RE_ENRICH": "Re-enrich",
    "REJECT": "Reject",
}


def validate_scope_result(result: dict[str, Any], *, allow_accept: bool = True) -> dict[str, Any]:
    """Validate one judge answer against the v4 rulebook.

    v13: the old 259-line gate spoke ACCEPT / REJECT / MANUAL_REVIEW and
    raised ValueError on anything else, so a v4 PARK or RE_ENRICH was thrown
    out as an "Invalid decision" and the record was skipped. All four
    verdicts are now first-class; decision.validate() holds the rules.

    The contract is unchanged for callers: the result dict comes back with a
    normalized "decision", cleaned "changes", and a ValueError only when the
    answer is unusable. The Judgement is attached as result["_judgement"] so
    the final action can reuse its note and suggested_url without
    re-deriving them.
    """
    judgement = decision.validate(result, allow_accept=allow_accept)

    for warning in judgement.warnings:
        print(f"  ⚠ {warning}")
    for note in judgement.downgrades:
        print(f"  ↓ downgraded to {judgement.decision}: {note}")

    result["decision"] = judgement.decision
    result["bucket"] = judgement.bucket
    result["changes"] = judgement.changes
    result["_judgement"] = judgement

    if judgement.decision == "MANUAL_REVIEW":
        # Preserve the key the manual-review logger already reads.
        result["manual_review_reason"] = judgement.note

    # scope_match is consulted by a few downstream helpers; make it concrete.
    if judgement.decision in ("ACCEPT", "PARK", "RE_ENRICH"):
        result.setdefault("scope_match", True)

    return result


def perform_final_action(
    page: Any,
    result: dict[str, Any],
    transport: QAClient,
    judgement: Any | None = None,
    review_url: str | None = None,
    *,
    card: tuple[str, str] | None = None,
) -> str:
    """Record the final verdict for the company currently on screen.

    Returns the transport outcome (saved / already recorded). Raises
    RuntimeError on lease-lost or supplier-missing, and never retries: the
    POST /verdict contract matches unit_id AND nonce, so the write either
    lands on the intended company or is refused.

    ``card`` optionally supplies ``(unit_id, nonce)`` directly; when omitted
    it is read from the live page, exactly as legacy did.
    """
    decision_name = (
        judgement.decision if judgement else safe_text(result.get("decision")).upper()
    )

    if decision_name not in DECISION_TO_VERDICT:
        raise RuntimeError(
            f"{decision_name!r} is not a final verdict. MANUAL_REVIEW goes to skip."
        )

    unit_id, nonce = card if card is not None else read_card_identity(page)
    note = judgement.note if judgement else safe_text(result.get("reason"))
    suggested_url = judgement.suggested_url if judgement else ""

    label = VERDICT_LABELS[decision_name]

    outcome = transport.submit_verdict(
        unit_id, nonce, decision_name, note=note, suggested_url=suggested_url
    )

    if outcome == VERDICT_SAVED:
        print(f"✓ {label} recorded for unit {unit_id}.")
    elif outcome == VERDICT_ALREADY:
        print(f"↷ Unit {unit_id} was already decided; left as it was.")
    elif outcome == VERDICT_LEASE_LOST:
        raise RuntimeError(
            f"The lease on unit {unit_id} expired before the verdict was "
            f"submitted, so NOTHING was recorded. The record is back in the "
            f"queue. This usually means the record took longer than the lease "
            f"window; no other company was affected."
        )
    else:
        raise RuntimeError(f"Unit {unit_id} was not found when submitting the verdict.")

    # The browser is still showing the decided card; move it to the next one.
    # NOTE: the transport never follows the 303 redirect after a verdict POST
    # (that could lease another card); navigating the page explicitly here is
    # the only forward movement, and a failure is a warning, not an error.
    if page is not None and review_url:
        try:
            page.goto(review_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - advancement is cosmetic
            print(f"⚠ Verdict saved but the page did not advance ({exc}); reload by hand.")

    return outcome


def prepare_final_snapshot(
    page: Any,
    record: dict[str, Any],
    result: dict[str, Any],
    expected_id: str,
    *,
    applied: list,
    cleared: list,
    failed: list,
    needs_clear: list,
    needs_review: list,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Reload -> re-extract the corrected record, BEFORE the verdict.

    Returns ``(corrected, problems)``. The accepted-snapshot file row that
    legacy wrote here (accepted_companies.xlsx pending/confirm/rollback)
    is owned by the storage task's durable store; the runner emits a
    ``snapshot_taken`` transition instead. Raises ReloadDriftError (callers
    must stop without pressing a verdict).
    """
    from review_hub.engine.corrections import fetch_corrected_record

    corrected = fetch_corrected_record(
        page, expected_id, result=result, applied=applied, cleared=cleared
    )
    problems: list[str] = []
    if corrected is None:
        problems.append("the final record could not be re-read after reloading the page")
    elif corrected.get("not_landed"):
        problems.append(
            f"{len(corrected['not_landed'])} saved correction(s) are not on the "
            "reloaded record: "
            + ", ".join(f for f, _e, _p in corrected["not_landed"])
        )
    del record, failed, needs_clear, needs_review  # row content moves to the store
    return corrected, problems
