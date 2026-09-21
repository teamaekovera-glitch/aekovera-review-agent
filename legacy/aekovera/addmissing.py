"""Detect and create fields offered under the review page's "ADD MISSING:" list.

The review UI hides some fields until you click a "+ Specialty" style control.
Until now the agent skipped those entirely. This module lets it:

  1. read which fields the page offers to add,
  2. show that list to the researcher so values can be proposed for them,
  3. click the control to create the field, then fill it normally.

SAFETY NOTES
------------
Creating a field is a write the agent could not previously make, so:
- A field is only ever created when a verified value is being written into it.
  The agent never clicks "+ Field" speculatively and never leaves a created
  field blank.
- Creation still goes through the normal save-and-verify path. If the value
  does not read back correctly, it is reported as failed like any other edit.
- Nothing here can clear or overwrite an existing value: these controls only
  appear for fields that are absent.
"""

import re

from jsonutil import safe_text


def normalize_label(text):
    """Reduce a label or field key to a comparable form.

    "Email 2" / "email_2" / "EMAIL2" all normalize to "email2", so a value
    proposed as either the UI label or the database key still matches.
    """
    return re.sub(r"[^a-z0-9]", "", safe_text(text).lower())


# Known label -> database key mappings. Anything not listed here is matched by
# its normalized label, which covers Specialty, DBA, LinkedIn and friends.
LABEL_TO_FIELD = {
    "email": "primary_email",
    "email2": "general_email",
    "email 2": "general_email",
    "phone": "primary_phone",
    "website": "website_url",
    "zip": "zip",
    "linkedin": "linkedin_url",
    "specialty": "specialty",
    "products": "products",
    "dba": "dba_name",
    "address": "street_address",
    "streetaddress": "street_address",
    "street address": "street_address",
}


def detect_missing_fields(page):
    """Return the fields the page offers to add.

    Each entry: {"label": "Specialty", "key": "specialty", "selector_text": "+ Specialty"}
    """
    found = []
    seen = set()

    try:
        candidates = page.locator(
            "button:has-text('+'), a:has-text('+'), [role='button']:has-text('+')"
        )
        count = candidates.count()
    except Exception:
        return found

    for i in range(min(count, 40)):
        try:
            element = candidates.nth(i)
            if not element.is_visible():
                continue
            text = safe_text(element.inner_text())
        except Exception:
            continue

        if not text.startswith("+"):
            continue

        label = text.lstrip("+").strip()
        # Guard against picking up unrelated "+" controls.
        if not label or len(label) > 40 or "\n" in label:
            continue

        normalized = normalize_label(label)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        found.append({
            "label": label,
            "key": LABEL_TO_FIELD.get(normalized, normalized),
            "selector_text": text,
        })

    return found


def match_missing_field(field_name, missing_fields):
    """Find the ADD MISSING entry matching a proposed field name, or None."""
    target = normalize_label(field_name)
    if not target:
        return None

    for entry in missing_fields:
        if normalize_label(entry["label"]) == target:
            return entry
        if normalize_label(entry["key"]) == target:
            return entry

    # Also match via the label->key table, so "general_email" finds "Email 2".
    for label, key in LABEL_TO_FIELD.items():
        if normalize_label(key) == target:
            for entry in missing_fields:
                if normalize_label(entry["label"]) == label:
                    return entry
    return None


def create_field(page, entry, timeout_ms=5000):
    """Click the "+ Field" control so the field becomes editable.

    Returns the database key of the newly exposed field, or None on failure.

    The Aekovera review page has a sticky verdict bar at the bottom that
    intercepts pointer events for buttons near the bottom of the viewport.
    This was the #1 cause of "could not create the field" failures: the button
    was visible and stable, but the verdict bar sat on top of it. Two
    mitigations:
      1. Scroll the button to the TOP of the viewport (well above the bar).
      2. If the normal click still fails due to interception, fall back to a
         JavaScript click that bypasses the hit-test entirely.
    """
    label = entry["label"]
    before = _hidden_field_values(page)

    try:
        control = page.locator(
            f"button:has-text('{entry['selector_text']}'), "
            f"a:has-text('{entry['selector_text']}')"
        ).first
        if not control.count():
            control = page.get_by_text(entry["selector_text"], exact=False).first

        # Scroll the button to the TOP of the viewport so the sticky verdict
        # bar at the bottom cannot intercept the click.
        try:
            control.evaluate("el => el.scrollIntoView({block: 'start'})")
            page.wait_for_timeout(150)
        except Exception:
            pass

        try:
            control.click(timeout=3000)
        except Exception:
            # Fallback: JS click bypasses the hit-test overlay entirely.
            control.evaluate("el => el.click()")
    except Exception as exc:
        print(f"↷ Could not click '+ {label}': {exc}")
        return None

    page.wait_for_timeout(400)

    after = _hidden_field_values(page)
    created = [f for f in after if f not in before]

    if not created:
        print(f"↷ Clicked '+ {label}' but no new editable field appeared.")
        return None

    # Prefer the expected database key from LABEL_TO_FIELD, then a candidate
    # whose normalized name matches the label, then the first new field.
    # This prevents the common bug where "+ LinkedIn" or "+ Email 2" lands
    # on the wrong newly-appeared key (primary_email, website_url, dba_name…).
    expected_key = LABEL_TO_FIELD.get(normalize_label(label))
    target = normalize_label(label)

    if expected_key and expected_key in created:
        print(f"✓ Created field '{label}' -> {expected_key}")
        return expected_key

    for candidate in created:
        if normalize_label(candidate) == target:
            print(f"✓ Created field '{label}' -> {candidate}")
            return candidate

    # Last resort: if only one field appeared, take it; otherwise refuse
    # rather than writing into a random field.
    if len(created) == 1:
        print(f"✓ Created field '{label}' -> {created[0]}")
        return created[0]

    print(
        f"↷ Clicked '+ {label}' but ambiguous new fields appeared "
        f"({created}); refusing to guess."
    )
    return None


def _hidden_field_values(page):
    """All field keys currently exposed by the page."""
    values = []
    try:
        hidden = page.locator("input[type='hidden'][name='field']")
        for i in range(hidden.count()):
            value = safe_text(hidden.nth(i).input_value())
            if value:
                values.append(value)
    except Exception:
        pass
    return values


def render_for_prompt(missing_fields):
    """Format the ADD MISSING list for the research prompt."""
    if not missing_fields:
        return "FIELDS AVAILABLE TO ADD: none on this record."

    listed = ", ".join(f'"{entry["label"]}"' for entry in missing_fields)
    return (
        "FIELDS AVAILABLE TO ADD (currently missing, but the agent CAN create and fill them):\n"
        f"{listed}\n"
        "You MAY propose a value for any of these when research verifies one. Use the label "
        "exactly as written above in the \"field\" key. Only propose a field here if you have "
        "a verified value - never propose a guess, and never propose one just to fill a gap."
    )
