"""`python -m sudoo_cli`, and the entry point of the single-file zipapp."""

import sys

from sudoo_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
