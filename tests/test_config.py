"""Login, profiles, the credentials file and keyring, and environment variables."""

from __future__ import annotations

import os
import stat
import sys
import types
from pathlib import Path

import pytest

from sudoo_cli import config, errors
from sudoo_cli.cli import main

from .conftest import Run
from .mock_panel import SECRET, TOKEN, MockPanel


def _login(panel: MockPanel, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    """Run `sudoo login --url <panel>` with the test token and secret in the environment.

    Args:
        panel: The mock panel to log in to.
        monkeypatch: Used to set and remove the environment variables.
        *args: More command-line arguments.

    Returns:
        The exit code of the command.
    """
    monkeypatch.setenv("SUDOO_TOKEN", TOKEN)
    monkeypatch.setenv("SUDOO_SECRET", SECRET)
    code = main(["login", "--url", panel.url, *args])
    monkeypatch.delenv("SUDOO_TOKEN")
    monkeypatch.delenv("SUDOO_SECRET")
    return code


def test_login_keeps_secrets_in_a_private_file(
    panel: MockPanel, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test: login keeps secrets in a private file."""
    assert _login(panel, monkeypatch) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out and SECRET not in out
    folder = config.config_dir()
    credentials = folder / "credentials.json"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert TOKEN not in (folder / "config.json").read_text()
    assert main(["instances", "list"]) == 0  # the default profile, no environment needed


def test_login_checks_the_credentials(panel: MockPanel, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test: login checks the credentials."""
    monkeypatch.setenv("SUDOO_TOKEN", TOKEN)
    monkeypatch.setenv("SUDOO_SECRET", "wrong")
    assert main(["login", "--url", panel.url]) == errors.AUTH
    assert not (config.config_dir() / "credentials.json").exists()


def test_login_refuses_plain_http_to_another_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test: login refuses plain http to another host."""
    monkeypatch.setenv("SUDOO_TOKEN", TOKEN)
    monkeypatch.setenv("SUDOO_SECRET", SECRET)
    assert main(["login", "--url", "http://panel.example.com", "--no-verify"]) == errors.USAGE


def test_profiles_and_logout(
    panel: MockPanel, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test: profiles and logout."""
    assert _login(panel, monkeypatch, "--profile", "staging") == 0
    assert _login(panel, monkeypatch, "--profile", "prod", "--default") == 0
    capsys.readouterr()
    assert main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "staging" in out and "prod" in out
    assert config.load_config()["default_profile"] == "prod"
    assert main(["--profile", "staging", "instances", "list"]) == 0
    monkeypatch.setenv("SUDOO_PROFILE", "staging")
    assert main(["instances", "list"]) == 0
    assert main(["logout", "--profile", "staging"]) == 0
    assert main(["instances", "list"]) == errors.AUTH
    assert main(["login", "--profile", "bad name!", "--url", panel.url, "--no-verify"]) in (errors.USAGE,)


def test_environment_overrides_a_profile(panel: MockPanel, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test: environment overrides a profile."""
    assert _login(panel, monkeypatch) == 0
    monkeypatch.setenv("SUDOO_URL", "http://127.0.0.1:9")
    assert main(["instances", "list"]) == errors.NETWORK
    monkeypatch.delenv("SUDOO_URL")
    monkeypatch.setenv("SUDOO_SIGNING_SECRET", "wrong")
    assert main(["instances", "list"]) == errors.AUTH


def test_not_logged_in(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Test: not logged in."""
    assert main(["instances", "list"]) == errors.AUTH
    assert "sudoo login" in capsys.readouterr().err


def test_the_keyring_is_used_when_available(panel: MockPanel, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test: the keyring is used when available."""
    store: dict[tuple[str, str], str] = {}

    class Backend:
        """A stand-in keyring backend with a positive priority, so that it counts as usable."""

        priority = 5

    fake = types.ModuleType("keyring")
    fake.get_keyring = Backend  # type: ignore[attr-defined]
    fake.set_password = lambda s, u, p: store.__setitem__((s, u), p)  # type: ignore[attr-defined]
    fake.get_password = lambda s, u: store.get((s, u))  # type: ignore[attr-defined]
    fake.delete_password = lambda s, u: store.pop((s, u))  # type: ignore[attr-defined]
    backends = types.ModuleType("keyring.backends")
    fail = types.ModuleType("keyring.backends.fail")
    fail.Keyring = type("Keyring", (), {})  # type: ignore[attr-defined]
    backends.fail = fail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setitem(sys.modules, "keyring.backends", backends)
    monkeypatch.setitem(sys.modules, "keyring.backends.fail", fail)
    monkeypatch.delenv("SUDOO_KEYRING")
    assert _login(panel, monkeypatch) == 0
    assert store[("sudoo-cli", "default:token")] == TOKEN
    assert not (config.config_dir() / "credentials.json").exists()
    assert config.load_config()["profiles"]["default"]["credentials"] == "keyring"
    assert main(["instances", "list"]) == 0
    assert main(["logout"]) == 0
    assert store == {}


def test_config_dir_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test: config dir follows xdg."""
    monkeypatch.delenv("SUDOO_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_dir() == tmp_path / "sudoo"
    assert os.fspath(config.config_dir()).endswith("sudoo")


def test_ci_needs_no_files(run: Run) -> None:
    """Test: ci needs no files."""
    assert run("instances", "list").code == 0
    assert not config.config_dir().exists()
