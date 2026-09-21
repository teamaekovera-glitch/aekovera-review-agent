"""Field constants and identity-rename detection.

Ported from the v34 archive (legacy/aekovera/main.py). These constants are
shared by discovery, prompting and corrections, so they live in one module
instead of a single 3,800-line main().
"""

import re

from review_hub.jsonutil import safe_text

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
# "Giraffe Foods" -> "Giraffe Foods Inc." or "LiDestri Foods" -> "LiDestri Foods, Inc."
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
