"""Errors the command reports, and the exit code each one ends with (documented in docs/CLI.md)."""

from __future__ import annotations

from typing import Any

# Exit codes. Scripts can rely on them: they never change meaning within a major version.
OK = 0
ERROR = 1  # anything not listed below
USAGE = 2  # a wrong command line, or a refused confirmation
AUTH = 3  # not logged in, or the panel refused the token or the signature (401)
FORBIDDEN = 4  # the token's scope or role does not allow this (403)
NOT_FOUND = 5  # no such instance, project, job, backup... (404)
CONFLICT = 6  # the panel refused because of the current state (409), for example an instance being created
INVALID = 7  # the panel refused a value (400, 415, 422)
RATE_LIMITED = 8  # still 429 after waiting Retry-After the allowed number of times
SERVER = 9  # the panel failed (5xx)
NETWORK = 10  # the panel could not be reached: DNS, connection, TLS certificate, timeout
JOB_FAILED = 11  # a followed job ended with status "failed"
INTERRUPTED = 130  # Ctrl+C

_BY_STATUS = {
    400: INVALID,
    401: AUTH,
    403: FORBIDDEN,
    404: NOT_FOUND,
    405: USAGE,
    409: CONFLICT,
    410: NOT_FOUND,
    415: INVALID,
    422: INVALID,
    429: RATE_LIMITED,
}


class CliError(Exception):
    """An error with a message for people and an exit code for scripts."""

    def __init__(self, message: str, code: int = ERROR) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def as_json(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "exit_code": self.code}}


class ApiError(CliError):
    """The panel answered with an error: an RFC 9457 problem detail when it sends one."""

    def __init__(self, status: int, problem: dict[str, Any], retry_after: float | None = None) -> None:
        self.status = status
        self.problem = problem
        self.retry_after = retry_after
        title = str(problem.get("title") or "Error")
        detail = str(problem.get("detail") or "").strip()
        message = f"{status} {title}" + (f": {detail}" if detail else "")
        if problem.get("request_id"):
            message += f" (request id {problem['request_id']})"
        code = _BY_STATUS.get(status, SERVER if status >= 500 else ERROR)
        super().__init__(message, code)

    def as_json(self) -> dict[str, Any]:
        result = super().as_json()
        result["error"]["status"] = self.status
        result["error"]["problem"] = self.problem
        return result
