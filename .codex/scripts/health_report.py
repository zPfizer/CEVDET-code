"""Cevo altyapı durumunu Command-Center'da tek bakışlık panele yazar.

Bütün okumalar kilitsiz ve sınırlıdır: panel yaklaşık bir anlık görüntüdür,
yetkili durum state dizinindeki dosyaların kendisidir. Not `dashboard` tipiyle
üretilir ki intake sözleşmesinin üst-bağlantı zorunluluğuna takılmasın.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
from pathlib import Path
import re
import stat
from typing import Any, Sequence

import compile_state
from state_store import HEALTH_SCHEMA_VERSION, atomic_write_text, state_dir_of

PANEL_RELATIVE = Path("🎯 100-Command-Center") / "Cevo Sağlık.md"
WORKER_STAGES = (
    "pending",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "dead-letter",
    "quarantined",
)
DEAD_LETTER_LIMIT = 10
MAX_RECORD_BYTES = 131072
_HEALTH_STATUSES = frozenset({"error", "warning"})
_DEAD_LETTER_REASONS = frozenset(
    {
        "legacy-failed",
        "recovered-by-successor",
        "retry-exhausted",
        "unrecoverable-input",
    }
)
_MAX_TIMESTAMP = datetime.datetime.max.replace(
    tzinfo=datetime.timezone.utc,
).timestamp()
_CREATED = re.compile(r"(?m)^created: (?P<value>.+)$")
_COMPILE_STATUS = re.compile(r"(?:ok|fail:[A-Za-z0-9][A-Za-z0-9._:-]{0,127})\Z")


def _default_report_target(vault: Path) -> Path:
    target = vault / PANEL_RELATIVE
    parent = target.parent
    try:
        parent_stat = parent.lstat()
        if (
            stat.S_ISLNK(parent_stat.st_mode)
            or parent.is_junction()
            or not stat.S_ISDIR(parent_stat.st_mode)
        ):
            raise ValueError("report-target-invalid")
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError) as exc:
        raise ValueError("report-target-invalid") from exc
    try:
        resolved_parent = parent.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("report-target-invalid") from exc
    if not resolved_parent.is_relative_to(vault):
        raise ValueError("report-target-invalid")
    return target


def _bounded_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _unreadable_dead_letter_row() -> dict[str, Any]:
    return {
        "job_id": "?",
        "kind": "?",
        "terminal_reason": "?",
        "finished_ts": None,
    }


def _dead_letter_row(path: Path) -> dict[str, Any]:
    record = _bounded_json(path)
    if record is None:
        return _unreadable_dead_letter_row()
    try:
        from worker_supervisor import _validate_job

        validation_record = record
        try:
            _validate_job(path, validation_record)
        except ValueError:
            # Timestamp formatting deliberately renders malformed values as
            # unknown, while every other worker-schema violation is unreadable.
            validation_record = dict(record)
            validation_record["finished_ts"] = 0
            _validate_job(path, validation_record)
        terminal_reason = record.get("terminal_reason")
        if not isinstance(terminal_reason, str) or terminal_reason not in _DEAD_LETTER_REASONS:
            return _unreadable_dead_letter_row()
    except (TypeError, ValueError):
        return _unreadable_dead_letter_row()
    return {
        "job_id": record["job_id"][:8],
        "kind": record["kind"],
        "terminal_reason": terminal_reason,
        "finished_ts": record.get("finished_ts"),
    }


def worker_counts(state_dir: Path) -> dict[str, int]:
    jobs = state_dir / "worker-jobs"
    return {stage: len(_json_files(jobs / stage)) for stage in WORKER_STAGES}


def _json_files(directory: Path) -> tuple[Path, ...]:
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError:
        return ()
    except (OSError, RuntimeError) as exc:
        raise OSError("worker-directory-unreadable") from exc
    if (
        stat.S_ISLNK(directory_stat.st_mode)
        or not stat.S_ISDIR(directory_stat.st_mode)
        or directory.is_junction()
    ):
        raise OSError("worker-directory-invalid")
    try:
        entries = tuple(directory.iterdir())
    except (OSError, RuntimeError) as exc:
        raise OSError("worker-directory-unreadable") from exc
    files = []
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            path_stat = path.lstat()
        except (OSError, RuntimeError) as exc:
            raise OSError("worker-record-unreadable") from exc
        if stat.S_ISREG(path_stat.st_mode):
            files.append(path)
    return tuple(sorted(files))


def dead_letter_rows(state_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in _json_files(state_dir / "worker-jobs" / "dead-letter"):
        rows.append(_dead_letter_row(path))
    rows.sort(key=lambda row: _timestamp_sort_key(row["finished_ts"]), reverse=True)
    return rows[:DEAD_LETTER_LIMIT]


def marker_counts(state_dir: Path) -> dict[str, int]:
    def count(prefix: str) -> int:
        return sum(
            1
            for path in state_dir.glob(f"{prefix}*")
            if path.is_file() and path.suffix != ".lock"
        )

    return {
        "read_only": count("memory-read-only-"),
        "session_only": count("memory-session-only-"),
    }


def compile_summary(state_dir: Path) -> tuple[str, str]:
    try:
        state = compile_state.load(state_dir)
    except compile_state.PolicyError:
        return "?", "bozuk kayıt"
    last_run = state.last_run
    last_status = state.last_status
    if not isinstance(last_run, str) or not isinstance(last_status, str):
        return "?", "bozuk kayıt"
    if last_run:
        try:
            datetime.datetime.fromisoformat(last_run)
        except ValueError:
            return "?", "bozuk kayıt"
    if _COMPILE_STATUS.fullmatch(last_status) is None:
        return "?", "bozuk kayıt"
    return last_run or "hiç", last_status


def health_summary(state_dir: Path) -> str:
    path = state_dir / "health.json"
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return "kayıt yok (temiz)"
    except OSError:
        return "okunamadı"
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return "okunamadı"
    loaded = _bounded_json(path)
    if loaded is None:
        return "okunamadı"
    if loaded.get("schema_version") != HEALTH_SCHEMA_VERSION:
        return "okunamadı"
    components = loaded.get("components")
    if not isinstance(components, dict):
        return "okunamadı"
    if any(
        not isinstance(key, str)
        or not isinstance(entry, dict)
        or not isinstance(entry.get("status"), str)
        or entry["status"] not in _HEALTH_STATUSES
        for key, entry in components.items()
    ):
        return "okunamadı"
    errors = sum(1 for entry in components.values() if isinstance(entry, dict) and entry.get("status") == "error")
    warnings = sum(1 for entry in components.values() if isinstance(entry, dict) and entry.get("status") == "warning")
    if not errors and not warnings:
        return "temiz"
    return f"hata={errors} uyarı={warnings}"


def flush_state_count(state_dir: Path) -> int:
    return sum(
        1
        for path in state_dir.glob("flush-*.json")
        if path.is_file()
    )


def _timestamp_value(value: Any) -> float | None:
    try:
        stamp = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if (
        isinstance(value, bool)
        or not math.isfinite(stamp)
        or stamp <= 0
        or stamp > _MAX_TIMESTAMP
    ):
        return None
    return stamp


def _timestamp_sort_key(value: Any) -> tuple[bool, float]:
    stamp = _timestamp_value(value)
    return stamp is not None, stamp or 0.0


def _format_ts(value: Any) -> str:
    stamp = _timestamp_value(value)
    if stamp is None:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def _previous_created(output: Path, fallback: str) -> str:
    try:
        output_stat = output.lstat()
        if stat.S_ISLNK(output_stat.st_mode) or not stat.S_ISREG(output_stat.st_mode):
            raise ValueError("report-target-invalid")
    except FileNotFoundError:
        return fallback
    except (OSError, RuntimeError) as exc:
        raise ValueError("report-target-invalid") from exc
    try:
        with output.open("r", encoding="utf-8") as handle:
            head = handle.read(2048)
    except (OSError, UnicodeError):
        return fallback
    match = _CREATED.search(head)
    return match.group("value").strip() if match else fallback


def render(
    vault: Path,
    *,
    now: datetime.datetime | None = None,
    output: Path | None = None,
) -> str:
    state_dir = state_dir_of(vault)
    moment = now or datetime.datetime.now()
    today = moment.strftime("%Y-%m-%d")
    selected_output = output if output is not None else vault / PANEL_RELATIVE
    created = _previous_created(selected_output, today)

    counts = worker_counts(state_dir)
    markers = marker_counts(state_dir)
    last_run, last_status = compile_summary(state_dir)
    dead_rows = dead_letter_rows(state_dir)

    lines = [
        "---",
        "title: Cevo Sağlık",
        f"created: {created}",
        f"updated: {today}",
        "type: dashboard",
        "status: active",
        "tags:",
        "  - sistem",
        "  - sağlık",
        "---",
        "# Cevo Sağlık",
        "",
        f"Üretilme: {moment.strftime('%Y-%m-%d %H:%M')} — bu notu `health_report.py` üretir; mevcut hedef yalnız açık `--overwrite` (veya API'de `overwrite=True`) ile değiştirilir.",
        "",
        "## İş kuyruğu",
        "",
        "| Aşama | Adet |",
        "| --- | --- |",
    ]
    lines.extend(f"| {stage} | {counts[stage]} |" for stage in WORKER_STAGES)
    lines += ["", "## Dead-letter", ""]
    if dead_rows:
        lines += [
            f"Takılı iş var: {counts['dead-letter']} kayıt. En yeniler:",
            "",
            "| İş | Tür | Neden | Bitiş |",
            "| --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {row['job_id']} | {row['kind']} | {row['terminal_reason']} | {_format_ts(row['finished_ts'])} |"
            for row in dead_rows
        )
    else:
        lines.append("Boş — takılı iş yok.")
    lines += [
        "",
        "## İşaretler ve durum",
        "",
        f"- Aktif read-only işareti: {markers['read_only']}",
        f"- Aktif session-only işareti: {markers['session_only']}",
        f"- Flush durum dosyası: {flush_state_count(state_dir)}",
        f"- Derleyici son çalışma: {last_run} (durum: {last_status})",
        f"- Sağlık kaydı (health.json): {health_summary(state_dir)}",
        "",
    ]
    return "\n".join(lines)


def write_report(
    vault: Path,
    output: Path | None = None,
    *,
    now: datetime.datetime | None = None,
    overwrite: bool = False,
) -> Path:
    vault = Path(vault).resolve()
    target = output if output is not None else _default_report_target(vault)
    atomic_write_text(target, render(vault, now=now, output=target), overwrite=overwrite)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing report target",
    )
    args = parser.parse_args(argv)
    target = write_report(args.vault, args.output, overwrite=args.overwrite)
    print(f"Panel yazıldı: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
