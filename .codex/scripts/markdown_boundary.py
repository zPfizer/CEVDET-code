"""Dependency-free Markdown boundary helpers shared by validation scripts."""

from __future__ import annotations

from collections.abc import Iterator
import re


FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)$")
BLOCKQUOTE_PREFIX = re.compile(r"^[ \t]{0,3}>[ \t]?")
HTML_LITERAL_OPEN = re.compile(
    r"^[ \t]{0,3}<(?P<tag>pre|script|style|textarea)(?:[ \t/>]|$)",
    re.IGNORECASE,
)
HTML_LITERAL_CLOSE = re.compile(
    r"</(?P<tag>pre|script|style|textarea)[ \t]*>",
    re.IGNORECASE,
)
INDENTED_CODE_LINE = re.compile(r"^(?: {4,}|\t)")
LIST_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])(?P<gap>[ \t]+|$)"
)
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


def _fence_parts(
    content: str,
    *,
    container: tuple[tuple[str, int], ...] | None = None,
) -> tuple[tuple[tuple[str, int], ...], str, str] | None:
    if container is None:
        remainder, container = _container_prefix(content)
    else:
        remainder = _container_body(content, container)
        if remainder is None:
            return None
    match = FENCE_LINE.fullmatch(remainder)
    if match is None:
        return None
    return container, match.group(1), match.group(2)


def _container_prefix(
    content: str,
) -> tuple[str, tuple[tuple[str, int], ...]]:
    remainder = content
    containers: list[tuple[str, int]] = []
    while True:
        quote_depth = 0
        while (prefix := BLOCKQUOTE_PREFIX.match(remainder)) is not None:
            quote_depth += 1
            remainder = remainder[prefix.end():]
        if quote_depth:
            containers.append(("quote", quote_depth))
            continue
        list_item = LIST_ITEM.match(remainder)
        if list_item is None:
            break
        gap = list_item.group("gap")
        if gap and len(gap) > 4:
            return content, ()
        content_indent = (
            len(list_item.group("indent"))
            + len(list_item.group("marker"))
            + (len(gap) if gap else 1)
        )
        containers.append(("list", content_indent))
        remainder = remainder[list_item.end():]
    return remainder, tuple(containers)


def _container_body(
    content: str,
    container: tuple[tuple[str, int], ...],
) -> str | None:
    remainder = content
    for kind, value in container:
        if kind == "quote":
            quote_depth = 0
            while (prefix := BLOCKQUOTE_PREFIX.match(remainder)) is not None:
                quote_depth += 1
                remainder = remainder[prefix.end():]
            if quote_depth != value:
                return None
            continue
        if len(remainder) < value:
            return None
        prefix = remainder[:value]
        if not prefix or not all(char in " \t" for char in prefix):
            return None
        remainder = remainder[value:]
    return remainder


def _is_html_literal_open(content: str) -> bool:
    remainder, _container = _container_prefix(content)
    return HTML_LITERAL_OPEN.match(remainder) is not None


def _container_present(
    content: str,
    container: tuple[tuple[str, int], ...],
) -> bool:
    remainder = content
    for kind, value in container:
        if kind == "quote":
            quote_depth = 0
            while (prefix := BLOCKQUOTE_PREFIX.match(remainder)) is not None:
                quote_depth += 1
                remainder = remainder[prefix.end():]
            if quote_depth < value:
                return False
            if quote_depth > value:
                return True
            continue
        if not content.strip():
            return True
        prefix = remainder[:value]
        if len(prefix) != value or not all(char in " \t" for char in prefix):
            return False
        remainder = remainder[value:]
    return True


def _is_paragraph_line(content: str) -> bool:
    if not content.strip() or INDENTED_CODE_LINE.match(content):
        return False
    if _fence_parts(content) is not None:
        return False
    stripped = content.lstrip(" \t")
    if stripped.startswith(">"):
        return False
    if re.match(r"^#{1,6}(?:[ \t]+|$)", stripped):
        return False
    if LIST_ITEM.match(content) is not None:
        return False
    if re.fullmatch(r"(?:[-*_][ \t]*){3,}|=+[ \t]*", stripped):
        return False
    return True


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
    if lines and lines[0][3] == "---":
        closing = next(
            (
                index
                for index, frontmatter_line in enumerate(lines[1:], start=1)
                if frontmatter_line[3] == "---"
            ),
            None,
        )
        if closing is None:
            if mask_frontmatter:
                frontmatter_end = len(lines) - 1
        else:
            frontmatter_end = closing
        if mask_frontmatter and frontmatter_end >= 0:
            for start, end, _line_end, _content in lines[: frontmatter_end + 1]:
                _blank(chars, start, end)

    fence_char: str | None = None
    fence_length = 0
    fence_container: tuple[tuple[str, int], ...] | None = None
    html_literal: str | None = None
    list_contexts: list[tuple[int, int]] = []
    paragraph_active = False
    for index, (start, end, _line_end, content) in enumerate(lines):
        if index <= frontmatter_end and mask_frontmatter:
            continue
        if html_literal is not None:
            _blank(chars, start, end)
            closing = HTML_LITERAL_CLOSE.search(content)
            if closing is not None and closing.group("tag").casefold() == html_literal:
                html_literal = None
            paragraph_active = False
            continue
        fence = _fence_parts(content, container=fence_container)
        if fence_char is not None:
            if (
                fence_container is not None
                and not _container_present(content, fence_container)
            ):
                fence_char = None
                fence_length = 0
                fence_container = None
                fence = _fence_parts(content)
            else:
                _blank(chars, start, end)
                if (
                    fence is not None
                    and fence_container is not None
                    and fence[0] == fence_container
                    and fence[1][0] == fence_char
                    and len(fence[1]) >= fence_length
                    and not fence[2].strip()
                ):
                    fence_char = None
                    fence_length = 0
                    fence_container = None
                paragraph_active = False
                continue
        if fence is not None:
            if fence[1][0] == "`" and "`" in fence[2]:
                paragraph_active = False
                continue
            fence_char = fence[1][0]
            fence_length = len(fence[1])
            fence_container = fence[0]
            _blank(chars, start, end)
            paragraph_active = False
            continue
        if _is_html_literal_open(content):
            _blank(chars, start, end)
            remainder, _container = _container_prefix(content)
            opening = HTML_LITERAL_OPEN.match(remainder)
            closing = HTML_LITERAL_CLOSE.search(content)
            if opening is not None and (
                closing is None
                or closing.group("tag").casefold() != opening.group("tag").casefold()
            ):
                html_literal = opening.group("tag").casefold()
            paragraph_active = False
            continue
        indentation = len(content) - len(content.lstrip(" \t"))
        list_item = LIST_ITEM.match(content)
        if list_item is not None:
            gap = list_item.group("gap")
            if gap and len(gap) > 4:
                _blank(chars, start, end)
                list_contexts = []
                paragraph_active = False
                continue
            nested = any(parent < indentation for parent, _content in list_contexts)
            if indentation < 4 or nested:
                list_contexts = [
                    context
                    for context in list_contexts
                    if context[0] < indentation
                ]
                content_indent = (
                    indentation
                    + len(list_item.group("marker"))
                    + (len(gap) if gap else 1)
                )
                list_contexts.append((indentation, content_indent))
                paragraph_active = False
                continue
            _blank(chars, start, end)
            list_contexts = []
            paragraph_active = False
            continue
        if INDENTED_CODE_LINE.match(content):
            if paragraph_active:
                continue
            list_context = next(
                (
                    context
                    for context in reversed(list_contexts)
                    if indentation >= context[1]
                ),
                None,
            )
            if (
                list_context is not None
                and indentation < list_context[1] + 4
            ):
                continue
            _blank(chars, start, end)
            paragraph_active = False
            continue
        if content.strip():
            list_contexts = []
        paragraph_active = _is_paragraph_line(content)
    if mask_inline_code:
        _blank_inline_code(chars, "".join(chars))
    return "".join(chars)


# Compatibility aliases for callers that imported the old private names.
_FENCE_LINE = FENCE_LINE
_is_escaped = is_escaped
_wikilinks = wikilinks
_markdown_body = markdown_body
