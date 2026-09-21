"""OpenRouter client for the Aekovera review agent.

Replaces the manual "copy the prompt into ChatGPT, copy the JSON back"
step with a direct API call.

Design notes for the free tier:
- Free model IDs end in ``:free`` and are $0 per token, but they are
  rate limited by REQUEST count (about 20 requests/minute, and ~50
  requests/day on an unfunded account, ~1000/day once $10 of credits has
  ever been purchased). Failed requests still consume the daily quota.
- The free roster rotates and individual IDs get delisted without notice,
  so we never hardcode a single model: OPENROUTER_MODELS is tried in
  order, and 404/429/5xx on one model falls through to the next.
- Free endpoints may use prompts for model training. Review-page context
  is sent to them, so keep PRIVACY_SAFE_CONTEXT in mind (see config).
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

from jsonutil import extract_first_json_value, safe_text
from config import (
    OPENROUTER_API_KEY,
    OPENROUTER_URL,
    OPENROUTER_MODELS,
    OPENROUTER_AUTO_DISCOVER,
    OPENROUTER_MAX_CANDIDATES,
    MIN_CONTEXT_TOKENS,
    OPENROUTER_TIMEOUT,
    OPENROUTER_MAX_RETRIES,
    OPENROUTER_MIN_INTERVAL,
    OPENROUTER_TEMPERATURE,
    OPENROUTER_MAX_TOKENS,
    OPENROUTER_REFERER,
    OPENROUTER_TITLE,
    LLM_LOG_DIR,
)


class LLMError(RuntimeError):
    """Raised when the model could not produce a usable result."""


class QuotaExhausted(LLMError):
    """Raised when every configured model is rate limited or out of quota."""


_last_call_at = 0.0


def _throttle():
    """Keep us under the free-tier requests-per-minute ceiling."""
    global _last_call_at
    wait = OPENROUTER_MIN_INTERVAL - (time.time() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.time()


def _log(name, payload):
    try:
        directory = Path(LLM_LOG_DIR)
        directory.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"{stamp}-{name}.txt"
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, indent=2, ensure_ascii=False)
        path.write_text(str(payload), encoding="utf-8")
    except Exception:
        # Logging must never break a review run.
        pass


def check_api_key():
    key = safe_text(OPENROUTER_API_KEY)
    if not key:
        raise LLMError(
            "OPENROUTER_API_KEY is not set.\n"
            "  PowerShell:  $env:OPENROUTER_API_KEY = 'sk-or-v1-...'\n"
            "  bash:        export OPENROUTER_API_KEY='sk-or-v1-...'\n"
            "Create a key at https://openrouter.ai/keys (no credit card required)."
        )
    return key


def check_quota():
    """Report remaining OpenRouter quota for the key, best effort.

    Returns a dict or None. Never raises - this is informational only.
    """
    try:
        response = requests.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {check_api_key()}"},
            timeout=20,
        )
        if response.status_code == 200:
            return response.json().get("data")
    except Exception:
        pass
    return None


_discovered_models = None


def discover_free_models():
    """Ask OpenRouter which models are actually $0 right now.

    The :free roster rotates constantly - a hardcoded ID that worked last
    month commonly returns 404 "unavailable for free, use the paid slug".
    Discovering at runtime means the agent keeps working without edits.

    Returns a list of (model_id, supports_json) tuples, best first.
    Falls back to the static OPENROUTER_MODELS list if discovery fails.
    """
    global _discovered_models
    if _discovered_models is not None:
        return _discovered_models

    static = [(m, True) for m in OPENROUTER_MODELS]

    if not OPENROUTER_AUTO_DISCOVER:
        _discovered_models = static
        return _discovered_models

    try:
        response = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {check_api_key()}"},
            timeout=45,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        data = response.json().get("data") or []
    except Exception as exc:
        print(f"  ! Could not discover free models ({exc}); using the configured list.")
        _discovered_models = static
        return _discovered_models

    candidates = []
    for model in data:
        model_id = safe_text(model.get("id"))
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
        candidates.append({
            "id": model_id,
            "json": "response_format" in params,
            "context": context,
        })

    if not candidates:
        print("  ! Discovery returned no free models; using the configured list.")
        _discovered_models = static
        return _discovered_models

    def rank(model):
        # Prefer explicitly preferred IDs, then native JSON support, then context.
        preferred = OPENROUTER_MODELS.index(model["id"]) if model["id"] in OPENROUTER_MODELS else 99
        return (preferred, not model["json"], -model["context"])

    candidates.sort(key=rank)
    chosen = candidates[:OPENROUTER_MAX_CANDIDATES]

    _discovered_models = [(c["id"], c["json"]) for c in chosen]
    # openrouter/free is the auto-router: a good last resort when the
    # specific IDs above are all saturated.
    if not any(m == "openrouter/free" for m, _ in _discovered_models):
        _discovered_models.append(("openrouter/free", False))

    print(f"  ✓ Discovered {len(candidates)} free models; using: "
          + ", ".join(m for m, _ in _discovered_models))
    return _discovered_models


def _post(model, messages, supports_json=True):
    headers = {
        "Authorization": f"Bearer {check_api_key()}",
        "Content-Type": "application/json",
        # Optional OpenRouter attribution headers.
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_TITLE,
    }
    body = {
        "model": model,
        "messages": messages,
        "temperature": OPENROUTER_TEMPERATURE,
        "max_tokens": OPENROUTER_MAX_TOKENS,
    }
    if supports_json:
        # Not every free model accepts this; sending it to one that doesn't
        # causes a 400/404, so it is only set when the model advertises it.
        body["response_format"] = {"type": "json_object"}

    _throttle()
    return requests.post(
        OPENROUTER_URL, headers=headers, json=body, timeout=OPENROUTER_TIMEOUT
    )


def complete(messages):
    """Call OpenRouter, falling through the configured free models.

    Returns (text, model_id). Raises LLMError / QuotaExhausted.
    """
    rate_limited = []
    last_error = None

    for model, supports_json in discover_free_models():
        for attempt in range(1, OPENROUTER_MAX_RETRIES + 1):
            try:
                response = _post(model, messages, supports_json)
            except requests.RequestException as exc:
                last_error = f"{model}: network error: {exc}"
                print(f"  ! {last_error}")
                time.sleep(min(2 ** attempt, 20))
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

                _log("response", f"MODEL: {model}\n\n{text}")
                return text, model

            if status == 429:
                # Free-tier throttle. Back off, then move to the next model.
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(5 * attempt, 30)
                print(f"  ! {model}: rate limited (429); waiting {delay:.0f}s")
                time.sleep(delay)
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
                    # The free variant graduated to paid. Discovery normally
                    # prevents this; it means the cached list is stale.
                    print(f"  ! {model}: no longer free - skipping.")
                    _drop_model(model)
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
                time.sleep(min(2 ** attempt, 20))
                continue

            last_error = f"{model}: HTTP {status} - {safe_text(response.text)[:300]}"
            print(f"  ! {last_error}")
            break

        print(f"  ↷ Falling back from {model}.")

    if rate_limited and len(rate_limited) >= 1 and last_error and "rate limit" in last_error.lower():
        raise QuotaExhausted(
            "All configured free models are rate limited or out of quota.\n"
            f"Last error: {last_error}\n"
            "Free tier is ~20 requests/minute and ~50 requests/day until $10 of "
            "credits has been purchased. Wait for the reset, add credits, or add "
            "more model IDs to OPENROUTER_MODELS in config.py."
        )

    raise LLMError(f"No configured model returned a usable response. Last error: {last_error}")


def _drop_model(model_id):
    """Remove a model from the cached list after it proves unusable."""
    global _discovered_models
    if _discovered_models:
        _discovered_models = [m for m in _discovered_models if m[0] != model_id]


def _coerce_object(value):
    """Accept the shapes free models actually return.

    Some wrap the result in a single-element array, some return
    {"result": {...}} or {"output": {...}}. Unwrap those rather than
    failing the record.
    """
    if isinstance(value, list):
        objects = [v for v in value if isinstance(v, dict)]
        if len(objects) == 1:
            return objects[0]
        # Several objects: pick the one that looks like our schema.
        for candidate in objects:
            if "decision" in candidate:
                return candidate
        raise ValueError("expected a JSON object, got a list")

    if isinstance(value, dict):
        if "decision" not in value:
            for key in ("result", "output", "response", "data", "json"):
                inner = value.get(key)
                if isinstance(inner, dict) and "decision" in inner:
                    return inner
        return value

    raise ValueError(f"expected a JSON object, got {type(value).__name__}")


def research(prompt, system_prompt, max_parse_attempts=2):
    """Run the research prompt and return parsed JSON.

    On a parse failure the model is re-asked once with an explicit
    "JSON only" correction before giving up.
    """
    _log("request", prompt)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    for attempt in range(1, max_parse_attempts + 1):
        text, model = complete(messages)
        try:
            value = _coerce_object(extract_first_json_value(text))
            print(f"  ✓ Research returned by {model}.")
            return value
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"  ! Could not parse model output as JSON: {exc}")
            if attempt >= max_parse_attempts:
                _log("unparsable", text)
                raise LLMError(
                    f"Model output was not valid JSON after {max_parse_attempts} attempts. "
                    f"Raw output saved in {LLM_LOG_DIR}/."
                )
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
