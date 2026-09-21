"""Research backend protocol and shared errors.

A backend takes a fully built research prompt (plus system prompt) and
returns the model's answer as a parsed JSON object, raising LLMError when
no usable answer could be produced.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from review_hub.jsonutil import safe_text


class LLMError(RuntimeError):
    """Raised when the model could not produce a usable result."""


class QuotaExhausted(LLMError):
    """Raised when every model in the chain is rate limited or out of quota."""


class ManualPauseRequested(Exception):
    """A manual (paste-box) backend needs the operator's ChatGPT response.

    The runner parks the run in ``awaiting_manual`` with the prompt surfaced,
    and the operator's pasted raw response arrives through the backend's gate
    on resume. Deliberately NOT an ``LLMError``: an unanswered paste is not a
    research failure - the run is parked, not failed, and no failure counter
    moves. ``prompt`` is the full research prompt to surface in the paste box;
    ``detail`` carries why a previous paste was rejected, if it was.
    """

    def __init__(self, prompt: str, *, detail: str = "") -> None:
        super().__init__(detail or "waiting for the operator's pasted ChatGPT response")
        self.prompt = prompt
        self.detail = detail


@runtime_checkable
class ResearchBackend(Protocol):
    """Anything that can run one research prompt to a JSON object."""

    def research(
        self, prompt: str, system_prompt: str, *, extra_fields: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run the research prompt and return the parsed JSON object.

        ``extra_fields`` are merged into the result by the caller's
        agreement with the prompt schema (kept optional so backends stay
        schema-agnostic).
        """
        ...


def coerce_object(value: Any) -> dict[str, Any]:
    """Accept the shapes free models actually return.

    Some wrap the result in a single-element array, some return
    ``{"result": {...}}`` or ``{"output": {...}}``. Unwrap those rather
    than failing the record.
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


def validate_research_result(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize a backend answer into the schema the runner consumes.

    ``decision`` must be one of the four v4 outcomes; ``new_values`` must
    be a dict when present. Extra keys pass through untouched.
    """
    decision = safe_text(value.get("decision")).upper()
    if decision not in {"ACCEPT", "PARK", "RE_ENRICH", "REJECT", "MANUAL_REVIEW"}:
        raise LLMError(f"decision {decision!r} is not one of the four v4 outcomes")
    new_values = value.get("new_values")
    if new_values is not None and not isinstance(new_values, dict):
        raise LLMError("new_values must be an object when present")
    return value
