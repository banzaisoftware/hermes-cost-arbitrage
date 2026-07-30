"""Tests for the entitlement probe.

Driven against a real ``http.server.ThreadingHTTPServer`` bound to
``127.0.0.1:0`` rather than a mocked ``urlopen``. The point of this module is
what goes on the wire — the path, the method, the body, the headers — and a
mock of the transport cannot prove any of that. Every test that cares about
traffic asserts on the server's own recording of what it received.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest

from hermes_cost_arbitrage_dashboard.entitlement import (
    ANTHROPIC_API_MODE,
    BLOCKING_STATUSES,
    PROBE_HANDLERS,
    PROBEABLE_API_MODE,
    PROVIDER_MESSAGE_LIMIT,
    ProbeResult,
    probe_model,
)

# Reserved by RFC 2606 for exactly this purpose: guaranteed never to resolve,
# so a DNS-failure test doesn't depend on the operator's network never
# accidentally routing it somewhere.
_UNRESOLVABLE_HOST = "this-host-does-not-exist.invalid"


def _probe_chat(*args, **kwargs):
    """``probe_model`` for the one api_mode that is actually probed.

    Every test below that cares about the wire — the path, the body, the
    headers, the status mapping — is a test of the ``chat_completions``
    protocol, because that is the only one the probe speaks. Filling the
    argument here keeps that fact stated once instead of on 24 call sites.
    The api_mode allowlist itself is tested against ``probe_model`` directly,
    under "api_mode allowlist" below.
    """
    kwargs.setdefault("api_mode", PROBEABLE_API_MODE)
    return probe_model(*args, **kwargs)


class _RecordingHandler(BaseHTTPRequestHandler):
    """Records every request it receives and answers via ``server.respond``.

    ``server.respond(path, body) -> (status, headers, response_body)`` is set
    per test, so each test controls exactly what the fake provider says
    without subclassing the handler again.
    """

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "path": self.path,
                "method": self.command,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        status, headers, response_body = self.server.respond(self.path, body)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        self._handle()

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        self._handle()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep test output clean


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.requests = []
    server.respond = lambda path, body: (200, {}, b"")
    server.thread = threading.Thread(target=server.serve_forever, daemon=True)
    server.thread.start()
    return server


def _stop_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()
    server.thread.join(timeout=5)


@pytest.fixture
def server():
    srv = _start_server()
    try:
        yield srv
    finally:
        _stop_server(srv)


def _base_url(srv: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{srv.server_port}/v1"


def _json_response(status: int, payload: dict) -> tuple[int, dict, bytes]:
    body = json.dumps(payload).encode("utf-8")
    return status, {"Content-Type": "application/json"}, body


# --- status table -----------------------------------------------------------


def test_2xx_returns_callable_and_is_not_blocking(server):
    server.respond = lambda path, body: _json_response(200, {"choices": []})

    result = _probe_chat(_base_url(server), "sk-test", "some-model")

    assert isinstance(result, ProbeResult)
    assert result.status == "callable"
    assert result.http_status == 200
    assert result.status not in BLOCKING_STATUSES
    assert result.reason


def test_404_returns_not_entitled_and_is_blocking(server):
    server.respond = lambda path, body: _json_response(
        404, {"error": "Function 'abc123': Not found for account 'redacted-account'"}
    )

    result = _probe_chat(_base_url(server), "sk-test", "moonshotai/kimi-k2.6")

    assert result.status == "not_entitled"
    assert result.http_status == 404
    assert result.status in BLOCKING_STATUSES


@pytest.mark.parametrize("code", [401, 403])
def test_401_and_403_return_credential_rejected_and_are_blocking(server, code):
    server.respond = lambda path, body: _json_response(code, {"error": "invalid credential"})

    result = _probe_chat(_base_url(server), "sk-test", "some-model")

    assert result.status == "credential_rejected"
    assert result.http_status == code
    assert result.status in BLOCKING_STATUSES


def test_429_returns_throttled_and_is_not_blocking(server):
    server.respond = lambda path, body: _json_response(429, {"error": "rate limited"})

    result = _probe_chat(_base_url(server), "sk-test", "some-model")

    assert result.status == "throttled"
    assert result.http_status == 429
    assert result.status not in BLOCKING_STATUSES


def test_other_http_code_returns_unknown_and_is_not_blocking(server):
    server.respond = lambda path, body: _json_response(500, {"error": "internal error"})

    result = _probe_chat(_base_url(server), "sk-test", "some-model")

    assert result.status == "unknown"
    assert result.http_status == 500
    assert result.status not in BLOCKING_STATUSES


def test_timeout_returns_unknown(server):
    def slow_respond(path, body):
        time.sleep(1.0)
        return _json_response(200, {})

    server.respond = slow_respond

    result = _probe_chat(_base_url(server), "sk-test", "some-model", timeout=0.2)

    assert result.status == "unknown"
    assert result.http_status is None
    assert result.status not in BLOCKING_STATUSES


def test_dns_failure_returns_unknown():
    result = _probe_chat(f"https://{_UNRESOLVABLE_HOST}/v1", "sk-test", "some-model", timeout=5.0)

    assert result.status == "unknown"
    assert result.http_status is None


def test_connection_refused_returns_unknown():
    # Bind and immediately close a socket to get a port nothing is
    # listening on, rather than guessing at one.
    import socket

    probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()

    result = _probe_chat(f"http://127.0.0.1:{port}/v1", "sk-test", "some-model", timeout=5.0)

    assert result.status == "unknown"
    assert result.http_status is None


def test_malformed_url_returns_unknown():
    result = _probe_chat("http://", "sk-test", "some-model")

    assert result.status == "unknown"
    assert result.http_status is None


@pytest.mark.parametrize("base_url", ["http://[", "https://[::1", "http://[not-an-address]/v1"])
def test_url_that_does_not_even_parse_returns_unknown_instead_of_raising(base_url):
    # urlsplit is not total: it raises ValueError("Invalid IPv6 URL") on
    # these. The module promises never to raise, and this value comes
    # straight off a config file, so the parse has to be guarded too.
    # "http://" (above) is not this case — it parses fine and fails later.
    result = _probe_chat(base_url, "sk-test", "some-model")

    assert result.status == "unknown"
    assert result.status not in BLOCKING_STATUSES
    assert result.http_status is None
    assert result.reason


def test_empty_base_url_returns_skipped(server):
    result = _probe_chat("", "sk-test", "some-model")

    assert result.status == "skipped"
    assert result.status not in BLOCKING_STATUSES
    assert result.http_status is None
    assert server.requests == []


def test_empty_api_key_returns_skipped(server):
    result = _probe_chat(_base_url(server), "", "some-model")

    assert result.status == "skipped"
    assert result.http_status is None
    assert server.requests == []


def test_non_http_scheme_returns_skipped(server):
    result = _probe_chat("ftp://127.0.0.1/v1", "sk-test", "some-model")

    assert result.status == "skipped"
    assert result.http_status is None
    assert server.requests == []


def test_file_url_returns_skipped_and_reads_nothing(tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("this must never be read by the probe")

    result = _probe_chat(f"file://{secret_file}", "sk-test", "some-model")

    assert result.status == "skipped"
    assert result.http_status is None
    assert result.provider_message == ""
    # The clearest proof "nothing was read": the file's contents never
    # appear anywhere in the result.
    assert "secret" not in result.reason
    assert "never be read" not in result.reason


# --- api_mode allowlist -------------------------------------------------------
#
# The host resolves four wire protocols and records the one it picked on
# ModelSwitchResult.api_mode (hermes_cli/model_switch.py:290; the four modes
# are mapped at hermes_cli/providers.py:385-390). PROBE_HANDLERS covers two of
# them: chat_completions (this section) and anthropic_messages (its own
# section further down, "anthropic_messages protocol"). Probing a
# codex_responses or bedrock_converse provider on either path would collect a
# 404 or 403 that says nothing about entitlement, and — since those are two of
# the blocking statuses — would refuse a switch that works. Hence an
# allowlist: probe only the modes we understand, skip everything else,
# including a mode we have never heard of.


@pytest.mark.parametrize(
    "api_mode",
    ["codex_responses", "bedrock_converse", "", "some_future_mode"],
)
def test_unmapped_api_mode_is_skipped_and_makes_no_request(server, api_mode):
    result = probe_model(_base_url(server), "sk-test", "some-model", api_mode=api_mode)

    assert result.status == "skipped"
    assert result.status not in BLOCKING_STATUSES
    assert result.http_status is None
    # Not merely "did not block": no request was made at all, so the provider
    # never saw a path it does not serve and never saw the credential.
    assert server.requests == []


@pytest.mark.parametrize("api_mode", ["codex_responses", ""])
def test_skipped_unmapped_mode_reason_names_the_mode(server, api_mode):
    result = probe_model(_base_url(server), "sk-test", "some-model", api_mode=api_mode)

    # The operator has to be able to tell *why* their switch was not
    # pre-checked, and the mode is the answer.
    assert (api_mode or "(unknown)") in result.reason


def test_probe_handlers_cover_exactly_chat_completions_and_anthropic_messages():
    assert set(PROBE_HANDLERS) == {"chat_completions", "anthropic_messages"}


def test_omitted_api_mode_is_skipped_rather_than_probed(server):
    # An older ModelSwitchResult has no api_mode field at all, and the
    # endpoint reads it with a getattr default. The safe default here is
    # "skip", never "probe anyway".
    result = probe_model(_base_url(server), "sk-test", "some-model")

    assert result.status == "skipped"
    assert server.requests == []


def test_chat_completions_api_mode_is_probed(server):
    server.respond = lambda path, body: _json_response(200, {})

    result = probe_model(_base_url(server), "sk-test", "some-model", api_mode="chat_completions")

    assert result.status == "callable"
    assert len(server.requests) == 1
    assert server.requests[0]["path"] == "/v1/chat/completions"


def test_probeable_api_mode_is_the_hosts_openai_chat_transport_mode():
    # hermes_cli/providers.py:385-390 maps transport "openai_chat" to api mode
    # "chat_completions"; nvidia is transport="openai_chat"
    # (providers.py:175-179). Pinning the literal here means a rename on the
    # host side surfaces as a failing test rather than a probe that silently
    # skips every provider forever.
    assert PROBEABLE_API_MODE == "chat_completions"


# --- request construction ----------------------------------------------------


def test_request_path_is_chat_completions_and_never_models(server):
    server.respond = lambda path, body: _json_response(200, {})

    _probe_chat(_base_url(server), "sk-test", "some-model")

    assert len(server.requests) == 1
    assert server.requests[0]["path"] == "/v1/chat/completions"
    assert not any(r["path"].endswith("/models") for r in server.requests)


def test_request_method_is_post(server):
    server.respond = lambda path, body: _json_response(200, {})

    _probe_chat(_base_url(server), "sk-test", "some-model")

    assert server.requests[0]["method"] == "POST"


def test_request_body_carries_max_tokens_one_and_the_model_id(server):
    server.respond = lambda path, body: _json_response(200, {})

    _probe_chat(_base_url(server), "sk-test", "moonshotai/kimi-k2.6")

    sent = json.loads(server.requests[0]["body"])
    assert sent["max_tokens"] == 1
    assert sent["model"] == "moonshotai/kimi-k2.6"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


def test_request_headers_carry_bearer_key_and_json_content_type(server):
    server.respond = lambda path, body: _json_response(200, {})

    _probe_chat(_base_url(server), "sk-super-secret", "some-model")

    headers = server.requests[0]["headers"]
    assert headers["authorization"] == "Bearer sk-super-secret"
    assert headers["content-type"] == "application/json"


# --- provider_message handling ------------------------------------------------


def test_404_message_returned_verbatim_and_untruncated_when_short(server):
    message = "Function 'function-id-placeholder': Not found for account 'placeholder-account'"
    server.respond = lambda path, body: (
        404,
        {"Content-Type": "application/json"},
        message.encode("utf-8"),
    )

    result = _probe_chat(_base_url(server), "sk-test", "some-model")

    assert result.provider_message == message


def test_long_error_body_is_truncated_to_the_provider_message_limit(server):
    long_message = "x" * (PROVIDER_MESSAGE_LIMIT * 3)
    server.respond = lambda path, body: (500, {}, long_message.encode("utf-8"))

    result = _probe_chat(_base_url(server), "sk-test", "some-model")

    assert len(result.provider_message) == PROVIDER_MESSAGE_LIMIT
    assert result.provider_message == long_message[:PROVIDER_MESSAGE_LIMIT]


def test_error_body_containing_the_api_key_is_redacted(server):
    api_key = "sk-super-secret-value"
    message = f"invalid request, credential used was {api_key}"
    server.respond = lambda path, body: (403, {}, message.encode("utf-8"))

    result = _probe_chat(_base_url(server), api_key, "some-model")

    assert api_key not in result.provider_message
    assert api_key not in result.reason
    assert "[REDACTED]" in result.provider_message


def test_key_straddling_the_truncation_boundary_is_still_fully_redacted(server):
    # Redaction must run on the untruncated text. If truncation ran first,
    # a key positioned across the PROVIDER_MESSAGE_LIMIT cut would no longer
    # be intact in the truncated text, so an exact-string redact would miss
    # it and a fragment of the real key would survive verbatim.
    api_key = "sk-boundary-straddling-secret-0123456789"
    # Position the key so a sizeable chunk of it (not just a couple of
    # characters) falls on the near side of the truncation cut: a leak of
    # only 1-2 characters could dodge the fragment_length check below for
    # the wrong reason (too short to test), which would defeat the point.
    prefix = "y" * (PROVIDER_MESSAGE_LIMIT - 30)
    suffix = "z" * 200
    message = prefix + api_key + suffix
    key_start = message.find(api_key)
    assert key_start < PROVIDER_MESSAGE_LIMIT < key_start + len(api_key), (
        "test setup bug: the key must straddle PROVIDER_MESSAGE_LIMIT for this to be a real check"
    )
    server.respond = lambda path, body: (500, {}, message.encode("utf-8"))

    result = _probe_chat(_base_url(server), api_key, "some-model")

    assert api_key not in result.provider_message
    # The failure mode is a *fragment* surviving, not just the whole key, so
    # check every sufficiently-long substring of the key rather than only
    # the full string.
    fragment_length = 8
    for start in range(0, len(api_key) - fragment_length + 1):
        fragment = api_key[start : start + fragment_length]
        assert fragment not in result.provider_message, (
            f"fragment {fragment!r} of the API key leaked into provider_message"
        )


# --- the API key never leaves the process --------------------------------------
#
# Two independent layers, tested independently:
#   1. an unusable key never reaches http.client at all, and
#   2. if it somehow did, the exception message it produces is not passed
#      through verbatim.
# Layer 2 is unreachable while layer 1 holds — which is the point of testing
# it with layer 1 disabled.


@pytest.mark.parametrize(
    "api_key",
    [
        "sk-trailing-newline\n",       # a key read from a file, or a Docker secret
        "sk-trailing-carriage\r",
        "sk-interior\nnewline",
        "sk-trailing-space ",
        " sk-leading-space",
        "sk-embedded\0null",
    ],
)
def test_api_key_that_cannot_be_sent_in_a_header_is_skipped_and_makes_no_request(server, api_key):
    result = _probe_chat(_base_url(server), api_key, "some-model")

    assert result.status == "skipped"
    assert result.status not in BLOCKING_STATUSES
    assert result.http_status is None
    assert server.requests == []
    # The diagnostic has to be usable on its own: an operator whose every call
    # fails needs to be told the key is malformed, not just "skipped".
    assert "header" in result.reason
    # ... and the malformed key itself must not be quoted back to say so.
    assert api_key.strip() not in result.reason


def test_a_header_rejected_key_does_not_leak_through_the_exception_message(server, monkeypatch):
    # Layer 1 removed on purpose. http.client.putheader raises
    # ValueError('Invalid header value %r' % value) — it really does echo the
    # header value, credential included — and %r escapes the newline into a
    # two-character \n, so a redact that matches only the verbatim key would
    # miss it and return the whole usable portion of the credential to the
    # browser. This is the regression test for that leak.
    import hermes_cost_arbitrage_dashboard.entitlement as entitlement

    monkeypatch.setattr(entitlement, "_key_is_header_safe", lambda api_key: True)

    api_key = "sk-super-secret-value-0123456789\n"
    result = _probe_chat(_base_url(server), api_key, "some-model")

    assert result.status == "unknown"
    assert server.requests == []
    haystack = f"{result.reason} {result.provider_message}"
    assert api_key not in haystack
    assert api_key.strip() not in haystack
    assert repr(api_key)[1:-1] not in haystack
    # A fragment is a leak too: a key is guessable from most of itself.
    usable = api_key.strip()
    fragment_length = 8
    for start in range(0, len(usable) - fragment_length + 1):
        fragment = usable[start : start + fragment_length]
        assert fragment not in haystack, f"fragment {fragment!r} of the API key leaked into the result"


def test_redact_all_removes_the_repr_escaped_form_of_the_key():
    # The defensive layer under the exception handling, unit-tested directly
    # because nothing reaches it while _key_is_header_safe holds.
    import hermes_cost_arbitrage_dashboard.entitlement as entitlement

    api_key = "sk-secret-with-newline\n"
    # Exactly what http.client.putheader would produce.
    text = "Invalid header value %r" % f"Bearer {api_key}".encode()

    redacted = entitlement._redact_all(text, api_key)

    assert api_key.strip() not in redacted
    assert repr(api_key)[1:-1] not in redacted
    assert "[REDACTED]" in redacted


def test_a_transport_failure_reason_names_the_exception_type_without_its_message():
    result = _probe_chat(f"https://{_UNRESOLVABLE_HOST}/v1", "sk-test", "some-model", timeout=5.0)

    assert result.status == "unknown"
    # Useful to an operator (which layer failed, and why the socket said so)
    # without ever interpolating str(exc), which is not vetted for the key.
    assert "URLError" in result.reason


def test_the_error_body_read_is_bounded(monkeypatch):
    # White-box on purpose: a read bound is not observable from the outside,
    # and this one matters — the probe runs inside the endpoint that writes
    # the host's config, and the timeout is per-recv, not a budget for the
    # whole transfer. An unbounded read lets a slow or oversized error body
    # hold that endpoint open indefinitely.
    import urllib.error
    import urllib.request

    import hermes_cost_arbitrage_dashboard.entitlement as entitlement

    class _RecordingHTTPError(urllib.error.HTTPError):
        def __init__(self):
            self.read_args = []
            super().__init__("http://example.invalid/v1/chat/completions", 404, "Not Found", {}, None)

        def read(self, *args):
            self.read_args.append(args)
            return b"not found"

    error = _RecordingHTTPError()

    class _RaisingOpener:
        def open(self, request, timeout=None):
            raise error

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: _RaisingOpener())

    result = entitlement.probe_model(
        "https://example.invalid/v1", "sk-test", "some-model", api_mode=PROBEABLE_API_MODE
    )

    assert result.status == "not_entitled"
    assert error.read_args == [(PROVIDER_MESSAGE_LIMIT * 8,)]


# --- redirect handling ---------------------------------------------------------


def test_redirect_to_another_host_is_not_followed_and_no_second_request(server):
    secondary = _start_server()
    try:
        secondary.respond = lambda path, body: _json_response(200, {})
        redirect_target = _base_url(secondary) + "/chat/completions"
        server.respond = lambda path, body: (
            302,
            {"Location": redirect_target},
            b"",
        )

        result = _probe_chat(_base_url(server), "sk-test", "some-model")

        assert result.status == "unknown"
        assert result.status not in BLOCKING_STATUSES
        assert result.http_status == 302
        assert len(server.requests) == 1
        assert secondary.requests == []
    finally:
        _stop_server(secondary)


# --- ProbeResult / BLOCKING_STATUSES shape ------------------------------------


def test_blocking_statuses_are_exactly_not_entitled_and_credential_rejected():
    assert BLOCKING_STATUSES == frozenset({"not_entitled", "credential_rejected"})


def test_probe_result_reason_is_always_set(server):
    server.respond = lambda path, body: _json_response(200, {})

    callable_result = _probe_chat(_base_url(server), "sk-test", "some-model")
    skipped_result = _probe_chat("", "", "some-model")

    assert callable_result.reason
    assert skipped_result.reason


# --- anthropic_messages protocol -----------------------------------------------
#
# No real-HTTP-server approach here: the probe speaks through the host's own
# agent.anthropic_adapter.build_anthropic_client, never a hand-built request
# (see dashboard/entitlement.py's _probe_anthropic_messages docstring for why
# — reimplementing OAuth header handling would 401 a working subscription
# token). So instead of a server recording what hit the wire, a fake
# agent.anthropic_adapter is injected into sys.modules — the same technique
# tests/test_plugin_api.py already uses to fake hermes_cli and agent.models_dev
# — and tests assert on what the fake client received.

_ABSENT = object()


class _FakeAnthropicStatusError(Exception):
    """Stands in for anthropic.APIStatusError without importing the SDK.

    The real exception exposes ``status_code``; that attribute (via
    ``getattr(exc, "status_code", None)``) is all the probe is allowed to
    rely on, per the task brief.
    """

    def __init__(self, status_code: int, message: str = "provider error") -> None:
        super().__init__(message)
        self.status_code = status_code


def _install_fake_anthropic_adapter(
    monkeypatch,
    *,
    create_side_effect: Exception | None = None,
    prefix: object = _ABSENT,
    importable: bool = True,
):
    """Fake ``agent.anthropic_adapter`` in sys.modules.

    Returns ``(fake_client, fake_adapter)`` so a test can inspect exactly
    what the probe sent (``fake_client.messages.create.call_args``,
    ``fake_client.mock_calls`` to prove nothing else on the client was ever
    touched) and how the probe called the builder
    (``fake_adapter.build_anthropic_client.call_args``).

    ``importable=False`` simulates an older host or a trimmed install where
    the module cannot be imported at all — returns ``(None, None)``.

    ``fake_adapter`` is a real ``types.ModuleType``, not a ``MagicMock``:
    a MagicMock auto-vivifies any attribute access, which would silently
    defeat the "the system-prefix name is genuinely absent" case
    (``prefix`` left at ``_ABSENT``) that the defensive-import fallback
    test below depends on.
    """
    if not importable:
        monkeypatch.setitem(sys.modules, "agent", None)
        monkeypatch.setitem(sys.modules, "agent.anthropic_adapter", None)
        return None, None

    fake_client = MagicMock(name="anthropic_client")
    fake_client.messages.create = MagicMock(side_effect=create_side_effect)

    fake_adapter = types.ModuleType("agent.anthropic_adapter")
    fake_adapter.build_anthropic_client = MagicMock(return_value=fake_client)
    if prefix is not _ABSENT:
        fake_adapter._CLAUDE_CODE_SYSTEM_PREFIX = prefix

    fake_agent = types.ModuleType("agent")
    fake_agent.anthropic_adapter = fake_adapter

    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.anthropic_adapter", fake_adapter)
    return fake_client, fake_adapter


def test_anthropic_api_mode_matches_the_hosts_anthropic_messages_transport_mode():
    # hermes_cli/providers.py:101-104 resolves the "anthropic" provider to
    # transport="anthropic_messages"; providers.py:385-390 maps that
    # transport onto the api_mode string of the same name. Pinning the
    # literal here means a rename on the host side surfaces as a failing
    # test rather than a probe that silently skips every Anthropic switch.
    assert ANTHROPIC_API_MODE == "anthropic_messages"


@pytest.mark.parametrize(
    "status_code,expected_status,blocking",
    [
        (404, "not_entitled", True),
        (401, "credential_rejected", True),
        (403, "credential_rejected", True),
        (429, "throttled", False),
        (400, "unknown", False),
        (500, "unknown", False),
    ],
)
def test_anthropic_status_table_matches_chat_completions_classification(
    monkeypatch, status_code, expected_status, blocking
):
    _install_fake_anthropic_adapter(
        monkeypatch, create_side_effect=_FakeAnthropicStatusError(status_code)
    )

    result = probe_model(
        "https://api.anthropic.com", "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE
    )

    assert result.status == expected_status
    assert result.http_status == status_code
    assert (result.status in BLOCKING_STATUSES) is blocking


def test_anthropic_successful_call_is_callable_and_not_blocking(monkeypatch):
    _install_fake_anthropic_adapter(monkeypatch)

    result = probe_model(
        "https://api.anthropic.com", "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE
    )

    assert result.status == "callable"
    assert result.http_status == 200
    assert result.status not in BLOCKING_STATUSES


def test_anthropic_exception_without_status_code_is_unknown(monkeypatch):
    _install_fake_anthropic_adapter(monkeypatch, create_side_effect=RuntimeError("connection reset"))

    result = probe_model(
        "https://api.anthropic.com", "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE
    )

    assert result.status == "unknown"
    assert result.http_status is None
    assert result.status not in BLOCKING_STATUSES


def test_anthropic_request_carries_max_tokens_one_and_the_model(monkeypatch):
    fake_client, _ = _install_fake_anthropic_adapter(monkeypatch)

    probe_model(
        "https://api.anthropic.com", "sk-ant-test", "claude-opus-5", api_mode=ANTHROPIC_API_MODE
    )

    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 1
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_probe_touches_nothing_but_messages_create(monkeypatch):
    # The doctor check's own mistake (hermes_cli/doctor.py:1756) is to probe
    # GET /v1/models, which is exactly why it never saw the NVIDIA outage this
    # module exists for. Asserting the *entire* call log against the client
    # mock — not just "create was called" — is what would catch a stray
    # client.models.list() the way a spec-less "was create called" check
    # would not.
    fake_client, _ = _install_fake_anthropic_adapter(monkeypatch)

    probe_model("https://api.anthropic.com", "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE)

    assert len(fake_client.mock_calls) == 1
    assert str(fake_client.mock_calls[0]).startswith("call.messages.create(")


def test_anthropic_import_failure_is_skipped_not_raised(monkeypatch):
    _install_fake_anthropic_adapter(monkeypatch, importable=False)

    result = probe_model(
        "https://api.anthropic.com", "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE
    )

    assert result.status == "skipped"
    assert result.status not in BLOCKING_STATUSES
    assert result.http_status is None
    assert ANTHROPIC_API_MODE in result.reason


def test_anthropic_exception_containing_the_api_key_is_redacted(monkeypatch):
    api_key = "sk-ant-oat-super-secret-0123456789"
    _install_fake_anthropic_adapter(
        monkeypatch,
        create_side_effect=_FakeAnthropicStatusError(403, message=f"invalid credential: {api_key}"),
    )

    result = probe_model("https://api.anthropic.com", api_key, "claude-x", api_mode=ANTHROPIC_API_MODE)

    assert api_key not in result.provider_message
    assert api_key not in result.reason
    assert "[REDACTED]" in result.provider_message


def test_anthropic_unsafe_key_is_skipped_and_client_never_built(monkeypatch):
    _, fake_adapter = _install_fake_anthropic_adapter(monkeypatch)

    result = probe_model(
        "https://api.anthropic.com", "sk-ant-trailing\n", "claude-x", api_mode=ANTHROPIC_API_MODE
    )

    assert result.status == "skipped"
    assert result.status not in BLOCKING_STATUSES
    fake_adapter.build_anthropic_client.assert_not_called()


def test_anthropic_empty_api_key_is_skipped_and_client_never_built(monkeypatch):
    _, fake_adapter = _install_fake_anthropic_adapter(monkeypatch)

    result = probe_model("https://api.anthropic.com", "", "claude-x", api_mode=ANTHROPIC_API_MODE)

    assert result.status == "skipped"
    fake_adapter.build_anthropic_client.assert_not_called()


def test_anthropic_empty_base_url_is_not_a_skip(monkeypatch):
    # Unlike chat_completions: the host's client resolves Anthropic's default
    # endpoint itself, so an empty base_url must not by itself refuse the
    # probe a chance to run.
    fake_client, fake_adapter = _install_fake_anthropic_adapter(monkeypatch)

    result = probe_model("", "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE)

    assert result.status == "callable"
    assert fake_adapter.build_anthropic_client.call_args.kwargs["base_url"] is None


def test_anthropic_base_url_and_timeout_are_forwarded_to_the_hosts_client(monkeypatch):
    _, fake_adapter = _install_fake_anthropic_adapter(monkeypatch)

    probe_model(
        "https://custom.anthropic.proxy",
        "sk-ant-test",
        "claude-x",
        api_mode=ANTHROPIC_API_MODE,
        timeout=3.5,
    )

    call = fake_adapter.build_anthropic_client.call_args
    assert call.args[0] == "sk-ant-test"
    assert call.kwargs["base_url"] == "https://custom.anthropic.proxy"
    assert call.kwargs["timeout"] == 3.5


def test_anthropic_request_carries_the_hosts_system_prefix(monkeypatch):
    fake_client, _ = _install_fake_anthropic_adapter(
        monkeypatch, prefix="You are Claude Code, custom host prefix."
    )

    probe_model("https://api.anthropic.com", "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE)

    assert (
        fake_client.messages.create.call_args.kwargs["system"]
        == "You are Claude Code, custom host prefix."
    )


def test_anthropic_falls_back_to_the_literal_system_prefix_when_the_hosts_name_is_gone(monkeypatch):
    # prefix left at _ABSENT: the fake module genuinely has no
    # _CLAUDE_CODE_SYSTEM_PREFIX attribute, exercising the defensive import's
    # except-branch rather than a value substitution.
    fake_client, _ = _install_fake_anthropic_adapter(monkeypatch)

    probe_model("https://api.anthropic.com", "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE)

    assert (
        fake_client.messages.create.call_args.kwargs["system"]
        == "You are Claude Code, Anthropic's official CLI for Claude."
    )


def test_chat_completions_mode_never_touches_the_anthropic_adapter(monkeypatch, server):
    _, fake_adapter = _install_fake_anthropic_adapter(monkeypatch)
    server.respond = lambda path, body: _json_response(200, {})

    _probe_chat(_base_url(server), "sk-test", "some-model")

    fake_adapter.build_anthropic_client.assert_not_called()


def test_anthropic_mode_never_hits_the_chat_completions_server(monkeypatch, server):
    # Belt and braces alongside test_anthropic_probe_touches_nothing_but_messages_create:
    # even with a real HTTP server standing by, the anthropic_messages
    # handler must never make a request to it.
    _install_fake_anthropic_adapter(monkeypatch)
    server.respond = lambda path, body: _json_response(200, {})

    probe_model(_base_url(server), "sk-ant-test", "claude-x", api_mode=ANTHROPIC_API_MODE)

    assert server.requests == []
