"""Shared pytest fixtures: a mock panel, an isolated environment and a helper that runs `sudoo ...` in-process."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sudoo_cli.cli import main

from .mock_panel import SECRET, TOKEN, MockPanel


@dataclass
class Result:
    """What one run of the command printed and returned.

    Attributes:
        code: The exit code.
        out: Everything written to stdout.
        err: Everything written to stderr.
    """

    code: int
    out: str
    err: str

    def json(self) -> Any:
        """Parse stdout as JSON.

        Returns:
            The parsed value.
        """
        return json.loads(self.out)


# Type of the `run` fixture: a function that takes the command-line arguments and returns a `Result`.
Run = Callable[..., Result]


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the person's own profiles or keyring, and never sleep for real.

    Clears the SUDOO_* variables, points SUDOO_CONFIG_DIR to a temporary folder, turns the keyring off and replaces
    time.sleep with a function that does nothing. Applied to every test.
    """
    for name in (
        "SUDOO_URL",
        "SUDOO_TOKEN",
        "SUDOO_SECRET",
        "SUDOO_SIGNING_SECRET",
        "SUDOO_PROFILE",
        "SUDOO_CA_CERT",
        "SUDOO_ACTING_FOR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUDOO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SUDOO_KEYRING", "0")
    monkeypatch.setattr("time.sleep", lambda seconds: None)


@pytest.fixture
def panel() -> Iterator[MockPanel]:
    """Start a mock panel for one test and stop it afterwards."""
    with MockPanel() as server:
        yield server


@pytest.fixture
def run(panel: MockPanel, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Run:
    """Run `sudoo ...` in-process against the mock panel (credentials from the environment, as in CI).

    Returns:
        A function that runs the command with the given arguments and returns its `Result`.
    """
    monkeypatch.setenv("SUDOO_URL", panel.url)
    monkeypatch.setenv("SUDOO_TOKEN", TOKEN)
    monkeypatch.setenv("SUDOO_SECRET", SECRET)

    def invoke(*argv: str) -> Result:
        """Run the command once and collect its output.

        Args:
            *argv: The command-line arguments, without the program name.

        Returns:
            The exit code, stdout and stderr as a `Result`.
        """
        capsys.readouterr()
        code = main(list(argv))
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return invoke
