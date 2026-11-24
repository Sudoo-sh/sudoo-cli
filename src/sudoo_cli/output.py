"""Output for people (tables, key/value lists) and for scripts (--json).

All printing helpers of the commands live here. Text for people goes to stdout, except `info`, which writes to
stderr so that stdout stays clean for scripts.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

# A table column: a pair of the header text and a function that reads the cell value from a row.
Column = tuple[str, Callable[[dict[str, Any]], Any]]


def emit_json(value: Any) -> None:
    """Print a value as indented JSON on stdout, followed by a newline.

    Args:
        value: Anything that `json.dump` accepts.
    """
    json.dump(value, sys.stdout, indent=2, ensure_ascii=False, sort_keys=False)
    sys.stdout.write("\n")


def text(value: Any) -> str:
    """Turn a value into one line of text for a table cell.

    Args:
        value: Any value. None gives "-", booleans "yes" or "no", lists a comma-separated text, dicts JSON.

    Returns:
        The text, with line breaks replaced by spaces.
    """
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
    """Show a number of bytes as a short text such as "12.3 MB".

    Args:
        value: A number of bytes.

    Returns:
        The text, or "-" when the value is not a number.
    """
    if not isinstance(value, (int, float)):
        return "-"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "-"


def when(value: Any) -> str:
    """An ISO 8601 time or Unix seconds, shown as YYYY-MM-DD HH:MM UTC.

    Args:
        value: The time as text or as Unix seconds. None and "" mean no time.

    Returns:
        The formatted time, "-" when there is none, or the value as text when it cannot be read.
    """
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
    """A column getter for a dotted path, for example `get("odoo.version")`.

    Args:
        path: Keys separated by dots. A missing key, or a step through something that is not a dict, gives None.

    Returns:
        A function that takes a row (a dict) and returns the value at the path.
    """

    def read(row: dict[str, Any]) -> Any:
        """Read the value at the path from one row.

        Args:
            row: The dict to read from.

        Returns:
            The value, or None when the path does not exist.
        """
        value: Any = row
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    return read


def table(rows: Sequence[dict[str, Any]], columns: Sequence[Column], empty: str = "Nothing to show.") -> None:
    """Print rows as a table with upper-case headers and aligned columns.

    Args:
        rows: The rows to show.
        columns: The columns, each a header and a getter.
        empty: The line to print when there are no rows.
    """
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
    """Print "Key: value" lines with the values aligned.

    Args:
        pairs: The keys and values, in the order to show them. Values are turned into text with `text`.
    """
    width = max((len(k) for k, _ in pairs), default=0)
    for key, value in pairs:
        print(f"{key + ':':<{width + 1}} {text(value)}")


def info(message: str) -> None:
    """A note for people, on stderr so that stdout stays clean for scripts.

    Args:
        message: The text to print.
    """
    print(message, file=sys.stderr)
