"""Obvious agent backend - dispatch/collect invariants and the relay contract.

The backend must never lose the pipeline's safety posture: a dispatch failure
or an unanswered session is a LOUD LLMError (the runner's research-failure
path), the answer must parse into the v34 decision schema, and the relay must
be first-write-wins with token enforcement - a late failurePrompt can never
overwrite a delivered answer.
"""

import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from relay.obvious_relay import make_handler
from review_hub.engine.research import LLMError
from review_hub.engine.research.obvious import ObviousAgentClient

DECISION = {"decision": "ACCEPT", "new_values": {"website_url": "https://example.com"}}
DECISION_JSON = json.dumps(DECISION)


# ---------------------------------------------------------------------------
# Transport fakes - record what would leave the process
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        return self._body


class _FakeTransport:
    """Scripted POSTs (the External API) and GETs (the relay).

    Every scripted entry is either a _FakeResponse to return or an Exception
    to raise (the real transport raises requests.RequestException subclasses).
    """

    def __init__(self, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        entry = self.post_responses.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        entry = self.get_responses.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


def _client(transport, tmp_path, **kw):
    defaults = dict(
        api_key="obv_test_key",
        project_id="prj_test",
        relay_base="https://relay.example",
        relay_token="tok",
        transport=transport,
        sleeper=lambda seconds: None,  # never actually wait in tests
        clock=lambda: 0.0,
        log_dir=tmp_path,
    )
    defaults.update(kw)
    return ObviousAgentClient(**defaults)


def _dispatch_ok():
    return _FakeResponse(200, {"thread": {"id": "th_1"}, "execution": {"id": "exec_1"}})


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_dispatch_posts_thread_to_project_endpoint(tmp_path):
    transport = _FakeTransport(post_responses=[_dispatch_ok()])
    client = _client(transport, tmp_path)

    result = client.dispatch("research prompt", "system rules")

    url, kwargs = transport.posts[0]
    assert url == "https://api.app.obvious.ai/api/v1/projects/prj_test/thread"
    assert kwargs["headers"]["Authorization"] == "Bearer obv_test_key"
    body = kwargs["json"]
    assert "research prompt" in body["starterPrompt"]
    assert "system rules" in body["starterPrompt"]
    # Delivery contract travels in all three prompts.
    for field in ("starterPrompt", "successPrompt", "failurePrompt"):
        assert "/answers/" in body[field]
        assert "?token=tok" in body[field]
    assert result["thread_id"] == "th_1"
    assert result["session_id"]


def test_dispatch_retries_documented_409_race_then_succeeds(tmp_path):
    transport = _FakeTransport(
        post_responses=[_FakeResponse(409, {"error": "active execution"}), _dispatch_ok()]
    )
    client = _client(transport, tmp_path)
    assert client.dispatch("p", "s")["thread_id"] == "th_1"
    assert len(transport.posts) == 2


def test_dispatch_retries_5xx_then_succeeds(tmp_path):
    transport = _FakeTransport(
        post_responses=[_FakeResponse(503, text="down"), _dispatch_ok()]
    )
    client = _client(transport, tmp_path)
    assert client.dispatch("p", "s")["thread_id"] == "th_1"


def test_dispatch_401_is_a_loud_key_error(tmp_path):
    transport = _FakeTransport(post_responses=[_FakeResponse(401, text="nope")])
    client = _client(transport, tmp_path)
    with pytest.raises(LLMError, match="OBVIOUS_API_KEY"):
        client.dispatch("p", "s")


def test_dispatch_404_is_a_loud_project_error(tmp_path):
    transport = _FakeTransport(post_responses=[_FakeResponse(404, text="gone")])
    client = _client(transport, tmp_path)
    with pytest.raises(LLMError, match="OBVIOUS_PROJECT_ID"):
        client.dispatch("p", "s")


def test_dispatch_exhausts_retries_on_network_errors(tmp_path):
    transport = _FakeTransport(post_responses=[requests.ConnectionError("down")] * 3)
    client = _client(transport, tmp_path)
    with pytest.raises(LLMError, match="did not succeed"):
        client.dispatch("p", "s")


# ---------------------------------------------------------------------------
# Collect (relay polling)
# ---------------------------------------------------------------------------

def _relay_answer(answer):
    return _FakeResponse(200, {"state": "answered", "answer": answer})


def test_collect_returns_answer_after_pending_polls(tmp_path):
    transport = _FakeTransport(
        get_responses=[
            _FakeResponse(200, {"state": "pending"}),
            _relay_answer(DECISION_JSON),
        ]
    )
    client = _client(transport, tmp_path)
    assert client.collect("abc") == DECISION_JSON
    url, kwargs = transport.gets[0]
    assert url.endswith("/answers/abc")
    assert kwargs["headers"] == {"X-Relay-Token": "tok"}


def test_collect_times_out_loud_when_no_answer_arrives(tmp_path):
    transport = _FakeTransport(get_responses=[])
    client = _client(transport, tmp_path, wait_timeout=0.0)
    with pytest.raises(LLMError, match="did not deliver an answer"):
        client.collect("abc")


def test_collect_tolerates_transient_relay_errors(tmp_path):
    # Two polls blip out before the answer lands - the poller must ride
    # through transient relay failures instead of failing the record.
    transport = _FakeTransport(
        get_responses=[
            requests.ConnectionError("blip"),
            requests.ConnectionError("blip"),
            _relay_answer(DECISION_JSON),
        ]
    )
    client = _client(transport, tmp_path)
    assert client.collect("abc") == DECISION_JSON


# ---------------------------------------------------------------------------
# research(): the ResearchBackend protocol
# ---------------------------------------------------------------------------

def test_research_returns_parsed_decision(tmp_path):
    transport = _FakeTransport(
        post_responses=[_dispatch_ok()],
        get_responses=[_relay_answer(f"Here is the answer:\n```json\n{DECISION_JSON}\n```")],
    )
    client = _client(transport, tmp_path)
    value = client.research("prompt", "system")
    assert value["decision"] == "ACCEPT"
    assert value["new_values"]["website_url"] == "https://example.com"


def test_research_surfaces_agent_failure_reports(tmp_path):
    transport = _FakeTransport(
        post_responses=[_dispatch_ok()],
        get_responses=[_relay_answer(json.dumps({"error": "no usable sources found"}))],
    )
    client = _client(transport, tmp_path)
    with pytest.raises(LLMError, match="no usable sources found"):
        client.research("prompt", "system")


def test_research_rejects_non_json_answers(tmp_path):
    transport = _FakeTransport(
        post_responses=[_dispatch_ok()],
        get_responses=[_relay_answer("the supplier looks fine, I accept it")],
    )
    client = _client(transport, tmp_path)
    with pytest.raises(LLMError, match="not usable JSON"):
        client.research("prompt", "system")


def test_research_without_relay_config_fails_loud(tmp_path):
    client = _client(_FakeTransport(post_responses=[_dispatch_ok()]), tmp_path,
                     relay_base="")
    with pytest.raises(LLMError, match="OBVIOUS_RELAY_URL"):
        client.research("prompt", "system")


def test_preflight_lists_every_missing_piece(tmp_path):
    client = _client(
        _FakeTransport(), tmp_path, api_key="", project_id="", relay_base="", relay_token=""
    )
    problems = "\n".join(client.preflight())
    assert "OBVIOUS_API_KEY" in problems
    assert "OBVIOUS_PROJECT_ID" in problems
    assert "OBVIOUS_RELAY_URL" in problems
    assert "OBVIOUS_RELAY_TOKEN" in problems


# ---------------------------------------------------------------------------
# The relay itself (real HTTP over loopback)
# ---------------------------------------------------------------------------

class _Relay:
    def __init__(self, token="tok"):
        self.token = token
        self.data_dir = Path(tempfile.mkdtemp())
        handler = make_handler(token, self.data_dir)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _http(method, url, token, body=None):
    request = urllib.request.Request(url, method=method)
    if token:
        request.add_header("X-Relay-Token", token)
    data = None
    if body is not None:
        data = body.encode("utf-8")
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data=data) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_relay_store_then_poll_roundtrip():
    relay = _Relay()
    try:
        status, payload = _http(
            "POST", relay.url("/answers/" + "a" * 32 + "?token=tok"), "tok", DECISION_JSON
        )
        assert (status, payload["state"]) == (201, "stored")

        status, payload = _http("GET", relay.url("/answers/" + "a" * 32), "tok")
        assert (status, payload["state"]) == (200, "answered")
        assert payload["answer"] == DECISION_JSON

        status, payload = _http("GET", relay.url("/answers/" + "b" * 32), "tok")
        assert (status, payload["state"]) == (200, "pending")
    finally:
        relay.stop()


def test_relay_first_write_wins():
    relay = _Relay()
    try:
        sid = "c" * 32
        _http("POST", relay.url(f"/answers/{sid}?token=tok"), "tok", DECISION_JSON)
        status, _ = _http(
            "POST", relay.url(f"/answers/{sid}?token=tok"), "tok",
            json.dumps({"error": "late failure report"}),
        )
        assert status == 409
        _, payload = _http("GET", relay.url(f"/answers/{sid}"), "tok")
        assert payload["answer"] == DECISION_JSON  # the answer survived
    finally:
        relay.stop()


def test_relay_refuses_wrong_or_missing_token():
    relay = _Relay(token="secret")
    try:
        sid = "d" * 32
        assert _http("POST", relay.url(f"/answers/{sid}?token=wrong"), "wrong", "{}")[0] == 403
        assert _http("POST", relay.url(f"/answers/{sid}"), "", "{}")[0] == 403
        assert _http("GET", relay.url(f"/answers/{sid}"), "wrong")[0] == 403
    finally:
        relay.stop()


def test_relay_rejects_malformed_session_ids_and_oversize_bodies():
    relay = _Relay()
    try:
        assert _http("POST", relay.url("/answers/../../etc?token=tok"), "tok", "{}")[0] in (400, 404)
        big = "x" * (256 * 1024 + 1)
        assert _http("POST", relay.url(f"/answers/{'e' * 32}?token=tok"), "tok", big)[0] == 413
    finally:
        relay.stop()
