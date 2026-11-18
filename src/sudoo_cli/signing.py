"""Request signing, exactly as the panel's API describes it (docs/PANEL_API.md, section 2).

The signed text is five lines joined with "\\n" (no newline at the end), six with X-Sudoo-Acting-For:

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

ACTING_FOR = re.compile(r"[A-Za-z0-9._@+\-]{1,120}")


def new_nonce() -> str:
    """32 random letters, digits, '-' or '_' (the panel accepts 16 to 64)."""
    return secrets.token_urlsafe(24)


def signed_text(
    timestamp: str, nonce: str, method: str, path: str, body: bytes, acting_for: str | None = None
) -> bytes:
    lines = [timestamp, nonce, method.upper(), path, hashlib.sha256(body).hexdigest()]
    if acting_for is not None:
        lines.append(acting_for)
    return "\n".join(lines).encode()


def signature(secret: str, text: bytes) -> str:
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
    """The authentication headers of one request. `path` starts with /api/v1 and includes the query string."""
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
