r"""Request signing, exactly as the panel's API describes it (docs/PANEL_API.md, section 2).

The signed text is five lines joined with "\n" (no newline at the end), six with X-Sudoo-Acting-For:

    <timestamp>
    <nonce>
    <METHOD>
    <path, with ?query>
    <hex SHA-256 of the exact body bytes; of an empty body when there is none>
    <X-Sudoo-Acting-For, only when sent>
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

# Pattern of an allowed X-Sudoo-Acting-For value: 1 to 120 letters, digits, ".", "_", "@", "+" or "-".
ACTING_FOR = re.compile(r"[A-Za-z0-9._@+\-]{1,120}")


def new_nonce() -> str:
    """Make a random one-time value for one request.

    Returns:
        32 random letters, digits, "-" or "_" (the panel accepts 16 to 64).
    """
    return secrets.token_urlsafe(24)


def signed_text(
    timestamp: str, nonce: str, method: str, path: str, body: bytes, acting_for: str | None = None
) -> bytes:
    """Build the text that is signed for one request.

    Args:
        timestamp: Unix time in seconds, as text.
        nonce: The one-time value of this request.
        method: The HTTP method; it is upper-cased.
        path: The path with the query string, starting with /api/v1.
        body: The exact request body bytes (empty when there is none).
        acting_for: The X-Sudoo-Acting-For value, or None when the header is not sent.

    Returns:
        The lines joined with newlines, encoded as bytes.
    """
    lines = [timestamp, nonce, method.upper(), path, hashlib.sha256(body).hexdigest()]
    if acting_for is not None:
        lines.append(acting_for)
    return "\n".join(lines).encode()


def signature(secret: str, text: bytes) -> str:
    """Sign a text with the signing secret.

    Args:
        secret: The signing secret of the profile.
        text: The text made by `signed_text`.

    Returns:
        "sha256=" followed by the hex HMAC-SHA-256 of the text.
    """
    return "sha256=" + hmac.new(secret.encode(), text, hashlib.sha256).hexdigest()


def sign(
    token: str,
    secret: str,
    method: str,
    path: str,
    body: bytes = b"",
    acting_for: str | None = None,
    now: float | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """The authentication headers of one request.

    Args:
        token: The API token, sent as a Bearer token.
        secret: The signing secret.
        method: The HTTP method.
        path: The request path. It starts with /api/v1 and includes the query string.
        body: The exact body bytes that will be sent.
        acting_for: Who the call is made for (X-Sudoo-Acting-For), or None.
        now: Unix time to use instead of the clock (for tests).
        nonce: The one-time value to use instead of a random one (for tests).

    Returns:
        The headers Authorization, X-Sudoo-Timestamp, X-Sudoo-Nonce, X-Sudoo-Signature and, when given,
        X-Sudoo-Acting-For.

    Raises:
        ValueError: When `acting_for` does not match `ACTING_FOR`.
    """
    if acting_for is not None and not ACTING_FOR.fullmatch(acting_for):
        raise ValueError("acting-for: 1 to 120 letters, digits, '.', '_', '@', '+' or '-'")
    timestamp = str(int(time.time() if now is None else now))
    nonce = nonce or new_nonce()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Sudoo-Timestamp": timestamp,
        "X-Sudoo-Nonce": nonce,
        "X-Sudoo-Signature": signature(secret, signed_text(timestamp, nonce, method, path, body, acting_for)),
    }
    if acting_for is not None:
        headers["X-Sudoo-Acting-For"] = acting_for
    return headers
