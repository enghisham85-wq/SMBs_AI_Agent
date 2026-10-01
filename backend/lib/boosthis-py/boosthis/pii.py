"""PII denylist + guard — identical to `@boosthis/runtime`'s `assertNoPII`.

The list is the canonical set of field names that must never appear in any
payload Boosthis transmits. ``assert_no_pii(payload)`` raises before any
outbound fetch / send / log if the payload trips the denylist.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

PII_DENYLIST: frozenset[str] = frozenset(
    {
        # identity
        "email", "mail", "username", "userid", "user_id", "uid",
        "firstname", "first_name", "lastname", "last_name",
        "fullname", "full_name", "phone", "mobile", "tel",
        "ssn", "nationalid", "national_id", "taxid", "tax_id",
        "dob", "birthdate", "birthday",
        # account/auth secrets
        "password", "passwd", "secret",
        "apikey", "api_key", "token", "auth", "authorization",
        "session", "cookie", "bearer", "jwt",
        "creditcard", "credit_card", "cardnumber", "card_number", "cvv",
        "iban", "swift", "routingnumber", "routing_number",
        # device-as-identity
        "deviceid", "device_id", "advertisingid", "advertising_id",
        "idfa", "idfv", "macaddress", "mac_address", "imei",
        # network identity
        "ipaddress", "ip_address", "ip", "ipv4", "ipv6",
        "useragent", "user_agent",
        # location
        "latitude", "longitude", "lat", "lng", "lon", "geohash",
        "address", "street", "city", "zipcode", "zip_code",
        "postalcode", "postal_code",
        # free-form content (high-risk for incidental PII)
        "message", "content", "body", "text", "comment", "note",
        "value", "input", "query", "search",
    }
)

# Allowlist of keys that look like denylisted ones but are explicitly safe.
# MUST match @boosthis/runtime's ALLOWLIST verbatim — see tests/test_boosthis.py.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Boosthis's "screen route" is a code-defined string like "team/[id]"
        # — never user content. Distinct from "screenName" of a person.
        "screen", "screenname", "screen_name",
        # boot/version metadata
        "appversion", "app_version",
        "osversion", "os_version",
        "osname", "os_name",
        # performance findings
        "findingid", "finding_id",
        "ruleid", "rule_id",
        # Boosthis's score/rating values
        "score", "rating", "perception",
        # Per-process correlation token used by the production sampler. It
        # is generated as s_<timestamp>_<random> at module load and never
        # contains a user/auth session id. Without this entry the denied
        # fragment session tokenizes out of sessionId and blocks every
        # sample. Other session-prefixed variants like sessionKey or
        # sessionToken remain denied via the tokenizer.
        "sessionid", "session_id",
        # Snapshot axis keys whose names collide with the denied fragment
        # content via the pass-2 substring check (gilContention and
        # lockContention contain it as a substring once normalized). They are
        # code-defined axis identifiers, never user content; their VALUES are
        # still screened by the value-based patterns below. Without these the
        # kit's own pre-transmit guard silently drops the ENTIRE snapshot the
        # moment such a meter leaves warming. Mirrors the server-side
        # exemption in api-server pii.ts; Go twin in boosthis-go/pii.go.
        "gilcontention", "lockcontention",
        # leakWatch (shared additive Leak Watch axis, Aug 2026) emits
        # per-category COUNTERS whose contract-mandated camelCase key names
        # embed denied fragments (secret in secretCount). These are integers —
        # never any matched value — so the field NAMES are allowlisted here.
        # Kept in lock-step across runtimes.
        "secretcount", "stackcount", "piicount",
        # cookieExposure axis KEY collides with the denied cookie
        # fragment; counts-only sub-object, never a cookie name or value.
        "cookieexposure",
        # AI-call visibility (Aug 2026). A provider reports how much of the
        # prompt it read and how much answer it wrote in units it calls TOKENS,
        # and the headroom it publishes is counted in the same units. The field
        # names below therefore collide with the denied `token` fragment while
        # carrying nothing but integers and percentages — never a credential,
        # never a prompt, never an answer. Allowlisted by exact name so the
        # collision cannot widen: any other `*token*` field is still refused.
        "tokensin",
        "tokensout",
        "worsttokenspct",
    }
)

# Value-based PII patterns — catch sensitive data hiding in allowlisted or
# neutral field names (e.g. screen="alice@example.com").
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_FULL_RE = re.compile(r"^[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){7}$", re.IGNORECASE)
_IPV6_HEX_ONLY_RE = re.compile(r"^[0-9a-f:]+$", re.IGNORECASE)


def _looks_like_ipv6(val: str) -> bool:
    """Whole-string IPv6 detection. Returns True only when the value is
    plausibly an IPv6 address — avoids false positives on common route
    labels like ``Module::Class`` or ``A:B:C`` that tripped the previous
    substring regex.
    """
    if len(val) < 3:
        return False
    if _IPV6_FULL_RE.match(val):
        return True
    if "::" not in val:
        return False
    if not _IPV6_HEX_ONLY_RE.match(val):
        return False
    # Only one "::" compression is legal in an IPv6 literal.
    if val.find("::") != val.rfind("::"):
        return False
    groups = [g for g in val.split(":") if g]
    return 1 <= len(groups) <= 7


_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_BEARER_RE = re.compile(r"bearer\s+\S{8,}", re.IGNORECASE)
# Candidate extractor: pull consecutive hex-and-colon runs that are long
# enough to contain an IPv6 address, then validate each with _looks_like_ipv6.
_IPV6_CANDIDATE_RE = re.compile(r"[0-9a-f:]{4,45}", re.IGNORECASE)
# UUID v4/v5 and similar hyphen-grouped hex identifiers. Common structured
# PII carriers in route labels (user ids, order ids, resource ids).
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
# Long unbroken numeric runs (6+ digits) that look like opaque identifiers
# (order numbers, customer refs, ticket ids). Word boundaries prevent false
# positives on short numbers embedded in version strings or timings.
_LONG_NUMERIC_RE = re.compile(r"\b\d{6,}\b")
_PHONE_RE = re.compile(
    # Require at least one separator between the 3-3-4 groups so a bare 10-digit
    # run inside a UUID does NOT match. Real phone numbers in JSON payloads are
    # virtually always written with separators (+1 555-123-4567, (555) 123-4567,
    # 555-123-4567, etc.). Hex-character boundaries on both ends prevent the
    # regex from matching digit windows embedded in longer hex/UUID strings.
    r"(?<![0-9a-fA-F])(?:\+\d{1,3}[\s\-.])?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}(?![0-9a-fA-F])"
)


def _normalize(name: str) -> str:
    """Strip non-alphanumerics, lowercase. Matches JS `normalize()`."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _tokenize(name: str) -> list[str]:
    """Split camelCase/snake_case/kebab-case into lowercase tokens.

    Matches JS `tokenize()` so per-token denylist matching is identical.
    """
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return [t.lower() for t in re.split(r"[^a-zA-Z0-9]+", s) if t]


class PIIDetectedError(ValueError):
    """Raised when a payload contains a denylisted field name or PII value.

    Mirrors the JS runtime's ``PIIDetectedError`` shape: stores ``path``,
    ``field_name``, and ``matched_fragment`` for the offending key.
    """

    def __init__(self, path: str, field_name: str, matched_fragment: str):
        super().__init__(
            f'Boosthis no-PII guard refused outgoing payload: field "{path}" '
            f'(name "{field_name}") matches denied fragment "{matched_fragment}". '
            "Boosthis's privacy contract forbids transmitting personally identifying data. "
            "If this field is genuinely non-PII, rename it; otherwise remove it."
        )
        self.path = path
        self.field_name = field_name
        self.matched_fragment = matched_fragment


def _find_denied_fragment(name: str) -> str | None:
    """Return the matching denylist entry for ``name``, or None if clean.

    Matches the JS algorithm exactly: normalize the full name, allowlist
    short-circuit, then check each denylist entry against (a) the full
    normalized name and (b) each tokenized sub-word.
    """
    normalized = _normalize(name)
    if normalized in _ALLOWLIST:
        return None
    tokens = _tokenize(name)
    # Pass 1: exact normalized match + per-token match.
    for denied in PII_DENYLIST:
        d_norm = _normalize(denied)
        if normalized == d_norm:
            return denied
        for tok in tokens:
            if tok == d_norm:
                return denied
    # Pass 2: concatenated-fragment containment for denylist entries
    # >= 5 chars. Catches "customertoken", "myemail", "sessiontokenv2"
    # where casing/separators give no token boundaries. Length floor
    # avoids false positives on short tokens (ip, uid, dob, cvv, jwt).
    for denied in PII_DENYLIST:
        d_norm = _normalize(denied)
        if len(d_norm) < 5:
            continue
        if d_norm in normalized:
            return denied
    return None


def _contains_embedded_ipv6(val: str) -> bool:
    """Return True if ``val`` contains an IPv6 address as a substring.

    Extracts every consecutive hex-and-colon run long enough to hold an IPv6
    address and validates each candidate with the existing ``_looks_like_ipv6``
    logic, which already guards against false positives on route labels like
    ``Module::Class``.
    """
    for m in _IPV6_CANDIDATE_RE.finditer(val):
        if _looks_like_ipv6(m.group()):
            return True
    return False


def _find_denied_value(val: str) -> str | None:
    """Return a fragment tag if the string value looks like PII, else None.

    Checks email addresses, IP addresses (v4 and v6), JWTs, bearer tokens,
    and phone numbers. Mirrors the JS ``findDeniedValueFragment()`` logic.

    Uses substring search (``re.search``) for JWT and bearer patterns so that
    secrets embedded inside longer strings — e.g.
    ``"Authorization: Bearer abc…"`` or ``"prefix eyJ…token… suffix"`` — are
    caught even when they do not occupy the entire field value.  Similarly,
    ``_contains_embedded_ipv6`` scans for IPv6 addresses that appear as part
    of a longer string like ``"client 2001:db8::1 connected"``.

    NOTE: UUID and long-numeric-ID checks are intentionally NOT included here
    because install IDs are UUID-shaped and would produce false positives.
    Use ``check_route_label`` for route/screen label values instead.
    """
    if len(val) < 5:
        return None
    if _EMAIL_RE.search(val):
        return "~email"
    if _JWT_RE.search(val):
        return "~jwt"
    if _BEARER_RE.search(val):
        return "~bearer"
    if _IPV4_RE.search(val):
        return "~ipv4"
    if _contains_embedded_ipv6(val):
        return "~ipv6"
    if _PHONE_RE.search(val):
        return "~phone"
    return None


_LABEL_SPACE_RE = re.compile(r"\s")

# A label that is a bare ADDRESS rather than one of our own words: a dotted
# hostname ("api.stripe.com") or an IPv4 literal ("10.0.0.5").  The retry-storm
# detector names the host it saw hammered, which is right on the developer's
# own screen inside their own process and wrong on the wire -- our published
# privacy claim lists hostnames among what never reaches us.  Anchored,
# dot-bearing and whitespace-free, so code-defined finding names ("responses",
# "event loop", "GET /pay") are never mistaken for one.
_BARE_ADDRESS_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$",
    re.IGNORECASE,
)


def looks_like_address(label: str) -> bool:
    """True when a label is a bare outbound address -- a hostname or an IPv4.

    Used at the transmit boundary: an address is replaced by the closed
    dependency-kind vocabulary, never uploaded.  Mirrors ``looksLikeAddress``
    in ``lib/boosthis-runtime-node/src/no-pii.ts``.
    """
    return bool(_BARE_ADDRESS_RE.match(label))


def check_route_label(label: str) -> str | None:
    """Return a PII tag if the route/screen label contains a high-risk pattern.

    Applies the general ``_find_denied_value`` checks PLUS UUID, long numeric
    ID, and whitespace detection.  Route/screen labels are code-defined
    identifiers (PascalCase, camelCase, snake_case, slash paths); any label
    containing whitespace is a strong signal of user-derived content (e.g.
    ``"Jane Doe"``, ``"Workspace acme-west"``) and is rejected with
    ``"~space-separated-label"``.  UUID and long-numeric checks are scoped
    here (not in the general guard) to avoid false positives on UUID-shaped
    install IDs.

    Call this on every ``routeLabel`` / screen name before including it in an
    outbound payload or persisting it to the database.

    Returns None when the label is clean; returns a ``~tag`` string
    (e.g. ``"~space-separated-label"``, ``"~uuid"``, ``"~email"``) when PII
    is detected.
    """
    if _LABEL_SPACE_RE.search(label):
        return "~space-separated-label"
    base = _find_denied_value(label)
    if base is not None:
        return base
    if _UUID_RE.search(label):
        return "~uuid"
    if _LONG_NUMERIC_RE.search(label):
        return "~numeric-id"
    return None


# A name a person typed is not a route label, and one rule is the whole
# difference: whitespace.  A route label is code-defined, so a space in one
# means a value got interpolated into a constant.  A JOB name is written by
# hand beside a schedule, so a space in one means it is a name -- "queue
# drain", "Send Weekly Report", "backup db" are entirely ordinary.
#
# Screening job names with the route-label guard made a whole class of
# legitimate job vanish: the run was dropped before upload with no counter and
# no notice, so the job was not late, not never-reported and not on the page.
# The same mistake on app names was measured on the hosted deployment (50 of
# 72 distinct names withheld, every one by the space rule, none by any value
# check).  See docs/job-name-screening.md.
_NAME_CONTROL_CHAR_RE = re.compile(r"[\u0000-\u001f\u007f]")


def check_job_name(name: str) -> str | None:
    """Return a PII tag if a BACKGROUND JOB's name must not be sent.

    Every VALUE check a route label gets still applies -- email, JWT, bearer
    token, IPv4, IPv6, phone, UUID, long numeric identifier -- because a name
    built out of a value ("sync-user-4482113") is user data whatever field it
    arrives in.  What is gone is the space rule, and only that.

    Mirrors ``jobNameHasPII`` in ``lib/boosthis-runtime-node/src/no-pii.ts``
    and ``checkJobName`` in ``artifacts/api-server/src/lib/pii.ts``.  The
    three are held to each other by tests; a name this returns None for must
    be one the server stores, or the kit reports a run into silence.
    """
    if _NAME_CONTROL_CHAR_RE.search(name):
        return "~control-characters"
    base = _find_denied_value(name)
    if base is not None:
        return base
    if _UUID_RE.search(name):
        return "~uuid"
    if _LONG_NUMERIC_RE.search(name):
        return "~numeric-id"
    return None


def check_no_pii(
    payload: Any,
    path: str = "$",
    _seen: set[int] | None = None,
) -> tuple[str, str, str] | None:
    """Walk a payload; return ``(path, field_name, matched_fragment)`` if PII is found.

    Returns ``None`` if the payload is clean. Use ``assert_no_pii()`` if you
    want it to throw instead. Mirrors the JS guard's key-name and value matching;
    traverses to unlimited depth with cycle detection to prevent infinite loops.
    """
    if _seen is None:
        _seen = set()

    if isinstance(payload, str):
        denied = _find_denied_value(payload)
        if denied is not None:
            field_name = path.rsplit(".", 1)[-1] if "." in path else path
            return (path, field_name, denied)
        return None

    if isinstance(payload, Mapping):
        obj_id = id(payload)
        if obj_id in _seen:
            return None
        _seen.add(obj_id)
        for key, val in payload.items():
            key_str = str(key)
            child_path = f"{path}.{key_str}"
            matched = _find_denied_fragment(key_str)
            if matched is not None:
                return (child_path, key_str, matched)
            inner = check_no_pii(val, child_path, _seen)
            if inner:
                return inner
        return None

    if isinstance(payload, (list, tuple)):
        obj_id = id(payload)
        if obj_id in _seen:
            return None
        _seen.add(obj_id)
        for i, item in enumerate(payload):
            inner = check_no_pii(item, f"{path}[{i}]", _seen)
            if inner:
                return inner
        return None

    return None


def assert_no_pii(payload: Any) -> None:
    """Throw ``PIIDetectedError`` if the payload contains any PII.

    Use as the very first line of any outbound transmit::

        from boosthis import assert_no_pii
        assert_no_pii(sample)
        requests.post(url, json=sample)
    """
    hit = check_no_pii(payload)
    if hit:
        path, field_name, matched = hit
        raise PIIDetectedError(path, field_name, matched)
