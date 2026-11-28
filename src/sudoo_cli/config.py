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
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows: no flock; the files are still replaced whole, but two changes at once may lose one
    fcntl = None  # type: ignore[assignment]

from .errors import AUTH, USAGE, CliError
from .signing import ACTING_FOR

KEYRING_SERVICE = "sudoo-cli"
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass
class Profile:
    """A panel to talk to and the credentials to use.

    Attributes:
        name: The profile's name.
        url: The panel's address, without a trailing slash.
        token: The API token.
        secret: The signing secret.
        ca_cert: Path of a PEM file with a CA certificate to trust for the panel's TLS, or None.
        acting_for: The X-Sudoo-Acting-For value to send, or None.
    """

    name: str
    url: str
    token: str
    secret: str
    ca_cert: str | None = None
    acting_for: str | None = None


def config_dir() -> Path:
    """Find the folder of the configuration files.

    Returns:
        $SUDOO_CONFIG_DIR when set, else $XDG_CONFIG_HOME/sudoo, else ~/.config/sudoo.
    """
    if os.environ.get("SUDOO_CONFIG_DIR"):
        return Path(os.environ["SUDOO_CONFIG_DIR"])
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "sudoo"


def _read(path: Path) -> dict[str, Any]:
    """Read a JSON file that holds an object.

    Args:
        path: The file to read.

    Returns:
        The object; an empty dict when the file does not exist or does not hold an object.

    Raises:
        CliError: When the file cannot be read or is not valid JSON.
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise CliError(f"Cannot read {path}: {error}") from None
    return data if isinstance(data, dict) else {}


def _private_folder(folder: Path) -> Path:
    """Make a folder only its owner can open (0700).

    Args:
        folder: The folder to make, with its parents.

    Returns:
        The folder.
    """
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    return folder


# How many `_changing` blocks this process is inside: the lock is taken by the outermost one only (an flock taken
# again through a second descriptor of the same process would wait for itself).
_held = 0


@contextlib.contextmanager
def _changing() -> Iterator[None]:
    """Hold the config folder's lock while config.json or credentials.json is read, changed and written back.

    Two `sudoo` processes at once (two terminals, two CI jobs on one runner) then change the files one after the
    other, and neither loses what the other wrote. The lock is an flock on `.lock` in the config folder, released when
    the block ends (or the process does). Where flock does not exist (Windows), nothing is locked.

    Yields:
        Nothing; the lock is held inside the block.
    """
    global _held
    if fcntl is None or _held:
        _held += 1
        try:
            yield
        finally:
            _held -= 1
        return
    fd = os.open(_private_folder(config_dir()) / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _held += 1
        try:
            yield
        finally:
            _held -= 1
    finally:
        os.close(fd)  # closing the file releases the lock


def _write_private(path: Path, data: dict[str, Any]) -> None:
    """Write a file only its owner can read (0600), in a folder only its owner can open (0700), atomically.

    The data is written to a new temporary file next to the target (its own name, never shared with another process),
    which then replaces the target in one step; the target is never seen half-written.

    Args:
        path: The file to write.
        data: The object to store as JSON.
    """
    _private_folder(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def load_config() -> dict[str, Any]:
    """Read config.json.

    Returns:
        The stored settings; an empty dict when there are none.
    """
    return _read(config_dir() / "config.json")


def save_config(data: dict[str, Any]) -> None:
    """Write config.json (private file, see `_write_private`).

    Args:
        data: The settings to store.
    """
    _write_private(config_dir() / "config.json", data)


def _keyring() -> Any | None:
    """The keyring module when it can really store secrets here, else None.

    It is None when SUDOO_KEYRING is 0, off, no or false, when the package is missing, or when only a failing or
    disabled backend exists (for example a server without a desktop session).

    Returns:
        The keyring module, or None.
    """
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
    """Keep the token and signing secret; returns where ("keyring" or the file's path).

    The OS keyring is used when it works. Otherwise the secrets go to credentials.json in the config folder.

    Args:
        profile: The profile's name.
        token: The API token.
        secret: The signing secret.

    Returns:
        "keyring", or the path of credentials.json.

    Side effects:
        Writes to the keyring or to credentials.json; removes a file entry of the profile when the keyring is used.
    """
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
    with _changing():
        data = _read(path)
        data[profile] = {"token": token, "secret": secret}
        _write_private(path, data)
    return str(path)


def _forget_file_credentials(profile: str) -> None:
    """Remove a profile's entry from credentials.json, if there is one.

    Args:
        profile: The profile's name.
    """
    path = config_dir() / "credentials.json"
    with _changing():
        data = _read(path)
        if profile in data:
            del data[profile]
            _write_private(path, data)


def load_credentials(profile: str, where: str | None) -> tuple[str, str]:
    """Read a profile's token and signing secret from where they are kept.

    Args:
        profile: The profile's name.
        where: "keyring" to read from the keyring; anything else reads credentials.json.

    Returns:
        The token and the secret; empty texts when they are not found.
    """
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
    """Remove a profile and its credentials; True when it existed.

    Args:
        profile: The profile's name.

    Returns:
        True when the profile was in config.json.

    Side effects:
        Changes config.json, deletes keyring entries and the entry in credentials.json.
    """
    with _changing():
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
    """Store a profile and its credentials.

    Args:
        name: The profile's name: 1 to 64 letters, digits, ".", "_" or "-".
        url: The panel's address.
        token: The API token.
        secret: The signing secret.
        ca_cert: Path of a CA certificate file, or None.
        acting_for: The X-Sudoo-Acting-For value, or None.
        make_default: When True the profile becomes the default one. The first profile is always the default.

    Returns:
        Where the credentials were kept: "keyring" or the path of credentials.json.

    Raises:
        CliError: With exit code USAGE when the name is not allowed.
    """
    if not PROFILE_NAME.fullmatch(name):
        raise CliError("A profile name is 1 to 64 letters, digits, '.', '_' or '-'.", USAGE)
    with _changing():
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
    """Pick the profile's name.

    Args:
        requested: The name given with --profile, or None.

    Returns:
        The requested name, else $SUDOO_PROFILE, else the default profile, else "default".
    """
    return requested or os.environ.get("SUDOO_PROFILE") or str(load_config().get("default_profile") or "default")


def resolve(requested: str | None, ca_cert: str | None = None, acting_for: str | None = None) -> Profile:
    """The profile to use, with the environment variables on top of it.

    Args:
        requested: The profile named on the command line, or None.
        ca_cert: A CA certificate path from the command line; it wins over the environment and the profile.
        acting_for: An X-Sudoo-Acting-For value from the command line; it wins over the environment and the profile.

    Returns:
        The complete `Profile`.

    Raises:
        CliError: With exit code AUTH when the URL, the token or the secret is missing, and with exit code USAGE when
            the acting-for value is not allowed.
    """
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
    acting = acting_for or env.get("SUDOO_ACTING_FOR") or stored.get("acting_for")
    if acting is not None and not ACTING_FOR.fullmatch(acting):
        raise CliError("--acting-for: 1 to 120 letters, digits, '.', '_', '@', '+' or '-'.", USAGE)
    return Profile(
        name=name,
        url=url.rstrip("/"),
        token=token,
        secret=secret,
        ca_cert=ca_cert or env.get("SUDOO_CA_CERT") or stored.get("ca_cert"),
        acting_for=acting,
    )


def _env_secret() -> str:
    """The signing secret from the environment.

    Returns:
        $SUDOO_SECRET, else $SUDOO_SIGNING_SECRET, else an empty text.
    """
    return os.environ.get("SUDOO_SECRET") or os.environ.get("SUDOO_SIGNING_SECRET") or ""
