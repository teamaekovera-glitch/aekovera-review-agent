"""Interactive CLI - the thin human-facing wrapper around BatchRunner.

Choosers, the manual login gate, and the browser session live here; every
decision the pipeline makes lives in review_hub.engine.runner. The legacy
main() flow is preserved: mode choice, backend choice (manual is the
DEFAULT), run count, ChatGPT-Project setup note, browser login prompt, then
the batch loop.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from review_hub.config import (
    BASE_URL,
    CHATGPT_PROJECT_MODE,
    RESEARCH_BACKEND,
    RUN_LOG_DIR,
)
from review_hub.engine.corrections import CorrectionApplier
from review_hub.engine.prompting import build_research_prompt
from review_hub.engine.research import LLMError
from review_hub.engine.research.manual import ManualChatGPTBackend
from review_hub.engine.research.obvious import ObviousAgentClient
from review_hub.engine.research.openrouter import OpenRouterClient
from review_hub.engine.runner import BatchRunner, PageOps
from review_hub.engine.session import open_review_page, open_session, transport_ready_banner
from review_hub.engine.transport import QAClient
from review_hub.persistence import FileTransitionSink, NullSink


def choose_mode() -> str | None:
    """Auto mode (agent clicks the verdict) or approval mode (human does)."""
    print("\n" + "=" * 42)
    print("            REVIEW RUN MODE")
    print("=" * 42)
    print("1. Auto mode      (agent submits verdicts; holds skip instead)")
    print("2. Approval mode  (agent prepares; human clicks every verdict)")
    value = input("Select mode [1]: ").strip()
    if value in ("", "1"):
        return "auto"
    if value == "2":
        return "approval"
    print("Enter 1 or 2.")
    return None


def choose_backend(default: str = RESEARCH_BACKEND) -> str:
    print("\n" + "=" * 42)
    print("            RESEARCH BACKEND")
    print("=" * 42)
    print("1. OpenRouter API  (automated - free models only, no copy/paste)")
    print("2. Manual ChatGPT  (clipboard workflow - the default)")
    print("3. Obvious agent   (automated web research - Obvious credits)")
    print("=" * 42)
    while True:
        value = input(f"Select backend [{'1' if default == 'api' else '2'}]: ").strip()
        if value == "":
            return default
        if value == "1":
            return "api"
        if value == "2":
            return "manual"
        if value == "3":
            return "obvious"
        print("Enter 1, 2, or 3.")


def preflight_api(backend: OpenRouterClient) -> bool:
    """Verify the key is present before any browser work starts."""
    try:
        backend.check_api_key()
    except LLMError as exc:
        print(f"\n✗ {exc}")
        return False
    print("✓ OPENROUTER_API_KEY is set.")
    return True


def preflight_obvious(backend: ObviousAgentClient) -> bool:
    """Verify key, project, and relay are configured before the browser opens."""
    problems = backend.preflight()
    if problems:
        print("\n" + "\n".join(problems))
        return False
    print("✓ Obvious config is set (API key, project, relay).")
    return True


def choose_run_count() -> int | None:
    value = input("\nHow many records this run? [1]: ").strip()
    if value == "":
        return 1
    try:
        count = int(value)
    except ValueError:
        print("Enter a number (or ENTER for 1).")
        return None
    return count if count > 0 else None


def build_backend(name: str) -> Any:
    if name == "api":
        return OpenRouterClient()
    if name == "obvious":
        return ObviousAgentClient()
    return ManualChatGPTBackend()


BACKEND_LABELS = {"api": "openrouter", "manual": "manual chatgpt", "obvious": "obvious agent"}


def default_jsonl_sink():
    """The file-backed JSONL run log (kept for CLI/debug via configuration)."""
    try:
        return FileTransitionSink(f"{RUN_LOG_DIR}/transitions.jsonl")
    except OSError:
        return NullSink()


def build_persistence():
    """The BatchRunner's persistence layer: (sink, lifecycle).

    The SQLite lifecycle store is the default - its transition sink writes
    the same records the JSONL sink does, into the system of record, and the
    RunLifecycle tracks the run's parked/resumed state there. The file-backed
    JSONL sink stays available for CLI/debug via ``REVIEW_HUB_SINK=jsonl``,
    and serves as the fallback when the store cannot be opened (never
    silently: the fallback prints why).
    """
    from review_hub.lifecycle import RunLifecycle
    from review_hub.store.repository import ReviewStore

    if os.environ.get("REVIEW_HUB_SINK", "store").lower() == "jsonl":
        return default_jsonl_sink(), None
    try:
        store = ReviewStore(f"{RUN_LOG_DIR}/review.db")
    except Exception as exc:
        print(f"⚠ SQLite store unavailable ({exc}); using the JSONL run log.")
        return default_jsonl_sink(), None
    return store.sink(), RunLifecycle(store)


def main() -> None:
    mode = choose_mode()
    if mode is None:
        return

    backend_name = choose_backend()
    backend = build_backend(backend_name)
    if backend_name == "api" and not preflight_api(backend):
        print("Cannot start the automated backend. Fix the API key and retry.")
        return
    if backend_name == "obvious" and not preflight_obvious(backend):
        print("Cannot start the Obvious backend. Fix the configuration above and retry.")
        return

    run_count = choose_run_count()
    if run_count is None:
        print("Run cancelled.")
        return

    if backend_name == "manual" and CHATGPT_PROJECT_MODE:
        print(
            "\nNOTE: ChatGPT Project mode is on. The manual backend builds "
            "compact prompts (rules live in your Project instructions)."
        )

    print(f"\nMODE: {mode.upper()}")
    print(f"BACKEND: {BACKEND_LABELS.get(backend_name, backend_name)}")
    print(f"Run count: {run_count} records")
    print(f"Session started: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    with open_session() as context:
        transport = QAClient(context, base_url=BASE_URL)
        print(transport_ready_banner(BASE_URL))

        page = open_review_page(context)
        print("\nBrowser opened.")
        print("Log in manually if necessary.")
        input("When the supplier review page is visible, press ENTER...")

        def build_prompt(record: dict) -> str:
            # ChatGPT does the research itself in manual mode, so the
            # rulebook must not tell it that it has no browsing.
            return build_research_prompt(record, browsing=True)

        ops = PageOps(transport=transport, applier=CorrectionApplier(transport=transport))
        sink, lifecycle = build_persistence()
        runner = BatchRunner(
            ops=ops,
            backend=backend,
            sink=sink,
            lifecycle=lifecycle,
            mode=mode,
            build_prompt=build_prompt,
        )
        summary = runner.run(page, run_count)

        print("\n" + "=" * 50)
        print("Session finished.")
        print(f"Records processed : {summary['processed']}")
        print(f"Repeat passes     : {summary['repeat_passes_total']}")
        print(f"Time used         : {summary['elapsed_s']:.0f}s")
        for key, count in sorted(summary["decision_tally"].items(), key=lambda kv: -kv[1]):
            print(f"   {count:>3}  {key}")
        print(transport.summary())
        for warning in transport.health_warnings():
            print(f"⚠ {warning}")
        print("=" * 50)


def run() -> None:
    """Entry point wrapper: exit cleanly on Ctrl+C."""
    start = time.time()
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        print(f"Time used before interrupt: {time.time() - start:.0f}s")
        print("No further action was performed.")
        sys.exit(130)


if __name__ == "__main__":
    run()
