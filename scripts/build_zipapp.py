"""Build dist/sudoo.pyz: the whole command in one file, for servers without pip (needs only Python 3.12+).

python3 scripts/build_zipapp.py && ./dist/sudoo.pyz version
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(target: Path) -> Path:
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
