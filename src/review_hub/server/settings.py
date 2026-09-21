"""Safe configuration editing for the server.

The dashboard can read and edit the ENGINE's operational knobs (safety
defaults, hold toggles, discovery retries, evidence caps, backend switch)
through a strict allowlist: typed values, no filesystem paths, no URLs, no
credentials. ``OPENROUTER_API_KEY`` lives only in the environment and is
never echoed in a response, never accepted in an update, never persisted.
Deployment identity (BASE_URL, REVIEW_URL, PROFILE_DIR) is excluded too -
an API that could retarget the review host could steer verdicts to an
arbitrary server, which is outside a settings editor's charter.

Edits apply to the in-process ``review_hub.config`` module immediately and
are persisted as JSON next to the SQLite store, so a server restart keeps
them. The engine reads these values at run construction and per loop
iteration exactly as it always has; the server only writes them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from review_hub import config


class SettingsError(ValueError):
    """A settings update was rejected (unknown key, type, or value)."""


# Never settable through the API and never included in a response.
SECRET_SETTING_NAMES = frozenset({"OPENROUTER_API_KEY"})

_BOOL_KEYS = (
    "REUSE_RESULT_ON_REPEAT",
    "CONTINUE_ON_RESEARCH_FAILURE",
    "ACCEPT_NON_US",
    "ENABLE_MANUAL_REVIEW",
    "ENABLE_WEB_EVIDENCE",
    "ENABLE_ACCEPTED_SNAPSHOT",
    "VERIFY_WEBSITE_BEFORE_APPLY",
    "HOLD_ACCEPT_ON_UNRESOLVED_FIELDS",
    "HOLD_ON_IDENTITY_RENAME",
    "HOLD_ACCEPT_IF_EDITS_NOT_LANDED",
    "HOLD_ACCEPT_IF_SNAPSHOT_FAILED",
    "RELOAD_BEFORE_FINAL_SNAPSHOT",
    "CHATGPT_PROJECT_MODE",
    "OPENROUTER_AUTO_DISCOVER",
)
_INT_KEYS = (
    "MAX_RECORDS",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_REPEAT_PASSES",
    "OPENROUTER_MAX_CANDIDATES",
    "OPENROUTER_MAX_RETRIES",
    "POST_SETTLE_TIMEOUT_MS",
    "FIELD_PRESENT_WAIT_MS",
    "ADVANCE_VERIFY_TIMEOUT_MS",
    "FINAL_ACTION_MAX_ATTEMPTS",
    "FIELD_DISCOVERY_MAX_ATTEMPTS",
    "FIELD_DISCOVERY_RETRY_DELAY_MS",
    "PAGE_CONTEXT_CHARS",
    "EVIDENCE_PAGE_CHARS",
    "EVIDENCE_TIMEOUT_MS",
)
_FLOAT_KEYS = ("OPENROUTER_TEMPERATURE", "OPENROUTER_MIN_INTERVAL", "OPENROUTER_TIMEOUT")
_LIST_KEYS = ("OPENROUTER_MODELS",)
_STR_KEYS = ("RESEARCH_BACKEND",)

# Order is the dashboard's display order.
EDITABLE_SETTINGS: tuple[str, ...] = _STR_KEYS + _BOOL_KEYS + _INT_KEYS + _FLOAT_KEYS + _LIST_KEYS

# The value vocabulary the backend switch accepts (must match the engine).
RESEARCH_BACKENDS = ("manual", "api")

# Every int knob here is a count, attempt cap, or millisecond budget: negative
# values are meaningless, so they are rejected wholesale.
_NON_NEGATIVE_KEYS = frozenset(_INT_KEYS)
_MINIMUMS: dict[str, int] = {"MAX_RECORDS": 1, "OPENROUTER_MAX_CANDIDATES": 1}


def effective_settings() -> dict[str, Any]:
    """Current editable values as plain JSON-compatible types."""
    return {key: getattr(config, key) for key in EDITABLE_SETTINGS}


def redacted_settings() -> dict[str, Any]:
    """Effective settings with secrets replaced by presence only."""
    out = effective_settings()
    for name in SECRET_SETTING_NAMES:
        out[name] = {"configured": bool(getattr(config, name, ""))}
    return out


def validate_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Validate an update batch; returns the validated {key: value} mapping."""
    validated: dict[str, Any] = {}
    for key, value in updates.items():
        if key in SECRET_SETTING_NAMES:
            raise SettingsError(
                f"{key} is a credential: set it in the environment, never via the API"
            )
        if key not in EDITABLE_SETTINGS:
            raise SettingsError(f"unknown setting {key!r}")
        validated[key] = _validate_value(key, value)
    return validated


def apply_updates(values: dict[str, Any]) -> None:
    for key, value in values.items():
        setattr(config, key, value)


def overrides_path(db_path: Any) -> Path:
    return Path(db_path).parent / "settings_overrides.json"


def save_overrides(path: Path, values: dict[str, Any]) -> None:
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}  # unreadable overrides are replaced, not trusted
    existing.update(values)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")


def load_and_apply_overrides(path: Path) -> int:
    """Apply persisted overrides at boot; returns how many were applied."""
    if not path.exists():
        return 0
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"stored settings overrides are unreadable: {exc}") from exc
    if not isinstance(stored, dict):
        raise SettingsError("stored settings overrides are not a JSON object")
    applied = 0
    for key, value in stored.items():
        if key in EDITABLE_SETTINGS:
            try:
                apply_updates({key: _validate_value(key, value)})
                applied += 1
            except SettingsError:
                continue  # a stale override for a changed key must not block boot
    return applied


def _validate_value(key: str, value: Any) -> Any:
    if key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise SettingsError(f"{key} expects true or false")
        return value
    if key in _INT_KEYS:
        # bool is a subclass of int in Python - reject it before the int check.
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{key} expects an integer")
        if key in _NON_NEGATIVE_KEYS and value < _MINIMUMS.get(key, 0):
            raise SettingsError(f"{key} expects an integer >= {_MINIMUMS.get(key, 0)}")
        return value
    if key in _FLOAT_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{key} expects a number")
        return float(value)
    if key in _LIST_KEYS:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{key} expects a list of strings")
        return value
    if key == "RESEARCH_BACKEND":
        if value not in RESEARCH_BACKENDS:
            accepted = " or ".join(RESEARCH_BACKENDS)
            raise SettingsError(f"RESEARCH_BACKEND expects {accepted}")
        return value
    raise AssertionError(f"unhandled editable key {key!r}")  # lists above are exhaustive
