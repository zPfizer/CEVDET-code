"""Metadata-only, cross-process indexing for append-only JSONL transcripts."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, cast

from jsonl_tail import _is_incomplete_eof, iter_line_spans
from memory_ledger import (
    PERSISTENT_TURNS_VERSION,
    PersistentTurnReducer,
    sanitize_text,
)
from state_store import atomic_write_json


INDEX_SCHEMA_VERSION = 1
COVERAGE_SCHEMA_VERSION = 4
PARSER_VERSION = "codex-message-envelope-v1"
POLICY_VERSION = PERSISTENT_TURNS_VERSION
CHUNK_CHARS = 12_000
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ROLES = {"user", "assistant"}
_CLASSIFICATIONS = {
    "blank",
    "ignored",
    "unsupported",
    "ordinary",
    "empty",
    "suppressed",
    "reply-suppressed",
    "secret",
    "forget",
    "forget-ambiguous",
    "do-not-save",
    "what-known",
    "session-only",
    "session-only-cleared",
    "removed-by-do-not-save",
}


@dataclass(frozen=True)
class ChunkRef:
    row_index: int
    chunk_index: int
    sha256: str
    length: int


@dataclass(frozen=True)
class TranscriptIndex:
    """Validated index metadata and the source identity it describes."""

    path: Path
    source_path: Path
    state: dict[str, Any]
    _rows: tuple[dict[str, Any], ...] = field(init=False, repr=False, compare=False)
    _chunks: tuple[ChunkRef, ...] = field(init=False, repr=False, compare=False)
    _coverage_chain: tuple[bytes, ...] = field(init=False, repr=False, compare=False)
    _scan_reused: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        rows = tuple(self.state["rows"])
        chunks = tuple(
            ChunkRef(row_index, chunk_index, chunk["sha256"], chunk["length"])
            for row_index, row in enumerate(rows)
            for chunk_index, chunk in enumerate(row["filtered_chunks"])
        )
        object.__setattr__(self, "_rows", rows)
        object.__setattr__(self, "_chunks", chunks)
        chain = [_coverage_seed(self.state)]
        for chunk in chunks:
            row = rows[chunk.row_index]
            chain.append(
                hashlib.sha256(
                    chain[-1] + _coverage_token(row, chunk)
                ).digest()
            )
        object.__setattr__(self, "_coverage_chain", tuple(chain))

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        return self._rows

    @property
    def counters(self) -> dict[str, int]:
        counters = dict(self.state["counters"])
        if self._scan_reused:
            counters.update({
                "json_decodes_last": 0,
                "reused_rows_last": len(self._rows),
            })
        return counters

    @property
    def indexed_bytes(self) -> int:
        return cast(int, self.state["indexed_bytes"])

    @property
    def partial_tail(self) -> dict[str, Any] | None:
        value = self.state["partial_tail"]
        return dict(value) if isinstance(value, dict) else None

    @property
    def session_only(self) -> bool:
        return self.state["reducer"]["session_only"] is True

    @property
    def chunks(self) -> tuple[ChunkRef, ...]:
        return self._chunks

    def coverage_digest(self, count: int) -> str:
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count < len(self._coverage_chain):
            raise ValueError("transcript-index-coverage-invalid")
        return self._coverage_chain[count].hex()

    def previous_retained_row(self, row_index: int) -> int | None:
        if not isinstance(row_index, int) or not 0 <= row_index < len(self.rows):
            raise ValueError("transcript-index-row-invalid")
        for index in range(row_index - 1, -1, -1):
            if self.rows[index]["retained"]:
                return index
        return None

    def verify_source_current(self) -> None:
        source_bytes, source_sha256, prefix_sha256 = _hash_source(
            self.source_path, self.state["indexed_bytes"]
        )
        if (
            source_bytes != self.state["source_bytes"]
            or source_sha256 != self.state["source_sha256"]
            or prefix_sha256 != self.state["prefix_sha256"]
        ):
            raise ValueError("transcript-index-source-drift")


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _coverage_seed(state: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        "\0".join(
            (
                "coverage",
                str(COVERAGE_SCHEMA_VERSION),
                state["parser_version"],
                state["policy_version"],
                state["suppression_revision"],
            )
        ).encode("ascii")
    ).digest()


def _coverage_token(row: dict[str, Any], chunk: ChunkRef) -> bytes:
    return "\0".join(
        (
            str(chunk.row_index),
            str(chunk.chunk_index),
            row["role"] or "none",
            row["privacy_classification"],
            chunk.sha256,
            str(chunk.length),
        )
    ).encode("ascii")


def _bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_identity(path: Path) -> str:
    """Hash a normalized source identity; the raw path never enters state."""
    identity = os.path.normcase(os.path.abspath(os.fspath(path)))
    return _bytes_hash(identity.encode("utf-8"))


def index_path(state_dir: Path, session_id: str) -> Path:
    return state_dir / f"flush-index-{_bytes_hash(session_id.encode('utf-8'))}.json"


def suppression_revision(hashes: frozenset[str]) -> str:
    return _bytes_hash("\0".join(sorted(hashes)).encode("ascii"))


def _hash_source(path: Path, prefix_length: int) -> tuple[int, str, str]:
    if not isinstance(prefix_length, int) or isinstance(prefix_length, bool) or prefix_length < 0:
        raise ValueError("transcript-index-prefix-invalid")
    before = path.stat()
    full = hashlib.sha256()
    prefix = hashlib.sha256()
    remaining = prefix_length
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            full.update(chunk)
            if remaining:
                prefix.update(chunk[:remaining])
                remaining -= min(remaining, len(chunk))
    after = path.stat()
    if size != before.st_size or after.st_size != before.st_size:
        raise ValueError("transcript-source-changed-during-read")
    if remaining:
        # A prefix longer than the file is invalid cache state; the full digest
        # already represents the only bytes available for that prefix.
        prefix = full.copy()
    return size, full.hexdigest(), prefix.hexdigest()


def _partial_tail_matches(path: Path, partial: dict[str, Any] | None) -> bool:
    if partial is None:
        return True
    try:
        with path.open("rb") as source:
            source.seek(partial["offset"])
            raw = source.read(partial["line_bytes"])
    except OSError:
        return False
    return bool(len(raw) == partial["line_bytes"] and _bytes_hash(raw) == partial["sha256"])


def _metadata_digest(value: dict[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "metadata_sha256"}
    return _json_hash(payload)


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _valid_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("transcript-index-state-invalid")
    required = {
        "schema_version",
        "parser_version",
        "policy_version",
        "source_identity_sha256",
        "source_sha256",
        "prefix_sha256",
        "source_bytes",
        "indexed_bytes",
        "partial_tail",
        "suppression_revision",
        "rows",
        "counters",
        "reducer",
        "metadata_sha256",
    }
    if set(value) != required or value.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise ValueError("transcript-index-state-invalid")
    if (
        value.get("parser_version") != PARSER_VERSION
        or value.get("policy_version") != POLICY_VERSION
        or not all(_valid_hash(value.get(key)) for key in (
            "source_identity_sha256",
            "source_sha256",
            "prefix_sha256",
            "suppression_revision",
            "metadata_sha256",
        ))
        or not _valid_nonnegative_int(value.get("source_bytes"))
        or not _valid_nonnegative_int(value.get("indexed_bytes"))
        or value["indexed_bytes"] > value["source_bytes"]
        or _metadata_digest(value) != value["metadata_sha256"]
    ):
        raise ValueError("transcript-index-state-invalid")

    partial = value["partial_tail"]
    if partial is not None:
        if (
            not isinstance(partial, dict)
            or set(partial) != {"offset", "line_bytes", "sha256", "complete"}
            or not _valid_nonnegative_int(partial.get("offset"))
            or not _valid_nonnegative_int(partial.get("line_bytes"))
            or not _valid_hash(partial.get("sha256"))
            or type(partial.get("complete")) is not bool
            or partial["offset"] >= value["source_bytes"]
            or partial["offset"] + partial["line_bytes"] != value["source_bytes"]
        ):
            raise ValueError("transcript-index-partial-tail-invalid")
        if partial["complete"]:
            if partial["offset"] >= value["indexed_bytes"]:
                raise ValueError("transcript-index-partial-tail-invalid")
        elif partial["offset"] != value["indexed_bytes"]:
            raise ValueError("transcript-index-partial-tail-invalid")
    elif value["indexed_bytes"] != value["source_bytes"]:
        raise ValueError("transcript-index-partial-tail-invalid")

    rows = value["rows"]
    if not isinstance(rows, list):
        raise ValueError("transcript-index-rows-invalid")
    offset = 0
    message_envelopes = recognized_messages = retained_turns = chunk_count = 0
    expected_retained: list[list[Any]] = []
    for row_id, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "offset",
            "line_bytes",
            "line_sha256",
            "role",
            "privacy_classification",
            "filtered_chunks",
            "filtered_length",
            "message_envelope",
            "retained",
            "skip_reply_after",
        }:
            raise ValueError("transcript-index-row-invalid")
        if (
            row.get("offset") != offset
            or not _valid_nonnegative_int(row.get("line_bytes"))
            or row["line_bytes"] < 1
            or not _valid_hash(row.get("line_sha256"))
            or row.get("role") not in (None, "user", "assistant")
            or not isinstance(row.get("privacy_classification"), str)
            or row["privacy_classification"] not in _CLASSIFICATIONS
            or not isinstance(row.get("filtered_chunks"), list)
            or not _valid_nonnegative_int(row.get("filtered_length"))
            or type(row.get("message_envelope")) is not bool
            or type(row.get("retained")) is not bool
            or type(row.get("skip_reply_after")) is not bool
        ):
            raise ValueError("transcript-index-row-invalid")
        expected_length = 0
        for chunk in row["filtered_chunks"]:
            if (
                not isinstance(chunk, dict)
                or set(chunk) != {"sha256", "length"}
                or not _valid_hash(chunk.get("sha256"))
                or not isinstance(chunk.get("length"), int)
                or isinstance(chunk["length"], bool)
                or not 0 < chunk["length"] <= CHUNK_CHARS
            ):
                raise ValueError("transcript-index-chunk-invalid")
            expected_length += chunk["length"]
        if (
            expected_length != row["filtered_length"]
            or row["retained"] != bool(row["filtered_chunks"])
            or (row["retained"] and row["role"] not in _ROLES)
        ):
            raise ValueError("transcript-index-row-invalid")
        message_envelopes += int(row["message_envelope"])
        recognized_messages += int(row["role"] in _ROLES)
        retained_turns += int(row["retained"])
        chunk_count += len(row["filtered_chunks"])
        if row["retained"]:
            expected_retained.append([row_id, row["role"]])
        offset += row["line_bytes"]
    if offset != value["indexed_bytes"]:
        if not (
            value["partial_tail"] is not None
            and value["partial_tail"]["complete"]
            and offset == value["source_bytes"] == value["indexed_bytes"]
        ):
            raise ValueError("transcript-index-offsets-invalid")

    counters = value["counters"]
    if not isinstance(counters, dict) or set(counters) != {
        "physical_lines",
        "message_envelopes",
        "recognized_messages",
        "retained_turns",
        "chunk_count",
        "partial_lines",
        "json_decodes_last",
        "json_decodes_total",
        "reused_rows_last",
    } or any(not _valid_nonnegative_int(counters.get(key)) for key in counters):
        raise ValueError("transcript-index-counters-invalid")
    expected = {
        "physical_lines": len(rows),
        "message_envelopes": message_envelopes,
        "recognized_messages": recognized_messages,
        "retained_turns": retained_turns,
        "chunk_count": chunk_count,
        "partial_lines": int(partial is not None and not partial["complete"]),
    }
    if any(counters[key] != expected[key] for key in expected):
        raise ValueError("transcript-index-counters-invalid")

    reducer = value["reducer"]
    if not isinstance(reducer, dict) or set(reducer) != {
        "skip_reply",
        "session_only",
        "retained_rows",
    } or type(reducer.get("skip_reply")) is not bool or type(reducer.get("session_only")) is not bool:
        raise ValueError("transcript-index-reducer-invalid")
    retained_rows = reducer["retained_rows"]
    if not isinstance(retained_rows, list):
        raise ValueError("transcript-index-reducer-invalid")
    if retained_rows != expected_retained:
        raise ValueError("transcript-index-reducer-invalid")
    if reducer["session_only"] and expected_retained:
        raise ValueError("transcript-index-reducer-invalid")
    return value


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return _validate_state(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def _new_row(span: Any) -> dict[str, Any]:
    return {
        "offset": span.offset,
        "line_bytes": len(span.raw),
        "line_sha256": _bytes_hash(span.raw),
        "role": None,
        "privacy_classification": "ignored",
        "filtered_chunks": [],
        "filtered_length": 0,
        "message_envelope": False,
        "retained": False,
        "skip_reply_after": False,
    }


def _chunks(text: str) -> list[dict[str, Any]]:
    return [
        {
            "sha256": _bytes_hash(part.encode("utf-8")),
            "length": len(part),
        }
        for offset in range(0, len(text), CHUNK_CHARS)
        for part in (text[offset : offset + CHUNK_CHARS],)
    ]


def _apply_decision(
    rows: list[dict[str, Any]],
    row: dict[str, Any],
    decision: Any,
    reducer: PersistentTurnReducer,
) -> None:
    for row_id in decision.removed_row_ids:
        previous = rows[row_id]
        previous["retained"] = False
        previous["filtered_chunks"] = []
        previous["filtered_length"] = 0
        previous["privacy_classification"] = (
            "session-only-cleared"
            if reducer.session_only
            else "removed-by-do-not-save"
        )
    safe = ""
    if decision.keep:
        safe = sanitize_text(
            decision.visible_text,
            max_chars=max(1, len(decision.visible_text)),
        )[0]
    row.update(
        {
            "role": decision.role,
            "privacy_classification": decision.classification,
            "filtered_chunks": _chunks(safe) if safe.strip() else [],
            "filtered_length": len(safe) if safe.strip() else 0,
            "retained": bool(safe.strip()),
            "message_envelope": True,
            "skip_reply_after": reducer.skip_reply,
        }
    )


def _process_span(
    span: Any,
    *,
    is_last: bool,
    rows: list[dict[str, Any]],
    reducer: PersistentTurnReducer,
    parser: Callable[[dict[str, Any]], tuple[str | None, Any, bool]],
    text_from_content: Callable[[Any], str],
) -> dict[str, Any] | None:
    row = _new_row(span)
    if not span.raw.strip():
        row["privacy_classification"] = "blank"
        row["skip_reply_after"] = reducer.skip_reply
        rows.append(row)
        return None
    try:
        record = json.loads(span.raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if is_last and _is_incomplete_eof(span.raw, exc):
            return {
                "offset": span.offset,
                "line_bytes": len(span.raw),
                "sha256": _bytes_hash(span.raw),
                "complete": False,
            }
        raise ValueError(f"transcript-jsonl-invalid:{len(rows) + 1}") from exc
    if not isinstance(record, dict):
        row["privacy_classification"] = "ignored"
        row["skip_reply_after"] = reducer.skip_reply
        rows.append(row)
        return None
    role, content, envelope = parser(record)
    envelope = bool(envelope)
    row["message_envelope"] = envelope
    if role not in _ROLES:
        row["privacy_classification"] = "unsupported" if envelope else "ignored"
        row["skip_reply_after"] = reducer.skip_reply
        rows.append(row)
        return None
    try:
        text = text_from_content(content)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"transcript-jsonl-invalid:{len(rows) + 1}") from exc
    if not isinstance(text, str):
        text = ""
    decision = reducer.add(role, text.rstrip(), row_id=len(rows))
    rows.append(row)
    _apply_decision(rows, row, decision, reducer)
    if is_last and not span.raw.endswith(b"\n"):
        return {
            "offset": span.offset,
            "line_bytes": len(span.raw),
            "sha256": _bytes_hash(span.raw),
            "complete": True,
        }
    return None


def _parse_from(
    source_path: Path,
    start: int,
    rows: list[dict[str, Any]],
    reducer: PersistentTurnReducer,
    *,
    parser: Callable[[dict[str, Any]], tuple[str | None, Any, bool]],
    text_from_content: Callable[[Any], str],
    max_line_bytes: int,
) -> tuple[dict[str, Any] | None, int]:
    source_path.stat()
    with source_path.open("rb") as source:
        source.seek(start)
        spans = iter_line_spans(
            source,
            max_line_bytes=max_line_bytes,
            line_number_start=len(rows),
            oversize_error="transcript-line-too-large:{line}",
        )
        try:
            current = next(spans)
        except StopIteration:
            return None, 0
        partial: dict[str, Any] | None = None
        decoded = 0
        while True:
            try:
                following = next(spans)
            except StopIteration:
                following = None
            raw = current.raw
            if raw.strip():
                decoded += 1
            partial = _process_span(
                current,
                is_last=following is None,
                rows=rows,
                reducer=reducer,
                parser=parser,
                text_from_content=text_from_content,
            )
            if partial is not None:
                break
            if following is None:
                break
            current = following
    return partial, decoded


def _clear_retention(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row["retained"]:
            row["retained"] = False
            row["filtered_chunks"] = []
            row["filtered_length"] = 0
            row["privacy_classification"] = "session-only-cleared"


def _state(
    *,
    source_path: Path,
    source_bytes: int,
    source_sha256: str,
    prefix_sha256: str,
    indexed_bytes: int,
    partial_tail: dict[str, Any] | None,
    hashes: frozenset[str],
    rows: list[dict[str, Any]],
    reducer: PersistentTurnReducer,
    json_decodes_last: int,
    json_decodes_total: int,
    reused_rows_last: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "policy_version": POLICY_VERSION,
        "source_identity_sha256": source_identity(source_path),
        "source_sha256": source_sha256,
        "prefix_sha256": prefix_sha256,
        "source_bytes": source_bytes,
        "indexed_bytes": indexed_bytes,
        "partial_tail": partial_tail,
        "suppression_revision": suppression_revision(hashes),
        "rows": rows,
        "counters": {
            "physical_lines": len(rows),
            "message_envelopes": sum(row["message_envelope"] for row in rows),
            "recognized_messages": sum(row["role"] in _ROLES for row in rows),
            "retained_turns": sum(row["retained"] for row in rows),
            "chunk_count": sum(len(row["filtered_chunks"]) for row in rows),
            "partial_lines": int(partial_tail is not None and not partial_tail["complete"]),
            "json_decodes_last": json_decodes_last,
            "json_decodes_total": json_decodes_total,
            "reused_rows_last": reused_rows_last,
        },
        "reducer": {
            "skip_reply": reducer.skip_reply,
            "session_only": reducer.session_only,
            "retained_rows": [
                [row_id, rows[row_id]["role"]]
                for row_id in reducer.retained_row_ids
            ],
        },
    }
    payload["metadata_sha256"] = _metadata_digest(payload)
    return payload


def _write_index(path: Path, payload: dict[str, Any]) -> None:
    try:
        atomic_write_json(path, payload, sort_keys=True)
    except OSError as exc:
        raise ValueError("transcript-index-write-failed") from exc


def open_or_update(
    state_dir: Path,
    session_id: str,
    source_path: Path,
    *,
    hashes: frozenset[str],
    parser: Callable[[dict[str, Any]], tuple[str | None, Any, bool]],
    text_from_content: Callable[[Any], str],
    max_line_bytes: int,
    force_session_only: bool = False,
) -> TranscriptIndex:
    """Load metadata or parse only an appended tail, then atomically persist it.

    Source hashing and metadata validation remain O(N); source JSON decoding is
    the work this index avoids for unchanged prefixes.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("transcript-index-session-invalid")
    if not isinstance(source_path, Path):
        source_path = Path(source_path)
    if not isinstance(max_line_bytes, int) or isinstance(max_line_bytes, bool) or max_line_bytes < 1:
        raise ValueError("transcript-index-line-budget-invalid")
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError("transcript-index-write-failed") from exc
    path = index_path(state_dir, session_id)
    cached = _load_cache(path) or {}
    rows: list[dict[str, Any]]
    source_bytes = source_path.stat().st_size
    cache_usable = bool(cached)
    if cache_usable:
        if (
            cached["source_identity_sha256"] != source_identity(source_path)
            or cached["suppression_revision"] != suppression_revision(hashes)
            or cached["indexed_bytes"] > source_bytes
            or any(row["line_bytes"] > max_line_bytes for row in cached["rows"])
            or not _partial_tail_matches(source_path, cached["partial_tail"])
        ):
            cache_usable = False
    prefix_length = cached["indexed_bytes"] if cache_usable else 0
    current_bytes, current_sha256, current_prefix_sha256 = _hash_source(
        source_path, prefix_length
    )
    if cache_usable:
        if (
            current_bytes < cached["indexed_bytes"]
            or current_prefix_sha256 != cached["prefix_sha256"]
            or (current_bytes == cached["indexed_bytes"] and current_sha256 != cached["source_sha256"])
            or (
                cached["partial_tail"] is not None
                and cached["partial_tail"]["complete"]
                and current_bytes > cached["source_bytes"]
            )
        ):
            cache_usable = False

    if cache_usable and current_bytes == cached["source_bytes"] and current_sha256 == cached["source_sha256"]:
        if force_session_only and not cached["reducer"]["session_only"]:
            rows = cached["rows"]
            _clear_retention(rows)
            reducer = PersistentTurnReducer(hashes, session_only=True)
            payload = _state(
                source_path=source_path,
                source_bytes=current_bytes,
                source_sha256=current_sha256,
                prefix_sha256=current_prefix_sha256,
                indexed_bytes=cached["indexed_bytes"],
                partial_tail=cached["partial_tail"],
                hashes=hashes,
                rows=rows,
                reducer=reducer,
                json_decodes_last=0,
                json_decodes_total=cached["counters"]["json_decodes_total"],
                reused_rows_last=len(rows),
            )
            _validate_state(payload)
            _write_index(path, payload)
            return TranscriptIndex(path, source_path, payload)
        return TranscriptIndex(path, source_path, cached, _scan_reused=True)

    if not cache_usable:
        rows = []
        reducer = PersistentTurnReducer(hashes)
        start = 0
        previous_decodes = 0
        reused_rows = 0
    else:
        rows = cached["rows"]
        reducer = PersistentTurnReducer(
            hashes,
            retained_rows=[tuple(item) for item in cached["reducer"]["retained_rows"]],
            skip_reply=cached["reducer"]["skip_reply"],
            session_only=cached["reducer"]["session_only"],
        )
        if force_session_only and not reducer.session_only:
            _clear_retention(rows)
            reducer = PersistentTurnReducer(hashes, session_only=True)
        partial = cached["partial_tail"]
        start = partial["offset"] if partial is not None else cached["indexed_bytes"]
        previous_decodes = cached["counters"]["json_decodes_total"]
        reused_rows = len(rows)

    partial_tail, decoded = _parse_from(
        source_path,
        start,
        rows,
        reducer,
        parser=parser,
        text_from_content=text_from_content,
        max_line_bytes=max_line_bytes,
    )
    if force_session_only and not reducer.session_only:
        _clear_retention(rows)
        reducer = PersistentTurnReducer(hashes, session_only=True)
    indexed_bytes = (
        partial_tail["offset"]
        if partial_tail is not None and not partial_tail["complete"]
        else current_bytes
    )
    final_bytes, final_sha256, final_prefix = _hash_source(source_path, indexed_bytes)
    if final_bytes != current_bytes or final_sha256 != current_sha256:
        raise ValueError("transcript-source-changed-during-read")
    payload = _state(
        source_path=source_path,
        source_bytes=final_bytes,
        source_sha256=final_sha256,
        prefix_sha256=final_prefix,
        indexed_bytes=indexed_bytes,
        partial_tail=partial_tail,
        hashes=hashes,
        rows=rows,
        reducer=reducer,
        json_decodes_last=decoded,
        json_decodes_total=previous_decodes + decoded,
        reused_rows_last=reused_rows,
    )
    _validate_state(payload)
    _write_index(path, payload)
    return TranscriptIndex(path, source_path, payload)


def read_selected_rows(
    index: TranscriptIndex,
    source_path: Path,
    row_indexes: list[int] | tuple[int, ...],
    *,
    hashes: frozenset[str],
    parser: Callable[[dict[str, Any]], tuple[str | None, Any, bool]],
    text_from_content: Callable[[Any], str],
    max_line_bytes: int,
    verify_source: bool = True,
) -> dict[int, tuple[str, str]]:
    """Read and privacy-check only source rows needed by the current batch."""
    if source_identity(source_path) != index.state["source_identity_sha256"]:
        raise ValueError("transcript-index-source-identity-drift")
    if verify_source:
        index.verify_source_current()
    wanted = tuple(dict.fromkeys(row_indexes))
    if any(not isinstance(row_id, int) or isinstance(row_id, bool) or not 0 <= row_id < len(index.rows) for row_id in wanted):
        raise ValueError("transcript-index-row-invalid")
    result: dict[int, tuple[str, str]] = {}
    with source_path.open("rb") as source:
        for row_id in wanted:
            row = index.rows[row_id]
            source.seek(row["offset"])
            raw = source.read(row["line_bytes"])
            if len(raw) != row["line_bytes"] or _bytes_hash(raw) != row["line_sha256"]:
                raise ValueError("transcript-index-row-drift")
            if len(raw) > max_line_bytes:
                raise ValueError("transcript-line-too-large:selected")
            try:
                record = json.loads(raw.decode("utf-8"))
                role, content, envelope = parser(record) if isinstance(record, dict) else (None, None, False)
                text = text_from_content(content)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError("transcript-index-privacy-drift") from exc
            if role not in _ROLES or not envelope or role != row["role"] or not isinstance(text, str):
                raise ValueError("transcript-index-privacy-drift")
            reducer = PersistentTurnReducer(hashes)
            decision = reducer.add(role, text.rstrip(), row_id=row_id)
            safe = sanitize_text(
                decision.visible_text,
                max_chars=max(1, len(decision.visible_text)),
            )[0]
            expected_chunks = _chunks(safe) if decision.keep and safe.strip() else []
            if (
                not decision.keep
                or decision.classification != row["privacy_classification"]
                or expected_chunks != row["filtered_chunks"]
                or len(safe) != row["filtered_length"]
            ):
                raise ValueError("transcript-index-privacy-drift")
            result[row_id] = (role, safe)
    return result
