"""Row assembly for the lifecycle store - ported from the v34 archive.

The store is the system of record, but the workbooks stay as exports, and an
operator diffing a v34 workbook against a store export must see the same
columns in the same order (and, row for row, the same cell text). So the row
shaping from ``legacy/aekovera/history.py`` and
``legacy/aekovera/accepted_snapshots.py`` is ported here nearly verbatim:
``flatten_text`` is legacy ``_text``, ``normalize_name`` is ``_norm_name``,
``dedupe_key`` and ``sheet_for`` keep their names, and ``build_history_row`` /
``build_accepted_row`` are ``history.build_row`` / ``accepted_snapshots.build_row``.

Deviations from the archive (all deliberate, none change a column layout):
- timestamps are timezone-aware UTC, not naive local time;
- ``openpyxl`` handling lives in ``exports.py`` - this module is pure data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# ---------------------------------------------------------------------------
# Column layout constants (verbatim from legacy history.py / accepted_snapshots.py)
# ---------------------------------------------------------------------------
# Decision/audit columns, always present and always in this order.
HISTORY_META_COLUMNS = [
    "first_seen",
    "last_seen",
    "times_seen",
    "record_id",
    "company_name",
    "decision",
    "outcome",
    "run_mode",
    "backend",
    "confidence",
    "reason",
    "scope_match",
    "food_beverage_connection",
    "qualifying_supplier_types",
    "supply_country",
    "is_us_based",
    "supply_origin_note",
    "manual_review_reason",
    "fields_applied",
    "fields_created",
    "fields_cleared",
    "fields_failed",
    "fields_left_uncleared",
    "fields_held_for_review",
    "identity_renamed",
]

# Company-detail columns come after the meta block. Fields are discovered per
# record at runtime, so this is a preferred ORDER for the ones we expect to
# see, not a whitelist - any other field is appended after these rather than
# dropped (the same lesson v12.18 learned about hardcoded field lists).
PREFERRED_DETAIL_ORDER = [
    "dba",
    "dba_name",
    "primary_email",
    "general_email",
    "email",
    "primary_phone",
    "phone",
    "website_url",
    "linkedin_url",
    "address",
    "city",
    "state",
    "zip",
    "country",
    "type",
    "supplier_type",
    "specialty",
    "products",
    "certs",
    "certifications",
    "description",
]

# Written into the detail block under these names but never treated as a
# company detail: they are identity/meta and already have a column.
DETAIL_SKIP = {"company_name", "master", "id", "record_id"}

# accepted_companies.xlsx: identity first, edit summary last.
ACCEPTED_LEAD_COLUMNS = ["accepted_at", "record_id", "company_name"]
ACCEPTED_TAIL_COLUMNS = [
    "verdict_status",
    "edit_status",
    "edits_made",
    "fields_failed",
    "fields_unresolved",
    "fields_not_landed",
    "confirmed_by",
    "snapshot_source",
    "first_accepted",
    "times_accepted",
]

# Sheet names (legacy config.py values - these ARE the v34 export shapes).
HISTORY_ACCEPTED_SHEET = "Accepted"
HISTORY_REJECTED_SHEET = "Rejected"
HISTORY_UNDECIDED_SHEET = "Manual review & held"
ACCEPTED_SNAPSHOT_SHEET = "Accepted companies"

SOURCE_PAGE = "page reloaded and re-read after corrections, before the verdict"
SOURCE_RECONSTRUCTED = "reconstructed (page re-read failed)"

PENDING_STATUS = "pending - written before Platform ready"
ACCEPTED_STATUS = "accepted"

_DIFF_VALUE_LIMIT = 120


# ---------------------------------------------------------------------------
# Value flattening (legacy _text / _norm_name / dedupe_key)
# ---------------------------------------------------------------------------
def flatten_text(value: Any) -> str:
    """Flatten any field value to a single clean cell string.

    Excel tolerates newlines in a cell, but a control character elsewhere in
    the range openpyxl rejects outright and would raise mid-save - control
    characters become spaces (ported from legacy ``_text``).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(v for v in (flatten_text(x) for x in value) if v)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return text.strip()


def normalize_name(value: Any) -> str:
    """Normalised company name, used only as a fallback de-dupe key.

    Apostrophes are DELETED, not turned into a separator: "Clayton's Crab"
    and "Claytons Crab" are the same supplier, but splitting on the
    apostrophe would give one company two rows (ported from ``_norm_name``).
    """
    text = flatten_text(value).lower()
    text = re.sub(r"['\u2018\u2019\u02bc`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(inc|llc|ltd|limited|co|corp|corporation|company|the|and)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_key(record_id: Any, company_name: Any) -> str:
    """Stable identity for a supplier across runs.

    The MASTER record id is authoritative when the page exposes one. The
    normalised company name is only a fallback - it is deliberately loose
    because the alternative is the same supplier silently occupying two rows.
    """
    rid = flatten_text(record_id)
    if rid:
        return f"id:{rid.lower()}"
    name = normalize_name(company_name)
    if name:
        return f"name:{name}"
    return ""


def format_pairs(items: Iterable[Any], fmt: Any) -> str:
    """Join (field, ...) pairs with the legacy ``_pairs`` tolerance: a format
    failure flattens the item instead of raising."""
    out = []
    for item in items or ():
        try:
            out.append(fmt(item))
        except Exception:
            out.append(flatten_text(item))
    return "; ".join(x for x in out if x)


def shorten_value(value: Any) -> str:
    """Cap a diff value at the legacy 120-char bound (``_short``)."""
    text = flatten_text(value)
    if len(text) > _DIFF_VALUE_LIMIT:
        text = text[: _DIFF_VALUE_LIMIT - 1] + "…"
    return text


# ---------------------------------------------------------------------------
# supplier_history.xlsx rows (legacy history.build_row)
# ---------------------------------------------------------------------------
def final_view_fields(
    record: Mapping[str, Any],
    result: Mapping[str, Any],
    applied: Sequence[Any] = (),
    cleared: Iterable[Any] = (),
    corrected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Company details as they stand AFTER this run.

    The record as read from the page, with every successfully applied
    correction overlaid. That is what makes the history useful as a reference
    - it holds the data the database now actually has, not the stale values
    the record arrived with. When the page was reloaded and re-read before the
    verdict (v32.1), those values ARE the record and they win.
    """
    fields: dict[str, Any] = dict(record.get("fields") or {})

    applied_set = {flatten_text(f) for f in applied or ()}
    for change in result.get("changes") or []:
        if not isinstance(change, dict):
            continue
        field = flatten_text(change.get("field"))
        if field and field in applied_set and change.get("new_value") is not None:
            fields[field] = change.get("new_value")
    for item in cleared or ():
        fields[flatten_text(item[0])] = ""
    if corrected and corrected.get("fields"):
        fields.update(corrected["fields"])
    return fields


def build_history_row(
    record: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    outcome: str,
    mode: str,
    backend: str,
    applied: Sequence[Any] = (),
    created: Sequence[Any] = (),
    cleared: Iterable[Any] = (),
    failed: Sequence[Any] = (),
    needs_clear: Iterable[Any] = (),
    needs_review: Iterable[Any] = (),
    identity_renamed: Iterable[Any] = (),
    corrected: Mapping[str, Any] | None = None,
    now: str = "",
) -> dict[str, Any]:
    """Assemble one supplier's full history row as {column: value}.

    Ported from legacy ``history.build_row``; ``now`` carries the timestamp
    the caller already formatted (first_seen/last_seen share one value).
    """
    fields = final_view_fields(record, result, applied, cleared, corrected)

    company = flatten_text(result.get("company_name")) or flatten_text(
        fields.get("company_name")
    )

    row: dict[str, Any] = {
        "first_seen": now,
        "last_seen": now,
        "times_seen": 1,
        "record_id": flatten_text(record.get("record_id")),
        "company_name": company,
        "decision": flatten_text(result.get("decision")).upper(),
        "outcome": flatten_text(outcome),
        "run_mode": flatten_text(mode),
        "backend": flatten_text(backend),
        "confidence": flatten_text(result.get("confidence")),
        "reason": flatten_text(result.get("reason")),
        "scope_match": flatten_text(result.get("scope_match")),
        "food_beverage_connection": flatten_text(result.get("food_beverage_connection")),
        "qualifying_supplier_types": flatten_text(result.get("qualifying_supplier_types")),
        "supply_country": flatten_text(result.get("supply_country")),
        "is_us_based": flatten_text(result.get("is_us_based")),
        "supply_origin_note": flatten_text(result.get("supply_origin_note")),
        "manual_review_reason": flatten_text(result.get("manual_review_reason")),
        "fields_applied": flatten_text(list(applied or ())),
        "fields_created": flatten_text(list(created or ())),
        "fields_cleared": format_pairs(cleared, lambda x: f"{x[0]} ({x[1]})"),
        "fields_failed": flatten_text(list(failed or ())),
        "fields_left_uncleared": format_pairs(needs_clear, lambda x: f"{x[0]}={x[1]!r}"),
        "fields_held_for_review": format_pairs(
            needs_review, lambda x: f"{x[0]}={x[1]!r} ({x[2]})"
        ),
        "identity_renamed": format_pairs(
            identity_renamed, lambda x: f"{x[0]}: {x[1]!r} -> {x[2]!r}"
        ),
    }

    for field, value in fields.items():
        key = flatten_text(field)
        if not key or key in DETAIL_SKIP or key in row:
            continue
        row[key] = flatten_text(value)

    return row


def sheet_for(decision: Any, finalized: bool) -> str:
    """Which sheet this record belongs in.

    Routed on what happened to the RECORD, not on what the research proposed:
    a held ACCEPT never lands in the Accepted sheet, because the supplier was
    left undecided in the review queue rather than accepted.
    """
    decision_text = flatten_text(decision).upper()
    if not finalized:
        return HISTORY_UNDECIDED_SHEET
    if decision_text == "ACCEPT":
        return HISTORY_ACCEPTED_SHEET
    if decision_text == "REJECT":
        return HISTORY_REJECTED_SHEET
    return HISTORY_UNDECIDED_SHEET


# ---------------------------------------------------------------------------
# accepted_companies.xlsx rows (legacy accepted_snapshots.build_row)
# ---------------------------------------------------------------------------
def _reconstruct_fields(
    record: Mapping[str, Any],
    result: Mapping[str, Any],
    applied: Sequence[Any],
    cleared: Iterable[Any],
) -> dict[str, Any]:
    """Fallback when the page could not be re-read: original + saved changes."""
    fields: dict[str, Any] = dict(record.get("fields") or {})
    applied_set = {flatten_text(f) for f in applied or ()}
    for change in result.get("changes") or []:
        if not isinstance(change, dict):
            continue
        field = flatten_text(change.get("field"))
        if field in applied_set and change.get("new_value") is not None:
            fields[field] = change.get("new_value")
    for item in cleared or ():
        fields[flatten_text(item[0])] = ""
    return fields


def format_edits(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """'field: old -> new' for every field whose value changed during the run."""
    lines = []
    for key in list(before) + [k for k in after if k not in before]:
        if flatten_text(key) in DETAIL_SKIP and flatten_text(key) != "company_name":
            continue
        old, new = flatten_text(before.get(key)), flatten_text(after.get(key))
        if old == new:
            continue
        if not new:
            lines.append(f"{key}: cleared (was {shorten_value(old)!r})")
        elif not old:
            lines.append(f"{key}: added {shorten_value(new)!r}")
        else:
            lines.append(f"{key}: {shorten_value(old)!r} -> {shorten_value(new)!r}")
    return lines


def build_accepted_row(
    record: Mapping[str, Any],
    corrected: Mapping[str, Any] | None,
    result: Mapping[str, Any],
    *,
    applied: Sequence[Any] = (),
    cleared: Iterable[Any] = (),
    failed: Sequence[Any] = (),
    needs_clear: Iterable[Any] = (),
    needs_review: Iterable[Any] = (),
    confirmed_by: str = "",
    now: str = "",
) -> dict[str, Any]:
    """One accepted-company row: the corrected record, exactly as uploaded.

    Ported from legacy ``accepted_snapshots.build_row``. ``corrected`` (the
    reloaded page read) wins when present; otherwise the row is reconstructed
    from the original record plus the corrections that were confirmed saved,
    and ``snapshot_source`` says so.
    """
    original: dict[str, Any] = dict(record.get("fields") or {})
    if corrected and corrected.get("fields"):
        fields: dict[str, Any] = dict(corrected["fields"])
        record_id = flatten_text(corrected.get("record_id")) or flatten_text(
            record.get("record_id")
        )
        source = SOURCE_PAGE
    else:
        fields = _reconstruct_fields(record, result, applied, cleared)
        record_id = flatten_text(record.get("record_id"))
        source = SOURCE_RECONSTRUCTED

    edits = format_edits(original, fields)
    failed_list = [flatten_text(f) for f in failed or () if flatten_text(f)]
    unresolved = [flatten_text(x[0]) for x in list(needs_clear or ()) + list(needs_review or ())]
    not_landed = list((corrected or {}).get("not_landed") or [])

    if failed_list or unresolved or not_landed:
        parts = []
        if not_landed:
            parts.append(f"{len(not_landed)} not on the reloaded record")
        if failed_list:
            parts.append(f"{len(failed_list)} failed")
        if unresolved:
            parts.append(f"{len(unresolved)} unresolved")
        status = "partial - " + ", ".join(parts)
    elif edits:
        status = "complete"
    else:
        status = "no changes needed"

    row: dict[str, Any] = {
        "accepted_at": now,
        "record_id": record_id,
        "company_name": flatten_text(fields.get("company_name"))
        or flatten_text(result.get("company_name")),
        "edit_status": status,
        "edits_made": "\n".join(edits),
        "fields_failed": ", ".join(failed_list),
        "fields_unresolved": ", ".join(unresolved),
        "fields_not_landed": "; ".join(
            f"{f}: saved {shorten_value(e)!r}, page shows {shorten_value(p)!r}"
            for f, e, p in not_landed
        ),
        "verdict_status": ACCEPTED_STATUS,
        "confirmed_by": flatten_text(confirmed_by),
        "snapshot_source": source,
        "first_accepted": now,
        "times_accepted": 1,
    }
    for key, value in fields.items():
        key = flatten_text(key)
        if key and key not in DETAIL_SKIP and key not in row:
            row[key] = flatten_text(value)
    return row
