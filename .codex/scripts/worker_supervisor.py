from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from typing import Any, Callable
import uuid

import process_control
from file_lock import LockUnavailable, locked, timeout_for_deadline
from process_control import (
    ProcessTreeCleanupError,
    ProcessTreeTimeout,
    pid_is_alive,
    run_with_tree_timeout,
    spawn_detached,
)
from state_store import atomic_write_bytes, atomic_write_json, vault_root_of, write_health


JOB_SCHEMA_VERSION = 1
SUPERVISOR_SCHEMA_VERSION = 1
JOB_STATES = (
    "pending",
    "claimed",
    "running",
    "succeeded",
    "dead-letter",
    "quarantined",
)
RECOVERABLE_TRANSITIONS = {
    "pending": {"claimed"},
    "claimed": {"running", "pending", "dead-letter"},
    "running": {"pending", "succeeded", "dead-letter"},
    "dead-letter": {"pending"},
}
JOB_KINDS = {"flush", "maintenance"}
# Supervisor process'inin admission lease'i; iş süresiyle ilgisizdir.
SUPERVISOR_LEASE_SECONDS = 90
LAUNCH_LEASE_SECONDS = 30
# Tek işin azami süresi ve ondan türeyen iş lease'i; daima timeout'tan büyüktür.
JOB_TIMEOUT_SECONDS = 300
JOB_TIMEOUTS = {"flush": JOB_TIMEOUT_SECONDS, "maintenance": 1900}
JOB_LEASE_GRACE_SECONDS = 30
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 5
# enqueue_flush yazar, worker tüketir: hookin ömrünün sahibi burasıdır.
# Başarı yolu dosyayı hemen siler; sweep dead-letter/retry rotalarının bıraktığı
# artıkları toplar, bu yüzden eşik en uzun iş ömründen belirgin şekilde uzundur.
HOOK_INPUT_NAME = re.compile(r"hookin-[^/]+\.json\Z")
STALE_HOOK_INPUT_SECONDS = 3_600
# The hook writes this transport before it contends for the queue lock.  If
# the host deadline wins, the supervisor can still recover the transport.
HOOK_INPUT_SCHEMA_VERSION = 1
# A queue entry remains admitted until its terminal state is safely resolved.
# Dead-letter and quarantine entries deliberately consume this budget as well.
MAX_UNRESOLVED_JOBS = 256
MAX_SUCCEEDED_RECEIPTS = 256
SUCCEEDED_RECEIPT_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
# Recovered-by-successor dead letters retain a bounded proof set outside the
# ordinary success receipt budget; unresolved jobs already cap this set.
MAX_PINNED_SUCCESSOR_RECEIPTS = MAX_UNRESOLVED_JOBS
FLUSH_REASON_PRIORITY = {"turnend": 0, "precompact": 1, "sessionend": 2}
FLUSH_CONTINUATION_FIELDS = (
    "continuation",
    "continuation_reason",
    "coverage",
    "coverage_count",
    "coverage_end",
    "coverage_digest",
)
TREE_CLEANUP_FENCE_NAME = 'worker-tree-cleanup-unverified.json'
REDRIVE_CONFLICT_REASONS = {
    "redrive-conflict",
    "redrive-identity-conflict",
}


def has_unverified_process_tree(state_dir: Path) -> bool:
    """A durable lane fence; a missing/reused leader PID cannot prove its descendants died."""
    try:
        (state_dir / TREE_CLEANUP_FENCE_NAME).lstat()
    except FileNotFoundError:
        return False
    return True


def _fenced_cleanup_error(
    state_dir: Path, job: dict[str, Any], timeout: float,
) -> ProcessTreeCleanupError:
    pid = job.get("owner_pid", 0)
    try:
        fence = json.loads(
            (state_dir / TREE_CLEANUP_FENCE_NAME).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError):
        fence = None
    if isinstance(fence, dict) and isinstance(fence.get("pid"), int):
        pid = fence["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 0:
        pid = 0
    return ProcessTreeCleanupError(
        ["worker-child", str(job.get("job_id", ""))],
        timeout,
        pid,
        OSError("worker-tree-cleanup-unverified"),
    )


def _fence_unverified_process_tree(
    state_dir: Path, job: dict[str, Any], error: ProcessTreeCleanupError, *, now: float,
) -> None:
    with locked(state_dir / 'worker-admission'), locked(state_dir / 'worker-queue'):
        # ponytail: stop this lane until tree shutdown is independently verified;
        # no automatic PID-only reset can establish that its descendants exited.
        atomic_write_json(state_dir / TREE_CLEANUP_FENCE_NAME, {
            'schema_version': 1, 'job_id': job['job_id'], 'pid': error.pid,
            'ts': int(now), 'error': 'ProcessTreeCleanupError', 'retryable': False,
        })
        receipt_path = _supervisor_receipt_path(state_dir)
        try:
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            receipt = None
        if isinstance(receipt, dict) and receipt.get('owner_pid') == os.getpid():
            receipt.update(status='failed', lease_until=0, last_error='ProcessTreeCleanupError')
            atomic_write_json(receipt_path, receipt)


def _process_owner_identity(pid: int) -> str | None:
    identity_reader = getattr(process_control, "process_identity", None)
    if not callable(identity_reader):
        return None
    try:
        identity = identity_reader(pid)
    except (OSError, TypeError, ValueError):
        return None
    return identity if isinstance(identity, str) and identity else None


def _process_owner_classification(record: dict[str, Any]) -> str:
    owner_pid = record.get("owner_pid")
    if not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid <= 0:
        return "inactive"
    if not pid_is_alive(owner_pid):
        return "inactive"
    if "owner_identity" not in record or record["owner_identity"] is None:
        return "unsupported"
    expected_identity = record["owner_identity"]
    if not isinstance(expected_identity, str) or not expected_identity:
        return "invalid"
    current_identity = _process_owner_identity(owner_pid)
    if current_identity is None:
        return "unreadable"
    return "matching" if current_identity == expected_identity else "mismatched"


def _process_owner_is_active(job: dict[str, Any]) -> bool:
    return _process_owner_classification(job) in {
        "unsupported",
        "invalid",
        "unreadable",
        "matching",
    }


def owner_identity_unreadable(record: dict[str, Any]) -> bool:
    """Kaydedilmis dogum kimligi artik okunamiyorsa True.

    Boyle bir kayitta sahiplik kaniti PID'e duser: `_process_owner_is_active`
    kasitli olarak koruma tarafinda kalir. Kanit zayifladigi icin saglik
    raporunun bu durumu ayrica gostermesi gerekir. Kimlik hic kaydedilmemisse
    veya platform kimlik uretemedigi icin None yazilmissa gerileme sinyali
    yoktur; baska bir gecersiz deger ise sahiplik kaniti degildir.
    """
    return _process_owner_classification(record) in {"invalid", "unreadable"}


INVALID_UNICODE_ESCAPE = re.compile(r"\\u(?![0-9a-fA-F]{4})")
INVALID_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _repair_invalid_json_escapes(raw: str) -> str:
    repaired = INVALID_UNICODE_ESCAPE.sub(r"\\\\u", raw)
    return INVALID_JSON_ESCAPE.sub(r"\\\\", repaired)


def load_hook_input(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = json.loads(_repair_invalid_json_escapes(raw))
    if not isinstance(value, dict):
        raise ValueError("hook-input-not-object")
    return resolve_hook_input(value)


def resolve_hook_input(value: dict[str, Any]) -> dict[str, Any]:
    """Refresh a moved transcript without reopening the transport file."""
    value = dict(value)
    transcript = value.get("transcript_path")
    session_id = value.get("session_id")
    if isinstance(transcript, str) and transcript and isinstance(session_id, str):
        if os.name == "nt" and transcript.startswith("\\\\?\\"):
            transcript = "\\\\" + transcript[8:] if transcript.startswith("\\\\?\\UNC\\") else transcript[4:]
        source = Path(transcript).expanduser()
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        archived = codex_home / "archived_sessions" / source.name
        # Codex moves the rollout before the detached SessionEnd worker reads it.
        if (not source.exists()
                and source.resolve().is_relative_to((codex_home / "sessions").resolve())
                and re.fullmatch(r"[0-9a-f-]{36}", session_id)
                and source.name.startswith("rollout-")
                and source.name.endswith(f"-{session_id}.jsonl")
                and archived.is_file()):
            value["transcript_path"] = str(archived)
    return value


def enqueue_flush(
    state_dir: Path,
    payload: dict[str, Any],
    reason: str,
    *,
    vault_root: Path,
    launcher: Callable[..., Any] = subprocess.Popen,
    deadline: float | None = None,
) -> Path:
    """Own the bounded transport from creation through worker cleanup."""
    state_dir.mkdir(parents=True, exist_ok=True)
    hook_input = state_dir / f"hookin-{uuid.uuid4().hex}.json"
    event_iso = dt.datetime.now().astimezone().isoformat()
    transport = {
        "delivery_schema_version": HOOK_INPUT_SCHEMA_VERSION,
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "reason": reason,
        "event_iso": event_iso,
    }
    if isinstance(payload.get('cwd'), str) and payload['cwd']:
        transport['cwd'] = payload['cwd']
    for field in FLUSH_CONTINUATION_FIELDS:
        if field in payload:
            transport[field] = payload[field]
    atomic_write_json(hook_input, transport, deadline=deadline)
    job_payload = {
        "hook_input": str(hook_input),
        "reason": reason,
        "event_iso": event_iso,
    }
    for field in FLUSH_CONTINUATION_FIELDS:
        if field in payload:
            job_payload[field] = payload[field]
    return enqueue_job(
        state_dir, "flush",
        job_payload,
        vault_root=vault_root, launcher=launcher, deadline=deadline,
    )


class _UnrecoverableWorkerInput(RuntimeError):
    pass


def enqueue_maintenance(
    state_dir: Path, *, vault_root: Path, start_supervisor: bool = True,
    launcher: Callable[..., Any] = subprocess.Popen,
    deadline: float | None = None,
) -> Path:
    """A separate lane keeps slow compilation from delaying conversation saves."""
    lane = state_dir / "maintenance"
    try:
        with locked(lane / "maintenance-admission", timeout=_lock_timeout(deadline)):
            pending = None
            with locked(lane / "worker-queue", timeout=_lock_timeout(deadline)):
                for candidate in (_job_root(lane) / "pending").glob("*.json"):
                    job = _load_job_quarantined(lane, candidate, deadline=deadline)
                    if job is not None and job['kind'] == 'maintenance':
                        pending = candidate
                        break
            if pending is None:
                pending = enqueue_job(
                    lane,
                    "maintenance",
                    {},
                    start_supervisor=False,
                    deadline=deadline,
                )
        if start_supervisor:
            ensure_supervisor(
                lane,
                vault_root=vault_root,
                launcher=launcher,
                deadline=deadline,
            )
        return pending
    except LockUnavailable as exc:
        if deadline is None:
            raise
        raise WorkerDeliveryTimeout("worker-maintenance-deadline") from exc


def _job_timeout(kind: str) -> int:
    if not isinstance(kind, str):
        raise ValueError("worker-job-kind-invalid")
    try:
        return JOB_TIMEOUTS[kind]
    except KeyError as exc:
        raise ValueError("worker-job-kind-invalid") from exc


def _job_lease_seconds(kind: str) -> int:
    return _job_timeout(kind) + JOB_LEASE_GRACE_SECONDS


def _report_terminal_maintenance(state_dir: Path, job: dict[str, Any]) -> None:
    if job.get('kind') == 'maintenance' and job.get('status') == 'dead-letter':
        write_health(state_dir.parent, component='compile',
                     error='maintenance:' + str(job.get('last_error', 'worker-failed')))


def _managed_hook_input(path: Path, state_dir: Path) -> bool:
    try:
        state = state_dir.resolve()
        same_parent = path.absolute().parent.resolve() == state
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    return (
        same_parent
        and resolved.parent == state
        and HOOK_INPUT_NAME.fullmatch(path.name) is not None
    )


def _hook_input_reference(payload: object) -> Path | None:
    if not isinstance(payload, dict):
        return None
    hook_input = payload.get("hook_input")
    if not isinstance(hook_input, str) or not hook_input:
        return None
    try:
        return Path(hook_input).resolve(strict=False)
    except OSError:
        return None


def _hook_input_references(payload: object) -> set[Path] | None:
    if not isinstance(payload, dict):
        return set()
    references: set[Path] = set()
    hook_input = payload.get("hook_input")
    if hook_input is not None:
        reference = _hook_input_reference(payload)
        if reference is None:
            return None
        references.add(reference)
    superseded = payload.get("superseded_hook_inputs", [])
    if not isinstance(superseded, list):
        return None
    for value in superseded:
        if not isinstance(value, str) or not value:
            return None
        try:
            references.add(Path(value).resolve(strict=False))
        except OSError:
            return None
    return references


def _find_hook_input_job_locked(
    state_dir: Path,
    payload: object,
    *,
    deadline: float | None = None,
) -> tuple[Path, str] | None:
    target = _hook_input_reference(payload)
    if target is None:
        return None
    for state in JOB_STATES:
        _check_deadline(deadline)
        for path in (_job_root(state_dir) / state).glob("*.json"):
            _check_deadline(deadline)
            if state == "quarantined":
                try:
                    tombstone = _load_job(path)
                    payload_path = path.with_name(str(tombstone["payload_file"]))
                    original = json.loads(payload_path.read_text(encoding="utf-8"))
                    job_payload = original.get("payload") if isinstance(original, dict) else None
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError):
                    continue
            else:
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(job, dict):
                    continue
                recorded_state = job.get("status")
                destination_state = (
                    recorded_state
                    if isinstance(recorded_state, str)
                    and (
                        recorded_state == state
                        or recorded_state in RECOVERABLE_TRANSITIONS.get(state, set())
                    )
                    else None
                )
                if destination_state is None:
                    job_references = _hook_input_references(job.get("payload"))
                    if job_references is None or target in job_references:
                        return path, "invalid"
                    continue
                destination = _job_root(state_dir) / destination_state / path.name
                try:
                    _validate_job(destination, job)
                except ValueError:
                    # A real corrupt record remains for the normal quarantine
                    # path; lookup must never quarantine it as a side effect.
                    job_references = _hook_input_references(job.get("payload"))
                    if job_references is None or target in job_references:
                        return path, "invalid"
                    continue
                job_payload = job.get("payload")
            references = _hook_input_references(job_payload)
            if references is not None and target in references:
                return path, state
    return None


def _referenced_hook_inputs_locked(
    state_dir: Path,
    *,
    excluded_paths: set[Path] | None = None,
    deadline: float | None = None,
) -> set[Path] | None:
    references: set[Path] = set()
    excluded = excluded_paths or set()
    for state in ("pending", "claimed", "running", "dead-letter"):
        _check_deadline(deadline)
        for job_path in (_job_root(state_dir) / state).glob("*.json"):
            _check_deadline(deadline)
            if job_path.resolve(strict=False) in excluded:
                continue
            try:
                payload = _load_job(job_path).get("payload", {})
            except ValueError:
                return None
            job_references = _hook_input_references(payload)
            if job_references is None:
                return None
            references.update(job_references)
    for tombstone in (_job_root(state_dir) / "quarantined").glob("*.json"):
        _check_deadline(deadline)
        if tombstone.resolve(strict=False) in excluded:
            continue
        try:
            tombstone_value = _load_job(tombstone)
            payload_path = tombstone.with_name(str(tombstone_value["payload_file"]))
            original = json.loads(payload_path.read_text(encoding="utf-8"))
            payload = original.get("payload") if isinstance(original, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError):
            return None
        job_references = _hook_input_references(payload)
        if job_references is None:
            return None
        references.update(job_references)
    return references


def _succeeded_hook_inputs_locked(
    state_dir: Path,
    *,
    deadline: float | None = None,
) -> set[Path] | None:
    references: set[Path] = set()
    for job_path in (_job_root(state_dir) / "succeeded").glob("*.json"):
        _check_deadline(deadline)
        try:
            payload = _load_job(job_path).get("payload", {})
        except ValueError:
            return None
        job_references = _hook_input_references(payload)
        if job_references is None:
            return None
        references.update(job_references)
    return references


def _sweep_stale_hook_inputs(state_dir: Path, now_epoch: float) -> None:
    if not state_dir.exists():
        return
    with locked(state_dir / "worker-queue"):
        referenced = _referenced_hook_inputs_locked(state_dir)
        if referenced is None:
            return
        for candidate in state_dir.glob("hookin-*.json"):
            try:
                if (
                    _managed_hook_input(candidate, state_dir)
                    and candidate.resolve(strict=False) not in referenced
                    and _hook_input_delivery_payload(state_dir, candidate) is None
                    and now_epoch - candidate.lstat().st_mtime >= STALE_HOOK_INPUT_SECONDS
                ):
                    candidate.unlink()
            except OSError:
                # Kayıp ya da kilitli artık: sweep hiçbir zaman supervisor'ı düşürmez.
                continue


def _job_id_from_path(path: Path) -> str | None:
    match = re.fullmatch(r"job-([0-9a-f]{32})\.json", path.name)
    return match.group(1) if match else None


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _validate_job(path: Path, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("worker-job-not-object")
    state = path.parent.name
    job_id = value.get("job_id")
    if state == "quarantined":
        if (
            value.get("schema_version") != JOB_SCHEMA_VERSION
            or value.get("status") != "quarantined"
            or not isinstance(job_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", job_id) is None
            or path.name != f"job-{job_id}.json"
            or not isinstance(value.get("payload_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["payload_sha256"]) is None
            or not isinstance(value.get("reason_code"), str)
            or value["reason_code"]
            not in {"worker-job-json-invalid", "worker-job-schema-invalid"}
        ):
            raise ValueError("worker-quarantine-tombstone-invalid")
        return value
    if state not in JOB_STATES or state == "quarantined":
        raise ValueError("worker-job-state-invalid")
    if (
        value.get("schema_version") != JOB_SCHEMA_VERSION
        or not isinstance(job_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", job_id) is None
        or path.name != f"job-{job_id}.json"
        or not isinstance(value.get("kind"), str)
        or value["kind"] not in JOB_KINDS
        or value.get("status") != state
        or not isinstance(value.get("payload"), dict)
        or isinstance(value.get("generation"), bool)
        or not isinstance(value.get("generation"), int)
        or value["generation"] < 1
        or isinstance(value.get("attempt"), bool)
        or not isinstance(value.get("attempt"), int)
        or not 0 <= value["attempt"] <= MAX_ATTEMPTS
        or isinstance(value.get("enqueue_sequence"), bool)
        or not isinstance(value.get("enqueue_sequence"), int)
        or value["enqueue_sequence"] < 1
        or any(
            key in value and not _finite_number(value[key])
            for key in (
                "enqueued_ts",
                "retry_scheduled_ts",
                "next_attempt_ts",
                "claimed_ts",
                "running_ts",
                "adopted_ts",
                "recovered_ts",
                "finished_ts",
                "lease_until",
            )
        )
    ):
        raise ValueError("worker-job-schema-invalid")
    if state in {"claimed", "running"} and (
        value["attempt"] < 1
        or not isinstance(value.get("claim_token"), str)
        or re.fullmatch(r"[0-9a-f]{32}", value["claim_token"]) is None
        or isinstance(value.get("owner_pid"), bool)
        or not isinstance(value.get("owner_pid"), int)
        or value["owner_pid"] < 1
        or isinstance(value.get("lease_until"), bool)
        or not isinstance(value.get("lease_until"), (int, float))
        or value["lease_until"] <= 0
        or (
            "owner_identity" in value
            and (
                not isinstance(value["owner_identity"], str)
                or not value["owner_identity"]
            )
        )
    ):
        raise ValueError("worker-job-schema-invalid")
    if state in {"succeeded", "dead-letter"} and (
        not isinstance(value.get("finished_ts"), int)
        or value.get("claim_token") is not None
    ):
        raise ValueError("worker-job-schema-invalid")
    return value


def _load_job(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("worker-job-json-invalid") from exc
    return _validate_job(path, value)


def _quarantine_job(
    state_dir: Path,
    path: Path,
    reason_code: str,
    *,
    deadline: float | None = None,
) -> None:
    try:
        payload = path.read_bytes()
        job_id = _job_id_from_path(path) or hashlib.sha256(
            path.name.encode("utf-8") + b"\0" + payload
        ).hexdigest()[:32]
        quarantine = _job_root(state_dir) / "quarantined"
        tombstone = quarantine / f"job-{job_id}.json"
        payload_path = tombstone.with_suffix(".payload")
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        atomic_write_bytes(payload_path, payload, deadline=deadline)
        atomic_write_json(
            tombstone,
            {
                "schema_version": JOB_SCHEMA_VERSION,
                "job_id": job_id,
                "status": "quarantined",
                "reason_code": reason_code,
                "payload_sha256": payload_sha256,
                "payload_file": payload_path.name,
                "source_state": path.parent.name,
                "quarantined_ts": int(time.time()),
            },
            sort_keys=True,
            deadline=deadline,
        )
        path.unlink()
    except OSError as exc:
        raise ValueError("worker-quarantine-failed") from exc


def _load_job_quarantined(
    state_dir: Path,
    path: Path,
    *,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    try:
        return _load_job(path)
    except ValueError as exc:
        reason = (
            "worker-job-json-invalid"
            if str(exc) == "worker-job-json-invalid"
            else "worker-job-schema-invalid"
        )
        _quarantine_job(state_dir, path, reason, deadline=deadline)
        return None


def _job_root(state_dir: Path) -> Path:
    return state_dir / "worker-jobs"


def _ensure_job_dirs(state_dir: Path) -> None:
    for name in JOB_STATES:
        (_job_root(state_dir) / name).mkdir(parents=True, exist_ok=True)


def _flush_scope(job: dict[str, Any]) -> tuple[str, str] | None:
    reference = _hook_input_reference(job.get("payload"))
    if reference is None or not reference.is_file():
        return None
    try:
        hook_payload = load_hook_input(reference)
        session_id = hook_payload.get("session_id")
        transcript = hook_payload.get("transcript_path")
        if not isinstance(session_id, str) or not session_id:
            return None
        if not isinstance(transcript, str) or not transcript:
            return None
        transcript_path = Path(transcript).expanduser().resolve(strict=False)
    except (OSError, UnicodeError, ValueError):
        return None
    return session_id, os.path.normcase(str(transcript_path))


def _flush_event_sort_key(payload: dict[str, Any]) -> tuple[int, float, str]:
    event_iso = payload.get("event_iso")
    try:
        event = dt.datetime.fromisoformat(str(event_iso))
        if event.tzinfo is None:
            event = event.replace(tzinfo=dt.timezone.utc)
        return 0, event.timestamp(), str(event_iso)
    except (TypeError, ValueError, OverflowError):
        return 0, float("-inf"), str(event_iso)


def _merge_flush_payload(
    current: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(current)
    superseded: set[str] = set()
    for source in (current, incoming):
        values = source.get("superseded_hook_inputs", [])
        if isinstance(values, list):
            superseded.update(value for value in values if isinstance(value, str) and value)
    current_reason = current.get("reason")
    incoming_reason = incoming.get("reason")
    current_priority = FLUSH_REASON_PRIORITY.get(str(current_reason), -1)
    incoming_priority = FLUSH_REASON_PRIORITY.get(str(incoming_reason), -1)
    current_event = _flush_event_sort_key(current)
    incoming_event = _flush_event_sort_key(incoming)
    incoming_reason_wins = (
        incoming_priority > current_priority
        or (incoming_priority == current_priority and incoming_event >= current_event)
    )
    if incoming_reason_wins:
        if "reason" in incoming:
            merged["reason"] = incoming["reason"]
    incoming_metadata_wins = incoming_event >= current_event
    if incoming_metadata_wins and "event_iso" in incoming:
        merged["event_iso"] = incoming["event_iso"]
    if incoming_metadata_wins and "hook_input" in incoming:
        merged["hook_input"] = incoming["hook_input"]
    if incoming_metadata_wins:
        for field in FLUSH_CONTINUATION_FIELDS:
            if field in incoming:
                merged[field] = incoming[field]
    if superseded:
        merged["superseded_hook_inputs"] = sorted(superseded)
    else:
        merged.pop("superseded_hook_inputs", None)
    return merged


def _job_sort_key(job: dict[str, Any], path: Path) -> tuple[int, int, str]:
    enqueue_sequence = job.get("enqueue_sequence")
    sequence_sort = (
        enqueue_sequence
        if isinstance(enqueue_sequence, int) and enqueue_sequence >= 0
        else 2**63 - 1
    )
    enqueued_ts = job.get("enqueued_ts", 0)
    timestamp_sort = int(enqueued_ts) if isinstance(enqueued_ts, (int, float)) else 0
    return (sequence_sort, timestamp_sort, str(job.get("job_id", path.name)))


def _coalesce_pending_flush_locked(
    state_dir: Path,
    payload: dict[str, Any],
    *,
    now: float,
    deadline: float | None = None,
) -> Path | None:
    incoming = {"payload": payload}
    scope = _flush_scope(incoming)
    if scope is None:
        return None
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in (_job_root(state_dir) / "pending").glob("*.json"):
        _check_deadline(deadline)
        job = _load_job_quarantined(state_dir, path, deadline=deadline)
        if job is None or job.get("kind") != "flush":
            continue
        if _flush_scope(job) == scope:
            matches.append((path, job))
    if not matches:
        return None
    matches.sort(key=lambda item: _job_sort_key(item[1], item[0]))
    retained_path, retained = matches[0]
    old_inputs: list[Path] = []
    for _path, job in matches:
        references = _hook_input_references(job.get("payload"))
        if references is not None:
            old_inputs.extend(references)
    incoming_references = _hook_input_references(payload)
    if incoming_references is not None:
        old_inputs.extend(incoming_references)
    merged_payload = retained.get("payload", {})
    for _path, job in matches[1:]:
        merged_payload = _merge_flush_payload(
            merged_payload,
            job.get("payload", {}),
        )
    retained["payload"] = _merge_flush_payload(merged_payload, payload)
    current_input = _hook_input_reference(retained["payload"])
    initial_superseded = set(old_inputs)
    if current_input is not None:
        initial_superseded.discard(current_input)
    if initial_superseded:
        retained["payload"]["superseded_hook_inputs"] = sorted(
            str(path) for path in initial_superseded
        )
    else:
        retained["payload"].pop("superseded_hook_inputs", None)
    retained["generation"] = int(retained.get("generation", 0)) + 1
    retained["coalesced_ts"] = int(now)
    atomic_write_json(retained_path, retained, deadline=deadline)
    excluded_paths = {retained_path.resolve(strict=False)}
    for duplicate_path, _job in matches[1:]:
        try:
            duplicate_path.unlink()
            excluded_paths.add(duplicate_path.resolve(strict=False))
        except OSError:
            continue
    references = _referenced_hook_inputs_locked(
        state_dir,
        excluded_paths=excluded_paths,
        deadline=deadline,
    )
    if references is not None:
        external_references = references
        superseded_failures: set[Path] = set()
        superseded_external: set[Path] = set()
        for old_input in old_inputs:
            if (
                old_input != current_input
                and _managed_hook_input(old_input, state_dir)
            ):
                if old_input in external_references:
                    superseded_external.add(old_input)
                    continue
                try:
                    old_input.unlink(missing_ok=True)
                except OSError:
                    superseded_failures.add(old_input)
        merged_payload = dict(retained.get("payload", {}))
        superseded_values = {
            str(path) for path in superseded_external | superseded_failures
        }
        if superseded_values:
            merged_payload["superseded_hook_inputs"] = sorted(superseded_values)
        else:
            merged_payload.pop("superseded_hook_inputs", None)
        if merged_payload != retained.get("payload", {}):
            retained["payload"] = merged_payload
            atomic_write_json(retained_path, retained, deadline=deadline)
    return retained_path


def _unresolved_job_count_locked(
    state_dir: Path,
    *,
    deadline: float | None = None,
) -> int:
    count = 0
    for state in ("pending", "claimed", "running", "dead-letter"):
        _check_deadline(deadline)
        for path in (_job_root(state_dir) / state).glob("*.json"):
            _check_deadline(deadline)
            if _load_job_quarantined(state_dir, path, deadline=deadline) is not None:
                count += 1
    # A quarantine tombstone is unresolved work even when its original schema
    # is no longer readable, so it remains part of admission accounting.
    for _path in (_job_root(state_dir) / "quarantined").glob("*.json"):
        _check_deadline(deadline)
        count += 1
    return count


class WorkerQueueBackpressure(RuntimeError):
    """The durable queue has no admission capacity for a new logical job."""


class WorkerDeliveryTimeout(RuntimeError):
    """The hook deadline expired before queue or supervisor admission."""


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise WorkerDeliveryTimeout("worker-queue-deadline")


def _sha256_file_with_deadline(
    path: Path,
    *,
    deadline: float | None = None,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _check_deadline(deadline)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


class WorkerDeliveryTerminal(RuntimeError):
    """A transport already has a terminal queue record and cannot be replayed."""


def _lock_timeout(deadline: float | None) -> float | None:
    return timeout_for_deadline(deadline)


def _enqueue_job_locked(
    state_dir: Path,
    kind: str,
    payload: dict[str, Any],
    *,
    observed_now: float,
    deadline: float | None = None,
) -> Path:
    if kind == "flush":
        existing = _find_hook_input_job_locked(
            state_dir,
            payload,
            deadline=deadline,
        )
        if existing is not None:
            existing_path, existing_state = existing
            if existing_state in {"dead-letter", "quarantined", "invalid"}:
                raise WorkerDeliveryTerminal("worker-input-terminal")
            return existing_path
        hook_input = _hook_input_reference(payload)
        if (
            hook_input is not None
            and _managed_hook_input(hook_input, state_dir)
            and not hook_input.is_file()
        ):
            raise WorkerDeliveryTerminal("worker-input-missing")
    path = (
        _coalesce_pending_flush_locked(
            state_dir,
            payload,
            now=observed_now,
            deadline=deadline,
        )
        if kind == "flush"
        else None
    )
    if path is not None:
        return path
    if _unresolved_job_count_locked(state_dir, deadline=deadline) >= MAX_UNRESOLVED_JOBS:
        raise WorkerQueueBackpressure("worker-queue-backpressure")
    sequence_path = state_dir / "worker-sequence.json"
    try:
        sequence_state = json.loads(sequence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        sequence_state = {}
    previous_sequence = (
        sequence_state.get("enqueue_sequence", 0)
        if isinstance(sequence_state, dict)
        else 0
    )
    enqueue_sequence = (
        previous_sequence + 1
        if isinstance(previous_sequence, int) and previous_sequence >= 0
        else 1
    )
    atomic_write_json(
        sequence_path,
        {
            "schema_version": JOB_SCHEMA_VERSION,
            "enqueue_sequence": enqueue_sequence,
        },
        deadline=deadline,
    )
    job_id = uuid.uuid4().hex
    job = {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "kind": kind,
        "status": "pending",
        "generation": 1,
        "attempt": 0,
        "enqueue_sequence": enqueue_sequence,
        "enqueued_ts": int(observed_now),
        "payload": payload,
    }
    path = _job_root(state_dir) / "pending" / f"job-{job_id}.json"
    atomic_write_json(path, job, deadline=deadline)
    return path


def _hook_input_delivery_payload(
    state_dir: Path,
    path: Path,
) -> dict[str, Any] | None:
    if not _managed_hook_input(path, state_dir):
        return None
    try:
        value = load_hook_input(path)
    except (OSError, UnicodeError, ValueError):
        return None
    if value.get("delivery_schema_version") != HOOK_INPUT_SCHEMA_VERSION:
        return None
    session_id = value.get("session_id")
    reason = value.get("reason")
    event_iso = value.get("event_iso")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(reason, str)
        or reason not in FLUSH_REASON_PRIORITY
        or not isinstance(event_iso, str)
        or not event_iso
    ):
        return None
    payload: dict[str, Any] = {
        "hook_input": str(path),
        "reason": reason,
        "event_iso": event_iso,
    }
    for field in FLUSH_CONTINUATION_FIELDS:
        if field in value:
            payload[field] = value[field]
    return payload


def _hook_input_recovery_sort_key(
    payload: dict[str, Any],
    path: Path,
) -> tuple[int, float, str]:
    event_iso = payload.get("event_iso")
    try:
        event = dt.datetime.fromisoformat(str(event_iso))
        if event.tzinfo is None:
            event = event.replace(tzinfo=dt.timezone.utc)
        return 0, event.timestamp(), path.name
    except (TypeError, ValueError, OverflowError):
        return 1, float("inf"), path.name


def _recover_orphan_hook_inputs_locked(
    state_dir: Path,
    *,
    now: float,
) -> int:
    """Admit transports left behind by a host timeout, under queue ownership."""
    recovered = 0
    candidates: list[tuple[tuple[int, float, str], Path, dict[str, Any]]] = []
    for candidate in state_dir.glob("hookin-*.json"):
        payload = _hook_input_delivery_payload(state_dir, candidate)
        if payload is None:
            continue
        candidates.append((_hook_input_recovery_sort_key(payload, candidate), candidate, payload))
    candidates.sort(key=lambda item: item[0])
    for _sort_key, candidate, _payload in candidates:
        payload = _hook_input_delivery_payload(state_dir, candidate)
        if payload is None:
            continue
        reference_sets = _orphan_reference_sets_locked(state_dir)
        if reference_sets is None:
            continue
        references, completed = reference_sets
        reference = candidate.resolve(strict=False)
        if reference in completed or reference in references:
            continue
        try:
            _enqueue_job_locked(state_dir, "flush", payload, observed_now=now)
        except WorkerQueueBackpressure:
            # Keep the source transport for a later admission attempt.
            continue
        recovered += 1
    return recovered


def _orphan_reference_sets_locked(
    state_dir: Path,
    *,
    deadline: float | None = None,
) -> tuple[set[Path], set[Path]] | None:
    references = _referenced_hook_inputs_locked(state_dir, deadline=deadline)
    completed = _succeeded_hook_inputs_locked(state_dir, deadline=deadline)
    if references is None or completed is None:
        return None
    return references, completed


def _has_recoverable_hook_inputs_locked(state_dir: Path) -> bool | None:
    reference_sets = _orphan_reference_sets_locked(state_dir)
    if reference_sets is None:
        return None
    references, completed = reference_sets
    for candidate in sorted(state_dir.glob("hookin-*.json")):
        if _hook_input_delivery_payload(state_dir, candidate) is None:
            continue
        reference = candidate.resolve(strict=False)
        if reference in references or reference in completed:
            continue
        if _find_hook_input_job_locked(
            state_dir,
            {"hook_input": str(candidate)},
        ) is None:
            return True
    return False


def recover_orphan_hook_inputs(
    state_dir: Path,
    *,
    now: float | None = None,
) -> int:
    observed_now = time.time() if now is None else now
    _ensure_job_dirs(state_dir)
    with locked(state_dir / "worker-queue"):
        return _recover_orphan_hook_inputs_locked(state_dir, now=observed_now)


def count_orphan_hook_inputs(
    state_dir: Path,
    *,
    strict: bool = False,
    deadline: float | None = None,
) -> int:
    """Read-only wake-up hint for SessionStart; races are resolved by recovery."""
    reference_sets = _orphan_reference_sets_locked(state_dir, deadline=deadline)
    if reference_sets is None:
        if strict:
            raise ValueError("worker-hook-input-references-unreadable")
        return 0
    references, completed = reference_sets
    count = 0
    for candidate in state_dir.glob("hookin-*.json"):
        _check_deadline(deadline)
        payload = _hook_input_delivery_payload(state_dir, candidate)
        if payload is None:
            if strict:
                raise ValueError("worker-hook-input-invalid")
            continue
        reference = candidate.resolve(strict=False)
        if (
            reference not in references
            and reference not in completed
            and _find_hook_input_job_locked(
                state_dir,
                payload,
                deadline=deadline,
            ) is None
        ):
            count += 1
    return count


def _pinned_successor_ids_locked(state_dir: Path) -> set[str] | None:
    pinned: set[str] = set()
    for path in (_job_root(state_dir) / "dead-letter").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(job, dict):
            return None
        if (
            job.get("terminal_reason") == "recovered-by-successor"
            and job.get("retryable") is False
        ):
            successor = job.get("recovery_job_id")
            if not isinstance(successor, str) or re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", successor
            ) is None:
                continue
            pinned.add(successor)
            if len(pinned) > MAX_PINNED_SUCCESSOR_RECEIPTS:
                return None
    return pinned


def _prune_succeeded_jobs_locked(state_dir: Path, now: float) -> None:
    pinned = _pinned_successor_ids_locked(state_dir)
    if pinned is None:
        return
    receipts: list[tuple[int, Path]] = []
    for path in (_job_root(state_dir) / "succeeded").glob("*.json"):
        try:
            job = _load_job(path)
        except ValueError:
            continue
        finished_ts = job.get("finished_ts")
        if isinstance(finished_ts, int) and not isinstance(finished_ts, bool):
            receipts.append((finished_ts, path))
    receipts.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    ordinary_index = 0
    for finished_ts, path in receipts:
        try:
            job = _load_job(path)
            job_id = job["job_id"]
        except ValueError:
            continue
        if job.get("hook_input_cleanup_pending") is True:
            continue
        if job_id in pinned:
            continue
        if (
            ordinary_index >= MAX_SUCCEEDED_RECEIPTS
            or now - finished_ts >= SUCCEEDED_RECEIPT_MAX_AGE_SECONDS
        ):
            try:
                path.unlink()
            except OSError:
                continue
        ordinary_index += 1


def _cleanup_succeeded_hook_inputs_locked(state_dir: Path) -> int:
    cleaned = 0
    for path in (_job_root(state_dir) / "succeeded").glob("*.json"):
        try:
            job = _load_job(path)
        except ValueError:
            continue
        payload = job.get("payload")
        references = _hook_input_references(payload)
        if not references:
            continue
        current = _hook_input_reference(payload)
        external_references = _referenced_hook_inputs_locked(
            state_dir,
            excluded_paths={path.resolve(strict=False)},
        )
        if external_references is None:
            external_references = set()
            external_lookup_failed = True
        else:
            external_lookup_failed = False
        failures: set[Path] = set()
        for reference in references:
            if not _managed_hook_input(reference, state_dir):
                continue
            if external_lookup_failed or reference in external_references:
                failures.add(reference)
                continue
            try:
                reference.unlink(missing_ok=True)
            except OSError:
                failures.add(reference)
        was_pending = job.get("hook_input_cleanup_pending") is True
        was_consumed = job.get("hook_input_consumed") is True
        current_managed = current is not None and _managed_hook_input(current, state_dir)
        new_pending = bool(failures or external_lookup_failed)
        if failures or external_lookup_failed:
            job["hook_input_cleanup_pending"] = True
            if current_managed and current not in failures:
                job["hook_input_consumed"] = True
        else:
            job.pop("hook_input_cleanup_pending", None)
            if current_managed:
                job["hook_input_consumed"] = True
        remaining_superseded = failures - ({current} if current is not None else set())
        updated_payload = dict(payload) if isinstance(payload, dict) else {}
        if remaining_superseded:
            updated_payload["superseded_hook_inputs"] = sorted(
                str(reference) for reference in remaining_superseded
            )
        else:
            updated_payload.pop("superseded_hook_inputs", None)
        changed = (
            updated_payload != payload
            or was_pending != new_pending
            or (not new_pending and current_managed and not was_consumed)
        )
        job["payload"] = updated_payload
        if changed:
            atomic_write_json(path, job)
        if not failures:
            cleaned += 1
    return cleaned


def prune_succeeded_jobs(state_dir: Path, *, now: float | None = None) -> None:
    observed_now = time.time() if now is None else now
    _ensure_job_dirs(state_dir)
    with locked(state_dir / "worker-queue"):
        _cleanup_succeeded_hook_inputs_locked(state_dir)
        _prune_succeeded_jobs_locked(state_dir, observed_now)


def migrate_legacy_failed_jobs(
    state_dir: Path,
    *,
    job_id: str | None = None,
    now: float | None = None,
    _fail_after: str | None = None,
) -> int:
    observed_now = time.time() if now is None else now
    source_dir = _job_root(state_dir) / "failed"
    if not source_dir.is_dir():
        return 0
    _ensure_job_dirs(state_dir)
    migrated = 0
    with locked(state_dir / "worker-queue"):
        for source in sorted(source_dir.glob("job-*.json")):
            if job_id is not None and _job_id_from_path(source) != job_id:
                continue
            try:
                original = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("worker-legacy-state-invalid") from exc
            record_id = _job_id_from_path(source)
            if (
                not isinstance(original, dict)
                or record_id is None
                or original.get("job_id") != record_id
                or original.get("status") != "failed"
                or not isinstance(original.get("kind"), str)
                or original["kind"] not in JOB_KINDS
                or not isinstance(original.get("payload"), dict)
            ):
                raise ValueError("worker-legacy-state-invalid")
            seen_records, _ = _scan_redrive_records_locked(
                state_dir, include_legacy_failed=False
            )
            if record_id in seen_records:
                active = seen_records[record_id]
                if not isinstance(active, dict) or not _same_redrive_identity(
                    active, original
                ):
                    raise ValueError("worker-legacy-state-conflict")
                if not _has_redrive_marker(active):
                    # Without an explicit redrive marker, the active copy's
                    # chronology is ambiguous. Preserve the legacy failure as
                    # a terminal conflict instead of discarding either copy.
                    destination = _job_root(state_dir) / "dead-letter" / source.name
                    conflict = dict(original)
                    _mark_redrive_conflict(
                        conflict, active, now=observed_now
                    )
                    conflict.pop("claim_token", None)
                    conflict.pop("owner_pid", None)
                    conflict.pop("owner_identity", None)
                    conflict["lease_until"] = 0
                    if destination.exists():
                        try:
                            existing = _load_job(destination)
                        except ValueError as exc:
                            raise ValueError("worker-legacy-state-conflict") from exc
                        if not _same_redrive_identity(existing, original):
                            raise ValueError("worker-legacy-state-conflict")
                        if _same_migration_record(existing, conflict):
                            source.unlink()
                            migrated += 1
                            continue
                        _mark_redrive_conflict(existing, active, now=observed_now)
                        atomic_write_json(destination, existing, sort_keys=True)
                        continue
                    atomic_write_json(destination, conflict, sort_keys=True)
                    source.unlink()
                    migrated += 1
                    continue
                # A newer queue record is already the canonical identity.  Do
                # not recreate a dead-letter copy from the legacy source.
                source.unlink()
                migrated += 1
                continue
            after = dict(original)
            after.update(
                {
                    "schema_version": JOB_SCHEMA_VERSION,
                    "status": "dead-letter",
                    "terminal_reason": "legacy-failed",
                    "retryable": False,
                    "finished_ts": int(observed_now),
                    "lease_until": 0,
                }
            )
            after.pop("claim_token", None)
            destination = _job_root(state_dir) / "dead-letter" / source.name
            if destination.exists():
                try:
                    existing = _load_job(destination)
                except ValueError as exc:
                    raise ValueError("worker-legacy-state-conflict") from exc
                if not _same_redrive_identity(existing, original):
                    raise ValueError("worker-legacy-state-conflict")
                if not _same_migration_record(existing, after):
                    # A same-identity dead-letter is not proof of an interrupted
                    # migration. Keep the legacy source and fence the collision.
                    _mark_redrive_conflict(existing, original, now=observed_now)
                    atomic_write_json(destination, existing, sort_keys=True)
                    continue
                receipt = state_dir / f"worker-migration-{record_id}.json"
                atomic_write_json(
                    receipt,
                    {
                        "schema_version": 1,
                        "status": "durable",
                        "job_id": record_id,
                        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "destination_sha256": hashlib.sha256(
                            destination.read_bytes()
                        ).hexdigest(),
                        "migrated_ts": int(observed_now),
                    },
                    sort_keys=True,
                )
                source.unlink()
                migrated += 1
                continue
            atomic_write_json(destination, after, sort_keys=True)
            if _fail_after == "destination":
                raise RuntimeError("worker-migration-injected:destination")
            receipt = state_dir / f"worker-migration-{record_id}.json"
            atomic_write_json(
                receipt,
                {
                    "schema_version": 1,
                    "status": "durable",
                    "job_id": record_id,
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "destination_sha256": hashlib.sha256(
                        destination.read_bytes()
                    ).hexdigest(),
                    "migrated_ts": int(observed_now),
                },
                sort_keys=True,
            )
            if _fail_after == "receipt":
                raise RuntimeError("worker-migration-injected:receipt")
            source.unlink()
            migrated += 1
    return migrated


def _has_verified_successor(
    root: Path,
    job: dict[str, Any],
    *,
    deadline: float | None = None,
) -> bool:
    _check_deadline(deadline)
    if (
        job.get("terminal_reason") != "recovered-by-successor"
        or job.get("retryable") is not False
    ):
        return False
    recovery_job_id = job.get("recovery_job_id")
    if not isinstance(recovery_job_id, str) or re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", recovery_job_id
    ) is None:
        return False
    successor_path = root / "succeeded" / f"job-{recovery_job_id}.json"
    try:
        successor = _load_job(successor_path)
    except ValueError:
        return False
    return (
        successor.get("status") == "succeeded"
        and successor.get("job_id") == recovery_job_id
        and successor.get("kind") == job.get("kind")
        and successor.get("payload") == job.get("payload")
    )


def _has_redrive_marker(job: dict[str, Any]) -> bool:
    marker = job.get("redriven_ts")
    return isinstance(marker, int) and not isinstance(marker, bool) and marker > 0


def _same_redrive_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left.get("kind") == right.get("kind") and left.get("payload") == right.get(
        "payload"
    )


def _same_migration_record(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_without_finished = dict(left)
    right_without_finished = dict(right)
    left_without_finished.pop("finished_ts", None)
    right_without_finished.pop("finished_ts", None)
    return left_without_finished == right_without_finished


def _redrive_conflict_reason(
    active: dict[str, Any] | None, dead_letter: dict[str, Any]
) -> str:
    return (
        "redrive-conflict"
        if isinstance(active, dict) and _same_redrive_identity(active, dead_letter)
        else "redrive-identity-conflict"
    )


def _mark_redrive_conflict(
    job: dict[str, Any], active: dict[str, Any] | None, *, now: float
) -> None:
    reason = _redrive_conflict_reason(active, job)
    # A recovered interrupted transition may still say pending in dead-letter.
    # Make the durable conflict terminal so later recovery cannot reactivate it.
    job["status"] = "dead-letter"
    job["terminal_reason"] = reason
    job["retryable"] = False
    if (
        not isinstance(job.get("finished_ts"), int)
        or isinstance(job.get("finished_ts"), bool)
    ):
        job["finished_ts"] = int(now)
    if not isinstance(job.get("last_error"), str) or not job["last_error"]:
        job["last_error"] = reason


def _scan_redrive_records_locked(
    state_dir: Path,
    *,
    include_legacy_failed: bool = True,
    job_id: str | None = None,
) -> tuple[dict[str, dict[str, Any] | None], dict[str, dict[str, Any]]]:
    """Return queue identities and marked redrive receipts under queue ownership."""
    seen_records: dict[str, dict[str, Any] | None] = {}
    seen_redriven: dict[str, dict[str, Any]] = {}
    states = [
        "pending",
        "claimed",
        "running",
        "succeeded",
        "quarantined",
    ]
    if include_legacy_failed:
        states.append("failed")
    for state in states:
        for path in sorted((_job_root(state_dir) / state).glob("*.json")):
            path_job_id = _job_id_from_path(path)
            if (
                job_id is not None
                and path_job_id != job_id
            ):
                continue
            try:
                if state == "failed":
                    record_id = _job_id_from_path(path)
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if (
                        not isinstance(value, dict)
                        or record_id is None
                        or value.get("job_id") != record_id
                        or not isinstance(value.get("kind"), str)
                        or value["kind"] not in JOB_KINDS
                        or not isinstance(value.get("payload"), dict)
                    ):
                        raise ValueError("worker-legacy-state-invalid")
                    job = {
                        "job_id": record_id,
                        "kind": value["kind"],
                        "payload": value["payload"],
                    }
                elif state == "quarantined":
                    tombstone = _load_job(path)
                    job = {
                        "job_id": tombstone["job_id"],
                        "kind": None,
                        "payload": None,
                    }
                    try:
                        payload_path = path.with_name(
                            str(tombstone["payload_file"])
                        )
                        original = json.loads(
                            payload_path.read_text(encoding="utf-8")
                        )
                    except (
                        OSError,
                        UnicodeError,
                        json.JSONDecodeError,
                        KeyError,
                        TypeError,
                        ValueError,
                    ):
                        original = None
                    if isinstance(original, dict):
                        job["kind"] = original.get("kind")
                        job["payload"] = original.get("payload")
                else:
                    job = _load_job(path)
            except ValueError:
                record_id = _job_id_from_path(path)
                if record_id is not None:
                    seen_records.setdefault(record_id, None)
                continue
            if (
                job["job_id"] not in seen_records
                or seen_records[job["job_id"]] is None
            ):
                seen_records[job["job_id"]] = job
            if state not in {"quarantined", "failed"} and _has_redrive_marker(job):
                seen_redriven.setdefault(job["job_id"], job)
    return seen_records, seen_redriven


def _console_safe(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def _has_pending_redrive(state_dir: Path, *, job_id: str | None = None) -> bool:
    with locked(state_dir / "worker-queue"):
        for path in sorted((_job_root(state_dir) / "pending").glob("*.json")):
            try:
                job = _load_job(path)
            except ValueError:
                continue
            if _has_redrive_marker(job) and (
                job_id is None or job["job_id"] == job_id
            ):
                return True
    return False


def _redrive_supervisor_state(state_dir: Path, *, now: float | None = None) -> str:
    """Classify a supervisor handoff after a duplicate launch was suppressed."""
    observed_now = time.time() if now is None else now
    with locked(state_dir / "worker-admission"):
        try:
            receipt = json.loads(
                _supervisor_receipt_path(state_dir).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "uncertain"
        if not isinstance(receipt, dict):
            return "uncertain"
        if (
            receipt.get("status") == "running"
            and _process_owner_is_active(receipt)
        ):
            if _process_owner_classification(receipt) in {"invalid", "unreadable"}:
                return "uncertain"
            return "active"
        lease_until = receipt.get("lease_until")
        if (
            receipt.get("status") == "launching"
            and isinstance(lease_until, (int, float))
            and not isinstance(lease_until, bool)
            and lease_until > observed_now
        ):
            return "deferred"
    return "uncertain"


def _redrive_state(state_dir: Path, job_id: str) -> str | None:
    with locked(state_dir / "worker-queue"):
        for state in ("pending", "claimed", "running", "succeeded"):
            path = _job_root(state_dir) / state / f"job-{job_id}.json"
            try:
                job = _load_job(path)
            except ValueError:
                continue
            if job["job_id"] == job_id and _has_redrive_marker(job):
                return state
    return None


def inspect_worker_queue(
    state_dir: Path,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    try:
        with locked(state_dir / "worker-queue", timeout=_lock_timeout(deadline)):
            return _inspect_worker_queue_locked(state_dir, deadline=deadline)
    except LockUnavailable as exc:
        if deadline is None:
            raise
        raise WorkerDeliveryTimeout("worker-queue-deadline") from exc


def _inspect_worker_queue_locked(
    state_dir: Path,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    _check_deadline(deadline)
    root = _job_root(state_dir)
    counts = {state: 0 for state in JOB_STATES}
    invalid = 0
    generation = 0
    terminal = {"recovered": 0, "unresolved": 0}
    orphan_hook_inputs = count_orphan_hook_inputs(
        state_dir,
        deadline=deadline,
    )
    for state in JOB_STATES:
        _check_deadline(deadline)
        for path in (root / state).glob("*.json"):
            _check_deadline(deadline)
            try:
                job = _load_job(path)
                if state == "quarantined":
                    payload_path = path.with_name(str(job.get("payload_file", "")))
                    if (
                        not payload_path.is_file()
                        or _sha256_file_with_deadline(
                            payload_path,
                            deadline=deadline,
                        )
                        != job["payload_sha256"]
                    ):
                        raise ValueError("worker-quarantine-payload-invalid")
            except ValueError:
                invalid += 1
                if state in {"dead-letter", "quarantined"}:
                    terminal["unresolved"] += 1
                continue
            counts[state] += 1
            value = job.get("generation", 0)
            if isinstance(value, int) and not isinstance(value, bool):
                generation = max(generation, value)
            if state == "quarantined":
                terminal["unresolved"] += 1
            elif state == "dead-letter":
                outcome = (
                    "recovered"
                    if _has_verified_successor(root, job, deadline=deadline)
                    else "unresolved"
                )
                terminal[outcome] += 1
    _check_deadline(deadline)
    cleanup_unverified = has_unverified_process_tree(state_dir)
    status = (
        "error"
        if invalid or counts["quarantined"] or cleanup_unverified
        else "warning"
        if orphan_hook_inputs
        or any(counts[state] for state in ("pending", "claimed", "running", "dead-letter"))
        else "ok"
    )
    digest = hashlib.sha256(
        json.dumps(
            {"counts": counts, "generation": generation, "invalid": invalid,
             "cleanup_unverified": cleanup_unverified,
             "terminal": terminal,
             "orphan_hook_inputs": orphan_hook_inputs},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "status": status,
        "generation": generation,
        "digest": digest,
        "counts": counts,
        "invalid": invalid,
        "cleanup_unverified": cleanup_unverified,
        "terminal": terminal,
        "orphan_hook_inputs": orphan_hook_inputs,
    }


def _supervisor_receipt_path(state_dir: Path) -> Path:
    return state_dir / "worker-supervisor.json"


def _default_vault_root(state_dir: Path) -> Path:
    try:
        return vault_root_of(state_dir)
    except IndexError as exc:
        raise ValueError("worker-state-root-invalid") from exc


def enqueue_job(
    state_dir: Path,
    kind: str,
    payload: dict[str, Any],
    *,
    start_supervisor: bool = True,
    vault_root: Path | None = None,
    launcher: Callable[..., Any] = subprocess.Popen,
    now: float | None = None,
    deadline: float | None = None,
) -> Path:
    if not isinstance(kind, str) or kind not in JOB_KINDS:
        raise ValueError("worker-job-kind-invalid")
    if not isinstance(payload, dict):
        raise ValueError("worker-job-payload-invalid")
    observed_now = time.time() if now is None else now
    _ensure_job_dirs(state_dir)
    try:
        with locked(state_dir / "worker-queue", timeout=_lock_timeout(deadline)):
            path = _enqueue_job_locked(
                state_dir,
                kind,
                payload,
                observed_now=observed_now,
                deadline=deadline,
            )
    except LockUnavailable as exc:
        if deadline is None:
            raise
        raise WorkerDeliveryTimeout("worker-queue-deadline") from exc
    if start_supervisor:
        try:
            ensure_supervisor(
                state_dir,
                vault_root=vault_root or _default_vault_root(state_dir),
                launcher=launcher,
                now=observed_now,
                deadline=deadline,
            )
        except LockUnavailable as exc:
            if deadline is None:
                raise
            raise WorkerDeliveryTimeout("worker-admission-deadline") from exc
    return path


def ensure_supervisor(
    state_dir: Path,
    *,
    vault_root: Path,
    launcher: Callable[..., Any] = subprocess.Popen,
    now: float | None = None,
    deadline: float | None = None,
) -> bool:
    observed_now = time.time() if now is None else now
    state_dir.mkdir(parents=True, exist_ok=True)
    admission_lock = state_dir / "worker-admission"
    token = ""
    try:
        with locked(admission_lock, timeout=_lock_timeout(deadline)):
            if has_unverified_process_tree(state_dir):
                raise ValueError('worker-tree-cleanup-unverified')
            receipt_path = _supervisor_receipt_path(state_dir)
            try:
                current = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
            if isinstance(current, dict):
                lease_until = current.get("lease_until", 0)
                if ((current.get("status") == "launching"
                     and isinstance(lease_until, (int, float)) and lease_until > observed_now)
                        or (current.get("status") == "running"
                            and _process_owner_is_active(current))):
                    return False
            previous_generation = (
                current.get("generation", 0) if isinstance(current, dict) else 0
            )
            token = uuid.uuid4().hex
            atomic_write_json(
                receipt_path,
                {
                    "schema_version": SUPERVISOR_SCHEMA_VERSION,
                    "status": "launching",
                    "generation": (
                        previous_generation + 1
                        if isinstance(previous_generation, int)
                        else 1
                    ),
                    "launch_token": token,
                    "owner_pid": 0,
                    "lease_until": observed_now + LAUNCH_LEASE_SECONDS,
                    "updated_ts": int(observed_now),
                },
                deadline=deadline,
            )
    except LockUnavailable as exc:
        if deadline is None:
            raise
        raise WorkerDeliveryTimeout("worker-admission-deadline") from exc
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--vault",
        str(vault_root),
        "--state-dir",
        str(state_dir),
        "--token",
        token,
    ]
    try:
        spawn_detached(
            command,
            popen_factory=launcher,
            cwd=vault_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        with locked(admission_lock, timeout=_lock_timeout(deadline)):
            current = json.loads(
                _supervisor_receipt_path(state_dir).read_text(encoding="utf-8")
            )
            if isinstance(current, dict) and current.get("launch_token") == token:
                current.update(
                    {
                        "status": "failed",
                        "lease_until": 0,
                        "updated_ts": int(observed_now),
                    }
                )
                atomic_write_json(
                    _supervisor_receipt_path(state_dir),
                    current,
                    deadline=deadline,
                )
        raise
    return True


def recover_stale_jobs(
    state_dir: Path, *, job_id: str | None = None, now: float | None = None
) -> int:
    observed_now = time.time() if now is None else now
    _ensure_job_dirs(state_dir)
    recovered = 0
    with locked(state_dir / "worker-queue"):
        if has_unverified_process_tree(state_dir):
            return 0
        seen_records, seen_redriven = _scan_redrive_records_locked(
            state_dir, job_id=job_id
        )
        # A crash can occur between the atomic JSON update and directory move.
        # Complete only a valid recorded transition; never overwrite another job.
        for source_state, targets in RECOVERABLE_TRANSITIONS.items():
            for path in sorted((_job_root(state_dir) / source_state).glob('*.json')):
                path_job_id = _job_id_from_path(path)
                if (
                    job_id is not None
                    and path_job_id != job_id
                ):
                    continue
                try:
                    value = json.loads(path.read_text(encoding='utf-8'))
                    target = value.get('status') if isinstance(value, dict) else None
                    if (
                        not isinstance(target, str)
                        or target not in targets
                        or (
                            source_state == "dead-letter"
                            and (
                                not isinstance(value, dict)
                                or not _has_redrive_marker(value)
                            )
                        )
                    ):
                        continue
                    destination = _job_root(state_dir) / target / path.name
                    _validate_job(destination, value)
                    if source_state == "dead-letter":
                        record_id = value["job_id"]
                        if record_id in seen_records:
                            active = seen_redriven.get(
                                record_id, seen_records[record_id]
                            )
                            _mark_redrive_conflict(
                                value, active, now=observed_now
                            )
                            atomic_write_json(path, value)
                            continue
                    if destination.exists():
                        raise ValueError('worker-transition-target-exists')
                    os.replace(path, destination)
                    if target == 'pending':
                        recovered += 1
                except (OSError, UnicodeError, ValueError):
                    # The normal loader preserves malformed records in quarantine.
                    continue
        for source_state in ("claimed", "running"):
            for path in sorted((_job_root(state_dir) / source_state).glob("*.json")):
                path_job_id = _job_id_from_path(path)
                if (
                    job_id is not None
                    and path_job_id != job_id
                ):
                    continue
                job = _load_job_quarantined(state_dir, path)
                if job is None:
                    continue
                if _process_owner_is_active(job):
                    continue
                if int(job.get("attempt", 0)) >= MAX_ATTEMPTS:
                    destination = _job_root(state_dir) / "dead-letter" / path.name
                else:
                    destination = _job_root(state_dir) / "pending" / path.name
                if destination.exists():
                    # Preserve both records when a same-ID collision is already
                    # durable; never overwrite the existing terminal evidence.
                    continue
                job["generation"] = int(job.get("generation", 0)) + 1
                job.pop("claim_token", None)
                job.pop("owner_pid", None)
                job.pop("owner_identity", None)
                job.pop("lease_until", None)
                job["recovered_ts"] = int(observed_now)
                if int(job.get("attempt", 0)) >= MAX_ATTEMPTS:
                    job["status"] = "dead-letter"
                    job['finished_ts'] = int(observed_now)
                    job['last_error'] = 'worker-lease-expired'
                    job['terminal_reason'] = 'retry-exhausted'
                    job['retryable'] = False
                else:
                    job["status"] = "pending"
                    job["next_attempt_ts"] = observed_now + _retry_delay(
                        int(job.get("attempt", 0)),
                        RETRY_BASE_SECONDS,
                    )
                atomic_write_json(path, job)
                os.replace(path, destination)
                _report_terminal_maintenance(state_dir, job)
                recovered += 1
    return recovered


def redrive_dead_letter(
    state_dir: Path,
    *,
    job_id: str | None = None,
    now: float | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Dead-letter kaydını operatör kararıyla aynı kimlikle pending'e döndürür.

    Kayıt yerinde güncellenip `os.replace` ile taşınır (recover_stale_jobs ile
    aynı kalıp); yarım kalan taşıma, kaydedilmiş `pending` durumundan mevcut
    recovery yolu tarafından tamamlanır. Önceki redrive'dan kalan `pending`,
    `claimed`, `running` veya `succeeded` kaydı da aynı kimlikle tekrar
    çalıştırmada tanınır; yalnızca hâlâ pending olan kayıt için supervisor
    yeniden uyandırılır.
    Dead-letter zaten çözülmemiş iş sayıldığından taşıma admission bütçesini
    değiştirmez.
    """
    observed_now = time.time() if now is None else now
    _ensure_job_dirs(state_dir)
    migrate_legacy_failed_jobs(state_dir, job_id=job_id, now=observed_now)
    redriven: list[str] = []
    skipped: list[tuple[str, str]] = []
    with locked(state_dir / "worker-queue"):
        seen_records, all_redriven = _scan_redrive_records_locked(
            state_dir, job_id=job_id
        )
        seen_redriven = {
            identifier: job
            for identifier, job in all_redriven.items()
            if job_id is None or identifier == job_id
        }
        redriven.extend(seen_redriven)
        for path in sorted((_job_root(state_dir) / "dead-letter").glob("*.json")):
            if job_id is not None and _job_id_from_path(path) != job_id:
                continue
            try:
                job = _load_job(path)
            except ValueError as exc:
                skipped.append((path.name, str(exc)))
                continue
            if job_id is not None and job["job_id"] != job_id:
                continue
            if (
                job.get("terminal_reason") in REDRIVE_CONFLICT_REASONS
                and job.get("retryable") is False
            ):
                skipped.append((job["job_id"], "redrive çakışması"))
                continue
            if job["job_id"] in seen_redriven:
                active = seen_redriven[job["job_id"]]
            else:
                active = seen_records.get(job["job_id"])
            if job["job_id"] in seen_redriven or job["job_id"] in seen_records:
                # Persist the conflict so a later receipt prune cannot reopen it.
                # The ID may be reused for a different logical job; retain both
                # records as an explicit unresolved identity conflict.
                _mark_redrive_conflict(job, active, now=observed_now)
                atomic_write_json(path, job)
                skipped.append((job["job_id"], "redrive çakışması"))
                continue
            if _has_verified_successor(_job_root(state_dir), job):
                skipped.append((job["job_id"], "zaten kurtarılmış"))
                continue
            if _has_redrive_marker(job):
                skipped.append((job["job_id"], "önceki redrive dead-letter'da kaldı"))
                continue
            if job["kind"] == "flush":
                hook_input = _hook_input_reference(job["payload"])
                if (
                    hook_input is not None
                    and _managed_hook_input(hook_input, state_dir)
                    and not hook_input.is_file()
                ):
                    skipped.append((job["job_id"], "girdi dosyası silinmiş"))
                    continue
            destination = _job_root(state_dir) / "pending" / path.name
            if destination.exists():
                skipped.append((job["job_id"], "hedef kayıt mevcut"))
                continue
            job["status"] = "pending"
            job["attempt"] = 0
            job["generation"] = int(job["generation"]) + 1
            job["enqueued_ts"] = int(observed_now)
            job["redriven_ts"] = int(observed_now)
            for stale_key in (
                "finished_ts",
                "last_error",
                "terminal_reason",
                "retryable",
                "claim_token",
                "owner_pid",
                "owner_identity",
                "lease_until",
                "recovery_job_id",
                "next_attempt_ts",
                "retry_scheduled_ts",
            ):
                job.pop(stale_key, None)
            atomic_write_json(path, job)
            os.replace(path, destination)
            redriven.append(job["job_id"])
    return redriven, skipped


def _claim_next_job(
    state_dir: Path,
    *,
    now: float,
    lease_seconds: int | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    with locked(state_dir / "worker-queue"):
        if has_unverified_process_tree(state_dir):
            return None
        pending: tuple[tuple[int, int, str], Path, dict[str, Any]] | None = None
        for path in (_job_root(state_dir) / "pending").glob("*.json"):
            job = _load_job_quarantined(state_dir, path)
            if job is None:
                continue
            next_attempt = job.get("next_attempt_ts", 0)
            if not isinstance(next_attempt, (int, float)) or next_attempt > now:
                continue
            candidate = (_job_sort_key(job, path), path, job)
            if pending is None or candidate[0] < pending[0]:
                pending = candidate
        if pending is None:
            return None
        _sort_key, pending_path, job = pending
        effective_lease = (
            _job_lease_seconds(str(job["kind"]))
            if lease_seconds is None
            else lease_seconds
        )
        token = uuid.uuid4().hex
        job.update(
            {
                "status": "claimed",
                "generation": int(job.get("generation", 0)) + 1,
                "attempt": int(job.get("attempt", 0)) + 1,
                "claim_token": token,
                "owner_pid": os.getpid(),
                "supervisor_pid": os.getpid(),
                "lease_until": now + effective_lease,
                "claimed_ts": int(now),
            }
        )
        owner_identity = _process_owner_identity(os.getpid())
        if owner_identity is not None:
            job["owner_identity"] = owner_identity
        job.pop("next_attempt_ts", None)
        atomic_write_json(pending_path, job)
        claimed = _job_root(state_dir) / "claimed" / pending_path.name
        os.replace(pending_path, claimed)
        job["status"] = "running"
        job["generation"] += 1
        job["running_ts"] = int(now)
        atomic_write_json(claimed, job)
        running = _job_root(state_dir) / "running" / pending_path.name
        os.replace(claimed, running)
        return running, job


def _retry_delay(attempt: int, base_seconds: int) -> int:
    if base_seconds < 1:
        raise ValueError("worker-retry-base-invalid")
    bounded_attempt = max(1, attempt)
    return base_seconds * (1 << (bounded_attempt - 1))


def _finish_job(
    state_dir: Path,
    running: Path,
    expected: dict[str, Any],
    *,
    status: str,
    error: str = "",
    terminal_reason: str = "",
    now: float | None = None,
    retry_base_seconds: int = RETRY_BASE_SECONDS,
) -> None:
    observed_now = time.time() if now is None else now
    with locked(state_dir / "worker-queue"):
        current = _load_job(running)
        if (
            current.get("status") != "running"
            or
            current.get("claim_token") != expected.get("claim_token")
            or current.get("job_id") != expected.get("job_id")
            or current.get("generation") != expected.get("generation")
        ):
            raise ValueError("worker-job-claim-drift")
        current["generation"] = int(current.get("generation", 0)) + 1
        attempt = int(current.get("attempt", 0))
        terminal = status
        if status == "failed":
            current["last_error"] = error or "worker-job-failed"
            failures = current.get("failures", [])
            if not isinstance(failures, list):
                failures = []
            failures.append(
                {
                    "attempt": attempt,
                    "ts": int(observed_now),
                    "error": current["last_error"],
                }
            )
            current["failures"] = failures[-MAX_ATTEMPTS:]
            if attempt >= MAX_ATTEMPTS:
                terminal = "dead-letter"
                current["terminal_reason"] = "retry-exhausted"
                current["retryable"] = False
                current["finished_ts"] = int(observed_now)
            else:
                terminal = "pending"
                current["retry_scheduled_ts"] = int(observed_now)
                current["next_attempt_ts"] = observed_now + _retry_delay(
                    attempt,
                    retry_base_seconds,
                )
                current.pop("claim_token", None)
                current.pop("owner_pid", None)
                current.pop("owner_identity", None)
        elif status == "dead-letter":
            current["last_error"] = error or "worker-job-unrecoverable-input"
            current["terminal_reason"] = terminal_reason or "unrecoverable-input"
            current["retryable"] = False
            current["finished_ts"] = int(observed_now)
            current.pop("retry_scheduled_ts", None)
            current.pop("next_attempt_ts", None)
        else:
            current["finished_ts"] = int(observed_now)
            current.pop("next_attempt_ts", None)
            if terminal == "succeeded":
                hook_input = _hook_input_reference(current.get("payload"))
                if hook_input is not None and _managed_hook_input(hook_input, state_dir):
                    current["hook_input_cleanup_pending"] = True
        if terminal != "pending":
            current.pop("claim_token", None)
        current["status"] = terminal
        current["lease_until"] = 0
        atomic_write_json(running, current)
        destination = _job_root(state_dir) / terminal / running.name
        os.replace(running, destination)
        if terminal == "succeeded":
            # A durable successor receipt is technical recovery, never user-task PASS.
            for failed_path in (_job_root(state_dir) / 'dead-letter').glob('*.json'):
                try:
                    failed = _load_job(failed_path)
                except ValueError:
                    continue
                if (failed.get('kind') == current.get('kind')
                        and failed.get('payload') == current.get('payload')
                        and failed.get('terminal_reason') not in REDRIVE_CONFLICT_REASONS):
                    failed.update(terminal_reason='recovered-by-successor', retryable=False,
                                  recovery_job_id=current['job_id'])
                    atomic_write_json(failed_path, failed)
            _cleanup_succeeded_hook_inputs_locked(state_dir)
        _prune_succeeded_jobs_locked(state_dir, observed_now)
        _report_terminal_maintenance(state_dir, current)


def _dispatch_job(vault_root: Path, state_dir: Path, job: dict[str, Any]) -> None:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("worker-job-payload-invalid")
    kind = job.get("kind")
    if kind == "maintenance":
        import compile as compiler
        import compile_state
        from state_store import state_dir_of

        memory_state = state_dir_of(vault_root)
        with locked(memory_state / "compile", timeout=0):
            if compiler._run_locked(vault_root, memory_state, False, 2, None):
                raise RuntimeError("worker-maintenance-failed")
        if compile_state.has_changes(vault_root):
            enqueue_maintenance(memory_state, vault_root=vault_root, start_supervisor=False)
        return
    if kind == "flush":
        import flush

        hook_input = payload.get("hook_input")
        reason = payload.get("reason")
        event_iso = payload.get("event_iso")
        if (
            not isinstance(hook_input, str)
            or not hook_input
            or not isinstance(reason, str)
            or not reason
            or not isinstance(event_iso, str)
            or not event_iso
        ):
            raise ValueError("worker-flush-payload-invalid")
        hook_path = Path(hook_input)
        try:
            hook_payload = load_hook_input(hook_path)
        except FileNotFoundError as exc:
            raise _UnrecoverableWorkerInput("hook-input-missing") from exc
        transcript_value = hook_payload.get("transcript_path")
        transcript_path = (
            Path(transcript_value).expanduser()
            if isinstance(transcript_value, str) and transcript_value
            else None
        )
        if transcript_path is not None and not transcript_path.is_file():
            raise _UnrecoverableWorkerInput("transcript-missing")
        args = argparse.Namespace(hook_input=hook_path, reason=reason)
        try:
            result = flush.flush_once(
                args,
                dt.datetime.fromisoformat(event_iso),
                vault_root,
                state_dir,
                hook_input=hook_payload,
            )
        except FileNotFoundError as exc:
            if transcript_path is not None and not transcript_path.is_file():
                raise _UnrecoverableWorkerInput("transcript-missing") from exc
            raise
        if result != 0:
            raise RuntimeError(f"worker-flush-failed:{result}")
        return
    raise ValueError("worker-job-kind-invalid")


def _adopt_job(
    state_dir: Path,
    running: Path,
    *,
    now: float,
    lease_seconds: int | None = None,
    expected_claim_token: str | None = None,
) -> dict[str, Any]:
    with locked(state_dir / "worker-queue"):
        if has_unverified_process_tree(state_dir):
            raise ValueError('worker-tree-cleanup-unverified')
        job = _load_job(running)
        if job.get("status") != "running" or not job.get("claim_token"):
            raise ValueError("worker-job-not-claimable")
        if (
            expected_claim_token is not None
            and job.get("claim_token") != expected_claim_token
        ):
            raise ValueError("worker-job-claim-drift")
        previous_owner_pid = job.get("owner_pid")
        job["owner_pid"] = os.getpid()
        if previous_owner_pid != job["owner_pid"]:
            job.pop("owner_identity", None)
        owner_identity = _process_owner_identity(os.getpid())
        if owner_identity is not None:
            job["owner_identity"] = owner_identity
        effective_lease = (
            _job_lease_seconds(str(job["kind"]))
            if lease_seconds is None
            else lease_seconds
        )
        job["lease_until"] = now + effective_lease
        job["adopted_ts"] = int(now)
        job["generation"] = int(job.get("generation", 0)) + 1
        atomic_write_json(running, job)
        return job


def execute_job_file(
    vault_root: Path,
    state_dir: Path,
    running: Path,
    *,
    now: Callable[[], float] = time.time,
    expected_claim_token: str | None = None,
) -> int:
    job = _adopt_job(
        state_dir,
        running,
        now=now(),
        expected_claim_token=expected_claim_token,
    )
    try:
        _dispatch_job(vault_root, state_dir, job)
    except _UnrecoverableWorkerInput as exc:
        _finish_job(
            state_dir,
            running,
            job,
            status="dead-letter",
            error=str(exc),
            terminal_reason="unrecoverable-input",
            now=now(),
        )
        return 1
    except ProcessTreeCleanupError as exc:
        _fence_unverified_process_tree(state_dir, job, exc, now=now())
        raise
    except Exception as exc:
        _finish_job(
            state_dir,
            running,
            job,
            status="failed",
            error=exc.__class__.__name__,
            now=now(),
            retry_base_seconds=60 if job["kind"] == "maintenance" else RETRY_BASE_SECONDS,
        )
        return 1
    _finish_job(
        state_dir,
        running,
        job,
        status="succeeded",
        now=now(),
    )
    return 0


def _run_claimed_job(
    vault_root: Path,
    state_dir: Path,
    running: Path,
    *,
    timeout: float | None = None,
) -> None:
    job = _load_job(running)
    claim_token = job.get("claim_token")
    if not isinstance(claim_token, str) or not claim_token:
        raise ValueError("worker-claim-token-missing")
    effective_timeout = _job_timeout(str(job["kind"])) if timeout is None else timeout
    result = run_with_tree_timeout(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--vault",
            str(vault_root),
            "--state-dir",
            str(state_dir),
            "--execute-job",
            str(running),
            "--claim-token",
            claim_token,
        ],
        timeout=effective_timeout,
        cwd=vault_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if has_unverified_process_tree(state_dir):
        raise _fenced_cleanup_error(state_dir, job, effective_timeout)
    if result.returncode not in {0, 1}:
        raise RuntimeError("worker-job-child-failed")
    if running.exists():
        # A child may exit before publishing its result. Complete recorded moves,
        # or requeue its abandoned lease; process exit alone is never success.
        recover_stale_jobs(state_dir)
        if running.exists():
            raise RuntimeError("worker-child-result-missing")


def _has_pending_locked(
    state_dir: Path, *, job_id: str | None = None
) -> bool:
    """Return whether any pending job exists while the queue lock is held."""
    for path in (_job_root(state_dir) / "pending").glob("*.json"):
        if job_id is not None and _job_id_from_path(path) != job_id:
            continue
        job = _load_job_quarantined(state_dir, path)
        if job is not None and (job_id is None or job["job_id"] == job_id):
            return True
    return False


def _has_pending(state_dir: Path, *, job_id: str | None = None) -> bool:
    with locked(state_dir / "worker-queue"):
        return _has_pending_locked(state_dir, job_id=job_id)


def _settle_supervisor(
    state_dir: Path,
    receipt_path: Path,
    *,
    owner_pid: int,
    now: float,
    release_supervisor: Callable[[], Any] | None = None,
) -> bool:
    with locked(state_dir / "worker-admission"):
        with locked(state_dir / "worker-queue"):
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                receipt = {}
            if not isinstance(receipt, dict) or receipt.get("owner_pid") != owner_pid:
                if release_supervisor is not None:
                    release_supervisor()
                return True
            _recover_orphan_hook_inputs_locked(state_dir, now=now)
            has_pending = _has_pending_locked(state_dir)
            has_orphan = _has_recoverable_hook_inputs_locked(state_dir)
            if has_orphan is None:
                receipt.update(
                    {
                        "status": "failed",
                        "lease_until": 0,
                        "updated_ts": int(now),
                        "last_error": "worker-hook-input-reference-unverified",
                    }
                )
                atomic_write_json(receipt_path, receipt)
                if release_supervisor is not None:
                    release_supervisor()
                return True
            queue_full = _unresolved_job_count_locked(state_dir) >= MAX_UNRESOLVED_JOBS
            if has_pending or (has_orphan and not queue_full):
                receipt.update(
                    {
                        "status": "running",
                        "lease_until": now + SUPERVISOR_LEASE_SECONDS,
                        "updated_ts": int(now),
                    }
                )
                atomic_write_json(receipt_path, receipt)
                return False
            receipt.update(
                {
                    "status": "idle",
                    "lease_until": 0,
                    "updated_ts": int(now),
                }
            )
            atomic_write_json(receipt_path, receipt)
            # The enqueue path waits on the same admission and queue locks.  Release
            # the lifetime lock before either lock is dropped so a newly launched
            # supervisor cannot consume the one wake-up while this process exits.
            if release_supervisor is not None:
                release_supervisor()
            return True


def run_supervisor(
    vault_root: Path,
    state_dir: Path,
    *,
    launch_token: str = "",
    now: Callable[[], float] = time.time,
    job_runner: Callable[..., Any] = _run_claimed_job,
) -> int:
    _ensure_job_dirs(state_dir)
    with ExitStack() as stack:
        try:
            with locked(state_dir / "worker-admission"):
                if has_unverified_process_tree(state_dir):
                    return 1
                receipt_path = _supervisor_receipt_path(state_dir)
                try:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    receipt = {}
                if launch_token and (
                    not isinstance(receipt, dict)
                    or receipt.get("launch_token") != launch_token
                ):
                    return 0
                lifetime_acquired = False
                try:
                    # Admission wins the startup race.  A delayed starter cannot
                    # replace this launch token while the lifetime lock is acquired
                    # and the running receipt is published.
                    stack.enter_context(locked(state_dir / "worker-supervisor", timeout=0))
                    lifetime_acquired = True
                    generation = receipt.get("generation", 0) if isinstance(receipt, dict) else 0
                    supervisor_receipt = {
                        "schema_version": SUPERVISOR_SCHEMA_VERSION,
                        "status": "running",
                        "generation": generation + 1 if isinstance(generation, int) else 1,
                        "launch_token": launch_token,
                        "owner_pid": os.getpid(),
                        "lease_until": now() + SUPERVISOR_LEASE_SECONDS,
                        "updated_ts": int(now()),
                    }
                    owner_identity = _process_owner_identity(os.getpid())
                    if owner_identity is not None:
                        supervisor_receipt["owner_identity"] = owner_identity
                    atomic_write_json(receipt_path, supervisor_receipt)
                except BaseException:
                    if lifetime_acquired:
                        # Keep admission held while releasing a partially started
                        # lifetime owner; waiters must observe no half-started handoff.
                        stack.pop_all().close()
                    raise
        except LockUnavailable:
            return 0

        supervisor_released = False

        def release_supervisor() -> None:
            nonlocal supervisor_released
            if not supervisor_released:
                stack.pop_all().close()
                supervisor_released = True

        migrate_legacy_failed_jobs(state_dir, now=now())
        recover_stale_jobs(state_dir, now=now())
        with locked(state_dir / "worker-queue"):
            _cleanup_succeeded_hook_inputs_locked(state_dir)
        recover_orphan_hook_inputs(state_dir, now=now())
        prune_succeeded_jobs(state_dir, now=now())
        _sweep_stale_hook_inputs(state_dir, now())
        while True:
            claimed = _claim_next_job(state_dir, now=now())
            if claimed is None:
                if _settle_supervisor(
                    state_dir,
                    receipt_path,
                    owner_pid=os.getpid(),
                    now=now(),
                    release_supervisor=release_supervisor,
                ):
                    break
                time.sleep(1)  # Keep delayed retries alive without holding admission/queue locks.
                continue
            running, job = claimed
            try:
                job_runner(vault_root, state_dir, running)
            except ProcessTreeCleanupError as exc:
                # The child may already have moved/deleted its result receipt.
                # Fence the lane independently of that receipt before releasing ownership.
                _fence_unverified_process_tree(state_dir, job, exc, now=now())
                return 1
            except ProcessTreeTimeout:
                if running.exists():
                    latest = _load_job_quarantined(state_dir, running)
                    if latest is not None:
                        _finish_job(
                            state_dir,
                            running,
                            latest,
                            status="failed",
                            error="ProcessTreeTimeout",
                            now=now(),
                        )
            except Exception as exc:
                if running.exists():
                    latest = _load_job_quarantined(state_dir, running)
                    if latest is not None:
                        _finish_job(
                            state_dir,
                            running,
                            latest,
                            status="failed",
                            error=exc.__class__.__name__,
                            now=now(),
                        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--token", default="")
    parser.add_argument("--claim-token")
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--execute-job", type=Path)
    parser.add_argument(
        "--redrive",
        nargs="?",
        const="all",
        metavar="JOB_ID",
        help="move dead-letter jobs to pending (single job: 32-hex ID)",
    )
    args = parser.parse_args()
    vault_root = args.vault.resolve(strict=True)
    state_dir = args.state_dir.resolve()
    if args.redrive is not None:
        target = None if args.redrive == "all" else args.redrive
        if target is not None and re.fullmatch(r"[0-9a-f]{32}", target) is None:
            raise ValueError("worker-redrive-job-id-invalid")
        recovered = recover_stale_jobs(state_dir, job_id=target)
        redriven, skipped = redrive_dead_letter(state_dir, job_id=target)
        supervisor_state = "started"
        should_wake = (
            redriven
            and _has_pending_redrive(state_dir, job_id=target)
        ) or (
            recovered > 0
            and _has_pending(state_dir, job_id=target)
        )
        if should_wake:
            if not ensure_supervisor(state_dir, vault_root=vault_root):
                supervisor_state = _redrive_supervisor_state(state_dir)
        for identifier in redriven:
            current_state = _redrive_state(state_dir, identifier)
            if current_state == "pending":
                if supervisor_state == "deferred":
                    message = f"redrive beklemede; supervisor başlatma belirsiz: {identifier}"
                elif supervisor_state == "uncertain":
                    message = f"redrive durumu belirsiz; supervisor doğrulanamadı: {identifier}"
                else:
                    message = f"pending'e döndü: {identifier}"
                print(_console_safe(message))
            elif current_state in {"claimed", "running"}:
                print(_console_safe(f"redrive zaten sürüyor: {identifier}"))
            elif current_state == "succeeded":
                print(_console_safe(f"redrive zaten tamamlandı: {identifier}"))
            else:
                print(_console_safe(f"redrive kabul edildi: {identifier}"))
        for identifier, reason in skipped:
            print(_console_safe(f"atlandı: {identifier} — {reason}"))
        if not redriven and not skipped:
            if recovered:
                print(_console_safe(f"stale işler toparlandı: {recovered}"))
            else:
                print(_console_safe("dead-letter boş ya da eşleşen iş yok"))
        return 1 if (
            skipped
            or supervisor_state in {"deferred", "uncertain"}
            or (target is not None and not redriven)
        ) else 0
    if args.execute_job is not None:
        if not isinstance(args.claim_token, str) or re.fullmatch(
            r"[0-9a-f]{32}", args.claim_token
        ) is None:
            raise ValueError("worker-claim-token-required")
        running = args.execute_job.resolve(strict=True)
        try:
            running.relative_to((_job_root(state_dir) / "running").resolve(strict=True))
        except ValueError as exc:
            raise ValueError("worker-execute-path-invalid") from exc
        return execute_job_file(
            vault_root,
            state_dir,
            running,
            expected_claim_token=args.claim_token,
        )
    return run_supervisor(
        vault_root,
        state_dir,
        launch_token=args.token,
    )


if __name__ == "__main__":
    raise SystemExit(main())
