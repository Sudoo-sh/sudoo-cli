# Contributing to sudoo-cli

Thank you for helping. Open an issue first for anything larger than a small fix, so we can agree on the shape.

## Before you open a pull request

```bash
uv sync
uv run pytest                     # against the in-process mock panel; never a real one
uv run ruff check . && uv run black --check . && uv run mypy
uvx pre-commit run --all-files    # the same checks the commit hook runs
```

Sign off every commit (`git commit -s`) to certify the [Developer Certificate of Origin](https://developercertificate.org).

## Documentation rule

Every module, class and public function has a docstring. It says what the code does, what it takes, what it
returns, which errors it raises and any side effect (files written, network calls, the exit code). Complex private
helpers get one too. Comments explain why the code is written the way it is, not what each line does.

The `interrogate` pre-commit hook fails when docstring coverage drops under 90%. A change to a command, an option
or an exit code also updates [docs/CLI.md](docs/CLI.md) and `CHANGELOG.md`.
