"""Collect web evidence for a supplier record.

WHY THIS EXISTS
---------------
In the manual pipeline, ChatGPT did the browsing. OpenRouter free models
have NO web access - they only see the prompt. Handing them the old
"independently research this company" prompt with no evidence would turn
a verification step into a hallucination generator, and the scope gate in
main.py would then be validating invented facts.

So the agent now does the browsing itself and passes the retrieved page
text to the model as quoted evidence. The model's job becomes reading and
judging evidence rather than recalling facts.

Fetches run in PARALLEL inside an isolated secondary browser context so
they never disturb the review-page session. Wall-clock time is roughly the
longest single fetch instead of the sum of all three.

Everything here is best effort: if a fetch fails, the model is told the
evidence is missing, and the scope gate rejects on insufficient evidence
rather than guessing.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus, urlparse

from config import (
    ENABLE_WEB_EVIDENCE,
    EVIDENCE_PAGE_CHARS,
    EVIDENCE_TIMEOUT_MS,
    EVIDENCE_SEARCH_URL,
    EVIDENCE_CONTACT_PATHS,
    WEBSITE_VERIFY_TIMEOUT_MS,
)
from jsonutil import safe_text


def _clean(text, limit):
    text = re.sub(r"\n{3,}", "\n\n", safe_text(text))
    text = re.sub(r"[ \t]{2,}", " ", text)
    if len(text) > limit:
        text = text[:limit] + "\n[TRUNCATED]"
    return text


def _normalize_url(raw):
    url = safe_text(raw)
    if not url or url.lower() in {"n/a", "none", "null", "-"}:
        return None
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url.lstrip("/")
    try:
        parsed = urlparse(url)
        if not parsed.netloc or "." not in parsed.netloc:
            return None
    except Exception:
        return None
    return url


def _fetch(context, url, limit, timeout_ms=None):
    """Open a URL in a throwaway tab and return its visible text."""
    timeout_ms = timeout_ms or EVIDENCE_TIMEOUT_MS
    tab = None
    try:
        tab = context.new_page()
        tab.set_default_timeout(timeout_ms)
        response = tab.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        status = response.status if response else None
        if status and status >= 400:
            return {"url": url, "status": status, "text": "", "error": f"HTTP {status}"}
        tab.wait_for_timeout(250)
        text = tab.locator("body").inner_text()
        final_url = tab.url
        try:
            title = tab.title()
        except Exception:
            title = ""
        return {
            "url": url,
            "final_url": final_url,
            "status": status,
            "title": safe_text(title),
            "text": _clean(text, limit),
            "error": None,
        }
    except Exception as exc:
        return {"url": url, "status": None, "text": "", "error": str(exc)[:200]}
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:
                pass


def _fetch_job(browser, url, limit, timeout_ms, label):
    """Run a single fetch inside its own short-lived context (thread-safe)."""
    ctx = None
    try:
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        result = _fetch(ctx, url, limit, timeout_ms=timeout_ms)
        result["label"] = label
        return result
    except Exception as exc:
        return {
            "url": url,
            "status": None,
            "text": "",
            "error": str(exc)[:200],
            "label": label,
        }
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


# Words that belong to the review application itself, not to a supplier.
APP_CHROME_WORDS = {
    "aekovera", "qa", "review", "reviews", "supplier", "suppliers", "record",
    "records", "dashboard", "login", "logout", "admin", "platform", "ready",
    "skip", "reject", "accept", "correction", "corrections", "edit", "save",
    "next", "previous", "pending", "queue", "loading", "untitled",
}


def _name_from_domain(url):
    """Derive a searchable company name from its domain.

    hongdaamerica.com -> "hongdaamerica". This is far more reliable than
    scraping a heading off the review page, which yields the app's own
    title (e.g. "Aekovera QA") and produces useless search results.
    """
    normalized = _normalize_url(url)
    if not normalized:
        return ""
    host = urlparse(normalized).netloc.lower()
    host = re.sub(r"^www\.", "", host)
    parts = host.split(".")
    if not parts:
        return ""
    name = parts[0]
    if name in {"", "com", "co"} or len(name) < 3:
        return ""
    return name


def _is_app_chrome(text):
    tokens = [t for t in re.split(r"[^a-z0-9]+", safe_text(text).lower()) if t]
    if not tokens:
        return True
    return all(t in APP_CHROME_WORDS for t in tokens)


def _guess_company_name(record):
    """Find a searchable company name for this supplier record.

    Order of preference: an explicit name field, the site's own <title>
    (captured during the website fetch), the domain, then a page-context
    line that is not application chrome.
    """
    fields = record.get("fields", {})
    for key in ("company_name", "name", "dba", "legal_name"):
        value = safe_text(fields.get(key))
        if value and not _is_app_chrome(value):
            return value

    # The title of the fetched website is usually the company's own name.
    title = safe_text(record.get("_website_title"))
    if title and not _is_app_chrome(title):
        # Trim marketing suffixes: "Hongda America | Food Equipment" -> first part.
        return re.split(r"\s*[|\-–—:]\s*", title)[0].strip()[:120]

    domain_name = _name_from_domain(fields.get("website_url"))
    if domain_name:
        return domain_name

    for line in safe_text(record.get("page_context")).splitlines():
        line = line.strip()
        if len(line) > 2 and not _is_app_chrome(line):
            return line[:120]
    return ""


def collect(context, record):
    """Return an evidence bundle for the current record.

    Fetches run in parallel inside isolated secondary browser contexts so
    the review-page session is never touched. Wall-clock time ≈ longest
    single fetch instead of the sum of all fetches.

    context: the Playwright browser context (page.context) — used only to
             obtain the parent browser object for launching secondary contexts.
    record:  the dict produced by extract_record()
    """
    if not ENABLE_WEB_EVIDENCE:
        return {"enabled": False, "sources": [], "company_name_guess": ""}

    website = _normalize_url(record.get("fields", {}).get("website_url"))
    name = _guess_company_name(record)

    # Build the job list. Contact/about is derived from the website base URL
    # and can be launched at the same time as the homepage + search.
    jobs = []  # (url, limit, timeout_ms, label)

    if website:
        jobs.append((
            website,
            EVIDENCE_PAGE_CHARS,
            EVIDENCE_TIMEOUT_MS,
            "WEBSITE ON FILE (untrusted - verify it matches the company)",
        ))
        base = website.rstrip("/")
        # Kick off the first contact/about candidate in parallel too.
        # (We only keep the first one that returns real content.)
        contact_path = EVIDENCE_CONTACT_PATHS[0] if EVIDENCE_CONTACT_PATHS else "contact"
        jobs.append((
            f"{base}/{contact_path.lstrip('/')}",
            EVIDENCE_PAGE_CHARS // 2,
            EVIDENCE_TIMEOUT_MS,
            f"CONTACT/ABOUT PAGE ({contact_path})",
        ))

    if name:
        query = f"{name} official site contact"
        search_url = EVIDENCE_SEARCH_URL.format(query=quote_plus(query))
        jobs.append((
            search_url,
            EVIDENCE_PAGE_CHARS,
            EVIDENCE_TIMEOUT_MS,
            f"WEB SEARCH RESULTS for: {query}",
        ))

    if not jobs:
        print("  Evidence collected: 0 usable source(s) of 0 attempted.")
        return {
            "enabled": True,
            "company_name_guess": name,
            "sources": [],
            "usable_count": 0,
        }

    for url, _, _, label in jobs:
        print(f"  → Queued: {label}  ({url})")

    # Use the parent browser so secondary contexts share the same Chromium
    # process (cheap) but remain fully isolated from the review session.
    browser = context.browser
    sources = []

    with ThreadPoolExecutor(max_workers=min(3, len(jobs))) as pool:
        futures = {
            pool.submit(_fetch_job, browser, url, limit, timeout, label): label
            for url, limit, timeout, label in jobs
        }
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception as exc:
                label = futures[fut]
                result = {
                    "url": "",
                    "status": None,
                    "text": "",
                    "error": str(exc)[:200],
                    "label": label,
                }
            sources.append(result)
            status = "ok" if result.get("text") else f"fail ({result.get('error') or 'empty'})"
            print(f"  ✓ Done: {result.get('label')}  [{status}]")

    # Prefer a successful contact/about page; if the first candidate failed,
    # try the remaining contact paths sequentially (rare, cheap).
    has_contact = any(
        s.get("text") and "CONTACT/ABOUT" in (s.get("label") or "") for s in sources
    )
    if website and not has_contact and len(EVIDENCE_CONTACT_PATHS) > 1:
        base = website.rstrip("/")
        for path in EVIDENCE_CONTACT_PATHS[1:]:
            candidate = f"{base}/{path.lstrip('/')}"
            print(f"  → Fallback contact: {candidate}")
            page_result = _fetch(context, candidate, EVIDENCE_PAGE_CHARS // 2)
            if page_result.get("text"):
                page_result["label"] = f"CONTACT/ABOUT PAGE ({path})"
                sources.append(page_result)
                break

    # Propagate website title back into the record for better name guessing
    # on subsequent logic that may re-use the record dict.
    for s in sources:
        if s.get("label", "").startswith("WEBSITE ON FILE") and s.get("title"):
            record["_website_title"] = s["title"]
            break

    usable = [s for s in sources if s.get("text")]
    print(f"  Evidence collected: {len(usable)} usable source(s) of {len(sources)} attempted (parallel).")

    return {
        "enabled": True,
        "company_name_guess": name,
        "sources": sources,
        "usable_count": len(usable),
    }


# Generic corporate-suffix/filler words, stripped before comparing a company
# name's tokens against a fetched page - keeping them in would let almost
# any two "... Inc." companies "match" on the word "inc" alone.
_COMPANY_STOPWORDS = {
    "the", "inc", "incorporated", "llc", "l.l.c", "co", "corp", "corporation",
    "company", "companies", "ltd", "limited", "group", "holdings", "dba",
    "brand", "brands", "foods", "food", "enterprises", "industries",
    "international", "usa", "us", "na", "and", "of", "a",
}


def _significant_tokens(name):
    """Lowercase, punctuation-stripped identity tokens for a company name,
    with generic corporate filler words removed. "North American Baking,
    Inc." -> ["north", "american", "baking"]."""
    tokens = re.split(r"[^a-z0-9]+", safe_text(name).lower())
    return [t for t in tokens if len(t) >= 3 and t not in _COMPANY_STOPWORDS]


def website_matches_company(context, company_name, url, limit=4000):
    """Best-effort independent check: does the company's OWN name actually
    appear anywhere on the page at `url`?

    This is deliberately cheap and approximate - a real fetch and a simple
    token-overlap check, not a verdict of legal identity. Its whole job is
    to catch the specific failure mode that let a Hain Celestial snack-brand
    domain get written onto an unrelated Cabot, AR company: a domain that
    shares literally none of the company's identifying words is almost
    never the company's real site, whatever a research step concluded.

    Returns (verdict, detail):
        verdict = True   -> at least one significant name token was found
                             on the fetched page.
        verdict = False  -> the page fetched cleanly but contains NONE of
                             the company's significant name tokens.
        verdict = None   -> inconclusive (no usable company-name tokens to
                             check, an unparseable URL, or the fetch itself
                             failed/timed out). NEVER treated as a mismatch
                             by the caller - a network hiccup must not
                             silently discard a real correction.
    """
    tokens = _significant_tokens(company_name)
    if not tokens:
        return None, "no significant company-name tokens available to check against"

    normalized = _normalize_url(url)
    if not normalized:
        return None, f"not a fetchable URL: {url!r}"

    # Its own timeout (WEBSITE_VERIFY_TIMEOUT_MS) rather than
    # EVIDENCE_TIMEOUT_MS: this check runs once per proposed website
    # change (potentially several per record), not once per record, so
    # operators may want it tighter without affecting the main evidence
    # collection pass.
    result = _fetch(context, normalized, limit, timeout_ms=WEBSITE_VERIFY_TIMEOUT_MS)
    if result.get("error") or not (safe_text(result.get("text")) or safe_text(result.get("title"))):
        return None, f"fetch failed: {result.get('error') or 'no content returned'}"

    title = safe_text(result.get("title"))
    text = safe_text(result.get("text"))
    haystack = f"{title} {text}".lower()
    hits = [t for t in tokens if t in haystack]
    final_url = result.get("final_url") or normalized
    if hits:
        return True, f"found token(s) {hits} on {final_url}"

    # Bot-walls / challenge pages often contain zero company tokens even when
    # the domain is correct. Treat them as inconclusive so a clean record is
    # not held solely because Cloudflare or a similar wall appeared.
    challenge_markers = (
        "client challenge", "just a moment", "checking your browser",
        "attention required", "enable javascript", "cloudflare",
        "access denied", "please wait", "verifying you are human",
        "security check", "captcha", "bot detection",
    )
    if any(m in haystack for m in challenge_markers):
        return None, (
            f"fetch hit a challenge/bot-wall page (title: {title[:80]!r}); "
            "treating as inconclusive rather than a mismatch"
        )

    return False, (
        f"none of the company's name tokens {tokens} appear anywhere on "
        f"{final_url} (page title: {title[:80]!r})"
    )


def render(evidence):
    """Format the evidence bundle for inclusion in the research prompt."""
    if not evidence or not evidence.get("enabled"):
        return (
            "WEB EVIDENCE: DISABLED.\n"
            "No independent evidence was retrieved. You cannot browse the web. "
            "Unless the record itself contains conclusive proof of a food/beverage/dietary-"
            "supplement CPG connection in an accepted product area, you MUST return "
            "decision=REJECT with scope_match=false and changes=[]. Being non-US, or having "
            "an uncertain country, is NOT a reason to reject - see LOCATION RULE."
        )

    usable = [s for s in evidence.get("sources", []) if s.get("text")]
    if not usable:
        return (
            "WEB EVIDENCE: NONE RETRIEVED (all fetches failed or returned nothing).\n"
            "You cannot browse the web and no evidence is available. You MUST return "
            "decision=REJECT, scope_match=false, changes=[], and a reason stating that "
            "the company could not be independently verified."
        )

    blocks = []
    for i, source in enumerate(usable, 1):
        final_url = source.get("final_url") or source.get("url")
        blocks.append(
            f"--- EVIDENCE SOURCE {i}: {source.get('label', 'source')} ---\n"
            f"URL: {final_url}\n"
            f"CONTENT:\n{source['text']}\n"
        )

    failed = [s for s in evidence.get("sources", []) if not s.get("text")]
    failure_note = ""
    if failed:
        details = "; ".join(
            f"{s.get('url')} ({s.get('error') or 'no content'})" for s in failed
        )
        failure_note = f"\nFETCHES THAT FAILED (treat as no information): {details}\n"

    return (
        "WEB EVIDENCE RETRIEVED BY THE AGENT:\n"
        "You cannot browse. The text below was fetched live by the automation and is the "
        "ONLY external information you have. Base every factual claim on it. Do NOT rely on "
        "memory or assumptions about this company. If the evidence does not clearly establish "
        "a food/beverage/dietary-supplement CPG connection in an accepted product area, return "
        "REJECT. Being non-US, or having an uncertain/unconfirmed country, is NOT a reason to "
        "reject on its own - see LOCATION RULE; it only means an origin note is required.\n\n"
        + "\n".join(blocks)
        + failure_note
    )
