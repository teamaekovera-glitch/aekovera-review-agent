"""Field discovery and record extraction from the review page.

Ported from the v34 archive (legacy/aekovera/main.py). Selector logic and
retry behaviour are unchanged; only module paths moved (fields, config,
jsonutil, addmissing live in the review_hub package now).
"""

import re

from review_hub.config import (
    FIELD_DISCOVERY_MAX_ATTEMPTS,
    FIELD_DISCOVERY_RETRY_DELAY_MS,
    PAGE_CONTEXT_CHARS,
    PAGE_CONTEXT_SELECTOR,
)
from review_hub.engine import addmissing
from review_hub.engine.fields import FIELD_BLOCKLIST
from review_hub.jsonutil import safe_text

_DISCOVER_FIELDS_JS = """
() => {
    const out = [];
    document.querySelectorAll('input[type="hidden"][name="field"]').forEach((h) => {
        const form = h.closest('form');
        if (!form) return;
        let kind = 'none';
        if (form.querySelector('input[name="new_value_multi"]')) kind = 'multi';
        else if (form.querySelector('textarea[name="new_value"]')) kind = 'textarea';
        else if (form.querySelector('input[name="new_value"]')) kind = 'text';
        out.push({ field: h.value, kind: kind });
    });
    return out;
}
"""


class FieldDiscoveryError(Exception):
    """Raised when discover_fields() finds zero field inputs after retrying.

    This is almost always a hydration race, not a page with genuinely no
    editable fields: right after the page auto-advances to the next
    supplier ("Saved - next supplier ready."), the new record's hidden
    `field` inputs can take a moment to render.

    Callers MUST NOT treat this as "proceed with whatever we've got." The
    previous behaviour silently substituted a hardcoded 8-field list
    (email/email2/phone/website/city/state/zip/country) whenever discovery
    came back empty - with no warning printed. Two things followed from
    that, both invisibly: (1) the researcher was handed `CURRENT RECORD:
    {}` - a blank structured payload - while still being asked to verify
    and correct a page it could not actually read; and (2) EDITABLE FIELDS
    silently narrowed to that same 8-field list, which excludes dba,
    specialty, products, and the description - exactly the fields most
    likely to carry a real, worth-fixing data-contamination problem. A
    model that correctly spots contamination in one of those fields then
    has no way to propose clearing it, and the failure produces no visible
    signal to the operator that anything went wrong.

    The fix is to fail loudly and skip the record instead: see
    FIELD_DISCOVERY_MAX_ATTEMPTS / FIELD_DISCOVERY_RETRY_DELAY_MS in
    config.py and the try/except around extract_record() in the runner.
    """


def discover_fields(page):
    """Read the record's real field keys straight from the page.

    The review UI exposes one hidden `field` input per editable field, inside
    the form that edits it. Reading those is far more reliable than keeping a
    hand-maintained key list in sync with the UI - the keys for Specialty,
    Products, Address, DBA and Certs are whatever the app calls them, not
    whatever we guessed.

    Retries up to FIELD_DISCOVERY_MAX_ATTEMPTS times, FIELD_DISCOVERY_RETRY_DELAY_MS
    apart (a JS-evaluate error is treated the same as an empty result and also
    retried, since either can be transient during a page transition).

    Raises FieldDiscoveryError if every attempt comes back empty. Deliberately
    does NOT fall back to a hardcoded field list on failure - see
    FieldDiscoveryError's docstring for why that used to make the failure
    invisible.

    Returns a list of {"field": key, "kind": "text"|"textarea"|"multi"}.
    """
    last_exc = None
    for attempt in range(1, FIELD_DISCOVERY_MAX_ATTEMPTS + 1):
        try:
            found = page.evaluate(_DISCOVER_FIELDS_JS)
        except Exception as exc:
            last_exc = exc
            found = None

        if found:
            seen = set()
            result = []
            for entry in found:
                key = safe_text(entry.get("field"))
                if not key or key in seen:
                    continue
                seen.add(key)
                result.append({"field": key, "kind": entry.get("kind") or "none"})
            if result:
                if attempt > 1:
                    print(
                        f"  (field discovery recovered on attempt "
                        f"{attempt}/{FIELD_DISCOVERY_MAX_ATTEMPTS})"
                    )
                return result

        if attempt < FIELD_DISCOVERY_MAX_ATTEMPTS:
            page.wait_for_timeout(FIELD_DISCOVERY_RETRY_DELAY_MS)

    detail = f" Last error: {last_exc}" if last_exc else " (no exception - just zero fields every time)"
    total_wait = (FIELD_DISCOVERY_MAX_ATTEMPTS - 1) * FIELD_DISCOVERY_RETRY_DELAY_MS
    raise FieldDiscoveryError(
        f"No field inputs found after {FIELD_DISCOVERY_MAX_ATTEMPTS} attempts "
        f"over ~{total_wait}ms.{detail}"
    )


def editable_fields_from(discovered):
    """Which discovered fields the automation may actually write."""
    return [
        e["field"] for e in discovered
        if e["field"].lower() not in FIELD_BLOCKLIST and e["kind"] in {"text", "textarea"}
    ]


def readonly_fields_from(discovered):
    """Fields shown to the researcher as context but never written."""
    return [
        e["field"] for e in discovered
        if e["field"].lower() in FIELD_BLOCKLIST or e["kind"] == "multi"
    ]


MASTER_ID_RE = re.compile(r"\bMST-[A-Za-z0-9_-]+\b")


def read_record_id(page):
    """Identify the supplier currently on screen, for drift detection.

    The review card shows a MASTER id (e.g. MST-11336) that we never edit,
    which makes it a stable fingerprint for "is this still the same company".
    Returns "" when no id is visible; callers must treat that as "unknown"
    rather than as a mismatch.
    """
    try:
        text = page.locator("body").inner_text()
    except Exception:
        return ""
    match = MASTER_ID_RE.search(text or "")
    return match.group(0) if match else ""


def read_field_values(page, known):
    """Read the current value of every discovered field straight from the page.

    Shared by extract_record() (the first read, before research) and
    fetch_corrected_record() (the re-read just before Platform ready), so both
    snapshots are taken in exactly the same way and are directly comparable.
    """
    record = {}
    fields = page.locator("input[type='hidden'][name='field']")
    for i in range(fields.count()):
        field_input = fields.nth(i)
        field = safe_text(field_input.input_value())
        # Previously this dropped anything outside the eight-field whitelist,
        # so the researcher never saw Products/Specialty/Address/DBA/Certs and
        # could not enrich what it could not read. Now every discovered field
        # is extracted; write permission is enforced separately via
        # `editable_fields`.
        if field not in known:
            continue

        form = field_input.locator("xpath=ancestor::form[1]")
        if form.count() == 0:
            continue

        # Textarea (description/products/address in the inspected page)
        textarea = form.locator("textarea[name='new_value']")
        if textarea.count():
            record[field] = safe_text(textarea.first.input_value())
            continue

        # Multi-value supplier type checkboxes
        multi = form.locator("input[name='new_value_multi']")
        if multi.count():
            checked = []
            for j in range(multi.count()):
                cb = multi.nth(j)
                if cb.is_checked():
                    checked.append(safe_text(cb.get_attribute("value")))
            # If nothing is checked, still return empty.
            record[field] = checked
            continue

        # Normal input
        normal = form.locator("input[name='new_value']")
        if normal.count():
            record[field] = safe_text(normal.first.input_value())
        else:
            record[field] = ""

    return record


def extract_record(page):
    """
    Read the current review page without trusting any field.
    The application exposes hidden `field` inputs and the corresponding
    editable `new_value` controls; we use those to reconstruct the record.
    """
    discovered = discover_fields(page)
    editable = editable_fields_from(discovered)
    readonly = readonly_fields_from(discovered)
    known = {e["field"] for e in discovered}
    record = read_field_values(page, known)

    # Useful identity hints from links.
    links = page.locator("a")
    link_data = []
    for i in range(min(links.count(), 50)):
        a = links.nth(i)
        try:
            link_data.append({
                "text": safe_text(a.inner_text()),
                "href": a.get_attribute("href") or ""
            })
        except Exception:
            pass

    # Page text is included only as context; the researcher must independently
    # verify it. Most of this page is the review UI's own chrome, so it is
    # filtered down: a smaller paste means a faster ChatGPT turn.
    #
    # PAGE_CONTEXT_SELECTOR, when set, scopes this read to a single
    # record-card container instead of the whole <body> - the strongest
    # defense against chrome leaking in, since nav links, the reviewer's
    # username, and shift-status text simply live outside that container.
    # condense_page_context()'s filter below still applies regardless, as a
    # second layer (and is the only layer while PAGE_CONTEXT_SELECTOR is
    # unset).
    raw_text = None
    if PAGE_CONTEXT_SELECTOR:
        try:
            container = page.locator(PAGE_CONTEXT_SELECTOR)
            if container.count():
                raw_text = safe_text(container.first.inner_text())
        except Exception:
            raw_text = None
    if raw_text is None:
        raw_text = safe_text(page.locator("body").inner_text())
    body_text = condense_page_context(raw_text)

    return {
        "fields": record,
        "links": link_data,
        "page_context": body_text,
        "missing_fields": addmissing.detect_missing_fields(page),
        "record_id": read_record_id(page),
        "editable_fields": editable,
        "readonly_fields": readonly,
    }


# Lines that are review-UI furniture rather than company information.
# "log\s*out" (not just "logout") so "Log out" - two words, as the live UI
# actually renders it - is caught; the original single-word pattern let it
# straight through. "saved\b.*ready" catches "Saved - next supplier ready."
# (the em dash and words between "Saved" and "ready" are why a plain prefix
# match wasn't enough). "open all" catches the "Open all" links shortcut.
CONTEXT_NOISE = re.compile(
    r"^(skip|reject|accept|platform ready|save correction|cancel|edit|next|previous|"
    r"log\s*out|login|sign out|dashboard|loading|keyboard shortcuts?|shortcuts?|"
    r"press [a-z]|[a-z] = .*|source url|optional|open all|saved\b.*ready)\b",
    re.IGNORECASE,
)

# Standalone nav/session chrome lines, matched against the WHOLE line rather
# than as a prefix. These specifically must NOT be prefix-matched: a company
# could plausibly be named "Review Foods Inc." or "Mine Specialty Coffee",
# and a prefix match would eat the real field value along with the chrome
# (the same false-rejection lesson v12.10/v12.11 already learned for the
# scope gate - see README). An exact, whole-line, case-insensitive match on
# known chrome strings is unambiguous.
CONTEXT_NOISE_EXACT_LINES = {
    "aekovera qa", "review", "mine", "log out", "logout",
}

# The reviewer's on-shift status line ("On shift since 16:18 UTC") leaks
# session state - who is logged in, and since when - into every research
# prompt. The clock varies per session, so this is a pattern, not a fixed
# string.
_ON_SHIFT_RE = re.compile(r"^on shift since\s+\d", re.IGNORECASE)


def condense_page_context(text, limit=None):
    """Strip UI chrome and duplication from the review page text.

    The raw page runs ~12,000 characters, most of it buttons, shortcut hints
    and repeated labels. Condensing keeps the company-identifying lines and
    cuts the paste size by roughly 80%.
    """
    if limit is None:
        limit = PAGE_CONTEXT_CHARS

    kept = []
    for raw_line in safe_text(text).splitlines():
        line = raw_line.strip()
        if not line or len(line) == 1:
            continue
        if CONTEXT_NOISE.match(line):
            continue
        if line.lower() in CONTEXT_NOISE_EXACT_LINES:
            continue
        if _ON_SHIFT_RE.match(line):
            continue
        # NOTE: repeated lines are deliberately NOT removed. Records commonly
        # carry the same value in two fields (EMAIL and EMAIL 2), and dropping
        # the duplicate would leave the second label with no value under it.
        kept.append(line)

    condensed = "\n".join(kept)
    if len(condensed) > limit:
        condensed = condensed[:limit] + "\n[TRUNCATED]"
    return condensed
