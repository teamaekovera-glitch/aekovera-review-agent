"""Obvious agent research backend - autonomous web research, no human.

Where the manual backend hands the prompt to a human's ChatGPT tab and the
OpenRouter backend sends it to a bare LLM, this backend dispatches the prompt
to an Obvious agent session (the External Developer API): a full agent with
web search and page reads - the research quality of the manual ChatGPT
workflow with the human removed.

The External API has no "read the answer" endpoint. An agent session is
fire-and-forget; the documented way data comes back is the agent itself
making an HTTP call (successPrompt / failurePrompt). So the answer flows
through a tiny token-checked relay (relay/obvious_relay.py, self-hostable):

    this process ── dispatch ──▶ Obvious External API (thread + agent session)
                                          │  the agent researches the web
    this process ◀── poll ────── relay ◀──┘  the agent POSTs the final JSON

The pipeline only ever makes OUTBOUND requests - the local-first posture
(no inbound exposure, no tunnel) survives. A fresh session id per research
call makes stale or replayed answers harmless, and an unanswered session
raises LLMError after OBVIOUS_WAIT_TIMEOUT so the runner's research-failure
path stays loud (skip the record / stop the run per configuration), never
silent.

Credentials are environment-only, mirroring OPENROUTER_API_KEY:
  OBVIOUS_API_KEY      a Bearer key from Settings → External Access
                       (workspace admin; inherits that user's permissions)
  OBVIOUS_PROJECT_ID   the Obvious project the sessions run in
  OBVIOUS_RELAY_URL    base URL of the answer relay, e.g. https://host/answers
  OBVIOUS_RELAY_TOKEN  the relay's shared write/read token
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from review_hub.config import (
    LLM_LOG_DIR,
    OBVIOUS_API_BASE,
    OBVIOUS_DISPATCH_TIMEOUT,
    OBVIOUS_MAX_RETRIES,
    OBVIOUS_POLL_INTERVAL,
    OBVIOUS_RELAY_TOKEN,
    OBVIOUS_WAIT_TIMEOUT,
)
from review_hub.engine.research import LLMError, coerce_object
from review_hub.jsonutil import extract_first_json_value, safe_text

# Consecutive failed relay polls tolerated before giving up (the relay or the
# network may blip while the agent is still working).
MAX_POLL_ERRORS = 4


class ObviousTransport:
    """The real HTTP transport (requests). Tests replace this."""

    def get(self, url: str, **kwargs: Any) -> Any:
        return requests.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return requests.post(url, **kwargs)


class ObviousAgentClient:
    """Dispatch the research prompt to an Obvious agent session, await the JSON."""

    # The agent browses the live web itself, so the runner hands it the
    # browsing-capable operating rules instead of the evidence-only ones.
    browsing = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        project_id: str | None = None,
        relay_base: str | None = None,
        relay_token: str | None = None,
        api_base: str | None = None,
        transport: ObviousTransport | None = None,
        sleeper: Any = time.sleep,
        clock: Any = time.time,
        log_dir: str | Path | None = None,
        poll_interval: float | None = None,
        wait_timeout: float | None = None,
        dispatch_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._api_key_override = api_key
        self._project_override = project_id
        self._relay_base_override = relay_base
        self._relay_token_override = relay_token
        self._api_base = (api_base or OBVIOUS_API_BASE).rstrip("/")
        self._transport = transport or ObviousTransport()
        self._sleep = sleeper
        self._clock = clock
        self._log_dir = Path(log_dir) if log_dir else Path(LLM_LOG_DIR)
        self._poll_interval = OBVIOUS_POLL_INTERVAL if poll_interval is None else poll_interval
        self._wait_timeout = OBVIOUS_WAIT_TIMEOUT if wait_timeout is None else wait_timeout
        self._dispatch_timeout = (
            OBVIOUS_DISPATCH_TIMEOUT if dispatch_timeout is None else dispatch_timeout
        )
        self._max_retries = OBVIOUS_MAX_RETRIES if max_retries is None else max_retries

    # ------------------------------------------------------------------ #
    # Credential and config checks (a key is only needed when used)
    # ------------------------------------------------------------------ #
    def check_api_key(self) -> str:
        key = self._api_key_override or safe_text(os.environ.get("OBVIOUS_API_KEY", ""))
        if not key:
            raise LLMError(
                "OBVIOUS_API_KEY is not set.\n"
                "  bash:        export OBVIOUS_API_KEY='obv_...'\n"
                "  PowerShell:  $env:OBVIOUS_API_KEY = 'obv_...'\n"
                "Create a key in Obvious: Settings → External Access "
                "(workspace admin; the key inherits that user's permissions)."
            )
        return key

    def project_id(self) -> str:
        pid = self._project_override or safe_text(os.environ.get("OBVIOUS_PROJECT_ID", ""))
        if not pid:
            raise LLMError(
                "OBVIOUS_PROJECT_ID is not set.\n"
                "Set it to the prj_... id of the Obvious project the research "
                "sessions should run in (any project your API key's workspace owns)."
            )
        return pid

    def relay_base(self) -> str:
        base = self._relay_base_override or safe_text(os.environ.get("OBVIOUS_RELAY_URL", ""))
        if not base:
            raise LLMError(
                "OBVIOUS_RELAY_URL is not set.\n"
                "Point it at the answer relay (relay/obvious_relay.py), e.g. "
                "https://your-host:8801 - the Obvious agent must be able to "
                "reach it, and this pipeline only polls it outbound."
            )
        return base.rstrip("/")

    def relay_token(self) -> str:
        return self._relay_token_override or safe_text(
            os.environ.get("OBVIOUS_RELAY_TOKEN", "") or OBVIOUS_RELAY_TOKEN
        )

    def preflight(self) -> list[str]:
        """Every configuration problem, as human-readable lines (CLI preflight)."""
        problems: list[str] = []
        for label, check in (
            ("OBVIOUS_API_KEY", self.check_api_key),
            ("OBVIOUS_PROJECT_ID", self.project_id),
            ("OBVIOUS_RELAY_URL", self.relay_base),
        ):
            try:
                check()
            except LLMError as exc:
                problems.append(f"✗ {exc}")
        if not self.relay_token():
            problems.append(
                "⚠ OBVIOUS_RELAY_TOKEN is not set - the relay will reject "
                "deliveries (see relay/obvious_relay.py)."
            )
        return problems

    # ------------------------------------------------------------------ #
    # Logging (same audit pattern as the OpenRouter backend)
    # ------------------------------------------------------------------ #
    def _log(self, name: str, payload: Any) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self._log_dir / f"{stamp}-{name}.txt"
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload, indent=2, ensure_ascii=False)
            path.write_text(str(payload), encoding="utf-8")
        except Exception:  # noqa: BLE001 - logging must never break a review run
            pass

    # ------------------------------------------------------------------ #
    # Dispatch: create the agent session
    # ------------------------------------------------------------------ #
    def callback_url(self, session_id: str) -> str:
        """Where the agent POSTs its answer (token travels in the URL)."""
        url = f"{self.relay_base()}/answers/{session_id}"
        token = self.relay_token()
        return f"{url}?token={quote(token)}" if token else url

    def _starter_prompt(self, prompt: str, system_prompt: str, callback_url: str) -> str:
        parts = [
            "You are the research step of an automated supplier-verification pipeline.",
            "Complete the task below YOURSELF using web search and page reads.",
            "No human is available: do not ask questions - decide from the "
            "evidence you gather, exactly as the task instructs.",
            "",
        ]
        if system_prompt.strip():
            parts += ["=== OPERATING RULES ===", system_prompt.strip(), ""]
        parts += [
            "=== RESEARCH TASK ===",
            prompt,
            "",
            "=== ANSWER DELIVERY (mandatory) ===",
            "When the task is complete, make ONE HTTP POST to:",
            f"    {callback_url}",
            "The request body must be the final JSON answer object ONLY - "
            "no prose, no markdown fences, no commentary around it.",
            'If you cannot complete the task, POST {"error": "<reason>"} to '
            "the same URL instead, then stop.",
        ]
        return "\n".join(parts)

    def _delivery_prompts(self, callback_url: str) -> tuple[str, str]:
        success = (
            "The research task is finished. Deliver the result now: POST the "
            "COMPLETE final JSON answer object as the raw request body to:\n"
            f"    {callback_url}\n"
            'If you could not complete it, POST {"error": "<reason>"} to that '
            "URL instead. End your turn only after the POST succeeds."
        )
        failure = (
            "The research task hit a blocker. POST {\"error\": \"<what blocked it>\"} "
            "as the raw request body to:\n"
            f"    {callback_url}\n"
            "Then stop."
        )
        return success, failure

    def dispatch(self, prompt: str, system_prompt: str) -> dict[str, Any]:
        """Create the agent session. Returns the API's thread/execution ids."""
        session_id = uuid.uuid4().hex
        callback_url = self.callback_url(session_id)
        success_prompt, failure_prompt = self._delivery_prompts(callback_url)
        body = {
            "starterPrompt": self._starter_prompt(prompt, system_prompt, callback_url),
            "name": f"Aekovera review research {session_id[:8]}",
            "successPrompt": success_prompt,
            "failurePrompt": failure_prompt,
        }
        self._log("obvious-request", body["starterPrompt"])

        url = f"{self._api_base}/projects/{self.project_id()}/thread"
        headers = {
            "Authorization": f"Bearer {self.check_api_key()}",
            "Content-Type": "application/json",
        }
        last_error: str | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._transport.post(
                    url, headers=headers, json=body, timeout=self._dispatch_timeout
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                print(f"  ! Obvious dispatch: {last_error}")
                self._sleep(min(2**attempt, 20))
                continue

            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError:
                    last_error = "non-JSON response body"
                    print(f"  ! Obvious dispatch: {last_error}")
                    break
                thread = (data or {}).get("thread") or {}
                execution = (data or {}).get("execution") or {}
                if not thread.get("id"):
                    last_error = f"response carried no thread id: {safe_text(response.text)[:300]}"
                    print(f"  ! Obvious dispatch: {last_error}")
                    break
                print(f"  → Obvious session dispatched (thread {thread.get('id')}).")
                return {
                    "session_id": session_id,
                    "thread_id": safe_text(thread.get("id")),
                    "execution_id": safe_text(execution.get("id")),
                }

            if status == 409:
                # Documented race: the thread already has an active execution.
                # Retry after a short delay per the API's guidance.
                last_error = "HTTP 409 (thread race)"
                print(f"  ! Obvious dispatch: {last_error}; retrying")
                self._sleep(3 * attempt)
                continue

            if status == 401:
                raise LLMError(
                    "Obvious dispatch rejected the API key (HTTP 401). Check "
                    "OBVIOUS_API_KEY - keys are created in Settings → External "
                    "Access and are workspace-scoped."
                )
            if status == 404:
                raise LLMError(
                    "Obvious dispatch found no such project (HTTP 404). Check "
                    "OBVIOUS_PROJECT_ID - the project must belong to the same "
                    "workspace as the API key."
                )
            if status >= 500:
                last_error = f"HTTP {status}"
                print(f"  ! Obvious dispatch: {last_error}; retrying")
                self._sleep(min(2**attempt, 20))
                continue

            raise LLMError(
                f"Obvious dispatch failed: HTTP {status} - {safe_text(response.text)[:300]}"
            )

        raise LLMError(f"Obvious dispatch did not succeed. Last error: {last_error}")

    # ------------------------------------------------------------------ #
    # Collect: poll the relay until the agent's answer lands
    # ------------------------------------------------------------------ #
    def collect(self, session_id: str) -> str:
        """Poll the relay until the agent's answer arrives (or we time out)."""
        url = f"{self.relay_base()}/answers/{session_id}"
        headers = {"X-Relay-Token": self.relay_token()} if self.relay_token() else {}
        deadline = self._clock() + self._wait_timeout
        consecutive_errors = 0
        while self._clock() < deadline:
            try:
                response = self._transport.get(url, headers=headers, timeout=self._dispatch_timeout)
            except requests.RequestException as exc:
                consecutive_errors += 1
                if consecutive_errors > MAX_POLL_ERRORS:
                    raise LLMError(
                        f"Obvious relay unreachable while waiting for session "
                        f"{session_id}: {exc}"
                    ) from exc
                self._sleep(self._poll_interval)
                continue

            consecutive_errors = 0
            if response.status_code == 200:
                try:
                    state = response.json().get("state")
                except ValueError:
                    state = None
                if state == "answered":
                    answer = response.json().get("answer")
                    if safe_text(answer):
                        return str(answer)
            self._sleep(self._poll_interval)

        raise LLMError(
            f"Obvious session {session_id} did not deliver an answer within "
            f"{self._wait_timeout:.0f}s. The session may still be running - "
            "check the Obvious project before re-running the record."
        )

    # ------------------------------------------------------------------ #
    # ResearchBackend protocol
    # ------------------------------------------------------------------ #
    def research(
        self,
        prompt: str,
        system_prompt: str,
        *,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch the prompt to an Obvious agent and return the parsed JSON."""
        del extra_fields  # schema-agnostic passthrough; the runner merges fields
        dispatched = self.dispatch(prompt, system_prompt)
        session_id = dispatched["session_id"]

        raw = self.collect(session_id)
        self._log(
            "obvious-response",
            f"SESSION: {session_id}\nTHREAD: {dispatched['thread_id']}\n\n{raw}",
        )
        try:
            value = coerce_object(extract_first_json_value(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMError(
                f"Obvious session {session_id} delivered an answer that was not "
                f"usable JSON: {exc}. Raw output saved in {self._log_dir}/."
            ) from exc

        if "decision" not in value and safe_text(value.get("error")):
            raise LLMError(
                f"Obvious session {session_id} reported a failure: "
                f"{safe_text(value.get('error'))}"
            )
        print(f"  ✓ Research returned by the Obvious agent (thread {dispatched['thread_id']}).")
        return value
