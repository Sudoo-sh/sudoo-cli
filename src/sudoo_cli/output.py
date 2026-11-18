"""Output for people (tables, key/value lists) and for scripts (--json)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

Column = tuple[str, Callable[[dict[str, Any]], Any]]


def emit_json(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, ensure_ascii=False, sort_keys=False)
    sys.stdout.write("\n")


def text(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(text(v) for v in value) or "-"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).replace("\n", " ")


def size(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "-"


def when(value: Any) -> str:
    """An ISO 8601 time or Unix seconds, shown as YYYY-MM-DD HH:MM UTC."""
    if value in (None, ""):
        return "-"
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(float(value), UTC)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, OSError):
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M")


def get(path: str) -> Callable[[dict[str, Any]], Any]:
    """A column getter for a dotted path: get("odoo.version")."""

    def read(row: dict[str, Any]) -> Any:
        value: Any = row
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    return read


def table(rows: Sequence[dict[str, Any]], columns: Sequence[Column], empty: str = "Nothing to show.") -> None:
    if not rows:
        print(empty)
        return
    cells = [[text(getter(row)) for _, getter in columns] for row in rows]
    headers = [name for name, _ in columns]
    widths = [max(len(headers[i]), *(len(line[i]) for line in cells)) for i in range(len(headers))]
    print("  ".join(h.upper().ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip())
    for line in cells:
        print("  ".join(c.ljust(w) for c, w in zip(line, widths, strict=True)).rstrip())


def fields(pairs: Sequence[tuple[str, Any]]) -> None:
    width = max((len(k) for k, _ in pairs), default=0)
    for key, value in pairs:
        print(f"{key + ':':<{width + 1}} {text(value)}")


def info(message: str) -> None:
    """A note for people, on stderr so that stdout stays clean for scripts."""
    print(message, file=sys.stderr)
