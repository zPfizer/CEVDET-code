#!/usr/bin/env python3
"""compile-state.json sözleşmesinin tek sahibi: şema, digest ve günlük tarama politikası.

compile/flush/checkpoint/doctor bu modülün caller'ıdır; cursor'ı kimse kendi
parser'ıyla okumaz. Kanonik hata `PolicyError` (ValueError alt sınıfı, böylece
`except ValueError` kuran mevcut caller'ların tel davranışı korunur).
Modül bilerek hafiftir (stdlib + state_store): flush ve checkpoint her hook
çalıştırmasında derleyicinin ağır import zincirini ödemez.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
from pathlib import Path
import re
import stat
from typing import Any, Iterator

from state_store import atomic_write_json, sha256_file, state_dir_of


STATE_NAME = "compile-state.json"
PUBLICATION_NAME = "compile-publication.json"
PUBLICATION_SCHEMA_VERSION = 1
PUBLICATION_TOKEN_NAME = "compile-publication-token.json"
PUBLICATION_TOKEN_SCHEMA_VERSION = 1
PUBLICATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_PUBLICATION_JSON_BYTES = 4 * 1024 * 1024
_MAX_PUBLICATION_TOKEN_JSON_BYTES = 4 * 1024
MAX_RUNS = 20
DATE_IN_NAME = re.compile(
    r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{2})"
    r"(?:-(?P<day>\d{2}))?(?!\d)"
)


class PolicyError(ValueError):
    """Compile cursor'ı ya da günlük kaynağı derleyici sınırını ihlal etti."""


@dataclass(frozen=True)
class PublicationSnapshot:
    pending: bool
    publication_id: str | None


@dataclass
class CompileState:
    """Doğrulanmış compile cursor'ı; diskteki tek şema budur."""

    ingested: dict[str, str] = field(default_factory=dict)
    cursor: str = ""
    last_run: str = ""
    last_status: str = "ok"
    runs: list[dict[str, Any]] = field(default_factory=list)

    def append_run(self, timestamp: str, daily_name: str, status: str) -> None:
        self.runs = [
            *self.runs,
            {"ts": timestamp, "daily_file": daily_name, "status": status},
        ][-MAX_RUNS:]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ingested": self.ingested,
            "cursor": self.cursor,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "runs": self.runs[-MAX_RUNS:],
        }


def state_file(state_dir: Path) -> Path:
    return Path(state_dir) / STATE_NAME


def publication_file(state_dir: Path) -> Path:
    return Path(state_dir) / PUBLICATION_NAME


def publication_token_file(state_dir: Path) -> Path:
    return Path(state_dir) / PUBLICATION_TOKEN_NAME


def _read_bounded_json(path: Path, limit: int, error: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = handle.read(limit + 1)
    except UnicodeError as exc:
        raise PolicyError(f"{error}-unreadable") from exc
    if len(raw) > limit:
        raise PolicyError(f"{error}-too-large")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{error}-unreadable") from exc


def _ensure_json_size(payload: dict[str, Any], limit: int, error: str) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        separators=None,
        sort_keys=True,
    )
    if len((encoded + "\n").encode("utf-8")) > limit:
        raise PolicyError(error)


def load_publication(state_dir: Path) -> dict[str, Any] | None:
    """Read the small publication journal; malformed state fails closed."""
    path = publication_file(state_dir)
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise PolicyError("publication-journal-invalid")
        value = _read_bounded_json(
            path,
            _MAX_PUBLICATION_JSON_BYTES,
            "publication-journal",
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PolicyError("publication-journal-unreadable") from exc
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    status = value.get("status") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or type(schema_version) is not int
        or schema_version != PUBLICATION_SCHEMA_VERSION
        or not isinstance(status, str)
        or status not in {"pending", "complete"}
    ):
        raise PolicyError("publication-journal-invalid")
    return value


def load_publication_token(state_dir: Path) -> str | None:
    path = publication_token_file(state_dir)
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise PolicyError("publication-token-invalid")
        value = _read_bounded_json(
            path,
            _MAX_PUBLICATION_TOKEN_JSON_BYTES,
            "publication-token",
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PolicyError("publication-token-unreadable") from exc
    publication_id = value.get("publication_id") if isinstance(value, dict) else None
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or type(schema_version) is not int
        or schema_version != PUBLICATION_TOKEN_SCHEMA_VERSION
        or not isinstance(publication_id, str)
        or PUBLICATION_ID.fullmatch(publication_id) is None
    ):
        raise PolicyError("publication-token-invalid")
    return publication_id


def save_publication_token(state_dir: Path, publication_id: str) -> None:
    if PUBLICATION_ID.fullmatch(publication_id) is None:
        raise PolicyError("publication-token-invalid")
    payload = {
        "schema_version": PUBLICATION_TOKEN_SCHEMA_VERSION,
        "publication_id": publication_id,
    }
    _ensure_json_size(
        payload,
        _MAX_PUBLICATION_TOKEN_JSON_BYTES,
        "publication-token-too-large",
    )
    atomic_write_json(publication_token_file(state_dir), payload, sort_keys=True)


def publication_snapshot(vault_root: Path) -> PublicationSnapshot:
    state_dir = state_dir_of(Path(vault_root))
    pending = load_publication(state_dir) is not None
    publication_id = load_publication_token(state_dir)
    pending_after_token = load_publication(state_dir) is not None
    return PublicationSnapshot(
        pending=pending or pending_after_token,
        publication_id=publication_id,
    )


def save_publication(state_dir: Path, journal: dict[str, Any]) -> None:
    _ensure_json_size(
        journal,
        _MAX_PUBLICATION_JSON_BYTES,
        "publication-journal-too-large",
    )
    atomic_write_json(
        publication_file(state_dir),
        journal,
        indent=2,
        separators=None,
        sort_keys=True,
    )


def clear_publication(state_dir: Path) -> None:
    path = publication_file(state_dir)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise PolicyError("publication-journal-invalid")
    path.unlink()


def require_publication_snapshot(
    vault_root: Path, expected: PublicationSnapshot | None = None,
) -> PublicationSnapshot:
    """Fence readers against both partial publication and completed generation changes."""
    current = publication_snapshot(vault_root)
    if current.pending:
        raise PolicyError("publication-pending")
    if expected is not None and current != expected:
        raise PolicyError("publication-changed")
    return current


def assert_publication_ready(vault_root: Path) -> None:
    require_publication_snapshot(vault_root)


def load(state_dir: Path) -> CompileState:
    """Cursor'ı doğrulanmış okur; dosya yoksa boş cursor, bozuksa `PolicyError`."""
    try:
        raw = state_file(state_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        return CompileState()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError("compile-state-unreadable") from exc
    if not isinstance(value, dict):
        raise PolicyError("compile-state-not-object")
    state = CompileState(
        ingested=value.get("ingested", {}),
        cursor=value.get("cursor", ""),
        last_run=value.get("last_run", ""),
        last_status=value.get("last_status", "ok"),
        runs=value.get("runs", []),
    )
    if (
        not isinstance(state.ingested, dict)
        or not isinstance(state.runs, list)
        or not isinstance(state.cursor, str)
    ):
        raise PolicyError("compile-state-schema-invalid")
    state.runs = state.runs[-MAX_RUNS:]
    return state


def save(state_dir: Path, state: CompileState) -> None:
    atomic_write_json(
        state_file(state_dir),
        state.as_dict(),
        indent=2,
        separators=None,
    )


def path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _daily_sort_key(path: Path) -> tuple[dt.date, str]:
    match = DATE_IN_NAME.search(path.stem)
    if match is None:
        return dt.date.max, path.name
    day = int(match.group("day") or "1")
    try:
        parsed = dt.date(
            int(match.group("year")),
            int(match.group("month")),
            day,
        )
    except ValueError:
        parsed = dt.date.max
    return parsed, path.name


def _iter_changed(
    vault_root: Path,
    ingested: dict[str, str],
) -> Iterator[tuple[Path, str]]:
    """Tek symlink/digest/sıralama politikası; `unsafe-daily-source` tek yerde."""
    daily_dir = Path(vault_root) / "daily"
    try:
        daily_stat = daily_dir.lstat()
    except FileNotFoundError:
        return
    if (stat.S_ISLNK(daily_stat.st_mode) or not stat.S_ISDIR(daily_stat.st_mode)
            or not daily_dir.exists()):
        raise PolicyError("unsafe-daily-directory")
    if not path_within(
        daily_dir.resolve(strict=True),
        Path(vault_root).resolve(strict=True),
    ):
        raise PolicyError("daily-directory-escape")
    for path in sorted(daily_dir.glob("*.md"), key=_daily_sort_key):
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise PolicyError(f"unsafe-daily-source:{path.name}")
        digest = sha256_file(path)
        if ingested.get(path.name) != digest:
            yield path, digest


def changed_dailies(
    vault_root: Path,
    state: CompileState | None = None,
) -> list[tuple[Path, str]]:
    """Son ingest'ten beri değişmiş günlükler, tarih sırasında.

    `state` verilmezse cursor vault'un kendi state dizininden okunur.
    """
    cursor = load(state_dir_of(vault_root)) if state is None else state
    return list(_iter_changed(vault_root, cursor.ingested))


def has_changes(vault_root: Path) -> bool:
    """`changed_dailies`'in ilk değişimde duran biçimi (tetikleyici sıcak yolu)."""
    ingested = load(state_dir_of(vault_root)).ingested
    return next(_iter_changed(vault_root, ingested), None) is not None
