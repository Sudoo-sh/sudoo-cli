"""Profiles: which panel to talk to and with which credentials.

Where things are kept:

* ``config.json`` in the config folder (``$SUDOO_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/sudoo``, else
  ``~/.config/sudoo``): the profiles' panel addresses and options, never a secret.
* The token and the signing secret: in the OS keyring when the optional ``keyring`` package and a working backend
  are present (``pip install 'sudoo-cli[keyring]'``), else in ``credentials.json`` next to it, mode 0600.
* Environment variables override a profile, and need no file at all (CI): ``SUDOO_URL``, ``SUDOO_TOKEN``,
  ``SUDOO_SECRET`` (or ``SUDOO_SIGNING_SECRET``), plus ``SUDOO_PROFILE``, ``SUDOO_CA_CERT``, ``SUDOO_ACTING_FOR``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AUTH, USAGE, CliError

KEYRING_SERVICE = "sudoo-cli"
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass
class Profile:
    name: str
    url: str
    token: str
    secret: str
    ca_cert: str | None = None
    acting_for: str | None = None


def config_dir() -> Path:
    if os.environ.get("SUDOO_CONFIG_DIR"):
        return Path(os.environ["SUDOO_CONFIG_DIR"])
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "sudoo"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise CliError(f"Cannot read {path}: {error}") from None
    return data if isinstance(data, dict) else {}


def _write_private(path: Path, data: dict[str, Any]) -> None:
    """Write a file only its owner can read (0600), in a folder only its owner can open (0700), atomically."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_config() -> dict[str, Any]:
    return _read(config_dir() / "config.json")


def save_config(data: dict[str, Any]) -> None:
    _write_private(config_dir() / "config.json", data)


def _keyring() -> Any | None:
    """The keyring module when it can really store secrets here, else None."""
    if os.environ.get("SUDOO_KEYRING", "").lower() in ("0", "off", "no", "false"):
        return None
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
            return None
        return keyring
    except Exception:  # no package, or no usable backend (a server without a desktop session)
        return None


def store_credentials(profile: str, token: str, secret: str) -> str:
    """Keep the token and signing secret; returns where ("keyring" or the file's path)."""
    keyring = _keyring()
    if keyring is not None:
        try:
            keyring.set_password(KEYRING_SERVICE, f"{profile}:token", token)
            keyring.set_password(KEYRING_SERVICE, f"{profile}:secret", secret)
            _forget_file_credentials(profile)
            return "keyring"
        except Exception:  # noqa: S110  the backend refused: fall back to the private file
            pass
    path = config_dir() / "credentials.json"
    data = _read(path)
    data[profile] = {"token": token, "secret": secret}
    _write_private(path, data)
    return str(path)


def _forget_file_credentials(profile: str) -> None:
    path = config_dir() / "credentials.json"
    data = _read(path)
    if profile in data:
        del data[profile]
        _write_private(path, data)


def load_credentials(profile: str, where: str | None) -> tuple[str, str]:
    if where == "keyring":
        keyring = _keyring()
        if keyring is not None:
            with contextlib.suppress(Exception):
                token = keyring.get_password(KEYRING_SERVICE, f"{profile}:token") or ""
                secret = keyring.get_password(KEYRING_SERVICE, f"{profile}:secret") or ""
                return token, secret
        return "", ""
    entry = _read(config_dir() / "credentials.json").get(profile) or {}
    return str(entry.get("token") or ""), str(entry.get("secret") or "")


def forget(profile: str) -> bool:
    """Remove a profile and its credentials; True when it existed."""
    config = load_config()
    profiles = config.get("profiles", {})
    existed = profile in profiles
    if existed:
        del profiles[profile]
        if config.get("default_profile") == profile:
            config.pop("default_profile")
        save_config(config)
    keyring = _keyring()
    if keyring is not None:
        for part in ("token", "secret"):
            with contextlib.suppress(Exception):
                keyring.delete_password(KEYRING_SERVICE, f"{profile}:{part}")
    _forget_file_credentials(profile)
    return existed


def save_profile(
    name: str, url: str, token: str, secret: str, ca_cert: str | None, acting_for: str | None, make_default: bool
) -> str:
    if not PROFILE_NAME.fullmatch(name):
        raise CliError("A profile name is 1 to 64 letters, digits, '.', '_' or '-'.", USAGE)
    where = store_credentials(name, token, secret)
    config = load_config()
    profiles = config.setdefault("profiles", {})
    entry: dict[str, Any] = {"url": url, "credentials": "keyring" if where == "keyring" else "file"}
    if ca_cert:
        entry["ca_cert"] = ca_cert
    if acting_for:
        entry["acting_for"] = acting_for
    profiles[name] = entry
    if make_default or "default_profile" not in config:
        config["default_profile"] = name
    save_config(config)
    return where


def profile_name(requested: str | None) -> str:
    return requested or os.environ.get("SUDOO_PROFILE") or str(load_config().get("default_profile") or "default")


def resolve(requested: str | None, ca_cert: str | None = None, acting_for: str | None = None) -> Profile:
    """The profile to use, with the environment variables on top of it."""
    env = os.environ
    name = profile_name(requested)
    stored: dict[str, Any] = {}
    token = secret = ""
    # A profile named explicitly is always read; otherwise only when the environment does not give everything.
    env_complete = bool(env.get("SUDOO_URL") and env.get("SUDOO_TOKEN") and _env_secret())
    if requested or not env_complete:
        stored = load_config().get("profiles", {}).get(name) or {}
        if stored:
            token, secret = load_credentials(name, stored.get("credentials"))
    url = env.get("SUDOO_URL") or str(stored.get("url") or "")
    token = env.get("SUDOO_TOKEN") or token
    secret = _env_secret() or secret
    if not (url and token and secret):
        if not stored and not env_complete:
            raise CliError(
                f"Not logged in (profile {name!r}). Run `sudoo login`, or set SUDOO_URL, SUDOO_TOKEN and SUDOO_SECRET.",
                AUTH,
            )
        missing = [label for label, value in (("URL", url), ("token", token), ("signing secret", secret)) if not value]
        raise CliError(f"Profile {name!r} has no {', '.join(missing)}. Run `sudoo login --profile {name}` again.", AUTH)
    return Profile(
        name=name,
        url=url.rstrip("/"),
        token=token,
        secret=secret,
        ca_cert=ca_cert or env.get("SUDOO_CA_CERT") or stored.get("ca_cert"),
        acting_for=acting_for or env.get("SUDOO_ACTING_FOR") or stored.get("acting_for"),
    )


def _env_secret() -> str:
    return os.environ.get("SUDOO_SECRET") or os.environ.get("SUDOO_SIGNING_SECRET") or ""
