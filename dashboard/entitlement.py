"""Prove a model actually answers before the plugin switches to it.

Measured in production on 2026-07-29: NVIDIA's ``/v1/models`` listed
``moonshotai/kimi-k2.6`` for this account, but every ``/chat/completions``
call to it returned ``HTTP 404 — "Function '<function-id>': Not found for
account '<account>'"``. A control model on the same key, same endpoint, same
code returned 200. At NVIDIA, ``/v1/models`` is a catalogue, not an
entitlement list — the listing over-declares what the account can actually
call. The switch landed anyway, the agent was dead, and nothing in the
plugin said why.

This module is the fix: it makes the one call that actually matters — a
one-token completion request on the wire protocol the host actually
resolved — and classifies what comes back. It knows nothing about FastAPI,
the switch-model endpoint, or the host config; it is a pure function of
(base_url, api_key, model, api_mode) to a :class:`ProbeResult`. The caller
decides what to do with that result.

Two wire protocols are covered so far: ``chat_completions`` (the original
fix, an unauthenticated urllib POST) and ``anthropic_messages`` (added
because the same over-declaration risk exists for an Anthropic-transport
provider, and refusing every switch to one — including a revert off a dead
NVIDIA model — is its own outage). See :data:`PROBE_HANDLERS`.

On redaction: everything this module shows the caller is passed through
:func:`_redact_all`, which is **exact substring matching** over three forms
of the key. It catches a provider echoing the key back as-is, and the
repr-escaped form an exception message can carry. It does **not** catch a
key that has been transformed on the way — JSON-escaped, URL-encoded,
base64'd, case-folded, or split across a line break. Do not read a redacted
message as proof no credential is in it; read it as one layer that removes
the forms actually seen in the wild.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
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

#: The host resolves four wire protocols
#: (``hermes_cli/providers.py:385-390`` maps transports to modes:
#: ``openai_chat`` → ``chat_completions``, plus ``anthropic_messages``,
#: ``codex_responses``, ``bedrock_converse``), and every successful switch
#: carries the resolved one on ``ModelSwitchResult.api_mode``
#: (``hermes_cli/model_switch.py:290``, populated via ``determine_api_mode``
#: at ``model_switch.py:1134`` when nothing set it earlier). ``anthropic``
#: resolves to ``anthropic_messages`` (``providers.py:101-104``),
#: ``openai-codex`` to ``codex_responses`` (``providers.py:57-61``), and a
#: ``base_url`` on ``api.openai.com`` to ``codex_responses`` regardless of
#: provider (``providers.py:520-521``). ``nvidia`` is
#: ``transport="openai_chat"`` (``providers.py:175-179``), so the incident
#: this module exists for is covered by the first of the two modes below.
PROBEABLE_API_MODE = "chat_completions"

#: The second wire protocol this module speaks. ``anthropic`` resolves to
#: this mode (``providers.py:101-104``); it is probed through the host's own
#: authenticated client rather than a hand-built request — see
#: :func:`_probe_anthropic_messages` for why.
ANTHROPIC_API_MODE = "anthropic_messages"

#: Dispatch table: wire protocol → the handler that speaks it, assembled
#: further down once both handlers exist (see :data:`PROBE_HANDLERS`). This
#: is an allowlist and must stay one — only an exact key match is probed.
#: Any other mode, and any absent or empty one, means we cannot tell whether
#: the model answers, and "cannot tell" must never block a switch. Adding a
#: third protocol (e.g. ``codex_responses``) is one more table entry, not
#: another branch.


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


def _sanitise_provider_text(text: str, api_key: str) -> str:
    """Redact and truncate a provider's error text for display.

    Shared tail end for both wire protocols: :func:`_sanitise_provider_message`
    (chat_completions, working from a raw response body) and
    :func:`_probe_anthropic_messages` (working from an SDK exception's own
    text) both funnel through here. Classifies nothing — the status mapping
    lives in :func:`_classify_http_status`.

    Redact before truncating, not after. Truncating first would let the
    PROVIDER_MESSAGE_LIMIT cut straddle the key — if the key spans the
    boundary, the truncated text no longer contains the whole key, so
    _redact's exact-string match misses it and a fragment of the real key
    would survive verbatim in the returned message. Redacting the full,
    untruncated text first means the key is gone before truncation can ever
    split it.
    """
    text = _redact_all(text, api_key)
    return text[:PROVIDER_MESSAGE_LIMIT]


def _sanitise_provider_message(raw: bytes, api_key: str) -> str:
    """Decode, redact and truncate a provider's error body for display."""
    message = raw.decode("utf-8", errors="replace").strip()
    return _sanitise_provider_text(message, api_key)


def _classify_http_status(http_status: int) -> tuple[str, str]:
    """Map an HTTP status to this module's own status and a human reason.

    Shared by every wire protocol handler below, so a 404 from
    :func:`_probe_chat_completions` and a 404 from
    :func:`_probe_anthropic_messages` produce the same ``ProbeResult.status``
    — and therefore the same :data:`BLOCKING_STATUSES` behaviour — no matter
    which protocol saw it.

    ``http_status`` is always non-``None`` here: a caller with no HTTP
    status at all (a timeout, a DNS failure, an SDK exception with no
    ``status_code``) is its own "unknown" case, handled by each caller
    before this function is ever reached.
    """
    if http_status == 404:
        return "not_entitled", "Provider returned 404: the account is not entitled to this model."
    if http_status in (401, 403):
        return "credential_rejected", f"Provider rejected the credential (HTTP {http_status})."
    if http_status == 429:
        return "throttled", "Provider throttled the test call (HTTP 429); switch proceeds anyway."
    return "unknown", f"Provider returned an unexpected status (HTTP {http_status})."


def _probe_chat_completions(base_url: str, api_key: str, model: str, timeout: float) -> ProbeResult:
    """Probe the chat_completions wire protocol.

    Calls ``POST {base_url}/chat/completions`` with a one-token completion
    request — never ``GET {base_url}/models``. The listing endpoint is a
    catalogue, not an entitlement check (see module docstring); only a real
    completions call proves the account can actually use the model.

    Never raises. Every failure mode — a bad scheme, an unparseable URL, a
    DNS failure, a timeout, a non-2xx status, a redirect — is caught and
    turned into a :class:`ProbeResult`. (An unusable key and an empty API
    key are screened by :func:`probe_model` before this function is ever
    called; only an empty *base_url* is this function's own concern.)
    """
    if not base_url:
        return ProbeResult(
            status="skipped",
            http_status=None,
            provider_message="",
            reason="Skipped: no base URL configured for this provider.",
        )

    try:
        scheme = urlsplit(base_url).scheme
    except ValueError:
        # urlsplit is not total: it raises ValueError("Invalid IPv6 URL") for
        # e.g. "http://[". This function promises never to raise, and a
        # base_url comes straight off a config file, so the parse itself has
        # to be inside a guard. Unparseable is "we could not tell" — unknown,
        # non-blocking. The URL is not echoed back: it can carry a key in a
        # query string on some providers.
        return ProbeResult(
            status="unknown",
            http_status=None,
            provider_message="",
            reason="Probe could not parse the configured base URL.",
        )

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
    # Only the network call is inside the try. Building the success
    # ProbeResult happens in the else branch below: a bug in that
    # construction is a bug, and must surface as one, not be swallowed by the
    # broad except and misreported as a network `unknown`.
    try:
        with opener.open(request, timeout=timeout) as response:
            http_status = response.getcode()
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        try:
            # Bounded. The per-recv timeout above is not a total-transfer
            # budget, and this runs inside the endpoint that writes the
            # host's config: an unbounded read lets a slow or oversized error
            # body hold that endpoint open for as long as it likes. Eight
            # times the limit we will actually show is generous enough that
            # nothing legitimate is cut in a way that changes the message.
            raw = exc.read(PROVIDER_MESSAGE_LIMIT * 8)
        except Exception:
            raw = b""
        provider_message = _sanitise_provider_message(raw, api_key)
        status, reason = _classify_http_status(http_status)

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
        # newline, and that %r is the credential. _key_is_header_safe (run by
        # probe_model before this function is ever called) should make that
        # unreachable, but this is a credential, so the message is rebuilt
        # from the exception *type* plus urllib's own `reason` — which is the
        # underlying transport error (a gaierror, a ConnectionRefusedError, a
        # timeout) and never sees the headers — and then redacted over every
        # form the key can wear anyway.
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
    else:
        return ProbeResult(
            status="callable",
            http_status=http_status,
            provider_message="",
            reason=f"Provider answered the test call (HTTP {http_status}).",
        )


#: Fallback copy of ``agent/anthropic_adapter.py:374``'s
#: ``_CLAUDE_CODE_SYSTEM_PREFIX``, used only when that private name cannot be
#: imported. Required for an OAuth request to route correctly (see
#: :func:`_probe_anthropic_messages`); kept as a plain literal here so its
#: absence on an older or trimmed host degrades to this string instead of
#: failing the probe.
_FALLBACK_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def _claude_code_system_prefix() -> str:
    """The system prompt prefix an Anthropic OAuth request needs.

    ``_CLAUDE_CODE_SYSTEM_PREFIX`` is a private name in the host
    (``agent/anthropic_adapter.py:374``) and can be renamed or removed
    without notice. Imported defensively: its absence must never fail the
    probe, so a stale copy of the current literal is the fallback, not an
    exception.
    """
    try:
        from agent.anthropic_adapter import _CLAUDE_CODE_SYSTEM_PREFIX as prefix
    except Exception:
        return _FALLBACK_CLAUDE_CODE_SYSTEM_PREFIX
    return prefix or _FALLBACK_CLAUDE_CODE_SYSTEM_PREFIX


def _probe_anthropic_messages(base_url: str, api_key: str, model: str, timeout: float) -> ProbeResult:
    """Probe the anthropic_messages wire protocol.

    Speaks through the host's own ``agent.anthropic_adapter.build_anthropic_client``
    (public: ``agent/anthropic_adapter.py:700``) rather than a hand-built
    request. That function is what detects an OAuth token vs a console API
    key, sets the ``anthropic-beta`` headers OAuth requires, and manages the
    user-agent version Anthropic's OAuth infrastructure validates and
    rejects when stale. Reimplementing any of that here would 401 an OAuth
    subscription token on every call — a *blocking* status — and refuse
    switches that work. If the import fails — an older host, a trimmed
    install, a developer machine where ``agent.*`` is not importable at all
    — this returns ``skipped``, never a hand-built fallback request.

    Each call that reaches the provider consumes one token of subscription
    quota on an OAuth token: unlike a ``GET`` to ``/models``, this is not
    free. That is accepted here because the probe runs once, right before a
    confirmed switch, not on every keystroke.

    An empty *base_url* is **not** a skip on this path, unlike
    :func:`_probe_chat_completions`: the host's client resolves Anthropic's
    default endpoint itself when given ``None``.

    Never raises: every failure — the import, client construction, or the
    call itself — is caught and turned into a :class:`ProbeResult`.
    """
    try:
        from agent.anthropic_adapter import build_anthropic_client
    except Exception:
        return ProbeResult(
            status="skipped",
            http_status=None,
            provider_message="",
            reason=(
                f"Skipped: this provider speaks '{ANTHROPIC_API_MODE}', but "
                "agent.anthropic_adapter is not importable in this process, so the "
                "probe cannot build an authenticated Anthropic client without "
                "reimplementing its OAuth/header handling."
            ),
        )

    try:
        client = build_anthropic_client(api_key, base_url=base_url or None, timeout=timeout)
        client.messages.create(
            model=model,
            max_tokens=1,
            system=_claude_code_system_prefix(),
            messages=[{"role": "user", "content": "hi"}],
        )
    except Exception as exc:
        # Map by HTTP status without importing the SDK: this is exactly what
        # anthropic.APIStatusError exposes, and it is enough to reuse
        # _classify_http_status so both protocols classify identically.
        http_status = getattr(exc, "status_code", None)
        if http_status is None:
            # No classifiable response: a timeout, a DNS failure, a client
            # construction error. Fail-open, same posture as
            # _probe_chat_completions's own network-failure branch — the
            # exception's own text is not interpolated raw, only its type
            # name, since an SDK exception can echo a header value,
            # credential included (see _probe_chat_completions's identical
            # comment for the concrete case this guards against).
            return ProbeResult(
                status="unknown",
                http_status=None,
                provider_message="",
                reason=_redact_all(
                    f"Probe failed before a classifiable response was received: {type(exc).__name__}.",
                    api_key,
                ),
            )
        status, reason = _classify_http_status(http_status)
        provider_message = _sanitise_provider_text(str(exc), api_key)
        return ProbeResult(
            status=status,
            http_status=http_status,
            provider_message=provider_message,
            reason=_redact_all(reason, api_key),
        )
    else:
        return ProbeResult(
            status="callable",
            # The Messages API has exactly one success status; a client call
            # that returns instead of raising was always answered with it.
            http_status=200,
            provider_message="",
            reason="Provider answered the test call.",
        )


PROBE_HANDLERS: dict[str, Callable[[str, str, str, float], ProbeResult]] = {
    PROBEABLE_API_MODE: _probe_chat_completions,
    ANTHROPIC_API_MODE: _probe_anthropic_messages,
}


def probe_model(
    base_url: str,
    api_key: str,
    model: str,
    api_mode: str = "",
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Ask the provider, with a real request, whether *model* actually answers.

    *api_mode* is the wire protocol the host resolved for this switch;
    :data:`PROBE_HANDLERS` maps it to the handler that speaks it. Only modes
    in that table are probed — see its own docstring for why that must stay
    an allowlist. Anything else, including an absent or empty mode, returns
    ``skipped``: a mode this module cannot speak means we cannot tell
    whether the model answers, and "cannot tell" must never block a switch.

    Two preconditions are shared by every protocol before its handler ever
    runs: a non-empty *api_key*, and one that can actually be sent (see
    :func:`_key_is_header_safe`). Everything else — whether an empty
    *base_url* is acceptable, how the request is built, how failures are
    classified — is the chosen handler's own concern, because the two
    protocols disagree on some of it (see :func:`_probe_anthropic_messages`
    on *base_url*).

    Never raises. Every failure mode a handler can hit — an unusable key, a
    bad scheme, an unparseable URL, a DNS failure, a timeout, a non-2xx
    status, a redirect, an unimportable dependency — is caught and turned
    into a :class:`ProbeResult` whose ``status`` is one of: ``callable``,
    ``not_entitled``, ``credential_rejected``, ``throttled``, ``unknown``,
    ``skipped``.
    """
    handler = PROBE_HANDLERS.get(api_mode)
    if handler is None:
        known = ", ".join(f"'{mode}'" for mode in PROBE_HANDLERS)
        return ProbeResult(
            status="skipped",
            http_status=None,
            provider_message="",
            reason=(
                f"Skipped: this provider speaks '{api_mode or '(unknown)'}', which is not "
                f"one of the wire protocols this probe knows how to speak ({known})."
            ),
        )

    if not api_key:
        return ProbeResult(
            status="skipped",
            http_status=None,
            provider_message="",
            reason="Skipped: no API key configured for this provider.",
        )

    if not _key_is_header_safe(api_key):
        # Refuse before the key can reach a transport that would echo it back
        # in an exception message this function would otherwise have to
        # launder (http.client does exactly that for chat_completions; an
        # SDK-built client is not guaranteed to behave any differently).
        # Reported as a real diagnostic, not swallowed: a key with a trailing
        # newline (read from a file, or a Docker secret) fails every call the
        # agent makes, and an operator can chase that for hours without being
        # told.
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

    return handler(base_url, api_key, model, timeout)
