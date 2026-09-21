"""Direct HTTP transport for the Aekovera QA app.

WHY THIS EXISTS
---------------
Everything the review page does is a plain HTML form POST.  Reading
``qa_app/app.py`` in the platform repo:

    Route("/verdict", verdict, methods=["POST"])
    Route("/edit",    edit,    methods=["POST"])
    Route("/skip",    skip,    methods=["POST"])
    Route("/undo",    undo,    methods=["POST"])

Driving those endpoints directly removes the DOM round-trip that dominated
the old runtime (open the pencil, wait for the form, fill, click Save, wait
for the redirect, re-read the card to verify) and replaces it with one
request per field.  Nothing in the platform repo changes: this speaks the
exact protocol the browser already speaks.

THE SAFETY PROPERTY THAT MATTERS
--------------------------------
``db.save_verdict`` requires BOTH the unit_id and the card's nonce, and
refuses anything else::

    if existing or row["status"] == "done":
        return "already"
    if (row["leased_by"] != member or row["nonce"] != nonce
            or not row["leased_until"] or row["leased_until"] <= now):
        return "lease_lost"

So a verdict POST is bound to one specific company.  Replaying it is a
no-op that returns "already".  It is structurally incapable of landing on
the next company -- which is exactly the failure the keyboard path had.

TWO THINGS THIS MODULE MUST NEVER DO
------------------------------------
1. Follow redirects.  Every handler answers 303 -> ``/review``, and
   ``GET /review`` calls ``db.claim_next``, which LEASES A CARD.  Following
   a redirect would lease an extra company per POST and orphan it for the
   lease duration.  Every request here is sent with ``max_redirects=0`` and
   the outcome is read from the Location header instead.
2. Retry a verdict blindly.  See ``submit_verdict``.
"""

import time
from urllib.parse import parse_qs, unquote_plus, urlparse

from review_hub.config import BASE_URL


def valid_url(value) -> bool:
    """Mirror of html.valid_url in the platform: absolute http(s) with a host."""
    try:
        parsed = urlparse((value or "").strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False

# ---------------------------------------------------------------------------
# Decision -> verdict mapping.
#
# From the v4 rulebook's own table, and the button labels in the review UI:
#
#   ACCEPT     -> Platform ready (G)  -> green
#   PARK       -> Outreach first (O)  -> orange
#   RE_ENRICH  -> Re-enrich (Y)       -> yellow
#   REJECT     -> Reject (R)          -> red
#
# app.py validates this server-side:
#   if choice not in ("green", "orange", "yellow", "red")
# ---------------------------------------------------------------------------
DECISION_TO_VERDICT = {
    "ACCEPT": "green",
    "PARK": "orange",
    "RE_ENRICH": "yellow",
    "REJECT": "red",
}

# MANUAL_REVIEW is deliberately absent: it is not a verdict.  It leaves the
# record undecided via /skip, matching the old agent's behaviour.

# Mirrors db.EDITABLE_FIELDS.  A field outside this set is rejected by
# _valid_edit() server-side with "That field cannot be edited."; checking it
# here too turns a wasted round-trip into an immediate, clearer error.
EDITABLE_FIELDS = frozenset((
    "primary_email", "primary_phone", "general_email", "website_url",
    "linkedin_url", "supplier_type", "specialty", "products", "description",
    "street_address", "city", "state", "zip", "country", "dba_name",
    "company_name",
))

# Outcomes save_verdict can return, as surfaced in the redirect message.
VERDICT_SAVED = "saved"
VERDICT_ALREADY = "already"
VERDICT_LEASE_LOST = "lease_lost"
VERDICT_MISSING = "missing"

_MESSAGE_TO_OUTCOME = {
    "saved — next supplier ready.": VERDICT_SAVED,
    "saved - next supplier ready.": VERDICT_SAVED,
    "already recorded.": VERDICT_ALREADY,
    "that review lease expired; another card is ready.": VERDICT_LEASE_LOST,
    "supplier not found.": VERDICT_MISSING,
}


class QAHttpError(RuntimeError):
    """A request failed in a way the caller must handle."""


class QATransientError(QAHttpError):
    """A failure that is worth retrying (connection reset, 5xx, timeout)."""


class QASessionExpired(QAHttpError):
    """The session cookie is no longer valid -- a human must log in again."""


def _message_from_location(location: str) -> str:
    """Pull the human-readable message out of a 303 Location header."""
    if not location:
        return ""
    query = parse_qs(urlparse(location).query)
    raw = (query.get("message") or [""])[0]
    return unquote_plus(raw).strip()


def _classify_verdict_message(message: str) -> str:
    key = message.strip().lower()
    if key in _MESSAGE_TO_OUTCOME:
        return _MESSAGE_TO_OUTCOME[key]
    # Be tolerant of wording drift on the platform side rather than crashing.
    if "already" in key:
        return VERDICT_ALREADY
    if "lease" in key:
        return VERDICT_LEASE_LOST
    if "not found" in key:
        return VERDICT_MISSING
    if "saved" in key or "next supplier" in key:
        return VERDICT_SAVED
    return ""


class QAClient:
    """Speaks the review app's form protocol over the browser's own session.

    Built from a Playwright ``BrowserContext``: ``context.request`` shares the
    context's cookie jar, so the agent inherits the operator's existing login
    and shift.  No credentials are handled here and none are stored.
    """

    def __init__(self, context, base_url: str = BASE_URL, timeout_ms: int = 20000,
                 max_retries: int = 3, backoff_s: float = 0.5, log=print):
        self._request = context.request
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._log = log
        self.stats = {"edits_ok": 0, "edits_failed": 0, "verdicts": 0,
                      "skips": 0, "retries": 0, "request_seconds": 0.0,
                      "source_url_dropped": 0}

    # -- low level ---------------------------------------------------------

    def _post(self, path: str, form: dict, *, retry: bool) -> tuple[int, str]:
        """POST a form without following redirects. Returns (status, message).

        ``retry`` is a caller decision, not a property of the endpoint: an
        idempotent edit may be retried, a verdict may not (see submit_verdict).
        """
        url = f"{self.base_url}{path}"
        attempts = self.max_retries if retry else 1
        last_exc = None

        for attempt in range(1, attempts + 1):
            started = time.time()
            try:
                response = self._request.post(
                    url,
                    form={k: ("" if v is None else str(v)) for k, v in form.items()},
                    max_redirects=0,          # never lease an extra card
                    timeout=self.timeout_ms,
                )
                self.stats["request_seconds"] += time.time() - started
                status = response.status

                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if "/login" in location:
                        raise QASessionExpired(
                            "The QA app redirected to /login: the session cookie "
                            "expired or the member was deactivated. Log in again "
                            "in the browser window and restart the run."
                        )
                    return status, _message_from_location(location)

                if status >= 500:
                    raise QATransientError(f"{path} returned HTTP {status}")
                if status == 200:
                    # A 200 from a form handler means it re-rendered instead of
                    # redirecting -- treat as a hard failure, not a success.
                    raise QAHttpError(f"{path} returned HTTP 200 (expected a 303 redirect)")
                raise QAHttpError(f"{path} returned HTTP {status}")

            except QASessionExpired:
                raise
            except QAHttpError as exc:
                last_exc = exc
                if not isinstance(exc, QATransientError) or attempt == attempts:
                    raise
            except Exception as exc:                      # network-level failure
                self.stats["request_seconds"] += time.time() - started
                last_exc = QATransientError(f"{path}: {exc}")
                if attempt == attempts:
                    raise last_exc from exc

            self.stats["retries"] += 1
            delay = self.backoff_s * (2 ** (attempt - 1))
            self._log(f"  ↻ {path} attempt {attempt}/{attempts} failed "
                      f"({last_exc}); retrying in {delay:.1f}s")
            time.sleep(delay)

        raise last_exc or QAHttpError(f"{path} failed")

    # -- edits -------------------------------------------------------------

    def apply_edit(self, unit_id, field: str, new_value: str,
                   source_url: str = "") -> tuple[bool, str]:
        """Save one field correction. Returns (ok, message).

        Safe to retry: ``db.save_edit`` appends a row to ``qa.edits`` and the
        writeback applies the latest value per field, so a duplicate write of
        the SAME value is harmless.  A failure here is reported, never
        silently swallowed -- an unreported failed edit is how bad data
        reaches the live record.
        """
        if field not in EDITABLE_FIELDS:
            return False, f"{field} is not an editable field"
        if new_value is None or str(new_value).strip() == "":
            return False, f"{field}: refusing to write an empty value"

        form = {"unit_id": unit_id, "field": field, "new_value": str(new_value)}
        # source_url is optional server-side, but if sent it must be an
        # absolute http(s) URL or the WHOLE edit is refused ("Source URL must
        # start with http:// or https://"). The judge often cites provenance
        # that is not a URL ("maps card", "search results"); that is still
        # useful in the log, but it must not cost the edit.
        if source_url:
            if valid_url(source_url):
                form["source_url"] = source_url.strip()
            else:
                self.stats["source_url_dropped"] += 1
                self._log(f"  \u2139 {field}: source {source_url!r} is not a URL; "
                          f"saving the edit without it")
        # supplier_type is read from new_value_multi when present; sending the
        # pipe-joined string in new_value is the documented fallback path.

        _, message = self._post("/edit", form, retry=True)
        ok = "saved" in message.lower()
        self.stats["edits_ok" if ok else "edits_failed"] += 1
        return ok, message or "no message returned"

    def clear_field(self, unit_id, field: str, reason: str = "") -> tuple[bool, str]:
        """Blank a contaminated field. Returns (ok, message).

        qa.edits.new_value is TEXT NOT NULL, so "" is a legal value; the only
        server-side block is company_name (< 2 chars refused), which is right:
        a record must keep its identity. ``reason`` is logged locally; the
        /edit form has no free-text field for it.
        """
        if field not in EDITABLE_FIELDS:
            return False, f"{field} is not an editable field"
        if field == "company_name":
            return False, "company_name cannot be cleared (identity field)"
        form = {"unit_id": unit_id, "field": field, "new_value": ""}
        if reason:
            self._log(f"  clearing {field}: {reason}")
        _, message = self._post("/edit", form, retry=True)
        ok = "saved" in message.lower()
        self.stats["edits_ok" if ok else "edits_failed"] += 1
        return ok, message or "no message returned"

    # -- verdicts ----------------------------------------------------------

    def submit_verdict(self, unit_id, nonce: str, decision: str,
                       note: str = "", suggested_url: str = "",
                       seconds_spent: int = 0) -> str:
        """Record the final decision for ONE company. Returns an outcome string.

        This is the single most dangerous call in the agent, so it is the
        least forgiving:

        * The verdict is bound to ``unit_id`` + ``nonce``.  It cannot apply to
          any other company, whatever the browser happens to be showing.
        * It is NEVER retried.  A timeout is genuinely ambiguous -- the write
          may well have committed -- and a blind retry is the old keyboard
          bug in a new costume.  On an ambiguous failure the caller is told to
          stop and look, which is the correct response to "I do not know
          whether a company was just accepted".
        * ``already`` is returned, not raised: it means this exact company was
          already decided, which is a safe no-op rather than an error.
        """
        decision = (decision or "").strip().upper()
        if decision not in DECISION_TO_VERDICT:
            raise QAHttpError(
                f"{decision!r} is not a verdict. Valid: "
                f"{', '.join(sorted(DECISION_TO_VERDICT))}. "
                "MANUAL_REVIEW must go through skip_record() instead."
            )
        if not unit_id:
            raise QAHttpError("refusing to submit a verdict without a unit_id")
        if not nonce:
            raise QAHttpError(
                "refusing to submit a verdict without the card's nonce -- "
                "without it the server cannot confirm which company this is for"
            )

        form = {
            "unit_id": unit_id,
            "nonce": nonce,
            "verdict": DECISION_TO_VERDICT[decision],
            "note": note or "",
            "seconds_spent": max(0, int(seconds_spent or 0)),
        }
        if suggested_url:
            form["suggested_url"] = suggested_url

        try:
            _, message = self._post("/verdict", form, retry=False)
        except QATransientError as exc:
            raise QAHttpError(
                f"The verdict for unit {unit_id} failed in an ambiguous way "
                f"({exc}). It was NOT retried, because the write may already "
                f"have committed. Check this company in the QA app before "
                f"resuming."
            ) from exc

        outcome = _classify_verdict_message(message)
        if not outcome:
            raise QAHttpError(
                f"Unrecognised response to the verdict for unit {unit_id}: "
                f"{message!r}. Treating as unknown rather than assuming success."
            )
        if outcome == VERDICT_SAVED:
            self.stats["verdicts"] += 1
        return outcome

    # -- skip / undo -------------------------------------------------------

    def skip_record(self, unit_id, nonce: str) -> bool:
        """Leave a record undecided (used for MANUAL_REVIEW and hard failures)."""
        if not unit_id or not nonce:
            raise QAHttpError("skip requires both unit_id and nonce")
        _, message = self._post("/skip", {"unit_id": unit_id, "nonce": nonce},
                                retry=False)
        ok = "skipped" in message.lower()
        if ok:
            self.stats["skips"] += 1
        return ok

    def undo_last(self) -> str:
        """Reopen this member's last verdict (manual recovery helper)."""
        _, message = self._post("/undo", {}, retry=False)
        return message

    # -- reporting ---------------------------------------------------------

    def summary(self) -> str:
        s = self.stats
        total = s["edits_ok"] + s["edits_failed"]
        rate = (s["edits_ok"] / total * 100) if total else 100.0
        return (f"HTTP transport: {s['edits_ok']}/{total} edits saved "
                f"({rate:.0f}%), {s['verdicts']} verdicts, {s['skips']} skips, "
                f"{s['retries']} retries, {s['source_url_dropped']} non-URL sources dropped, "
                f"{s['request_seconds']:.1f}s in requests")

    def health_warnings(self) -> list[str]:
        """Conditions worth interrupting a run for."""
        warnings = []
        s = self.stats
        total = s["edits_ok"] + s["edits_failed"]
        if total >= 10 and s["edits_failed"] / total > 0.25:
            warnings.append(
                f"{s['edits_failed']} of {total} edits failed (>25%). "
                "Stop and check the app before continuing -- this usually means "
                "the lease is expiring or a field validation rule changed."
            )
        if s["retries"] >= 15:
            warnings.append(
                f"{s['retries']} retries so far; the app or the link to it is "
                "unhealthy. Throughput numbers below are not meaningful."
            )
        return warnings


def read_card_identity(page):
    """Read unit_id and nonce from the review page's hidden inputs.

    html.py renders them on the verdict form:
        <input type="hidden" name="unit_id" value="...">
        <input type="hidden" name="nonce"   value="...">

    These two values are what bind every subsequent write to this company, so
    a missing nonce is an error rather than something to paper over.
    """
    def _value(name):
        locator = page.locator(f"input[name='{name}']")
        if not locator.count():
            return ""
        return (locator.first.get_attribute("value") or "").strip()

    unit_id, nonce = _value("unit_id"), _value("nonce")
    if not unit_id:
        raise QAHttpError(
            "No unit_id on the review page -- the agent cannot tell which "
            "company is displayed. Is a card actually loaded?"
        )
    if not nonce:
        raise QAHttpError(
            f"No nonce on the review page for unit {unit_id}. Without it the "
            "server cannot verify the lease, so no write will be attempted."
        )
    return unit_id, nonce
