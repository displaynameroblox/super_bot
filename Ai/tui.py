"""Small terminal layout helpers; no terminal or network required."""

import unicodedata


def clean(text: str) -> str:
    """Keep control characters from breaking the terminal layout."""
    return "".join(
        "    " if char == "\t" else " " if unicodedata.category(char).startswith("C") else char
        for char in str(text)
    )


def cell_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def width(text: str) -> int:
    return sum(cell_width(char) for char in clean(text))


def clip(text: str, columns: int, ellipsis: bool = False) -> str:
    text = clean(text)
    if columns <= 0:
        return ""
    if width(text) <= columns:
        return text
    suffix = "…" if ellipsis else ""
    budget = columns - len(suffix)
    result = []
    used = 0
    for char in text:
        size = cell_width(char)
        if used + size > budget:
            break
        result.append(char)
        used += size
    return "".join(result) + suffix


def pad(text: str, columns: int) -> str:
    text = clip(text, columns)
    return text + " " * max(0, columns - width(text))


def editor_view(text: str, cursor: int, columns: int) -> tuple[str, int]:
    """A horizontally scrolling editor that keeps the insertion point visible."""
    cursor = max(0, min(cursor, len(text)))
    start = cursor
    used = 0
    while start > 0:
        size = width(text[start - 1])
        if used + size > max(0, columns - 1):
            break
        start -= 1
        used += size
    return clip(text[start:], columns), used


def diff_style(line: str) -> str:
    if line.startswith(("+++", "---", "@@", "diff ")):
        return "player"
    if line.startswith("+"):
        return "success"
    if line.startswith("-"):
        return "error"
    return "dim"
