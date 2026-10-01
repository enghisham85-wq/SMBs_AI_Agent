"""`safe_transmit()` — the single outbound chokepoint.

Refuses to send if the payload, caller-supplied headers, or URL query
parameters trip the PII guard. Keeps stdlib-only so the package has zero
runtime dependencies.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from boosthis.pii import PIIDetectedError, assert_no_pii, check_no_pii
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.kill_switch import is_runtime_inert, _is_runtime_killed_internal

#: What an internal send reports when the kill-switch or the ACTIVATION LOCK
#: stopped it before it reached the network.
#:
#: Deliberately NOT a 2xx. It used to be a synthetic ``204``, which every
#: caller's ``if 200 <= status < 300`` read as "the server took it": the kit
#: recorded an upload it had not made, stamped its "last upload" clock, and
#: dropped the batch. A whole project's measurements disappeared that way while
#: every counter in the process said they had been shipped.
#:
#: Deliberately not a real HTTP status either — no server said this. ``0``
#: reads as falsy, fails every 2xx test, and matches no error branch keyed to a
#: real code.
LOCKED_NOT_SENT = 0


def _assert_no_pii_in_headers(headers: dict[str, str] | None) -> None:
    """Scan caller-supplied header values for PII patterns.

    Header names are not checked against the JSON-payload denylist because
    HTTP header names like "Content-Encoding" would produce false positives.
    Values are checked using the same value-pattern scan applied to JSON
    string fields (email, JWT, bearer, IPv4/v6, phone).
    """
    if not headers:
        return
    for name, value in headers.items():
        hit = check_no_pii(value)
        if hit is not None:
            _path, _field_name, matched_fragment = hit
            raise PIIDetectedError(f"$headers.{name}", name, matched_fragment)


def _assert_no_pii_in_url(url: str) -> None:
    """Scan URL query-parameter names and values for PII.

    Query-parameter names are developer-chosen keys and are checked the same
    way as JSON field names (denylist + tokeniser). Values are checked with
    the value-pattern scan (email, JWT, bearer, IPv4/v6, phone).
    """
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    except Exception:
        # Un-parseable URL — the request itself will fail; don't block here.
        return
    for name, values in params.items():
        # Check param name via the key-name guard (same as JSON field names).
        name_hit = check_no_pii({name: None})
        if name_hit is not None:
            _path, _field_name, matched_fragment = name_hit
            raise PIIDetectedError(f"$url.query.{name}", name, matched_fragment)
        # Check each param value via the value-pattern guard.
        for value in values:
            value_hit = check_no_pii(value)
            if value_hit is not None:
                _path, _field_name, matched_fragment = value_hit
                raise PIIDetectedError(
                    f"$url.query.{name}", name, matched_fragment
                )


def _do_post(
    url: str,
    payload: Any,
    caller_headers: dict[str, str] | None,
    auth_header: str | None,
    timeout: float,
    trusted_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Shared low-level POST helper used by both public and internal paths.

    ``trusted_headers`` (like ``auth_header``) are Boosthis-server-issued
    credentials, not user data, and are added AFTER all PII checks. They must
    only ever be supplied by Boosthis-internal callers.
    """
    data = json.dumps(payload).encode("utf-8")
    req_headers: dict[str, str] = {"Content-Type": "application/json"}
    if caller_headers:
        req_headers.update(caller_headers)
    if auth_header:
        req_headers["Authorization"] = auth_header
    if trusted_headers:
        req_headers.update(trusted_headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body_bytes = resp.read()
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return resp.status, body


def safe_transmit(
    url: str,
    payload: Any,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> int:
    """POST a JSON payload to ``url`` after running the PII guard.

    Returns the HTTP status code on success. Raises ``PIIDetectedError`` if
    the payload, any header value, or any URL query-parameter fails the PII
    guard; raises ``urllib.error.URLError`` on transport failures. Never
    silently swallows errors — let the caller decide.

    Honors ``BOOSTHIS_DISABLED=1`` — when set, returns 204 without touching
    the network. The PII guard still runs first so disabling can never be
    used to weaken the privacy contract.
    """
    status, _body = safe_transmit_with_response(
        url, payload, headers=headers, timeout=timeout
    )
    return status


def safe_transmit_with_response(
    url: str,
    payload: Any,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, Any]]:
    """Same as ``safe_transmit`` but also returns the parsed JSON response body.

    Returns a ``(status, body)`` tuple. ``body`` is an empty dict if the
    response is empty or not JSON. Honors ``BOOSTHIS_DISABLED=1``.
    """
    # PII guard runs unconditionally — disabled mode silences outbound
    # traffic only, it does NOT relax the privacy contract.
    assert_no_pii(payload)
    _assert_no_pii_in_headers(headers)
    _assert_no_pii_in_url(url)
    if is_boosthis_disabled():
        return 204, {}
    return _do_post(url, payload, headers, None, timeout)


def _safe_transmit_internal(
    url: str,
    payload: Any,
    *,
    headers: dict[str, str] | None = None,
    auth_header: str | None = None,
    trusted_headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    allow_when_locked: bool = False,
) -> int:
    """Internal transmit path for Boosthis-runtime use only.

    Identical to ``safe_transmit`` except that ``auth_header`` (the value for
    the ``Authorization`` header, e.g. ``"Bearer <deleteToken>"``) and
    ``trusted_headers`` (e.g. ``{"X-Boosthis-Install-Token": <deleteToken>}``)
    are added to the request AFTER all PII checks. Those values are
    Boosthis-server-issued credentials, not user data, so they are allowed to
    bypass the bearer/JWT/token pattern guard.

    Honors the server-authority KILL-SWITCH: every internal send is gated by
    ``is_runtime_inert()`` so a revoked / unpaid / paused / grace-expired /
    never-activated install stops uploading. ``allow_when_locked`` relaxes the
    ACTIVATION LOCK for the consent/registration path ONLY (it is the step that
    LEADS to activation and would otherwise deadlock every fresh install); a
    KILLED install stays silenced even with that flag. Mirrors Node's
    ``_safeTransmitInternal`` ``allowWhenLocked``.

    Not listed in ``__all__`` and not re-exported from ``boosthis/__init__.py``.
    Host-app code must not call this function directly; it is only accessible
    to Boosthis-internal modules (telemetry.py etc.) within the package.
    """
    status, _body = _safe_transmit_with_response_internal(
        url,
        payload,
        headers=headers,
        auth_header=auth_header,
        trusted_headers=trusted_headers,
        timeout=timeout,
        allow_when_locked=allow_when_locked,
    )
    return status


def _safe_transmit_with_response_internal(
    url: str,
    payload: Any,
    *,
    headers: dict[str, str] | None = None,
    auth_header: str | None = None,
    trusted_headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    allow_when_locked: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Same as ``_safe_transmit_internal`` but returns the parsed response body."""
    assert_no_pii(payload)
    _assert_no_pii_in_headers(headers)
    _assert_no_pii_in_url(url)
    # Kill-switch gate. The consent/registration path passes ``allow_when_locked``
    # (it must run while merely LOCKED so a fresh install can activate); every
    # other internal send is fully gated by ``is_runtime_inert()``. A KILLED
    # install stays silenced in both cases. The env kill-switch always wins.
    if is_boosthis_disabled():
        return 204, {}
    if _is_runtime_killed_internal() if allow_when_locked else is_runtime_inert():
        return LOCKED_NOT_SENT, {}
    return _do_post(url, payload, headers, auth_header, timeout, trusted_headers)
