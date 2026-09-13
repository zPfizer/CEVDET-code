"""Dependency-free Markdown boundary helpers shared by validation scripts."""

from __future__ import annotations

from collections.abc import Iterator
from html.parser import HTMLParser
import re


FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)$")
BLOCKQUOTE_PREFIX = re.compile(r"^[ \t]{0,3}>[ \t]?")
HTML_LITERAL_OPEN = re.compile(
    r"^[ \t]{0,3}<(?P<tag>pre|script|style|textarea)(?:[ \t>]|$)",
    re.IGNORECASE,
)
HTML_LITERAL_CLOSE = re.compile(
    r"</(?P<tag>pre|script|style|textarea)>",
    re.IGNORECASE,
)
HTML_TAG = re.compile(r'<(?:[^"\'>]|"[^"]*"|\'[^\']*\')*>', re.DOTALL)
HTML_START_TAG_NAME = re.compile(
    r"<(?P<tag>[A-Za-z][A-Za-z0-9-]*)(?=[ \t\r\n\f/>])"
)
HTML_START_TAG = re.compile(
    r"<(?P<tag>[A-Za-z][A-Za-z0-9-]*)"
    r"(?:[ \t\r\n\f]+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:[ \t\r\n\f]*=[ \t\r\n\f]*"
    r"(?:\"[^\"]*\"|'[^']*'|[^ \t\r\n\f\"'=<>`]+))?"
    r")*[ \t\r\n\f]*/?>"
)
HTML_END_TAG_NAME = re.compile(r'</\s*(?P<tag>[A-Za-z][A-Za-z0-9-]*)\b')
HTML_END_TAG = re.compile(r'</[A-Za-z][A-Za-z0-9-]*[ \t\r\n\f]*>')
HTML_LITERAL_TAGS = frozenset({"pre", "script", "style", "textarea"})
LIST_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|[0-9]{1,9}[.)])(?P<gap>[ \t]+|$)"
)
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
ATX_HEADING_LINE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")
THEMATIC_BREAK = re.compile(r'(?P<marker>[-*_])(?:[ \t]*(?P=marker)){2,}[ \t]*')
REFERENCE_DEFINITION = re.compile(
    r'(?m)^[ \t]{0,3}\[(?P<label>(?:\\[^\r\n]|[^\]\\\r\n])+)\]:[^\r\n]*'
)


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


def _reference_definition_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []

    def line_end(start: int) -> int:
        end = text.find('\n', start)
        return len(text) if end < 0 else end

    def skip_hspace(start: int) -> int:
        while start < len(text) and text[start] in ' \t':
            start += 1
        return start

    def newline_after(start: int) -> int | None:
        if text.startswith('\r\n', start):
            return start + 2
        if text.startswith('\n', start):
            return start + 1
        return None

    def consume_destination(start: int) -> int | None:
        if start >= len(text) or text[start] in '\r\n':
            return None
        if text[start] == '<':
            cursor = start + 1
            while cursor < len(text):
                if text[cursor] in '\r\n':
                    return None
                if (
                    text[cursor] == '\\'
                    and cursor + 1 < len(text)
                    and text[cursor + 1] not in '\r\n'
                ):
                    cursor += 2
                    continue
                if text[cursor] == '>':
                    return cursor + 1
                cursor += 1
            return None
        cursor = start
        depth = 0
        while cursor < len(text) and text[cursor] not in ' \t\r\n':
            if (
                text[cursor] == '\\'
                and cursor + 1 < len(text)
                and text[cursor + 1] not in '\r\n'
            ):
                cursor += 2
                continue
            if text[cursor] == '(':
                depth += 1
            elif text[cursor] == ')':
                if depth == 0:
                    return None
                depth -= 1
            cursor += 1
        if cursor == start or depth:
            return None
        return cursor

    def title_start_after_definition(
        definition: re.Match[str], offset: int,
    ) -> tuple[int, str] | None:
        cursor = offset + definition.end('label') + 2
        cursor = skip_hspace(cursor)
        if text.startswith('\r\n', cursor) or text.startswith('\n', cursor):
            after_definition = newline_after(cursor)
            if after_definition is None:
                return None
            cursor = skip_hspace(after_definition)
        destination_end = consume_destination(cursor)
        if destination_end is None:
            return None
        candidate = skip_hspace(destination_end)
        if candidate < len(text) and text[candidate] in "\"'(":
            if candidate == destination_end:
                return None
            return candidate, text[candidate]
        if candidate >= len(text) or text[candidate] not in '\r\n':
            return None
        after_destination = newline_after(candidate)
        if after_destination is None:
            return None
        candidate = skip_hspace(after_destination)
        if candidate < len(text) and text[candidate] in "\"'(":
            return candidate, text[candidate]
        return None

    def title_end(start: int, opener: str) -> int | None:
        closer = ')' if opener == '(' else opener
        cursor = start + 1
        while cursor < len(text):
            current_end = line_end(cursor)
            escaped = False
            for index in range(cursor, current_end):
                if escaped:
                    escaped = False
                elif text[index] == '\\':
                    escaped = True
                elif opener == '(' and text[index] == '(':
                    return None
                elif text[index] == closer:
                    if text[index + 1:current_end].strip(' \t\r'):
                        return None
                    return index + 1
            if current_end >= len(text):
                return None
            next_start = current_end + 1
            if not text[next_start:line_end(next_start)].rstrip('\r').strip():
                return None
            cursor = next_start
        return None

    offset = 0
    list_contexts: list[tuple[int, int]] = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip('\r\n')
        list_item = LIST_ITEM.match(content)
        if list_item is not None:
            indentation = _indent_columns(list_item.group('indent'))
            content_indent, gap_width = _list_content_indent(list_item)
            list_contexts = [item for item in list_contexts if item[0] < indentation]
            if gap_width <= 4:
                list_contexts.append((indentation, content_indent))
            logical_line, _containers = _container_prefix(content)
        else:
            logical_line = content
            for _indentation, content_indent in reversed(list_contexts):
                candidate = _container_body(content, (('list', content_indent),))
                if candidate is not None:
                    logical_line = candidate
                    list_contexts = [item for item in list_contexts if item[1] <= content_indent]
                    break
            else:
                if content.strip():
                    list_contexts = []
            logical_line, _containers = _container_prefix(logical_line)
        logical_offset = offset + len(content) - len(logical_line)
        offset += len(line)
        definition = REFERENCE_DEFINITION.match(logical_line)
        if definition is None:
            continue
        start = logical_offset + definition.start()
        end = logical_offset + definition.end()
        title = title_start_after_definition(definition, logical_offset)
        if title is None:
            spans.append((start, end))
            continue
        span_end = title_end(*title)
        if span_end is None:
            spans.append((start, end))
        else:
            spans.append((start, span_end))
    return tuple(spans)


def markdown_link_spans(
    text: str, *, include_labels: bool = True
) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index] != '[' or is_escaped(text, index):
            index += 1
            continue
        label_depth = 1
        label_end = None
        cursor = index + 1
        while cursor < len(text):
            if text[cursor] == '\\':
                cursor += 2
                continue
            if text[cursor] == '[':
                label_depth += 1
            elif text[cursor] == ']':
                label_depth -= 1
                if label_depth == 0:
                    label_end = cursor
                    break
            cursor += 1
        if label_end is None or label_end + 1 >= len(text) or text[label_end + 1] != '(':
            index = max(cursor, index + 1)
            continue
        cursor = label_end + 2
        while cursor < len(text) and text[cursor] in ' \t\r\n':
            cursor += 1
        depth = 1
        angle_destination = cursor < len(text) and text[cursor] == '<'
        if angle_destination:
            cursor += 1
        title = None
        span_end = None
        while cursor < len(text):
            if text[cursor] == '\\':
                cursor += 2
                continue
            if angle_destination:
                if text[cursor] == '>':
                    angle_destination = False
                elif text[cursor] in '<\r\n':
                    break
                cursor += 1
                continue
            if title is not None:
                if text[cursor] == title:
                    cursor += 1
                    while cursor < len(text) and text[cursor] in ' \t\r\n':
                        cursor += 1
                    if cursor >= len(text) or text[cursor] != ')':
                        break
                    span_end = cursor + 1
                    break
                if title == ')' and text[cursor] == '(':
                    break
                cursor += 1
                continue
            if depth == 1 and text[cursor] in ' \t\r\n':
                while cursor < len(text) and text[cursor] in ' \t\r\n':
                    cursor += 1
                if cursor < len(text) and text[cursor] in "\"'(":
                    title = ')' if text[cursor] == '(' else text[cursor]
                    cursor += 1
                    continue
                if cursor >= len(text) or text[cursor] != ')':
                    break
            if text[cursor] == '(':
                depth += 1
            elif text[cursor] == ')':
                depth -= 1
                if depth == 0:
                    span_end = cursor + 1
                    break
            cursor += 1
        if span_end is not None and not _crosses_inline_block(text, index, span_end - 1):
            start = index if include_labels else label_end + 1
            spans.append((start, span_end))
            index = span_end
        else:
            index = label_end + 2
    spans.extend(_reference_definition_spans(text))
    return tuple(spans)


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


def _crosses_inline_block(text: str, start: int, end: int) -> bool:
    opening_line = text.rfind('\n', 0, start) + 1
    closing_line = text.rfind('\n', 0, end) + 1
    if opening_line == closing_line:
        return False
    line_start = opening_line
    opening_quote_depth = 0
    while line_start <= closing_line:
        line_end = text.find('\n', line_start)
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end].rstrip('\r')
        logical_line, containers = _container_prefix(line)
        quote_depth = sum(value for kind, value in containers if kind == 'quote')
        if line_start == opening_line:
            opening_quote_depth = quote_depth
        elif quote_depth > opening_quote_depth:
            return True
        if not logical_line.strip() or ATX_HEADING_LINE.match(logical_line):
            return True
        if line_end >= len(text):
            break
        line_start = line_end + 1
    return False


def _blank_invalid_html_tags(
    parser_text: list[str], text: str, tags: frozenset[str]
) -> None:
    protected_tags = tags | HTML_LITERAL_TAGS
    for match in HTML_TAG.finditer(text):
        raw = match.group(0)
        closing = raw.startswith('</')
        name = (HTML_END_TAG_NAME if closing else HTML_START_TAG_NAME).match(raw)
        if name is None or name.group("tag").casefold() not in protected_tags:
            continue
        if (HTML_END_TAG if closing else HTML_START_TAG).fullmatch(raw) is None:
            _blank(parser_text, match.start(), match.end())


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
        if _crosses_inline_block(text, index, close):
            index = delimiter_end
            continue
        _blank(chars, index, close + len(delimiter))
        index = close + len(delimiter)


def _blank_inline_html_elements(
    chars: list[str], text: str, tags: frozenset[str]
) -> None:
    metadata_spans = markdown_link_spans(text, include_labels=False)
    parser_text = list(text)
    for start, end in metadata_spans:
        _blank(parser_text, start, end)
    parser_input = "".join(parser_text)
    _blank_invalid_html_tags(parser_text, parser_input, tags)
    for match in HTML_TAG.finditer(parser_input):
        if is_escaped(text, match.start()):
            parser_text[match.start()] = ' '
    parser_input = ''.join(parser_text)

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
            if is_escaped(text, self._offset()):
                return
            for index in range(len(self.open_tags) - 1, -1, -1):
                if self.open_tags[index][0] != folded:
                    continue
                _tag, start = self.open_tags.pop(index)
                end = text.find(">", self._offset())
                self.spans.append((start, len(text) if end < 0 else end + 1))
                break

    parser = ElementParser()
    parser.feed(parser_input)
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


def _html_literal_parts(
    content: str,
    *,
    container: tuple[tuple[str, int], ...] | None = None,
) -> tuple[tuple[tuple[str, int], ...], re.Match[str]] | None:
    if container is None:
        remainder, container = _container_prefix(content)
    else:
        remainder = _container_body(content, container)
        if remainder is None:
            return None
    opening = HTML_LITERAL_OPEN.match(remainder)
    if opening is None or _indent_columns(remainder[: opening.start('tag') - 1]) > 3:
        return None
    return container, opening


def _is_html_literal_open(content: str) -> bool:
    return _html_literal_parts(content) is not None


def _html_literal_close(content: str, tag: str) -> re.Match[str] | None:
    return next(
        (
            match
            for match in HTML_LITERAL_CLOSE.finditer(content)
            if match.group("tag").casefold() == tag
        ),
        None,
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
    indentation = _indent_columns(content[:len(content) - len(content.lstrip(' \t'))])
    if not content.strip() or indentation >= 4:
        return False
    if _fence_parts(content) is not None:
        return False
    stripped = content.lstrip(" \t")
    if stripped.startswith(">"):
        return False
    if ATX_HEADING_LINE.match(content):
        return False
    if LIST_ITEM.match(content) is not None:
        return False
    if THEMATIC_BREAK.fullmatch(stripped) or re.fullmatch(r'=+[ \t]*', stripped):
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
                closing = _html_literal_close(content, html_literal)
                if closing is not None:
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
        html_open = None
        for _indentation, content_indent in reversed(list_contexts):
            html_open = _html_literal_parts(
                content, container=(('list', content_indent),)
            )
            if html_open is not None:
                break
        if html_open is None:
            html_open = _html_literal_parts(content)
        if html_open is not None:
            _blank(chars, start, end)
            container, opening = html_open
            closing = _html_literal_close(content, opening.group('tag').casefold())
            if closing is None:
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
            indentation >= 4
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
                paragraph_active = _is_paragraph_line(content[list_item.end():])
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
