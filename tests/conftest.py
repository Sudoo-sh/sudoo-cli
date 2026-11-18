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
    code: int
    out: str
    err: str

    def json(self) -> Any:
        return json.loads(self.out)


Run = Callable[..., Result]


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the person's own profiles or keyring, and never sleep for real."""
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
    with MockPanel() as server:
        yield server


@pytest.fixture
def run(panel: MockPanel, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Run:
    """Run `sudoo ...` in-process against the mock panel (credentials from the environment, as in CI)."""
    monkeypatch.setenv("SUDOO_URL", panel.url)
    monkeypatch.setenv("SUDOO_TOKEN", TOKEN)
    monkeypatch.setenv("SUDOO_SECRET", SECRET)

    def invoke(*argv: str) -> Result:
        capsys.readouterr()
        code = main(list(argv))
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return invoke
