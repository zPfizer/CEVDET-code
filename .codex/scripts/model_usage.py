"""Model çağrılarının deterministik muhasebesi: amaç, boyut, süre, sonuç.

'Token nereye gidiyor?' sorusu ölçüm olmadan cevaplanamaz. Her `codex exec`
çağrısı amaç etiketiyle günlük JSONL dosyasına düşer; özet CLI'dan okunur.
Kayıt katmanı asıl çağrının kaderini belirlemez: yazma hatasını çağıran yutar.
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import re
import stat
from typing import Any, Sequence

from file_lock import LockUnavailable, locked

SCHEMA_VERSION = 1
KEEP_DAYS = 30
DEFAULT_SUMMARY_DAYS = 7
USAGE_FILE = re.compile(r"model-usage-(\d{8})\.jsonl$")
MAX_RECORD_BYTES = 4096
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
    """Refuse links, reparse points, and non-files before append can follow them."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISLNK(path_stat.st_mode)
        or bool(getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT)
        or not stat.S_ISREG(path_stat.st_mode)
    )


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
    state_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    try:
        # ponytail: contention drops telemetry; durable queueing belongs to a separate recorder.
        with locked(state_dir / "model-usage", timeout=0):
            usage_path = _usage_path(state_dir, moment.date())
            if _unsafe_usage_target(usage_path):
                return
            with usage_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            _prune(state_dir, moment.date())
    except LockUnavailable:
        return


def _iter_records(state_dir: Path, since: datetime.date) -> list[dict[str, Any]]:
    records = []
    for path in sorted(Path(state_dir).glob("model-usage-*.jsonl")):
        day = _file_day(path)
        if day is None or day < since:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            if len(line) > MAX_RECORD_BYTES:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("schema") == SCHEMA_VERSION:
                records.append(value)
    return records


def usage_summary(
    state_dir: Path,
    *,
    days: int = DEFAULT_SUMMARY_DAYS,
    now: datetime.datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Amaç başına: çağrı, başarı, hata, toplam prompt karakteri, ortalama süre."""
    moment = now or datetime.datetime.now()
    since = moment.date() - datetime.timedelta(days=days - 1)
    summary: dict[str, dict[str, int]] = {}
    for entry in _iter_records(state_dir, since):
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
        bucket["prompt_chars"] += int(entry.get("prompt_chars", 0) or 0)
        bucket["duration_ms"] += int(entry.get("duration_ms", 0) or 0)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_state = Path(__file__).resolve().parent / ".state"
    parser.add_argument("--state-dir", type=Path, default=default_state)
    parser.add_argument("--days", type=int, default=DEFAULT_SUMMARY_DAYS)
    args = parser.parse_args(argv)
    summary = usage_summary(args.state_dir, days=args.days)
    if not summary:
        print(f"Son {args.days} günde kayıtlı model çağrısı yok.")
        return 0
    print(f"Son {args.days} gün — amaç başına model çağrıları:")
    print("| Amaç | Çağrı | Başarılı | Hatalı | Prompt (kchar) | Ort. süre (sn) |")
    print("| --- | --- | --- | --- | --- | --- |")
    for purpose in sorted(summary):
        bucket = summary[purpose]
        average_s = (bucket["duration_ms"] / bucket["calls"]) / 1000 if bucket["calls"] else 0
        print(
            f"| {purpose} | {bucket['calls']} | {bucket['ok']} | {bucket['failed']} "
            f"| {bucket['prompt_chars'] / 1000:.1f} | {average_s:.1f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
