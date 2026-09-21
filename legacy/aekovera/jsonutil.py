"""Tolerant JSON extraction shared by the clipboard and API paths."""

import json
import re


def safe_text(value):
    if value is None:
        return ""
    return str(value).strip()


def extract_first_json_value(raw):
    """Extract the first complete JSON object/array from arbitrary model text.

    Models (and ChatGPT copy/paste) may return valid JSON followed by an extra
    sentence, a second block, or markdown fences. json.loads() rejects that
    with "Extra data" even though the first JSON object itself is valid, so we
    use JSONDecoder.raw_decode() and ignore harmless trailing content.
    """
    text = safe_text(raw).lstrip("\ufeff")
    if not text:
        raise ValueError("no content to parse")

    # Strip reasoning-model <think> blocks before looking for JSON.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)

    # Remove common markdown fences without requiring the whole payload to
    # consist exclusively of one fenced block.
    text = re.sub(r"```(?:json)?", "", text, flags=re.I)
    text = text.replace("```", "")

    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON object or array found in response")
    start = min(starts)

    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(text[start:])

    trailing = text[start + end:].strip()
    if trailing:
        print("⚠ Extra text after JSON detected; ignoring trailing content.")

    return value
