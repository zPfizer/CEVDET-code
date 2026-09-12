from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
from contextlib import contextmanager
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
_EXPECTED_DIGEST_UNSET = object()


class ReplacementConflict(OSError):
    """The target changed during a guarded Windows replacement."""

    def __init__(self, reason: str, backup: Path | None = None) -> None:
        super().__init__(reason)
        self.backup = backup


class _RetryableGuardOpen(OSError):
    """The guarded target handle could not be acquired yet."""


def _windows_api() -> Any:
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    api.ReplaceFileW.restype = wintypes.BOOL
    return api


def _windows_handle_value(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    if value is None:
        raise OSError("windows-invalid-handle")
    return int(value)


def _windows_open(path: Path) -> Any:
    api = _windows_api()
    handle = api.CreateFileW(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000004,  # FILE_SHARE_READ | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    value = getattr(handle, "value", handle)
    if value in (None, -1, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


@contextmanager
def _locked_windows_file(path: Path) -> Any:
    import msvcrt

    handle = _windows_open(path)
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(
            _windows_handle_value(handle),
            os.O_RDONLY | os.O_BINARY,
        )
        handle = None
        with os.fdopen(descriptor, "rb") as source:
            yield source
    finally:
        if handle is not None:
            _windows_api().CloseHandle(handle)


def _locked_windows_digest(path: Path) -> str:
    with _locked_windows_file(path) as source:
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def _windows_replace(
    replaced: Path,
    replacement: Path,
    backup: Path | None,
) -> None:
    api = _windows_api()
    if not api.ReplaceFileW(
        str(replaced),
        str(replacement),
        str(backup) if backup is not None else None,
        0,
        None,
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_windows_guarded(
    source: Path,
    destination: Path,
    expected_digest: str | None,
    *,
    before_replace: Callable[[], object] | None,
    backup: Path,
) -> None:
    replacement_digest = sha256_file(source)
    try:
        target_handle = _windows_open(destination)
    except OSError as exc:
        if _is_windows_share_error(exc):
            raise _RetryableGuardOpen("target-sharing") from exc
        raise
    try:
        if before_replace is not None:
            before_replace()
        _windows_replace(destination, source, backup)
        displaced_digest = _locked_windows_digest(backup)
        if displaced_digest != expected_digest:
            raise ReplacementConflict("replace-displaced-target", backup)

        with _locked_windows_file(destination) as output:
            digest = hashlib.sha256()
            for chunk in iter(lambda: output.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != replacement_digest:
                raise ReplacementConflict("replace-output-changed", backup)
            if sha256_file(destination) != replacement_digest:
                raise ReplacementConflict("replace-output-replaced", backup)
    finally:
        _windows_api().CloseHandle(target_handle)


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


def sha256_file_locked(path: Path) -> str:
    if os.name == "nt":
        return _locked_windows_digest(path)
    return sha256_file(path)


def _is_windows_share_error(exc: OSError) -> bool:
    """Only Win32 sharing/lock violations are retryable replace races."""
    return os.name == "nt" and getattr(exc, "winerror", None) in _WINDOWS_SHARE_ERRORS


def _create_replacement_marker(source: Path, marker: Path | None) -> bool:
    """Keep a hard-link identity receipt for a no-clobber publication."""
    if marker is None:
        return False
    try:
        os.link(source, marker)
    except FileExistsError as exc:
        raise ReplacementConflict("replace-marker-exists", marker) from exc
    return True


def replace_with_retry(
    source: Path,
    destination: Path,
    *,
    timeout: float = REPLACE_RETRY_SECONDS,
    deadline: float | None = None,
    before_replace: Callable[[], object] | None = None,
    expected_digest: str | None | object = _EXPECTED_DIGEST_UNSET,
    backup: Path | None = None,
    on_marker_created: Callable[[], object] | None = None,
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
    if expected_digest is not _EXPECTED_DIGEST_UNSET and os.name == "nt":
        if expected_digest is None:
            marker_created = False
            while True:
                if before_replace is not None:
                    before_replace()
                if not marker_created:
                    marker_created = _create_replacement_marker(source, backup)
                    if marker_created and on_marker_created is not None:
                        on_marker_created()
                try:
                    os.rename(source, destination)
                    return
                except FileExistsError as exc:
                    raise ReplacementConflict("replace-target-created") from exc
                except OSError as exc:
                    if not _is_windows_share_error(exc):
                        raise
                    remaining = deadline - time.monotonic()
                    if not math.isfinite(remaining) or remaining <= 0:
                        raise
                    time.sleep(min(REPLACE_RETRY_SLEEP_SECONDS, remaining))
        if backup is None:
            raise ValueError("replace-backup-required")
        while True:
            if before_replace is not None:
                before_replace()
            try:
                _replace_windows_guarded(
                    source,
                    destination,
                    expected_digest,
                    before_replace=before_replace,
                    backup=backup,
                )
                return
            except OSError as exc:
                if isinstance(exc, ReplacementConflict) or not isinstance(exc, _RetryableGuardOpen):
                    raise
                remaining = deadline - time.monotonic()
                if not math.isfinite(remaining) or remaining <= 0:
                    raise
                time.sleep(min(REPLACE_RETRY_SLEEP_SECONDS, remaining))
    if expected_digest is not _EXPECTED_DIGEST_UNSET and expected_digest is None:
        marker_created = False
        while True:
            if before_replace is not None:
                before_replace()
            if not marker_created:
                marker_created = _create_replacement_marker(source, backup)
                if marker_created and on_marker_created is not None:
                    on_marker_created()
            try:
                os.link(source, destination)
                source.unlink(missing_ok=True)
                return
            except FileExistsError as exc:
                raise ReplacementConflict("replace-target-created") from exc
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
    overwrite: bool = True,
    before_replace: Callable[[], object] | None = None,
    expected_digest: str | None | object = _EXPECTED_DIGEST_UNSET,
    backup: Path | None = None,
) -> None:
    """Aynı dizinde temp + `os.replace`; temp adı daima `.{ad}.*.tmp`.

    `newline=None` platform çevirisini korur, `"\\n"` byte'ları aynen yazar.
    `keep_mode` hedefin mevcut iznini taşır (hedef yoksa FileNotFoundError).
    `overwrite=False` hedefi atomik biçimde yalnız yoksa oluşturur; mevcut
    hedefi değiştirmez.
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
        if overwrite:
            replace_with_retry(
                temporary,
                path,
                deadline=deadline,
                before_replace=before_replace,
                expected_digest=expected_digest,
                backup=backup,
            )
        else:
            # The guarded create-only path is atomic on both platforms and
            # retries transient Windows sharing violations.
            try:
                replace_with_retry(
                    temporary,
                    path,
                    deadline=deadline,
                    before_replace=before_replace,
                    expected_digest=None,
                    backup=backup,
                )
            except ReplacementConflict as exc:
                if str(exc) == "replace-target-created":
                    raise FileExistsError(path) from exc
                raise
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    deadline: float | None = None,
    before_replace: Callable[[], object] | None = None,
    expected_digest: str | None | object = _EXPECTED_DIGEST_UNSET,
    backup: Path | None = None,
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
        replace_with_retry(
            temporary,
            path,
            deadline=deadline,
            before_replace=before_replace,
            expected_digest=expected_digest,
            backup=backup,
        )
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


def report_health(
    state_dir: Path,
    *,
    component: str,
    error: str,
    warning: bool = False,
    scope_key: str = "global",
) -> None:
    """Best-effort ``write_health``: reporting must never crash the caller."""
    try:
        write_health(
            state_dir,
            component=component,
            error=error,
            warning=warning,
            scope_key=scope_key,
        )
    except OSError:
        pass


def discard_health(
    state_dir: Path,
    *,
    component: str,
    scope_key: str = "global",
    expected_error: str | None = None,
) -> None:
    """Best-effort ``clear_health``: cleanup must never crash the caller."""
    try:
        clear_health(
            state_dir,
            component=component,
            scope_key=scope_key,
            expected_error=expected_error,
        )
    except OSError:
        pass
