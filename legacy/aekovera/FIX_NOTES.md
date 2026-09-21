# v12.25 — why 100 iterations only finished 46 companies, and the fix

## What the log actually shows

| Company | Iterations burned |
|---|---|
| K-town Bulgogi | 8 |
| Acai Roots | 6 |
| Maretai Organics | 4 |
| Lone Star, Terrasoul, Vannspices, Gusto Italiano, Ricky Powe, Regal Kitchen, Universal Specialty, Cali Dumpling | 3 each |
| Quality Pork, Satispie, Hartung, Verde Fruits, CFS, Going Nuts, Delizia, Fruitelli, Olde Craft, IE Mills, Ice Cream For Bears, Baobab, Unipharm | 2 each |

**54 of the 100 iterations were the same supplier being served again** — each one a fresh ChatGPT round-trip.

Every repeat has the same signature:

```
⌫ Cleared contaminated dba_name ...
↷ Skipped primary_phone: not present in the database/UI ...   ← field IS in "Current fields" above
✓ Final action triggered with keyboard shortcut G (Platform ready).
... next iteration: same company, only the first change persisted
```

## Root cause (code, not ChatGPT, not the human)

`Save correction`, `Platform ready`, `Reject` and `Skip` are all real `<form>` POSTs — the page reloads.
`main.py` waited a flat **150 ms** after clicking Save, then immediately queried
`input[type=hidden][name=field][value=…]` for the next change. Mid-reload that
locator counts **0**, so every remaining change was reported as *"not present in
the current UI"*. It then pressed `G` into a page that was still being replaced,
the keypress was swallowed, and the same supplier came back. The `✓ Final action
triggered` line only meant "the keypress didn't throw", not "the record advanced".

The field-update loop itself is not slow — it was being *aborted* after the first save.

## What changed

**`main.py`**
1. `wait_for_page_settle()` — after every Save/verdict click, wait for `load` → `networkidle` → hidden field inputs back in the DOM (max 4 s), instead of 150–300 ms.
2. `field_present()` — polls up to 2.5 s for a field's hidden input before declaring it absent. Replaces the three one-shot `hidden.count()` gates (apply, clear, ADD MISSING).
3. `perform_final_action()` — settles first, reads the MASTER id, clicks the button (falls back to `G`/`R`), then **waits for the MASTER id to change**. Retries up to 3× and only prints ✓ once the page really advanced. `skip_current_record()` gets the same verification.
4. Change filter — a proposed value identical to what the record already holds is skipped (`record already holds this value`), so a re-served record never re-POSTs the same text.
5. Same-supplier guard — if the record on screen has the same MASTER id / company name as the one just finalized, the previous ChatGPT result is **reused** (no clipboard round-trip), only the leftover changes are applied, and the verdict is pressed again. Repeat passes do **not** count toward the run count. Capped at `MAX_REPEAT_PASSES = 2`, then it researches afresh.

**`config.py`** (new, at the bottom)
```
POST_SETTLE_TIMEOUT_MS    = 4000
FIELD_PRESENT_WAIT_MS     = 2500
ADVANCE_VERIFY_TIMEOUT_MS = 5000
FINAL_ACTION_MAX_ATTEMPTS = 3
REUSE_RESULT_ON_REPEAT    = True
MAX_REPEAT_PASSES         = 2
```

## Expected effect

- One iteration per company: all clears + applies land in a single pass, then one verified verdict.
- "100 records" now means 100 **distinct** suppliers; the session summary prints how many repeat passes happened (should be ~0).
- Per-record machine time goes *up* slightly (it now genuinely waits for each POST, ~0.3–1 s each) but total time for 100 companies drops from ~110 min (extrapolated) to roughly 100 × (10 s clipboard + 15–25 s applies/verdict) ≈ **40–55 min**.

## How to roll out

1. Back up your current `main.py` / `config.py`, drop in the two files here (or apply `aekovera_v12_25.diff`).
2. Run a 5-record pilot. Watch for:
   - no `not present in the current UI` lines on fields that are listed in "Current fields";
   - `✓ Platform ready confirmed via … page advanced (MST-x → MST-y)` instead of the old bare ✓;
   - `Repeat passes : 0` in the session summary.
3. If a `⚠ … did not advance the page` retry line appears often, raise `ADVANCE_VERIFY_TIMEOUT_MS` — that means the server is slower than 5 s to serve the next supplier.

## Not touched (still worth doing later)
- Records #33 / #53 (`empty value proposed with no clear flag`) and #90 (identity rename) are policy holds, not bugs — `HOLD_ACCEPT_ON_UNRESOLVED_FIELDS` / `HOLD_ON_IDENTITY_RENAME` in `config.py`.
- The `⚠ Payload is 2,300 chars (target ≤900)` warning fires on every record: the project-mode prompt is not actually compact. Separate issue.
