"""Entry point for `python -m sudoo_cli` and for the single-file zipapp built by scripts/build_zipapp.py.

Calls `sudoo_cli.cli.main` and exits with the exit code it returns.
"""

import sys

from sudoo_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
