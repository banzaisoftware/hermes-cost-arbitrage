"""Tests for the entitlement probe.

Driven against a real ``http.server.ThreadingHTTPServer`` bound to
``127.0.0.1:0`` rather than a mocked ``urlopen``. The point of this module is
what goes on the wire — the path, the method, the body, the headers — and a
mock of the transport cannot prove any of that. Every test that cares about
traffic asserts on the server's own recording of what it received.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cost_arbitrage_dashboard.entitlement import (
    BLOCKING_STATUSES,
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
# are mapped at hermes_cli/providers.py:385-390). /chat/completions is a valid
# path for exactly one of them. Probing an anthropic_messages or
# codex_responses provider would collect a 404 or 403 that says nothing about
# entitlement, and — since 404 and 403 are the two blocking statuses — would
# refuse a switch that works. Hence an allowlist: probe the one mode we
# understand, skip everything else, including a mode we have never heard of.


@pytest.mark.parametrize(
    "api_mode",
    ["anthropic_messages", "codex_responses", "bedrock_converse", "", "some_future_mode"],
)
def test_non_chat_completions_api_mode_is_skipped_and_makes_no_request(server, api_mode):
    result = probe_model(_base_url(server), "sk-test", "some-model", api_mode=api_mode)

    assert result.status == "skipped"
    assert result.status not in BLOCKING_STATUSES
    assert result.http_status is None
    # Not merely "did not block": no request was made at all, so the provider
    # never saw a path it does not serve and never saw the credential.
    assert server.requests == []


@pytest.mark.parametrize("api_mode", ["anthropic_messages", "codex_responses", ""])
def test_skipped_api_mode_reason_names_the_mode(server, api_mode):
    result = probe_model(_base_url(server), "sk-test", "some-model", api_mode=api_mode)

    # The operator has to be able to tell *why* their switch was not
    # pre-checked, and the mode is the answer.
    assert (api_mode or "(unknown)") in result.reason


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
    message = "Function 'abc12345-1111': Not found for account 'placeholder-account'"
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
