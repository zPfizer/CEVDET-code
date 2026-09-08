from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Callable, Mapping

from file_lock import locked


HEALTH_SCHEMA_VERSION = 2
STATE_DIR_PARTS = (".codex", "scripts", ".state")
REPLACE_RETRY_SECONDS = 1.0
REPLACE_RETRY_SLEEP_SECONDS = 0.02
_WINDOWS_SHARE_ERRORS = frozenset({5, 32, 33})


def session_scope(session_id: str | None) -> str:
    if not isinstance(session_id, str) or not session_id:
        return "global"
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def state_dir_of(vault_root: Path) -> Path:
    return Path(vault_root).joinpath(*STATE_DIR_PARTS)


def vault_root_of(state_dir: Path) -> Path:
    """`.codex/scripts/.state` yolundan vault kökü; sığ yolda IndexError."""
    return Path(state_dir).resolve().parents[len(STATE_DIR_PARTS) - 1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_windows_share_error(exc: OSError) -> bool:
    """Only Win32 sharing/lock violations are retryable replace races."""
    return os.name == "nt" and getattr(exc, "winerror", None) in _WINDOWS_SHARE_ERRORS


def replace_with_retry(
    source: Path,
    destination: Path,
    *,
    timeout: float = REPLACE_RETRY_SECONDS,
    deadline: float | None = None,
    before_replace: Callable[[], object] | None = None,
) -> None:
    """Replace without deleting the destination; bound Windows share retries."""
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("replace-timeout-invalid") from exc
    if not math.isfinite(timeout):
        raise ValueError("replace-timeout-invalid")
    if deadline is None:
        deadline = time.monotonic() + max(0.0, timeout)
    else:
        try:
            deadline = float(deadline)
        except (TypeError, ValueError) as exc:
            raise ValueError("replace-deadline-invalid") from exc
        if not math.isfinite(deadline):
            raise ValueError("replace-deadline-invalid")
    while True:
        if before_replace is not None:
            before_replace()
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if not _is_windows_share_error(exc):
                raise
            remaining = deadline - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                raise
            time.sleep(min(REPLACE_RETRY_SLEEP_SECONDS, remaining))


def atomic_write_text(
    path: Path,
    text: str,
    *,
    fsync: bool = True,
    newline: str | None = None,
    keep_mode: bool = False,
    deadline: float | None = None,
) -> None:
    """Aynı dizinde temp + `os.replace`; temp adı daima `.{ad}.*.tmp`.

    `newline=None` platform çevirisini korur, `"\\n"` byte'ları aynen yazar.
    `keep_mode` hedefin mevcut iznini taşır (hedef yoksa FileNotFoundError).
    """
    mode = stat.S_IMODE(path.stat().st_mode) if keep_mode else None
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        replace_with_retry(temporary, path, deadline=deadline)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    deadline: float | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path, deadline=deadline)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    indent: int | None = None,
    separators: tuple[str, str] | None = (",", ":"),
    sort_keys: bool = False,
    fsync: bool = True,
    newline: str | None = None,
    deadline: float | None = None,
) -> None:
    """`atomic_write_text` + sondaki `\\n`; biçim anahtarları json.dumps'a gider.

    Varsayılan repo biçimi kompakt; `separators=None` json.dumps varsayılanıdır.
    """
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=indent,
        separators=separators,
        sort_keys=sort_keys,
    )
    atomic_write_text(
        path,
        encoded + "\n",
        fsync=fsync,
        newline=newline,
        deadline=deadline,
    )


def _load_health(path: Path) -> dict[str, Any]:
    empty = {"schema_version": HEALTH_SCHEMA_VERSION, "generation": 0, "components": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return empty
    if not isinstance(loaded, dict):
        return empty
    if loaded.get("schema_version") == HEALTH_SCHEMA_VERSION and isinstance(
        loaded.get("components"),
        dict,
    ):
        for key, entry in loaded["components"].items():
            if (
                not isinstance(key, str)
                or not isinstance(entry, dict)
                or entry.get("status") not in {"error", "warning"}
            ):
                return empty
        return loaded
    components: dict[str, Any] = {}
    component = loaded.get("component")
    error = loaded.get("error")
    if isinstance(component, str) and component and isinstance(error, str) and error:
        components[f"{component}:global"] = {
            "component": component,
            "scope_key": "global",
            "generation": 1,
            "ts": int(loaded.get("ts", 0)),
            "status": "warning" if error.startswith("warn:") else "error",
            "error": error,
            "warnings": loaded.get("warnings", []),
        }
    elif loaded:
        return empty
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "generation": 1 if components else 0,
        "components": components,
    }


def _summarize_health(payload: dict[str, Any]) -> None:
    components = payload.get("components", {})
    entries = [entry for entry in components.values() if isinstance(entry, dict)]
    failing = [entry for entry in entries if entry.get("status") == "error"]
    warnings = [entry for entry in entries if entry.get("status") == "warning"]
    selected = failing[0] if failing else (warnings[0] if warnings else None)
    if selected is None:
        payload["status"] = "ok"
        payload.pop("component", None)
        payload.pop("error", None)
        payload.pop("warnings", None)
        return
    payload["status"] = selected["status"]
    payload["component"] = selected.get("component", "")
    payload["error"] = selected.get("error", "")
    if selected.get("warnings"):
        payload["warnings"] = selected["warnings"]


def write_health(
    state_dir: Path,
    *,
    component: str,
    error: str,
    warning: bool = False,
    scope_key: str = "global",
    now: float | None = None,
) -> dict[str, Any]:
    if not component or not error or not scope_key:
        raise ValueError("health-identity-invalid")
    path = state_dir / "health.json"
    with locked(path):
        payload = _load_health(path)
        components = payload.setdefault("components", {})
        key = f"{component}:{scope_key}"
        previous = components.get(key, {})
        generation = previous.get("generation", 0) if isinstance(previous, dict) else 0
        warnings = previous.get("warnings", []) if isinstance(previous, dict) else []
        if not isinstance(warnings, list):
            warnings = []
        if warning and error not in warnings:
            warnings.append(error)
        components[key] = {
            "component": component,
            "scope_key": scope_key,
            "generation": generation + 1 if isinstance(generation, int) else 1,
            "ts": int(time.time() if now is None else now),
            "status": "warning" if warning else "error",
            "error": error,
            "warnings": warnings[-20:],
        }
        payload["schema_version"] = HEALTH_SCHEMA_VERSION
        payload["generation"] = int(payload.get("generation", 0)) + 1
        _summarize_health(payload)
        atomic_write_json(path, payload)
        return payload


def clear_health(
    state_dir: Path,
    *,
    component: str,
    scope_key: str = "global",
    expected_error: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    path = state_dir / "health.json"
    if not path.is_file():
        return None
    with locked(path):
        payload = _load_health(path)
        components = payload.setdefault("components", {})
        key = f"{component}:{scope_key}"
        previous = components.get(key)
        if not isinstance(previous, dict):
            return payload
        if expected_error is not None and previous.get("error") != expected_error:
            return payload
        components.pop(key, None)
        payload["schema_version"] = HEALTH_SCHEMA_VERSION
        payload["generation"] = int(payload.get("generation", 0)) + 1
        _summarize_health(payload)
        atomic_write_json(path, payload)
        return payload
