"""URL sanitisation: never persist credentials or signed-URL secrets.

Applied before URLs are written to the database, snapshots/exports, reports,
and the browser queue. Mirrors the paper-lib rule set (sensitive query keys are
dropped; a signed redirect wrapper is replaced by its https ``redirect_uri``)
and extends it to common cloud signing parameters and URL userinfo.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

SENSITIVE_QUERY_KEYS = frozenset({
    # paper-lib set
    "access_token", "api_key", "casa_token", "key", "signature", "token",
    "x-amz-credential", "x-amz-signature", "x-amz-security-token",
    # common extensions
    "apikey", "api-key", "auth", "auth_token", "authorization", "client_secret", "credential",
    "expires", "id_token", "jsessionid", "key-pair-id", "password", "passwd", "policy", "refresh_token",
    "secret", "session", "sessionid", "session_id", "session_token", "sig", "ticket",
    "x-goog-credential", "x-goog-signature", "x-goog-security-token",
})
SENSITIVE_PREFIXES = ("x-amz-", "x-goog-")


def is_sensitive(key: str) -> bool:
    folded = key.casefold()
    return folded in SENSITIVE_QUERY_KEYS or folded.startswith(SENSITIVE_PREFIXES)


def sanitize_url(value: Any) -> Any:
    if not isinstance(value, str) or not re.match(r"https?://", value, re.I):
        return value
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    netloc = parsed.netloc.rsplit("@", 1)[-1] if "@" in parsed.netloc else parsed.netloc
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(is_sensitive(key) for key, _ in query):
        return urllib.parse.urlunsplit(parsed._replace(netloc=netloc)) if netloc != parsed.netloc else value
    redirect = next((item for key, item in query if key.casefold() == "redirect_uri"), "")
    if redirect.startswith("https://"):
        return sanitize_url(redirect)
    safe = [(key, item) for key, item in query if not is_sensitive(key)]
    return urllib.parse.urlunsplit(parsed._replace(netloc=netloc, query=urllib.parse.urlencode(safe)))


def sanitize_text(value: Any) -> Any:
    """Sanitise every http(s) URL embedded in a string (e.g. ``"a | b"`` or ``"url#member"``)."""
    if not isinstance(value, str) or "http" not in value:
        return value
    return re.sub(r"https?://[^\s|,;\"'<>]+", lambda m: sanitize_url(m.group(0)), value)


def sanitize_obj(value: Any) -> Any:
    """Recursively sanitise URLs in report/export structures."""
    if isinstance(value, dict):
        return {key: sanitize_obj(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_obj(item) for item in value]
    return sanitize_text(value)
