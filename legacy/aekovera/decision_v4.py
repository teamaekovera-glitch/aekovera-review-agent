"""Validation for the v4 judge output.

The v4 rulebook replaces the old three-way ACCEPT / REJECT / MANUAL_REVIEW
vocabulary with four verdicts that map one-to-one onto the review UI's four
buttons, plus a ``bucket`` that says *why* a record was parked.

The job of this module is narrow and deliberately suspicious: take whatever
JSON came back from the model and decide what the agent is allowed to DO with
it.  Everything it cannot vouch for is downgraded rather than guessed at.  The
guiding rule throughout is that the expensive mistake is a wrong ACCEPT -- a
company going live on the platform without a human having looked at it -- so
every ambiguity resolves away from ACCEPT.
"""

from pathlib import Path

VALID_DECISIONS = ("ACCEPT", "PARK", "RE_ENRICH", "REJECT")

VALID_BUCKETS = (
    "future_category", "foreign_brand", "cosmetic_ingredient",
    "big_brand_borderline", "no_site", "dead_traces", "thin_data",
    "unclear_farm", "social_only", "unverified_identity", "conflated", "other",
)

# Section 6 taxonomy. "Equipment / Services" is intentionally included because
# the rulebook requires it to be REPORTED on a REJECT -- but it can never
# qualify, which is enforced separately below.
TAXONOMY = (
    "Co-Manufacturer", "Co-Packer", "Private Label Manufacturer",
    "Contract R&D / Formulation", "Ingredient Supplier", "Packaging Supplier",
    "3PL / Fulfillment", "Food Manufacturer / Brand", "Distributor / Wholesaler",
    "Equipment / Services",
)

NON_QUALIFYING_TYPES = ("Equipment / Services",)

# Section 6: these four need literal on-site proof of work for other brands.
CONTRACT_TYPES = (
    "Co-Manufacturer", "Co-Packer", "Private Label Manufacturer",
    "Contract R&D / Formulation",
)

# Section 4: the contract types must be US-based. The rest may be anywhere.
US_REQUIRED_TYPES = CONTRACT_TYPES

PROMPT_PATH = Path(__file__).with_name("prompts") / "judge_v4.md"
BROWSING_SECTION_PATH = Path(__file__).with_name("prompts") / "material_browsing.md"

_SECTION_1_START = "## 1. Material and honesty"
_SECTION_2_START = "## 2. Decisions"


def load_prompt(browsing: bool = False) -> str:
    """Return the v4 rulebook text used as the system/rules block.

    v13.4: judge_v4.md section 1 was written for the API harness, which fetches
    the website and search results and attaches them as MATERIAL. It tells the
    model it has NO browsing and must not call anything verified unless it is
    in that material. The manual ChatGPT backend attaches no material at all,
    so a model obeying section 1 can never confirm identity or a contact and
    parks nearly every record (unverified_identity / thin_data).

    browsing=True swaps section 1 for prompts/material_browsing.md, which tells
    the model to do the searches and page opens itself. The rest of the
    rulebook is unchanged.
    """
    text = PROMPT_PATH.read_text(encoding="utf-8")
    if not browsing:
        return text
    start = text.find(_SECTION_1_START)
    end = text.find(_SECTION_2_START)
    if start < 0 or end < 0 or end <= start:
        raise RuntimeError(
            "judge_v4.md no longer has the expected '## 1.' / '## 2.' headings, "
            "so the browsing-mode section cannot be substituted. Refusing to send "
            "a prompt that tells ChatGPT it cannot browse.")
    browsing_section = BROWSING_SECTION_PATH.read_text(encoding="utf-8")
    return text[:start] + browsing_section.rstrip() + "\n\n" + text[end:]


def _normalize_identity(value) -> str:
    """Map a free-text site_identity onto confirmed / failed / unverifiable.

    The schema asks for one of three words, but ChatGPT often answers
    "Confirmed - name and address on contact page". An exact-match check
    turned every such answer into a PARK.
    """
    text = _text(value).lower()
    if not text:
        return ""
    head = text.replace("-", " ").replace(":", " ").split()[0].strip(".,;")
    if head in ("confirmed", "confirm", "verified", "matches", "match", "yes", "true"):
        return "confirmed"
    if head in ("failed", "fail", "mismatch", "no", "false", "wrong"):
        return "failed"
    if head in ("unverifiable", "unverified", "unknown", "unclear", "partial", "partially"):
        return "unverifiable"
    return text


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _as_bool(value):
    """Tolerant boolean read. Returns None when genuinely undecidable.

    The v4 prompt demands real JSON booleans, but a manual ChatGPT paste still
    sometimes answers a boolean field with a sentence. Returning None (rather
    than guessing False) lets the caller treat it as a missing field.
    """
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if not text:
        return None
    if text in ("true", "yes", "y", "1", "confirmed", "in scope", "in-scope"):
        return True
    if text in ("false", "no", "n", "0", "failed", "out of scope", "out-of-scope"):
        return False
    first = text.split()[0].strip(".,;:")
    if first in ("true", "yes", "confirmed", "strong", "clear", "definite"):
        return True
    if first in ("false", "no", "none", "unlikely"):
        return False
    return None


def parse_types(raw) -> list[str]:
    """Split a ' | '-joined supplier_type into known taxonomy labels."""
    if isinstance(raw, (list, tuple)):
        parts = [_text(p) for p in raw]
    else:
        parts = [p.strip() for p in _text(raw).split("|")]
    out, lowered = [], {t.lower(): t for t in TAXONOMY}
    for part in parts:
        if not part:
            continue
        canonical = lowered.get(part.lower())
        if canonical and canonical not in out:
            out.append(canonical)
    return out


class Judgement:
    """The validated, actionable form of one model answer."""

    def __init__(self, decision, bucket, types, changes, note,
                 suggested_url, downgrades, warnings, raw):
        self.decision = decision
        self.bucket = bucket
        self.types = types
        self.changes = changes
        self.note = note
        self.suggested_url = suggested_url
        self.downgrades = downgrades      # why the decision was lowered
        self.warnings = warnings          # non-fatal observations
        self.raw = raw

    @property
    def is_accept(self):
        return self.decision == "ACCEPT"

    @property
    def needs_human(self):
        """True when the agent must not finalize this record itself."""
        return self.decision == "MANUAL_REVIEW"

    def describe(self) -> str:
        line = self.decision + (f" [{self.bucket}]" if self.bucket else "")
        if self.downgrades:
            line += "  (downgraded: " + "; ".join(self.downgrades) + ")"
        return line


def validate(result: dict, *, allow_accept: bool = True) -> Judgement:
    """Turn raw model JSON into a Judgement, downgrading anything unproven.

    ``allow_accept=False`` forces every ACCEPT down to MANUAL_REVIEW; it is how
    Approval Mode and the various "hold" settings are expressed without
    duplicating this logic.
    """
    downgrades: list[str] = []
    warnings: list[str] = []

    decision = _text(result.get("decision")).upper().replace("-", "_").replace(" ", "_")
    if decision not in VALID_DECISIONS:
        return Judgement("MANUAL_REVIEW", "other", [], [],
                         f"Unusable decision {decision!r} from the research step.",
                         "", [f"{decision!r} is not one of {', '.join(VALID_DECISIONS)}"],
                         warnings, result)

    bucket = _text(result.get("bucket")).lower() or None
    if bucket in ("null", "none", ""):
        bucket = None
    types = parse_types(result.get("supplier_type"))
    type_quote = _text(result.get("type_quote"))
    scope_match = _as_bool(result.get("scope_match"))
    is_us = _as_bool(result.get("is_us_based"))
    site_identity = _normalize_identity(result.get("site_identity"))
    reason = _text(result.get("reason"))
    suggested_url = ""

    # A non-empty `needs` means the model asked for lookups instead of doing
    # them. Nothing in the agent fulfils `needs`, so the decision it returned
    # is only provisional -- surface that loudly rather than silently acting.
    needs = result.get("needs")
    if isinstance(needs, list) and needs:
        warnings.append(
            f"the judge requested lookups instead of doing them (needs={needs}); "
            "its decision is provisional -- in manual mode this means ChatGPT did not browse")

    # -- PARK needs a bucket (section 2) ----------------------------------
    if decision == "PARK":
        if not bucket:
            bucket = "other"
            warnings.append("PARK arrived without a bucket; recorded as 'other'.")
        elif bucket not in VALID_BUCKETS:
            warnings.append(f"Unknown bucket {bucket!r}; recorded as-is.")

    # -- checks that only bite an ACCEPT ----------------------------------
    if decision == "ACCEPT":
        # Scope decides ACCEPT vs REJECT and nothing overrides it (section 2).
        if scope_match is False:
            decision = "REJECT"
            downgrades.append("scope_match is false, and scope outranks everything else")
        elif scope_match is None:
            decision = "MANUAL_REVIEW"
            downgrades.append("scope_match was not a usable boolean")

        # A non-qualifying type can never be an ACCEPT (section 6).
        if decision == "ACCEPT" and any(t in NON_QUALIFYING_TYPES for t in types):
            decision = "REJECT"
            downgrades.append(
                f"{', '.join(t for t in types if t in NON_QUALIFYING_TYPES)} "
                "is never a qualifying type")

        # A contract type asserted without its verbatim proof (section 6, and
        # named again in the section 9 leak list).
        if decision == "ACCEPT":
            claimed = [t for t in types if t in CONTRACT_TYPES]
            if claimed and not type_quote:
                decision = "MANUAL_REVIEW"
                downgrades.append(
                    f"claims {claimed[0]} with no type_quote proving work for other brands")

        # Identity must be confirmed for an ACCEPT (section 2: "identity-verified").
        if decision == "ACCEPT" and site_identity and site_identity != "confirmed":
            decision = "PARK"
            bucket = bucket or "unverified_identity"
            downgrades.append(f"site_identity is {site_identity!r}, not 'confirmed'")

        # US-only types (section 4).
        if decision == "ACCEPT" and is_us is False:
            if any(t in US_REQUIRED_TYPES for t in types):
                decision = "PARK"
                bucket = bucket or "foreign_brand"
                downgrades.append(
                    "a contract type outside the US -- section 4 requires US-based")
            # Other types may be anywhere; foreignness never lowers the verdict.

        if decision == "ACCEPT" and not types:
            decision = "MANUAL_REVIEW"
            downgrades.append("no supplier type was classified")

    # An identity swap is explicitly a human call (section 5).
    if bucket == "conflated":
        if decision == "ACCEPT":
            decision = "PARK"
            downgrades.append("conflated records are never auto-accepted")

    # -- caller-level hold -------------------------------------------------
    if decision == "ACCEPT" and not allow_accept:
        decision = "MANUAL_REVIEW"
        downgrades.append("accepting is held for a human in this mode")

    # -- changes (section 7) ----------------------------------------------
    changes = _clean_changes(result.get("changes"), warnings)
    if decision == "REJECT":
        changes = []
    elif decision == "PARK" and bucket == "conflated":
        changes = []
    elif decision == "MANUAL_REVIEW":
        changes = []

    if decision == "RE_ENRICH":
        for change in changes:
            if change["field"] == "website_url":
                suggested_url = change["new_value"]
                break

    note = reason
    if decision == "MANUAL_REVIEW" and not note:
        note = "The research step did not give a usable reason; queued for a human."

    return Judgement(decision, bucket, types, changes, note, suggested_url,
                     downgrades, warnings, result)


def _clean_changes(raw, warnings: list) -> list[dict]:
    """Keep only changes that carry their evidence (section 7).

    'A change without both [source_url and quote] is dropped by the verifier' --
    so drop it here, loudly, rather than writing an unevidenced value.
    """
    if not isinstance(raw, list):
        return []
    kept = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        field = _text(item.get("field"))
        if not field:
            continue

        if item.get("clear") is True:
            clear_reason = _text(item.get("clear_reason"))
            if not clear_reason:
                warnings.append(f"{field}: clear requested with no clear_reason; skipped")
                continue
            kept.append({"field": field, "new_value": "", "clear": True,
                         "clear_reason": clear_reason, "source_url": "",
                         "quote": ""})
            continue

        new_value = item.get("new_value")
        if new_value is None or _text(new_value) == "":
            continue                                  # null/"" = do not touch

        source_url, quote = _text(item.get("source_url")), _text(item.get("quote"))
        if not source_url or not quote:
            warnings.append(
                f"{field}: dropped, missing "
                f"{'source_url' if not source_url else 'quote'}")
            continue

        kept.append({"field": field, "new_value": _text(new_value), "clear": False,
                     "clear_reason": "", "source_url": source_url, "quote": quote})
    return kept
