"""Dependency-free Markdown boundary helpers shared by validation scripts."""

from __future__ import annotations

from collections.abc import Iterator
import re


FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)$")
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def is_escaped(text: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def wikilinks(text: str) -> Iterator[re.Match[str]]:
    return (
        match
        for match in WIKILINK.finditer(text)
        if not is_escaped(text, match.start())
    )


def _blank(chars: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if chars[index] not in "\r\n":
            chars[index] = " "


def _blank_inline_code(chars: list[str], text: str) -> None:
    index = 0
    while index < len(text):
        if text[index] != "`" or is_escaped(text, index):
            index += 1
            continue
        delimiter_end = index + 1
        while delimiter_end < len(text) and text[delimiter_end] == "`":
            delimiter_end += 1
        delimiter = text[index:delimiter_end]
        close = text.find(delimiter, delimiter_end)
        while close >= 0:
            if close and text[close - 1] == "`":
                close = text.find(delimiter, close + 1)
                continue
            if close + len(delimiter) < len(text) and text[close + len(delimiter)] == "`":
                close = text.find(delimiter, close + 1)
                continue
            break
        if close < 0:
            # An unmatched delimiter is ordinary text; advance past it.
            index = delimiter_end
            continue
        _blank(chars, index, close + len(delimiter))
        index = close + len(delimiter)


def markdown_body(
    text: str,
    *,
    mask_frontmatter: bool = True,
    mask_inline_code: bool = True,
) -> str:
    """Mask frontmatter and Markdown code while preserving source offsets."""
    chars = list(text)
    lines: list[tuple[int, int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        lines.append((offset, offset + len(content), offset + len(line), content))
        offset += len(line)
    if offset < len(text) or not lines:
        lines.append((offset, len(text), len(text), text[offset:]))

    frontmatter_end = -1
    if lines and lines[0][3].strip() == "---":
        frontmatter_end = len(lines) - 1
        for index, frontmatter_line in enumerate(lines[1:], start=1):
            if frontmatter_line[3].rstrip(" \t") == "---":
                frontmatter_end = index
                break
        if mask_frontmatter:
            for start, end, _line_end, _content in lines[: frontmatter_end + 1]:
                _blank(chars, start, end)

    fence_char: str | None = None
    fence_length = 0
    for index, (start, end, _line_end, content) in enumerate(lines):
        if index <= frontmatter_end:
            continue
        fence = FENCE_LINE.fullmatch(content)
        if fence_char is not None:
            _blank(chars, start, end)
            if (
                fence is not None
                and fence.group(1)[0] == fence_char
                and len(fence.group(1)) >= fence_length
                and not fence.group(2).strip()
            ):
                fence_char = None
            continue
        if fence is not None:
            if fence.group(1)[0] == "`" and "`" in fence.group(2):
                continue
            fence_char = fence.group(1)[0]
            fence_length = len(fence.group(1))
            _blank(chars, start, end)
            continue
    if mask_inline_code:
        _blank_inline_code(chars, "".join(chars))
    return "".join(chars)


# Compatibility aliases for callers that imported the old private names.
_FENCE_LINE = FENCE_LINE
_is_escaped = is_escaped
_wikilinks = wikilinks
_markdown_body = markdown_body
