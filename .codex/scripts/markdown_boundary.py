"""Dependency-free Markdown boundary helpers shared by validation scripts."""

from __future__ import annotations

from collections.abc import Iterator
from html.parser import HTMLParser
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
HTML_TAG = re.compile(r'<(?:[^"\'>]|"[^"]*"|\'[^\']*\')*>', re.DOTALL)
HTML_LITERAL_TAGS = frozenset({"pre", "script", "style", "textarea"})
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


def _indent_columns(value: str, start: int = 0) -> int:
    columns = start
    for char in value:
        columns += 4 - (columns % 4) if char == "\t" else 1
    return columns - start


def _list_content_indent(list_item: re.Match[str]) -> tuple[int, int]:
    indent = list_item.group("indent")
    marker = list_item.group("marker")
    gap = list_item.group("gap")
    marker_end = _indent_columns(indent) + len(marker)
    gap_width = _indent_columns(gap, marker_end) if gap else 0
    return marker_end + (gap_width if gap else 1), gap_width


def _fence_match(content: str) -> re.Match[str] | None:
    match = FENCE_LINE.fullmatch(content)
    if match is None or _indent_columns(content[:match.start(1)]) > 3:
        return None
    return match


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


def _blank_inline_html_elements(
    chars: list[str], text: str, tags: frozenset[str]
) -> None:
    class ElementParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.open_tags: list[tuple[str, int]] = []
            self.spans: list[tuple[int, int]] = []
            self.line_starts = [0]
            self.line_starts.extend(
                index + 1 for index, char in enumerate(text) if char == "\n"
            )

        def _offset(self) -> int:
            line, column = self.getpos()
            return self.line_starts[line - 1] + column

        def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
            folded = tag.casefold()
            if folded not in tags:
                return
            start = self._offset()
            if not is_escaped(text, start):
                self.open_tags.append((folded, start))

        def handle_endtag(self, tag: str) -> None:
            folded = tag.casefold()
            if folded not in tags:
                return
            for index in range(len(self.open_tags) - 1, -1, -1):
                if self.open_tags[index][0] != folded:
                    continue
                _tag, start = self.open_tags.pop(index)
                end = text.find(">", self._offset())
                self.spans.append((start, len(text) if end < 0 else end + 1))
                break

    parser = ElementParser()
    parser.feed(text)
    parser.close()
    for start, end in parser.spans:
        _blank(chars, start, end)
    for _tag, start in parser.open_tags:
        _blank(chars, start, len(text))


def _blank_inline_html_code(chars: list[str], text: str) -> None:
    _blank_inline_html_elements(chars, text, frozenset({"code"}))


def _blank_inline_html_literals(chars: list[str], text: str) -> None:
    _blank_inline_html_elements(chars, text, HTML_LITERAL_TAGS)


def _indented_content(content: str) -> str:
    """Remove real quote/list containers before testing indented code."""
    remainder = content
    while True:
        if (prefix := BLOCKQUOTE_PREFIX.match(remainder)) is not None:
            remainder = remainder[prefix.end():]
            continue
        list_item = LIST_ITEM.match(remainder)
        if list_item is None:
            return remainder
        indent = _indent_columns(list_item.group("indent"))
        _content_indent, gap_width = _list_content_indent(list_item)
        if indent >= 4 or gap_width > 4:
            return remainder
        remainder = remainder[list_item.end():]


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
    match = _fence_match(remainder)
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
        _content_indent, gap_width = _list_content_indent(list_item)
        if gap_width > 4:
            return content, ()
        content_indent = _content_indent
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
        columns = 0
        index = 0
        while index < len(remainder) and columns < value:
            char = remainder[index]
            if char not in " \t":
                return None
            columns += 4 - (columns % 4) if char == "\t" else 1
            index += 1
        if columns < value:
            return None
        remainder = remainder[index:]
    return remainder


def _continuation_fence_parts(
    content: str,
    list_contexts: list[tuple[int, int]],
) -> tuple[tuple[tuple[str, int], ...], str, str] | None:
    for _indentation, content_indent in reversed(list_contexts):
        container = (("list", content_indent),)
        remainder = _container_body(content, container)
        if remainder is None:
            continue
        match = _fence_match(remainder)
        if match is not None:
            return container, match.group(1), match.group(2)
    return None


def _is_html_literal_open(content: str) -> bool:
    remainder, _container = _container_prefix(content)
    opening = HTML_LITERAL_OPEN.match(remainder)
    return (
        opening is not None
        and _indent_columns(remainder[: opening.start("tag") - 1]) <= 3
    )


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
        columns = 0
        index = 0
        while index < len(remainder) and columns < value:
            char = remainder[index]
            if char not in " \t":
                return False
            columns += 4 - (columns % 4) if char == "\t" else 1
            index += 1
        if columns < value:
            return False
        remainder = remainder[index:]
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
    html_container: tuple[tuple[str, int], ...] | None = None
    list_contexts: list[tuple[int, int]] = []
    paragraph_active = False
    for index, (start, end, _line_end, content) in enumerate(lines):
        if index <= frontmatter_end and mask_frontmatter:
            continue
        if html_literal is not None:
            if html_container is not None and not _container_present(content, html_container):
                html_literal = None
                html_container = None
            else:
                _blank(chars, start, end)
                closing = HTML_LITERAL_CLOSE.search(content)
                if closing is not None and closing.group("tag").casefold() == html_literal:
                    html_literal = None
                    html_container = None
                paragraph_active = False
                continue
        fence = _fence_parts(content, container=fence_container)
        if fence_container is None and fence is not None and not fence[0]:
            continuation = _continuation_fence_parts(content, list_contexts)
            if continuation is not None:
                fence = continuation
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
            remainder, container = _container_prefix(content)
            opening = HTML_LITERAL_OPEN.match(remainder)
            closing = HTML_LITERAL_CLOSE.search(content)
            if opening is not None and (
                closing is None
                or closing.group("tag").casefold() != opening.group("tag").casefold()
            ):
                html_literal = opening.group("tag").casefold()
                html_container = container
            paragraph_active = False
            continue
        indented_content = _indented_content(content)
        indentation = _indent_columns(
            indented_content[: len(indented_content) - len(indented_content.lstrip(" \t"))]
        )
        indented_list_item = LIST_ITEM.match(indented_content)
        excessive_list_gap = (
            indented_list_item is not None
            and _list_content_indent(indented_list_item)[1] > 4
        )
        indented_code = (
            INDENTED_CODE_LINE.match(indented_content) is not None
            or excessive_list_gap
        )
        if indented_code:
            if paragraph_active and indented_content == content and not excessive_list_gap:
                continue
            list_context = None
            if indented_content == content:
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
            list_contexts = []
            paragraph_active = False
            continue
        list_item = LIST_ITEM.match(content)
        if list_item is not None:
            content_indent, gap_width = _list_content_indent(list_item)
            if gap_width > 4:
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
                list_contexts.append((indentation, content_indent))
                paragraph_active = False
                continue
            _blank(chars, start, end)
            list_contexts = []
            paragraph_active = False
            continue
        if content.strip():
            list_contexts = [
                context
                for context in list_contexts
                if indentation >= context[1]
            ]
        paragraph_active = _is_paragraph_line(content)
    if mask_inline_code:
        _blank_inline_code(chars, "".join(chars))
        _blank_inline_html_code(chars, "".join(chars))
    _blank_inline_html_literals(chars, "".join(chars))
    return "".join(chars)


# Compatibility aliases for callers that imported the old private names.
_FENCE_LINE = FENCE_LINE
_is_escaped = is_escaped
_wikilinks = wikilinks
_markdown_body = markdown_body
