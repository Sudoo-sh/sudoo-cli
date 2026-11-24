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

# The path every API call starts with.
API = "/api/v1"
# The longest wait, in seconds, accepted for one Retry-After answer. A longer request is reported as an error.
MAX_RETRY_WAIT = 300.0  # never sleep longer than this for one Retry-After
# Header names (lower case) whose values are hidden in --debug output.
REDACTED = ("authorization", "x-sudoo-signature", "cookie", "set-cookie")


@dataclass
class Response:
    """An answer from the panel.

    Attributes:
        status: The HTTP status code.
        headers: The response headers.
        body: The raw response body.
    """

    status: int
    headers: dict[str, str]
    body: bytes
    _json: Any = field(default=None, repr=False)

    def json(self) -> Any:
        """The body parsed as JSON (an empty object when the body is empty). The result is kept for the next call.

        Returns:
            The parsed body.

        Raises:
            ValueError: When the body is not valid JSON.
        """
        if self._json is None:
            self._json = json.loads(self.body) if self.body else {}
        return self._json

    @property
    def data(self) -> Any:
        """The "data" part of the answer, or the whole answer when it is not an object.

        Returns:
            The data.
        """
        payload = self.json()
        return payload.get("data") if isinstance(payload, dict) else payload

    @property
    def meta(self) -> dict[str, Any]:
        """The "meta" part of the answer (paging counts and similar).

        Returns:
            The meta object; an empty dict when there is none.
        """
        payload = self.json()
        meta = payload.get("meta") if isinstance(payload, dict) else None
        return meta if isinstance(meta, dict) else {}


def _loopback(host: str) -> bool:
    """Tell whether a host name or address is this machine.

    Args:
        host: A host name or IP address.

    Returns:
        True for localhost, *.localhost and loopback addresses.
    """
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> str:
    """The panel's address: https, or http only on this machine (never send a token in clear over a network).

    Args:
        url: The address as given.

    Returns:
        The address without a trailing slash.

    Raises:
        CliError: With exit code USAGE when it is not an http(s) address, uses http on another host, or has a query
            or fragment.
    """
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise CliError(f"Not a panel address: {url!r}. Give it like https://panel.example.com", USAGE)
    if parsed.scheme == "http" and not _loopback(parsed.hostname):
        raise CliError("The panel's address must use https:// (http:// is accepted only for localhost).", USAGE)
    if parsed.query or parsed.fragment:
        raise CliError("Give the panel's address without ?query or #fragment.", USAGE)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def retry_after_seconds(value: str | None) -> float | None:
    """Read a Retry-After header.

    Args:
        value: The header value: a number of seconds or an HTTP date. None when the header is missing.

    Returns:
        Seconds to wait (never negative), or None when there is no usable value.
    """
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        moment = email.utils.parsedate_to_datetime(value)
        return max(0.0, moment.timestamp() - time.time()) if moment else None


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Hide the secret headers (see `REDACTED`) for debug output.

    Args:
        headers: The request headers.

    Returns:
        A copy with "<redacted>" in place of the secret values.
    """
    return {k: ("<redacted>" if k.lower() in REDACTED else v) for k, v in headers.items()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the signature covers the path, and the token must not go to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        """Refuse every redirect.

        Returns:
            Always None, so the redirect answer is raised as an error.
        """
        return None


class Client:
    """Signed calls to one panel.

    Attributes:
        profile: The profile (address and credentials) in use.
        base: The checked panel address.
        timeout: Seconds to wait for one request.
        retries: How many times a 429 answer is retried.
        debug: When True requests are described on `log`.
        log: Where debug and retry messages go (stderr by default).
        sleep: The function used to wait; tests replace it.
    """

    def __init__(
        self,
        profile: Profile,
        *,
        timeout: float = 60.0,
        retries: int = 3,
        debug: bool = False,
        log: IO[str] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Prepare the client: check the address and set up TLS.

        Args:
            profile: The profile to use.
            timeout: Seconds to wait for one request.
            retries: How many times to retry after a 429 answer.
            debug: When True, describe every request and answer on `log` (secrets hidden).
            log: The stream for messages; stderr when None.
            sleep: A replacement for `time.sleep`, for tests.

        Raises:
            CliError: When the address is not allowed or the CA certificate cannot be used (exit code USAGE).
        """
        self.profile = profile
        self.base = check_url(profile.url)
        self.timeout = timeout
        self.retries = retries
        self.debug = debug
        self.log = log or sys.stderr
        self.sleep = sleep or time.sleep
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
        """Print a debug line when --debug is on.

        Args:
            text: The line, without the "debug:" prefix.
        """
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
        """Send one signed request (retrying 429 after Retry-After); returns the open response.

        Each attempt is signed again, with a new timestamp and nonce.

        Args:
            method: The HTTP method.
            path: The path with the query string, starting with /api/v1.
            body: A value sent as JSON, or None for no body.
            extra: Headers that are added or replace the standard ones.
            timeout: Seconds to wait; the client's timeout when None.

        Returns:
            The open response. The caller reads and closes it.

        Raises:
            ApiError: When the panel answers with an error status.
            CliError: With exit code NETWORK when the panel cannot be reached.

        Side effects:
            Network call. Prints a note to the log and waits when a 429 answer is retried.
        """
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
        """Make a call and read the whole answer.

        Args:
            method: The HTTP method.
            path: The path below /api/v1, for example "/instances".
            body: A value sent as JSON, or None.
            query: Query parameters. None and empty values are left out, booleans become 1 or 0.

        Returns:
            The `Response`.

        Raises:
            ApiError: When the panel answers with an error status.
            CliError: When the panel cannot be reached.
        """
        full = API + path + _query(query)
        with self._open(method, full, body) as response:
            try:
                payload = response.read()
            except (TimeoutError, OSError, http.client.HTTPException) as error:
                raise _network_error(self.base, error) from None
            return Response(response.status, dict(response.headers.items()), payload)

    # -- conveniences --------------------------------------------------------------------------------------------

    def get(self, path: str, **query: Any) -> Response:
        """GET a path with query parameters.

        Args:
            path: The path below /api/v1.
            **query: The query parameters.

        Returns:
            The `Response`.
        """
        return self.request("GET", path, query=query)

    def post(self, path: str, body: Any = None) -> Response:
        """POST a path with a JSON body.

        Args:
            path: The path below /api/v1.
            body: The JSON body; an empty object when None.

        Returns:
            The `Response`.
        """
        return self.request("POST", path, {} if body is None else body)

    def paged(self, path: str, page: int = 100, **query: Any) -> list[Any]:
        """Every item of a list that has meta.total (follows limit/offset).

        Args:
            path: The path of the list.
            page: How many items to ask for at a time.
            **query: More query parameters.

        Returns:
            All the items.
        """
        found: list[Any] = []
        while True:
            response = self.get(path, limit=page, offset=len(found), **query)
            items = response.data or []
            found += items
            total = response.meta.get("total")
            if not items or total is None or len(found) >= int(total):
                return found

    def download(self, path: str, out: IO[bytes], progress: Callable[[int], None] | None = None) -> int:
        """Save the answer of a GET call into a file object, in chunks.

        Args:
            path: The path below /api/v1.
            out: A binary file object to write to.
            progress: Called with the number of bytes saved so far, after each chunk.

        Returns:
            The number of bytes saved.
        """
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

        Args:
            topics: The event topics to subscribe to; all when None.
            last_event_id: The id of the last event seen, to continue after it.
            read_timeout: Seconds a single read may wait.

        Returns:
            The open response, to be used as a context manager.
        """
        path = API + "/events" + _query({"topics": ",".join(topics) if topics else None})
        extra = {"Accept": "text/event-stream"}
        if last_event_id:
            extra["Last-Event-ID"] = last_event_id
        return self._open("GET", path, extra=extra, timeout=read_timeout)

    def events(self, topics: list[str] | None = None, last_event_id: str | None = None) -> Iterator[dict[str, Any]]:
        """Read events from the live stream until it ends.

        Args:
            topics: The event topics to subscribe to; all when None.
            last_event_id: The id of the last event seen, to continue after it.

        Yields:
            Each event, as `parse_events` shows it.
        """
        with self.open_events(topics, last_event_id) as response:
            yield from parse_events(response)

    def healthz(self) -> bool:
        """The panel's public health check (no signature).

        Returns:
            True when /healthz answers with a 2xx status; False on any other answer or when it cannot be reached.
        """
        request = urllib.request.Request(  # noqa: S310  https (http on loopback) only
            self.base + "/healthz", headers={"User-Agent": f"sudoo-cli/{__version__}"}
        )
        try:
            with self._opener.open(request, timeout=min(self.timeout, 15)) as response:
                return 200 <= int(response.status) < 300
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            return False


def parse_events(lines: Iterator[bytes] | IO[bytes]) -> Iterator[dict[str, Any]]:
    """Server-Sent Events as {"id", "event", "key", "data"}; the panel sends `data` as {"key", "data"}.

    Args:
        lines: The stream, as an iterator or file of byte lines.

    Yields:
        One dict per event. "event" is "message" when the stream gives no name. Comment lines (starting with ":") are
        ignored. When the data is not JSON, it is an empty dict.
    """
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
    """Make a query string.

    Args:
        query: The parameters; None and empty values are left out, booleans become "1" or "0".

    Returns:
        The text starting with "?", or an empty text when there is nothing to send.
    """
    items = {k: v for k, v in (query or {}).items() if v is not None and v != ""}
    for key, value in items.items():
        if isinstance(value, bool):
            items[key] = "1" if value else "0"
    return "?" + urllib.parse.urlencode(items) if items else ""


def _problem(payload: bytes, status: int, reason: Any) -> dict[str, Any]:
    """Make a problem detail object from an error answer.

    Args:
        payload: The raw body of the answer.
        status: The HTTP status code.
        reason: The HTTP reason phrase, used as the title when the body is not a JSON object.

    Returns:
        The JSON object when the body is one. Otherwise a small object with a title, the status and, when the body
        is short plain text, a detail.
    """
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
    """Describe a failure to reach the panel.

    Args:
        base: The panel address.
        reason: The exception or text that caused the failure.

    Returns:
        A `CliError` with exit code NETWORK. It has a special message for certificate errors and for timeouts.
    """
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
    """One part of a path (an instance name, a branch that may contain '/', a file name), made safe for a URL.

    Args:
        part: The text to encode.
        keep_slash: When True, "/" is kept as it is (for branch names).

    Returns:
        The encoded text.
    """
    return urllib.parse.quote(part, safe="/" if keep_slash else "")


__all__ = ["API", "RATE_LIMITED", "Client", "Response", "check_url", "quote"]
