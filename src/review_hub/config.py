"""Engine configuration - ported from the v34 archive (legacy/aekovera/config.py).

Every value here is a production lesson; the engine port changes module paths,
not defaults. Excel/CSV output filenames from the old desktop layout
(supplier_history.xlsx, manual_review_queue.csv, ...) are intentionally absent:
durable state now flows through the runner's persistence interface, and the
same-shape exports are rebuilt from the store by the storage task.
"""

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
#
# The manual ChatGPT backend is the default and needs no key at all.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Free model IDs end in ":free". The free roster ROTATES and IDs get delisted
# without notice, so this is a fallback chain, not a single choice: the client
# tries each in order and moves on after a 404/402/429.
#
# Verify the current list at https://openrouter.ai/models?max_price=0 and edit
# this list when a model stops working. "openrouter/free" is the auto-router
# that picks any available free model, which makes it a good last resort.
# These are PREFERENCES, not a fixed list: with OPENROUTER_AUTO_DISCOVER on, the
# agent queries /api/v1/models at startup, keeps only models that are genuinely
# $0 right now, and ranks these IDs first if they are still free.
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
RUN_LOG_DIR = "run_logs"  # BatchRunner transition logs (JSONL, one per run)


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
MAX_CONSECUTIVE_FAILURES = 3  # legacy main.py value: stop the run after this many in a row


# ---------------------------------------------------------------------------
# Scope: non-US companies
# ---------------------------------------------------------------------------
# Non-US companies are now ACCEPTED, but their supply origin must be recorded.
ACCEPT_NON_US = True


# ---------------------------------------------------------------------------
# Manual review queue
# ---------------------------------------------------------------------------
# When the model cannot confidently establish scope after real research, it
# may return decision: "MANUAL_REVIEW" instead of forcing an ACCEPT/REJECT
# guess. These records are never auto-decided: no field corrections are
# applied and neither Platform ready nor Reject is clicked.
ENABLE_MANUAL_REVIEW = True


# ---------------------------------------------------------------------------
# Manual (ChatGPT) workflow speed-ups
# ---------------------------------------------------------------------------
# PROJECT MODE: paste the standing rules into a ChatGPT Project's custom
# instructions ONCE (the CLI writes them to PROJECT_INSTRUCTIONS_FILE at
# startup). Each record then pastes only its own payload - roughly 1.5k chars
# instead of 22k. Set False to keep pasting the full rulebook every time.
CHATGPT_PROJECT_MODE = True
PROJECT_INSTRUCTIONS_FILE = "chatgpt_project_instructions.txt"

# Watch the clipboard and pick up ChatGPT's answer the moment you copy it,
# instead of requiring an ENTER press. Saves a keystroke and an alt-tab.
CLIPBOARD_AUTO_WATCH = True
CLIPBOARD_POLL_INTERVAL = 0.4     # seconds
CLIPBOARD_WATCH_TIMEOUT = 900     # give up after 15 minutes and ask manually
LAST_REQUEST_FILE = "last_research_request.txt"  # research prompt is always saved here

# Review-page text is mostly UI chrome. Condensed and capped at this size.
PAGE_CONTEXT_CHARS = 2500

# Optional CSS selector scoping page_context to a single record-card
# container instead of the whole <body>. Leave None until the real container
# selector on the live review page is confirmed - the text filter still
# applies either way as a second layer.
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
# 8-field fallback.
FIELD_DISCOVERY_MAX_ATTEMPTS = 4
FIELD_DISCOVERY_RETRY_DELAY_MS = 500


# ---------------------------------------------------------------------------
# Independent website sanity check
# ---------------------------------------------------------------------------
# Before writing a proposed website_url, fetch it and check whether the
# company's own name tokens actually appear on it. A failed fetch (network
# hiccup, blocked domain, slow site) never blocks the write - only a fetch
# that SUCCEEDS and finds no match does.
VERIFY_WEBSITE_BEFORE_APPLY = True
WEBSITE_VERIFY_TIMEOUT_MS = 15000


# ---------------------------------------------------------------------------
# Holding an ACCEPT with unresolved flagged fields
# ---------------------------------------------------------------------------
# In auto mode, an ACCEPT is held (skipped, leaving the record undecided)
# when any of these fire. Approval Mode is unaffected - a human already sees
# the same warning before clicking anything themselves.
HOLD_ACCEPT_ON_UNRESOLVED_FIELDS = True
HOLD_ON_IDENTITY_RENAME = True


# ---------------------------------------------------------------------------
# v12.25 - post-save settle / advance verification / repeat guard
# ---------------------------------------------------------------------------
# Every "Save correction", "Platform ready", "Reject" and "Skip" is a real
# form POST that reloads the review page. The agent waits for that reload to
# land before touching the next field or pressing the verdict key.
POST_SETTLE_TIMEOUT_MS = 4000      # max wait for the page to be usable after a POST
FIELD_PRESENT_WAIT_MS = 2500       # how long to poll for a field's hidden input
ADVANCE_VERIFY_TIMEOUT_MS = 5000   # how long to wait for the MASTER id to change after a verdict
FINAL_ACTION_MAX_ATTEMPTS = 3      # retries if Platform ready / Reject did not advance

# If the SAME supplier is still served after a verdict, reuse the previous
# research result (apply what is left, press the verdict again) instead of
# a new research round-trip. Repeat passes do not count toward the run count.
REUSE_RESULT_ON_REPEAT = True
MAX_REPEAT_PASSES = 2


# ---------------------------------------------------------------------------
# Accepted-companies snapshot (corrected record, as uploaded)
# ---------------------------------------------------------------------------
# Only accepted companies are snapshotted, and the snapshot is taken from a
# RELOADED page immediately BEFORE the verdict is pressed, then confirmed
# once the accept lands (or rolled back if it does not).
ENABLE_ACCEPTED_SNAPSHOT = True


# =============================================================================
# FINAL SNAPSHOT: reload -> extract -> snapshot -> verdict (v32.1)
# =============================================================================
# Edits are saved with direct HTTP POSTs, which never refresh the browser tab.
# Without a reload, the "corrected record" was read from the stale page.
RELOAD_BEFORE_FINAL_SNAPSHOT = True

# After the reload, every saved correction is compared with what the page now
# shows. In auto mode an ACCEPT whose corrections did not land is held
# (skipped) instead of pressing Platform ready. Approval mode prints a warning.
HOLD_ACCEPT_IF_EDITS_NOT_LANDED = True

# An ACCEPT whose final page could not be re-read after the reload is also held
# in auto mode: nothing is accepted that could not be read back first.
HOLD_ACCEPT_IF_SNAPSHOT_FAILED = True
