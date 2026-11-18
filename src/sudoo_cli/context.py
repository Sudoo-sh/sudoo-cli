"""What every command gets: the parsed options, the client (made on first use) and helpers."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import config, jobs
from .client import Client
from .errors import USAGE, CliError
from .output import emit_json, info


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.json: bool = bool(getattr(args, "json", False))
        self.quiet: bool = bool(getattr(args, "quiet", False))
        self._client: Client | None = None

    @property
    def client(self) -> Client:
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
        """Ask before something that cannot be undone; --yes answers for scripts."""
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
        """A job the panel accepted (202): follow it unless --no-wait, print it, fail when it failed."""
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
