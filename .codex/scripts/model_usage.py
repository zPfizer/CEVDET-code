"""Kaydedilebilen model çağrıları: amaç, boyut, süre, sonuç.

'Token nereye gidiyor?' sorusu ölçüm olmadan cevaplanamaz. `codex exec`
çağrıları amaç etiketiyle günlük JSONL dosyasına kaydedilmeye çalışılır.
Özet CLI'dan okunur; eksik okumada toplamın eksik olduğu belirtilir.
Kayıt katmanı asıl çağrının kaderini belirlemez: yazma hatasını çağıran yutar.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, BinaryIO, Sequence

from file_lock import LockUnavailable, locked

SCHEMA_VERSION = 1
KEEP_DAYS = 30
DEFAULT_SUMMARY_DAYS = 7
USAGE_FILE = re.compile(r"model-usage-(\d{8})\.jsonl$")
MAX_RECORD_BYTES = 4096
MAX_METRIC_VALUE = 1_000_000_000_000
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def _usage_path(state_dir: Path, day: datetime.date) -> Path:
    return state_dir / f"model-usage-{day.strftime('%Y%m%d')}.jsonl"


def _file_day(path: Path) -> datetime.date | None:
    match = USAGE_FILE.fullmatch(path.name)
    if match is None:
        return None
    try:
        return datetime.datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _prune(state_dir: Path, today: datetime.date) -> None:
    cutoff = today - datetime.timedelta(days=KEEP_DAYS)
    for path in state_dir.glob("model-usage-*.jsonl"):
        day = _file_day(path)
        if day is not None and day < cutoff:
            path.unlink(missing_ok=True)


def _unsafe_usage_target(path: Path) -> bool:
    """Refuse linked or shared files before append/read can follow them."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    return _unsafe_usage_stat(path_stat)


def _unsafe_usage_stat(path_stat: os.stat_result) -> bool:
    return (
        _link_or_reparse(path_stat)
        or not stat.S_ISREG(path_stat.st_mode)
        or getattr(path_stat, "st_nlink", 1) != 1
    )


def _link_or_reparse(path_stat: os.stat_result) -> bool:
    return (
        stat.S_ISLNK(path_stat.st_mode)
        or bool(getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT)
    )


def _unsafe_usage_directory(path: Path) -> bool:
    """Reject linked/reparse state directories and ancestors before mkdir/open."""
    requested = Path(path).absolute()
    current = requested
    try:
        while True:
            try:
                path_stat = current.lstat()
            except FileNotFoundError:
                path_stat = None
            if path_stat is not None:
                if _link_or_reparse(path_stat) or not stat.S_ISDIR(path_stat.st_mode):
                    return True
            parent = current.parent
            if parent == current:
                return False
            current = parent
    except OSError:
        return True


def _usage_handle_matches(path: Path, handle: BinaryIO) -> bool:
    try:
        path_stat = path.lstat()
        handle_stat = os.fstat(handle.fileno())
    except OSError:
        return False
    return (
        not _unsafe_usage_stat(path_stat)
        and not _unsafe_usage_stat(handle_stat)
        and path_stat.st_dev == handle_stat.st_dev
        and path_stat.st_ino == handle_stat.st_ino
    )


def _open_usage_target(path: Path) -> BinaryIO | None:
    """Open append-only without accepting a path replaced after its first check."""
    try:
        previous_stat = path.lstat()
    except FileNotFoundError:
        previous_stat = None
    except OSError:
        return None
    if previous_stat is not None and _unsafe_usage_stat(previous_stat):
        return None

    handle: BinaryIO | None = None
    try:
        handle = path.open("a+b")
        handle_stat = os.fstat(handle.fileno())
        current_stat = path.lstat()
        if (
            _unsafe_usage_stat(handle_stat)
            or _unsafe_usage_stat(current_stat)
            or handle_stat.st_dev != current_stat.st_dev
            or handle_stat.st_ino != current_stat.st_ino
            or (
                previous_stat is not None
                and (
                    previous_stat.st_dev != handle_stat.st_dev
                    or previous_stat.st_ino != handle_stat.st_ino
                )
            )
        ):
            handle.close()
            return None
        return handle
    except OSError:
        if handle is not None:
            handle.close()
        return None


def _ensure_record_boundary(path: Path, handle: BinaryIO) -> bool:
    """Separate a torn final write before appending the next JSONL record."""
    handle.seek(0, 2)
    size = handle.tell()
    if size == 0:
        return True
    handle.seek(-1, 2)
    if handle.read(1) == b"\n":
        return True
    if not _usage_handle_matches(path, handle):
        return False
    handle.seek(0, 2)
    handle.write(b"\n")
    return True


def record(
    state_dir: Path,
    *,
    purpose: str,
    prompt_chars: int,
    duration_ms: int,
    outcome: str,
    result_chars: int = 0,
    now: datetime.datetime | None = None,
) -> None:
    moment = now or datetime.datetime.now()
    entry = {
        "schema": SCHEMA_VERSION,
        "ts": int(moment.timestamp()),
        "purpose": purpose,
        "prompt_chars": int(prompt_chars),
        "duration_ms": int(duration_ms),
        "outcome": outcome,
        "result_chars": int(result_chars),
    }
    state_dir = Path(state_dir)
    if _unsafe_usage_directory(state_dir):
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    try:
        # ponytail: contention drops telemetry; durable queueing belongs to a separate recorder.
        lock_path = (state_dir / "model-usage").with_suffix(".lock")
        if _unsafe_usage_target(lock_path):
            return
        with locked(state_dir / "model-usage", timeout=0):
            usage_path = _usage_path(state_dir, moment.date())
            handle = _open_usage_target(usage_path)
            if handle is None:
                return
            try:
                if not _ensure_record_boundary(usage_path, handle):
                    return
                if not _usage_handle_matches(usage_path, handle):
                    return
                handle.write((line + "\n").encode("utf-8"))
            finally:
                handle.close()
            _prune(state_dir, moment.date())
    except LockUnavailable:
        return


def _iter_records(
    state_dir: Path,
    since: datetime.date,
    until: datetime.date | None = None,
) -> Iterator[dict[str, Any] | None]:
    """Yield usable records; None marks an unreadable/invalid selected input."""
    try:
        paths = tuple(Path(state_dir).iterdir())
    except FileNotFoundError:
        return
    except OSError:
        yield None
        return
    for path in paths:
        day = _file_day(path)
        if day is None or day < since or (until is not None and day > until):
            continue
        try:
            if _unsafe_usage_target(path):
                yield None
                continue
            with path.open("rb") as handle:
                if not _usage_handle_matches(path, handle):
                    yield None
                    continue
                discarding = False
                while raw := handle.readline(MAX_RECORD_BYTES + 2):
                    line = raw.removesuffix(b"\n").removesuffix(b"\r")
                    if discarding or len(line) > MAX_RECORD_BYTES:
                        if not discarding:
                            yield None
                        discarding = not raw.endswith(b"\n")
                        continue
                    if not raw.endswith(b"\n"):
                        yield None  # A parseable tail can still be a torn write.
                    try:
                        value = json.loads(line.decode("utf-8"))
                    except (UnicodeError, ValueError):
                        yield None
                        continue
                    if (
                        isinstance(value, dict)
                        and type(value.get("schema")) is int
                        and value["schema"] == SCHEMA_VERSION
                    ):
                        yield value
                    else:
                        yield None
                if not _usage_handle_matches(path, handle):
                    yield None
        except OSError:
            yield None


def usage_summary(
    state_dir: Path,
    *,
    days: int = DEFAULT_SUMMARY_DAYS,
    now: datetime.datetime | None = None,
) -> tuple[dict[str, dict[str, int]], bool]:
    """Return purpose totals and whether selected ledger reads were incomplete.

    A complete read still covers recorded calls only: recording is best-effort.
    """
    if days < 1 or days > KEEP_DAYS:
        raise ValueError("summary-window-out-of-retention")
    if _unsafe_usage_directory(Path(state_dir)):
        return {}, True
    moment = now or datetime.datetime.now()
    since = moment.date() - datetime.timedelta(days=days - 1)
    summary: dict[str, dict[str, int]] = {}
    incomplete = False
    for entry in _iter_records(state_dir, since, until=moment.date()):
        if entry is None:
            incomplete = True
            continue
        try:
            prompt_chars = int(entry.get("prompt_chars", 0) or 0)
            duration_ms = int(entry.get("duration_ms", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            incomplete = True
            continue
        if not (
            0 <= prompt_chars <= MAX_METRIC_VALUE
            and 0 <= duration_ms <= MAX_METRIC_VALUE
        ):
            incomplete = True
            continue
        purpose = str(entry.get("purpose", "unknown"))
        bucket = summary.setdefault(
            purpose,
            {"calls": 0, "ok": 0, "failed": 0, "prompt_chars": 0, "duration_ms": 0},
        )
        bucket["calls"] += 1
        if entry.get("outcome") == "ok":
            bucket["ok"] += 1
        else:
            bucket["failed"] += 1
        bucket["prompt_chars"] += prompt_chars
        bucket["duration_ms"] += duration_ms
    return summary, incomplete


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_state = Path(__file__).resolve().parent / ".state"
    parser.add_argument("--state-dir", type=Path, default=default_state)
    parser.add_argument("--days", type=int, default=DEFAULT_SUMMARY_DAYS)
    args = parser.parse_args(argv)
    try:
        summary, incomplete = usage_summary(args.state_dir, days=args.days)
    except ValueError as exc:
        parser.error(str(exc))
    if incomplete:
        print("Uyarı: Kullanım raporu eksik; bazı kayıtlar okunamadı veya geçersiz.",
              file=sys.stderr)
    if not summary and incomplete:
        print(f"Son {args.days} günün kayıtlı çağrı toplamı doğrulanamadı.")
        return 1
    if not summary:
        print(f"Son {args.days} günde kayıtlı model çağrısı yok.")
        return 0
    qualifier = "okunabilen kayıtlar (eksik)" if incomplete else "kayıtlı model çağrıları"
    print(f"Son {args.days} gün — amaç başına {qualifier}:")
    print("| Amaç | Çağrı | Başarılı | Hatalı | Prompt (kchar) | Ort. süre (sn) |")
    print("| --- | --- | --- | --- | --- | --- |")
    for purpose in sorted(summary):
        bucket = summary[purpose]
        average_s = (bucket["duration_ms"] / bucket["calls"]) / 1000 if bucket["calls"] else 0
        print(
            f"| {purpose} | {bucket['calls']} | {bucket['ok']} | {bucket['failed']} "
            f"| {bucket['prompt_chars'] / 1000:.1f} | {average_s:.1f} |"
        )
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
