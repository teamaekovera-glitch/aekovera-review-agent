"""OpenRouter discovery invariants - the zero-paid-resources gate.

The owner directive is absolute: model discovery must filter to free
endpoints only, must never surface or fall back to a paid model, and no
API key may be required by default (the manual ChatGPT backend is the
default; OpenRouter is an explicit per-run switch). A paid model anywhere
in the discovered chain is a test failure.
"""

from review_hub.engine.research import LLMError
from review_hub.engine.research.openrouter import (
    AUTO_ROUTER_MODEL,
    OPENROUTER_MODELS,
    OpenRouterClient,
)

# ---------------------------------------------------------------------------
# Fakes - the transport seam records everything that would leave the process
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._body


class _FakeTransport:
    def __init__(self, catalog=None, discovery_status=200, post_responses=None,
                 discovery_error=None):
        self.catalog = catalog or []
        self.discovery_status = discovery_status
        self.discovery_error = discovery_error
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.posted_models = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.discovery_error is not None:
            raise self.discovery_error
        return _FakeResponse(self.discovery_status, {"data": self.catalog})

    def post(self, url, **kwargs):
        self.posted_models.append(kwargs["json"]["model"])
        if self.post_responses:
            return self.post_responses.pop(0)
        return _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})


def _entry(model_id, prompt="0", completion="0", context=200_000,
           params=("response_format",)):
    return {
        "id": model_id,
        "pricing": {"prompt": prompt, "completion": completion},
        "context_length": context,
        "supported_parameters": list(params),
    }


def _client(transport, tmp_path, **kw):
    return OpenRouterClient(
        transport=transport,
        sleeper=lambda seconds: None,  # never actually wait in tests
        clock=lambda: 0.0,
        log_dir=tmp_path,
        **kw,
    )


def _is_free_endpoint(model_id):
    # ":free"-suffixed ids are literal $0 endpoints; the auto-router is the
    # explicit free-model last resort (config: openrouter/free).
    return model_id.endswith(":free") or model_id == AUTO_ROUTER_MODEL


# ---------------------------------------------------------------------------
# Discovery: the zero-paid-resources gate
# ---------------------------------------------------------------------------

MIXED_CATALOG = [
    _entry("z-ai/glm-5.2:free"),                       # good free model
    _entry("openai/gpt-4o", prompt="0.0025", completion="0.01"),  # PAID slug
    _entry("anthropic/claude-4:paid", prompt="0", completion="0"),  # $0 but no :free
    _entry("x/experimental:free", prompt="0.001", completion="0"),  # :free but paid price
    _entry("y/broken:free", prompt="ask", completion="0"),          # unparsable price
    _entry("nvidia/tiny:free", context=4096),          # below MIN_CONTEXT_TOKENS
    _entry("google/gemma-4-31b-it:free"),              # good free model
]


def test_every_discovered_model_is_a_free_endpoint(tmp_path):
    transport = _FakeTransport(catalog=MIXED_CATALOG)
    models = _client(transport, tmp_path).discover_free_models()

    assert models, "discovery must return models for a healthy catalog"
    for model_id, _supports_json in models:
        assert _is_free_endpoint(model_id), (
            f"paid model {model_id!r} entered the discovery chain"
        )


def test_paid_models_never_surface_in_discovery(tmp_path):
    transport = _FakeTransport(catalog=MIXED_CATALOG)
    ids = [m for m, _ in _client(transport, tmp_path).discover_free_models()]

    assert "openai/gpt-4o" not in ids  # paid slug
    assert "anthropic/claude-4:paid" not in ids  # $0 listing without :free
    assert "x/experimental:free" not in ids  # free suffix, non-zero price
    assert "y/broken:free" not in ids  # unparsable price
    assert "nvidia/tiny:free" not in ids  # context too small


def test_discovery_keeps_only_zero_priced_free_models(tmp_path):
    transport = _FakeTransport(catalog=MIXED_CATALOG)
    ids = [m for m, _ in _client(transport, tmp_path).discover_free_models()]

    assert "z-ai/glm-5.2:free" in ids
    assert "google/gemma-4-31b-it:free" in ids


def test_auto_router_free_model_is_the_last_resort(tmp_path):
    transport = _FakeTransport(catalog=MIXED_CATALOG)
    ids = [m for m, _ in _client(transport, tmp_path).discover_free_models()]

    assert ids[-1] == AUTO_ROUTER_MODEL
    assert _is_free_endpoint(AUTO_ROUTER_MODEL)


def test_discovery_failure_falls_back_to_the_static_free_roster(tmp_path):
    transport = _FakeTransport(discovery_status=500)
    models = _client(transport, tmp_path).discover_free_models()

    assert models == [(m, True) for m in OPENROUTER_MODELS]
    # The fallback roster itself must be free-only.
    for model_id, _supports_json in models:
        assert _is_free_endpoint(model_id), (
            f"static fallback roster contains a non-free model: {model_id!r}"
        )


def test_no_api_key_is_required_by_default(tmp_path, monkeypatch):
    # Keyless discovery must not raise and must not even reach the network:
    # the static free roster keeps the backend usable with zero setup.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    transport = _FakeTransport(catalog=MIXED_CATALOG)

    models = _client(transport, tmp_path).discover_free_models()

    assert transport.get_calls == []  # never called without a key
    for model_id, _supports_json in models:
        assert _is_free_endpoint(model_id)


def test_check_api_key_raises_a_loud_error_only_when_used(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _client(_FakeTransport(), tmp_path)

    try:
        client.check_api_key()
    except LLMError as exc:
        assert "OPENROUTER_API_KEY is not set" in str(exc)
    else:
        raise AssertionError("key check must fail loudly when the backend is selected")


# ---------------------------------------------------------------------------
# The live chain: every POST goes to a free endpoint, even on fall-through
# ---------------------------------------------------------------------------

def test_complete_never_posts_a_paid_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    rate_limited = _FakeResponse(429, {}, headers={"Retry-After": "0"})
    transport = _FakeTransport(
        catalog=MIXED_CATALOG,
        # First free model: throttled until the retries run out; second
        # free model: answers. The chain must stay free end to end.
        post_responses=[rate_limited, rate_limited, rate_limited, _FakeResponse(
            200, {"choices": [{"message": {"content": "{}"}}]}
        )],
    )

    text, model = _client(transport, tmp_path).complete([{"role": "user", "content": "hi"}])

    assert text == "{}"
    assert transport.posted_models, "the chain must attempt at least one model"
    for model_id in transport.posted_models:
        assert _is_free_endpoint(model_id), (
            f"paid model {model_id!r} received a research request"
        )
    assert model == transport.posted_models[-1]


def test_completion_via_graduated_free_model_never_falls_back_to_paid(
    tmp_path, monkeypatch
):
    # A cached ":free" id that graduated to paid answers 404 "unavailable
    # for free" - it must be dropped, never swapped for a paid slug.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    gone_paid = _FakeResponse(404, {}, text="unavailable for free, use the paid slug")
    good = [_FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})]
    transport = _FakeTransport(
        catalog=MIXED_CATALOG,
        post_responses=[gone_paid] + good,
    )

    _text, model = _client(transport, tmp_path).complete(
        [{"role": "user", "content": "hi"}]
    )

    for model_id in transport.posted_models:
        assert _is_free_endpoint(model_id)
    assert model.endswith(":free") or model == AUTO_ROUTER_MODEL
