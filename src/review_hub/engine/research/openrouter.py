"""OpenRouter research backend - free-models-only by hard directive.

Port of legacy ``llm.py`` with the module globals folded into an instance
so tests can inject transport, clock, and sleep.

Design notes for the free tier:
- Free model IDs end in ``:free`` and are $0 per token, but they are rate
  limited by REQUEST count (about 20 requests/minute, and ~50 requests/day
  on an unfunded account, ~1000/day once $10 of credits has ever been
  purchased). Failed requests still consume the daily quota.
- The free roster rotates and individual IDs get delisted without notice,
  so we never hardcode a single model: OPENROUTER_MODELS is tried in
  order, and 404/429/5xx on one model falls through to the next.
- ZERO PAID RESOURCES: discovery filters to $0 endpoints only. A model
  without ``:free`` AND a zero prompt+completion price can never enter
  the chain, and there is no paid fallback anywhere.
- Free endpoints may use prompts for model training. Review-page context
  is sent to them, so keep PRIVACY_SAFE_CONTEXT in mind (see config).

No API key is required to *import* or to run the manual backend; a key is
only checked when this backend actually fires a request.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from review_hub.config import (
    LLM_LOG_DIR,
    MIN_CONTEXT_TOKENS,
    OPENROUTER_AUTO_DISCOVER,
    OPENROUTER_MAX_CANDIDATES,
    OPENROUTER_MAX_RETRIES,
    OPENROUTER_MAX_TOKENS,
    OPENROUTER_MIN_INTERVAL,
    OPENROUTER_MODELS,
    OPENROUTER_REFERER,
    OPENROUTER_TEMPERATURE,
    OPENROUTER_TIMEOUT,
    OPENROUTER_TITLE,
    OPENROUTER_URL,
)
from review_hub.engine.research import LLMError, QuotaExhausted, coerce_object
from review_hub.jsonutil import extract_first_json_value, safe_text

AUTO_ROUTER_MODEL = "openrouter/free"


class _Response:
    """Minimal stand-in for :class:`requests.Response` (tests)."""

    def __init__(self, status_code: int, body: str = "", headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}

    def json(self) -> Any:
        return json.loads(self.text)


class OpenRouterTransport:
    """The real HTTP transport (requests). Tests replace this."""

    def get(self, url: str, **kwargs: Any) -> Any:
        return requests.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return requests.post(url, **kwargs)


class OpenRouterClient:
    """Automated research over OpenRouter using only free endpoints."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: OpenRouterTransport | None = None,
        sleeper: Any = time.sleep,
        clock: Any = time.time,
        log_dir: str | Path | None = None,
        static_models: list[str] | None = None,
    ) -> None:
        self._api_key_override = api_key
        self._transport = transport or OpenRouterTransport()
        self._sleep = sleeper
        self._clock = clock
        self._log_dir = Path(log_dir) if log_dir else Path(LLM_LOG_DIR)
        self._static_models = list(
            static_models if static_models is not None else OPENROUTER_MODELS
        )
        self._discovered_models: list[tuple[str, bool]] | None = None
        self._last_call_at = 0.0

    # ------------------------------------------------------------------ #
    # Key handling - a key is only needed when this backend is used.
    # ------------------------------------------------------------------ #
    def check_api_key(self) -> str:
        key = self._api_key_override or safe_text(os.environ.get("OPENROUTER_API_KEY", ""))
        if not key:
            raise LLMError(
                "OPENROUTER_API_KEY is not set.\n"
                "  PowerShell:  $env:OPENROUTER_API_KEY = 'sk-or-v1-...'\n"
                "  bash:        export OPENROUTER_API_KEY='sk-or-v1-...'\n"
                "Create a key at https://openrouter.ai/keys (no credit card required)."
            )
        return key

    # ------------------------------------------------------------------ #
    # Throttle and logging
    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        """Keep us under the free-tier requests-per-minute ceiling."""
        wait = OPENROUTER_MIN_INTERVAL - (self._clock() - self._last_call_at)
        if wait > 0:
            self._sleep(wait)
        self._last_call_at = self._clock()

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
    # Discovery - the zero-paid-resources gate
    # ------------------------------------------------------------------ #
    def discover_free_models(self) -> list[tuple[str, bool]]:
        """Ask OpenRouter which models are actually $0 right now.

        The ``:free`` roster rotates constantly - a hardcoded ID that
        worked last month commonly returns 404 "unavailable for free, use
        the paid slug". Discovering at runtime means the agent keeps
        working without edits.

        Returns a list of ``(model_id, supports_json)`` tuples, best
        first. Falls back to the static OPENROUTER_MODELS list if
        discovery fails. Every returned model is a free endpoint.
        """
        if self._discovered_models is not None:
            return self._discovered_models

        static = [(m, True) for m in self._static_models]

        if not OPENROUTER_AUTO_DISCOVER:
            self._discovered_models = static
            return self._discovered_models

        try:
            response = self._transport.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {self.check_api_key()}"},
                timeout=45,
            )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            data = response.json().get("data") or []
        except Exception as exc:  # noqa: BLE001 - fall back to the static list
            print(f"  ! Could not discover free models ({exc}); using the configured list.")
            self._discovered_models = static
            return self._discovered_models

        candidates = []
        for model in data:
            model_id = safe_text(model.get("id"))
            # Free endpoints only: the :free suffix AND a literal $0 price
            # on both prompt and completion tokens. Anything else - the
            # paid slug, a paid price, an unparsable price - is skipped.
            if not model_id.endswith(":free"):
                continue
            pricing = model.get("pricing") or {}
            try:
                if float(pricing.get("prompt") or 0) != 0:
                    continue
                if float(pricing.get("completion") or 0) != 0:
                    continue
            except (TypeError, ValueError):
                continue

            params = model.get("supported_parameters") or []
            context = model.get("context_length") or 0
            # Need room for the record plus several pages of fetched evidence.
            if context < MIN_CONTEXT_TOKENS:
                continue
            candidates.append(
                {
                    "id": model_id,
                    "json": "response_format" in params,
                    "context": context,
                }
            )

        if not candidates:
            print("  ! Discovery returned no free models; using the configured list.")
            self._discovered_models = static
            return self._discovered_models

        def rank(model: dict[str, Any]) -> tuple[int, bool, int]:
            # Prefer explicitly preferred IDs, then native JSON support,
            # then context.
            preferred = (
                self._static_models.index(model["id"]) if model["id"] in self._static_models else 99
            )
            return (preferred, not model["json"], -model["context"])

        candidates.sort(key=rank)
        chosen = candidates[:OPENROUTER_MAX_CANDIDATES]

        self._discovered_models = [(c["id"], c["json"]) for c in chosen]
        # openrouter/free is the auto-router: a good last resort when the
        # specific IDs above are all saturated.
        if not any(m == AUTO_ROUTER_MODEL for m, _ in self._discovered_models):
            self._discovered_models.append((AUTO_ROUTER_MODEL, False))

        print(
            f"  ✓ Discovered {len(candidates)} free models; using: "
            + ", ".join(m for m, _ in self._discovered_models)
        )
        return self._discovered_models

    def _drop_model(self, model_id: str) -> None:
        """Remove a model from the cached list after it proves unusable."""
        if self._discovered_models:
            self._discovered_models = [m for m in self._discovered_models if m[0] != model_id]

    # ------------------------------------------------------------------ #
    # Completion chain
    # ------------------------------------------------------------------ #
    def _post(self, model: str, messages: list[dict[str, str]], supports_json: bool) -> Any:
        headers = {
            "Authorization": f"Bearer {self.check_api_key()}",
            "Content-Type": "application/json",
            # Optional OpenRouter attribution headers.
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_TITLE,
        }
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": OPENROUTER_TEMPERATURE,
            "max_tokens": OPENROUTER_MAX_TOKENS,
        }
        if supports_json:
            # Not every free model accepts this; sending it to one that
            # doesn't causes a 400/404, so it is only set when the model
            # advertises it.
            body["response_format"] = {"type": "json_object"}

        self._throttle()
        return self._transport.post(
            OPENROUTER_URL, headers=headers, json=body, timeout=OPENROUTER_TIMEOUT
        )

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        """Call OpenRouter, falling through the discovered free models.

        Returns ``(text, model_id)``. Raises LLMError / QuotaExhausted.
        """
        rate_limited: list[str] = []
        last_error: str | None = None

        for model, supports_json in self.discover_free_models():
            for attempt in range(1, OPENROUTER_MAX_RETRIES + 1):
                try:
                    response = self._post(model, messages, supports_json)
                except requests.RequestException as exc:
                    last_error = f"{model}: network error: {exc}"
                    print(f"  ! {last_error}")
                    self._sleep(min(2**attempt, 20))
                    continue

                status = response.status_code

                if status == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        last_error = f"{model}: non-JSON HTTP body"
                        print(f"  ! {last_error}")
                        break

                    # OpenRouter can return a 200 that carries an error object.
                    if isinstance(data, dict) and data.get("error"):
                        last_error = f"{model}: {data['error']}"
                        print(f"  ! {last_error}")
                        break

                    choices = (data or {}).get("choices") or []
                    if not choices:
                        last_error = f"{model}: empty choices"
                        print(f"  ! {last_error}")
                        break

                    message = choices[0].get("message") or {}
                    text = safe_text(message.get("content"))
                    if not text:
                        # Some reasoning models put everything in `reasoning`.
                        text = safe_text(message.get("reasoning"))
                    if not text:
                        last_error = f"{model}: empty completion"
                        print(f"  ! {last_error}")
                        break

                    self._log("response", f"MODEL: {model}\n\n{text}")
                    return text, model

                if status == 429:
                    # Free-tier throttle. Back off, then move to the next model.
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else min(5 * attempt, 30)
                    print(f"  ! {model}: rate limited (429); waiting {delay:.0f}s")
                    self._sleep(delay)
                    last_error = f"{model}: rate limited"
                    if attempt == OPENROUTER_MAX_RETRIES:
                        rate_limited.append(model)
                    continue

                if status == 402:
                    # Out of credits, or the model is no longer actually free.
                    last_error = f"{model}: payment required (402) - not free on this account"
                    print(f"  ! {last_error}")
                    rate_limited.append(model)
                    break

                if status in (404, 400):
                    detail = safe_text(response.text)[:300]
                    if "unavailable for free" in detail.lower():
                        # The free variant graduated to paid. Discovery
                        # normally prevents this; it means the cached list
                        # is stale.
                        print(f"  ! {model}: no longer free - skipping.")
                        self._drop_model(model)
                    elif supports_json and "response_format" in detail:
                        # Retry the same model without the JSON parameter.
                        print(f"  ! {model}: rejected response_format; retrying without it.")
                        supports_json = False
                        last_error = f"{model}: HTTP {status}"
                        continue
                    else:
                        print(f"  ! {model}: HTTP {status} - {detail}")
                    last_error = f"{model}: HTTP {status} - {detail}"
                    break

                if status >= 500:
                    print(f"  ! {model}: HTTP {status}; retrying")
                    last_error = f"{model}: HTTP {status}"
                    self._sleep(min(2**attempt, 20))
                    continue

                last_error = f"{model}: HTTP {status} - {safe_text(response.text)[:300]}"
                print(f"  ! {last_error}")
                break

            print(f"  ↷ Falling back from {model}.")

        if rate_limited and last_error and "rate limit" in last_error.lower():
            raise QuotaExhausted(
                "All configured free models are rate limited or out of quota.\n"
                f"Last error: {last_error}\n"
                "Free tier is ~20 requests/minute and ~50 requests/day until $10 of "
                "credits has been purchased. Wait for the reset, add credits, or add "
                "more model IDs to OPENROUTER_MODELS in config.py."
            )

        raise LLMError(f"No configured model returned a usable response. Last error: {last_error}")

    # ------------------------------------------------------------------ #
    # Research entry point
    # ------------------------------------------------------------------ #
    def research(
        self,
        prompt: str,
        system_prompt: str,
        *,
        extra_fields: dict[str, Any] | None = None,
        max_parse_attempts: int = 2,
    ) -> dict[str, Any]:
        """Run the research prompt and return parsed JSON.

        On a parse failure the model is re-asked once with an explicit
        "JSON only" correction before giving up.
        """
        del extra_fields  # schema-agnostic passthrough; the runner merges fields
        self._log("request", prompt)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        for attempt in range(1, max_parse_attempts + 1):
            text, model = self.complete(messages)
            try:
                value = coerce_object(extract_first_json_value(text))
                print(f"  ✓ Research returned by {model}.")
                return value
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"  ! Could not parse model output as JSON: {exc}")
                if attempt >= max_parse_attempts:
                    self._log("unparsable", text)
                    raise LLMError(
                        f"Model output was not valid JSON after {max_parse_attempts} attempts. "
                        f"Raw output saved in {self._log_dir}/."
                    ) from exc
                messages = messages + [
                    {"role": "assistant", "content": text[:4000]},
                    {
                        "role": "user",
                        "content": (
                            "That was not valid JSON. Reply again with ONLY the JSON object "
                            "matching the schema. No prose, no markdown fences, no commentary."
                        ),
                    },
                ]

        raise LLMError("unreachable")
