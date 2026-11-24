"""Build dist/sudoo.pyz: the whole command in one file, for servers without pip (needs only Python 3.12+).

python3 scripts/build_zipapp.py && ./dist/sudoo.pyz version
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

# The root folder of the repository.
ROOT = Path(__file__).resolve().parent.parent


def build(target: Path) -> Path:
    """Build the zipapp.

    Copies the package into a temporary folder, adds a small __main__.py and packs the folder into one executable
    file that starts with "#!/usr/bin/env python3".

    Args:
        target: The file to write; its folder is created when missing.

    Returns:
        The path of the finished file.

    Side effects:
        Writes the file and sets its mode to 0755.
    """
    with tempfile.TemporaryDirectory() as folder:
        staging = Path(folder)
        shutil.copytree(ROOT / "src" / "sudoo_cli", staging / "sudoo_cli", ignore=shutil.ignore_patterns("__pycache__"))
        (staging / "__main__.py").write_text("import sys\n\nfrom sudoo_cli.cli import main\n\nsys.exit(main())\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(staging, target, interpreter="/usr/bin/env python3", compressed=True)
    target.chmod(0o755)
    return target


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "sudoo.pyz")
    print(f"{out} ({out.stat().st_size} bytes)")
