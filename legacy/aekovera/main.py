import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import accepted_snapshots
import addmissing
import evidence as evidence_mod
import history
import llm
from jsonutil import extract_first_json_value
import decision_v4
import qa_http
from qa_http import QAClient, QAHttpError, QASessionExpired, read_card_identity

from config import (
    BASE_URL,
    REVIEW_URL,
    PROFILE_DIR,
    MAX_RECORDS,
    RESEARCH_BACKEND,
    CONTINUE_ON_RESEARCH_FAILURE,
    ACCEPT_NON_US,
    NON_US_LOG,
    ENABLE_MANUAL_REVIEW,
    MANUAL_REVIEW_LOG,
    CHATGPT_PROJECT_MODE,
    PROJECT_INSTRUCTIONS_FILE,
    CLIPBOARD_AUTO_WATCH,
    CLIPBOARD_POLL_INTERVAL,
    CLIPBOARD_WATCH_TIMEOUT,
    PAGE_CONTEXT_CHARS,
    PAGE_CONTEXT_SELECTOR,
    OPENROUTER_MODELS,
    FIELD_DISCOVERY_MAX_ATTEMPTS,
    FIELD_DISCOVERY_RETRY_DELAY_MS,
    FIELD_DISCOVERY_FAILURE_LOG,
    VERIFY_WEBSITE_BEFORE_APPLY,
    WEBSITE_VERIFY_LOG,
    HOLD_ACCEPT_ON_UNRESOLVED_FIELDS,
    FIELD_HOLD_LOG,
    HOLD_ON_IDENTITY_RENAME,
    POST_SETTLE_TIMEOUT_MS,
    FIELD_PRESENT_WAIT_MS,
    ADVANCE_VERIFY_TIMEOUT_MS,
    FINAL_ACTION_MAX_ATTEMPTS,
    REUSE_RESULT_ON_REPEAT,
    MAX_REPEAT_PASSES,
    ENABLE_HISTORY_EXCEL,
    HISTORY_EXCEL_FILE,
    ENABLE_ACCEPTED_SNAPSHOT,
    ACCEPTED_SNAPSHOT_FILE,
    RELOAD_BEFORE_FINAL_SNAPSHOT,
    HOLD_ACCEPT_IF_EDITS_NOT_LANDED,
    HOLD_ACCEPT_IF_SNAPSHOT_FAILED,
)

# pyperclip is only needed for the legacy manual/ChatGPT fallback backend.
try:
    import pyperclip
except ImportError:  # pragma: no cover
    pyperclip = None

if sys.platform == "win32":
    import ctypes


# Set once in main() from the Playwright context. When present, edits and
# verdicts go over HTTP instead of through the DOM (see qa_http.py).
QA = None


def use_http():
    """True when the fast HTTP transport is available for this run."""
    return QA is not None


def _release_stuck_clipboard():
    """Self-heal a known pyperclip Windows issue.

    pyperclip's Windows paste() does OpenClipboard() -> GetClipboardData() ->
    CloseClipboard(), with no try/finally around the middle call. If
    GetClipboardData() ever raises (e.g. the clipboard holds a format other
    than plain CF_UNICODETEXT - such as the rich/HTML clipboard payload some
    browsers write alongside plain text on copy), CloseClipboard() is never
    reached and our process is left holding the Windows clipboard open.
    From that point on, EVERY OpenClipboard() call system-wide fails
    silently - ours and the user's normal Ctrl+C/Ctrl+V - until the lock is
    released, which looks exactly like "the clipboard just stopped working"
    partway through a long run. Force-closing before every clipboard touch
    is harmless when nothing is stuck (CloseClipboard() just no-ops) and
    clears the lock the moment it happens.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.CloseClipboard()
    except Exception:
        pass


FIELDS = [
    # Fallback core set, used only if runtime field discovery fails.
    # The authoritative list per record now comes from discover_fields(),
    # which reads the page's own hidden `field` inputs.
    "primary_email",
    "general_email",
    "primary_phone",
    "website_url",
    "city",
    "state",
    "zip",
    "country",
]

# Never editable by the automation, whatever the page offers.
#   type/supplier_type - rendered as a checkbox group (new_value_multi).
#     Writing it means toggling ten checkboxes with no reliable read-back,
#     so it stays READ-ONLY by explicit request. It is still extracted and
#     shown to the researcher as context.
#   master/id - identity anchors used for drift detection; editing them
#     would break the safety check before the final verdict.
FIELD_BLOCKLIST = {
    "type", "types", "supplier_type", "supplier_types",
    "master", "master_id", "id", "record_id",
}

FIELD_LABELS = {
    "primary_email": "Email",
    "general_email": "Email 2",
    "primary_phone": "Phone",
    "website_url": "Website",
    "city": "City",
    "state": "State",
    "zip": "ZIP",
    "country": "Country",
}

# Whatever key the live DOM actually uses for the company's name/DBA/legal
# name (discover_fields() reads it dynamically, so this is a set of plausible
# candidates, not the one true key). Not in FIELD_BLOCKLIST - a company-name
# correction is legitimate under the "company name outlier" rule in the
# prompt - but renaming the record's own anchor identity is categorically
# higher-stakes than fixing a phone number, so any change here is tracked
# separately and always held for a human's confirmation before Auto Mode
# finalizes the record. See HOLD_ON_IDENTITY_RENAME in config.py.
IDENTITY_FIELD_KEYS = {"company_name", "name", "legal_name", "business_name", "company"}

# Legal-form / punctuation noise that does NOT count as a real identity change.
# "Giraffe Foods" → "Giraffe Foods Inc." or "LiDestri Foods" → "LiDestri Foods, Inc."
# should NOT trigger a hold. Only a change that alters the core name tokens does.
_IDENTITY_NOISE_TOKENS = {
    "inc", "incorporated", "llc", "l.l.c", "ltd", "limited", "co", "corp",
    "corporation", "company", "companies", "group", "holdings", "the", "and",
    "of", "dba", "d/b/a", "usa", "us",
}


def _significant_name_tokens(text):
    """Tokens that actually identify the company, ignoring legal-form noise."""
    tokens = re.split(r"[^a-z0-9]+", safe_text(text).lower())
    return {t for t in tokens if len(t) >= 2 and t not in _IDENTITY_NOISE_TOKENS}


def is_substantial_identity_change(old_value, new_value):
    """True only when the core name tokens actually change.

    Minor cleanups (add/remove Inc./LLC, capitalization, punctuation, extra
    spaces) return False so Auto Mode can still finalize the record.
    """
    old_toks = _significant_name_tokens(old_value)
    new_toks = _significant_name_tokens(new_value)
    if not old_toks and not new_toks:
        return False
    # Substantial if the sets differ by more than pure noise.
    return old_toks != new_toks


# Free-text fields we want enriched rather than merely corrected.
ENRICHABLE_FIELDS = {"products", "specialty", "certs", "certifications", "dba", "address"}


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
    config.py and the try/except around extract_record() in main().
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


def choose_mode():
    print("\n==========================================")
    print("        AEKOVERA REVIEW AGENT")
    print("==========================================")
    print("1. Approval Mode")
    print("2. Auto Mode")
    print("3. Exit")
    print("==========================================")

    while True:
        value = input("Select mode: ").strip()
        if value == "1":
            return "approval"
        if value == "2":
            return "auto"
        if value == "3":
            return None
        print("Enter 1, 2, or 3.")


def choose_run_count(default=MAX_RECORDS):
    """Choose how many supplier records to process in this session.

    This replaces the hard-coded pilot count while keeping the configured
    default as a convenient safe starting point.
    """
    print("\n==========================================")
    print("        NUMBER OF PILOT RUNS")
    print("==========================================")
    print(f"Default: {default} records")
    print("Examples: 1, 5, 10, 25")
    print("Enter 0 to cancel.")
    print("==========================================")

    while True:
        value = input(f"How many records should be processed? [{default}]: ").strip()
        if value == "":
            return default
        try:
            count = int(value)
        except ValueError:
            print("Please enter a whole number.")
            continue

        if count == 0:
            return None
        if count < 0:
            print("Enter a positive number or 0 to cancel.")
            continue
        if count > 10000:
            print("For safety, choose 10,000 records or fewer per session.")
            continue
        return count


def safe_text(value):
    if value is None:
        return ""
    return str(value).strip()


_TRUE_STRINGS = {"true", "yes", "y", "1"}
_FALSE_STRINGS = {"false", "no", "n", "0"}

# Leading words a free-text verdict commonly opens with when the model
# writes a qualitative judgement instead of a strict boolean - e.g.
# "Strong - US-based food manufacturer/brand producing packaged hot sauce
# products." for scope_match (a real case: it got auto-rejected purely for
# this phrasing, not for an actual scope problem). Checked only against the
# FIRST word, so a sentence that merely mentions "yes" or "strong" further
# in isn't misread.
_TRUE_LEAD_WORDS = {
    "true", "yes", "y", "1", "strong", "strongly", "confirmed", "clear",
    "clearly", "definite", "definitely", "correct",
}
_FALSE_LEAD_WORDS = {"false", "no", "n", "0", "weak", "incorrect"}
# If any of these appear anywhere in the text, read it as negative
# regardless of the leading word - "Strong claim, but does not match" must
# not be read as affirmative just because it opens with "Strong".
_NEGATION_PHRASES = (
    "not a match", "not match", "does not qualify", "doesn't qualify",
    "not qualify", "not qualifying", "not eligible", "not accepted",
    "not confirmed", "not in scope", "no match", "not verified",
)


def to_bool(value):
    """Tolerantly coerce a model's answer to a Python bool, or None if unclear.

    The schema asks for a JSON boolean (`true`/`false`), but ChatGPT's manual
    copy/paste path sometimes answers with a qualitative verdict instead of a
    strict literal - "Yes"/"No" (see scope_match in llm_logs), or even a full
    descriptive sentence. A strict exact-match check reads anything but the
    literal words "true"/"yes" as unrecognized -> "not confirmed" -> the
    scope gate force-rejects the record. That is a false rejection caused by
    phrasing, not a real scope problem, and it is exactly the failure mode
    this function exists to catch.

    This only widens what counts as an affirmative/negative answer; it never
    invents one. A negation phrase anywhere in the text always wins over a
    leading affirmative word. Anything still unrecognized returns None and
    is treated as "not confirmed" by the caller, same as before.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = safe_text(value).strip().lower()
    if not text:
        return None
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False

    if any(phrase in text for phrase in _NEGATION_PHRASES):
        return False

    lead_word = re.split(r"[^a-z]+", text, maxsplit=1)[0]
    if lead_word in _TRUE_LEAD_WORDS:
        return True
    if lead_word in _FALSE_LEAD_WORDS:
        return False

    return None


# scope_match-specific fallback for when to_bool() still returns None. Real
# cases (all decision=ACCEPT, all genuinely in-scope): "US-based food
# manufacturer producing bakery and frozen breakfast/snack products",
# "Seafood harvester, wholesaler, and retailer supplying fresh stone crabs
# and seafood products", "Wholesale and retail seafood supplier offering
# fresh and frozen seafood products and related food items." None of these
# open with a yes/strong/confirmed-style lead word - to_bool()'s leading-word
# check requires the sentence to OPEN with one, and these open with "us-",
# "seafood", "wholesale" instead. The whole sentence IS the verdict; there is
# no separate signal word anywhere in it. This is the same false-rejection
# failure mode to_bool already documents, one level further along: the
# ChatGPT project behind this consistently answers scope_match with a plain
# business description rather than true/false OR a "Strong -"/"Yes -" style
# verdict, for every company regardless of type.
_SCOPE_POSITIVE_KEYWORDS = (
    "food", "beverage", "bakery", "baked good", "seafood", "snack",
    "ingredient", "supplement", "nutrition", "nutritional", "vitamin",
    "protein", "dietary", "manufactur", "co-pack", "copack", "co pack",
    "private label", "formulation", "packaging", "distributor",
    "wholesaler", "wholesale", "3pl", "fulfillment", "fulfilment",
    "confection", "culinary", "edible", "crab", "shrimp", "lobster",
    "fish", "meal replacement", "sports nutrition", "functional nutrition",
)
_SCOPE_NEGATIVE_KEYWORDS = (
    "cosmetic", "skincare", "skin care", "personal care", "home care",
    "pharmaceutical", "prescription drug", "otc drug", "medical device",
    "software", "apparel", "electronics", "automotive", "not a food",
    "not food", "unrelated to food",
)


def infer_scope_match_from_description(text):
    """Read an implicit true/false out of a scope_match value that to_bool()
    could not resolve because it is a plain description with no leading
    affirmative/negative word at all - see the block comment above.

    Only returns a verdict when the description gives an UNAMBIGUOUS
    domain signal in exactly one direction (positive keywords present,
    negative absent, or vice versa). Anything mixed or silent on domain
    returns None and is left to fail safe, same as before - this only
    widens recognition of an already-unambiguous description; it never
    invents scope for a genuinely unclear one.
    """
    haystack = safe_text(text).lower()
    if not haystack:
        return None
    has_negative = any(kw in haystack for kw in _SCOPE_NEGATIVE_KEYWORDS)
    has_positive = any(kw in haystack for kw in _SCOPE_POSITIVE_KEYWORDS)
    if has_positive and not has_negative:
        return True
    if has_negative and not has_positive:
        return False
    return None


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
        text = re.sub(r"^mailto:", "", text, flags=re.I).strip()

    text = text.strip()

    if field == "primary_phone" and text:
        # The UI stores bare digits (e.g. 2073734513). Strip tel: prefixes and
        # human formatting so the written value matches what the app expects,
        # while preserving a leading + for international numbers. Anything that
        # does not look like a single phone number is left untouched rather
        # than mangled - better a visible odd value than a silent corruption.
        candidate = re.sub(r"^tel:", "", text, flags=re.I).strip()
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
    re.I,
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
_ON_SHIFT_RE = re.compile(r"^on shift since\s+\d", re.I)


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


SYSTEM_PROMPT = (
    "You are a strict data-verification agent for a US food & beverage CPG supply-chain "
    "database. You cannot browse the web; you judge only the evidence supplied in the "
    "prompt. You never invent facts, never guess contact details, and never assume a "
    "company is in scope. When the evidence is insufficient, follow the rulebook: "
    "PARK or RE_ENRICH a real in-scope company, REJECT only out-of-scope or no-trace entries. "
    "You reply with a single valid JSON object and nothing else."
)


def build_rules_block(browsing=False):
    """The v4 judge rulebook (prompts/judge_v4.md).

    v13: the whole rules + schema block is now the founder-aligned v4 rulebook,
    kept in one editable file rather than embedded here, so it can be changed
    without touching code. Its section 10 already defines the output schema,
    which is why build_schema_block() is now only a strict-JSON reminder.
    """
    return decision_v4.load_prompt(browsing=browsing)


def build_record_payload(record, evidence_text="", compact=False):
    """The per-record part of the prompt - the only thing that changes.

    When compact=True (Project mode) the payload is deliberately dense:
    single-line JSON for fields, truncated page context, minimal links,
    and a hard character target so ChatGPT chats stay usable longer.
    """
    evidence_block = ""
    if evidence_text:
        evidence_block = "\n" + evidence_text.strip() + "\n"

    editable = record.get("editable_fields") or FIELDS
    readonly = record.get("readonly_fields") or []
    readonly_block = ""
    if readonly:
        readonly_block = (
            "READ-ONLY (context only, never change): " + ", ".join(readonly) + "\n"
        )

    # Dense single-line field dump for compact mode (saves hundreds of chars).
    if compact:
        fields_json = json.dumps(record["fields"], ensure_ascii=False, separators=(",", ":"))
        # Cap page context hard so the whole paste stays near the 600-800
        # char target that keeps Project chats healthy longer.
        page_ctx = safe_text(record.get("page_context"))[:700]
        missing = addmissing.render_for_prompt(record.get("missing_fields") or [])
        if missing:
            missing = missing.strip() + "\n"
        # Links are omitted in compact mode: the website field + page context
        # already give the model what it needs, and dropping them saves ~200-400
        # chars that would otherwise push every paste over the healthy limit.
        return (
            f"CURRENT RECORD (UNTRUSTED):\n{fields_json}\n\n"
            f"EDITABLE: {', '.join(editable)}\n"
            f"{readonly_block}"
            f"PAGE CONTEXT (UNTRUSTED):\n{page_ctx}\n\n"
            f"{missing}"
            f"{evidence_block}"
        )

    # Full (non-compact) version kept for the non-Project path.
    return f"""CURRENT RECORD (UNTRUSTED):
{json.dumps(record["fields"], indent=2, ensure_ascii=False)}

EDITABLE FIELDS (you may propose changes ONLY to these keys):
{", ".join(editable)}

{readonly_block}LINKS GENERATED BY THE CURRENT RECORD (ALSO UNTRUSTED):
{json.dumps(record["links"], indent=2, ensure_ascii=False)}

PAGE CONTEXT (UNTRUSTED):
{record["page_context"]}

{addmissing.render_for_prompt(record.get("missing_fields") or [])}
{evidence_block}"""


def build_schema_block():
    """Closing reminder. The schema itself lives in section 10 of the rulebook."""
    return (
        "\n\nReturn exactly one JSON object matching section 10. "
        "No prose, no markdown fences. decision must be exactly one of "
        "ACCEPT, PARK, RE_ENRICH, REJECT.\n"
    )


def build_research_prompt(record, evidence_text="", compact=False, browsing=False):
    """Assemble the full prompt.

    compact=True omits the static rules and schema, for use with a ChatGPT
    Project that already carries them in its custom instructions. The per-record
    payload is deliberately kept under ~800 characters whenever possible so
    that a single Project chat stays usable for more records before drift.
    """
    payload = build_record_payload(record, evidence_text, compact=compact)
    if compact:
        # Hard reset line is critical: ChatGPT Projects slowly dilute
        # instructions after many turns. Starting every paste with an explicit
        # "this is a brand-new independent record" keeps the model honest longer.
        hard_reset = (
            "NEW RECORD. Ignore all previous companies and all prior answers. "
            "Follow the Project custom instructions exactly. "
            "Search the web for this company and open its website and contact page "
            "BEFORE deciding; nothing has been fetched for you. "
            "Output ONLY valid JSON matching the schema. No markdown, no prose.\n\n"
        )
        return hard_reset + payload
    return build_rules_block(browsing=browsing) + "\n" + payload + "\n" + build_schema_block()


def research_via_api(page, record):
    """Fully automated research: collect evidence, then call OpenRouter.

    Returns the parsed JSON dict, or None if research failed.
    """
    print("\nCollecting web evidence...")
    try:
        bundle = evidence_mod.collect(page.context, record)
    except Exception as exc:
        print(f"  ! Evidence collection failed: {exc}")
        bundle = {"enabled": True, "sources": [], "usable_count": 0}

    prompt = build_research_prompt(record, evidence_mod.render(bundle))
    Path("last_research_request.txt").write_text(prompt, encoding="utf-8")

    print("Querying OpenRouter...")
    try:
        return llm.research(prompt, SYSTEM_PROMPT)
    except llm.QuotaExhausted as exc:
        print(f"\n✗ OpenRouter quota exhausted:\n{exc}")
        return None
    except llm.LLMError as exc:
        print(f"\n✗ Research failed: {exc}")
        return None


def write_project_instructions():
    """Write the standing rules for pasting into a ChatGPT Project ONCE.

    With these in the project's custom instructions, each record only needs
    its own small payload pasted, instead of the full rulebook every time.
    """
    text = (
        build_rules_block(browsing=True)
        + "\n"
        + build_schema_block()
        + "\n\n"
        "=== HOW YOU WILL RECEIVE RECORDS ===\n"
        "You will be given supplier records one at a time.\n"
        "Every message that begins with \"NEW RECORD\" is a completely independent\n"
        "company. Ignore all previous companies, previous answers, and any\n"
        "conversation history. Treat each NEW RECORD as if it is the first and\n"
        "only record you have ever seen.\n"
        "Do the web research required by the rules above and reply with ONLY the\n"
        "complete JSON object described in the schema. No markdown fences, no\n"
        "prose, no partial objects.\n"
    )
    path = Path(PROJECT_INSTRUCTIONS_FILE)
    path.write_text(text, encoding="utf-8")
    return path


def copy_research_request(record, compact=False):
    """Legacy manual backend: hand the prompt to ChatGPT via the clipboard."""
    # browsing=True: ChatGPT does the research itself, so the rulebook must not
    # tell it that it has no browsing (see decision_v4.load_prompt).
    prompt = build_research_prompt(record, compact=compact, browsing=True)
    char_count = len(prompt)
    if pyperclip is not None:
        _release_stuck_clipboard()
        pyperclip.copy(prompt)
        print(f"\nResearch request copied to clipboard ({char_count:,} chars).")
    else:
        print("\npyperclip is not installed; use the saved file below.")
    Path("last_research_request.txt").write_text(prompt, encoding="utf-8")
    print("It is also saved as: last_research_request.txt")
    if compact:
        print("Paste it into your Aekovera ChatGPT Project (rules already loaded there).")
        if char_count > 1100:
            print(f"⚠ Payload is {char_count} chars (target ≤900). Consider a fresh chat sooner.")
        else:
            print(f"✓ Compact payload ({char_count} chars) — good for longer Project chats.")
    else:
        print("\nPaste it into ChatGPT, complete the research, and ask for ONLY the JSON output.")
    print("Then copy the JSON response to your clipboard.")


def wait_for_clipboard_json(timeout_s=CLIPBOARD_WATCH_TIMEOUT):
    """Watch the clipboard and return as soon as valid JSON appears.

    This removes a keystroke per record: copy ChatGPT's answer and the agent
    picks it up immediately, instead of waiting for you to alt-tab and press
    ENTER. Press Ctrl+C to fall back to the manual prompt.
    """
    if pyperclip is None:
        return None

    print("\nWatching the clipboard - just copy ChatGPT's JSON answer.")
    print("(Ctrl+C to enter it manually instead.)")

    deadline = time.time() + timeout_s

    # The clipboard can be transiently locked by another process (clipboard
    # managers, DLP/security tools, sync utilities) right after we just wrote
    # the prompt to it. Retry the initial read the same way the polling loop
    # below retries later reads, instead of giving up after one failed call.
    baseline = None
    while time.time() < deadline:
        try:
            _release_stuck_clipboard()
            baseline = pyperclip.paste()
            break
        except Exception:
            time.sleep(CLIPBOARD_POLL_INTERVAL)
    else:
        print("Could not read the clipboard - falling back to manual paste.")
        return None
    try:
        while time.time() < deadline:
            try:
                _release_stuck_clipboard()
                current = pyperclip.paste()
            except Exception:
                time.sleep(CLIPBOARD_POLL_INTERVAL)
                continue

            if current and current != baseline:
                try:
                    value = extract_first_json_value(current)
                    if isinstance(value, dict) and "decision" in value:
                        print("✓ JSON detected on the clipboard.")
                        return value
                    # Changed but not our JSON yet: re-baseline and keep waiting.
                    baseline = current
                except Exception:
                    baseline = current
            time.sleep(CLIPBOARD_POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nClipboard watch cancelled.")
        return None

    print("Clipboard watch timed out.")
    return None


def get_research_result(page, record, backend, compact=False):
    if backend == "api":
        return research_via_api(page, record)

    copy_research_request(record, compact=compact)

    if CLIPBOARD_AUTO_WATCH:
        result = wait_for_clipboard_json()
        if result is not None:
            return result
        print("Falling back to manual paste confirmation.")

    return read_json_from_clipboard()


def _extract_first_json_value(raw):
    """Backwards-compatible wrapper around the shared tolerant JSON parser."""
    return extract_first_json_value(raw)


def read_json_from_clipboard(max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        print("\nPaste the ChatGPT JSON into the clipboard, then press ENTER.")
        if attempt > 1:
            print(f"Retry {attempt}/{max_attempts}: copy the JSON response again.")
        input("Press ENTER when the JSON is copied...")

        if pyperclip is None:
            print("pyperclip is not installed; cannot read the clipboard.")
            return None
        _release_stuck_clipboard()
        raw = pyperclip.paste()
        try:
            return _extract_first_json_value(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print("\nCould not parse the clipboard as JSON.")
            print(f"JSON error: {exc}")
            if attempt < max_attempts:
                print("No correction has been applied. Please copy the JSON response again.")
                continue
            print("No correction has been applied after 3 attempts.")
            return None


def normalize_value(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return value
    return safe_text(value)


def values_equal(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return [safe_text(x).lower() for x in a] == [safe_text(x).lower() for x in b]
    return safe_text(a).strip().lower() == safe_text(b).strip().lower()


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


def apply_text_change(page, field, new_value, already_open=False):
    """Save one field correction.

    v13: when the HTTP transport is up this is a single POST to /edit -- the
    same form the pencil submits -- instead of open-pencil / fill / click-save
    / wait-for-redirect / re-read-to-verify. The DOM path below is kept as a
    fallback and is still used when the transport is unavailable.

    Verification is no longer a second page read: /edit answers "Correction
    saved" only after db.save_edit() has committed the row, so the response
    *is* the confirmation.
    """
    if use_http():
        unit_id, _ = read_card_identity(page)
        cleaned = clean_corrected_value(field, new_value)
        if looks_like_json_leak(cleaned):
            raise RuntimeError(
                f"{field}: refusing to write a value containing a JSON "
                f"fragment ({cleaned[:60]!r})"
            )
        source_url = CURRENT_SOURCE_URL.get(field, "")
        ok, message = QA.apply_edit(unit_id, field, cleaned, source_url)
        LAST_WRITTEN_VALUE[field] = cleaned
        if not ok:
            raise RuntimeError(f"{field}: {message}")
        return True

    return _apply_text_change_via_dom(page, field, new_value, already_open)


# Populated per record so the HTTP path can attach each change's source_url.
CURRENT_SOURCE_URL = {}
# The exact text each field was saved as this record (for the post-reload check).
LAST_WRITTEN_VALUE = {}


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


def apply_field_clear(page, field, previous_value=""):
    """Blank a contaminated field. v13.2: one POST /edit with new_value=""."""
    if use_http():
        unit_id, _ = read_card_identity(page)
        ok, message = QA.clear_field(unit_id, field, CURRENT_SOURCE_URL.get(field, ""))
        if not ok:
            raise RuntimeError(f"{field}: {message}")
        return True
    return _apply_field_clear_via_dom(page, field, previous_value)


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
    writable = [l for l in desired_labels if l in QUALIFYING_SUPPLIER_TYPES]
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


def apply_type_change(page, values, source_url):
    """Set supplier_type. v13.2: POST /edit with the pipe-joined labels.

    app.py validates every part against SUPPLIER_TYPES, so an off-taxonomy
    label is refused server-side with a clear message rather than saved.
    """
    if use_http():
        labels = values if isinstance(values, (list, tuple)) else [values]
        joined = " | ".join(dict.fromkeys(safe_text(v) for v in labels if safe_text(v)))
        unit_id, _ = read_card_identity(page)
        ok, message = QA.apply_edit(unit_id, "supplier_type", joined, source_url)
        if not ok:
            raise RuntimeError(f"supplier_type: {message}")
        return True
    return _apply_type_change_via_dom(page, values, source_url)


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



def skip_current_record(page, reason):
    """Leave the current card undecided.

    v13.2: POST /skip bound to unit_id+nonce. The DOM version pressed "s" and,
    if the page had not visibly advanced, pressed it AGAIN -- the same
    retry-on-a-moved-page defect the verdict path had.
    """
    if use_http():
        try:
            unit_id, nonce = read_card_identity(page)
            if QA.skip_record(unit_id, nonce):
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
        page.get_by_role("button", name=re.compile(r"^Skip\b", re.I)),
        page.locator("button").filter(has_text=re.compile(r"^\s*Skip\b", re.I)),
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

def apply_change(page, change, newly_created=False):
    field = change.get("field")
    new_value = change.get("new_value")

    # FIELDS is the always-editable core set. A field created from the page's
    # "ADD MISSING:" list is also editable, but only once it actually exists as
    # a hidden input on the page - which the check below enforces.

    if safe_text(field).lower() in FIELD_BLOCKLIST:
        print(f"↷ Skipped {field}: read-only field")
        return False

    # Only edit if the field actually exists in the current UI/database.
    # If it is absent, skip it rather than creating a new field.
    if not field_present(page, field):
        print(f"↷ Skipped {field}: field is not present in the current database/UI")
        return False
    hidden = page.locator(
        f"input[type='hidden'][name='field'][value='{field}']"
    )

    # Checkbox-group fields (supplier Type) have no reliable fill/verify path,
    # so they are never written. Detected structurally, so any future
    # checkbox field is covered without adding it to the blocklist by name.
    form = hidden.first.locator("xpath=ancestor::form[1]")
    if form.count() and form.first.locator("input[name='new_value_multi']").count():
        print(f"↷ Skipped {field}: checkbox field, left for a human reviewer")
        return False

    apply_text_change(page, field, new_value, already_open=newly_created)
    return True


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


def has_food_beverage_relevance(result):
    """True when the research text itself shows a genuine food/beverage/
    supplement domain connection, independent of the final ACCEPT/REJECT
    verdict.

    Used to decide whether a REJECTed record's field corrections (phone,
    address, website, etc.) are still worth writing to the database, or
    whether the company is unrelated enough that saving them would just add
    irrelevant enrichment to a record that has nothing to do with the actual
    supplier domain (a software vendor, a cosmetics company, etc.). This
    reuses the same domain keyword lists as infer_scope_match_from_description
    rather than a separate list, so "in scope enough to keep the changes"
    stays consistent with "in scope enough to accept".
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


def validate_scope_result(result, allow_accept=True):
    """Validate one judge answer against the v4 rulebook.

    v13: the old 259-line gate spoke ACCEPT / REJECT / MANUAL_REVIEW and
    raised ValueError on anything else, so a v4 PARK or RE_ENRICH was thrown
    out as an "Invalid decision" and the record was skipped. All four verdicts
    are now first-class; decision_v4.validate() holds the rules.

    The contract is unchanged for callers: the result dict comes back with a
    normalized "decision", cleaned "changes", and a ValueError only when the
    answer is unusable. The Judgement is attached as result["_judgement"] so
    the final action can reuse its note and suggested_url without re-deriving
    them.
    """
    judgement = decision_v4.validate(result, allow_accept=allow_accept)

    for warning in judgement.warnings:
        print(f"  \u26a0 {warning}")
    for note in judgement.downgrades:
        print(f"  \u2193 downgraded to {judgement.decision}: {note}")

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


def record_supply_origin(result):
    """Persist the origin note for an accepted non-US company.

    The review UI has no dedicated origin field, so the note is written to a
    CSV alongside the run. This is the durable record of where an accepted
    foreign supplier actually supplies from.
    """
    if result.get("decision") != "ACCEPT":
        return
    if result.get("is_us_based") is not False:
        # True (US) or None (location genuinely unknown) - neither is a
        # confirmed foreign origin, so there is nothing to log here.
        return

    row = [
        datetime.now().isoformat(timespec="seconds"),
        safe_text(result.get("company_name")),
        safe_text(result.get("supply_country")),
        safe_text(result.get("supply_origin_note")),
        ", ".join(safe_text(x) for x in (result.get("qualifying_supplier_types") or [])),
    ]

    try:
        path = Path(NON_US_LOG)
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(
                    ["timestamp", "company_name", "supply_country",
                     "supply_origin_note", "supplier_types"]
                )
            writer.writerow(row)
        print(f"✓ Origin note logged to {NON_US_LOG}")
    except Exception as exc:
        print(f"⚠ Could not write {NON_US_LOG}: {exc}")


def apply_origin_note_to_ui(page, result):
    """Best-effort: write the origin note into the review UI's note field.

    The reject flow uses input[name='note']. If the same input is present on
    the accept flow, the origin note goes there too so the note travels with
    the record. If it is absent, the CSV log above is the record of truth.
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
            print("↷ No note field in the accept flow; origin kept in the CSV log only.")
    except Exception as exc:
        print(f"↷ Could not write the origin note to the UI: {exc}")


def log_manual_review(result, record):
    """Persist a manual-review record to a durable queue for the review team.

    The review UI has no manual-review bucket of its own (its action bar is
    Platform ready / Outreach first / Re-enrich / Reject / Skip / Undo), so
    this CSV IS the queue a human reviewer works from - the durable record of
    which suppliers were flagged and why, same role NON_US_LOG plays for
    accepted non-US companies.
    """
    row = [
        datetime.now().isoformat(timespec="seconds"),
        safe_text(record.get("record_id")) or read_record_id_hint(record),
        safe_text(result.get("company_name")),
        safe_text(result.get("manual_review_reason")),
        safe_text(result.get("food_beverage_connection")),
        ", ".join(safe_text(x) for x in (result.get("qualifying_supplier_types") or [])),
    ]
    try:
        path = Path(MANUAL_REVIEW_LOG)
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow([
                    "timestamp", "record_id", "company_name",
                    "manual_review_reason", "food_beverage_connection",
                    "qualifying_supplier_types",
                ])
            writer.writerow(row)
        print(f"✓ Queued for manual review in {MANUAL_REVIEW_LOG}")
    except Exception as exc:
        print(f"⚠ Could not write {MANUAL_REVIEW_LOG}: {exc}")


def log_field_discovery_failure(page, detail):
    """Durable record of a record skipped because field discovery never
    recovered within the retry budget.

    Lets a human check whether these cluster on one record/page shape (a
    real DOM/selector change worth investigating) or scatter randomly
    across otherwise-normal records (the page-transition timing race this
    was built to survive) - same audit role MANUAL_REVIEW_LOG plays for
    scope uncertainty.
    """
    row = [
        datetime.now().isoformat(timespec="seconds"),
        read_record_id(page) or "(unknown - discovery failed before the id could be read)",
        detail,
    ]
    try:
        path = Path(FIELD_DISCOVERY_FAILURE_LOG)
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["timestamp", "record_id", "detail"])
            writer.writerow(row)
        print(f"✓ Logged to {FIELD_DISCOVERY_FAILURE_LOG}")
    except Exception as exc:
        print(f"⚠ Could not write {FIELD_DISCOVERY_FAILURE_LOG}: {exc}")


def log_website_verify(result, url, verdict, detail):
    """Durable record of every independent website sanity check, whatever
    the outcome - so a human can spot-check the inconclusive ones and
    confirm the mismatches were genuinely worth holding."""
    verdict_text = {True: "match", False: "MISMATCH", None: "inconclusive"}[verdict]
    row = [
        datetime.now().isoformat(timespec="seconds"),
        safe_text(result.get("company_name")),
        url,
        verdict_text,
        detail,
    ]
    try:
        path = Path(WEBSITE_VERIFY_LOG)
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(
                    ["timestamp", "company_name", "proposed_url", "verdict", "detail"]
                )
            writer.writerow(row)
    except Exception as exc:
        print(f"⚠ Could not write {WEBSITE_VERIFY_LOG}: {exc}")


def log_field_hold(result, record, needs_clear, needs_review, identity_renamed=None):
    """Durable queue for an ACCEPT that Auto Mode held back from Platform
    ready because a field was flagged - possibly contaminated, a website
    the independent check found no trace of, a proposed value that appears
    to be non-English, or a company-identity field that was renamed this
    run - but never actually resolved/confirmed.

    Same audit role as MANUAL_REVIEW_LOG / FIELD_DISCOVERY_FAILURE_LOG.
    Without this, a held record's only trace was a console line in an
    unattended Auto Mode run - easy to miss - even though the record itself
    was correctly left undecided on the review page rather than being
    marked accepted with a known-suspect value still live.
    """
    needs_clear = needs_clear or []
    needs_review = needs_review or []
    identity_renamed = identity_renamed or []
    clear_text = "; ".join(f"{f}={v!r}" for f, v in needs_clear)
    review_text = "; ".join(f"{f}={v!r} ({d})" for f, v, d in needs_review)
    identity_text = "; ".join(f"{f}: {o!r} -> {n!r}" for f, o, n in identity_renamed)
    row = [
        datetime.now().isoformat(timespec="seconds"),
        safe_text(record.get("record_id")) or read_record_id_hint(record),
        safe_text(result.get("company_name")),
        clear_text,
        review_text,
        identity_text,
    ]
    try:
        path = Path(FIELD_HOLD_LOG)
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow([
                    "timestamp", "record_id", "company_name",
                    "fields_left_uncleared", "fields_held_by_website_check",
                    "identity_field_renamed",
                ])
            writer.writerow(row)
        print(f"✓ Logged to {FIELD_HOLD_LOG} - record left undecided for a human to resolve.")
    except Exception as exc:
        print(f"⚠ Could not write {FIELD_HOLD_LOG}: {exc}")


def read_record_id_hint(record):
    """Best-effort MASTER id for the log row, without requiring a live page."""
    for value in (record.get("fields") or {}).values():
        text = safe_text(value)
        if MASTER_ID_RE.search(text):
            return MASTER_ID_RE.search(text).group(0)
    match = MASTER_ID_RE.search(record.get("page_context") or "")
    return match.group(0) if match else ""


def flag_for_manual_review(page, result):
    """Route a genuinely uncertain record to a human reviewer.

    This never clicks Platform ready or Reject - it best-effort writes the
    reason into the UI's note field (the same input[name='note'] the reject
    flow and the non-US origin note use; if this particular action's flow
    does not expose one, nothing is lost - log_manual_review() already wrote
    the reason to MANUAL_REVIEW_LOG), then leaves the record via the
    existing, hardened Skip path rather than reimplementing a raw keyboard
    shortcut here. skip_current_record() already tries the UI Skip button
    first and only falls back to pressing "s" after blurring any focused
    input, which matters: pressing a bare "S" into a field that still has
    focus types the letter instead of triggering the shortcut.
    """
    reason = safe_text(result.get("manual_review_reason")) or safe_text(result.get("reason"))

    try:
        note_input = page.locator("input[name='note']")
        if note_input.count():
            note_input.first.fill(("MANUAL REVIEW: " + reason)[:500])
            print("✓ Manual-review reason written to the UI note field.")
        else:
            print("↷ No note field visible here; reason is kept in the CSV log only.")
    except Exception as exc:
        print(f"↷ Could not write the manual-review note to the UI: {exc}")

    print(f"⚑ Flagged for manual review: {reason or '(no reason given)'}")
    skip_current_record(page, "manual review")


def show_result(result, applied, skipped=None, needs_clear=None, cleared=None, needs_review=None,
                 identity_renamed=None):
    print("\n" + "=" * 60)
    print("FINAL REVIEW RESULT")
    print("=" * 60)
    print(f"Company:   {result.get('company_name', 'Unknown')}")
    print(f"Decision:  {result.get('decision', 'UNKNOWN')}")
    country = safe_text(result.get("supply_country"))
    is_us = result.get("is_us_based")
    if country:
        origin = country if is_us else f"{country}  ** NON-US **"
        print(f"Supplies from: {origin}")
        note = safe_text(result.get("supply_origin_note"))
        if note:
            print(f"Origin note: {note}")
    elif is_us is False:
        print("Supplies from: ** NON-US ** (exact country not confirmed)")
        note = safe_text(result.get("supply_origin_note"))
        if note:
            print(f"Origin note: {note}")
    print(f"Confidence:{result.get('confidence', 'N/A')}")
    print(f"Reason:    {result.get('reason', '')}")
    print(f"Changes applied: {len(applied)}")

    for item in applied:
        print(f"  - {item}")

    skipped = skipped or []
    if skipped:
        print(f"Changes skipped: {len(skipped)}")
        for item in skipped:
            print(f"  - {item}")

    cleared = cleared or []
    if cleared:
        print(f"\n⌫ Contaminated fields cleared: {len(cleared)}")
        for field, reason in cleared:
            print(f"  - {field}  (reason: {reason})")

    needs_clear = needs_clear or []
    if needs_clear:
        print(
            f"\n⚠ Fields flagged for clearing but LEFT UNTOUCHED: "
            f"{len(needs_clear)}"
        )
        print(
            "  The agent only empties a field when ChatGPT sends an explicit "
            "clear flag WITH a reason. These still hold their old (likely "
            "contaminated) value and must be cleared by hand before Platform ready:"
        )
        for field, current_value in needs_clear:
            print(f"  - {field}: still shows {current_value!r}")

    needs_review = needs_review or []
    if needs_review:
        print(
            f"\n⚠ Proposed values HELD, NOT written - flagged for human review: "
            f"{len(needs_review)}"
        )
        print(
            "  Each value below failed an automated check (website identity mismatch, "
            "non-English text, etc.) - verify by hand before Platform ready:"
        )
        for field, value, detail in needs_review:
            print(f"  - {field}: proposed {value!r} — {detail}")

    identity_renamed = identity_renamed or []
    if identity_renamed:
        print(f"\n⚠ Company identity corrected this run: {len(identity_renamed)}")
        print(
            "  The \"company name outlier\" rule fired - the record's name pointed to one "
            "company while the rest of its data described another. Applied, but held for a "
            "human glance before Platform ready, since renaming the record's identity is "
            "higher-stakes than an ordinary field fix:"
        )
        for field, old_value, new_value in identity_renamed:
            print(f"  - {field}: {old_value!r} → {new_value!r}")

    if result.get("decision") == "REJECT":
        print("\nREJECTION NOTE:")
        print(result.get("reason", ""))

    if result.get("decision") == "MANUAL_REVIEW":
        print("\nMANUAL REVIEW REASON:")
        print(result.get("manual_review_reason", ""))

    print("=" * 60)


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


def fetch_corrected_record(page, expected_id="", *, result=None, applied=(), cleared=()):
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

    if RELOAD_BEFORE_FINAL_SNAPSHOT:
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


def prepare_final_snapshot(page, record, result, expected_id, *, applied, cleared,
                           failed, needs_clear, needs_review):
    """Reload -> extract -> write the accepted-companies row, BEFORE the verdict.

    Returns (corrected, snapshot_key, problems). snapshot_key is what
    accepted_snapshots.confirm_pending()/rollback_pending() need afterwards.
    Raises ReloadDriftError (callers must stop without pressing a verdict).
    """
    corrected = fetch_corrected_record(page, expected_id, result=result,
                                       applied=applied, cleared=cleared)
    problems = []
    if corrected is None:
        problems.append("the final record could not be re-read after reloading the page")
    elif corrected.get("not_landed"):
        problems.append(f"{len(corrected['not_landed'])} saved correction(s) are not on the "
                        "reloaded record: " + ", ".join(f for f, _e, _p in corrected["not_landed"]))

    key = None
    if ENABLE_ACCEPTED_SNAPSHOT and corrected is not None:
        key = accepted_snapshots.record_pending(
            record, corrected, result, applied=applied, cleared=cleared, failed=failed,
            needs_clear=needs_clear, needs_review=needs_review,
        )
    return corrected, key, problems


def perform_final_action(page, result, judgement=None):
    """Record the final verdict for the company currently on screen.

    v13 replaces the keyboard/button path with a single POST to /verdict.

    The old path had a real defect. click_or_shortcut() retried up to
    FINAL_ACTION_MAX_ATTEMPTS whenever it could not SEE the page advance --
    but a verdict that saved while the redirect was slow looks identical to
    one that was lost. The retry then pressed G again on a page that had
    already moved to the NEXT company, accepting it with no review. Nothing
    in the DOM could distinguish those two cases.

    POST /verdict cannot have that failure mode: db.save_verdict matches the
    unit_id AND the card's nonce, so the write either lands on the intended
    company or is refused as "already"/"lease_lost". It is never retried.
    """
    decision = (judgement.decision if judgement
                else safe_text(result.get("decision")).upper())

    if decision not in qa_http.DECISION_TO_VERDICT:
        raise RuntimeError(
            f"{decision!r} is not a final verdict. MANUAL_REVIEW goes to skip."
        )

    if not use_http():
        raise RuntimeError(
            "The HTTP transport is not available, so no verdict will be "
            "submitted. Decide this record by hand in the browser."
        )

    unit_id, nonce = read_card_identity(page)
    note = (judgement.note if judgement else safe_text(result.get("reason")))
    suggested_url = judgement.suggested_url if judgement else ""

    label = {"ACCEPT": "Platform ready", "PARK": "Outreach first",
             "RE_ENRICH": "Re-enrich", "REJECT": "Reject"}[decision]

    outcome = QA.submit_verdict(unit_id, nonce, decision, note=note,
                                suggested_url=suggested_url)

    if outcome == qa_http.VERDICT_SAVED:
        print(f"\u2713 {label} recorded for unit {unit_id}.")
    elif outcome == qa_http.VERDICT_ALREADY:
        print(f"\u21b7 Unit {unit_id} was already decided; left as it was.")
    elif outcome == qa_http.VERDICT_LEASE_LOST:
        raise RuntimeError(
            f"The lease on unit {unit_id} expired before the verdict was "
            f"submitted, so NOTHING was recorded. The record is back in the "
            f"queue. This usually means the record took longer than the lease "
            f"window; no other company was affected."
        )
    else:
        raise RuntimeError(f"Unit {unit_id} was not found when submitting the verdict.")

    # The browser is still showing the decided card; move it to the next one.
    try:
        page.goto(REVIEW_URL, wait_until="domcontentloaded")
    except Exception as exc:
        print(f"\u26a0 Verdict saved but the page did not advance ({exc}); "
              f"reload {REVIEW_URL} by hand.")


def wait_for_manual_verdict(page):
    print("\nAPPROVAL MODE: corrections are complete.")
    print("The agent will NOT click Accept/Reject.")
    print("Review the changes in the browser.")
    print("Then manually click Platform ready or Reject.")
    input("\nAfter you have made the final decision, press ENTER here...")


MAX_CONSECUTIVE_FAILURES = 3


def choose_backend(default=RESEARCH_BACKEND):
    print("\n==========================================")
    print("        RESEARCH BACKEND")
    print("==========================================")
    print("1. OpenRouter API  (automated - no copy/paste)")
    print("2. Manual ChatGPT  (legacy clipboard workflow)")
    print("==========================================")

    while True:
        value = input(f"Select backend [{'1' if default == 'api' else '2'}]: ").strip()
        if value == "":
            return default
        if value == "1":
            return "api"
        if value == "2":
            return "manual"
        print("Enter 1 or 2.")


def preflight_api():
    """Verify the key works and report remaining free-tier quota."""
    try:
        llm.check_api_key()
    except llm.LLMError as exc:
        print(f"\n✗ {exc}")
        return False

    print("\nChecking OpenRouter key...")
    data = llm.check_quota()
    if data is None:
        print("  ! Could not reach OpenRouter to verify the key.")
        print("  Continuing anyway; individual requests will report their own errors.")
        return True

    limit = data.get("limit")
    remaining = data.get("limit_remaining")
    usage = data.get("usage")
    print(f"  ✓ Key valid. Usage so far: {usage}")
    if limit is not None:
        print(f"  Credit limit on this key: {limit} (remaining: {remaining})")
    if not data.get("is_free_tier", True):
        print("  Account has purchased credits: free-model cap is ~1000 requests/day.")
    else:
        print("  Unfunded account: free-model cap is ~50 requests/day, ~20/minute.")
    print(f"  Model fallback chain: {', '.join(OPENROUTER_MODELS)}")
    return True


def _format_elapsed(seconds: float) -> str:
    """Human-readable elapsed time (e.g. '1 h 12 min 05 sec')."""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} h")
    if m or h:
        parts.append(f"{m} min")
    parts.append(f"{s:02d} sec")
    return " ".join(parts)


def main():
    session_start = time.time()

    mode = choose_mode()
    if mode is None:
        return

    backend = choose_backend()
    if backend == "api" and not preflight_api():
        print("Cannot start the automated backend. Fix the API key and retry.")
        return

    run_count = choose_run_count()
    if run_count is None:
        print("Run cancelled.")
        return

    compact = False
    if backend == "manual" and CHATGPT_PROJECT_MODE:
        path = write_project_instructions()
        print("\n" + "=" * 60)
        print("ONE-TIME SETUP: ChatGPT Project")
        print("=" * 60)
        print(f"The standing rules were written to: {path}")
        print("Paste that file into your ChatGPT Project's custom instructions")
        print("(REPLACE the old text - v13.4 changed section 1 so ChatGPT browses)")
        print("(ChatGPT > Projects > your project > Instructions), then run every")
        print("record inside that project.")
        print("Each record now starts with a hard NEW RECORD reset and stays")
        print("under ~800 characters so the Project chat lasts longer before drift.")
        print("=" * 60)
        answer = input("Are the project instructions loaded? [Y/n]: ").strip().lower()
        compact = answer in ("", "y", "yes")
        if not compact:
            print("Using full prompts (rules included in every paste).")

    print(f"\nMODE: {mode.upper()}")
    print(f"BACKEND: {backend.upper()}")
    print(f"Run count: {run_count} records")
    print(f"Session started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if backend == "api" and run_count > 40:
        print(
            "\n⚠ Note: each record costs 1 OpenRouter request. An unfunded free "
            "account allows ~50 requests/day, and failed requests still count."
        )

    global QA

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport=None,
            args=[
                "--start-maximized",
                # browser_profile/ is a persistent context reused on every
                # run, so its disk cache otherwise grows unbounded across
                # hundreds of records/sessions. Cap it so a bloated or
                # corrupted cache can't crash Chromium mid-run.
                "--disk-cache-size=104857600",  # 100MB
            ],
        )

        # Share the browser's cookie jar, so the agent inherits the operator's
        # existing login and open shift. No credentials are handled here.
        QA = QAClient(context, base_url=BASE_URL)
        print(f"\u2713 HTTP transport ready ({BASE_URL}) \u2014 edits and verdicts "
              f"go straight to the app's own endpoints.")

        page = context.pages[0] if context.pages else context.new_page()

        page.goto(REVIEW_URL, wait_until="domcontentloaded")

        print("\nBrowser opened.")
        print("Log in manually if necessary.")
        input("When the supplier review page is visible, press ENTER...")

        # Snapshot of suppliers already decided in earlier sessions, used only
        # to print a heads-up when one comes round again. Read once: a stale
        # entry costs an informational line, never a decision.
        history_index = history.load_index()
        if history_index:
            print(f"\nHistory: {len(history_index)} supplier(s) already on record "
                  f"in {HISTORY_EXCEL_FILE}.")
        elif ENABLE_HISTORY_EXCEL:
            print(f"\nHistory: starting a new {HISTORY_EXCEL_FILE}.")

        processed = 0
        failures = 0
        discovery_failures = 0
        # Same-company guard (v12.25): if the page serves the supplier we
        # just finalized (verdict POST lost, or leftover corrections), reuse
        # the previous research instead of another ChatGPT round-trip.
        last_finalized_id = ""
        last_finalized_name = ""
        last_result = None
        repeat_passes = 0
        repeat_passes_total = 0
        # v13.4: decision mix, so a skewed run (e.g. 9/10 PARK) is visible
        # in the summary together with whether the model or the gate chose it.
        decision_tally = {}

        while processed < run_count:
            print("\n" + "#" * 60)
            print(f"RECORD {processed + 1} / {run_count}")
            print("#" * 60)

            # The automated Chromium window must stay open on the review page
            # for the whole session - it is never navigated to ChatGPT and no
            # part of the script closes it between records. If it's gone here,
            # it was closed outside the script (most often by accident while
            # switching over to paste into ChatGPT in what turned out to be
            # this same window). Fail with a clear, specific message instead
            # of a confusing downstream Playwright error.
            if page.is_closed():
                print(
                    "\nThe review-page browser window is closed. This script "
                    "never navigates that window to ChatGPT itself - paste "
                    "ChatGPT's prompt/response using a SEPARATE browser "
                    "window, and leave this one open and untouched. "
                    f"Progress: {processed} of {run_count} records completed."
                )
                break

            try:
                record = extract_record(page)
            except FieldDiscoveryError as exc:
                # Fail loudly and skip rather than silently sending a
                # blank/degraded record for research - see
                # FieldDiscoveryError's docstring for why the old silent
                # fallback made this exact failure invisible.
                print(
                    f"\n⚠ Field discovery failed for this record: {exc}\n"
                    "  Skipping this record instead of researching against a "
                    "page it could not actually read."
                )
                discovery_failures += 1
                log_field_discovery_failure(page, str(exc))
                if discovery_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"\n{discovery_failures} consecutive field-discovery "
                        "failures. Stopping safely - this usually means the "
                        "review page's DOM structure changed (a real "
                        "selector break), not a transient timing race. Check "
                        f"{FIELD_DISCOVERY_FAILURE_LOG} before resuming."
                    )
                    break
                skip_current_record(page, "field discovery failed")
                page.wait_for_timeout(400)
                continue
            discovery_failures = 0

            print("\nCurrent fields:")
            print(json.dumps(record["fields"], indent=2, ensure_ascii=False))

            history.warn_if_seen(history_index, record)

            this_id = read_record_id(page)
            this_name = safe_text(record["fields"].get("company_name")).lower()
            is_repeat = (
                REUSE_RESULT_ON_REPEAT
                and last_result is not None
                and (
                    (this_id and this_id == last_finalized_id)
                    or (this_name and this_name == last_finalized_name)
                )
            )
            if is_repeat and repeat_passes < MAX_REPEAT_PASSES:
                repeat_passes += 1
                repeat_passes_total += 1
                print(
                    f"\n↻ SAME SUPPLIER SERVED AGAIN ({this_id or this_name}). "
                    f"Reusing the previous research result - no ChatGPT round-trip. "
                    f"(repeat pass {repeat_passes}/{MAX_REPEAT_PASSES})"
                )
                result = dict(last_result)
            else:
                if is_repeat:
                    print(
                        f"\n⚠ {this_id or this_name} served {repeat_passes} extra times; "
                        "researching afresh in case the record really changed."
                    )
                repeat_passes = 0
                result = get_research_result(page, record, backend, compact=compact)

            # Any of these three problems means this one record's research is
            # unusable. Previously only the "result is None" case, and only
            # for backend == "api", was treated as skippable - a single bad
            # clipboard round-trip on the manual backend (or a malformed
            # response on either backend) always killed the whole batch.
            # CONTINUE_ON_RESEARCH_FAILURE now applies uniformly.
            failure_reason = None
            if result is None:
                failure_reason = "Research unavailable for this record"
            else:
                try:
                    result = validate_scope_result(result)
                except Exception as exc:
                    failure_reason = f"Invalid research result ({exc})"
                else:
                    # All four v4 verdicts are actionable; MANUAL_REVIEW is
                    # handled separately below (it is a skip, not a verdict).
                    valid_decisions = tuple(qa_http.DECISION_TO_VERDICT)
                    if ENABLE_MANUAL_REVIEW:
                        valid_decisions += ("MANUAL_REVIEW",)
                    if result.get("decision") not in valid_decisions:
                        failure_reason = (
                            f"Unusable decision {result.get('decision')!r}")

            if failure_reason is not None:
                if CONTINUE_ON_RESEARCH_FAILURE:
                    print(f"{failure_reason}; skipping this record.")
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        print(
                            f"\n{failures} consecutive research failures. "
                            "Stopping safely - check your API key/quota, model "
                            "list, or the manual copy/paste workflow."
                        )
                        break
                    skip_current_record(page, failure_reason)
                    page.wait_for_timeout(400)
                    continue
                print(f"{failure_reason}. Stopping safely. No final action performed.")
                break

            failures = 0

            if not is_repeat:
                _j = result.get("_judgement")
                _key = result.get("decision") or "?"
                if result.get("bucket"):
                    _key += f" [{result['bucket']}]"
                if _j is not None and _j.downgrades:
                    _key += " (downgraded by gate)"
                decision_tally[_key] = decision_tally.get(_key, 0) + 1

            if result.get("decision") == "MANUAL_REVIEW":
                # Genuinely uncertain after real research - route to a human
                # reviewer instead of forcing a guess. No field corrections
                # are applied (validate_scope_result already forced
                # changes=[]), and neither Platform ready nor Reject is ever
                # clicked for this record.
                show_result(result, applied=[], skipped=[])
                log_manual_review(result, record)
                if mode == "approval":
                    input(
                        "\nAPPROVAL MODE: reviewed above. Press ENTER to flag this "
                        "record for manual review and move to the next supplier..."
                    )
                flag_for_manual_review(page, result)
                # finalized=False: no verdict was written, so this goes to the
                # undecided sheet rather than Accepted/Rejected.
                history.record_decision(
                    record, result,
                    outcome="left undecided - flagged for manual review",
                    mode=mode, backend=backend, finalized=False,
                )

                last_finalized_id, last_finalized_name, last_result = "", "", None
                processed += 1
                if processed >= run_count:
                    print(f"\nRequested run count reached ({run_count} records).")
                    break

                print("\nWaiting for the next supplier...")
                page.wait_for_timeout(500)
                continue

            # FAST PATH: REJECT decisions skip all field changes. There is no
            # point editing, clearing, or creating fields on a record that is
            # about to be rejected - it wastes time on clicks, verification
            # loops, and website fetches for data that will never be used.
            if result.get("decision") == "REJECT":
                show_result(result, applied=[], skipped=[])

                if mode == "approval":
                    wait_for_manual_verdict(page)
                    reject_outcome = "rejected - final click made by the reviewer (approval mode)"
                else:
                    print("\nAUTO MODE: performing final action...")
                    perform_final_action(page, result, result.get("_judgement"))
                    page.wait_for_timeout(300)
                    reject_outcome = "rejected - Reject clicked by the agent"

                # Recorded only after the verdict actually landed: if
                # perform_final_action() raised, nothing was rejected, and the
                # history must not claim otherwise.
                history.record_decision(
                    record, result, outcome=reject_outcome,
                    mode=mode, backend=backend, finalized=True,
                )

                last_finalized_id, last_finalized_name, last_result = this_id, this_name, result
                if not is_repeat:
                    processed += 1
                if processed >= run_count:
                    print(f"\nRequested run count reached ({run_count} records).")
                    break

                print("\nWaiting for the next supplier...")
                page.wait_for_timeout(500)
                continue

            changes = result.get("changes", [])
            if isinstance(changes, dict):
                # A lone change object instead of a one-item list - same
                # shape drift as scope_match/decision/qualifying_supplier_types
                # above. Wrap it rather than silently dropping every proposed
                # correction on the record.
                changes = [changes]
            elif not isinstance(changes, list):
                print(f"↷ Ignoring malformed 'changes' value: {changes!r}")
                changes = []
            applied = []
            skipped = []
            # Fields ChatGPT asked to CLEAR (proposed an empty value) that
            # currently hold a real value. The agent never blanks a field
            # automatically, so these are reported separately - the stale value
            # is still on the card and a human must clear it by hand. Kept out
            # of `skipped` so it does not read as a failed correction.
            needs_clear = []
            # Fields actually emptied this run via the explicit clear protocol.
            cleared = []
            # website_url (and any future identity-bearing field) changes an
            # independent fetch could not confirm - held back, never applied
            # automatically. See VERIFY_WEBSITE_BEFORE_APPLY in config.py.
            needs_review = []
            # Successful company-name/identity-field corrections this run
            # (the "company name outlier" rule). Applied normally - the
            # correction is real and wanted - but always tracked separately
            # so the final-action gate below can hold Auto Mode's Platform
            # ready for a human glance before an identity rename goes live.
            identity_renamed = []

            # Hard safety filter: only fields discover_fields() actually found
            # on THIS record (record["editable_fields"], read fresh from the
            # live DOM - not a hardcoded list) or explicitly offered under
            # FIELDS AVAILABLE TO ADD may be edited. FIELD_BLOCKLIST identity
            # fields (type/master/id) are excluded regardless.
            allowed_changes = []
            created_fields = []

            for change in changes:
                if not isinstance(change, dict):
                    print("↷ Ignoring malformed change entry.")
                    continue

                field = change.get("field")
                editable = record.get("editable_fields") or FIELDS
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
                    print(f"↷ Ignoring read-only field: {field}")
                    continue
                if allow_type_fill:
                    # Route to the dedicated checkbox handler in the apply stage.
                    allowed_changes.append(dict(change, _type_fill=True, _type_entry=type_add_missing))
                    continue
                if field not in editable and missing_match is None:
                    print(f"↷ Ignoring non-editable field proposed by ChatGPT: {field}")
                    continue

                # null means DO NOT EDIT. Never clear/delete a field automatically.
                if change.get("new_value") is None and not change.get("clear"):
                    print(f"↷ Skipped {field}: proposed new_value is null")
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
                        print(
                            f"⚠ {field}: clear requested but no clear_reason given - refusing "
                            f"to empty a field without a stated reason. LEFT UNCHANGED."
                        )
                        needs_clear.append((field, current_value or "(already empty)"))
                        continue
                    if not current_value:
                        print(f"↷ Skipped {field}: clear requested but field is already empty")
                        continue
                    # Passed the gate: queue a genuine clear.
                    allowed_changes.append(dict(change, _clear=True))
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
                    print(f"↷ Skipped {field}: record already holds this value")
                    continue
                if cleaned_proposal is None or safe_text(cleaned_proposal) == "":
                    current_value = safe_text(record["fields"].get(field))
                    if current_value:
                        print(
                            f"⚠ {field}: empty value proposed with no clear flag - ignored. "
                            f"Current value ({current_value!r}) left as-is. If it is "
                            f"contaminated, ChatGPT must send clear=true with a reason."
                        )
                        needs_clear.append((field, current_value))
                    else:
                        print(f"↷ Skipped {field}: proposed empty value and field is already empty")
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
                        print(
                            f"↷ Skipped general_email: same address as primary_email "
                            f"({proposed}); the two email slots must differ."
                        )
                        continue

                # LANGUAGE GUARD: this database is filtered/searched in
                # English. A value that is clearly not English (Cyrillic/CJK,
                # or a real concentration of non-English Latin diacritics -
                # e.g. Czech "ořechové máslo, arašídový krém") would sit in
                # the database invisibly to those filters even though it is
                # factually correct, exactly the failure mode that let a
                # Czech-language sub_categories value through untranslated.
                # Held back rather than guess-translated in code: the
                # research step is responsible for producing the English
                # value, this is only a safety net for when it doesn't.
                if field_norm in _LANGUAGE_CHECKED_FIELDS and looks_non_english(
                    cleaned_proposal
                ):
                    print(
                        f"⚠ {field}: proposed value appears to be non-English "
                        f"({cleaned_proposal!r}) - held back, not written. "
                        "Needs an English value before this can be applied."
                    )
                    needs_review.append(
                        (field, cleaned_proposal, "value appears non-English")
                    )
                    continue

                allowed_changes.append(change)

            # Apply only explicit, non-null changes.
            for change in allowed_changes:
                field = change.get("field")
                new_value = clean_corrected_value(field, change.get("new_value"))
                newly_created = False

                # EXPLICIT CLEAR path: remove a contaminated value. Gated in the
                # filter above (flag present + reason given + field non-empty).
                if change.get("_clear"):
                    reason = safe_text(change.get("clear_reason"))
                    # The field must exist on the page to be cleared; a clear on
                    # an absent field is a no-op we simply report.
                    if not field_present(page, field):
                        print(f"↷ Clear skipped for {field}: not present in the current UI")
                        continue
                    try:
                        apply_field_clear(page, field, previous_value=safe_text(record['fields'].get(field)))
                        cleared.append((field, reason))
                        print(f"⌫ Cleared contaminated {field} (reason: {reason})")
                    except Exception as exc:
                        print(f"✗ Could not clear {field}: {exc}")
                        needs_clear.append((field, safe_text(record['fields'].get(field))))
                    continue

                # TYPE FILL path: the record had no type and the page offered to
                # add one. Tick ChatGPT's proposed qualifying categories via the
                # checkbox group, then verify (handled in apply_type_missing).
                if change.get("_type_fill"):
                    desired = _canonical_type_labels(change)
                    entry = change.get("_type_entry")
                    try:
                        written = apply_type_missing(page, entry, desired)
                        applied.append("supplier_type")
                        created_fields.append("Type")
                        print(f"✓ Set type (was missing): {', '.join(written)}")
                    except Exception as exc:
                        print(f"✗ Could not set type: {exc}")
                        skipped.append("supplier_type")
                    continue

                # If the field does not exist yet, try to create it from the
                # page's "ADD MISSING:" list before giving up. A field is only
                # ever created when a verified value is about to be written.
                if not field_present(page, field):
                    entry = addmissing.match_missing_field(
                        field, record.get("missing_fields") or []
                    )
                    if entry is None:
                        print(
                            f"↷ Skipped {field}: not present in the database/UI "
                            "and not offered under ADD MISSING"
                        )
                        continue

                    if new_value is None or safe_text(new_value) == "":
                        print(f"↷ Skipped {field}: refusing to create a field with no value")
                        continue

                    if use_http():
                        # v13.2: db.save_edit() uses jsonb_set(..., TRUE), which
                        # creates the key when absent. A missing field is just
                        # an edit; the "+ Field" click was only ever a UI
                        # affordance for opening the same form.
                        created_key = field
                        print(f"+ Adding missing field '{entry['label']}' as {field} (direct edit)")
                    else:
                        print(f"+ Creating missing field '{entry['label']}' for {field}...")
                        created_key = addmissing.create_field(page, entry)
                        if not created_key:
                            print(f"↷ Skipped {field}: could not create the field")
                            continue

                    # The UI may expose it under a different key than proposed.
                    field = created_key
                    change = dict(change, field=created_key)
                    created_fields.append(entry["label"])
                    newly_created = True

                # Independent sanity check for a proposed website: fetch it
                # and confirm the company's own name actually appears there
                # before writing it. This is exactly the net that would have
                # caught sensibleportions.com being written onto an
                # unrelated Cabot, AR company - two contaminated candidates
                # (a snack brand's site, a bank's LinkedIn) sat on that
                # record, and nothing re-checked either one against the
                # company itself before applying. A failed FETCH (network,
                # timeout, blocked domain) never blocks the write - only a
                # fetch that succeeds and finds no match does, and every
                # outcome is logged either way.
                if VERIFY_WEBSITE_BEFORE_APPLY and field == "website_url" and safe_text(new_value):
                    verdict, detail = evidence_mod.website_matches_company(
                        page.context, result.get("company_name") or "", new_value
                    )
                    log_website_verify(result, new_value, verdict, detail)
                    if verdict is False:
                        print(
                            f"⚠ HOLDING website_url — independent check found no match: {detail}\n"
                            f"  NOT writing {new_value!r} automatically. Verify by hand."
                        )
                        needs_review.append((field, new_value, detail))
                        continue
                    elif verdict is None:
                        print(f"↷ website_url sanity check inconclusive ({detail}); applying anyway.")
                    else:
                        print(f"✓ website_url sanity check passed: {detail}")

                try:
                    CURRENT_SOURCE_URL[field] = safe_text(change.get("source_url"))
                    did_apply = apply_change(page, change, newly_created=newly_created)
                    if did_apply and field in LAST_WRITTEN_VALUE:
                        change["_written_value"] = LAST_WRITTEN_VALUE[field]
                    if did_apply:
                        applied.append(field)
                        print(f"✓ Applied: {field} → {new_value!r}")
                        if safe_text(field).lower() in IDENTITY_FIELD_KEYS:
                            old_value = safe_text(record["fields"].get(field))
                            if is_substantial_identity_change(old_value, new_value):
                                identity_renamed.append((field, old_value, new_value))
                                print(
                                    f"  ⚠ Substantial company-identity change. "
                                    f"{old_value!r} → {new_value!r} - held for confirmation "
                                    "before the record is finalized (see below)."
                                )
                            else:
                                print(
                                    f"  (minor name cleanup only — not held: "
                                    f"{old_value!r} → {new_value!r})"
                                )
                except Exception as exc:
                    error_text = str(exc)
                    print(f"✗ Could not apply {field}: {error_text}")

                    # A failed field is a field-level problem. Record it and
                    # move on to the next proposed change ON THIS SAME RECORD.
                    #
                    # This deliberately does NOT touch the UI. The previous
                    # version called the Skip action here, but Skip (S) on
                    # this UI abandons the ENTIRE supplier and advances to the
                    # next one. So a single missing edit button silently moved
                    # the page to a different company, and every later action
                    # in this loop - the remaining corrections and the final
                    # Platform ready / Reject - was applied to that wrong
                    # company. Abandoning one field just means not submitting
                    # that field's inline form, which is already the case.
                    skipped.append(field)
                    print("Continuing with the remaining changes for this record.")
                    continue

            if created_fields:
                print(f"\n+ Fields created and filled: {', '.join(created_fields)}")
            show_result(result, applied, skipped, needs_clear=needs_clear, cleared=cleared,
                        needs_review=needs_review, identity_renamed=identity_renamed)
            record_supply_origin(result)
            apply_origin_note_to_ui(page, result)

            # Last line of defence before an irreversible verdict: confirm the
            # page still shows the supplier this research is about. If the page
            # drifted (a stray record-level action, a manual keypress in the
            # automated window, a UI auto-advance), applying Platform ready /
            # Reject here would mark the WRONG company - a silent, incorrect
            # write into the production database.
            expected_id = safe_text(record.get("record_id"))
            current_id = read_record_id(page)
            if expected_id and current_id and expected_id != current_id:
                print(
                    f"\n✗ SAFETY STOP: the review page moved to a different supplier.\n"
                    f"  Research was for {expected_id}; the page now shows {current_id}.\n"
                    f"  No final action was performed, so no verdict was written to the\n"
                    f"  wrong company. Re-run this supplier and report this message."
                )
                break

            # v32.1 - FINAL SNAPSHOT ORDER: corrections saved -> RELOAD the page
            # -> extract every field -> write it to accepted_companies.xlsx ->
            # THEN press the verdict. The row is written as "pending" and is
            # confirmed once Platform ready lands, or rolled back if it does not.
            is_accept = result.get("decision") == "ACCEPT"
            corrected = None
            snapshot_key = None
            snapshot_problems = []
            accepted_confirmed = False
            accepted_by = ""
            CURRENT_SOURCE_URL.clear()
            LAST_WRITTEN_VALUE.clear()

            if is_accept:
                try:
                    corrected, snapshot_key, snapshot_problems = prepare_final_snapshot(
                        page, record, result, expected_id, applied=applied, cleared=cleared,
                        failed=skipped, needs_clear=needs_clear, needs_review=needs_review)
                except ReloadDriftError as exc:
                    print(
                        f"\n\u2717 SAFETY STOP: {exc}.\n"
                        "  No verdict was pressed and nothing was written to the accepted-companies\n"
                        "  file. Check both companies in the review app before resuming."
                    )
                    break

            if mode == "approval":
                if snapshot_problems:
                    print("\n" + "!" * 60)
                    print("FINAL RECORD CHECK FAILED - check before clicking Platform ready:")
                    for line in snapshot_problems:
                        print(f"  - {line}")
                    print("!" * 60)
                wait_for_manual_verdict(page)
                final_outcome = (
                    f"{safe_text(result.get('decision')).lower()} - final click "
                    "made by the reviewer (approval mode)"
                )
                was_finalized = True
                if is_accept and ENABLE_ACCEPTED_SNAPSHOT:
                    # The reviewer may have rejected or skipped instead; only
                    # a confirmed Platform ready belongs in the accepted file.
                    answer = input(
                        f"Did you click Platform ready? Saves this company to "
                        f"{ACCEPTED_SNAPSHOT_FILE}. [Y/n]: "
                    ).strip().lower()
                    accepted_confirmed = answer in ("", "y", "yes")
                    accepted_by = "reviewer (approval mode)"
            else:
                unresolved = bool(needs_clear) or bool(needs_review)
                renamed_identity = HOLD_ON_IDENTITY_RENAME and bool(identity_renamed)
                snapshot_hold = bool(snapshot_problems) and (
                    (corrected is None and HOLD_ACCEPT_IF_SNAPSHOT_FAILED)
                    or (corrected is not None and HOLD_ACCEPT_IF_EDITS_NOT_LANDED))
                if result.get("decision") == "ACCEPT" and (
                    (HOLD_ACCEPT_ON_UNRESOLVED_FIELDS and unresolved) or renamed_identity
                    or snapshot_hold
                ):
                    reasons = []
                    if snapshot_problems:
                        reasons.extend(snapshot_problems)
                    if HOLD_ACCEPT_ON_UNRESOLVED_FIELDS and needs_clear:
                        reasons.append(f"{len(needs_clear)} field(s) left uncleared")
                    if HOLD_ACCEPT_ON_UNRESOLVED_FIELDS and needs_review:
                        reasons.append(f"{len(needs_review)} field(s) flagged for review")
                    if renamed_identity:
                        reasons.append(f"{len(identity_renamed)} company-identity field(s) renamed")
                    print(
                        f"\n⚠ AUTO MODE HOLD: {', '.join(reasons)} on an ACCEPT decision. NOT "
                        "clicking Platform ready - skipping instead so a human decides."
                    )
                    log_field_hold(result, record, needs_clear, needs_review, identity_renamed)
                    skip_current_record(page, "unresolved flagged fields on an ACCEPT (auto mode hold)")
                    page.wait_for_timeout(400)
                    final_outcome = f"HELD, left undecided - {', '.join(reasons)}"
                    was_finalized = False
                else:
                    print("\nAUTO MODE: performing final action...")
                    # Raises if Platform ready does not land, so
                    # accepted_confirmed below is only ever set on a real accept.
                    try:
                        perform_final_action(page, result, result.get("_judgement"))
                    except Exception:
                        if snapshot_key:
                            accepted_snapshots.rollback_pending(
                                snapshot_key, "Platform ready did not land")
                        raise
                    page.wait_for_timeout(300)
                    final_outcome = (
                        f"{safe_text(result.get('decision')).lower()} - verdict "
                        "clicked by the agent"
                    )
                    was_finalized = True
                    if is_accept:
                        accepted_confirmed = True
                        accepted_by = "agent (auto mode)"

            if accepted_confirmed and snapshot_key:
                accepted_snapshots.confirm_pending(snapshot_key, confirmed_by=accepted_by)
            elif accepted_confirmed:
                # Snapshot could not be taken before the verdict (reload failed);
                # file what we have, clearly marked as reconstructed.
                accepted_snapshots.record_accepted(
                    record, corrected, result,
                    applied=applied, cleared=cleared, failed=skipped,
                    needs_clear=needs_clear, needs_review=needs_review,
                    confirmed_by=accepted_by,
                )
            elif snapshot_key:
                accepted_snapshots.rollback_pending(snapshot_key, "not accepted")

            # A held record is NOT accepted, so it goes to the undecided sheet.
            # If a human resolves it and it comes back through the pipeline
            # later, its row moves to Accepted rather than being duplicated.
            history.record_decision(
                record, result, outcome=final_outcome, mode=mode, backend=backend,
                corrected=corrected,
                finalized=was_finalized, applied=applied, created=created_fields,
                cleared=cleared, failed=skipped, needs_clear=needs_clear,
                needs_review=needs_review, identity_renamed=identity_renamed,
            )

            last_finalized_id, last_finalized_name, last_result = this_id, this_name, result
            if not is_repeat:
                processed += 1

            if processed >= run_count:
                print(f"\nRequested run count reached ({run_count} records).")
                break

            print("\nWaiting for the next supplier...")
            wait_for_page_settle(page)

        elapsed = time.time() - session_start
        print("\n" + "=" * 50)
        print("Session finished.")
        print(f"Records processed : {processed}")
        print(f"Repeat passes     : {repeat_passes_total}  (same supplier served again; no ChatGPT round-trip)")
        print(f"Time used         : {_format_elapsed(elapsed)}")
        if decision_tally:
            print("Decisions         :")
            for _key, _n in sorted(decision_tally.items(), key=lambda kv: -kv[1]):
                print(f"   {_n:>3}  {_key}")
        if ENABLE_HISTORY_EXCEL:
            print(f"History workbook  : {HISTORY_EXCEL_FILE}  "
                  f"(Accepted / Rejected / Manual review & held)")
        if ENABLE_ACCEPTED_SNAPSHOT:
            print(f"Accepted companies: {ACCEPTED_SNAPSHOT_FILE}  "
                  f"(corrected record as uploaded, accepted only)")
        if use_http():
            print(QA.summary())
            for warning in QA.health_warnings():
                print(f"\u26a0 {warning}")
        print("=" * 50)
        close_browser(context)


def is_browser_gone(exc):
    """True when an exception means the browser/driver connection died."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "connection closed",
            "target closed",
            "browser has been closed",
            "target page, context or browser has been closed",
            "websocket",
        )
    )


def close_browser(context):
    """Close the browser context without letting teardown noise mask a good run.

    If the browser window was closed manually, or Chromium exited first, the
    driver connection is already gone and close() raises. By this point every
    record has been applied and saved, so the failure is cosmetic - it must not
    surface as a traceback that looks like the session failed.
    """
    try:
        context.close()
    except Exception as exc:
        print(f"(Browser was already closed: {type(exc).__name__})")


def run():
    """Entry point wrapper: exit cleanly on Ctrl+C and on teardown errors."""
    # Capture start here as a safety net so early exits still show elapsed time
    # even if main() itself never got far enough to set its own timer.
    _run_start = time.time()
    try:
        main()
    except KeyboardInterrupt:
        elapsed = time.time() - _run_start
        print("\n\nInterrupted.")
        print(f"Time used before interrupt: {_format_elapsed(elapsed)}")
        print("No further action was performed.")
        sys.exit(130)
    except Exception as exc:
        if is_browser_gone(exc):
            # The browser window was closed. Every record processed before that
            # point was already applied and saved, so this is not a failed run.
            elapsed = time.time() - _run_start
            print(
                "\nThe browser was closed, so the session ended early. "
                "Records completed before that point were saved."
            )
            print(f"Time used: {_format_elapsed(elapsed)}")
            sys.exit(0)
        raise


if __name__ == "__main__":
    run()
