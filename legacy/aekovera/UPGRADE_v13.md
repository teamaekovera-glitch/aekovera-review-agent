# v13 — HTTP transport, v4 rulebook, verdict binding

## What changed and why

### 1. Edits and verdicts no longer go through the DOM

Every action on the review page is a plain HTML form POST. From
`qa_app/app.py` in the platform repo:

    Route("/verdict", verdict, methods=["POST"])
    Route("/edit",    edit,    methods=["POST"])
    Route("/skip",    skip,    methods=["POST"])

`qa_http.py` posts to those directly, using the browser context's own cookie
jar, so the agent inherits your existing login and open shift. **Nothing in
the platform repo changes** — this speaks the protocol the browser already
speaks.

Per field, the old path was: find the pencil → click → wait for the form →
fill → click Save → wait for the redirect → re-read the card to verify.
It is now one request.

### 2. The double-accept bug is fixed structurally

This was a real defect, not a hypothetical. `perform_final_action()` called
`click_or_shortcut()`, which retried up to `FINAL_ACTION_MAX_ATTEMPTS`
whenever it could not *see* the page advance:

    after_id = wait_for_record_change(page, before_id)
    if after_id or not before_id: return
    ...retry: page.keyboard.press("g")

A verdict that saved while the redirect was slow is indistinguishable, in the
DOM, from one that was lost. The retry then pressed **G again on a page that
had already moved to the next company** — accepting it unreviewed.

`db.save_verdict` matches both the unit_id and the card's nonce:

    if existing or row["status"] == "done":  return "already"
    if (row["leased_by"] != member or row["nonce"] != nonce ...): return "lease_lost"

So a verdict POST is bound to one specific company. A replay returns
`already` and does nothing. It *cannot* land on the next company.

On top of that, **a verdict is never retried.** A timeout is genuinely
ambiguous — the write may have committed — so the agent stops and tells you
to check that company, rather than guessing.

### 3. Redirects are never followed

`GET /review` calls `db.claim_next`, which **leases a card**. Every request
here uses `max_redirects=0` and reads the outcome from the `Location` header.
Following redirects would silently lease and orphan an extra company per POST.

### 4. The v4 rulebook replaces the embedded prompt

`prompts/judge_v4.md` holds the rulebook — edit it without touching code.
The decision vocabulary changed, and `decision_v4.py` enforces it:

| decision  | button              | verdict |
|-----------|---------------------|---------|
| ACCEPT    | Platform ready (G)  | green   |
| PARK      | Outreach first (O)  | orange  |
| RE_ENRICH | Re-enrich (Y)       | yellow  |
| REJECT    | Reject (R)          | red     |

`PARK` and `RE_ENRICH` were previously unreachable — the agent only ever
accepted or rejected. Everything that should have been outreach or re-enrich
was being forced into one of two buckets.

Ambiguity always resolves **away from ACCEPT**: unreadable `scope_match`,
a contract type with no `type_quote`, unconfirmed identity, no classified
type, or a conflated record all downgrade. A change missing `source_url` or
`quote` is dropped rather than written.

## What I could not verify

I have no access to your VPS, so nothing here has run against the live app.
Everything is tested against a local reimplementation of `save_verdict`'s
rules (`test_pipeline.py`, 46 assertions, all passing).

**Run the first batch in Approval Mode.** Specifically check:

1. `read_card_identity()` finds `unit_id` and `nonce`. It reads
   `input[name='unit_id']` and `input[name='nonce']`, which is what `html.py`
   renders — but confirm against your deployed template.
2. The redirect messages match. `_MESSAGE_TO_OUTCOME` expects
   `"Saved — next supplier ready."` with an em dash; there is fuzzy matching
   behind it, but an unrecognised message raises rather than assuming success.
3. Field edits land. Compare a few corrected records in `/admin` against what
   the agent reported.

## Not done

- `supplier_type` posts the pipe-joined string in `new_value`. The UI sends
  `new_value_multi[]` from a dropdown. The server falls back to `new_value`,
  but confirm this saves before trusting it on a batch.
- The DOM fallback (`_apply_text_change_via_dom`) is retained but now
  unexercised in normal runs.
- Throughput: the transport tracks `request_seconds` and
  `health_warnings()` flags a >25% edit failure rate, but I have no baseline
  from your machine to quote a speedup figure against.

---

## v13.1 — fixes "Invalid decision: PARK"

The first build swapped the prompt but left `validate_scope_result()` speaking
the old vocabulary:

    valid_decisions = {"ACCEPT", "REJECT"}
    if ENABLE_MANUAL_REVIEW: valid_decisions.add("MANUAL_REVIEW")
    if decision not in valid_decisions:
        raise ValueError(f"Invalid decision: {decision}")

So the model correctly returned `PARK`, the gate threw it out, and the record
was skipped — research spent, nothing saved. `RE_ENRICH` would have failed the
same way.

That 259-line function is now a wrapper around `decision_v4.validate()`, and
the main loop's second gate derives its list from `DECISION_TO_VERDICT` rather
than hard-coding it, so the two can no longer drift apart. The regression is
pinned in `test_pipeline.py` for all four verdicts.

---

## v13.2 — audit against the original 12 requirements

v13.1 left four write paths on the DOM and two monitors unwired. All fixed.

| Path | Before | Now | Repo fact that makes it safe |
|---|---|---|---|
| clear a field | pencil → empty → save | `POST /edit new_value=""` | `qa.edits.new_value TEXT NOT NULL` accepts `""`; only `company_name` is blocked (<2 chars), which is correct |
| supplier_type | dropdown clicks | `POST /edit "A \| B"` | `_valid_edit` validates each part against `SUPPLIER_TYPES` |
| ADD MISSING | click "+ Field" then fill | `POST /edit` directly | `save_edit` uses `jsonb_set(card, …, TRUE)` — creates the key when absent |
| skip | keyboard `s`, **pressed twice on no-advance** | `POST /skip unit_id+nonce`, never retried | same retry-on-moved-page defect the verdict had |
| run summary + health warnings | dead code | printed at end of run | — |

Every write now goes through one of three endpoints: `/edit`, `/verdict`,
`/skip`. Every one is bound to a specific `unit_id`; the two that change
queue state are also bound to the `nonce`. No path presses a key.

The DOM functions are retained as `_*_via_dom` fallbacks, unexercised when
the transport is up. `addmissing.py` is unchanged; its `create_field()` is
only reached on the fallback path.

56 tests pass.

---

## v13.3 — `Source URL must start with http:// or https://`

`/edit` treats `source_url` as optional, but if present it must pass
`html.valid_url` (absolute http(s) + host) or the **whole edit is refused**.
The judge cites provenance that is often not a URL — `"maps card"`,
`"search results"` — and the transport forwarded it raw, so four correct
address fields on EMPWR Nutrition were lost to an optional field.

Transport now mirrors `valid_url`: a real URL is sent, anything else is
dropped with a log line and counted in the run summary. The edit always goes
through. Also corrected: `clear_field` docstring claimed the reason was sent
as `source_url`; it never was, and the form has no field for it.

61 tests pass.

---

## v13.4 — 9 of 10 companies came back PARK

Not a model problem and not bad luck. The manual ChatGPT backend was sending
ChatGPT a rulebook that told it it could not browse.

`judge_v4.md` section 1 was written for the API backend, where `evidence.py`
fetches the website and search results and attaches them as MATERIAL:

    You have NO live browsing unless the message explicitly lists tools. Never
    write "verified", "confirmed" or "the site shows" about anything that is
    not literally in the material.

But `copy_research_request()` calls `build_research_prompt(record)` with no
evidence, so the only "material" was the record fields and ~700 chars of
review-page text. ACCEPT requires an identity-verified live site and a working
contact, neither of which can be proven from that, so a model obeying the
rules returns PARK (`unverified_identity` / `thin_data`) with lookups listed in
`needs` — which nothing in the agent ever fulfils. The old v12 prompt said
"Independently identify the actual company using web research"; v4 silently
dropped that for the manual path. `write_project_instructions()` still said
"do the web research", contradicting section 1 two screens earlier.

### Fixes

1. `prompts/material_browsing.md` replaces section 1 for the manual backend
   (`decision_v4.load_prompt(browsing=True)`): search the name, open the site
   and contact page, never park because "the site text wasn't included",
   `needs` must be empty, and "working contact" means published on the site —
   not tested by calling. The API backend keeps the original section 1.
   Substitution fails loudly if the headings change.
2. Every compact per-record paste now says to browse before deciding.
3. `site_identity` is read tolerantly. `"Confirmed - name on contact page"`
   used to fail the exact `== "confirmed"` check and downgrade a valid ACCEPT
   to PARK. Negatives (`not confirmed`, `unconfirmed`) still park.
4. A non-empty `needs` prints a warning that the decision is provisional.
5. API `SYSTEM_PROMPT` no longer says "when evidence is insufficient, you
   reject" (v4 says PARK/RE_ENRICH for real in-scope companies).
6. Session summary prints the decision mix, marking gate downgrades, so the
   next skew shows where it came from.

### You must do this once

Run the agent, pick Manual, and **replace** the ChatGPT Project instructions
with the newly written `chatgpt_project_instructions.txt`. The old text still
says "NO live browsing", and Project mode does not resend the rules per record.
Also make sure web search is enabled for chats in that Project.

75 tests pass.
