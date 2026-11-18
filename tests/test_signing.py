from __future__ import annotations

import hashlib
import hmac

import pytest

from sudoo_cli.signing import sign, signed_text

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_signed_text_is_five_lines_without_a_final_newline() -> None:
    text = signed_text("1794430123", "p4Xq9mW2abcdefgh", "get", "/api/v1/instances", b"")
    assert text == f"1794430123\np4Xq9mW2abcdefgh\nGET\n/api/v1/instances\n{EMPTY_SHA}".encode()


def test_acting_for_is_the_sixth_line() -> None:
    text = signed_text("1", "n" * 16, "POST", "/api/v1/instances", b"{}", "sara@example.com")
    assert text.split(b"\n")[-1] == b"sara@example.com"
    assert text.split(b"\n")[4] == hashlib.sha256(b"{}").hexdigest().encode()


def test_sign_matches_an_independent_hmac() -> None:
    headers = sign("sudoo_x", "s3cret", "GET", "/api/v1/jobs?limit=5", now=1794430123, nonce="abcdefghijklmnop")
    expected = hmac.new(
        b"s3cret", f"1794430123\nabcdefghijklmnop\nGET\n/api/v1/jobs?limit=5\n{EMPTY_SHA}".encode(), hashlib.sha256
    ).hexdigest()
    assert headers == {
        "Authorization": "Bearer sudoo_x",
        "X-Sudoo-Timestamp": "1794430123",
        "X-Sudoo-Nonce": "abcdefghijklmnop",
        "X-Sudoo-Signature": "sha256=" + expected,
    }


def test_nonces_are_new_each_time_and_valid() -> None:
    first, second = (sign("t", "s", "GET", "/api/v1/x")["X-Sudoo-Nonce"] for _ in range(2))
    assert first != second
    assert 16 <= len(first) <= 64


@pytest.mark.parametrize("bad", ["", "a b", "x" * 121, "semi;colon"])
def test_a_bad_acting_for_is_refused_before_sending(bad: str) -> None:
    with pytest.raises(ValueError):
        sign("t", "s", "GET", "/api/v1/x", acting_for=bad)
