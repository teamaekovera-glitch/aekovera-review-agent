"""Correction cleaning, application, verification, holds, and final snapshot.

Ported from the v34 archive (legacy/aekovera/main.py, corrections sections).
Behaviour - including every safety guard - is unchanged. Two seams were
extracted for testability without changing behavior:

- filter_changes(): the pre-apply hard safety filter, now a pure function.
- evaluate_accept_holds(): the four Auto Mode hold conditions, now a pure
  function pinned by tests exactly as legacy/test_pipeline.py and main()
  assemble them.

The module-level QA global of the archive becomes an injected transport
(CorrectionApplier). The DOM paths and their comments are moved intact.
"""

import re
import time

from review_hub.config import (
    ADVANCE_VERIFY_TIMEOUT_MS,
    FIELD_PRESENT_WAIT_MS,
    HOLD_ACCEPT_IF_EDITS_NOT_LANDED,
    HOLD_ACCEPT_IF_SNAPSHOT_FAILED,
    HOLD_ACCEPT_ON_UNRESOLVED_FIELDS,
    HOLD_ON_IDENTITY_RENAME,
    POST_SETTLE_TIMEOUT_MS,
    RELOAD_BEFORE_FINAL_SNAPSHOT,
    REVIEW_URL,
)
from review_hub.engine import addmissing
from review_hub.engine.discovery import (
    MASTER_ID_RE,
    discover_fields,
    read_field_values,
    read_record_id,
)
from review_hub.engine.fields import FIELD_BLOCKLIST, FIELD_LABELS
from review_hub.engine.fields import FIELDS as FIELDS_FALLBACK
from review_hub.engine.transport import QAClient, QAHttpError, read_card_identity
from review_hub.jsonutil import safe_text

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")

# A cleaned value should never contain a fragment of raw or percent-encoded
# JSON. Seeing one here means something upstream (ChatGPT's own malformed
# citation-link formatting, in the real cases this caught) spliced the wrong
# object into a plain field value - a real example turned a country/city
# value into text like '...MN, [United States](https://site.com%22},
# {%22field%22:%22city%22,%22new_value%22:%22Madelia%22},...)' which then
# got written verbatim into the live database. This is the last line of
# defence: if a leak marker survives all cleaning below, the value is
# refused rather than saved.
_JSON_LEAK_MARKERS = (
    "%22field%22", "%22new_value%22", "%22company_name%22",
    "%22decision%22", "%22scope_match%22", "%22qualifying_supplier_types%22",
    '"field":', '"new_value":', '"company_name":', '"decision":',
)


def looks_like_json_leak(text):
    """True when text contains a fragment of raw/percent-encoded JSON that
    has no business being in a plain field value."""
    haystack = safe_text(text).lower()
    return any(marker in haystack for marker in _JSON_LEAK_MARKERS)


def clean_corrected_value(field, value):
    """Convert ChatGPT's corrected value into plain UI input text.

    ChatGPT sometimes returns Markdown links such as
    [name@example.com](mailto:name@example.com) or
    [https://example.com](https://example.com). The review UI expects only
    the actual value, never Markdown or a mailto/source wrapper.

    A markdown link can also appear PARTWAY through a longer value rather
    than being the whole string - e.g. a city/country value where only part
    of it got wrapped in a citation-style link, with the link's target itself
    corrupted into a percent-encoded fragment of the changes JSON (a real
    case: Harris Honey Company's "country" value became "United]
    (https://harrishoneymn.com%22},{%22field%22:%22city%22,...) States" and
    was written straight into the live record). The old exact-fullmatch
    check only caught a value that WAS ENTIRELY one link; this now removes
    every markdown link found ANYWHERE in the text, keeping only its visible
    portion (or, for email fields, its mailto target) and discarding
    whatever was inside the parens - garbage target included.
    """
    if value is None:
        return None

    text = safe_text(value)

    def _replace_link(m):
        visible, target = m.group(1).strip(), m.group(2).strip()
        if field in {"primary_email", "general_email"} and target.lower().startswith("mailto:"):
            return target[7:]
        return visible

    text = _MARKDOWN_LINK_RE.sub(_replace_link, text)

    # ChatGPT sometimes returns a half-formed markdown link as the whole
    # value, e.g. "[https://www.linkedin.com/company/foo" (missing closing
    # bracket/parens). Strip the leading "[" so we don't write garbage.
    if text.startswith("[") and ("http://" in text or "https://" in text):
        text = text.lstrip("[").strip()

    # Remove accidental mailto: prefixes.
    if field in {"primary_email", "general_email"}:
        text = re.sub(r"^mailto:", "", text, flags=re.IGNORECASE).strip()

    text = text.strip()

    if field == "primary_phone" and text:
        # The UI stores bare digits (e.g. 2073734513). Strip tel: prefixes and
        # human formatting so the written value matches what the app expects,
        # while preserving a leading + for international numbers. Anything that
        # does not look like a single phone number is left untouched rather
        # than mangled - better a visible odd value than a silent corruption.
        candidate = re.sub(r"^tel:", "", text, flags=re.IGNORECASE).strip()
        intl = candidate.lstrip().startswith("+")
        digits = re.sub(r"\D", "", candidate)
        if 7 <= len(digits) <= 15:
            text = ("+" + digits) if intl else digits

    return text


# Fields where free text is expected to be researched/written prose - these
# are exactly the fields the LANGUAGE RULE in the prompt applies to. Identity/
# structural fields (emails, phone, url, address, zip, master id, etc.) are
# excluded: an address or a brand name can legitimately contain non-English
# characters and is not what "filters won't match" is about.
_LANGUAGE_CHECKED_FIELDS = {
    "products",
    "specialty",
    "description",
    "sub_categories",
    "subcategories",
    "sub_category",
    "product_category",
    "product_category_parent",
    "certifications",
    "other_certifications",
    "food_beverage_connection",
    "reason",
    "dba_name",
}

# Non-Latin scripts are an unambiguous signal (Cyrillic, CJK, Greek, Arabic,
# Hebrew, Thai, etc.) - any real presence means the value cannot be plain
# English and is flagged outright.
_NON_LATIN_RE = re.compile(
    "[\u0370-\u1fff\u2e80-\ua8df\uac00-\ud7ff\uf900-\ufaff\uff66-\uffdc]"
)

# Diacritics/letters that occur in Latin-alphabet European languages (Czech,
# Polish, French, German, etc.) but essentially never in ordinary English
# prose. Case variants are listed explicitly rather than relying on re.I:
# Python's Unicode case-folding maps dotless-i "ı" onto plain ASCII "i"/"I",
# which previously caused ordinary English words (e.g. "kombucha") to
# register false diacritic hits.
_DIACRITIC_CHARS = (
    "àáâãäåæçèéêëìíîïñòóôõöøùúûüýÿ"
    "ăąćčđďěęğłńňőœřśşšťůűźżž"
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜÝ"
    "ĂĄĆČĐĎĚĘĞŁŃŇŐŒŘŚŞŠŤŮŰŹŻŽ"
    "ẞß"
)
_LATIN_DIACRITIC_RE = re.compile("[" + re.escape(_DIACRITIC_CHARS) + "]")


def looks_non_english(text):
    """Best-effort, no-false-confidence check that a value is not English.

    This never rewrites or translates anything - it only flags a proposed
    value so it surfaces for human/model attention instead of being written
    to a database that is searched and filtered in English. Deliberately
    conservative: it is tuned to catch obvious cases (Cyrillic, CJK, or
    ordinary words carrying European diacritics throughout, as in "ořechové
    máslo, arašídový krém, kokosové máslo") without flagging normal English
    text that happens to contain one or two accented proper nouns (e.g.
    "Nestlé", "Müsli").
    """
    text = safe_text(text)
    if not text:
        return False

    if _NON_LATIN_RE.search(text):
        return True

    letters = re.findall(r"[A-Za-z\u00C0-\u024F]", text)
    if not letters:
        return False

    words = text.split()
    diacritic_words = sum(1 for w in words if _LATIN_DIACRITIC_RE.search(w))
    # Require several distinct diacritic-bearing words AND a real majority
    # share of the whole value, so a couple of accented proper nouns/loan-
    # words in an English sentence don't trip this, while text that is
    # genuinely written in another language - where diacritics appear on
    # ordinary words throughout, not just names - still does.
    return diacritic_words >= 3 and (diacritic_words / max(len(words), 1)) > 0.4


def get_edit_button(page, field):
    """Use the exact aria-label exposed by the Aekovera UI.

    The inspection showed buttons such as:
      <button aria-label="edit description">
    Role/name matching can be unreliable for icon-only buttons, so the
    attribute selector is the primary locator.
    """
    button = edit_button_locator(page, field)
    if button is None:
        raise RuntimeError(
            f"Edit button not found for {field} "
            f"(no aria-label match and no pencil button next to the {field} form)"
        )
    return button


# JS that finds the pencil belonging to one field and tags it so Playwright can
# address it. See edit_button_locator() for why this is done structurally.
_TAG_FIELD_NODES_JS = """
(field) => {
    document.querySelectorAll('[data-aek-edit]').forEach(
        (e) => e.removeAttribute('data-aek-edit'));
    document.querySelectorAll('[data-aek-card]').forEach(
        (e) => e.removeAttribute('data-aek-card'));

    const hidden = document.querySelector(
        'input[type="hidden"][name="field"][value="' + field + '"]');
    if (!hidden) return { found: false, reason: 'no hidden field input' };

    const form = hidden.closest('form');
    if (!form) return { found: false, reason: 'no ancestor form' };

    // The pencil that opens this field's inline correction form is the
    // nearest button BEFORE that form in document order. Buttons inside the
    // form itself (Save correction) are excluded.
    const buttons = Array.from(document.querySelectorAll('button'));
    let pencil = null;
    for (const b of buttons) {
        if (form.contains(b)) continue;
        const formFollowsButton =
            b.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING;
        if (formFollowsButton) pencil = b;
    }
    if (!pencil) return { found: false, reason: 'no button precedes the form' };

    pencil.setAttribute('data-aek-edit', '1');
    if (pencil.parentElement) {
        pencil.parentElement.setAttribute('data-aek-card', '1');
    }
    return {
        found: true,
        label: pencil.getAttribute('aria-label') || pencil.title || '',
    };
}
"""


def edit_button_locator(page, field):
    """Locate the pencil (edit) button for one field, or return None.

    Why this is structural rather than label-based
    ----------------------------------------------
    An earlier UI exposed per-field labels such as ``aria-label="edit Phone"``,
    and the code looked those up via FIELD_LABELS. The current review UI gives
    EVERY pencil the same generic accessible name - the hover tooltip is just
    "edit" - so ``edit Phone`` / ``edit Email`` match nothing and every single
    correction failed with "Edit button not found".

    The reliable anchor is the field's own hidden input
    (``input[name="field"][value="primary_phone"]``), which is already what
    extract_record() and find_field_form() use. From that we walk to the
    enclosing form and take the nearest preceding button, which is the pencil
    that toggles it.

    This also removes a latent KeyError: the old code did FIELD_LABELS[field],
    which raised for any field created from the ADD MISSING list (address,
    linkedin, ...) because those keys are not in that table.
    """
    label = FIELD_LABELS.get(field)

    # Keep the old label-based lookup as the first choice so deployments that
    # still expose per-field labels keep working unchanged.
    if label:
        button = page.locator(f"button[aria-label=\"edit {label}\"]")
        if button.count():
            return button.first
        button = page.get_by_role("button", name=f"edit {label}", exact=True)
        if button.count():
            return button.first

    try:
        info = page.evaluate(_TAG_FIELD_NODES_JS, field)
    except Exception as exc:
        print(f"↷ Could not locate the edit control for {field}: {exc}")
        return None

    if not info or not info.get("found"):
        reason = (info or {}).get("reason", "unknown")
        print(f"↷ No edit control found for {field} ({reason}).")
        return None

    tagged = page.locator('[data-aek-edit="1"]')
    if not tagged.count():
        return None
    return tagged.first


def safe_click(element, page, timeout=2000):
    """Click an element, scrolling it clear of the sticky verdict bar first.

    The Aekovera review page has a fixed-position verdict bar at the bottom
    that intercepts pointer events for any element underneath it. This helper:
      1. Scrolls the element to the TOP of the viewport (above the bar).
      2. Attempts a normal Playwright click.
      3. Falls back to a JavaScript click if the normal click is intercepted.
    """
    try:
        element.evaluate("el => el.scrollIntoView({block: 'start'})")
        page.wait_for_timeout(80)
    except Exception:
        pass
    try:
        element.click(timeout=timeout)
    except Exception:
        element.evaluate("el => el.click()")


# ---------------------------------------------------------------------------
# v12.25 - post-save settle / advance verification
# ---------------------------------------------------------------------------
# "Save correction", "Platform ready", "Reject" and "Skip" are all real form
# POSTs on this UI: the page navigates and re-renders. The previous code
# waited a flat 150-300 ms and then went straight back to querying the DOM.
# On a normal-speed round trip that query landed mid-navigation, found zero
# hidden `field` inputs, and reported "not present in the current UI" for
# every remaining change; the final G/R keypress was then swallowed by the
# reload and the SAME supplier was served again. That is why one company
# could take 3-8 iterations (and a fresh ChatGPT round-trip each time).

def wait_for_page_settle(page, max_ms=None):
    """Block until the review page is actually usable again after a POST."""
    max_ms = max_ms or POST_SETTLE_TIMEOUT_MS
    try:
        page.wait_for_load_state("load", timeout=max_ms)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=min(1500, max_ms))
    except Exception:
        pass
    deadline = time.time() + max_ms / 1000.0
    while time.time() < deadline:
        try:
            if page.locator("input[type='hidden'][name='field']").count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(100)
    return False


def field_present(page, field, wait_ms=None):
    """True once the field's hidden input exists; polls instead of one count()."""
    wait_ms = wait_ms if wait_ms is not None else FIELD_PRESENT_WAIT_MS
    selector = f"input[type='hidden'][name='field'][value='{field}']"
    deadline = time.time() + wait_ms / 1000.0
    while True:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        page.wait_for_timeout(100)


def wait_for_record_change(page, before_id, timeout_ms=None):
    """After a verdict/skip, wait until a DIFFERENT supplier is on screen."""
    timeout_ms = timeout_ms or ADVANCE_VERIFY_TIMEOUT_MS
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        wait_for_page_settle(page, max_ms=800)
        now = read_record_id(page)
        if now and before_id and now != before_id:
            return now
        if not before_id and now:
            return now
        page.wait_for_timeout(150)
    return ""


def find_field_form(page, field):
    hidden = page.locator(
        f"input[type='hidden'][name='field'][value='{field}']"
    )
    if not hidden.count():
        raise RuntimeError(f"Correction form not found for {field}")

    form = hidden.first.locator("xpath=ancestor::form[1]")
    if not form.count():
        raise RuntimeError(f"Form not found for {field}")
    return form


def normalize_for_verification(field, value):
    """Normalize values conservatively for post-save verification."""
    value = clean_corrected_value(field, value) or ""
    if field in {"primary_email", "general_email"}:
        return re.sub(r"\s+", "", value).lower()
    if field == "website_url":
        value = value.strip().lower()
        value = re.sub(r"^https?://", "", value)
        return value.rstrip("/")
    if field == "primary_phone":
        return re.sub(r"\D", "", value)
    return re.sub(r"\s+", " ", value).strip().lower()


# Reads the visible row that belongs to ONE field: everything sitting between
# the field's OWN visual cell, not a document-order slice between adjacent forms.
#
# Why this changed (v12.14)
# -------------------------
# The previous implementation bounded a field's "card" by document order
# between the previous <form> and this field's <form>. That assumes fields are
# laid out one-per-row in source order. This review UI lays them out in a
# 3-column grid (e.g. TYPE / EMAIL / PHONE share a visual row, then WEBSITE /
# LINKEDIN / CITY, then STATE / ZIP / COUNTRY). Document order is NOT visual
# order, so the between-forms slice for EMAIL, PHONE, LINKEDIN and ZIP captured
# text and <a> hrefs belonging to a NEIGHBOURING column. The saved value was
# then verified against the wrong cell and reported unverified -> "skipped",
# even though the write had actually persisted (which is why those values are
# visible on the card after the run).
#
# The reliable bound is the field's own cell: walk UP from the field's hidden
# input to the nearest ancestor that also contains this field's editable
# control (its new_value input/textarea) but does NOT contain any OTHER field's
# hidden input. That ancestor is exactly the one field's row/cell, regardless
# of how the grid is nested, so text and hrefs collected inside it belong to
# this field alone.
_FIELD_CARD_JS = """
(field) => {
    const hidden = document.querySelector(
        'input[type="hidden"][name="field"][value="' + field + '"]');
    if (!hidden) return { found: false, text: '', hrefs: [] };

    // Count how many distinct field hidden-inputs an element contains. The
    // field's own cell must contain exactly one (this field's).
    const otherFieldCount = (el) => {
        const hiddens = el.querySelectorAll(
            'input[type="hidden"][name="field"]');
        let others = 0;
        hiddens.forEach((h) => { if (h !== hidden) others += 1; });
        return others;
    };

    // Start at the enclosing form (the edit form for this field) and expand
    // outward to include the visible value cell, stopping BEFORE the scope
    // would swallow another field's hidden input.
    let cell = hidden.closest('form') || hidden.parentElement;
    if (!cell) return { found: false, text: '', hrefs: [] };

    let parent = cell.parentElement;
    while (parent && parent !== document.body && otherFieldCount(parent) === 0) {
        cell = parent;
        parent = parent.parentElement;
    }

    const texts = [];
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
        const t = (walker.currentNode.textContent || '').trim();
        if (t) texts.push(t);
    }

    const hrefs = [];
    cell.querySelectorAll('a').forEach((a) => {
        hrefs.push(a.href || a.getAttribute('href') || '');
    });

    return { found: true, text: texts.join(' '), hrefs: hrefs };
}
"""


def field_card_info(page, field):
    """Return {"text", "hrefs"} for one field's visible row.

    Anchored on the field's own form rather than on DOM nesting. The previous
    implementation walked UP from the pencil and returned the first ancestor
    with text under 500 chars - which silently returned the pencil's own icon
    glyph when the pencil had no wrapping cell, so every save "could not be
    verified". Bounding the row by the surrounding forms does not care how the
    page is nested.
    """
    try:
        info = page.evaluate(_FIELD_CARD_JS, field)
    except Exception:
        return {"text": "", "hrefs": []}
    if not info or not info.get("found"):
        return {"text": "", "hrefs": []}
    return {"text": safe_text(info.get("text")), "hrefs": info.get("hrefs") or []}


def field_display_text(page, field):
    """Return the visible text of the field's own row."""
    return field_card_info(page, field)["text"]


def likely_saved_audit_marker(before_text, after_text, expected):
    """Detect the UI's post-save audit marker when the displayed value does
    not immediately change.

    Aekovera can keep the original displayed value while attaching an audit
    badge (for example the user's name) after a correction is saved. In that
    case, the presence of a new marker is the meaningful save signal.
    """
    before = safe_text(before_text)
    after = safe_text(after_text)
    if not after or after == before:
        return False

    # Remove the field label/value text and look for newly-added short tokens.
    def tokens(text):
        return [x for x in re.split(r"\s+", text.lower()) if x]

    before_tokens = tokens(before)
    after_tokens = tokens(after)
    new_tokens = [x for x in after_tokens if x not in before_tokens]

    # Common audit-marker words rendered by the review UI.
    marker_words = {
        "hassan", "agent", "edited", "editedby", "updated", "updatedby",
        "correction", "corrected", "saved", "you"
    }
    if any(x.strip(".,:;()[]{}") in marker_words for x in new_tokens):
        return True

    # If the card changed substantially after Save and the expected value is
    # not visible, it may still be an audit-only correction. Require at least
    # one genuinely new non-control token to avoid counting a redraw as save.
    return len(new_tokens) > 0


def verify_saved_change(page, field, expected, timeout_ms=2000):
    """Verify that Save correction produced a real UI change.

    Preferred verification is the corrected value appearing in the field
    card. If Aekovera keeps the old displayed value but adds its post-save
    audit badge, that badge is accepted as the save confirmation. A plain
    successful click/fill is never counted as an applied change.

    The card text returned by field_card_info may include form-chrome text
    ("Corrected value", "Source URL (optional)", "Save correction") when the
    inline correction form is still visible at verification time. This chrome
    is stripped before comparison so that a value like "Buffalo Pullet Group"
    is not lost inside "Corrected value Source URL (optional) Save correction
    Buffalo Pullet Group, LLC ...". Without this, fields whose form closes
    slowly fail verification even though the save succeeded - the expected
    value IS in the card text, but the raw contains() check is drowned by
    unrelated tokens.
    """
    expected_norm = normalize_for_verification(field, expected)
    before_text = field_display_text(page, field)
    deadline = time.time() + (timeout_ms / 1000)

    # Form-chrome phrases that contaminate the card text when the inline
    # correction form is still open. Stripped case-insensitively before the
    # text-based match so the expected value is not masked.
    _FORM_CHROME = [
        "corrected value",
        "source url (optional)",
        "source url",
        "save correction",
    ]

    def _strip_form_chrome(text):
        """Remove inline-form UI labels from card text for cleaner matching."""
        t = text
        for phrase in _FORM_CHROME:
            t = re.sub(re.escape(phrase), " ", t, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", t).strip()

    while time.time() < deadline:
        # Strategy 1: inspect the visible field card itself.
        visible = field_display_text(page, field)
        if visible:
            candidate_values = []
            card = field_card_info(page, field)
            try:
                candidate_values = list(card["hrefs"])
            except Exception:
                candidate_values = []

            try:
                for href in (card["hrefs"] or []):
                    if field in {"primary_email", "general_email"} and str(href).lower().startswith("mailto:"):
                        candidate_values.append(str(href)[7:])
                    elif field == "website_url" and str(href):
                        candidate_values.append(str(href))
            except Exception:
                pass

            for candidate in candidate_values:
                if expected_norm and expected_norm == normalize_for_verification(field, candidate):
                    return True, candidate, "field value/link"

            visible_norm = normalize_for_verification(field, visible)
            if expected_norm and expected_norm in visible_norm:
                return True, visible, "visible field card"

            # Second chance: strip form chrome and retry the text match.
            # This catches the case where the inline form is still open and
            # its labels ("Corrected value", "Save correction") are included
            # in the card text, masking the actual value.
            cleaned_visible = _strip_form_chrome(visible)
            cleaned_norm = normalize_for_verification(field, cleaned_visible)
            if expected_norm and expected_norm in cleaned_norm:
                return True, cleaned_visible, "visible field card (form-chrome stripped)"

            if likely_saved_audit_marker(before_text, visible, expected):
                return True, visible, "post-save audit marker"

        # Strategy 2: the form may still be open after save, with the value
        # sitting inside the input/textarea rather than rendered as card text.
        # Read the control's value attribute directly.
        try:
            form = find_field_form(page, field)
            for sel in ["textarea[name='new_value']", "input[name='new_value']"]:
                ctrl = form.locator(sel)
                if ctrl.count():
                    input_val = ctrl.first.input_value()
                    input_norm = normalize_for_verification(field, input_val)
                    if expected_norm and expected_norm == input_norm:
                        return True, input_val, "form input value"
                    break
        except Exception:
            pass

        page.wait_for_timeout(100)

    # Final diagnostic values are returned to the caller for the report.
    return False, field_display_text(page, field), "visible field card"


class CorrectionApplier:
    """Applies field corrections, carrying the per-record write state.

    The v34 archive kept this state in module globals (QA, CURRENT_SOURCE_URL,
    LAST_WRITTEN_VALUE); the engine passes a transport in instead, and the
    maps are cleared per record via start_record(). Behaviour is unchanged:
    the HTTP path is one POST /edit per field, the DOM path is the fallback.
    """

    def __init__(self, transport: QAClient | None = None, log=print):
        self.transport = transport
        self.log = log
        # Populated per record so the HTTP path can attach each change's source_url.
        self.source_urls: dict = {}
        # The exact text each field was saved as this record (for the post-reload check).
        self.last_written: dict = {}

    def start_record(self):
        self.source_urls.clear()
        self.last_written.clear()

    def use_http(self):
        return self.transport is not None

    def apply_text_change(self, page, field, new_value, already_open=False):
        """Save one field correction.

        v13: when the HTTP transport is up this is a single POST to /edit -- the
        same form the pencil submits -- instead of open-pencil / fill / click-save
        / wait-for-redirect / re-read-to-verify. The DOM path below is kept as a
        fallback and is still used when the transport is unavailable.

        Verification is no longer a second page read: /edit answers "Correction
        saved" only after db.save_edit() has committed the row, so the response
        *is* the confirmation.
        """
        if self.use_http():
            unit_id, _ = read_card_identity(page)
            cleaned = clean_corrected_value(field, new_value)
            if looks_like_json_leak(cleaned):
                raise RuntimeError(
                    f"{field}: refusing to write a value containing a JSON "
                    f"fragment ({cleaned[:60]!r})"
                )
            source_url = self.source_urls.get(field, "")
            ok, message = self.transport.apply_edit(unit_id, field, cleaned, source_url)
            self.last_written[field] = cleaned
            if not ok:
                raise RuntimeError(f"{field}: {message}")
            return True

        return _apply_text_change_via_dom(page, field, new_value, already_open)

    def apply_field_clear(self, page, field, previous_value=""):
        """Blank a contaminated field. v13.2: one POST /edit with new_value=\"\"."""
        if self.use_http():
            unit_id, _ = read_card_identity(page)
            ok, message = self.transport.clear_field(
                unit_id, field, self.source_urls.get(field, ""))
            if not ok:
                raise RuntimeError(f"{field}: {message}")
            return True
        return _apply_field_clear_via_dom(page, field, previous_value)

    def apply_type_change(self, page, values, source_url):
        """Set supplier_type. v13.2: POST /edit with the pipe-joined labels.

        app.py validates every part against SUPPLIER_TYPES, so an off-taxonomy
        label is refused server-side with a clear message rather than saved.
        """
        if self.use_http():
            labels = values if isinstance(values, (list, tuple)) else [values]
            joined = " | ".join(dict.fromkeys(safe_text(v) for v in labels if safe_text(v)))
            unit_id, _ = read_card_identity(page)
            ok, message = self.transport.apply_edit(unit_id, "supplier_type", joined, source_url)
            if not ok:
                raise RuntimeError(f"supplier_type: {message}")
            return True
        return _apply_type_change_via_dom(page, values, source_url)

    def apply_change(self, page, change, newly_created=False):
        field = change.get("field")
        new_value = change.get("new_value")

        # FIELDS is the always-editable core set. A field created from the page's
        # "ADD MISSING:" list is also editable, but only once it actually exists as
        # a hidden input on the page - which the check below enforces.

        if safe_text(field).lower() in FIELD_BLOCKLIST:
            self.log(f"↷ Skipped {field}: read-only field")
            return False

        # Only edit if the field actually exists in the current UI/database.
        # If it is absent, skip it rather than creating a new field.
        if not field_present(page, field):
            self.log(f"↷ Skipped {field}: field is not present in the current database/UI")
            return False
        hidden = page.locator(
            f"input[type='hidden'][name='field'][value='{field}']"
        )

        # Checkbox-group fields (supplier Type) have no reliable fill/verify path,
        # so they are never written. Detected structurally, so any future
        # checkbox field is covered without adding it to the blocklist by name.
        form = hidden.first.locator("xpath=ancestor::form[1]")
        if form.count() and form.first.locator("input[name='new_value_multi']").count():
            self.log(f"↷ Skipped {field}: checkbox field, left for a human reviewer")
            return False

        self.apply_text_change(page, field, new_value, already_open=newly_created)
        return True


def _apply_text_change_via_dom(page, field, new_value, already_open=False):
    """Edit, save, then VERIFY the persisted/displayed value.

    The Aekovera correction form contains an optional Source URL field, but
    the automation deliberately does NOT touch it.

    already_open: True when the field's "Corrected value" form was already
    revealed by a PRIOR action - specifically, clicking a "+ Specialty"
    style control under ADD MISSING opens that form directly (see
    addmissing.create_field), unlike an existing field's pencil, which
    toggles a form that starts closed. Calling get_edit_button() again in
    that case has no real pencil to find for a field that didn't exist a
    moment ago - the structural "nearest preceding button" search can
    still return SOMETHING (a nearby unrelated field's pencil), and
    clicking it either does nothing useful or, worse, toggles a different
    field's form while this one silently closes. This was why every ADD
    MISSING field failed or misfired: the code always tried to "open" a
    form that was already open. When already_open is True, the pencil-click
    step is skipped and the already-visible form is used directly; if it
    turns out not to be open after all (a deployment where the control
    behaves like a normal toggle), this falls back to the pencil-click path
    exactly as before, so nothing regresses on a different DOM shape.
    """
    new_value = clean_corrected_value(field, new_value)
    if new_value is None or new_value == "":
        raise RuntimeError(f"Refusing to apply empty/null correction for {field}")
    if looks_like_json_leak(new_value):
        raise RuntimeError(
            f"Refusing to write a value that still contains a raw/percent-"
            f"encoded JSON fragment after cleaning: {new_value!r}. This is "
            f"the last-line-of-defence guard, not the normal path - if this "
            f"fires, clean_corrected_value's markdown-link stripping missed "
            f"a new corruption shape and needs to be extended for it."
        )

    form = None
    if already_open:
        try:
            form = find_field_form(page, field)
            control = form.locator("textarea[name='new_value'], input[name='new_value']").first
            control.wait_for(state="visible", timeout=1500)
        except Exception:
            form = None  # Not actually open yet - fall through to the normal path.

    if form is None:
        edit = get_edit_button(page, field)
        safe_click(edit, page)
        page.wait_for_timeout(150)
        form = find_field_form(page, field)

    textarea = form.locator("textarea[name='new_value']")
    if textarea.count():
        target = textarea.first
    else:
        value_input = form.locator("input[name='new_value']")
        if not value_input.count():
            raise RuntimeError(f"new_value control not found for {field}")
        target = value_input.first

    # Ensure the control is visible before filling - avoids 30s default
    # timeout hangs when the form didn't actually open.
    try:
        target.evaluate("el => el.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(100)
        target.wait_for(state="visible", timeout=1500)
    except Exception:
        # The form may not have opened (click intercepted). Retry with JS click.
        try:
            edit = get_edit_button(page, field)
            edit.evaluate("el => { el.scrollIntoView({block: 'start'}); el.click(); }")
            page.wait_for_timeout(200)
            # Re-locate the target after re-opening
            form = find_field_form(page, field)
            textarea = form.locator("textarea[name='new_value']")
            if textarea.count():
                target = textarea.first
            else:
                target = form.locator("input[name='new_value']").first
            target.evaluate("el => el.scrollIntoView({block: 'center'})")
            page.wait_for_timeout(100)
            target.wait_for(state="visible", timeout=1500)
        except Exception as exc:
            raise RuntimeError(
                f"Edit form for {field} did not become visible: {exc}"
            )
    target.fill(str(new_value))

    # IMPORTANT: never fill source_url.
    save = form.locator("button", has_text="Save correction")
    if not save.count():
        save = form.get_by_role("button", name="Save correction", exact=True)
    if not save.count():
        save = form.locator("button:has-text('Save correction')")
    if not save.count():
        raise RuntimeError(f"Save correction button not found for {field}")

    safe_click(save.first, page)
    wait_for_page_settle(page)

    verified, actual, source = verify_saved_change(page, field, new_value)
    if not verified:
        raise RuntimeError(
            f"Save was clicked, but the change could not be verified. "
            f"Expected={new_value!r}; observed={actual!r}; checked={source}."
        )

    return actual


def _apply_field_clear_via_dom(page, field, previous_value=""):
    """Explicitly REMOVE a contaminated value from a field, then verify it is gone.

    This is the only path that may empty a field, and it is reached only when
    ChatGPT sent an explicit clear flag with a reason (see the apply loop). It
    is deliberately separate from apply_text_change so that function's
    "refuse empty/null" guard stays intact for every ordinary correction - an
    accidental "" can never reach here.

    previous_value is the value shown BEFORE the clear. When known, the check
    is exact: the clear succeeded iff that specific value no longer appears in
    the field's card. This is more reliable than guessing from the residual's
    shape (a leftover single-word city is otherwise indistinguishable from an
    audit badge). A shape heuristic is kept only as a fallback for when the
    previous value was not supplied.
    """
    edit = get_edit_button(page, field)
    safe_click(edit, page)
    page.wait_for_timeout(150)
    form = find_field_form(page, field)

    textarea = form.locator("textarea[name='new_value']")
    if textarea.count():
        control = textarea.first
    else:
        control = form.locator("input[name='new_value']").first
        if not control.count():
            raise RuntimeError(f"new_value control not found for {field}")

    # Ensure the control is scrolled into view and visible before filling.
    try:
        control.evaluate("el => el.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(100)
        control.wait_for(state="visible", timeout=1500)
    except Exception:
        # Retry: JS click the edit button again in case the first click
        # was intercepted by the verdict bar.
        try:
            edit.evaluate("el => { el.scrollIntoView({block: 'start'}); el.click(); }")
            page.wait_for_timeout(200)
            form = find_field_form(page, field)
            textarea = form.locator("textarea[name='new_value']")
            if textarea.count():
                control = textarea.first
            else:
                control = form.locator("input[name='new_value']").first
            control.evaluate("el => el.scrollIntoView({block: 'center'})")
            page.wait_for_timeout(100)
            control.wait_for(state="visible", timeout=1500)
        except Exception as exc:
            raise RuntimeError(
                f"Clear form for {field} did not become visible after two attempts: {exc}"
            )

    # Clear whatever is in the control (do NOT type anything in its place).
    control.fill("")

    # IMPORTANT: never fill source_url.
    save = form.locator("button", has_text="Save correction")
    if not save.count():
        save = form.get_by_role("button", name="Save correction", exact=True)
    if not save.count():
        save = form.locator("button:has-text('Save correction')")
    if not save.count():
        raise RuntimeError(f"Save correction button not found for {field}")

    safe_click(save.first, page)
    wait_for_page_settle(page)

    remaining = normalize_for_verification(field, field_display_text(page, field))

    prev_norm = normalize_for_verification(field, previous_value) if previous_value else ""
    if prev_norm:
        # Exact check: the specific contaminated value must no longer appear.
        if prev_norm in remaining:
            raise RuntimeError(
                f"Clear was saved, but the old value is still shown "
                f"({previous_value!r}). Clear it by hand."
            )
        return ""

    # Fallback (previous value unknown): infer from the residual's shape.
    label_norm = normalize_for_verification(field, FIELD_LABELS.get(field, ""))
    residual = remaining.replace(label_norm, "").strip() if label_norm else remaining
    audit_words = {"hassan", "agent", "you", "edited", "updated", "corrected", "saved"}
    residual_tokens = [t for t in re.split(r"\s+", residual) if t and t not in audit_words]
    looks_like_value = (
        "@" in residual
        or re.search(r"\d{3,}", residual)
        or re.search(r"[a-z0-9]+\.[a-z]{2,}", residual)
        or len(residual_tokens) > 1
        or any(len(t) > 12 for t in residual_tokens)
    )
    if looks_like_value:
        raise RuntimeError(
            f"Clear was saved, but the field still shows a value: {residual!r}. "
            f"The contaminated value may not have been removed - clear it by hand."
        )
    return ""


def _canonical_type_labels(change):
    """Extract the desired canonical type label(s) from a type change entry.

    The value can arrive as a list or a single string (e.g. "Food Manufacturer
    / Brand"). Each is normalized onto a canonical QUALIFYING_SUPPLIER_TYPES
    label so it can be matched against the checkboxes regardless of the exact
    punctuation/spacing ChatGPT used.
    """
    raw = change.get("new_value")
    if raw is None:
        raw = change.get("new_value_multi")
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    labels = []
    for item in items:
        canon = normalize_supplier_type(item)
        if canon and canon not in labels:
            labels.append(canon)
    return labels


def apply_type_missing(page, entry, desired_labels, timeout_ms=5000):
    """Create the type field from ADD MISSING and tick the desired categories.

    Only ever reached when the record had NO type and the page offered to add
    one. Ticks the checkbox(es) whose visible label / value normalizes to a
    desired canonical label, saves, and VERIFIES those boxes are checked.

    Returns the list of labels actually checked, or raises on failure.
    """
    if not desired_labels:
        raise RuntimeError("No recognizable supplier type to set")

    # Only qualifying categories are ever written. If ChatGPT's value maps only
    # to a non-qualifying label (e.g. equipment), refuse rather than tick it.
    writable = [label for label in desired_labels if label in QUALIFYING_SUPPLIER_TYPES]
    if not writable:
        raise RuntimeError(
            f"Proposed type(s) {desired_labels} are not a qualifying category; not set"
        )

    # Open the checkbox form via the ADD MISSING control.
    try:
        control = page.locator(
            f"button:has-text('{entry['selector_text']}'), "
            f"a:has-text('{entry['selector_text']}')"
        ).first
        if not control.count():
            control = page.get_by_text(entry["selector_text"], exact=False).first
        control.click(timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(f"Could not open the type add-control: {exc}")

    page.wait_for_timeout(250)

    # The checkbox group is now visible. Find it via new_value_multi inputs.
    checkboxes = page.locator("input[name='new_value_multi']")
    if not checkboxes.count():
        raise RuntimeError("Type checkbox group did not appear after clicking add")

    def cb_label(cb):
        # Prefer the associated <label> text; fall back to the value attribute.
        try:
            val = safe_text(cb.get_attribute("value"))
        except Exception:
            val = ""
        text = val
        try:
            cid = cb.get_attribute("id")
            if cid:
                lab = page.locator(f"label[for='{cid}']")
                if lab.count():
                    text = safe_text(lab.first.inner_text()) or val
        except Exception:
            pass
        return text, val

    checked_now = []
    want = set(writable)
    for i in range(checkboxes.count()):
        cb = checkboxes.nth(i)
        text, val = cb_label(cb)
        canon = normalize_supplier_type(text) or normalize_supplier_type(val)
        if canon in want:
            try:
                if not cb.is_checked():
                    cb.check()
                checked_now.append(canon)
            except Exception as exc:
                raise RuntimeError(f"Could not tick '{canon}': {exc}")

    missing = want - set(checked_now)
    if missing:
        raise RuntimeError(
            f"Could not find checkbox(es) for {sorted(missing)} in the type group"
        )

    # IMPORTANT: never fill source_url.
    form = checkboxes.first.locator("xpath=ancestor::form[1]")
    save = form.locator("button", has_text="Save correction")
    if not save.count():
        save = form.get_by_role("button", name="Save correction", exact=True)
    if not save.count():
        save = form.locator("button:has-text('Save correction')")
    if not save.count():
        raise RuntimeError("Save correction button not found for type")

    safe_click(save.first, page)
    wait_for_page_settle(page)

    # VERIFY: re-read the checkboxes and confirm the desired ones are checked.
    verify_boxes = page.locator("input[name='new_value_multi']")
    still_checked = set()
    for i in range(verify_boxes.count()):
        cb = verify_boxes.nth(i)
        text, val = cb_label(cb)
        canon = normalize_supplier_type(text) or normalize_supplier_type(val)
        try:
            if cb.is_checked() and canon:
                still_checked.add(canon)
        except Exception:
            pass
    if verify_boxes.count():
        not_confirmed = want - still_checked
        if not_confirmed:
            raise RuntimeError(
                f"Type saved, but could not verify {sorted(not_confirmed)} is checked"
            )
    # If the group is no longer present, the form closed after save - fall back
    # to reading the displayed TYPE card text.
    else:
        card = normalize_for_verification("supplier_type", field_display_text(page, "supplier_type"))
        for label in writable:
            if normalize_for_verification("supplier_type", label) not in card:
                raise RuntimeError(
                    f"Type saved, but '{label}' is not visible on the card"
                )

    return writable


def _apply_type_change_via_dom(page, values, source_url):
    field = "supplier_type"

    edit = get_edit_button(page, field)
    edit.click()

    form = find_field_form(page, field)

    checkboxes = form.locator("input[name='new_value_multi']")
    desired = set(values if isinstance(values, list) else [values])

    for i in range(checkboxes.count()):
        cb = checkboxes.nth(i)
        val = safe_text(cb.get_attribute("value"))
        if val in desired:
            if not cb.is_checked():
                cb.check()
        else:
            if cb.is_checked():
                cb.uncheck()

    source = form.locator("input[name='source_url']")
    if source.count() and source_url:
        source.first.fill(source_url)

    save = form.get_by_role("button", name="Save correction")
    if not save.count():
        save = form.get_by_text("Save correction", exact=True)

    safe_click(save.first, page)
    wait_for_page_settle(page)


def skip_current_record(page, transport, reason):
    """Leave the current card undecided.

    v13.2: POST /skip bound to unit_id+nonce. The DOM version pressed "s" and,
    if the page had not visibly advanced, pressed it AGAIN -- the same
    retry-on-a-moved-page defect the verdict path had.
    """
    if transport is not None:
        try:
            unit_id, nonce = read_card_identity(page)
            if transport.skip_record(unit_id, nonce):
                print(f"\u21b7 Unit {unit_id} skipped ({reason}).")
            else:
                print(f"\u21b7 Skip for unit {unit_id} refused (lease lost); "
                      f"card returns to the queue on its own.")
        except QAHttpError as exc:
            print(f"\u26a0 Could not skip over HTTP ({exc}); leaving the card "
                  f"for the lease to expire.")
        try:
            page.goto(REVIEW_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        return
    return _skip_current_record_via_dom(page, reason)


def _skip_current_record_via_dom(page, reason):
    """Abandon the WHOLE supplier currently on screen and advance to the next.

    DANGER - READ BEFORE CALLING.
    ---------------------------------------------------------------------
    On the Aekovera review UI, Skip (S) is a RECORD-level action. It lives in
    the bottom action bar next to Platform ready (G), Outreach first (O),
    Re-enrich (Y), Reject (R) and Undo (Z). There is NO per-field skip: an
    individual field is edited via its pencil icon and an inline form, and
    abandoning one field means simply not submitting that form.

    Calling this because a single FIELD failed advances the page to the next
    supplier, after which every remaining action in the loop - further
    corrections, and the final Platform ready / Reject - lands on the WRONG
    company. Only call this when the entire record is being abandoned.
    """
    candidates = [
        page.get_by_role("button", name=re.compile(r"^Skip\b", re.IGNORECASE)),
        page.locator("button").filter(has_text=re.compile(r"^\s*Skip\b", re.IGNORECASE)),
        page.locator("[aria-label*='Skip' i]"),
    ]

    for locator in candidates:
        try:
            if locator.count():
                locator.first.click(timeout=1500)
                page.wait_for_timeout(250)
                print(f"↷ Record skipped ({reason}) using UI Skip.")
                return True
        except Exception:
            continue

    # The UI explicitly documents S as Skip. Blur any input first so the
    # shortcut is handled by the review page rather than typed into a field.
    try:
        page.evaluate("document.activeElement && document.activeElement.blur()")
    except Exception:
        pass

    try:
        before_id = read_record_id(page)
        page.keyboard.press("s")
        if not wait_for_record_change(page, before_id) and before_id:
            page.keyboard.press("s")
            wait_for_record_change(page, before_id)
        print(f"↷ Record skipped ({reason}) using keyboard shortcut S.")
        return True
    except Exception as exc:
        print(f"↷ Could not trigger Skip ({reason}): {exc}")
        return False


QUALIFYING_SUPPLIER_TYPES = {
    "co-manufacturer",
    "co-packer",
    "private label manufacturer",
    "contract r&d / formulation",
    "ingredient supplier",
    "packaging supplier",
    "3pl / fulfillment",
    "food manufacturer / brand",
    "distributor / wholesaler",
}

# The review UI still OFFERS this category as a checkbox, but it no longer
# qualifies a company for ACCEPT: equipment makers are out of scope. It is
# listed here so the gate can give a clear, specific reason rather than the
# generic "no qualifying supplier category".
NON_QUALIFYING_SUPPLIER_TYPES = {
    "equipment / services",
    "equipment",
    "equipment manufacturer",
    "equipment / service",
}

# Curated, not a generic regex window: each phrase on its own is a strong,
# specific signal that the company's core business is equipment/machinery
# rather than ingredients, packaging materials, finished product, or
# distribution/fulfilment of those things. A loose "equipment near
# manufacturer/distributor" pattern would also flag ordinary packaging or
# ingredient suppliers who merely mention using their own equipment - this
# list intentionally does not.
EQUIPMENT_PHRASES = [
    "packaging equipment",
    "processing equipment",
    "food equipment",
    "beverage equipment",
    "foodservice equipment",
    "kitchen equipment",
    "food processing machinery",
    "packaging machinery",
    "food machinery",
    "beverage machinery",
    "industrial machinery",
    "equipment manufacturer",
    "equipment distributor",
    "equipment wholesaler",
    "equipment supplier",
    "equipment dealer",
    "equipment provider",
    "manufacturer of equipment",
    "distributor of equipment",
    "wholesaler of equipment",
    "supplier of equipment",
    "machinery manufacturer",
    "machinery distributor",
    "machinery wholesaler",
    "machinery supplier",
    "bottling line",
    "filling line",
    "filling machine",
    "capping machine",
    "labeling machine",
    "labelling machine",
    "conveyor system",
    "processing line",
    "canning line",
]


def find_equipment_phrase(text):
    """Return the first curated equipment phrase found in text, or None."""
    haystack = safe_text(text).lower()
    if not haystack:
        return None
    for phrase in EQUIPMENT_PHRASES:
        if phrase in haystack:
            return phrase
    return None


# Keyword -> canonical QUALIFYING_SUPPLIER_TYPES label. Order matters: more
# specific keywords are listed before generic ones so, e.g., "private label"
# is caught before the generic "manufactur" catch-all would claim it.
_SUPPLIER_TYPE_KEYWORDS = [
    ("3pl", "3pl / fulfillment"),
    ("third-party logistics", "3pl / fulfillment"),
    ("third party logistics", "3pl / fulfillment"),
    ("fulfillment", "3pl / fulfillment"),
    ("fulfilment", "3pl / fulfillment"),
    ("private label", "private label manufacturer"),
    ("white label", "private label manufacturer"),
    ("contract r&d", "contract r&d / formulation"),
    ("r&d", "contract r&d / formulation"),
    ("formulation", "contract r&d / formulation"),
    ("co-manufactur", "co-manufacturer"),
    ("co manufactur", "co-manufacturer"),
    ("contract manufactur", "co-manufacturer"),
    ("co-pack", "co-packer"),
    ("co pack", "co-packer"),
    ("copack", "co-packer"),
    # Labeling/relabeling of food or beverage product is a co-packing
    # service (applying labels as part of contract packing), not its own
    # category - map it onto Co-Packer. This is checked AFTER the equipment
    # guard below, so a company that manufactures/sells labeling MACHINES
    # (equipment) is never caught here by mistake.
    ("labeling", "co-packer"),
    ("labelling", "co-packer"),
    ("ingredient", "ingredient supplier"),
    ("packaging", "packaging supplier"),
    ("distributor", "distributor / wholesaler"),
    ("wholesaler", "distributor / wholesaler"),
    ("wholesale", "distributor / wholesaler"),
    ("brand", "food manufacturer / brand"),
    ("bakery", "food manufacturer / brand"),
    ("bottler", "food manufacturer / brand"),
    ("processor", "food manufacturer / brand"),
    ("producer", "food manufacturer / brand"),
    ("manufactur", "food manufacturer / brand"),
]


def normalize_supplier_type(text):
    """Map a ChatGPT-provided category string onto ONE of the canonical
    QUALIFYING_SUPPLIER_TYPES (or the "equipment / services" label), tolerating
    the punctuation/wording drift that comes out of a manual copy/paste
    workflow instead of requiring the literal review-UI string.

    Real cases that were auto-rejected with "no qualifying supplier category"
    despite scope_match=true and a reason that plainly called the company a
    manufacturer: Home Run Inn Frozen Foods (a frozen-pizza manufacturer) and
    Bisousweet Confections (an SQF-certified bakery). ChatGPT's copy/paste
    answer said something like "Food Manufacturer", "Manufacturer", or
    "Food Manufacturer/Brand" (no spaces around the slash) - never the exact
    literal "food manufacturer / brand" - so the old exact-string set
    intersection never matched, and a real, in-scope company was rejected
    purely on category-label phrasing, not on an actual scope problem.

    This only WIDENS what counts as a match to an already-qualifying
    category; it never invents scope where the text gives no signal at all.
    An unrecognized string is returned unchanged (lowercased, whitespace-
    normalized) so it still correctly fails the gate.
    """
    raw = safe_text(text).lower()
    # Punctuation-only drift ("Manufacturer/Brand" vs "Manufacturer / Brand",
    # doubled spaces, etc.) must never break an otherwise-exact match.
    collapsed = re.sub(r"\s*/\s*", " / ", raw)
    collapsed = re.sub(r"\s+", " ", collapsed).strip()
    if not collapsed:
        return collapsed
    if collapsed in QUALIFYING_SUPPLIER_TYPES or collapsed in NON_QUALIFYING_SUPPLIER_TYPES:
        return collapsed

    # Equipment must never fall through to a service/manufacturer keyword
    # below - checked first and returned as a non-qualifying label, not
    # remapped. Bare "machine" is included alongside "equipment"/"machinery"
    # so a "Labeling Machine Manufacturer" routes here rather than being
    # caught by the "labeling" -> co-packer keyword further down; a company
    # that makes labeling machines is an equipment business, not a co-packer
    # performing labeling as a contract service.
    if any(w in collapsed for w in ("equipment", "machinery", "machine")):
        return "equipment / services"

    for keyword, canonical in _SUPPLIER_TYPE_KEYWORDS:
        if keyword in collapsed:
            return canonical
    return collapsed


def normalize_country(value):
    """Map common spellings/abbreviations to a canonical country name."""
    text = safe_text(value)
    if not text:
        return ""
    key = re.sub(r"[^a-z]", "", text.lower())
    us_aliases = {
        "us", "usa", "unitedstates", "unitedstatesofamerica", "america",
        "usofa", "unitedstatesamerica",
    }
    if key in us_aliases:
        return "United States"
    aliases = {
        "uk": "United Kingdom", "unitedkingdom": "United Kingdom",
        "greatbritain": "United Kingdom", "england": "United Kingdom",
        "uae": "United Arab Emirates", "unitedarabemirates": "United Arab Emirates",
        "prc": "China", "china": "China", "india": "India",
        "canada": "Canada", "mexico": "Mexico", "germany": "Germany",
        "netherlands": "Netherlands", "holland": "Netherlands",
        "southkorea": "South Korea", "korea": "South Korea",
    }
    if key in aliases:
        return aliases[key]
    # Title-case whatever else was supplied.
    return " ".join(w.capitalize() for w in text.split())


def is_us_country(value):
    return normalize_country(value) == "United States"


# Scope keyword lists shared with the (unused-in-v34) description fallback
# helpers. They are kept because has_food_beverage_relevance() relies on them.
_SCOPE_POSITIVE_KEYWORDS = (
    "co-manufacturer", "co-manufactur", "co packer", "co-packer", "copacker",
    "contract manufacturer", "contract packer", "toll manufacturer",
    "tolling", "private label", "white label", "copacking", "co packing",
    "ingredient supplier", "ingredients supplier", "packaging supplier",
    "flavors", "flavour", "flavor", "seasoning", "spices", "blend",
    "extracts", "essences", "botanicals", "vitamins", "supplements",
    "nutraceutical", "functional", "probiotic", "enzyme", "protein",
)
_SCOPE_NEGATIVE_KEYWORDS = (
    "equipment", "machinery", "machine", "restaurant", "cafe", "bakery shop",
    "retail store", "grocery store", "supermarket", "distributor of finished",
    "cosmetics", "skincare", "personal care", "cleaning products",
    "household", "pet food", "pharmaceutical",
)


def has_food_beverage_relevance(result):
    """True when the research text itself shows a genuine food/beverage/
    supplement domain connection, independent of the final ACCEPT/REJECT
    verdict.

    Used to decide whether a REJECTed record's field corrections (phone,
    address, website, etc.) are still worth writing to the database, or
    whether the company is unrelated enough that saving them would just add
    irrelevant enrichment to a record that has nothing to do with the actual
    supplier domain (a software vendor, a cosmetics company, etc.). This
    reuses the same domain keyword lists rather than a separate list, so
    "in scope enough to keep the changes" stays consistent with "in scope
    enough to accept".
    """
    food_connection = safe_text(result.get("food_beverage_connection"))
    reason = safe_text(result.get("reason"))
    scope_text = safe_text(result.get("scope_match"))
    combined = f"{food_connection} {reason} {scope_text}".lower()
    if find_equipment_phrase(combined):
        return False
    if any(kw in combined for kw in _SCOPE_NEGATIVE_KEYWORDS):
        return False
    return bool(food_connection) or any(kw in combined for kw in _SCOPE_POSITIVE_KEYWORDS)


# ---------------------------------------------------------------------------
# Pre-apply safety filter (extracted verbatim from the main loop)
# ---------------------------------------------------------------------------


class FilteredChanges:
    """What may be applied, and what a human must resolve instead."""

    def __init__(self):
        self.allowed: list = []
        self.needs_clear: list = []
        self.needs_review: list = []


def filter_changes(record, changes, log=print):
    """Hard safety filter over the judge's proposed changes.

    Ported line-for-line from the apply-stage filter in the v34 main loop:
    only fields discover_fields() actually found on THIS record (or offered
    under ADD MISSING) may be edited; identity fields stay read-only except
    the narrow type-fill exception; null never clears; a clear requires a
    stated reason; an empty value with no clear flag is inert; the two email
    slots must differ; and non-English prose is held back, never written.
    """
    out = FilteredChanges()
    editable = record.get("editable_fields") or FIELDS_FALLBACK

    for change in changes:
        if not isinstance(change, dict):
            log("↷ Ignoring malformed change entry.")
            continue

        field = change.get("field")
        missing_match = addmissing.match_missing_field(
            field, record.get("missing_fields") or []
        )

        # NARROW EXCEPTION to the type/supplier_type blocklist.
        # type is normally read-only (a checkbox group with no reliable
        # write path). But when the record has NO type at all AND the
        # page offers to add it under "ADD MISSING", we DO let ChatGPT's
        # proposed type populate it - only in that exact situation. A
        # record that already has a type is never touched.
        field_norm = safe_text(field).lower()
        is_type_field = field_norm in {"type", "types", "supplier_type", "supplier_types"}
        type_currently_set = bool(record["fields"].get("supplier_type"))
        type_add_missing = addmissing.match_missing_field("type", record.get("missing_fields") or []) \
            or addmissing.match_missing_field("supplier_type", record.get("missing_fields") or [])
        allow_type_fill = (
            is_type_field and not type_currently_set and type_add_missing is not None
        )

        if field_norm in FIELD_BLOCKLIST and not allow_type_fill:
            log(f"↷ Ignoring read-only field: {field}")
            continue
        if allow_type_fill:
            # Route to the dedicated checkbox handler in the apply stage.
            out.allowed.append(dict(change, _type_fill=True, _type_entry=type_add_missing))
            continue
        if field not in editable and missing_match is None:
            log(f"↷ Ignoring non-editable field proposed by ChatGPT: {field}")
            continue

        # null means DO NOT EDIT. Never clear/delete a field automatically.
        if change.get("new_value") is None and not change.get("clear"):
            log(f"↷ Skipped {field}: proposed new_value is null")
            continue

        # EXPLICIT CLEAR: ChatGPT flagged this field's current value as
        # contamination (another company's data) and asked to remove it.
        # This is the ONLY way a field is ever emptied, and it requires a
        # stated reason - a bare clear flag is refused and the value is
        # left in place. An empty-string new_value with no clear flag is
        # NOT a clear (see below); it is ignored.
        if change.get("clear"):
            clear_reason = safe_text(change.get("clear_reason"))
            current_value = safe_text(record["fields"].get(field))
            if not clear_reason:
                log(
                    f"⚠ {field}: clear requested but no clear_reason given - refusing "
                    f"to empty a field without a stated reason. LEFT UNCHANGED."
                )
                out.needs_clear.append((field, current_value or "(already empty)"))
                continue
            if not current_value:
                log(f"↷ Skipped {field}: clear requested but field is already empty")
                continue
            # Passed the gate: queue a genuine clear.
            out.allowed.append(dict(change, _clear=True))
            continue

        # An EMPTY-but-not-null value with NO clear flag is inert: it is
        # NOT a delete instruction. Report it so a contaminated value is
        # visible to the reviewer, but never act on it.
        cleaned_proposal = clean_corrected_value(field, change.get("new_value"))
        if (
            cleaned_proposal is not None
            and safe_text(cleaned_proposal) != ""
            and normalize_for_verification(field, cleaned_proposal)
            == normalize_for_verification(field, safe_text(record["fields"].get(field)))
        ):
            log(f"↷ Skipped {field}: record already holds this value")
            continue
        if cleaned_proposal is None or safe_text(cleaned_proposal) == "":
            current_value = safe_text(record["fields"].get(field))
            if current_value:
                log(
                    f"⚠ {field}: empty value proposed with no clear flag - ignored. "
                    f"Current value ({current_value!r}) left as-is. If it is "
                    f"contaminated, ChatGPT must send clear=true with a reason."
                )
                out.needs_clear.append((field, current_value))
            else:
                log(f"↷ Skipped {field}: proposed empty value and field is already empty")
            continue

        # Never write the same address into both email slots - the UI
        # has exactly two, and duplicating one wastes the second.
        if field == "general_email":
            proposed = safe_text(clean_corrected_value(field, change.get("new_value"))).lower()
            current_primary = safe_text(record["fields"].get("primary_email")).lower()
            primary_change = next(
                (c for c in changes
                 if isinstance(c, dict) and c.get("field") == "primary_email"),
                None,
            )
            if primary_change is not None:
                current_primary = safe_text(
                    clean_corrected_value("primary_email", primary_change.get("new_value"))
                ).lower()
            if proposed and proposed == current_primary:
                log(
                    f"↷ Skipped general_email: same address as primary_email "
                    f"({proposed}); the two email slots must differ."
                )
                continue

        # LANGUAGE GUARD: this database is filtered/searched in
        # English. A value that is clearly not English (Cyrillic/CJK,
        # or a real concentration of non-English Latin diacritics - e.g.
        # Czech "ořechové máslo, arašídový krém") would sit in the
        # database invisibly to those filters even though it is
        # factually correct, exactly the failure mode that let a
        # Czech-language sub_categories value through untranslated.
        # Held back rather than guess-translated in code: the research
        # step is responsible for producing the English value, this is
        # only a safety net for when it doesn't.
        if field_norm in _LANGUAGE_CHECKED_FIELDS and looks_non_english(
            cleaned_proposal
        ):
            log(
                f"⚠ {field}: proposed value appears to be non-English "
                f"({cleaned_proposal!r}) - held back, not written. "
                "Needs an English value before this can be applied."
            )
            out.needs_review.append(
                (field, cleaned_proposal, "value appears non-English")
            )
            continue

        out.allowed.append(change)

    return out


# ---------------------------------------------------------------------------
# The four Auto Mode hold conditions (extracted verbatim from the main loop)
# ---------------------------------------------------------------------------


def evaluate_accept_holds(
    *,
    needs_clear,
    needs_review,
    identity_renamed,
    snapshot_problems,
    corrected_is_none,
    hold_accept_on_unresolved_fields=HOLD_ACCEPT_ON_UNRESOLVED_FIELDS,
    hold_on_identity_rename=HOLD_ON_IDENTITY_RENAME,
    hold_accept_if_edits_not_landed=HOLD_ACCEPT_IF_EDITS_NOT_LANDED,
    hold_accept_if_snapshot_failed=HOLD_ACCEPT_IF_SNAPSHOT_FAILED,
):
    """Reasons an ACCEPT must be held (skipped, left undecided) in Auto Mode.

    Ported verbatim from the v34 main loop's hold assembly: unresolved
    needs_clear/needs_review items, a company-identity rename, edits that did
    not land after the reload, or a failed final snapshot - each only when its
    config flag is on. Approval Mode is unaffected (a human already sees the
    same warnings before clicking anything themselves). Empty list = no hold.
    """
    unresolved = bool(needs_clear) or bool(needs_review)
    renamed_identity = hold_on_identity_rename and bool(identity_renamed)
    snapshot_hold = bool(snapshot_problems) and (
        (corrected_is_none and hold_accept_if_snapshot_failed)
        or (not corrected_is_none and hold_accept_if_edits_not_landed)
    )
    if not (
        (hold_accept_on_unresolved_fields and unresolved)
        or renamed_identity
        or snapshot_hold
    ):
        return []

    reasons = []
    if snapshot_problems:
        reasons.extend(snapshot_problems)
    if hold_accept_on_unresolved_fields and needs_clear:
        reasons.append(f"{len(needs_clear)} field(s) left uncleared")
    if hold_accept_on_unresolved_fields and needs_review:
        reasons.append(f"{len(needs_review)} field(s) flagged for review")
    if renamed_identity:
        reasons.append(f"{len(identity_renamed)} company-identity field(s) renamed")
    return reasons


# ---------------------------------------------------------------------------
# Final snapshot: reload -> extract -> compare (v32.1)
# ---------------------------------------------------------------------------


class ReloadDriftError(RuntimeError):
    """The reload served a different card than the one that was corrected."""


def _landed_key(field, value):
    """Comparison key for 'did this saved value really land on the record'."""
    field = safe_text(field)
    if field in {"supplier_type", "type", "types", "supplier_types"}:
        parts = re.split(r"\s*[|,;]\s*", safe_text(value).lower())
        return "|".join(sorted(p for p in parts if p))
    if field == "primary_phone":
        digits = re.sub(r"\D", "", safe_text(value))
        return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    return normalize_for_verification(field, value)


def find_unlanded_changes(fields, changes, applied=(), cleared=()):
    """Every correction we saved that the reloaded page does NOT show.

    Returns [(field, expected_value, value_on_page)]. Pure function so it can
    be tested without a browser.
    """
    fields = fields or {}
    applied_set = {safe_text(f) for f in applied or ()}
    type_keys = {"supplier_type", "type", "types", "supplier_types"}
    problems = []
    for change in changes or []:
        if not isinstance(change, dict) or change.get("clear") or change.get("_clear"):
            continue
        field = safe_text(change.get("field"))
        if field in type_keys:
            field = next((k for k in ("supplier_type", "type") if k in applied_set), field)
        if field not in applied_set:
            continue
        expected = clean_corrected_value(field, change.get("new_value"))
        if isinstance(change.get("_written_value"), str):
            expected = change["_written_value"]
        page_key = field
        if field not in fields:
            page_key = next((k for k in type_keys if k in fields), None) if field in type_keys else None
        if page_key is None:
            problems.append((field, expected, "(field not shown on the reloaded page)"))
            continue
        on_page = safe_text(fields.get(page_key))
        if _landed_key(field, on_page) != _landed_key(field, expected):
            problems.append((field, expected, on_page))
    for item in cleared or ():
        field = safe_text(item[0])
        on_page = safe_text(fields.get(field))
        if on_page:
            problems.append((field, "", on_page))
    return problems


def fetch_corrected_record(page, expected_id="", *, result=None, applied=(), cleared=(),
                           reload_before_snapshot=RELOAD_BEFORE_FINAL_SNAPSHOT):
    """RELOAD the review page, then read back every field (v32.1).

    Why the reload is mandatory
    ---------------------------
    Since v13 every correction is saved with a direct HTTP POST (qa_http), not
    through the browser. The tab never navigates, so its hidden field inputs
    still carry the values rendered BEFORE the corrections. The old version of
    this function waited for the (already loaded) page and read those stale
    inputs, which is why accepted_companies.xlsx held the original data and
    every row said "no changes needed".

    Now:
      1. note the unit_id of the card being corrected;
      2. reload the page, so the server renders the saved values;
      3. refuse to continue if the reload shows a different card
         (raises ReloadDriftError - no verdict may be pressed);
      4. read every field from the fresh page;
      5. compare each saved correction with what the page shows.

    Returns {"fields", "record_id", "unit_id", "not_landed"} or None when the
    page could not be read. Raises ReloadDriftError on a card mismatch.
    """
    try:
        before_unit, _ = read_card_identity(page)
    except Exception:
        before_unit = ""

    if reload_before_snapshot:
        print("\n\u21bb Reloading the review page so the saved corrections are shown...")
        try:
            page.reload(wait_until="domcontentloaded", timeout=max(15000, POST_SETTLE_TIMEOUT_MS * 3))
        except Exception as exc:
            print(f"\u2717 Reload failed ({exc}); the final record cannot be read back.")
            return None

    wait_for_page_settle(page)

    try:
        after_unit, _ = read_card_identity(page)
    except Exception:
        after_unit = ""
    record_id = read_record_id(page)

    if (before_unit and after_unit and after_unit != before_unit) or (
            expected_id and record_id and record_id != expected_id):
        raise ReloadDriftError(
            f"after the reload the page shows unit {after_unit or '?'} / {record_id or '?'}, "
            f"but the corrections were made on unit {before_unit or '?'} / {expected_id or '?'}"
        )
    if before_unit and not after_unit:
        print("\u2717 The reloaded page has no card identity; cannot confirm it is the same company.")
        return None

    try:
        discovered = discover_fields(page)
        known = {e["field"] for e in discovered}
        fields = read_field_values(page, known)
    except Exception as exc:
        print(f"\u2717 Could not read the reloaded record ({exc}).")
        return None
    if not fields:
        print("\u2717 The reloaded record came back empty.")
        return None

    not_landed = find_unlanded_changes(
        fields, (result or {}).get("changes") or [], applied, cleared)

    print(f"\u2713 Final record re-read from the reloaded page ({len(fields)} fields).")
    if not_landed:
        print(f"\u26a0 {len(not_landed)} saved correction(s) are NOT on the reloaded record:")
        for field, expected, on_page in not_landed:
            print(f"   - {field}: saved {expected!r}, page shows {on_page!r}")
    elif applied or cleared:
        print(f"\u2713 All {len(applied or ()) + len(cleared or ())} correction(s) confirmed on the reloaded record.")
    return {"fields": fields, "record_id": record_id or expected_id,
            "unit_id": after_unit or before_unit, "not_landed": not_landed}


def read_record_id_hint(record):
    """Best-effort MASTER id for a log/artifact row, without a live page."""
    for value in (record.get("fields") or {}).values():
        text = safe_text(value)
        if MASTER_ID_RE.search(text):
            return MASTER_ID_RE.search(text).group(0)
    match = MASTER_ID_RE.search(record.get("page_context") or "")
    return match.group(0) if match else ""


def supply_origin_row(result, record=None):
    """Row for an accepted non-US company's supply origin.

    The review UI has no dedicated origin field, so this is the durable
    record of where an accepted foreign supplier actually supplies from.
    (The v34 archive wrote it to a CSV; the runner persists it as an
    artifact through the store interface instead.)
    """
    return {
        "company_name": safe_text(result.get("company_name")),
        "supply_country": safe_text(result.get("supply_country")),
        "supply_origin_note": safe_text(result.get("supply_origin_note")),
        "supplier_types": ", ".join(
            safe_text(x) for x in (result.get("qualifying_supplier_types") or [])),
        "record_id": (safe_text((record or {}).get("record_id"))
                      or read_record_id_hint(record or {})),
    }


def apply_origin_note_to_ui(page, result):
    """Best-effort: write the origin note into the review UI's note field.

    The reject flow uses input[name='note']. If the same input is present on
    the accept flow, the origin note goes there too so the note travels with
    the record. If it is absent, the persisted origin artifact is the record
    of truth.
    """
    if result.get("decision") != "ACCEPT" or result.get("is_us_based") is not False:
        return

    note = safe_text(result.get("supply_origin_note"))
    if not note:
        return

    try:
        note_input = page.locator("input[name='note']")
        if note_input.count():
            note_input.first.fill(note)
            print(f"✓ Origin note written to the UI note field: {note}")
        else:
            print("↷ No note field in the accept flow; origin kept in the artifact log only.")
    except Exception as exc:
        print(f"↷ Could not write the origin note to the UI: {exc}")


def manual_review_row(result, record):
    """Row describing a record routed to a human reviewer."""
    return {
        "record_id": safe_text(record.get("record_id")) or read_record_id_hint(record),
        "company_name": safe_text(result.get("company_name")),
        "manual_review_reason": safe_text(result.get("manual_review_reason")),
        "food_beverage_connection": safe_text(result.get("food_beverage_connection")),
        "qualifying_supplier_types": ", ".join(
            safe_text(x) for x in (result.get("qualifying_supplier_types") or [])),
    }


def discovery_failure_row(page, detail):
    """Row for a record skipped because field discovery never recovered."""
    return {
        "record_id": read_record_id(page) or "(unknown - discovery failed before the id could be read)",
        "detail": detail,
    }


def website_verify_row(result, url, verdict, detail):
    """Row for one independent website sanity check, whatever the outcome."""
    verdict_text = {True: "match", False: "MISMATCH", None: "inconclusive"}[verdict]
    return {
        "company_name": safe_text(result.get("company_name")),
        "proposed_url": url,
        "verdict": verdict_text,
        "detail": detail,
    }


def field_hold_row(result, record, needs_clear, needs_review, identity_renamed=None):
    """Row for an ACCEPT that Auto Mode held back from Platform ready.

    A field was flagged - possibly contaminated, a website the independent
    check found no trace of, a proposed value that appears to be non-English,
    or a company-identity field that was renamed this run - but never
    actually resolved/confirmed. Without this durable record, a held record's
    only trace was a console line in an unattended Auto Mode run.
    """
    needs_clear = needs_clear or []
    needs_review = needs_review or []
    identity_renamed = identity_renamed or []
    return {
        "record_id": safe_text(record.get("record_id")) or read_record_id_hint(record),
        "company_name": safe_text(result.get("company_name")),
        "fields_left_uncleared": "; ".join(f"{f}={v!r}" for f, v in needs_clear),
        "fields_held_by_website_check": "; ".join(f"{f}={v!r} ({d})" for f, v, d in needs_review),
        "identity_field_renamed": "; ".join(f"{f}: {o!r} -> {n!r}" for f, o, n in identity_renamed),
    }


def flag_for_manual_review(page, result, transport):
    """Route a genuinely uncertain record to a human reviewer.

    This never clicks Platform ready or Reject - it best-effort writes the
    reason into the UI's note field (the same input[name='note'] the reject
    flow and the non-US origin note use; if this particular action's flow
    does not expose one, nothing is lost - the manual-review artifact
    already carries the reason), then leaves the record via the existing,
    hardened Skip path rather than reimplementing a raw keyboard shortcut
    here. skip_current_record() already tries the UI Skip button first and
    only falls back to pressing "s" after blurring any focused input, which
    matters: pressing a bare "S" into a field that still has focus types the
    letter instead of triggering the shortcut.
    """
    reason = safe_text(result.get("manual_review_reason")) or safe_text(result.get("reason"))

    try:
        note_input = page.locator("input[name='note']")
        if note_input.count():
            note_input.first.fill(("MANUAL REVIEW: " + reason)[:500])
            print("✓ Manual-review reason written to the UI note field.")
        else:
            print("↷ No note field visible here; reason is kept in the artifact log only.")
    except Exception as exc:
        print(f"↷ Could not write the manual-review note to the UI: {exc}")

    print(f"⚑ Flagged for manual review: {reason or '(no reason given)'}")
    skip_current_record(page, transport, "manual review")
