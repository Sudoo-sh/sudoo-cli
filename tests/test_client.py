"""Authentication, signing against the mock panel, errors, 429, TLS, job following and secret hygiene."""

from __future__ import annotations

import io
import json
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

from sudoo_cli import errors
from sudoo_cli.client import check_url, parse_events, redact_headers, retry_after_seconds
from sudoo_cli.errors import CliError

from .conftest import Run
from .mock_panel import READ_TOKEN, SECRET, TOKEN, MockPanel


def _job_reads(panel: MockPanel) -> int:
    """Count the requests that read a job (GET /api/v1/jobs/<id>).

    Args:
        panel: The mock panel.

    Returns:
        The number of such requests so far.
    """
    return sum(1 for r in panel.state.requests if r["method"] == "GET" and r["path"].startswith("/api/v1/jobs/"))


def test_every_request_is_signed_and_accepted(run: Run, panel: MockPanel) -> None:
    """Test: every request is signed and accepted."""
    assert run("instances", "list").code == 0
    request = panel.state.requests[-1]
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["X-Sudoo-Signature"].startswith("sha256=")
    assert request["headers"]["User-Agent"].startswith("sudoo-cli/")


def test_acting_for_is_sent_and_signed(run: Run, panel: MockPanel) -> None:
    """Test: acting for is sent and signed."""
    assert run("instances", "list", "--acting-for", "sara@example.com").code == 0
    assert panel.state.requests[-1]["headers"]["X-Sudoo-Acting-For"] == "sara@example.com"
    assert run("instances", "list", "--acting-for", "not valid").code == errors.USAGE


def test_a_wrong_secret_is_refused(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test: a wrong secret is refused."""
    monkeypatch.setenv("SUDOO_SECRET", "wrong")
    result = run("instances", "list")
    assert result.code == errors.AUTH
    assert "signature does not match" in result.err


def test_a_read_token_cannot_change_anything(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test: a read token cannot change anything."""
    monkeypatch.setenv("SUDOO_TOKEN", READ_TOKEN)
    assert run("instances", "list").code == 0
    result = run("instances", "restart", "sales-dev", "--json")
    assert result.code == errors.FORBIDDEN
    assert result.json()["error"]["problem"]["detail"] == "This token may only read."


def test_429_waits_and_retries_with_a_new_nonce(run: Run, panel: MockPanel) -> None:
    """Test: 429 waits and retries with a new nonce."""
    panel.state.rate_limit_next = 2
    result = run("instances", "list")
    assert result.code == 0
    assert result.err.count("too many requests") == 2
    nonces = [r["headers"]["X-Sudoo-Nonce"] for r in panel.state.requests]
    assert len(set(nonces)) == len(nonces) == 3


def test_429_gives_up_after_the_retries(run: Run, panel: MockPanel) -> None:
    """Test: 429 gives up after the retries."""
    panel.state.rate_limit_next = 10
    assert run("instances", "list").code == errors.RATE_LIMITED


def test_jobs_are_followed_live_through_the_event_stream(run: Run, panel: MockPanel) -> None:
    """Test: jobs are followed live through the event stream."""
    result = run("instances", "restart", "sales-dev")
    assert result.code == 0
    assert any(r["path"] == "/api/v1/events?topics=job" for r in panel.state.requests)
    assert _job_reads(panel) == 1  # once after opening the stream; the rest came as events
    assert "[ 50%] Restart (running)" in result.err
    assert "finished" in result.err


def test_jobs_fall_back_to_polling(run: Run, panel: MockPanel) -> None:
    """Test: jobs fall back to polling."""
    panel.state.events_status = 403  # an `instances` token may not open the stream
    assert run("instances", "restart", "sales-dev").code == 0
    assert _job_reads(panel) >= 2
    panel.state.events_status = None
    before = _job_reads(panel)
    assert run("instances", "restart", "sales-dev", "--poll").code == 0
    assert not any(r["path"].startswith("/api/v1/events") for r in panel.state.requests[-3:])
    assert _job_reads(panel) > before


def test_a_failed_job_exits_11_with_its_error(run: Run, panel: MockPanel) -> None:
    """Test: a failed job exits 11 with its error."""
    panel.state.fail_jobs = True
    result = run("deploy", "sales-dev")
    assert result.code == errors.JOB_FAILED
    assert "Odoo did not start." in result.err
    as_json = run("deploy", "sales-dev", "--json", "--poll")
    assert as_json.code == errors.JOB_FAILED


def test_debug_never_shows_secrets(run: Run) -> None:
    """Test: debug never shows secrets."""
    result = run("instances", "list", "--debug")
    assert "debug: > GET /api/v1/instances" in result.err
    assert TOKEN not in result.err and SECRET not in result.err
    assert "<redacted>" in result.err


def test_redact_headers() -> None:
    """Test: redact headers."""
    assert redact_headers({"Authorization": "Bearer x", "X-Sudoo-Signature": "sha256=y", "X-Sudoo-Nonce": "n"}) == {
        "Authorization": "<redacted>",
        "X-Sudoo-Signature": "<redacted>",
        "X-Sudoo-Nonce": "n",
    }


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://panel.example.com/", True),
        ("http://127.0.0.1:9000", True),
        ("http://localhost:8000", True),
        ("http://panel.example.com", False),
        ("ftp://panel.example.com", False),
        ("https://panel.example.com/?x=1", False),
        ("panel.example.com", False),
    ],
)
def test_check_url(url: str, ok: bool) -> None:
    """Test: check url."""
    if ok:
        assert check_url(url) == url.rstrip("/")
    else:
        with pytest.raises(CliError):
            check_url(url)


def test_retry_after() -> None:
    """Test: retry after."""
    assert retry_after_seconds("7") == 7
    assert retry_after_seconds(None) is None
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0


def test_parse_events() -> None:
    """Test: parse events."""
    stream = io.BytesIO(
        b': keep-alive\n\nid: 4\nevent: job\ndata: {"key": "a1", "data": {"id": "a1"}}\n\nevent: reset\ndata: {}\n\n'
    )
    events = list(parse_events(stream))
    assert events[0] == {"id": "4", "event": "job", "key": "a1", "data": {"id": "a1"}}
    assert events[1]["event"] == "reset"


def test_network_errors_exit_10(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test: network errors exit 10."""
    monkeypatch.setenv("SUDOO_URL", "http://127.0.0.1:9")
    result = run("instances", "list")
    assert result.code == errors.NETWORK
    assert "Cannot reach" in result.err


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl to make test certificates")
def test_tls_is_verified_and_a_custom_ca_can_be_trusted(
    run: Run, panel: MockPanel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test: tls is verified and a custom ca can be trusted."""
    names = ("ca.key", "ca.pem", "ca.cnf", "s.key", "s.csr", "s.pem", "s.cnf")
    ca_key, ca, ca_cnf, key, csr, cert, leaf_cnf = (tmp_path / n for n in names)
    # Certificates as Python 3.13+ (VERIFY_X509_STRICT) requires them, independent of the system's openssl.cnf.
    ca_cnf.write_text(
        "[req]\ndistinguished_name = dn\nprompt = no\nx509_extensions = v3_ca\n"
        "[dn]\nCN = Test CA\n"
        "[v3_ca]\nbasicConstraints = critical, CA:TRUE\nkeyUsage = critical, keyCertSign, cRLSign\n"
        "subjectKeyIdentifier = hash\n"
    )
    leaf_cnf.write_text(
        "[leaf]\nbasicConstraints = critical, CA:FALSE\nkeyUsage = critical, digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\nsubjectAltName = DNS:localhost\n"
        "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid:always\n"
    )
    for command in (
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            ca_key,
            "-out",
            ca,
            "-days",
            "2",
            "-config",
            ca_cnf,
        ],
        ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr, "-subj", "/CN=localhost"],
        [
            "x509",
            "-req",
            "-in",
            csr,
            "-CA",
            ca,
            "-CAkey",
            ca_key,
            "-CAcreateserial",
            "-out",
            cert,
            "-days",
            "2",
            "-extfile",
            leaf_cnf,
            "-extensions",
            "leaf",
        ],
    ):
        subprocess.run(["openssl", *map(str, command)], check=True, capture_output=True)  # noqa: S603, S607
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)
    with MockPanel() as secure:
        secure.socket = context.wrap_socket(secure.socket, server_side=True)
        monkeypatch.setenv("SUDOO_URL", f"https://localhost:{secure.server_address[1]}")
        refused = run("instances", "list")
        assert refused.code == errors.NETWORK
        assert "--ca-cert" in refused.err
        trusted = run("instances", "list", "--ca-cert", str(ca), "--json")
        assert trusted.code == 0, trusted.err
        assert len(trusted.json()) == 3
    assert run("instances", "list", "--ca-cert", str(tmp_path / "missing.pem")).code == errors.USAGE


def test_json_errors_are_machine_readable(run: Run) -> None:
    """Test: json errors are machine readable."""
    result = run("instances", "show", "ghost", "--json")
    payload = json.loads(result.out)
    assert payload["error"]["status"] == 404
    assert payload["error"]["exit_code"] == errors.NOT_FOUND
