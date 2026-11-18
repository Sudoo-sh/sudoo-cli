"""Signed HTTPS calls to a panel's API (/api/v1), with Python's standard library only."""

from __future__ import annotations

import email.utils
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import IO, Any

from . import __version__
from .config import Profile
from .errors import NETWORK, RATE_LIMITED, USAGE, ApiError, CliError
from .signing import sign

API = "/api/v1"
MAX_RETRY_WAIT = 300.0  # never sleep longer than this for one Retry-After
REDACTED = ("authorization", "x-sudoo-signature", "cookie", "set-cookie")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    _json: Any = field(default=None, repr=False)

    def json(self) -> Any:
        if self._json is None:
            self._json = json.loads(self.body) if self.body else {}
        return self._json

    @property
    def data(self) -> Any:
        payload = self.json()
        return payload.get("data") if isinstance(payload, dict) else payload

    @property
    def meta(self) -> dict[str, Any]:
        payload = self.json()
        meta = payload.get("meta") if isinstance(payload, dict) else None
        return meta if isinstance(meta, dict) else {}


def _loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> str:
    """The panel's address: https, or http only on this machine (never send a token in clear over a network)."""
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise CliError(f"Not a panel address: {url!r}. Give it like https://panel.example.com", USAGE)
    if parsed.scheme == "http" and not _loopback(parsed.hostname):
        raise CliError("The panel's address must use https:// (http:// is accepted only for localhost).", USAGE)
    if parsed.query or parsed.fragment:
        raise CliError("Give the panel's address without ?query or #fragment.", USAGE)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        moment = email.utils.parsedate_to_datetime(value)
        return max(0.0, moment.timestamp() - time.time()) if moment else None


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in REDACTED else v) for k, v in headers.items()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the signature covers the path, and the token must not go to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Client:
    def __init__(
        self,
        profile: Profile,
        *,
        timeout: float = 60.0,
        retries: int = 3,
        debug: bool = False,
        log: IO[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.profile = profile
        self.base = check_url(profile.url)
        self.timeout = timeout
        self.retries = retries
        self.debug = debug
        self.log = log or sys.stderr
        self.sleep = sleep
        context: ssl.SSLContext | None = None
        if self.base.startswith("https://"):
            try:
                context = ssl.create_default_context(cafile=profile.ca_cert) if profile.ca_cert else None
            except (OSError, ssl.SSLError) as error:
                raise CliError(f"Cannot use the CA certificate {profile.ca_cert}: {error}", USAGE) from None
            context = context or ssl.create_default_context()
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect)

    # -- low level -----------------------------------------------------------------------------------------------

    def _debug(self, text: str) -> None:
        if self.debug:
            print(f"debug: {text}", file=self.log)

    def _open(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        extra: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> http.client.HTTPResponse:
        """Send one signed request (retrying 429 after Retry-After); returns the open response."""
        data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        attempt = 0
        while True:
            attempt += 1
            headers = sign(self.profile.token, self.profile.secret, method, path, data, self.profile.acting_for)
            headers["User-Agent"] = f"sudoo-cli/{__version__}"
            headers["Accept"] = "application/json"
            headers["X-Request-ID"] = "cli-" + secrets.token_hex(8)
            if body is not None:
                headers["Content-Type"] = "application/json"
            headers.update(extra or {})
            self._debug(f"> {method} {path} {json.dumps(redact_headers(headers))}")
            # check_url allows https, or http on loopback only: never file: or another scheme.
            request = urllib.request.Request(  # noqa: S310
                self.base + path, data=data if body is not None else None, method=method
            )
            for key, value in headers.items():
                request.add_header(key, value)
            started = time.monotonic()
            try:
                response: http.client.HTTPResponse = self._opener.open(request, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as error:
                payload = error.read()
                self._debug(
                    f"< {error.code} in {time.monotonic() - started:.2f}s, request id {error.headers.get('X-Request-ID')}"
                )
                problem = _problem(payload, error.code, error.reason)
                wait = retry_after_seconds(error.headers.get("Retry-After"))
                if error.code == 429 and attempt <= self.retries:
                    wait = 1.0 if wait is None else wait
                    if wait <= MAX_RETRY_WAIT:
                        print(f"The panel asks to wait {wait:g} s (too many requests); retrying.", file=self.log)
                        self.sleep(wait)
                        continue
                if 300 <= error.code < 400:
                    problem.setdefault(
                        "detail", f"The panel redirected to {error.headers.get('Location')}; use that address."
                    )
                raise ApiError(error.code, problem, wait) from None
            except urllib.error.URLError as error:
                raise _network_error(self.base, error.reason) from None
            except (TimeoutError, ConnectionError, http.client.HTTPException) as error:
                raise _network_error(self.base, error) from None
            self._debug(
                f"< {response.status} in {time.monotonic() - started:.2f}s, request id {response.headers.get('X-Request-ID')}"
            )
            return response

    def request(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Response:
        full = API + path + _query(query)
        with self._open(method, full, body) as response:
            try:
                payload = response.read()
            except (TimeoutError, OSError, http.client.HTTPException) as error:
                raise _network_error(self.base, error) from None
            return Response(response.status, dict(response.headers.items()), payload)

    # -- conveniences --------------------------------------------------------------------------------------------

    def get(self, path: str, **query: Any) -> Response:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: Any = None) -> Response:
        return self.request("POST", path, {} if body is None else body)

    def paged(self, path: str, page: int = 100, **query: Any) -> list[Any]:
        """Every item of a list that has meta.total (follows limit/offset)."""
        found: list[Any] = []
        while True:
            response = self.get(path, limit=page, offset=len(found), **query)
            items = response.data or []
            found += items
            total = response.meta.get("total")
            if not items or total is None or len(found) >= int(total):
                return found

    def download(self, path: str, out: IO[bytes], progress: Callable[[int], None] | None = None) -> int:
        size = 0
        with self._open("GET", API + path, timeout=self.timeout) as response:
            while chunk := response.read(1 << 16):
                out.write(chunk)
                size += len(chunk)
                if progress:
                    progress(size)
        return size

    def open_events(
        self, topics: list[str] | None = None, last_event_id: str | None = None, read_timeout: float = 45.0
    ) -> http.client.HTTPResponse:
        """Open the live event stream (Server-Sent Events); read it with `parse_events`.

        The panel sends a ':' comment every 20 seconds, so a read that waits longer than `read_timeout` means the
        connection is gone (TimeoutError); the caller decides whether to reconnect.
        """
        path = API + "/events" + _query({"topics": ",".join(topics) if topics else None})
        extra = {"Accept": "text/event-stream"}
        if last_event_id:
            extra["Last-Event-ID"] = last_event_id
        return self._open("GET", path, extra=extra, timeout=read_timeout)

    def events(self, topics: list[str] | None = None, last_event_id: str | None = None) -> Iterator[dict[str, Any]]:
        with self.open_events(topics, last_event_id) as response:
            yield from parse_events(response)

    def healthz(self) -> bool:
        """The panel's public health check (no signature)."""
        request = urllib.request.Request(  # noqa: S310  https (http on loopback) only
            self.base + "/healthz", headers={"User-Agent": f"sudoo-cli/{__version__}"}
        )
        try:
            with self._opener.open(request, timeout=min(self.timeout, 15)) as response:
                return 200 <= int(response.status) < 300
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            return False


def parse_events(lines: Iterator[bytes] | IO[bytes]) -> Iterator[dict[str, Any]]:
    """Server-Sent Events as {"id", "event", "key", "data"}; the panel sends `data` as {"key", "data"}."""
    event: dict[str, str] = {}
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if "data" in event:
                try:
                    parsed = json.loads(event["data"])
                except ValueError:
                    parsed = {}
                wrapped = isinstance(parsed, dict)
                yield {
                    "id": event.get("id"),
                    "event": event.get("event", "message"),
                    "key": parsed.get("key") if wrapped else None,
                    "data": parsed.get("data") if wrapped else parsed,
                }
            event = {}
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            event[name] = event[name] + "\n" + value if name == "data" and name in event else value


def _query(query: dict[str, Any] | None) -> str:
    items = {k: v for k, v in (query or {}).items() if v is not None and v != ""}
    for key, value in items.items():
        if isinstance(value, bool):
            items[key] = "1" if value else "0"
    return "?" + urllib.parse.urlencode(items) if items else ""


def _problem(payload: bytes, status: int, reason: Any) -> dict[str, Any]:
    try:
        problem = json.loads(payload)
        if isinstance(problem, dict):
            return problem
    except ValueError:
        pass
    text = payload.decode("utf-8", "replace").strip()
    problem = {"title": str(reason or "Error"), "status": status}
    if text and len(text) < 300 and "<" not in text:
        problem["detail"] = text
    return problem


def _network_error(base: str, reason: Any) -> CliError:
    if isinstance(reason, ssl.SSLCertVerificationError):
        return CliError(
            f"The TLS certificate of {base} could not be verified ({reason.verify_message}). "
            "If the panel uses a private certificate authority, pass its certificate with --ca-cert FILE.",
            NETWORK,
        )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return CliError(f"{base} did not answer in time.", NETWORK)
    return CliError(f"Cannot reach {base}: {reason}", NETWORK)


def quote(part: str, keep_slash: bool = False) -> str:
    """One part of a path (an instance name, a branch that may contain '/', a file name)."""
    return urllib.parse.quote(part, safe="/" if keep_slash else "")


__all__ = ["API", "RATE_LIMITED", "Client", "Response", "check_url", "quote"]
