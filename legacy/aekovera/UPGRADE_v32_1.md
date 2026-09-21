# v32.1 / v14.1 — accepted_companies.xlsx now holds the corrected record

## The bug (you found it)

Since v13, corrections are saved with direct HTTP POSTs (`qa_http.apply_edit`), sent with
`max_redirects=0`. The browser tab never navigates, so it keeps showing the card as it was
rendered **before** the corrections.

`fetch_corrected_record()` claimed to "re-read the page after every correction had been
saved". What it actually did was wait for a page that was already loaded, then read that
stale page's hidden `new_value` inputs.

So the accepted-companies row was built from the **original** data. Comparing the original
to itself, `edit_status` came out as `no changes needed` on every row. The console still
printed `✓ Corrected record fetched from the page`, which hid the problem.

`test_snapshot.py` reproduces this. On the old code, with the new values already saved on
the server, the "corrected" snapshot still returns `spm@smingredients.com` and `12345`.

## The new order

```
corrections saved over HTTP
  -> page.reload()                      (server renders the saved values)
  -> same card?  unit_id and MASTER id must match, otherwise SAFETY STOP, no verdict
  -> extract every field from the reloaded page
  -> check every saved correction is really on the record
  -> write the row to accepted_companies.xlsx   (verdict_status = "pending ...")
  -> press Platform ready / Reject
  -> Platform ready landed?  yes: verdict_status = "accepted"
                             no:  row removed (or the company's earlier accepted row restored)
```

## What else changed

- **Corrections that did not land.** Each correction is compared with the reloaded page;
  phone formats and supplier-type order are normalized so they don't cause false alarms.
  - Auto mode: an ACCEPT with any mismatch is held (skipped), not accepted.
  - Approval mode: a warning is printed before you click.
  - Both controlled by `HOLD_ACCEPT_IF_EDITS_NOT_LANDED`.
- **Reload failed or the page could not be read.** Auto mode holds the ACCEPT
  (`HOLD_ACCEPT_IF_SNAPSHOT_FAILED`).
- **New columns in accepted_companies.xlsx:**
  - `verdict_status`
  - `fields_not_landed`
  - `snapshot_source` now reads "page reloaded and re-read after corrections, before the
    verdict"
- **supplier_history.xlsx** also takes its company columns from the reloaded page when
  available, so both workbooks agree.
- **`config.py`** has a new block at the bottom with `RELOAD_BEFORE_FINAL_SNAPSHOT` and the
  two hold switches.

## Watch on the first run

`GET /review` leases a card. The fix assumes that reloading while you already hold a lease
gives you **the same card back**; normal browser refreshes behave that way. The code does
not trust this: it compares unit_id and MASTER id after the reload.

If the server ever serves a different card, you'll see:

    ✗ SAFETY STOP: after the reload the page shows unit ... but the corrections were made on unit ...

The run then stops with nothing pressed and nothing written. If that happens on your
server, tell me — the fix then needs a different read path instead of a reload.

Also confirm in the log, per accepted company:

    ↻ Reloading the review page so the saved corrections are shown...
    ✓ Final record re-read from the reloaded page (N fields).
    ✓ All N correction(s) confirmed on the reloaded record.
    ✓ Reloaded record written to accepted_companies.xlsx ... (pending until Platform ready lands)
    ✓ Accepted company confirmed in accepted_companies.xlsx

Tests: `python test_snapshot.py` (20 new), plus the existing suites, all passing.
