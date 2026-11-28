# sudoo-cli

[![PyPI](https://img.shields.io/pypi/v/sudoo-cli)](https://pypi.org/project/sudoo-cli/)

`sudoo` is the command-line client of the [Sudoo.sh](https://sudoo.sh) panel: list and create instances, deploy
the latest code of a branch, follow jobs live, back up and restore, manage custom domains and read caught mail, from
a terminal or a CI pipeline. Every request is signed with your API token's signing secret, exactly as the panel's
API requires.

It needs Python 3.12 or later and nothing else (the OS keyring is optional).

## Install

`sudoo-cli` is published on [PyPI](https://pypi.org/project/sudoo-cli/).

```bash
pipx install sudoo-cli          # or: uv tool install sudoo-cli
pip install sudoo-cli           # in a virtual environment
pipx install 'sudoo-cli[keyring]'   # keep credentials in the OS keyring
```

On a server without pip, download `sudoo.pyz` (one file) from the
[latest release](https://github.com/Sudoo-sh/sudoo-cli/releases/latest), check it against `SHA256SUMS`, and run
`python3 sudoo.pyz`.

## Log in

Create an API token in the panel (**API tokens**, **Create token**), then:

```bash
sudoo login --url https://panel.example.com     # asks for the token and the signing secret
sudoo status
```

The credentials go to the OS keyring when available, else to `~/.config/sudoo/credentials.json` (mode 0600). Use
`--profile NAME` for several panels. In CI, set `SUDOO_URL`, `SUDOO_TOKEN` and `SUDOO_SECRET` instead: no login and
no file are needed.

## Examples

```bash
sudoo instances list
sudoo instances create --repo https://github.com/acme/sales --branch feature-x --odoo-version 19.0 --delete-after-days 7
sudoo deploy --project sales --branch main          # get the latest code and follow the job
sudoo instances logs sales-main --follow
sudoo backups create sales-main
sudoo backups download sales-main__sales_main__20261112-000012__daily.zip
sudoo domains add sales-main erp.acme.com --confirm-production
sudoo jobs follow a12fff66a05d
sudoo instances show sales-main --json | jq -r .data.url
```

`--json` prints machine-readable output; exit codes are documented (11 when a followed job fails, 8 when still
rate-limited, ...).

### GitHub Actions deploy step

```yaml
- name: Deploy to the panel
  env:
    SUDOO_URL: ${{ vars.SUDOO_URL }}
    SUDOO_TOKEN: ${{ secrets.SUDOO_TOKEN }}
    SUDOO_SECRET: ${{ secrets.SUDOO_SECRET }}
  run: |
    pipx install sudoo-cli==3.0.0
    sudoo deploy --project sales --branch "${GITHUB_REF_NAME}"
```

Deploying needs a token with the `write` scope; give each pipeline its own token.

## Documentation

Every command, option, exit code and more CI examples: [docs/CLI.md](docs/CLI.md), also on
[docs.sudoo.sh/cli](https://docs.sudoo.sh/cli).

## How this code is organised

| Path | What lives there |
| :--- | :--- |
| `src/sudoo_cli/cli.py` | The `sudoo` command line: options, sub-commands and the exit code. |
| `src/sudoo_cli/commands.py` | The commands themselves; each takes the context and raises `CliError` on failure. |
| `src/sudoo_cli/context.py` | What every command gets: the parsed options, the client and output helpers. |
| `src/sudoo_cli/client.py` | Signed HTTPS calls to the panel's API (`/api/v1`), standard library only. |
| `src/sudoo_cli/signing.py` | Request signing, exactly as the panel's API describes it. |
| `src/sudoo_cli/config.py` | Profiles and credentials: which panel to talk to and how. |
| `src/sudoo_cli/jobs.py` | Following a job to its end, live or by polling. |
| `src/sudoo_cli/output.py` | Tables and key/value lists for people, `--json` for scripts. |
| `src/sudoo_cli/completion.py` | Shell completion scripts, made from the command line's own definition. |
| `src/sudoo_cli/errors.py` | The errors a command reports and the exit code of each. |
| `scripts/build_zipapp.py` | Builds `dist/sudoo.pyz`, the whole command in one file. |
| `tests/` | Tests against an in-process mock panel (`tests/mock_panel.py`); never a real panel. |
| `docs/CLI.md` | The reference: every command, option and exit code. |

## Development

```bash
uv sync
uv run pytest                     # against a local mock panel; never a real one
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python scripts/build_zipapp.py && ./dist/sudoo.pyz version
```

Releases: bump the version in `pyproject.toml` and `src/sudoo_cli/__init__.py`, add the changes to `CHANGELOG.md`,
tag `vX.Y.Z` and push the tag; the publish workflow tests, builds, publishes to PyPI (Trusted Publishing) and
creates the GitHub release with `sudoo.pyz`.

### Formatting

Code is formatted with black and linted with ruff (settings in `pyproject.toml`; ruff also sorts imports), and mypy
checks the types. Run `uv run black .` to rewrite files and `uv run black --check .` to check them without changes.
The same checks run on every commit with pre-commit (run `uvx pre-commit install` once).

## Licence

[Apache License 2.0](LICENSE.md). "Sudoo" and "Sudoo.sh" are trademarks of the copyright holder.
