"""What every command gets: the parsed options, the client (made on first use) and helpers.

The `Context` class is created once per run by `cli.main` and passed to the command handler. It keeps the
command-line options, builds the API client only when a command asks for it, asks for confirmations and follows
jobs.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import config, jobs
from .client import Client
from .errors import USAGE, CliError
from .output import emit_json, info


class Context:
    """The options of this run plus the helpers a command needs.

    Attributes:
        args: The parsed command-line options (argparse namespace).
        json: True when --json was given: commands print JSON instead of text.
        quiet: True when --quiet was given: no progress lines.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        """Keep the options; the client is not made yet.

        Args:
            args: The parsed command-line options.
        """
        self.args = args
        self.json: bool = bool(getattr(args, "json", False))
        self.quiet: bool = bool(getattr(args, "quiet", False))
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        """The API client for the selected profile, made on first use and then reused.

        Returns:
            The `Client`, with the timeout and debug options of this run.

        Raises:
            CliError: When no profile or credentials can be found (exit code AUTH) or an option is not valid.
        """
        if self._client is None:
            profile = config.resolve(
                getattr(self.args, "profile", None),
                ca_cert=getattr(self.args, "ca_cert", None),
                acting_for=getattr(self.args, "acting_for", None),
            )
            self._client = Client(
                profile,
                timeout=float(getattr(self.args, "timeout", 60) or 60),
                debug=bool(getattr(self.args, "debug", False)),
            )
        return self._client

    def confirm(self, question: str, expected: str | None = None) -> None:
        """Ask before something that cannot be undone; --yes answers for scripts.

        Args:
            question: The question shown to the person.
            expected: When given, the person must type exactly this text (for example an instance name). When None, "y"
                or "yes" is enough.

        Raises:
            CliError: With exit code USAGE when the answer is not the expected one, or when there is no terminal and
                --yes was not given.
        """
        if getattr(self.args, "yes", False):
            return
        if not sys.stdin.isatty():
            raise CliError(f"{question} Pass --yes to confirm when not running in a terminal.", USAGE)
        prompt = f"{question} Type {expected!r} to confirm: " if expected else f"{question} [y/N] "
        try:
            answer = input(prompt).strip()
        except EOFError:
            answer = ""
        if (expected is not None and answer != expected) or (expected is None and answer.lower() not in ("y", "yes")):
            raise CliError("Not confirmed; nothing was changed.", USAGE)

    def job(self, started: dict[str, Any]) -> dict[str, Any]:
        """Handle a job the panel accepted (202): follow it unless --no-wait, print it, and fail when it failed.

        Args:
            started: The job object returned by the panel when it accepted the work.

        Returns:
            The job as it was when the function stopped waiting: the last known state, or `started` with --no-wait.

        Raises:
            CliError: When the job failed (exit code JOB_FAILED), or the wait ran out (--wait-timeout).

        Side effects:
            Prints progress to stderr, and the job as JSON to stdout with --json. Calls the panel while following.
        """
        if getattr(self.args, "no_wait", False):
            if self.json:
                emit_json(started)
            else:
                info(f"Started: {started.get('action', 'job')} (job {started.get('id')}). Follow it with:")
                print(f"sudoo jobs follow {started.get('id')}")
            return started
        if not self.quiet and not self.json:
            info(
                f"{started.get('action', 'Job')} on {started.get('instance') or 'the panel'} (job {started.get('id')})"
            )
        final = jobs.follow(
            self.client,
            started,
            quiet=self.quiet,
            live=not getattr(self.args, "poll", False),
            max_wait=getattr(self.args, "wait_timeout", None),
        )
        if self.json:
            emit_json(final)
        jobs.check(final)
        return final
