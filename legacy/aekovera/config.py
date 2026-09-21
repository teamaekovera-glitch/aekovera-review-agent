import os

BASE_URL = "https://qa.37-27-139-42.sslip.io"
REVIEW_URL = BASE_URL + "/review"
PROFILE_DIR = "browser_profile"

# Safety defaults for the pilot.
MAX_RECORDS = 5
APPROVAL_DEFAULT = True


# ---------------------------------------------------------------------------
# OpenRouter (replaces the manual ChatGPT copy/paste step)
# ---------------------------------------------------------------------------

# Never hardcode the key. Set it in the environment before running:
#   PowerShell:  $env:OPENROUTER_API_KEY = "sk-or-v1-..."
#   bash:        export OPENROUTER_API_KEY="sk-or-v1-..."
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Free model IDs end in ":free". The free roster ROTATES and IDs get delisted
# without notice, so this is a fallback chain, not a single choice: the client
# tries each in order and moves on after a 404/402/429.
#
# Verify the current list at https://openrouter.ai/models?max_price=0 and edit
# this list when a model stops working. "openrouter/free" is the auto-router
# that picks any available free model, which makes it a good last resort.
# Verified against the live OpenRouter catalogue on 2026-08-20. These are
# PREFERENCES, not a fixed list: with OPENROUTER_AUTO_DISCOVER on, the agent
# queries /api/v1/models at startup, keeps only models that are genuinely $0
# right now, and ranks these IDs first if they are still free.
OPENROUTER_MODELS = [
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "openai/gpt-oss-20b:free",
    "openrouter/free",
]

# Discover currently-free models at startup instead of trusting the list
# above. Free IDs graduate to paid without notice - this is what stops the
# "unavailable for free, use the paid slug" 404 wall.
OPENROUTER_AUTO_DISCOVER = True
OPENROUTER_MAX_CANDIDATES = 4      # how many discovered models to keep in the chain
MIN_CONTEXT_TOKENS = 60000         # record + several pages of fetched evidence

OPENROUTER_TIMEOUT = 120          # seconds per request
OPENROUTER_MAX_RETRIES = 3        # attempts per model before falling through

# Free tier allows roughly 20 requests/minute. 3.5s between calls keeps us
# comfortably under it, including the retries.
OPENROUTER_MIN_INTERVAL = 3.5

# Low temperature: this is verification work, not creative writing.
OPENROUTER_TEMPERATURE = 0.1
OPENROUTER_MAX_TOKENS = 2000

# Optional attribution headers shown on the OpenRouter dashboard.
OPENROUTER_REFERER = "https://aekovera.com"
OPENROUTER_TITLE = "Aekovera Review Agent"

# Every prompt and response is written here for auditing.
LLM_LOG_DIR = "llm_logs"


# ---------------------------------------------------------------------------
# Web evidence
# ---------------------------------------------------------------------------
# Free models cannot browse. The agent fetches pages itself and passes the
# text to the model as quoted evidence. Turning this off means the model has
# no external information and will reject almost everything by design.
ENABLE_WEB_EVIDENCE = True
EVIDENCE_PAGE_CHARS = 6000        # per-source character cap
EVIDENCE_TIMEOUT_MS = 20000
EVIDENCE_SEARCH_URL = "https://duckduckgo.com/html/?q={query}"
EVIDENCE_CONTACT_PATHS = ["contact", "contact-us", "about", "about-us"]


# ---------------------------------------------------------------------------
# Pipeline behaviour
# ---------------------------------------------------------------------------
# "manual" -> ChatGPT clipboard workflow (default; ChatGPT does the browsing)
# "api"    -> automated via OpenRouter, kept available but not the default
RESEARCH_BACKEND = "manual"

# In fully automated runs, what to do when research fails for one record:
# True  -> skip that record and continue the batch
# False -> stop the session immediately (the old, most conservative behaviour)
CONTINUE_ON_RESEARCH_FAILURE = True


# ---------------------------------------------------------------------------
# Scope: non-US companies
# ---------------------------------------------------------------------------
# Non-US companies are now ACCEPTED, but their supply origin must be recorded.
ACCEPT_NON_US = True

# Every accepted non-US company is appended here, so there is a durable record
# of supply origin even if the review UI has nowhere to store the note.
NON_US_LOG = "accepted_non_us.csv"


# ---------------------------------------------------------------------------
# Manual review queue
# ---------------------------------------------------------------------------
# When the model cannot confidently establish scope after real research, it
# may return decision: "MANUAL_REVIEW" instead of forcing an ACCEPT/REJECT
# guess. These records are never auto-decided: no field corrections are
# applied and neither Platform ready nor Reject is clicked. They are logged
# here for a human reviewer to resolve and update the record directly.
#
# Set False to go back to the old behaviour: MANUAL_REVIEW is downgraded to
# REJECT by the code gate (the model may still emit it; the prompt is not
# rewritten based on this flag, same as ACCEPT_NON_US above).
ENABLE_MANUAL_REVIEW = True
MANUAL_REVIEW_LOG = "manual_review_queue.csv"


# ---------------------------------------------------------------------------
# Manual (ChatGPT) workflow speed-ups
# ---------------------------------------------------------------------------
# PROJECT MODE: paste the standing rules into a ChatGPT Project's custom
# instructions ONCE (the agent writes them to PROJECT_INSTRUCTIONS_FILE at
# startup). Each record then pastes only its own payload - roughly 1.5k chars
# instead of 22k. Set False to keep pasting the full rulebook every time.
CHATGPT_PROJECT_MODE = True
PROJECT_INSTRUCTIONS_FILE = "chatgpt_project_instructions.txt"

# Watch the clipboard and pick up ChatGPT's answer the moment you copy it,
# instead of requiring an ENTER press. Saves a keystroke and an alt-tab.
CLIPBOARD_AUTO_WATCH = True
CLIPBOARD_POLL_INTERVAL = 0.4     # seconds
CLIPBOARD_WATCH_TIMEOUT = 900     # give up after 15 minutes and ask manually

# Review-page text is mostly UI chrome. Condensed and capped at this size.
PAGE_CONTEXT_CHARS = 2500

# Optional CSS selector scoping page_context to a single record-card
# container instead of the whole <body>. If set and found, this avoids
# picking up nav bar / session chrome (username, shift clock, logout links)
# by construction, rather than relying only on the regex/exact-line filter
# in condense_page_context(). Leave None until the real container selector
# on the live review page is confirmed - the text filter still applies
# either way as a second layer.
PAGE_CONTEXT_SELECTOR = None


# ---------------------------------------------------------------------------
# Field discovery reliability
# ---------------------------------------------------------------------------
# discover_fields() reads the record's editable keys straight from the DOM
# (hidden `field` inputs). Right after the page auto-advances to the next
# supplier ("Saved - next supplier ready."), the new record's inputs can
# take a moment to render, and a discovery attempt in that window can see
# zero fields even though the page is perfectly fine a beat later. These
# settings retry through that window instead of treating an empty result as
# "this record has no editable fields" and silently substituting the
# 8-field fallback (which is what happened before this was added: the
# fallback fired silently, CURRENT RECORD was sent to the model as `{}`,
# and dba/specialty/products/description - the fields most likely to carry
# real corrections - dropped out of EDITABLE FIELDS with no warning).
FIELD_DISCOVERY_MAX_ATTEMPTS = 4
FIELD_DISCOVERY_RETRY_DELAY_MS = 500

# Durable log of records skipped because field discovery never recovered
# within the retry budget above - lets a human check whether failures
# cluster on one record/page shape (a real DOM change worth investigating)
# or scatter randomly (the timing race this was built to survive).
FIELD_DISCOVERY_FAILURE_LOG = "field_discovery_failures.csv"


# ---------------------------------------------------------------------------
# Independent website sanity check
# ---------------------------------------------------------------------------
# Before writing a proposed website_url, fetch it and check whether the
# company's own name tokens actually appear on it. This is a best-effort
# net for exactly the failure that let sensibleportions.com (a Hain
# Celestial snack brand) get written onto an unrelated Cabot, AR company
# whose DBA/description had been contaminated with that brand's data - a
# domain sharing zero identifying tokens with the company name is a strong
# signal something upstream (the record, a link on the page, or the
# model's own research) pointed at the wrong company.
#
# A failed fetch (network hiccup, blocked domain, slow site) never blocks
# the write - only a fetch that SUCCEEDS and finds no match does. Every
# outcome (match / mismatch / inconclusive) is logged either way so a human
# can spot-check the inconclusive ones.
VERIFY_WEBSITE_BEFORE_APPLY = True
WEBSITE_VERIFY_TIMEOUT_MS = 15000
WEBSITE_VERIFY_LOG = "website_verify_log.csv"


# ---------------------------------------------------------------------------
# Holding an ACCEPT with unresolved flagged fields
# ---------------------------------------------------------------------------
# needs_clear (a field ChatGPT flagged as suspect but sent no clear=true/
# reason for) and needs_review (a website_url the independent check
# couldn't confirm) both leave a field's OLD value in place - correct, since
# neither is confident enough to justify an automatic delete or overwrite.
# But leaving the field alone is not the same as the record being fine: in
# Auto Mode, perform_final_action() used to fire Platform ready right after
# printing that warning regardless of it, so an ACCEPT with a known-suspect
# email/DBA/website still got marked accepted in production, with only a
# console line - easy to miss in an unattended run - as any record of it.
# With this on, Auto Mode holds (Skips, leaving the record undecided) any
# ACCEPT that still has unresolved needs_clear/needs_review items, and logs
# it to FIELD_HOLD_LOG instead. Approval Mode is unaffected - a human
# already sees the same warning before clicking anything themselves.
HOLD_ACCEPT_ON_UNRESOLVED_FIELDS = True
FIELD_HOLD_LOG = "held_for_field_review.csv"

# When the record was a "company name outlier" case (name pointed to one
# company, everything else consistently pointed to a different one that
# turned out to pass scope - see the prompt's "HOW MANY COMPANIES ARE
# ACTUALLY MIXED INTO THIS RECORD" rule), the correction itself is still
# applied, but Auto Mode holds the FINAL Platform-ready action for a human
# to glance at before the record is marked done - renaming a company's
# identity in the database is a bigger consequence than a wrong phone
# number, and deserves a checkpoint even when scope verification passed.
# Set False to let Auto Mode finalize identity-renamed records same as any
# other clean ACCEPT.
HOLD_ON_IDENTITY_RENAME = True


# ---------------------------------------------------------------------------
# v12.26 - supplier decision history (Excel)
# ---------------------------------------------------------------------------
# One workbook holding every supplier the pipeline has decided on, so
# "have we already done this company, and what happened?" has an answer
# outside the review UI. The existing CSVs each cover one exception case
# (non-US, manual review, held, discovery failure); this covers the normal
# path - the clean accepts and clean rejects - which previously left no
# durable trace at all.
#
# Three sheets, routed by what actually happened to the RECORD:
#   Accepted   - Platform ready was clicked
#   Rejected   - Reject was clicked
#   Undecided  - deliberately left undecided (MANUAL_REVIEW, or an Auto-Mode
#                hold). A held ACCEPT is NOT in the Accepted sheet - nothing
#                was accepted - and its row moves to Accepted automatically
#                if the record later comes back through and is finalized.
#
# One row per supplier, keyed on the MASTER record id (falling back to a
# normalised company name). A repeat updates that row in place rather than
# appending a second one: first_seen is preserved, last_seen/times_seen
# advance, everything else is refreshed. The workbook is saved after every
# record, so a crash mid-batch never costs the history.
#
# Requires openpyxl (added to requirements.txt). If it is missing, or if the
# workbook is locked because it is open in Excel, rows spill to
# HISTORY_PENDING_FILE and are merged in on the next successful save - the
# run is never blocked and no record is lost.
ENABLE_HISTORY_EXCEL = True
HISTORY_EXCEL_FILE = "supplier_history.xlsx"
HISTORY_ACCEPTED_SHEET = "Accepted"
HISTORY_REJECTED_SHEET = "Rejected"
HISTORY_UNDECIDED_SHEET = "Manual review & held"
HISTORY_PENDING_FILE = "supplier_history_pending.jsonl"

# Print a heads-up mid-run when the supplier on screen was already decided in
# an earlier session, with that decision and its reason. Informational only -
# the record is still researched and processed normally, and its existing row
# is updated rather than duplicated.
HISTORY_WARN_ON_REPEAT = True


# ---------------------------------------------------------------------------
# v12.25 - post-save settle / advance verification / repeat guard
# ---------------------------------------------------------------------------
# Every "Save correction", "Platform ready", "Reject" and "Skip" is a real
# form POST that reloads the review page. The agent now waits for that reload
# to land (instead of a flat 150-300 ms) before touching the next field or
# pressing the verdict key. This removes the "not present in the current UI"
# skips and the lost G/R keypress that made one supplier come back 3-8 times.
POST_SETTLE_TIMEOUT_MS = 4000      # max wait for the page to be usable after a POST
FIELD_PRESENT_WAIT_MS = 2500       # how long to poll for a field's hidden input
ADVANCE_VERIFY_TIMEOUT_MS = 5000   # how long to wait for the MASTER id to change after a verdict
FINAL_ACTION_MAX_ATTEMPTS = 3      # retries if Platform ready / Reject did not advance

# If the SAME supplier is still served after a verdict, reuse the previous
# ChatGPT result (apply what is left, press the verdict again) instead of
# a new clipboard round-trip. Repeat passes do not count toward the run count.
REUSE_RESULT_ON_REPEAT = True
MAX_REPEAT_PASSES = 2


# ---------------------------------------------------------------------------
# v12.28 - accepted companies workbook (corrected record, as uploaded)
# ---------------------------------------------------------------------------
# ONLY accepted companies go in here. Right after every correction for a
# supplier has been saved, and immediately BEFORE Platform ready is clicked,
# the agent re-reads the whole record back from the review page. That fresh
# read - the record exactly as it is being uploaded - is written to this
# workbook once the accept is CONFIRMED (the page actually advanced).
#
# Not written: REJECT, MANUAL_REVIEW, Auto-Mode holds (a held ACCEPT is
# skipped, not accepted), or a Platform ready that failed to land. In
# Approval Mode you are asked "Did you click Platform ready?" after ENTER,
# because the agent cannot see which button you pressed.
#
# One row per company (keyed on the MASTER id). If the same company is
# accepted again later, its row is refreshed with the newer record and
# times_accepted goes up; first_accepted is kept.
#
# If the page re-read fails, the row is still written, reconstructed from
# the original record plus the corrections that were confirmed saved, and
# snapshot_source says so.
#
# Same lock handling as the history workbook: if the file is open in Excel,
# rows queue to ACCEPTED_SNAPSHOT_PENDING_FILE and merge on the next save.
ENABLE_ACCEPTED_SNAPSHOT = True
ACCEPTED_SNAPSHOT_FILE = "accepted_companies.xlsx"
ACCEPTED_SNAPSHOT_SHEET = "Accepted companies"
ACCEPTED_SNAPSHOT_PENDING_FILE = "accepted_companies_pending.jsonl"


# =============================================================================
# FINAL SNAPSHOT: reload -> extract -> accepted_companies -> verdict (v32.1)
# =============================================================================
# Edits are saved with direct HTTP POSTs, which never refresh the browser tab.
# Without a reload, the "corrected record" was read from the stale page and the
# accepted-companies workbook received the ORIGINAL (uncorrected) data.
RELOAD_BEFORE_FINAL_SNAPSHOT = True

# After the reload, every saved correction is compared with what the page now
# shows. In auto mode an ACCEPT whose corrections did not land is held (skipped)
# instead of pressing Platform ready. Approval mode prints a warning.
HOLD_ACCEPT_IF_EDITS_NOT_LANDED = True

# An ACCEPT whose final page could not be re-read after the reload is also held
# in auto mode: nothing is accepted that could not be read back first.
HOLD_ACCEPT_IF_SNAPSHOT_FAILED = True
