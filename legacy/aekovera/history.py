"""Durable Excel history of every supplier this pipeline has decided on.

Why this exists
---------------
The agent already writes several narrow CSVs - accepted_non_us.csv,
manual_review_queue.csv, held_for_field_review.csv, field_discovery_failures.csv
- but each one only covers its own exception case. There was no single place
answering the most ordinary question a reviewer has: *"have we already put this
company through the pipeline, and what happened?"* A supplier that was cleanly
accepted or cleanly rejected left no durable trace at all outside the review
UI itself.

This module maintains one workbook (HISTORY_EXCEL_FILE) with three sheets:

    Accepted            - Platform ready was clicked for this supplier
    Rejected            - Reject was clicked for this supplier
    Manual review/Held  - the record was deliberately left UNDECIDED
                          (MANUAL_REVIEW, or an Auto-Mode hold)

Routing is by what actually happened to the record on the review page, not by
what the research proposed. A held ACCEPT is not in the Accepted sheet, because
nothing was accepted - the record is still sitting in the queue. When that
record is later resolved and comes back through the pipeline, its row MOVES to
the correct sheet automatically (see the de-duplication rules below).

De-duplication
--------------
One row per supplier, not one row per pass. A record is keyed on its MASTER
record id, falling back to a normalised company name when no id is exposed. On
a repeat the existing row is UPDATED in place: `first_seen` is preserved,
`last_seen` and `times_seen` advance, every other column is overwritten with
the newer information, and the row is moved between sheets if the outcome
changed. That keeps the workbook a lookup table rather than an append-only log.

Durability
----------
The workbook is saved after EVERY record, not at the end of the session, so a
crash or a closed browser half-way through a batch never costs the history.

If the workbook cannot be written - overwhelmingly the common case being the
user having it open in Excel, which makes the file read-only on Windows - the
row is appended to HISTORY_PENDING_FILE (JSON Lines) instead and a warning is
printed. Pending rows are merged into the workbook automatically on the next
save that succeeds, so closing Excel is all that is needed to recover; nothing
is lost and the run is never blocked waiting for a file lock.

openpyxl is an optional dependency. If it is not installed, this module
degrades to writing HISTORY_PENDING_FILE only, prints how to install it, and
lets the run continue - history is a record-keeping feature and must never be
able to stop a review session.
"""

import json
import re
from datetime import datetime
from pathlib import Path

from config import (
    ENABLE_HISTORY_EXCEL,
    HISTORY_EXCEL_FILE,
    HISTORY_ACCEPTED_SHEET,
    HISTORY_REJECTED_SHEET,
    HISTORY_UNDECIDED_SHEET,
    HISTORY_PENDING_FILE,
    HISTORY_WARN_ON_REPEAT,
)

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover - optional dependency
    Workbook = None
    load_workbook = None


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------
# Decision/audit columns, always present and always in this order.
META_COLUMNS = [
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
# see, not a whitelist - any other field the page exposes is appended after
# these rather than dropped (the same lesson v12.18 learned about hardcoded
# field lists).
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
# company detail: they are identity/meta and already have a meta column.
DETAIL_SKIP = {"company_name", "master", "id", "record_id"}

_HEADER_FILL = "FFEFEFEF"


def _text(value):
    """Flatten any field value to a single clean cell string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_text(v) for v in value if _text(v))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    # Excel tolerates newlines in a cell, but a control character elsewhere in
    # the range openpyxl rejects outright and would raise mid-save.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return text.strip()


def _norm_name(value):
    """Normalised company name, used only as a fallback de-dupe key."""
    text = _text(value).lower()
    # Apostrophes are DELETED, not turned into a separator: "Clayton's Crab"
    # and "Claytons Crab" are the same supplier, but splitting on the
    # apostrophe would make them "clayton s crab" vs "claytons crab" and give
    # one company two rows. Every other separator becomes a space as usual.
    text = re.sub(r"['\u2018\u2019\u02bc`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(
        r"\b(inc|llc|ltd|limited|co|corp|corporation|company|the|and)\b", " ", text
    )
    return re.sub(r"\s+", " ", text).strip()


def dedupe_key(record_id, company_name):
    """Stable identity for a supplier across runs.

    The MASTER record id is authoritative when the page exposes one. The
    normalised company name is only a fallback - it is deliberately loose
    (punctuation, case and corporate suffixes removed) because the alternative
    is the same supplier silently occupying two rows.
    """
    rid = _text(record_id)
    if rid:
        return f"id:{rid.lower()}"
    name = _norm_name(company_name)
    if name:
        return f"name:{name}"
    return ""


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------

def _pairs(items, fmt):
    out = []
    for item in items or []:
        try:
            out.append(fmt(item))
        except Exception:
            out.append(_text(item))
    return "; ".join(x for x in out if x)


def build_row(record, result, *, outcome, mode, backend, applied=(), created=(),
              cleared=(), failed=(), needs_clear=(), needs_review=(),
              identity_renamed=(), corrected=None):
    """Assemble one supplier's full history row as {column: value}.

    Company details are the values as they stand AFTER this run: the record as
    read from the page, with every successfully applied correction overlaid.
    That is what makes the workbook useful as a reference - it holds the data
    the database now actually has, not the stale values the record arrived
    with.
    """
    fields = dict(record.get("fields") or {})

    # Overlay applied corrections so the snapshot reflects the corrected state.
    applied_set = {_text(f) for f in applied}
    for change in result.get("changes") or []:
        if not isinstance(change, dict):
            continue
        field = _text(change.get("field"))
        if field and field in applied_set and change.get("new_value") is not None:
            fields[field] = change.get("new_value")
    for field, _reason in cleared or []:
        fields[_text(field)] = ""
    # v32.1: when the page was reloaded and re-read before the verdict, those
    # values ARE the record; they win over the reconstruction above.
    if corrected and corrected.get("fields"):
        fields.update(corrected["fields"])

    company = _text(result.get("company_name")) or _text(fields.get("company_name"))
    now = datetime.now().isoformat(timespec="seconds")

    row = {
        "first_seen": now,
        "last_seen": now,
        "times_seen": 1,
        "record_id": _text(record.get("record_id")),
        "company_name": company,
        "decision": _text(result.get("decision")).upper(),
        "outcome": _text(outcome),
        "run_mode": _text(mode),
        "backend": _text(backend),
        "confidence": _text(result.get("confidence")),
        "reason": _text(result.get("reason")),
        "scope_match": _text(result.get("scope_match")),
        "food_beverage_connection": _text(result.get("food_beverage_connection")),
        "qualifying_supplier_types": _text(result.get("qualifying_supplier_types")),
        "supply_country": _text(result.get("supply_country")),
        "is_us_based": _text(result.get("is_us_based")),
        "supply_origin_note": _text(result.get("supply_origin_note")),
        "manual_review_reason": _text(result.get("manual_review_reason")),
        "fields_applied": _text(list(applied)),
        "fields_created": _text(list(created)),
        "fields_cleared": _pairs(cleared, lambda x: f"{x[0]} ({x[1]})"),
        "fields_failed": _text(list(failed)),
        "fields_left_uncleared": _pairs(needs_clear, lambda x: f"{x[0]}={x[1]!r}"),
        "fields_held_for_review": _pairs(
            needs_review, lambda x: f"{x[0]}={x[1]!r} ({x[2]})"
        ),
        "identity_renamed": _pairs(
            identity_renamed, lambda x: f"{x[0]}: {x[1]!r} -> {x[2]!r}"
        ),
    }

    for field, value in fields.items():
        key = _text(field)
        if not key or key in DETAIL_SKIP or key in row:
            continue
        row[key] = _text(value)

    return row


def sheet_for(decision, finalized):
    """Which sheet this record belongs in.

    Routed on what happened to the RECORD, not on what the research proposed:
    a held ACCEPT never lands in the Accepted sheet, because the supplier was
    left undecided in the review queue rather than accepted.
    """
    decision = _text(decision).upper()
    if not finalized:
        return HISTORY_UNDECIDED_SHEET
    if decision == "ACCEPT":
        return HISTORY_ACCEPTED_SHEET
    if decision == "REJECT":
        return HISTORY_REJECTED_SHEET
    return HISTORY_UNDECIDED_SHEET


# ---------------------------------------------------------------------------
# Pending-row spillover (workbook locked, or openpyxl missing)
# ---------------------------------------------------------------------------

def _append_pending(row, sheet):
    try:
        path = Path(HISTORY_PENDING_FILE)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"sheet": sheet, "row": row}, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # pragma: no cover - disk-level failure
        print(f"⚠ Could not write {HISTORY_PENDING_FILE} either: {exc}")
        return False


def _drain_pending():
    """Read and clear the pending spillover file. Returns [(sheet, row), ...]."""
    path = Path(HISTORY_PENDING_FILE)
    if not path.exists():
        return []
    entries = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                entries.append((item.get("sheet"), item.get("row") or {}))
            except Exception:
                continue
        path.unlink()
    except Exception as exc:  # pragma: no cover
        print(f"⚠ Could not read back {HISTORY_PENDING_FILE}: {exc}")
        return []
    return entries


# ---------------------------------------------------------------------------
# Workbook I/O
# ---------------------------------------------------------------------------

def _open_workbook():
    path = Path(HISTORY_EXCEL_FILE)
    if path.exists():
        try:
            return load_workbook(path)
        except Exception as exc:
            # A corrupt/partial workbook must not take the run down, and must
            # not be silently overwritten either - move it aside so the user
            # still has whatever it held.
            backup = path.with_name(
                f"{path.stem}.corrupt-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}"
            )
            try:
                path.rename(backup)
                print(f"⚠ {HISTORY_EXCEL_FILE} could not be opened ({exc}).")
                print(f"  Moved it to {backup.name} and started a fresh workbook.")
            except Exception:
                print(f"⚠ {HISTORY_EXCEL_FILE} is unreadable ({exc}).")
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def _get_sheet(wb, name):
    if name in wb.sheetnames:
        return wb[name]
    ws = wb.create_sheet(name)
    ws.append(list(META_COLUMNS))
    return ws


def _headers(ws):
    if ws.max_row < 1:
        ws.append(list(META_COLUMNS))
    return [_text(c.value) for c in ws[1]]


def _ensure_columns(ws, headers, row):
    """Widen the sheet with any column this row has and the sheet does not.

    Fields are discovered per record, so a supplier processed next month may
    legitimately expose a field no earlier record had. Appending a column is
    always safe; dropping the value would not be.
    """
    known = set(headers)
    new = [k for k in row if k not in known]
    if not new:
        return headers
    # Keep the detail block in a predictable order rather than pure arrival
    # order, so the workbook stays readable across many sessions.
    def sort_key(name):
        try:
            return (0, PREFERRED_DETAIL_ORDER.index(name))
        except ValueError:
            return (1, name)

    for name in sorted(new, key=sort_key):
        headers.append(name)
        ws.cell(row=1, column=len(headers), value=name)
    return headers


def _write_row(ws, headers, row_number, row):
    for index, header in enumerate(headers, start=1):
        if header in row:
            ws.cell(row=row_number, column=index, value=row[header])


def _index_workbook(wb):
    """Map dedupe key -> (sheet name, row number, existing row dict)."""
    index = {}
    for name in wb.sheetnames:
        ws = wb[name]
        headers = [_text(c.value) for c in ws[1]] if ws.max_row >= 1 else []
        if not headers:
            continue
        for row_number in range(2, ws.max_row + 1):
            values = {}
            for col, header in enumerate(headers, start=1):
                values[header] = ws.cell(row=row_number, column=col).value
            key = dedupe_key(values.get("record_id"), values.get("company_name"))
            if key:
                index[key] = (name, row_number, values)
    return index


def _style(ws):
    """Freeze the header row, bold it, add a filter and sane column widths."""
    if ws.max_row < 1:
        return
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor=_HEADER_FILL)
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    try:
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}1"
    except Exception:
        pass
    for col in range(1, ws.max_column + 1):
        header = _text(ws.cell(row=1, column=col).value)
        width = 42 if header in {
            "reason", "food_beverage_connection", "manual_review_reason",
            "products", "description", "specialty", "supply_origin_note",
        } else max(12, min(len(header) + 6, 28))
        ws.column_dimensions[get_column_letter(col)].width = width


def _apply(wb, index, sheet_name, row):
    """Insert or update one row, moving it between sheets if the outcome changed."""
    key = dedupe_key(row.get("record_id"), row.get("company_name"))
    previous = index.get(key) if key else None

    if previous:
        old_sheet, old_number, old_values = previous
        # Carry the original sighting forward and count this pass.
        row["first_seen"] = _text(old_values.get("first_seen")) or row["first_seen"]
        try:
            row["times_seen"] = int(old_values.get("times_seen") or 0) + 1
        except (TypeError, ValueError):
            row["times_seen"] = 2
        if old_sheet == sheet_name:
            ws = wb[old_sheet]
            headers = _ensure_columns(ws, _headers(ws), row)
            _write_row(ws, headers, old_number, row)
            return "updated"
        # Decision changed since last time (a held record resolved, a reject
        # revisited): remove the stale row so the supplier appears once, in
        # the sheet that now reflects reality.
        wb[old_sheet].delete_rows(old_number)
        index.update(_index_workbook(wb))
        verb = "moved"
    else:
        verb = "added"

    ws = _get_sheet(wb, sheet_name)
    headers = _ensure_columns(ws, _headers(ws), row)
    row_number = ws.max_row + 1
    _write_row(ws, headers, row_number, row)
    if key:
        index[key] = (sheet_name, row_number, row)
    return verb


def record_decision(record, result, *, outcome, mode, backend, finalized,
                    applied=(), created=(), cleared=(), failed=(),
                    needs_clear=(), needs_review=(), identity_renamed=(), corrected=None):
    """Write one supplier's outcome into the history workbook.

    Called once per record, right after the verdict is performed (or
    deliberately withheld). Never raises: a history problem must not be able
    to interrupt a review session, so every failure path here degrades to a
    printed warning plus the pending-file spillover.
    """
    if not ENABLE_HISTORY_EXCEL:
        return

    try:
        row = build_row(
            record, result, outcome=outcome, mode=mode, backend=backend,
            applied=applied, created=created, cleared=cleared, failed=failed,
            needs_clear=needs_clear, needs_review=needs_review,
            identity_renamed=identity_renamed, corrected=corrected,
        )
        sheet_name = sheet_for(result.get("decision"), finalized)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"⚠ Could not build the history row: {exc}")
        return

    if load_workbook is None:
        _append_pending(row, sheet_name)
        print(
            f"⚠ openpyxl is not installed, so {HISTORY_EXCEL_FILE} was not updated.\n"
            f"  This record was queued in {HISTORY_PENDING_FILE} and will be merged\n"
            "  in automatically once you run:  pip install openpyxl"
        )
        return

    try:
        wb = _open_workbook()
        index = _index_workbook(wb)

        # Flush anything stranded by an earlier locked-file save first, so the
        # workbook ends up in chronological order rather than with this
        # record ahead of ones that happened before it.
        for pending_sheet, pending_row in _drain_pending():
            if pending_row:
                _apply(wb, index, pending_sheet or HISTORY_UNDECIDED_SHEET, pending_row)

        verb = _apply(wb, index, sheet_name, row)

        for name in (HISTORY_ACCEPTED_SHEET, HISTORY_REJECTED_SHEET,
                     HISTORY_UNDECIDED_SHEET):
            if name in wb.sheetnames:
                _style(wb[name])
            else:
                _style(_get_sheet(wb, name))

        wb.save(HISTORY_EXCEL_FILE)
        print(f"✓ History {verb}: '{row['company_name'] or row['record_id']}' "
              f"→ {HISTORY_EXCEL_FILE} [{sheet_name}]")
    except PermissionError:
        # Almost always: the workbook is open in Excel. Don't block the run,
        # don't lose the row - spill it and merge on the next good save.
        if _append_pending(row, sheet_name):
            print(
                f"⚠ {HISTORY_EXCEL_FILE} is locked (it is probably open in Excel).\n"
                f"  This record was queued in {HISTORY_PENDING_FILE} and will be\n"
                "  merged in automatically on the next record once you close it."
            )
    except Exception as exc:
        if _append_pending(row, sheet_name):
            print(f"⚠ Could not update {HISTORY_EXCEL_FILE} ({exc}); "
                  f"queued in {HISTORY_PENDING_FILE} instead.")


# ---------------------------------------------------------------------------
# "Have we seen this company before?"
# ---------------------------------------------------------------------------

def load_index():
    """Snapshot of prior decisions for the repeat warning.

    Returns {dedupe key: {company_name, decision, outcome, sheet, last_seen,
    times_seen, reason}}. Read once at startup; a stale entry only ever costs
    an informational line, so it is not re-read per record.
    """
    if not ENABLE_HISTORY_EXCEL or load_workbook is None:
        return {}
    path = Path(HISTORY_EXCEL_FILE)
    if not path.exists():
        return {}
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return {}
    seen = {}
    try:
        for name in wb.sheetnames:
            ws = wb[name]
            rows = ws.iter_rows(values_only=True)
            try:
                headers = [_text(h) for h in next(rows)]
            except StopIteration:
                continue
            for values in rows:
                item = dict(zip(headers, values))
                key = dedupe_key(item.get("record_id"), item.get("company_name"))
                if not key:
                    continue
                seen[key] = {
                    "company_name": _text(item.get("company_name")),
                    "decision": _text(item.get("decision")),
                    "outcome": _text(item.get("outcome")),
                    "sheet": name,
                    "last_seen": _text(item.get("last_seen")),
                    "times_seen": _text(item.get("times_seen")),
                    "reason": _text(item.get("reason")),
                }
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return seen


def warn_if_seen(index, record):
    """Print a heads-up when the current record was decided in an earlier run.

    Purely informational - the record is still researched and processed
    normally. The point is that a reviewer watching the run can immediately
    tell they are looking at something the pipeline has already ruled on, and
    why, instead of discovering the duplicate later in the workbook.
    """
    if not (HISTORY_WARN_ON_REPEAT and index):
        return None
    fields = record.get("fields") or {}
    key = dedupe_key(record.get("record_id"), fields.get("company_name"))
    previous = index.get(key) if key else None
    if not previous:
        return None
    print(
        f"\n↺ ALREADY IN HISTORY: this supplier was processed on "
        f"{previous['last_seen'] or 'an earlier run'} "
        f"→ {previous['decision'] or '?'} [{previous['sheet']}]"
    )
    if previous.get("reason"):
        print(f"  Previous reason: {previous['reason'][:200]}")
    print(f"  See {HISTORY_EXCEL_FILE}. Processing it again now; the existing "
          "row will be updated in place.")
    return previous
