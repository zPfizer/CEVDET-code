#!/usr/bin/env python3
"""Bounded JSONL reading for the transcript consumers.

One owner for the tail-window arithmetic, the partial-first-line drop, the
allocation cap that keeps a newline-free giant line from ever being
materialized, and decode/parse. The caller opens (and, for cursor readers,
seeks) its own handle, keeps its own record filter, and picks its own
malformed/oversize policy: no error template means skip a completed malformed
line, a template means raise ``ValueError`` with that message. An incomplete
newline-free EOF row raises ``IncompleteRecord`` so it can be retried.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, NamedTuple


class TailWindow(NamedTuple):
    """Truncation receipt for a tail seek."""

    start: int
    scanned_bytes: int
    truncated: bool


class IncompleteRecord(ValueError):
    """A newline-free final row that may become valid when the writer resumes."""

    __slots__ = ("line", "offset", "reason")

    def __init__(self, line: int, offset: int, reason: str = "eof") -> None:
        self.line = line
        self.offset = offset
        self.reason = reason
        super().__init__(f"jsonl-incomplete:{line}")


class LineSpan(NamedTuple):
    """A bounded raw line with its byte position in the source."""

    line_number: int
    offset: int
    raw: bytes


def _drop_line(source: Any, max_line_bytes: int) -> None:
    """Consume the rest of the current line without materializing it."""
    while True:
        chunk = source.readline(max_line_bytes)
        if not chunk or chunk.endswith(b"\n"):
            return


def seek_tail(
    source: Any,
    size: int,
    max_bytes: int,
    *,
    max_line_bytes: int,
) -> TailWindow:
    """Seek to the last ``max_bytes`` of ``source``, dropping the partial line."""
    start = max(0, size - max_bytes)
    source.seek(start)
    if start:
        source.seek(start - 1)
        boundary = source.readline(max_line_bytes + 1) == b"\n"
        source.seek(start)
        if not boundary:
            _drop_line(source, max_line_bytes)
    return TailWindow(start, size - start, start > 0)


def _is_incomplete_eof(
    raw: bytes,
    error: UnicodeDecodeError | json.JSONDecodeError,
) -> bool:
    """A newline marks a finished row; an undecided EOF row waits for append."""
    if raw.endswith(b"\n") or not raw.strip():
        return False
    if isinstance(error, UnicodeDecodeError):
        return error.reason == "unexpected end of data" and error.end >= len(raw) - 3
    return True


def iter_records(
    source: Any,
    *,
    max_line_bytes: int,
    skip_blank: bool = False,
    require_newline: bool = False,
    oversize_error: str | None = None,
    malformed_error: str | None = None,
) -> Iterator[tuple[int, Any]]:
    """Yield ``(line_number, record)`` for parseable lines from the current position.

    ``line_number`` counts every line this pass reads, skipped ones included.
    Error templates take ``{line}``. ``require_newline`` leaves a partial last
    line unread so a cursor caller can resume on it after the writer finishes.
    """
    for span in iter_line_spans(
        source,
        max_line_bytes=max_line_bytes,
        require_newline=require_newline,
        oversize_error=oversize_error,
    ):
        line, _offset, raw = span
        if skip_blank and not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if _is_incomplete_eof(raw, exc):
                reason = "utf8" if isinstance(exc, UnicodeDecodeError) else "json"
                raise IncompleteRecord(line, span.offset, reason) from exc
            if malformed_error is not None:
                raise ValueError(malformed_error.format(line=line)) from exc
            continue
        yield line, record


def iter_line_spans(
    source: Any,
    *,
    max_line_bytes: int,
    line_number_start: int = 0,
    require_newline: bool = False,
    oversize_error: str | None = None,
) -> Iterator[LineSpan]:
    """Yield bounded raw lines while retaining byte offsets for an index."""
    line = line_number_start
    while True:
        line_start = source.tell()
        raw = source.readline(max_line_bytes + 1)
        if not raw:
            return
        line += 1
        if len(raw) > max_line_bytes:
            if oversize_error is not None:
                raise ValueError(oversize_error.format(line=line))
            if not raw.endswith(b"\n"):
                _drop_line(source, max_line_bytes)
            continue
        if require_newline and not raw.endswith(b"\n"):
            source.seek(line_start)
            return
        yield LineSpan(line, line_start, raw)
