"""Two `sudoo` processes that change the configuration at the same moment (two terminals, two CI jobs on one runner)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sudoo_cli import config

# Each process saves its profile this many times, as fast as it can, while the other one does the same.
ROUNDS = 40

WORKER = """
import sys
from sudoo_cli import config

name = sys.argv[1]
for n in range({rounds}):
    config.save_profile(name, f"https://{{name}}.example", f"token-{{name}}-{{n}}", f"secret-{{name}}", None, None, False)
"""


def _run_two(folder: Path, first: str, second: str) -> list[subprocess.CompletedProcess[str]]:
    """Start two processes that each save a profile ROUNDS times, at once, and wait for both.

    Args:
        folder: The config folder both use (SUDOO_CONFIG_DIR).
        first: The profile the first process saves.
        second: The profile the second process saves.

    Returns:
        The two finished processes.
    """
    env = {**os.environ, "SUDOO_CONFIG_DIR": str(folder), "SUDOO_KEYRING": "0"}
    source = str(Path(config.__file__).resolve().parents[1])
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    code = WORKER.format(rounds=ROUNDS)
    procs = [
        subprocess.Popen(  # noqa: S603  the test's own interpreter and code
            [sys.executable, "-c", code, name], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for name in (first, second)
    ]
    finished = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        finished.append(subprocess.CompletedProcess(proc.args, proc.returncode, out, err))
    return finished


def test_two_logins_at_once_keep_both_profiles_and_a_readable_config(tmp_path: Path) -> None:
    """Test: two processes saving different profiles at once both succeed, and both profiles are kept."""
    folder = tmp_path / "shared"
    for attempt in range(3):
        for proc in _run_two(folder, f"alpha{attempt}", f"beta{attempt}"):
            assert proc.returncode == 0, proc.stderr
        stored = json.loads((folder / "config.json").read_text())
        assert {f"alpha{attempt}", f"beta{attempt}"} <= set(stored["profiles"]), stored
        credentials = json.loads((folder / "credentials.json").read_text())
        assert credentials[f"alpha{attempt}"]["token"] == f"token-alpha{attempt}-{ROUNDS - 1}"
        assert credentials[f"beta{attempt}"]["token"] == f"token-beta{attempt}-{ROUNDS - 1}"
    # nothing half-written is left next to the files
    assert sorted(p.name for p in folder.iterdir() if not p.name.startswith(".lock")) == [
        "config.json",
        "credentials.json",
    ]
