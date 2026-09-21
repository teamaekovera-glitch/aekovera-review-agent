# Aekovera Review Hub

An internal supplier-review and enrichment control plane for consumer packaged goods (CPG)
sourcing. It productizes the v34 supplier-review script: a hardened Python engine
(`src/review_hub`) researches supplier companies, evaluates sourcing fit across food,
beverage, ingredient, and supplement categories, applies only verified corrections, and
routes uncertain, unsafe, or held records to human review with auditable outcomes. Manual
ChatGPT research is the default backend; a FastAPI web control plane manages run lifecycle
and review queues, with SQLite as the system of record and Excel/CSV as exports.

## Setup

> Placeholder - finalized in the last PR of the build plan.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
pytest
```

## Legacy reference

`legacy/` contains the unmodified v34 supplier-review sources, preserved verbatim as the
porting reference. Do not edit files there; port logic into `src/review_hub`.
