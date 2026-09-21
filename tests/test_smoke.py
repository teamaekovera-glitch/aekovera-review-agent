"""Smoke test: the harness is real from PR #1."""

import review_hub


def test_review_hub_imports() -> None:
    """The package resolves and imports cleanly."""
    assert review_hub.__version__ == "0.1.0"
