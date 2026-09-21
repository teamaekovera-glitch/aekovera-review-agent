# Aekovera Review Hub

An internal supplier-review and enrichment control plane for consumer packaged goods (CPG)
sourcing. It productizes the v34 supplier-review script: a hardened Python engine
(`src/review_hub`) researches supplier companies, evaluates sourcing fit across food,
beverage, ingredient, and supplement categories, applies only verified corrections, and
routes uncertain, unsafe, or held records to human review with auditable outcomes. Manual
ChatGPT research is the default backend; Obvious agent sessions and OpenRouter are the
automated options; a FastAPI web control plane manages run lifecycle
and review queues, with SQLite as the system of record and Excel/CSV as exports.

## Architecture

Four layers, each replaceable behind the previous one's seams:

| Layer | Where | Responsibility |
|---|---|---|
| Engine | `src/review_hub/engine` | The ported v34 pipeline: transport, DOM discovery, evidence collection, research backends (manual ChatGPT clipboard workflow, Obvious agent sessions, or OpenRouter API), corrections, decision gates, and the `BatchRunner` state machine. Holds, the no-redirect rule, the repeat guard, and ambiguity-away-from-ACCEPT are pinned by tests. |
| Store | `src/review_hub/store` | Versioned SQLite (WAL) system of record: runs, records, transitions, holds, audit events, evidence, history, accepted snapshots — plus the v34-parity CSV/XLSX exports. |
| API | `src/review_hub/server` | FastAPI control plane: run creation/control, manual paste responses, queues, history, settings (validated allowlist), and export downloads. |
| Dashboard | `src/review_hub/server/dashboard.py`, `templates/`, `static/` | Server-rendered operator UI (Jinja2 + minimal vanilla JS polling) over the same JSON API. The dashboard adapts to the API — never the reverse. |

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium   # the engine drives a real Chromium session
```

Run the control plane and dashboard:

```bash
python -m review_hub.server
# → http://127.0.0.1:8734 (JSON index at /, operator dashboard at /dashboard)
```

The SQLite store is created on first boot (`review_hub.db` in the working directory by
default; override with `REVIEW_HUB_DB`). Bind address and port are `REVIEW_HUB_HOST`
(default `127.0.0.1`) and `REVIEW_HUB_PORT` (default `8734`).

## Operations

### CLI workflow (browser in your hands)

```bash
review-hub
```

An interactive session: choose **auto mode** (agent submits verdicts; holds skip instead)
or **approval mode** (human clicks every verdict), choose the research backend (manual
ChatGPT clipboard workflow is the default; OpenRouter API and Obvious agent sessions are
the automated options — see below), pick a run count, log into the review page in the
opened browser, and press ENTER. The `BatchRunner` then processes records, printing
per-record progress and a final decision tally. Set `REVIEW_HUB_SINK=jsonl` to also keep a
file-backed JSONL transition log for debugging.

### Obvious agent backend (option 3)

The Obvious backend runs each research prompt as an autonomous Obvious agent session: the
agent searches the web and reads supplier pages itself — the same evidence quality as the
manual ChatGPT workflow, without the human. The handshake preserves the local-first
posture: the pipeline only makes **outbound** requests.

1. The pipeline POSTs the research thread to the Obvious External Developer API
   (`/api/v1/projects/{project_id}/thread`), embedding the answer-delivery contract in all
   three prompts (`starterPrompt`, `successPrompt`, `failurePrompt`).
2. The agent researches, then POSTs its final JSON (a v34 decision, or a structured
   failure report) to the public answer relay — `relay/obvious_relay.py`, a
   dependency-free stdlib server that is token-gated, disk-persistent, and
   **first-write-wins** (a late failure report can never overwrite a delivered answer).
3. The pipeline polls the relay until the answer lands or the 15-minute timeout raises the
   usual loud `LLMError` (the runner's research-failure path).

Configuration is environment-only, checked by a preflight before the browser opens:

| Variable | Purpose |
|---|---|
| `OBVIOUS_API_KEY` | Obvious External Access API key (`obv_…`, admin-created) |
| `OBVIOUS_PROJECT_ID` | The `prj_…` project the research sessions run in |
| `OBVIOUS_RELAY_URL` | Base URL of the deployed answer relay |
| `OBVIOUS_RELAY_TOKEN` | The relay's shared token |

Run the relay on any small host: `OBVIOUS_RELAY_TOKEN=… python3 relay/obvious_relay.py
--port 8801`. Unlike manual ChatGPT (free) and OpenRouter (free models only), Obvious
sessions consume Obvious credits — check project credit burn per research call before
scaling.

### Dashboard workflow (browser as a monitor)

Everything the terminal can do is on screen at `http://127.0.0.1:8734/dashboard`:

- **Overview** — start a run (mode, backend, record count) and watch every run's status,
  per-record progress, and worker liveness.
- **Run monitor** — per-record rows with decisions, a decision tally, run controls
  (pause/resume/cancel), and the status event log.
- **Manual review** — when a run parks at `awaiting_manual`, the run page shows the exact
  research prompt (with a copy button) and a paste box. Paste ChatGPT's raw answer — prose
  around the JSON is fine. An unparsable paste is re-parked with its reason on screen and a
  fresh box; a valid one visibly resumes the record and the run.
- **Queues** — manual-review and held holds, each surfaced with its reason (review reason;
  held fields needing clear/review, identity renames), each with a resolve form
  (outcome + note).
- **Accepted companies / History** — accepted-company snapshots and the full decision
  history, with one-click downloads of the v34-parity exports
  (`/export/accepted-companies.xlsx`, `/export/supplier-history.xlsx`,
  `/export/website-verify-log.csv`).
- **Audit** — per-record inspection: the research prompt, raw response, website checks, and
  collected evidence.
- **Settings** — the engine's operational knobs, read and edited through the API's strict
  typed allowlist (no paths, no URLs, no credentials). Changes apply to new runs
  immediately and persist next to the store for the next boot. Secrets (e.g.
  `OPENROUTER_API_KEY`, `OBVIOUS_API_KEY`, `OBVIOUS_RELAY_TOKEN`) are environment-only and
  render as presence badges — never values.

### Settings and secrets

Editable keys are allowlisted in `src/review_hub/server/settings.py` (booleans, integers,
floats, and string lists with type validation). `OPENROUTER_API_KEY`, `OBVIOUS_API_KEY`,
and `OBVIOUS_RELAY_TOKEN` live only in the environment and are never accepted or echoed by
the API.

## Zero-cost, offline-first posture

- No paid services anywhere in the stack by default: the default research backend is your
  own ChatGPT session via the clipboard workflow; the optional OpenRouter backend is
  configured for free models only. The Obvious backend is the one opt-in that consumes
  metered Obvious credits, and only when you select it.
- The dashboard ships two self-contained static files (CSS + JS), system fonts, and no
  third-party assets — no CDNs, no external fonts, no analytics. Everything works on
  `localhost` with the network unplugged.
- SQLite and file exports are the only storage. Delete the data directory and the system
  is gone — nothing to unsubscribe from.

## Security posture (local-first)

This is a single-operator tool. By design:

- The server binds **127.0.0.1** and there is **no login in v1** — your machine is the
  trust boundary.
- If you must reach the UI from beyond loopback, set `REVIEW_HUB_ACCESS_TOKEN` and bind a
  non-loopback host (`REVIEW_HUB_HOST`); every request — dashboard and API — will then be
  required to present that token (`X-Access-Token` header). The seam is implemented in
  `src/review_hub/server/auth.py` and documented in the settings screen.
- Do not run the server on a shared or untrusted network without understanding that the
  hold/decision data is commercially sensitive.

## Testing

```bash
ruff check .
pytest
```

The suite (184 tests) covers the engine's safety behavior, the store's lifecycle
guarantees, the API contract, and the dashboard's rendered screens — including the
paste-box loop end-to-end, hold reasons on the queues, export links, secret redaction, and
the Obvious backend's dispatch/retry/timeout/parse invariants plus the relay's
first-write-wins and token enforcement contract.

## Legacy reference

`legacy/` contains the unmodified v34 supplier-review sources, preserved verbatim as the
porting reference. Do not edit files there; port logic into `src/review_hub`.
