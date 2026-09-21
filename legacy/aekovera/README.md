# Aekovera Review Agent v12

Browser-side QA helper for the Aekovera supplier review page.

**v12.10 fixes false rejections from `qualifying_supplier_types` phrasing.**
See "Scope rules" below — the code gate was doing an exact-string match
against the review-UI category list, which silently rejected genuinely
in-scope manufacturers (a plain "Food Manufacturer" or "Manufacturer" never
matched the literal "Food Manufacturer / Brand"). This was rejecting roughly
two out of every three ACCEPTs in some batches.

**v12.11 fixes a second, related false rejection: `scope_match` as a bare
description with no leading yes/no word.** `to_bool()` (added in v12.9)
already tolerated a qualitative verdict like `"Strong - US-based food
manufacturer..."`, but only when the sentence OPENS with a recognized word
(`strong`, `confirmed`, `yes`, etc.). The manual ChatGPT workflow frequently
skips that lead word entirely and answers with just the plain description -
`"US-based food manufacturer producing bakery and frozen breakfast/snack
products"`, `"Seafood harvester, wholesaler, and retailer supplying fresh
stone crabs and seafood products"` - real cases that force-rejected Cyril's
Foods, Combs Fish Company, and Clayton's Crab Co. despite `decision=ACCEPT`
and an unambiguous reason. `infer_scope_match_from_description()` now reads
an implicit verdict from a curated set of food/beverage/seafood/supplement
domain keywords when the description contains ONLY positive signals (never
when negative keywords like "cosmetic" or "pharmaceutical" are also
present, or when both point in different directions) - a genuinely
ambiguous or off-domain description still fails safe to REJECT exactly as
before.

**v12.12: labeling counts as Co-Packer, and a REJECT no longer wipes out
food/beverage-relevant field corrections.**

*Labeling → Co-Packer.* Applying labels to food/beverage product as a
contract service is a form of co-packing, but it wasn't one of the keyword
mappings in `normalize_supplier_type()`, so a company whose
`qualifying_supplier_types` said something like `"Labeling Services"` still
fell through to "no qualifying supplier category." `"labeling"`/`"labelling"`
now map to `co-packer`. This is checked AFTER the equipment guard (which was
widened to also catch a bare `"machine"`, not just `"equipment"`/
`"machinery"`), so a company that *manufactures labeling machines* is still
correctly routed to the non-qualifying equipment bucket rather than being
swept in as a co-packer.

*REJECT no longer means "discard the research."* Previously, ANY reject -
whether the raw ChatGPT decision or a scope-gate override - forced
`changes = []`, on the theory that an out-of-scope company should never be
edited. In practice this threw away verified phone numbers, addresses, and
websites for companies that were genuinely food/beverage-related but got
rejected for some other reason (wrong role, borderline category, non-US
policy, etc.) - useful research, lost. `has_food_beverage_relevance()` now
gates this: field corrections are KEPT on a REJECT when the research shows
a real food/beverage/supplement connection (via `food_beverage_connection`,
`reason`, or `scope_match` text), and still discarded when the company has
no domain connection at all (a software vendor, a cosmetics company, an
equipment maker) - so an irrelevant record is never enriched, but a
legitimate supplier's data isn't lost just because it didn't clear the
scope bar this time. `MANUAL_REVIEW` is unchanged and still always clears
`changes`, since that case is genuinely unconfirmed either way.

**v12.13 fixes a real data-corruption bug: a markdown link embedded PARTWAY
through a field value survived cleaning and got written to the live
database verbatim.** `clean_corrected_value()` already stripped a value that
was *entirely* one markdown link (`[visible](target)`, exact `re.fullmatch`),
for the well-known case of ChatGPT wrapping an email/URL in citation syntax.
It never handled a link appearing partway through a longer string. Two real
records hit this: Harris Honey Company's `country` correction came back as
`"Madelia, MN, [United](https://harrishoneymn.com%22},{%22field%22:%22city%
22,%22new_value%22:%22Madelia%22},{%22field%22:%22country%22,%22new_value%2
2:%22United) States"` - a citation-style link wrapped around part of the
address, with the link's OWN target somehow containing a percent-encoded
fragment of the changes JSON itself - and Captain's Catch Seafood's `city`
came back similarly mangled. Because the value wasn't ENTIRELY a link, the
old exact-match cleaner left it completely untouched, and the automation
typed the raw brackets/parens/percent-encoded JSON straight into the live
`country`/`city` fields.

`clean_corrected_value()` now removes every markdown link found ANYWHERE in
a value (not just a whole-string match), keeping only the visible portion
and discarding the target - garbage target included - which turns both real
examples above back into the clearly-intended `"Madelia, MN, United States"`
and `"North Providence"`. A second, independent guard,
`looks_like_json_leak()`, is checked in `apply_text_change()` as a hard
stop: if a cleaned value still contains a raw or percent-encoded JSON
fragment (`"field":`, `%22new_value%22`, etc.) after all cleaning, the field
is refused and reported as a normal skipped-field error rather than written
to the database - so a future corruption shape this specific fix doesn't
anticipate still can't reach production silently.

**v12.14 fixes ADD MISSING: clicking "+ Specialty" (etc.) never actually got
a value saved.** `apply_change()`/`apply_text_change()` always tried to
click a pencil to "open" the correction form before filling it - correct
for an existing field, whose form starts closed. But clicking a "+ Field"
control under ADD MISSING opens its "Corrected value" form DIRECTLY - there
is no separate pencil for a field that didn't exist a moment ago. The old
code's structural "nearest preceding button" search for a pencil either
found nothing (`Edit button not found for specialty`) or, worse, could grab
an unrelated nearby field's pencil and click that instead. Either way, every
ADD MISSING field silently failed to save. `apply_text_change()` now takes
an `already_open` flag - set by `apply_change(..., newly_created=True)`,
which `main()`'s change-application loop now passes whenever the field came
from `addmissing.create_field()` - and when set, fills the form that's
already open instead of trying to open it again. If a given deployment's
"+ Field" control turns out NOT to open the form directly, it safely falls
back to the normal pencil-click path rather than failing, so this doesn't
assume one specific DOM shape. Verified against a local Playwright fixture
reproducing both the direct-open case (matching the real screenshot) and a
hypothetical toggle-required case - both fill the correct field's form; the
un-patched code provably raises `Edit button not found` on the direct-open
case.

**Default backend is the manual ChatGPT workflow** (ChatGPT does the web
research). The OpenRouter automation from v12 is still bundled and selectable,
but it is no longer the default.

**v12.2 widens the scope gate:** non-US companies are now accepted with a
recorded country of supply, and dietary supplement / vitamin companies are in
scope.

## What changed from v11

| | v11 (manual) | v12 (automated) |
|---|---|---|
| Research | You paste the prompt into ChatGPT, copy JSON back | `llm.py` calls OpenRouter directly |
| Web research | ChatGPT browsed | `evidence.py` fetches pages in the browser and passes the text to the model |
| Per record | 2 manual copy/paste steps | none |
| Failure handling | session stops | bad record is skipped, batch continues |

The clipboard workflow is still available as a fallback (backend option 2).

## Setup

```powershell
cd Desktop\aekovera-review-agent
.venv\Scripts\activate
pip install -r requirements.txt
```

Get a free API key at https://openrouter.ai/keys (email signup, no credit
card), then set it in the environment — never in the source:

```powershell
$env:OPENROUTER_API_KEY = "sk-or-v1-..."     # PowerShell, current session
setx OPENROUTER_API_KEY "sk-or-v1-..."       # persist across sessions
```

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."     # bash
```

## Run

```powershell
python main.py
```

Startup asks for:

1. **Mode** — Approval or Auto
2. **Backend** — manual ChatGPT (default) or OpenRouter API
3. **Number of records**

The agent then validates the key, prints your remaining quota, and runs
unattended in Auto mode.

## Speed-ups for the manual workflow

Three changes cut the per-record effort substantially. All are on by default.

**1. ChatGPT Project mode (biggest win).** The scope rules and output schema
are identical for every record — ~8,400 characters re-pasted every time. At
startup the agent writes them to `chatgpt_project_instructions.txt`. Paste
that **once** into a ChatGPT Project's custom instructions, then run every
record inside that project. Each record's paste drops from ~9,200 characters
to ~900 — a **90% reduction**, and ChatGPT answers noticeably faster on a
short prompt. Answer `n` at the startup question to keep full prompts.

**2. Condensed page context.** The review page's raw text ran to ~12,000
characters, nearly all buttons, shortcut hints and repeated labels. It is now
filtered to company-identifying lines and capped at 2,500
(`PAGE_CONTEXT_CHARS`).

**3. Clipboard auto-detect.** The agent watches the clipboard and picks up
ChatGPT's answer the moment you copy it — no ENTER press, no alt-tab back.
Intermediate copies are ignored until valid JSON with a `decision` appears.
Ctrl+C falls back to manual paste. Disable with `CLIPBOARD_AUTO_WATCH`.

Net effect per record: two copy/paste round-trips become one copy out, one
copy back, and no keystrokes in the terminal.

## Free-tier limits — read this before a big batch

Each record costs **one** OpenRouter request.

- ~20 requests/minute (the client self-throttles to stay under this)
- ~50 requests/day on an unfunded account
- ~1,000 requests/day once you have ever purchased $10 of credits
- **Failed requests still count against the daily quota**

So an unfunded key realistically processes ~40–50 suppliers per day. A
one-time $10 credit purchase takes that to ~1,000/day and is the single
change that makes this pipeline production-viable.

Free model IDs rotate constantly — an ID that was free last month commonly
returns `404 "This model is unavailable for free, use the paid slug"`. So the
agent does **not** trust a hardcoded list: at startup it queries
`/api/v1/models`, keeps only models priced at $0 *right now* with enough
context for the evidence, and builds a fallback chain from them.
`OPENROUTER_MODELS` in `config.py` is only a ranking preference. Set
`OPENROUTER_AUTO_DISCOVER = False` to pin the static list instead.

If a model still 404s mid-run as no-longer-free, it is dropped from the chain
for the rest of the session rather than retried on every record.

## Web evidence (important)

Free OpenRouter models **cannot browse the web**. The v11 prompt told the
model to "independently research the company" — with no browsing, that
would produce confident hallucinated emails and addresses, and the scope
gate would then be validating invented facts.

So the agent now does the browsing itself. For each record, `evidence.py`
opens background tabs in the same Playwright context and fetches:

1. the website currently on file,
2. its `/contact` or `/about` page,
3. a DuckDuckGo results page for the company name.

That text is passed to the model as quoted evidence, and the prompt
requires every proposed `new_value` to appear literally in it. If no
evidence is retrievable, the model is instructed to REJECT rather than
guess.

**Quality caveat:** a free 70B model reading three fetched pages is not
equivalent to ChatGPT with browsing. Run Approval Mode on 20–30 records
and check the accept/reject calls against your own judgement before you
trust Auto Mode on a large batch.

**Privacy caveat:** free OpenRouter endpoints may use prompts for model
training. Review-page content is sent to them. If any of that is
confidential, use a paid model ID (drop the `:free` suffix) instead.

## Data safety

All v11 safety properties are unchanged:

- **HARD scope gate** — an ACCEPT without `scope_match: true`, a non-empty
  `food_beverage_connection`, and at least one qualifying supplier category
  is overridden to REJECT and all proposed edits are discarded.
- **Rejected companies never receive field corrections.**
- **Manual-review companies never receive field corrections either**, and
  never get a final Platform ready / Reject action — see below.
- **Fields are discovered dynamically per record, not a hardcoded eight.**
  `discover_fields()` reads whatever hidden `field` inputs the live page
  actually exposes for THIS record — normally that includes Email, Email 2,
  Phone, Website, City, State, ZIP, Country **and** DBA, Specialty, Products,
  Certs, Address (see ENRICHABLE_FIELDS / the ENRICHMENT RULE in the prompt,
  which explicitly asks the model to research and correct those too).
  `FIELDS` in `main.py` is only the fallback used if discovery ever comes
  back with nothing usable to build the prompt payload from — it is not,
  and was never meant to be, the real ceiling on what can be corrected.
  (Earlier revisions of this doc described "eight editable fields only,
  anything else dropped" — that was accurate for a much older hardcoded
  whitelist, and had gone stale by the time dynamic discovery replaced it.
  See v12.18 below: the eight-field fallback firing on a record that
  clearly has more fields than that, with no warning, was itself a real
  bug in the discovery step, not the documented design.)
- `new_value: null` is skipped and **never** clears a field.
- Source URL is never filled.
- A correction counts as applied only after Save is followed by UI
  verification.
- Fields absent from the current UI/database, and not offered under ADD
  MISSING, are skipped, not created.

Added in v12:

- Fetched web pages and review-page text are explicitly labelled as data,
  not instructions, so a prompt-injection attempt on a supplier's website
  cannot flip a decision.
- Every prompt and raw model response is written to `llm_logs/` for audit.

**v12.18 fixes a real-record failure with three parts: field discovery
silently degrading, a proposed website never independently checked, and a
prompt gap that let contamination get accepted as scope evidence.**

*What happened.* North American Baking, Inc. (Cabot, AR) came back from
this record's research as ACCEPT with `website_url` set to
`sensibleportions.com` — the website of Hain Celestial's Sensible Portions
snack brand, unrelated to the Cabot, AR company. The record's DBA and
description had already been contaminated with Sensible Portions/Hain
Celestial data (a different plant, in Mountville, PA); the record's own
Site and LinkedIn links, separately, pointed to North American **Banking**
Company, a Minnesota community bank — a second, unrelated contamination
source, matched on a one-letter name collision. The research step noticed
the LinkedIn/Site mismatch but then used the *other* contaminated field
(the DBA) as if it were reliable evidence, instead of treating both as
untrustworthy and independently verifying the real company.

*Bug 1 — field discovery silently degraded.* `discover_fields()` queries
the DOM for hidden `field` inputs; right after a Save transition to the
next record, that query can transiently return zero results while the new
page finishes rendering. The old code treated "zero results, no exception"
identically to "this record genuinely has no fields" and silently
substituted the hardcoded 8-field `FIELDS` fallback — no warning printed.
On the affected run this meant `CURRENT RECORD` went to the model as `{}`
and `EDITABLE FIELDS` silently narrowed to the 8-field list, excluding
`dba`/`specialty`/`products`/description — the exact fields carrying the
contamination. Even a model that correctly identified the contamination had
no field in `EDITABLE FIELDS` to attach a `"clear": true` to.
`discover_fields()` now retries (`FIELD_DISCOVERY_MAX_ATTEMPTS`,
`FIELD_DISCOVERY_RETRY_DELAY_MS` in `config.py`) and raises
`FieldDiscoveryError` instead of silently falling back when every attempt
comes back empty; `main()` catches that, skips the record loudly, and logs
it to `field_discovery_failures.csv` rather than researching against a page
it could not actually read.

*Bug 2 — no independent check on a proposed website before writing it.*
`evidence.py`'s real fetch-and-verify machinery was wired only into the
unused `"api"` backend; the default manual-ChatGPT workflow had nothing
that re-checked a proposed `website_url` against the company itself before
applying it. New `evidence.website_matches_company()` fetches the proposed
URL and checks whether the company's own name tokens appear on it at all —
cheap, approximate, and exactly enough to have caught this case (none of
"north"/"american"/"baking" appear anywhere on sensibleportions.com). A
clear mismatch holds the field (`needs_review`, reported in the run
summary, logged to `website_verify_log.csv`) instead of auto-applying it. A
**failed fetch never blocks the write** — only a fetch that succeeds and
finds nothing does — so a network hiccup can't silently discard a real
correction. Toggle: `VERIFY_WEBSITE_BEFORE_APPLY`.

*Bug 3 — the prompt had no rule for "contamination I can't clear this run."*
The `"clear": true` protocol only works on a field that IS in `EDITABLE
FIELDS`. When contamination sits on a field that isn't (which is also what
Bug 1 caused here), the model had no instruction covering that case, and
defaulted to building a scope justification on top of data it had already
identified as belonging to another company. The prompt now has an explicit
rule: never use a value flagged as contamination as scope evidence, never
adopt an already-present link just because it seems "less wrong" than
another contaminated field, and — if the company's actual business can't be
independently confirmed once contaminated fields are set aside — return
`MANUAL_REVIEW`, naming which fields are contaminated and which of those
sit outside `EDITABLE FIELDS` so a human has to clear them by hand.

*Also in this pass:* `CONTEXT_NOISE` (the page-text filter feeding
`PAGE CONTEXT`) missed several review-UI chrome lines that leaked into the
research prompt — "Log out" specifically (the old pattern only matched the
one-word "logout"), plus "Aekovera QA", the on-shift status line, "Mine",
"Review" as a standalone nav line, and "Saved — next supplier ready.". All
now stripped; verified against the exact leaked lines from the affected
run. An optional `PAGE_CONTEXT_SELECTOR` config value can scope extraction
to a single record-card container instead of the whole `<body>`, once a
stable selector for that container is confirmed — left unset for now.

**v12.19 fixes a related but separate gap: Auto Mode clicked Platform ready
on an ACCEPT even when fields were flagged and left unresolved.** ChatGPT
correctly declined to blank `primary_email`/`dba_name` on a real record
(Willamette Valley Meat Company) without a stated `clear` reason — the
existing safety net worked as designed and printed the warning. But nothing
stopped Auto Mode from clicking Platform ready right afterward anyway, so
the record was marked accepted in production with both flagged values still
live, and the only trace was a console line in an unattended run - easy to
miss, and never written anywhere durable. With `HOLD_ACCEPT_ON_UNRESOLVED_FIELDS`
(on by default), Auto Mode now Skips instead of accepting whenever an ACCEPT
still has unresolved `needs_clear` or `needs_review` items, and logs the
held record - which field(s), what value, why - to `held_for_field_review.csv`
so it isn't silently lost. Approval Mode is unaffected: a human already
sees the identical warning before clicking anything themselves.

## Supplier history workbook (v12.26)

Every supplier the pipeline decides on is now written to
**`supplier_history.xlsx`**, so *"have we already done this company, and what
happened?"* has an answer outside the review UI. The existing CSVs each cover
one exception case (`accepted_non_us.csv`, `manual_review_queue.csv`,
`held_for_field_review.csv`, `field_discovery_failures.csv`) — the ordinary
path, a clean ACCEPT or a clean REJECT, previously left no durable trace at
all.

**Three sheets**, routed by what actually happened to the record:

| Sheet | Contains |
|---|---|
| `Accepted` | Platform ready was clicked |
| `Rejected` | Reject was clicked |
| `Manual review & held` | deliberately left undecided — `MANUAL_REVIEW`, or an Auto-Mode hold (v12.19/v12.20) |

A held ACCEPT is deliberately **not** in the Accepted sheet: nothing was
accepted, the supplier is still sitting in the queue. Filing it under
"Accepted" would make the workbook claim a verdict that was never written.

**One row per supplier, not one per pass.** Rows are keyed on the MASTER
record id, falling back to a normalised company name (case, punctuation and
corporate suffixes stripped, so `Clayton's Crab Co., Inc.` and
`Claytons Crab Co Inc` are one row, not two). When a supplier comes round
again the existing row is **updated in place** — `first_seen` is preserved,
`last_seen`/`times_seen` advance, every other column is refreshed — and the
row **moves between sheets** if the outcome changed. So a record that was held
for review last week and finalized today ends up in `Accepted` exactly once,
with `times_seen: 2`, rather than appearing in two sheets with two different
answers.

**What each row holds.** The decision block — timestamps, decision, outcome,
run mode, backend, confidence, reason, `scope_match`,
`food_beverage_connection`, qualifying types, supply origin, manual-review
reason — followed by every company detail the page exposed for that record
(DBA, emails, phone, website, full address, specialty, products, certs,
supplier types, description, and anything else discovered). Company details
are the values **after** the run: the record as read, with every successfully
applied correction overlaid, so the workbook reflects what the database now
holds rather than the stale values the record arrived with. Which fields were
applied, created, cleared, failed, left uncleared or held is recorded
alongside.

Columns are not a fixed list. Fields are discovered per record, so a supplier
processed next month that exposes a field no earlier record had gets a **new
column appended** rather than having the value dropped — the same lesson
v12.18 learned about hardcoded field lists.

**Mid-run heads-up.** When a record on screen was already decided in an
earlier session, the agent prints the previous decision and reason before
researching it (`HISTORY_WARN_ON_REPEAT`). Informational only — the record is
still processed normally, and its existing row is updated rather than
duplicated.

**Durability.** The workbook is saved after *every* record, not at the end of
the session, so a crash or a closed browser half-way through a batch never
costs the history. If the file can't be written — overwhelmingly the common
case being **you have it open in Excel**, which makes it read-only on Windows
— the row is queued to `supplier_history_pending.jsonl` and merged in
automatically on the next save that succeeds. Closing Excel is all that's
needed; the run is never blocked waiting for a file lock and no record is
lost. A corrupt workbook is moved aside rather than overwritten. `openpyxl` is
an optional dependency: without it, rows queue to the pending file and merge
once it's installed, and the review session still runs.

Toggles in `config.py`: `ENABLE_HISTORY_EXCEL`, `HISTORY_EXCEL_FILE`, the
three sheet names, `HISTORY_PENDING_FILE`, `HISTORY_WARN_ON_REPEAT`.

## Accepted companies workbook (v12.28)

**`accepted_companies.xlsx`** keeps a history of every company the pipeline
**accepted**, holding the corrected record exactly as it was uploaded.
**Only accepted companies go in this file.**

**When it is captured.** After every correction for a supplier has been saved,
and immediately **before** Platform ready is clicked, the agent re-reads the
whole record back from the review page (`fetch_corrected_record()` in
`main.py`, reading the fields exactly the way the first read does). That read
is written to the workbook only **after** the accept is confirmed, meaning the
page actually advanced to the next supplier.

**Never written:** REJECT, MANUAL_REVIEW, Auto-Mode holds (a held ACCEPT is
skipped, not accepted), or a Platform ready that failed to land. In Approval
Mode the agent can't see which button you pressed, so after ENTER it asks
*"Did you click Platform ready?"* (ENTER = yes). The snapshot there is taken
just before control is handed to you, so any edit you make by hand after that
point is not in it.

**Layout.** One sheet, one row per company, keyed on the MASTER id:

| Columns | Contents |
|---|---|
| `accepted_at`, `record_id`, `company_name` | identity, frozen so they stay visible while scrolling |
| company fields | every field the page exposed (DBA, emails, phone, website, address, types, certs, products, description, and anything else discovered), as read back from the page |
| `edit_status` | `complete`, `partial - N failed, M unresolved`, or `no changes needed` |
| `edits_made` | every field that changed during the run: `field: 'old' -> 'new'`, `added`, or `cleared` |
| `fields_failed`, `fields_unresolved` | corrections that did not save, and fields left uncleared or flagged |
| `confirmed_by` | `agent (auto mode)` or `reviewer (approval mode)` |
| `snapshot_source` | `page re-read before Platform ready`, or `reconstructed (page re-read failed)` |
| `first_accepted`, `times_accepted` | kept across runs; a company accepted again has its row refreshed in place |

`edits_made` is a diff between the record as it arrived and the record as it
was read back, so it shows what actually persisted on the platform, not just
what was attempted. Filter `edit_status` to `complete` for the companies that
were fully edited with nothing left over.

If the page re-read fails, the company is still recorded (reconstructed from
the original record plus the corrections confirmed saved) and
`snapshot_source` says so. Same durability as the history workbook: saved
after every accept via a temp file, queued to
`accepted_companies_pending.jsonl` when the file is open in Excel, and merged
in automatically on the next accept.

This is separate from the `Accepted` sheet in `supplier_history.xlsx`, which
reconstructs the final values and carries the full decision audit. This file
is the clean company list as read back from the platform.

Toggles in `config.py`: `ENABLE_ACCEPTED_SNAPSHOT`, `ACCEPTED_SNAPSHOT_FILE`,
`ACCEPTED_SNAPSHOT_SHEET`, `ACCEPTED_SNAPSHOT_PENDING_FILE`.

## Scope rules

The prompt follows the proven v11 wording, with two changes.

**1. Non-US companies are accepted — with origin recorded, even when the exact
country is uncertain.**
Location is never a rejection reason — and, as of this version, neither is an
unconfirmed or uncertain country. Only scope decides ACCEPT vs REJECT: a
genuine manufacturer, supplier, or co-packer of an accepted product area is
accepted whether it's US-based, confirmed non-US, or non-US with the exact
country unclear.

Every non-US ACCEPT still carries a `supply_origin_note` saying the company
is outside the US and naming the country when it's known, e.g. *"Outside US —
India. Contract manufacturer of vitamin gummies and softgels."* When research
genuinely cannot pin down the exact country, the note says so honestly
instead — *"Outside US — exact country not confirmed; evidence points to
Southeast Asia."* — rather than blocking the ACCEPT or inventing a country.

`is_us_based` can land in three states in the code gate: `True` (US, no
note), `False` (confirmed non-US — the note above is generated), or `None`
(location genuinely couldn't be established either way — the record is still
accepted on scope alone, and no note is fabricated). Earlier versions treated
a missing `supply_country` as a hard REJECT; that was overly strict and
turned some genuinely in-scope non-US suppliers away just because the exact
country wasn't nailed down. That guard is gone — `ACCEPT_NON_US = False` in
`config.py` is still the switch to go back to US-only.

The note is printed in the run summary (flagged `** NON-US **`), appended to
`accepted_non_us.csv`, and written to the UI note field if the accept flow
exposes one. `accepted_non_us.csv` only ever gets a row for a *confirmed*
non-US ACCEPT — the genuinely-unknown-location case is never logged there,
since it isn't actually known to be foreign.

**Boolean fields tolerate a qualitative verdict, not just `true`/`false`.**
`scope_match` and `is_us_based` are supposed to be a literal JSON boolean,
but ChatGPT's manual path sometimes writes a verdict sentence instead - e.g.
`"scope_match": "Strong - US-based food manufacturer/brand producing
packaged hot sauce products."` A strict exact-match parser reads that as
unrecognized -> not confirmed -> the scope gate force-rejects an otherwise
clean ACCEPT, purely because of phrasing. `to_bool()` now also recognizes a
handful of unambiguous leading words (`strong`, `confirmed`, `clearly`,
`yes` / `weak`, `no`, `incorrect`), with a negation-phrase check so "Strong
claim, but does not qualify" still reads as negative. Anything genuinely
ambiguous (`unclear`, `uncertain`, `maybe`) still falls through to "not
confirmed" and fails safe to REJECT, same as before - this only fixes the
specific false-rejection case where the model's answer already was
unambiguous, just not spelled `true`.

**A leftover contradictory line was removed.** A separate summary line
further down the same prompt ("If the country of supply cannot be
determined, decision MUST be REJECT") survived from before the location rule
above was rewritten, sitting right next to a line saying the opposite.
`evidence.py` (used only by the automated OpenRouter backend) had the same
problem in an even stronger form — it required "both a US location and a
food/beverage CPG connection" before allowing ACCEPT, predating the non-US
acceptance feature entirely. Both are fixed now; the prompt no longer tells
the model two contradictory things about location in the same breath.

**2. Accepted product areas (all in scope).**
Food · Beverages · Food ingredients · Beverage ingredients · Dietary
supplements · Nutritional supplements · Protein supplements · Protein powders
· Sports nutrition · Meal-replacement products · Functional nutrition products
· Vitamins/mineral products relevant to CPG scope · Snack and nutrition
products.

**Accepted roles:** Co-Packer, Co-Manufacturer, Contract Manufacturer, Private
Label Manufacturer, Ingredient Supplier, Manufacturer, Supplier, Contract R&D /
Formulation, Packaging Supplier, 3PL / Fulfillment, Food Manufacturer / Brand,
Distributor / Wholesaler. (Equipment / Services is NOT accepted.) Generic roles are mapped onto
the closest review-UI category — a "contract manufacturer" reports as
Co-Manufacturer, an ingredient "supplier" as Ingredient Supplier — because the
code gate checks against the UI category list.

**v12.10: `qualifying_supplier_types` matching is now tolerant, not exact.**
The prompt *tells* ChatGPT to map a generic role onto the exact review-UI
label ("Food Manufacturer / Brand", "Distributor / Wholesaler", etc.), but the
manual copy/paste path doesn't reliably comply — a real batch had two
straightforward manufacturers (Home Run Inn Frozen Foods, a frozen-pizza
maker; Bisousweet Confections, an SQF-certified bakery) both auto-rejected
with "no qualifying supplier category" even though `scope_match` was `true`
and the reason text plainly called them manufacturers. The category ChatGPT
actually returned was something like `"Food Manufacturer"` or `"Bakery
Manufacturer"` — never the exact compound string — so the old exact-set
intersection never matched. `main.py`'s new `normalize_supplier_type()` first
normalizes punctuation/whitespace (`"Food Manufacturer/Brand"` and `"Food
Manufacturer / Brand"` are now the same string), then falls back to a curated
keyword map (`manufactur`, `bakery`, `brand`, `distributor`, `wholesaler`,
`ingredient`, `packaging`, `co-pack`, `private label`, `3pl`, `fulfillment`,
`formulation`, etc.) onto the nine canonical categories. This only widens
what counts as a match to an *already-qualifying* category — it never
invents scope, an "equipment"/"machinery" mention is still routed to the
non-qualifying bucket first, and a category with no recognizable keyword
still correctly fails the gate.

Still rejected: cosmetics, personal care, home care, **prescription pharma and
OTC drugs**, medical devices, industrial, automotive. A company doing both
supplements and pharma is accepted, with that noted in the reason.

Role alone is never sufficient — the products must fall in an accepted area.

**Product detail matters more than anything else here.** The prompt tells the
model to treat the Products field as the main scope signal, not just
something to tidy up — vague entries like "food products" are not enough. It
lists a few concrete qualifying items (chocolates, candies, snack foods,
baked goods, cooking ingredients, packaged meals) and disqualifying ones
(cooking equipment, kitchen supplies, pet food, other non-food items), and
tells the model to dig into the company's own catalog/product pages — not
just its homepage — before deciding.

`example_full_prompt.txt` shows exactly what ChatGPT receives.

### Manual review queue

If, after that deeper product research, the model still cannot pin down
scope, it can return `decision: "MANUAL_REVIEW"` with a `manual_review_reason`
instead of forcing an ACCEPT/REJECT guess. This is a third, deliberately
narrow outcome:

- No field corrections are ever proposed or applied on a manual-review
  record — `changes` is forced to `[]`, same rule as REJECT.
- Neither **Platform ready** nor **Reject** is clicked. The record is left
  via the existing **Skip** action instead, so it is not marked decided.
- The reason is written to the review UI's note field when one is exposed,
  and always appended to `manual_review_queue.csv` (`MANUAL_REVIEW_LOG`) —
  timestamp, record id, company name, the reason, and the scope context the
  model gathered — so a human reviewer has a durable queue to work from and
  can update the record and notify the original researcher of the outcome.
- In Approval Mode, the agent still pauses for ENTER before flagging it, the
  same as every other final action.
- A `manual_review_reason` that ChatGPT leaves empty is not silently
  dropped — a placeholder reason is generated and a warning is printed, so a
  thin answer is visible in the log rather than hidden.

Set `ENABLE_MANUAL_REVIEW = False` in `config.py` to turn this off: the model
may still return `MANUAL_REVIEW` (the prompt is not rewritten based on this
flag, the same convention `ACCEPT_NON_US` follows), but the code gate
downgrades it to REJECT rather than routing it anywhere.

### Empty fields vs missing fields

These are different, and the prompt now says so explicitly:

| Case | In the record? | Action |
|---|---|---|
| Field present with a wrong value | yes | propose the correction |
| Field present but **blank** (`""`/null) | yes | **propose the verified value — filling blanks is wanted** |
| Field under "ADD MISSING:" on the page | not yet | **agent creates it and fills it** (verified values only) |
| ChatGPT returns `new_value: null` | — | skipped; a field is never cleared automatically |

A blank field counts as present, so a verified value does get written into it.

**Creating missing fields.** The agent now reads the page's "ADD MISSING:"
list, passes it to ChatGPT as *FIELDS AVAILABLE TO ADD*, and will click
"+ Field" to create any of them when a verified value has been proposed.
Guards:

- A field is **only created when a value is about to be written** — never
  speculatively, never left blank.
- Creation goes through the same save-and-verify path as any other edit; if
  the value doesn't read back, it's reported as failed.
- A field that is neither present nor on that list is still rejected.
- These controls only appear for absent fields, so this can never overwrite or
  clear existing data.
- Created fields are listed in the run summary.

Because this widens what the agent writes beyond the original eight fields,
run it in Approval Mode for a few records and check the created values before
trusting Auto Mode.

**v12.20 adds a policy for how many companies' data are mixed into one
record**, and a matching safety hold. Previously, any contamination that
touched a non-editable field forced `MANUAL_REVIEW` (v12.18) regardless of
how tangled it actually was. That's still right for genuinely unrecoverable
records, but it also caught a narrower, fixable case: the company name is
wrong, but the description/specialty/products consistently and coherently
describe one other real company. The prompt now tells the model to count how
many distinct companies a record's fields actually point to:

- **Three or more** distinct companies mixed in → `MANUAL_REVIEW`, naming
  each company and which fields pointed to it. Unchanged in spirit from
  before, just made explicit as a counting rule rather than left implicit.
- **Exactly two**, where company_name is the sole outlier and everything else
  consistently points to one other company → independently verify that other
  company from its own sources, run the normal scope check against it, and
  only if it passes, correct the company-name field and rebuild every other
  proposed field from that company's own verified information (never a
  leftover from the original, wrong identity).
- Anything less than fully clean about that two-way split still falls back
  to `MANUAL_REVIEW`.

Because renaming a record's own identity field is a bigger consequence than
an ordinary field fix, the correction is still applied when proposed, but
Auto Mode now always holds the final Platform-ready action on it for a human
to glance at first (`HOLD_ON_IDENTITY_RENAME`) - same hold-and-log mechanism
as v12.19's unresolved-fields case, logged to the same `FIELD_HOLD_LOG` with
an added `identity_field_renamed` column.

## Files

| File | Purpose |
|---|---|
| `main.py` | Playwright automation, prompt, scope gate, correction/verification |
| `llm.py` | OpenRouter client — model fallback, throttling, retries, quota check |
| `evidence.py` | Live web evidence collection, `website_matches_company()` sanity check |
| `jsonutil.py` | Tolerant JSON extraction (fences, trailing text, `<think>` blocks) |
| `config.py` | All settings — models, limits, toggles |
| `history.py` | Supplier decision history workbook — Accepted / Rejected / undecided sheets, de-duplication, lock recovery |
| `accepted_snapshots.py` | Accepted companies workbook — corrected record re-read just before Platform ready, accepted only (v12.28) |

## Tuning

In `config.py`:

- `RESEARCH_BACKEND` — `"api"` or `"manual"` default
- `OPENROUTER_MODELS` — the fallback chain
- `OPENROUTER_MIN_INTERVAL` — seconds between calls (3.5 ≈ 17 req/min)
- `ENABLE_WEB_EVIDENCE` — turn evidence fetching off (rejects nearly everything)
- `EVIDENCE_PAGE_CHARS` — per-source text cap sent to the model
- `CONTINUE_ON_RESEARCH_FAILURE` — skip a failed record vs. stop the session
- `ACCEPT_NON_US` — set to `False` to return to US-only
- `NON_US_LOG` — path of the origin-note CSV
- `ENABLE_MANUAL_REVIEW` — set to `False` to downgrade `MANUAL_REVIEW` decisions to REJECT instead of queuing them
- `MANUAL_REVIEW_LOG` — path of the manual-review queue CSV
- `FIELD_DISCOVERY_MAX_ATTEMPTS` / `FIELD_DISCOVERY_RETRY_DELAY_MS` — how hard
  to retry reading the page's fields before skipping the record (v12.18)
- `FIELD_DISCOVERY_FAILURE_LOG` — path of the skipped-record CSV when discovery never recovers
- `PAGE_CONTEXT_SELECTOR` — CSS selector to scope page-text extraction to one
  record-card container instead of the whole `<body>`; unset by default
- `VERIFY_WEBSITE_BEFORE_APPLY` — turn off the independent website fetch-and-check before applying `website_url` (v12.18)
- `WEBSITE_VERIFY_TIMEOUT_MS` / `WEBSITE_VERIFY_LOG` — timeout and audit-log path for that check
- `HOLD_ACCEPT_ON_UNRESOLVED_FIELDS` — set to `False` to go back to Auto Mode clicking Platform ready on an ACCEPT regardless of unresolved flagged fields (v12.19)
- `FIELD_HOLD_LOG` — path of the held-record CSV when Auto Mode holds an ACCEPT instead
- `HOLD_ON_IDENTITY_RENAME` — set to `False` to let Auto Mode finalize a company-name correction same as any other clean ACCEPT (v12.20)
- `ENABLE_HISTORY_EXCEL` — set to `False` to stop writing the supplier history workbook (v12.26)
- `HISTORY_EXCEL_FILE` — path of the workbook; `HISTORY_ACCEPTED_SHEET` / `HISTORY_REJECTED_SHEET` / `HISTORY_UNDECIDED_SHEET` name its three sheets
- `HISTORY_PENDING_FILE` — spillover queue used when the workbook is locked (open in Excel) or `openpyxl` is missing; merged in automatically on the next successful save
- `HISTORY_WARN_ON_REPEAT` — set to `False` to stop printing the previous decision when an already-processed supplier comes round again
- `ENABLE_ACCEPTED_SNAPSHOT` — set to `False` to stop writing `accepted_companies.xlsx` (v12.28)
- `ACCEPTED_SNAPSHOT_FILE` / `ACCEPTED_SNAPSHOT_SHEET` / `ACCEPTED_SNAPSHOT_PENDING_FILE` — workbook path, sheet name, and the queue used while it is open in Excel

