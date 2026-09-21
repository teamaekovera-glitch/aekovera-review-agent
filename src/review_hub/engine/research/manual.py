"""Manual ChatGPT research backend - the DEFAULT, zero paid resources.

Port of the legacy manual flow from ``main.py``: the prompt goes to the
clipboard (plus a saved file), the operator pastes it into ChatGPT, and the
backend watches the clipboard for the JSON answer. No API key, no service,
no payment - just a human and a browser tab.

Failures raise :class:`ResearchAborted` (a subclass of LLMError) so the
runner's research-failure path stays loud, never silent.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Protocol

from review_hub.config import (
    CLIPBOARD_AUTO_WATCH,
    CLIPBOARD_POLL_INTERVAL,
    CLIPBOARD_WATCH_TIMEOUT,
    LAST_REQUEST_FILE,
)
from review_hub.engine.research import LLMError, ManualPauseRequested
from review_hub.jsonutil import extract_first_json_value

try:  # pyperclip is optional; the saved file fallback still works without it.
    import pyperclip
except ImportError:  # pragma: no cover - exercised only on bare installs
    pyperclip = None  # type: ignore[assignment]


class ResearchAborted(LLMError):
    """The operator cancelled, the watch timed out, or pastes kept failing."""


def release_stuck_clipboard() -> None:
    """Self-heal a known pyperclip Windows issue.

    pyperclip's Windows paste() does OpenClipboard() -> GetClipboardData() ->
    CloseClipboard(), with no try/finally around the middle call. If
    GetClipboardData() ever raises (e.g. the clipboard holds a format other
    than plain CF_UNICODETEXT - such as the rich/HTML clipboard payload some
    browsers write alongside plain text on copy), CloseClipboard() is never
    reached and our process is left holding the Windows clipboard open.
    From that point on, EVERY OpenClipboard() call system-wide fails
    silently - ours and the user's normal Ctrl+C/Ctrl+V - until the lock is
    released, which looks exactly like "the clipboard just stopped working"
    partway through a long run. Force-closing before every clipboard touch
    is harmless when nothing is stuck (CloseClipboard() just no-ops) and
    clears the lock the moment it happens.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - best-effort unlock
        pass


class ManualChatGPTBackend:
    """Copy the prompt, watch the clipboard for ChatGPT's JSON answer."""

    def __init__(
        self,
        *,
        auto_watch: bool = CLIPBOARD_AUTO_WATCH,
        poll_interval: float = CLIPBOARD_POLL_INTERVAL,
        watch_timeout: float = CLIPBOARD_WATCH_TIMEOUT,
        save_path: str | Path = LAST_REQUEST_FILE,
    ) -> None:
        self._auto_watch = auto_watch
        self._poll_interval = poll_interval
        self._watch_timeout = watch_timeout
        self._save_path = Path(save_path)

    # ------------------------------------------------------------------ #
    # Prompt delivery
    # ------------------------------------------------------------------ #
    def copy_research_request(self, prompt: str, *, compact: bool = False) -> None:
        """Hand the research prompt to the operator via the clipboard."""
        char_count = len(prompt)
        if pyperclip is not None:
            release_stuck_clipboard()
            pyperclip.copy(prompt)
            print(f"\nResearch request copied to clipboard ({char_count:,} chars).")
        else:
            print("\npyperclip is not installed; use the saved file below.")
        self._save_path.write_text(prompt, encoding="utf-8")
        print(f"It is also saved as: {self._save_path}")
        if compact:
            print("Paste it into your Aekovera ChatGPT Project (rules already loaded there).")
            if char_count > 1100:
                print(f"⚠ Payload is {char_count} chars (target ≤900). Consider a fresh chat sooner.")
            else:
                print(f"✓ Compact payload ({char_count} chars) — good for longer Project chats.")
        else:
            print("\nPaste it into ChatGPT, complete the research, and ask for ONLY the JSON output.")
        print("Then copy the JSON response to your clipboard.")

    # ------------------------------------------------------------------ #
    # Answer collection
    # ------------------------------------------------------------------ #
    def wait_for_clipboard_json(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        """Watch the clipboard and return as soon as valid JSON appears.

        This removes a keystroke per record: copy ChatGPT's answer and the
        agent picks it up immediately, instead of waiting for you to
        alt-tab and press ENTER. Press Ctrl+C to fall back to the manual
        prompt.
        """
        if pyperclip is None:
            return None
        timeout_s = self._watch_timeout if timeout_s is None else timeout_s

        print("\nWatching the clipboard - just copy ChatGPT's JSON answer.")
        print("(Ctrl+C to enter it manually instead.)")

        deadline = time.time() + timeout_s

        # The clipboard can be transiently locked by another process
        # (clipboard managers, DLP/security tools, sync utilities) right
        # after we just wrote the prompt to it. Retry the initial read the
        # same way the polling loop below retries later reads, instead of
        # giving up after one failed call.
        baseline = None
        while time.time() < deadline:
            try:
                release_stuck_clipboard()
                baseline = pyperclip.paste()
                break
            except Exception:  # noqa: BLE001 - transient lock
                time.sleep(self._poll_interval)
        else:
            print("Could not read the clipboard - falling back to manual paste.")
            return None
        try:
            while time.time() < deadline:
                try:
                    release_stuck_clipboard()
                    current = pyperclip.paste()
                except Exception:  # noqa: BLE001 - transient lock
                    time.sleep(self._poll_interval)
                    continue

                if current and current != baseline:
                    try:
                        value = extract_first_json_value(current)
                        if isinstance(value, dict) and "decision" in value:
                            print("✓ JSON detected on the clipboard.")
                            return value
                        # Changed but not our JSON yet: re-baseline and keep waiting.
                        baseline = current
                    except Exception:  # noqa: BLE001 - not JSON yet
                        baseline = current
                time.sleep(self._poll_interval)
        except KeyboardInterrupt:
            print("\nClipboard watch cancelled.")
            return None

        print("Clipboard watch timed out.")
        return None

    def read_json_from_clipboard(self, max_attempts: int = 3) -> dict[str, Any] | None:
        """Interactive fallback: paste the JSON, press ENTER, retry up to 3x."""
        for attempt in range(1, max_attempts + 1):
            print("\nPaste the ChatGPT JSON into the clipboard, then press ENTER.")
            if attempt > 1:
                print(f"Retry {attempt}/{max_attempts}: copy the JSON response again.")
            input("Press ENTER when the JSON is copied...")

            if pyperclip is None:
                print("pyperclip is not installed; cannot read the clipboard.")
                return None
            release_stuck_clipboard()
            raw = pyperclip.paste()
            try:
                value = extract_first_json_value(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                print("\nCould not parse the clipboard as JSON.")
                print(f"JSON error: {exc}")
                if attempt < max_attempts:
                    print("No correction has been applied. Please copy the JSON response again.")
                    continue
                print("No correction has been applied after 3 attempts.")
                return None
            if value is None:
                continue
            return value
        return None

    # ------------------------------------------------------------------ #
    # ResearchBackend protocol
    # ------------------------------------------------------------------ #
    def research(
        self,
        prompt: str,
        system_prompt: str,
        *,
        extra_fields: dict[str, Any] | None = None,
        compact: bool = False,
    ) -> dict[str, Any]:
        """Copy the prompt and collect the JSON answer from the clipboard.

        ``system_prompt`` is unused - ChatGPT gets the rulebook inside the
        prompt itself (the manual prompt is built with browsing=True so the
        rulebook never claims ChatGPT lacks browsing).
        """
        del system_prompt, extra_fields
        self.copy_research_request(prompt, compact=compact)

        if self._auto_watch:
            result = self.wait_for_clipboard_json()
            if result is not None:
                return result
            print("Falling back to manual paste confirmation.")

        result = self.read_json_from_clipboard()
        if result is None:
            raise ResearchAborted("No usable research result was provided.")
        return result


# --------------------------------------------------------------------------- #
# Paste box (the dashboard's awaiting_manual flow)
# --------------------------------------------------------------------------- #
class ManualResponseGate(Protocol):
    """Where a paste-box backend reads the operator's pasted raw response.

    ``take_response()`` returns the raw pasted text, or None while the run
    should park in ``awaiting_manual``. Implementations keep the consume-once
    semantics (an answered response is returned exactly once) - see the
    store-backed :class:`review_hub.lifecycle.StoreManualGate`.
    """

    def take_response(self) -> str | None: ...


class PasteBoxBackend:
    """Manual ChatGPT through a paste box instead of the clipboard watch.

    The web control plane's replacement for the clipboard loop (same manual
    ChatGPT workflow, same zero-paid-resource posture): when research runs,
    the backend first checks the gate for an operator-pasted response. With
    none yet it raises :class:`ManualPauseRequested`, which the BatchRunner
    turns into an ``awaiting_manual`` park with this prompt surfaced; the
    operator pastes ChatGPT's raw response and the resumed run parses it here
    - through the same JSON-leak cleaning guard the clipboard flow uses, so
    prose, markdown fences, and trailing chatter around the JSON are handled
    identically. A paste that still yields no decision object parks the run
    again with the reason attached (an input error is not a research failure;
    no failure counter moves and the operator can simply re-paste).
    """

    def __init__(self, gate: ManualResponseGate) -> None:
        self._gate = gate

    def research(
        self,
        prompt: str,
        system_prompt: str,
        *,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del system_prompt, extra_fields  # ChatGPT gets the rulebook in the prompt itself
        raw = self._gate.take_response()
        if raw is None:
            raise ManualPauseRequested(prompt)
        try:
            value = extract_first_json_value(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ManualPauseRequested(
                prompt, detail=f"the pasted response was not usable: {exc}"
            ) from exc
        if not (isinstance(value, dict) and "decision" in value):
            raise ManualPauseRequested(
                prompt, detail="the pasted response contained no decision object"
            )
        return value
