"""Prove a model actually answers before the plugin switches to it.

Measured in production on 2026-07-29: NVIDIA's ``/v1/models`` listed
``moonshotai/kimi-k2.6`` for this account, but every ``/chat/completions``
call to it returned ``HTTP 404 — "Function 'abc12345-…': Not found for
account '<account>'"``. A control model on the same key, same endpoint, same
code returned 200. At NVIDIA, ``/v1/models`` is a catalogue, not an
entitlement list — the listing over-declares what the account can actually
call. The switch landed anyway, the agent was dead, and nothing in the
plugin said why.

This module is the fix: it makes the one call that actually matters — a
one-token ``/chat/completions`` request — and classifies what comes back.
It knows nothing about FastAPI, the switch-model endpoint, or the host
config; it is a pure function of (base_url, api_key, model) to a
:class:`ProbeResult`. The caller decides what to do with that result.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

#: How long the probe waits for a response before giving up. Kept well under
#: a human's patience for a single button click; a provider that cannot
#: answer in this window is no more entitled than one that answers with a
#: 404 (both fail open here — see BLOCKING_STATUSES).
PROBE_TIMEOUT_SECONDS = 10.0

#: The provider's error body is shown verbatim in the dashboard, but an
#: adversarial or misbehaving provider could return an arbitrarily large
#: body; cap what we carry so a single probe response can't bloat the
#: switch-model response indefinitely.
PROVIDER_MESSAGE_LIMIT = 400

#: The one wire protocol this module knows how to speak. The host resolves
#: four of them: ``hermes_cli/providers.py:385-390`` maps transports to modes
#: (``openai_chat`` → ``chat_completions``, plus ``anthropic_messages``,
#: ``codex_responses``, ``bedrock_converse``), and every successful switch
#: carries the resolved one on ``ModelSwitchResult.api_mode``
#: (``hermes_cli/model_switch.py:290``, populated via ``determine_api_mode``
#: at ``model_switch.py:1134`` when nothing set it earlier). ``anthropic``
#: resolves to ``anthropic_messages`` (``providers.py:101-104``),
#: ``openai-codex`` to ``codex_responses`` (``providers.py:57-61``), and a
#: ``base_url`` on ``api.openai.com`` to ``codex_responses`` regardless of
#: provider (``providers.py:520-521``). ``nvidia`` is
#: ``transport="openai_chat"`` (``providers.py:175-179``), so the incident
#: this module exists for is covered exactly.
#:
#: This is an allowlist and must stay one: only an exact match is probed.
#: Any other mode — and any absent or empty one — means we cannot tell
#: whether the model answers, and "cannot tell" must never block a switch.
PROBEABLE_API_MODE = "chat_completions"

#: The only two statuses that stop a model switch. Every other status this
#: module can produce — throttled, unknown, skipped — is fail-open: the
#: switch proceeds and the caller just gets to see what the probe saw. This
#: module only classifies; it is the caller's job to check membership here
#: and decide policy (see dashboard/plugin_api.py's Task 2 wiring).
BLOCKING_STATUSES = frozenset({"not_entitled", "credential_rejected"})


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of one entitlement probe.

    Always fully populated — there is no "probe didn't run" state distinct
    from ``status == "skipped"``, so a caller never has to guard against a
    partially-filled result.
    """

    status: str
    #: The HTTP status code the provider returned, or ``None`` when no HTTP
    #: response was ever obtained (a timeout, a DNS failure, a skip, ...).
    http_status: int | None
    #: The provider's own error text: verbatim, with the API key redacted if
    #: it appears, then truncated to PROVIDER_MESSAGE_LIMIT — redaction runs
    #: first so a key that would straddle the truncation boundary can't
    #: survive as a fragment. "" when the provider gave us nothing to show
    #: (a clean 2xx, a network failure with no body, a skip).
    provider_message: str
    #: Our own one-line, operator-readable explanation of what happened —
    #: shown in the dashboard to someone deciding whether to trust the
    #: switch. Always set, regardless of status.
    reason: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow any redirect.

    A 3xx response would otherwise cause urllib to replay the
    ``Authorization`` header at whatever host the ``Location`` header names
    — an endpoint the operator never configured and never approved sending
    their credential to. Raising here turns that into an ordinary
    :class:`urllib.error.HTTPError` for the 3xx status, which the caller
    classifies as ``unknown`` (non-blocking) rather than blindly chasing it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 (stdlib override)
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


def _redact(text: str, api_key: str) -> str:
    """Remove every occurrence of *api_key* from *text*.

    Guarded on a non-empty key: ``str.replace(text, "", ...)`` would insert
    the replacement between every character of *text*, corrupting any
    string whenever the key happens to be empty (which is itself the
    ``skipped`` path and never reaches this function today, but a future
    caller with an empty key must not get silently mangled output).
    """
    if not api_key:
        return text
    return text.replace(api_key, "[REDACTED]")


def _redact_all(text: str, api_key: str) -> str:
    """Redact *api_key* and the forms it can wear in an exception message.

    Three forms, because a key does not always appear as itself:

    - the key verbatim;
    - ``api_key.strip()``, since a key read from a file or a Docker secret
      carries a trailing newline that the surrounding text often drops;
    - ``repr(api_key)[1:-1]``, the repr-escaped form. This is the one that
      matters. ``http.client.putheader`` raises
      ``ValueError('Invalid header value %r' % value)`` — it echoes the
      header value, credential included — and ``%r`` turns a real newline
      into a two-character ``\\n``, so the verbatim form no longer matches
      and an exact-substring redact would leave the whole usable portion of
      the key in place.
    """
    for form in (api_key, api_key.strip(), repr(api_key)[1:-1]):
        text = _redact(text, form)
    return text


def _key_is_header_safe(api_key: str) -> bool:
    """Can *api_key* be sent as an ``Authorization`` header value at all?

    A key with a trailing newline is the realistic case — one read from a
    file or a Docker secret. ``http.client`` refuses to send it, and the
    ValueError it raises carries the key. Checking here means the operator
    gets a real diagnostic instead of a mystery failure, and the credential
    never reaches the code path that would echo it.

    Kept a separate function so a test can neutralise it and exercise the
    redaction behind it; the two are independent layers, not one.
    """
    if api_key != api_key.strip():
        return False
    return not any(char in api_key for char in ("\r", "\n", "\0"))


def _classify_provider_message(raw: bytes, api_key: str) -> str:
    # Redact before truncating, not after. Truncating first would let the
    # PROVIDER_MESSAGE_LIMIT cut straddle the key — if the key spans the
    # boundary, the truncated text no longer contains the whole key, so
    # _redact's exact-string match misses it and a fragment of the real key
    # would survive verbatim in the returned message. Redacting the full,
    # untruncated text first means the key is gone before truncation can
    # ever split it.
    message = raw.decode("utf-8", errors="replace").strip()
    message = _redact_all(message, api_key)
    return message[:PROVIDER_MESSAGE_LIMIT]


def probe_model(
    base_url: str,
    api_key: str,
    model: str,
    api_mode: str = "",
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Ask the provider, with a real request, whether *model* actually answers.

    Calls ``POST {base_url}/chat/completions`` with a one-token completion
    request — never ``GET {base_url}/models``. The listing endpoint is a
    catalogue, not an entitlement check (see module docstring); only a real
    completions call proves the account can actually use the model.

    *api_mode* is the wire protocol the host resolved for this switch. Only
    ``chat_completions`` is probed; see :data:`PROBEABLE_API_MODE` for why
    that is an allowlist. Anything else returns ``skipped`` — an Anthropic-
    or Codex-transport provider does not serve ``/chat/completions`` at all,
    so probing one would collect a 404 that says nothing about entitlement
    and would refuse a switch that works. Defaulting to ``""`` means a caller
    that forgets to pass it skips rather than probes blindly.

    Never raises. Every failure mode — an unusable key, a bad scheme, an
    unparseable URL, a DNS failure, a timeout, a non-2xx status, a redirect
    — is caught and turned into a :class:`ProbeResult` whose ``status`` is
    one of: ``callable``, ``not_entitled``, ``credential_rejected``,
    ``throttled``, ``unknown``, ``skipped``.
    """
    if api_mode != PROBEABLE_API_MODE:
        return ProbeResult(
            status="skipped",
            http_status=None,
            provider_message="",
            reason=(
                f"Skipped: this provider speaks '{api_mode or '(unknown)'}', not "
                f"'{PROBEABLE_API_MODE}', and the probe only knows how to call "
                "/chat/completions."
            ),
        )

    if not base_url or not api_key:
        return ProbeResult(
            status="skipped",
            http_status=None,
            provider_message="",
            reason="Skipped: no base URL or no API key configured for this provider.",
        )

    if not _key_is_header_safe(api_key):
        # Refuse before the key can reach http.client, which raises
        # ValueError('Invalid header value %r' % value) on a key carrying a
        # newline — echoing the credential into an exception message this
        # function would otherwise have to launder. Reported as a real
        # diagnostic, not swallowed: a key with a trailing newline (read from
        # a file, or a Docker secret) fails every call the agent makes, and
        # an operator can chase that for hours without being told.
        return ProbeResult(
            status="skipped",
            http_status=None,
            provider_message="",
            reason=(
                "Skipped: the configured API key has surrounding whitespace or contains "
                "characters that cannot be sent in an HTTP header (a stray newline is the "
                "usual cause — check for a trailing newline in the key file or secret)."
            ),
        )

    scheme = urlsplit(base_url).scheme
    if scheme not in ("http", "https"):
        # Reject before opening anything: urlopen honours file:// (and other
        # schemes urllib supports), and a base_url read back from a config
        # file must never be able to make this function read the local
        # filesystem or reach an unintended handler.
        return ProbeResult(
            status="skipped",
            http_status=None,
            provider_message="",
            reason=f"Skipped: base URL scheme '{scheme or '(none)'}' is not http or https.",
        )

    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            http_status = response.getcode()
            return ProbeResult(
                status="callable",
                http_status=http_status,
                provider_message="",
                reason=f"Provider answered the test call (HTTP {http_status}).",
            )
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        provider_message = _classify_provider_message(raw, api_key)

        if http_status == 404:
            status = "not_entitled"
            reason = "Provider returned 404: the account is not entitled to this model."
        elif http_status in (401, 403):
            status = "credential_rejected"
            reason = f"Provider rejected the credential (HTTP {http_status})."
        elif http_status == 429:
            status = "throttled"
            reason = "Provider throttled the test call (HTTP 429); switch proceeds anyway."
        else:
            status = "unknown"
            reason = f"Provider returned an unexpected status (HTTP {http_status})."

        return ProbeResult(
            status=status,
            http_status=http_status,
            provider_message=provider_message,
            # reason is a static template with no provider-controlled text
            # today, but it is redacted anyway: the constraint is "the key
            # never leaves the process", not "the key never leaves the
            # process except when we're confident it's not there".
            reason=_redact_all(reason, api_key),
        )
    except Exception as exc:
        # Timeouts, DNS failures, connection resets, and anything else that
        # never produced an HTTP response all land here. Fail-open: the
        # switch is not blocked by a network problem, only by a provider
        # that actually answered "no".
        #
        # str(exc) is deliberately NOT interpolated. Exception messages down
        # here are not all transport-level: http.client.putheader raises
        # ValueError('Invalid header value %r' % value) for a key carrying a
        # newline, and that %r is the credential. _key_is_header_safe above
        # should make that unreachable, but this is a credential, so the
        # message is rebuilt from the exception *type* plus urllib's own
        # `reason` — which is the underlying transport error (a gaierror, a
        # ConnectionRefusedError, a timeout) and never sees the headers —
        # and then redacted over every form the key can wear anyway.
        detail = type(exc).__name__
        transport_reason = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else None
        if transport_reason is not None:
            detail = f"{detail} ({transport_reason})"
        return ProbeResult(
            status="unknown",
            http_status=None,
            provider_message="",
            reason=_redact_all(f"Probe failed before a response was received: {detail}", api_key),
        )
