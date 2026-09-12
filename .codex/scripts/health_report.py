"""Cevo altyapı durumunu Command-Center'da tek bakışlık panele yazar.

Bütün okumalar kilitsiz ve sınırlıdır: panel yaklaşık bir anlık görüntüdür,
yetkili durum state dizinindeki dosyaların kendisidir. Not `dashboard` tipiyle
üretilir ki intake sözleşmesinin üst-bağlantı zorunluluğuna takılmasın.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import stat
import time
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
        "redrive-conflict",
        "redrive-identity-conflict",
        "retry-exhausted",
        "unrecoverable-input",
    }
)
_MAX_TIMESTAMP = datetime.datetime.max.replace(
    tzinfo=datetime.timezone.utc,
).timestamp()
_FRONTMATTER = re.compile(
    r"\A---\r?\n(?P<body>.*?)(?:\r?\n)---(?:\r?\n|\Z)",
    re.DOTALL,
)
_CREATED = re.compile(r"(?m)^created: (?P<value>[^\r\n]+)$")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_COMPILE_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?\Z"
)
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


def _validate_report_target(vault: Path, target: Path) -> None:
    lexical = target if target.is_absolute() else Path.cwd() / target
    lexical = lexical.absolute()
    for candidate in (lexical, lexical.resolve(strict=False)):
        if any(candidate.is_relative_to(vault / name) for name in ("daily", "knowledge", ".codex")):
            raise ValueError("report-target-protected")
    try:
        relative_parent = lexical.parent.relative_to(vault)
    except ValueError:
        return
    current = vault
    for part in relative_parent.parts:
        current /= part
        try:
            current_stat = current.lstat()
            resolved = current.resolve(strict=False)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            raise ValueError("report-target-invalid") from exc
        if (
            stat.S_ISLNK(current_stat.st_mode)
            or not stat.S_ISDIR(current_stat.st_mode)
            or current.is_junction()
            or not resolved.is_relative_to(vault)
        ):
            raise ValueError("report-target-invalid")


def _validate_vault(vault: Path) -> None:
    try:
        vault_stat = vault.lstat()
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise ValueError("vault-invalid") from exc
    if (
        stat.S_ISLNK(vault_stat.st_mode)
        or not stat.S_ISDIR(vault_stat.st_mode)
        or vault.is_junction()
    ):
        raise ValueError("vault-invalid")


def _validate_runtime_paths(vault: Path) -> None:
    current = vault
    for part in (".codex", "scripts", ".state"):
        current /= part
        try:
            current_stat = current.lstat()
            resolved = current.resolve(strict=False)
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            raise ValueError("vault-runtime-invalid") from exc
        if (
            stat.S_ISLNK(current_stat.st_mode)
            or not stat.S_ISDIR(current_stat.st_mode)
            or current.is_junction()
            or not resolved.is_relative_to(vault)
        ):
            raise ValueError("vault-runtime-invalid")


def _bounded_json(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or getattr(metadata, "st_nlink", 1) != 1
            or metadata.st_size > MAX_RECORD_BYTES
        ):
            return None
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                or opened.st_nlink != 1
            ):
                return None
            payload = handle.read(MAX_RECORD_BYTES + 1)
        if len(payload) > MAX_RECORD_BYTES:
            return None
        loaded = json.loads(payload.decode("utf-8"))
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
    timestamp_repaired = False
    try:
        from worker_supervisor import _has_verified_successor, _validate_job

        validation_record = record
        try:
            _validate_job(path, validation_record)
        except ValueError:
            # Timestamp formatting deliberately renders malformed values as
            # unknown, while every other worker-schema violation is unreadable.
            validation_record = dict(record)
            validation_record["finished_ts"] = 0
            _validate_job(path, validation_record)
            timestamp_repaired = True
        terminal_reason = record.get("terminal_reason")
        if not isinstance(terminal_reason, str) or terminal_reason not in _DEAD_LETTER_REASONS:
            return _unreadable_dead_letter_row()
        if timestamp_repaired and terminal_reason == "recovered-by-successor":
            return _unreadable_dead_letter_row()
    except (TypeError, ValueError):
        return _unreadable_dead_letter_row()
    return {
        "job_id": record["job_id"][:8],
        "kind": record["kind"],
        "terminal_reason": terminal_reason,
        "finished_ts": record.get("finished_ts"),
        "recovered": _has_verified_successor(path.parent.parent, record),
    }


def worker_counts(
    state_dir: Path, *, include_dead_letter: bool = True
) -> dict[str, int]:
    jobs_roots = _worker_job_roots(state_dir)
    counts = {stage: 0 for stage in WORKER_STAGES}
    for jobs in jobs_roots:
        for stage in WORKER_STAGES:
            paths = _json_files(jobs / stage)
            if stage != "dead-letter":
                _validate_worker_records(paths, stage=stage)
            counts[stage] += len(paths)
    if include_dead_letter and jobs_roots:
        counts["dead-letter"] = _dead_letter_summary(
            state_dir, jobs=jobs_roots
        )[0]
    return counts


def _validate_worker_records(
    paths: Sequence[Path], *, stage: str | None = None
) -> None:
    from worker_supervisor import JOB_KINDS, _job_id_from_path, _validate_job

    for path in paths:
        record = _bounded_json(path)
        try:
            if stage == "failed":
                job_id = _job_id_from_path(path)
                if (
                    not isinstance(record, dict)
                    or job_id is None
                    or record.get("job_id") != job_id
                    or record.get("status") != "failed"
                    or not isinstance(record.get("kind"), str)
                    or record["kind"] not in JOB_KINDS
                    or not isinstance(record.get("payload"), dict)
                ):
                    raise ValueError("worker-legacy-state-invalid")
            else:
                validated = _validate_job(path, record)
                if stage == "quarantined":
                    payload_path = path.with_name(str(validated.get("payload_file", "")))
                    payload_stat = payload_path.lstat()
                    payload_resolved = payload_path.resolve(strict=False)
                    if (
                        stat.S_ISLNK(payload_stat.st_mode)
                        or not stat.S_ISREG(payload_stat.st_mode)
                        or getattr(payload_stat, "st_nlink", 1) != 1
                        or payload_path.is_junction()
                        or not payload_resolved.is_relative_to(path.parent)
                    ):
                        raise ValueError("worker-quarantine-payload-invalid")
                    with payload_path.open("rb") as payload_file:
                        digest = hashlib.file_digest(payload_file, "sha256").hexdigest()
                    if digest != validated["payload_sha256"]:
                        raise ValueError("worker-quarantine-payload-invalid")
        except (OSError, TypeError, UnicodeError, ValueError):
            raise OSError("worker-record-invalid") from None


def _worker_jobs_root(state_dir: Path) -> Path | None:
    try:
        lane_stat = state_dir.lstat()
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError) as exc:
        raise OSError("worker-directory-unreadable") from exc
    if (
        stat.S_ISLNK(lane_stat.st_mode)
        or not stat.S_ISDIR(lane_stat.st_mode)
        or state_dir.is_junction()
    ):
        raise OSError("worker-directory-invalid")
    jobs = state_dir / "worker-jobs"
    try:
        jobs_stat = jobs.lstat()
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError) as exc:
        raise OSError("worker-directory-unreadable") from exc
    if (
        stat.S_ISLNK(jobs_stat.st_mode)
        or not stat.S_ISDIR(jobs_stat.st_mode)
        or jobs.is_junction()
    ):
        raise OSError("worker-directory-invalid")
    return jobs


def _worker_job_roots(state_dir: Path) -> tuple[Path, ...]:
    roots = []
    for lane in (state_dir, state_dir / "maintenance"):
        jobs = _worker_jobs_root(lane)
        if jobs is not None:
            roots.append(jobs)
    return tuple(roots)


def _directory_entries(directory: Path, *, error_prefix: str) -> tuple[Path, ...]:
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError:
        return ()
    except (OSError, RuntimeError) as exc:
        raise OSError(f"{error_prefix}-directory-unreadable") from exc
    if (
        stat.S_ISLNK(directory_stat.st_mode)
        or not stat.S_ISDIR(directory_stat.st_mode)
        or directory.is_junction()
    ):
        raise OSError(f"{error_prefix}-directory-invalid")
    try:
        return tuple(directory.iterdir())
    except (OSError, RuntimeError) as exc:
        raise OSError(f"{error_prefix}-directory-unreadable") from exc


def _json_files(
    directory: Path, *, error_prefix: str = "worker"
) -> tuple[Path, ...]:
    entries = _directory_entries(directory, error_prefix=error_prefix)
    files = []
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            path_stat = path.lstat()
        except (OSError, RuntimeError) as exc:
            raise OSError(f"{error_prefix}-record-unreadable") from exc
        if not stat.S_ISREG(path_stat.st_mode) or getattr(path_stat, "st_nlink", 1) != 1:
            raise OSError(f"{error_prefix}-record-invalid")
        files.append(path)
    return tuple(sorted(files))


def _all_dead_letter_rows(
    state_dir: Path, *, jobs: Sequence[Path] | None = None
) -> list[dict[str, Any]]:
    return _dead_letter_summary(state_dir, jobs=jobs)[1]


def _dead_letter_summary(
    state_dir: Path,
    *,
    jobs: Sequence[Path] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    jobs = _worker_job_roots(state_dir) if jobs is None else jobs
    count = 0
    sequence = 0
    newest: list[tuple[tuple[bool, float, int], dict[str, Any]]] = []
    for jobs_root in jobs:
        for path in _json_files(jobs_root / "dead-letter"):
            row = _dead_letter_row(path)
            if row.get("recovered", False):
                continue
            count += 1
            sequence += 1
            rank = (*_timestamp_sort_key(row["finished_ts"]), sequence)
            item = (rank, row)
            if len(newest) < DEAD_LETTER_LIMIT:
                heapq.heappush(newest, item)
            elif rank > newest[0][0]:
                heapq.heapreplace(newest, item)
    rows = [item[1] for item in newest]
    rows.sort(key=lambda row: _timestamp_sort_key(row["finished_ts"]), reverse=True)
    return count, rows


def dead_letter_rows(state_dir: Path) -> list[dict[str, Any]]:
    return _dead_letter_summary(state_dir)[1]


def orphan_hook_input_count(state_dir: Path) -> int | None:
    from worker_supervisor import (
        FLUSH_REASON_PRIORITY,
        HOOK_INPUT_SCHEMA_VERSION,
        count_orphan_hook_inputs,
    )

    try:
        for path in _directory_entries(state_dir, error_prefix="state"):
            if not path.name.startswith("hookin-") or path.suffix != ".json":
                continue
            path_stat = path.lstat()
            record = _bounded_json(path)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or record is None
                or record.get("delivery_schema_version")
                != HOOK_INPUT_SCHEMA_VERSION
                or not isinstance(record.get("session_id"), str)
                or not record["session_id"]
                or record.get("reason") not in FLUSH_REASON_PRIORITY
                or not isinstance(record.get("event_iso"), str)
                or not record["event_iso"]
            ):
                raise OSError("worker-hook-input-invalid")
        return count_orphan_hook_inputs(state_dir, strict=True)
    except OSError as exc:
        if str(exc) == "worker-hook-input-invalid":
            raise
        raise OSError("worker-hook-input-unreadable") from exc
    except (RuntimeError, TypeError, ValueError, UnicodeError) as exc:
        if str(exc) == "worker-hook-input-references-unreadable":
            return None
        raise OSError("worker-hook-input-unreadable") from exc


def stale_running_count(
    state_dir: Path,
    *,
    jobs: Sequence[Path] | None = None,
    now: float,
) -> int:
    from worker_supervisor import _process_owner_classification

    total = 0
    for jobs_root in _worker_job_roots(state_dir) if jobs is None else jobs:
        for stage in ("claimed", "running"):
            for path in _json_files(jobs_root / stage):
                record = _bounded_json(path)
                if not isinstance(record, dict):
                    raise OSError("worker-record-invalid")
                lease_until = _timestamp_value(record.get("lease_until"))
                if lease_until is None:
                    raise OSError("worker-record-invalid")
                try:
                    owner_status = _process_owner_classification(record)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise OSError("worker-owner-unreadable") from exc
                if owner_status in {"inactive", "mismatched"} or (
                    lease_until <= now and owner_status == "unreadable"
                ):
                    total += 1
    return total


def worker_fences(state_dir: Path) -> tuple[str, ...]:
    from worker_supervisor import has_unverified_process_tree

    fences = []
    for label, lane in (
        ("global", state_dir),
        ("maintenance", state_dir / "maintenance"),
    ):
        if has_unverified_process_tree(lane):
            fences.append(label)
    return tuple(fences)


def _worker_observation(state_dir: Path) -> dict[Path, tuple[int, ...]]:
    """Detect ordinary atomic queue moves/writes across a report scan."""
    observation = {}
    paths = []
    for jobs in _worker_job_roots(state_dir):
        paths.extend(path for stage in WORKER_STAGES for path in _json_files(jobs / stage))
        paths.extend((jobs / "quarantined").glob("*.payload"))
    for lane in (state_dir, state_dir / "maintenance"):
        paths.extend(_directory_entries(lane, error_prefix="state"))
        supervisor = lane / "worker-supervisor.json"
        if supervisor.exists():
            if _bounded_json(supervisor) is None:
                raise OSError("worker-supervisor-unreadable")
            paths.append(supervisor)
    for path in paths:
        info = path.lstat()
        if path.parent in (state_dir, state_dir / "maintenance") and not stat.S_ISDIR(info.st_mode) and (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
        ):
            raise OSError("state-record-invalid")
        observation[path] = (info.st_dev, info.st_ino, info.st_size,
                             info.st_mtime_ns, info.st_ctime_ns)
    return observation


def marker_counts(state_dir: Path) -> dict[str, int]:
    entries = _directory_entries(state_dir, error_prefix="state")

    def count(prefix: str) -> int:
        total = 0
        for path in entries:
            if not path.name.startswith(prefix) or path.suffix == ".lock":
                continue
            try:
                path_stat = path.lstat()
            except (OSError, RuntimeError) as exc:
                raise OSError("state-marker-unreadable") from exc
            if not stat.S_ISREG(path_stat.st_mode):
                raise OSError("state-marker-invalid")
            total += 1
        return total

    return {
        "read_only": count("memory-read-only-"),
        "session_only": count("memory-session-only-"),
    }


def compile_summary(state_dir: Path) -> tuple[str, str]:
    try:
        if compile_state.load_publication(state_dir) is not None:
            return "?", "yayın kurtarma bekliyor"
        publication_id = compile_state.load_publication_token(state_dir)
    except compile_state.PolicyError:
        return "?", "yayın kaydı doğrulanamadı"
    result = _compile_state_summary(state_dir)
    try:
        if (
            compile_state.load_publication(state_dir) is not None
            or compile_state.load_publication_token(state_dir) != publication_id
            or compile_state.load_publication(state_dir) is not None
        ):
            return "?", "yayın kurtarma bekliyor"
    except compile_state.PolicyError:
        return "?", "yayın kaydı doğrulanamadı"
    return result


def _compile_state_summary(state_dir: Path) -> tuple[str, str]:
    path = compile_state.state_file(state_dir)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        path_stat = None
    except (OSError, RuntimeError):
        return "?", "okunamadı"
    if path_stat is None:
        return "hiç", "kayıt yok"
    if path_stat is not None and (
        stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode)
        or getattr(path_stat, "st_nlink", 1) != 1
    ):
        return "?", "okunamadı"
    try:
        state = compile_state.parse_state(_bounded_json(path))
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
        if _COMPILE_TIMESTAMP.fullmatch(last_run) is None:
            return "?", "bozuk kayıt"
    if _COMPILE_STATUS.fullmatch(last_status) is None:
        return "?", "bozuk kayıt"
    return last_run or "hiç", last_status


def health_summary(state_dir: Path) -> str:
    path = state_dir / "health.json"
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return "kayıt yok"
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
        return "kayıtlı hata veya uyarı yok"
    return f"hata={errors} uyarı={warnings}"


def flush_state_count(state_dir: Path, *, now: float | None = None) -> int:
    from doctor import check_flush_state

    paths = [
        path
        for path in _json_files(state_dir, error_prefix="state")
        if path.name.startswith("flush-")
        and not path.name.startswith(
            ("flush-coverage-", "flush-batch-", "flush-index-")
        )
    ]
    if any(_bounded_json(path) is None for path in paths):
        raise OSError("flush-state-invalid")
    report = check_flush_state(state_dir, time.time() if now is None else now)
    if report.status != "OK":
        raise OSError("flush-state-invalid-or-stale")
    return len(paths)


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
    except (OSError, UnicodeError) as exc:
        raise ValueError("report-target-unreadable") from exc
    frontmatter = _FRONTMATTER.match(head)
    if frontmatter is None:
        return fallback
    match = _CREATED.search(frontmatter.group("body"))
    if match is None:
        return fallback
    value = match.group("value").strip()
    if _DATE.fullmatch(value) is None:
        return fallback
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return fallback
    return value


def render(
    vault: Path,
    *,
    now: datetime.datetime | None = None,
    output: Path | None = None,
) -> str:
    vault = Path(vault)
    _validate_vault(vault)
    vault = vault.resolve()
    _validate_runtime_paths(vault)
    state_dir = state_dir_of(vault)
    moment = now or datetime.datetime.now()
    today = moment.strftime("%Y-%m-%d")
    selected_output = output if output is not None else vault / PANEL_RELATIVE
    created = _previous_created(selected_output, today)

    from doctor import check_ready_worker_jobs

    worker_before = _worker_observation(state_dir)
    counts = worker_counts(state_dir, include_dead_letter=False)
    markers = marker_counts(state_dir)
    last_run, last_status = compile_summary(state_dir)
    dead_count, dead_rows = _dead_letter_summary(state_dir)
    counts["dead-letter"] = dead_count
    orphan_hook_inputs = orphan_hook_input_count(state_dir)
    stale_running = stale_running_count(
        state_dir,
        now=moment.timestamp(),
    )
    fences = worker_fences(state_dir)
    readiness = [
        ("global" if jobs.parent == state_dir else "maintenance",
         check_ready_worker_jobs(jobs.parent, moment.timestamp()))
        for jobs in _worker_job_roots(state_dir)
    ]
    if _worker_observation(state_dir) != worker_before:
        raise OSError("worker-state-changed")

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
    elif fences:
        lines.append(
            "Worker temizleme fence'i etkin — kuyruk güvenle doğrulanamıyor: "
            + ", ".join(fences)
            + "."
        )
    elif counts["quarantined"]:
        lines.append(
            "Quarantine'da çözülemeyen iş var — "
            f"{counts['quarantined']} kayıt incelenmeyi bekliyor."
        )
    elif counts["failed"]:
        lines.append(
            "Başarısız iş göçü bekliyor — "
            f"{counts['failed']} eski kayıt taşınmayı bekliyor."
        )
    elif stale_running:
        lines.append(
            "Sahiplik veya süre sorunu olan iş var — "
            f"{stale_running} iş yeniden ele alınmayı bekliyor."
        )
    elif orphan_hook_inputs is None:
        lines.append("Hook girdilerinin kuyruk referansları doğrulanamadı.")
    elif orphan_hook_inputs:
        lines.append(
            "Worker kurtarma bekliyor — "
            f"{orphan_hook_inputs} hook girdisi kuyruğa alınmayı bekliyor."
        )
    elif any(check.status != "OK" for _, check in readiness):
        lines.append("Hazır işler için yürütücü durumu doğrulanamadı.")
    elif any(counts[stage] for stage in ("pending", "claimed", "running")):
        lines.append("Bekleyen veya yürütülen iş var; tamamlanma doğrulanmadı.")
    else:
        lines.append("Taranan kuyruklarda takılı iş saptanmadı.")
    lines += [
        "",
        "## İşaretler ve durum",
        "",
        f"- Aktif read-only işareti: {markers['read_only']}",
        f"- Aktif session-only işareti: {markers['session_only']}",
        f"- Flush durum dosyası: {flush_state_count(state_dir, now=moment.timestamp())}",
        f"- Kurtarılmayı bekleyen hook girdisi: {orphan_hook_inputs if orphan_hook_inputs is not None else 'doğrulanamadı'}",
        f"- Sahiplik veya süre sorunu olan iş: {stale_running}",
        f"- Derleyici son çalışma: {last_run} (durum: {last_status})",
        f"- Sağlık kaydı (health.json): {health_summary(state_dir)}",
        f"- Worker temizleme fence'i: {', '.join(fences) if fences else 'yok'}",
        "",
    ]
    lines.extend(f"- Hazır iş denetimi ({lane}): {check.status}; {check.evidence}"
                 for lane, check in readiness)
    return "\n".join(lines)


def write_report(
    vault: Path,
    output: Path | None = None,
    *,
    now: datetime.datetime | None = None,
    overwrite: bool = False,
) -> Path:
    vault = Path(vault)
    _validate_vault(vault)
    vault = vault.resolve()
    _validate_runtime_paths(vault)
    target = output if output is not None else _default_report_target(vault)
    _validate_report_target(vault, target)
    rendered = render(vault, now=now, output=target)
    _validate_report_target(vault, target)
    atomic_write_text(target, rendered, overwrite=overwrite)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
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
