"""Playwright session setup - the persistent browser context from legacy main().

The browser profile directory is reused on every run so the operator's login
survives restarts (no credentials are handled here - the agent inherits the
existing cookie jar). The operator logs in manually when prompted.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from review_hub.config import BASE_URL, PROFILE_DIR, REVIEW_URL

# browser_profile/ is a persistent context reused on every run, so its disk
# cache otherwise grows unbounded across hundreds of records/sessions. Cap it
# so a bloated or corrupted cache can't crash Chromium mid-run.
DISK_CACHE_SIZE_BYTES = 104857600  # 100MB


def default_profile_dir() -> str:
    """Resolve the browser profile directory (config override → repo default)."""
    return os.path.expanduser(str(PROFILE_DIR))


def launch_persistent_context(playwright: Any, profile_dir: str | None = None) -> Any:
    """Launch Chromium exactly as legacy v34 did (headful, maximized, capped cache)."""
    return playwright.chromium.launch_persistent_context(
        profile_dir or default_profile_dir(),
        headless=False,
        viewport=None,
        args=[
            "--start-maximized",
            f"--disk-cache-size={DISK_CACHE_SIZE_BYTES}",
        ],
    )


def open_review_page(context: Any) -> Any:
    """Open the review page on the context's first tab (or a new one)."""
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(REVIEW_URL, wait_until="domcontentloaded")
    return page


@contextmanager
def open_session(profile_dir: str | None = None) -> Iterator[Any]:
    """Yield a live browser context with the operator's persistent profile.

    Callers are responsible for logging in manually if necessary and for
    building the QAClient on this context (the cookie jar is shared).
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = launch_persistent_context(p, profile_dir)
        try:
            yield context
        finally:
            context.close()


def transport_ready_banner(base_url: str = BASE_URL) -> str:
    """The legacy one-line confirmation that edits/verdicts use the app's own endpoints."""
    return (
        f"✓ HTTP transport ready ({base_url}) — edits and verdicts "
        f"go straight to the app's own endpoints."
    )
