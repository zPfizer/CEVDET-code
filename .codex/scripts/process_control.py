from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Sequence


# Win32 Job Objects: closing the last KILL_ON_JOB_CLOSE handle terminates the
# owned tree; BREAKAWAY_OK lets an explicit CREATE_BREAKAWAY_FROM_JOB child
# remain independent. See Microsoft Learn:
# https://learn.microsoft.com/windows/win32/procthread/job-objects.
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_BREAKAWAY_FROM_JOB = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_CLEANUP_TIMEOUT_SECONDS = 2.0
_WAIT_OBJECT_0 = 0x00000000
_ERROR_INVALID_PARAMETER = 87
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_JOB_ATTRIBUTE_KEY = "beyin_job_handle"


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = (("values", ctypes.c_ulonglong * 6),)


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    )


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = (
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("this_period_total_user_time", ctypes.c_longlong),
        ("this_period_total_kernel_time", ctypes.c_longlong),
        ("total_page_fault_count", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("total_terminated_processes", wintypes.DWORD),
    )


class _StartupInfoW(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    )


class _StartupInfoExW(ctypes.Structure):
    _fields_ = (
        ("startup_info", _StartupInfoW),
        ("attribute_list", ctypes.c_void_p),
    )


class _ProcessInformation(ctypes.Structure):
    _fields_ = (
        ("h_process", wintypes.HANDLE),
        ("h_thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    )


_WINDOWS_KERNEL32: Any | None = None
_PROCESS_LAUNCH_LOCK = threading.RLock()


def _windows_kernel32() -> Any:
    global _WINDOWS_KERNEL32
    if _WINDOWS_KERNEL32 is not None:
        return _WINDOWS_KERNEL32
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessInformation),
    ]
    api.CreateProcessW.restype = wintypes.BOOL
    api.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    api.DeleteProcThreadAttributeList.restype = None
    api.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    api.GetProcessTimes.restype = wintypes.BOOL
    api.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    api.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.QueryInformationJobObject.restype = wintypes.BOOL
    api.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateJobObject.restype = wintypes.BOOL
    api.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    api.UpdateProcThreadAttribute.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    _WINDOWS_KERNEL32 = api
    return api


def _windows_error(message: str) -> OSError:
    error = ctypes.WinError(ctypes.get_last_error())
    return OSError(error.errno, f"{message}: {error.strerror}")


def _handle_value(handle: Any) -> int:
    try:
        value = int(handle)
    except (TypeError, ValueError) as exc:
        raise OSError("windows-handle-invalid") from exc
    if value <= 0:
        raise OSError("windows-handle-invalid")
    return value


def _close_windows_handle(handle: Any) -> None:
    value = _handle_value(handle)
    if not _windows_kernel32().CloseHandle(value):
        raise _windows_error("windows-close-handle-failed")


def _create_windows_job() -> int:
    api = _windows_kernel32()
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise _windows_error("windows-create-job-failed")
    job_value = _handle_value(job)
    limits = _JobObjectExtendedLimitInformation()
    limits.basic_limit_information.limit_flags = (
        _JOB_OBJECT_LIMIT_BREAKAWAY_OK | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    if api.SetInformationJobObject(
        job_value,
        _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        return job_value
    error = _windows_error("windows-configure-job-failed")
    try:
        _close_windows_handle(job_value)
    except Exception as cleanup_error:
        raise cleanup_error from error
    raise error


def _native_startup_info(startup_info: Any, attribute_list: ctypes.c_void_p) -> _StartupInfoExW:
    native = _StartupInfoExW()
    native.startup_info.cb = ctypes.sizeof(native)
    native.startup_info.dwFlags = int(getattr(startup_info, "dwFlags", 0))
    native.startup_info.wShowWindow = int(getattr(startup_info, "wShowWindow", 0))
    for field in ("hStdInput", "hStdOutput", "hStdError"):
        value = getattr(startup_info, field, None)
        setattr(native.startup_info, field, 0 if value is None else int(value))
    native.attribute_list = attribute_list
    return native


def _attribute_list_for_job(
    job: int, startup_info: Any
) -> tuple[ctypes.Array[ctypes.c_char], ctypes.c_void_p, list[Any]]:
    attributes = getattr(startup_info, "lpAttributeList", None) or {}
    handles = attributes.get("handle_list", []) if isinstance(attributes, dict) else []
    handle_values = [_handle_value(handle) for handle in handles]
    keep_alive: list[Any] = []
    job_values = (ctypes.c_void_p * 1)(job)
    keep_alive.append(job_values)
    handle_values_array = (
        (ctypes.c_void_p * len(handle_values))(*handle_values)
        if handle_values
        else None
    )
    if handle_values_array is not None:
        keep_alive.append(handle_values_array)
    attribute_count = 1 + int(handle_values_array is not None)
    size = ctypes.c_size_t()
    _windows_kernel32().InitializeProcThreadAttributeList(
        None, attribute_count, 0, ctypes.byref(size)
    )
    if size.value <= 0:
        raise _windows_error("windows-attribute-list-size-failed")
    storage = ctypes.create_string_buffer(size.value)
    pointer = ctypes.cast(storage, ctypes.c_void_p)
    if not _windows_kernel32().InitializeProcThreadAttributeList(
        pointer, attribute_count, 0, ctypes.byref(size)
    ):
        raise _windows_error("windows-attribute-list-init-failed")
    api = _windows_kernel32()
    if not api.UpdateProcThreadAttribute(
        pointer,
        0,
        _PROC_THREAD_ATTRIBUTE_JOB_LIST,
        ctypes.cast(job_values, ctypes.c_void_p),
        ctypes.sizeof(job_values),
        None,
        None,
    ):
        api.DeleteProcThreadAttributeList(pointer)
        raise _windows_error("windows-job-attribute-failed")
    if handle_values_array is not None and not api.UpdateProcThreadAttribute(
        pointer,
        0,
        _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        ctypes.cast(handle_values_array, ctypes.c_void_p),
        ctypes.sizeof(handle_values_array),
        None,
        None,
    ):
        api.DeleteProcThreadAttributeList(pointer)
        raise _windows_error("windows-handle-attribute-failed")
    keep_alive.append(storage)
    keep_alive.append(pointer)
    return storage, pointer, keep_alive


def _validate_native_text(value: Any, name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be str or None")
    if isinstance(value, str) and "\0" in value:
        raise ValueError("embedded null byte")


def _native_environment_buffer(env_mapping: Any) -> ctypes.Array[Any] | None:
    if env_mapping is None:
        return None
    try:
        items = env_mapping.items()
    except AttributeError as exc:
        raise TypeError("env must be a mapping") from exc
    entries: list[str] = []
    for key, value in items:
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("environment keys and values must be str")
        _validate_native_text(key, "environment key")
        _validate_native_text(value, "environment value")
        entries.append(f"{key}={value}")
    return ctypes.create_unicode_buffer("\0".join(sorted(entries)) + "\0")


def _create_process_with_job(
    application_name: str | None,
    command_line: str | None,
    proc_attrs: Any,
    thread_attrs: Any,
    inherit_handles: int,
    creation_flags: int,
    env_mapping: Any,
    current_directory: str | None,
    startup_info: Any,
    *,
    job: int,
) -> tuple[int, int, int, int]:
    _validate_native_text(application_name, "application_name")
    _validate_native_text(command_line, "command_line")
    _validate_native_text(current_directory, "current_directory")
    environment_buffer = _native_environment_buffer(env_mapping)
    storage, attribute_pointer, keep_alive = _attribute_list_for_job(job, startup_info)
    del proc_attrs, thread_attrs
    command_buffer = ctypes.create_unicode_buffer(command_line) if command_line is not None else None
    flags = int(creation_flags) | _EXTENDED_STARTUPINFO_PRESENT
    if env_mapping is not None:
        flags |= _CREATE_UNICODE_ENVIRONMENT
    native_startup = _native_startup_info(startup_info, attribute_pointer)
    information = _ProcessInformation()
    api = _windows_kernel32()
    try:
        if not api.CreateProcessW(
            application_name,
            command_buffer,
            None,
            None,
            bool(inherit_handles),
            flags,
            environment_buffer,
            current_directory,
            ctypes.cast(ctypes.byref(native_startup), ctypes.c_void_p),
            ctypes.byref(information),
        ):
            raise _windows_error("windows-create-owned-process-failed")
        return (
            _handle_value(information.h_process),
            _handle_value(information.h_thread),
            int(information.process_id),
            int(information.thread_id),
        )
    finally:
        api.DeleteProcThreadAttributeList(attribute_pointer)


def _launch_windows_owned(
    command: Sequence[str], options: dict[str, Any]
) -> subprocess.Popen[Any]:
    # PROC_THREAD_ATTRIBUTE_JOB_LIST assigns the process during CreateProcess;
    # there is no suspended, unassigned process window for an owner crash.
    job = _create_windows_job()
    launch_options = dict(options)
    # Normal owned children remain inside host job constraints (e.g. CI).
    # Only spawn_detached requests an explicit breakaway.
    launch_options["creationflags"] = int(launch_options.get("creationflags", 0))
    startup_info = launch_options.get("startupinfo")
    startup_info = (
        subprocess.STARTUPINFO()
        if startup_info is None
        else startup_info.copy()
    )
    attributes = dict(getattr(startup_info, "lpAttributeList", {}) or {})
    attributes[_JOB_ATTRIBUTE_KEY] = job
    startup_info.lpAttributeList = attributes
    launch_options["startupinfo"] = startup_info
    process: subprocess.Popen[Any] | None = None
    try:
        import _winapi

        with _PROCESS_LAUNCH_LOCK:
            original_create_process = _winapi.CreateProcess

            def dispatch(*args: Any) -> Any:
                startup = args[8] if len(args) > 8 else None
                attrs = getattr(startup, "lpAttributeList", None) or {}
                requested_job = attrs.get(_JOB_ATTRIBUTE_KEY) if isinstance(attrs, dict) else None
                if isinstance(requested_job, int) and requested_job > 0:
                    return _create_process_with_job(*args, job=requested_job)
                return original_create_process(*args)

            _winapi.CreateProcess = dispatch
            try:
                process = subprocess.Popen(list(command), **launch_options)
            finally:
                _winapi.CreateProcess = original_create_process
        setattr(process, "_beyin_job_handle", job)
        return process
    except BaseException as original:
        try:
            _close_windows_handle(job)
        except Exception as cleanup_error:
            raise cleanup_error from original
        raise


def _launch_process(
    command: Sequence[str], options: dict[str, Any], *, owned: bool = True
) -> subprocess.Popen[Any]:
    if os.name == "nt" and owned:
        return _launch_windows_owned(command, options)
    return subprocess.Popen(list(command), **options)


class ProcessTreeTimeout(subprocess.TimeoutExpired):
    def __init__(self, command: Sequence[str], timeout: float, pid: int) -> None:
        super().__init__(command, timeout)
        self.pid = pid


class ProcessTreeCleanupError(ProcessTreeTimeout, OSError):
    """The process outcome is known, but tree cleanup was not verified."""

    def __init__(
        self,
        command: Sequence[str],
        timeout: float,
        pid: int,
        cleanup_error: BaseException,
    ) -> None:
        ProcessTreeTimeout.__init__(self, command, timeout, pid)
        OSError.__init__(self, str(cleanup_error))
        self.cleanup_error = cleanup_error


def _cleanup_error(process: Any, error: BaseException) -> ProcessTreeCleanupError:
    metadata = getattr(process, "__dict__", {})
    command = metadata.get("_beyin_command", [str(getattr(process, "pid", 0))])
    timeout = metadata.get("_beyin_timeout", 0.0)
    return ProcessTreeCleanupError(
        command,
        timeout,
        int(getattr(process, "pid", 0)),
        error,
    )


def _release_windows_job(process: Any) -> None:
    if os.name != "nt":
        return
    job = getattr(process, "__dict__", {}).get("_beyin_job_handle")
    if not isinstance(job, int) or job <= 0:
        return
    api = _windows_kernel32()
    errors: list[BaseException] = []
    closed = False
    try:
        if not api.TerminateJobObject(job, 1):
            errors.append(_windows_error("windows-terminate-job-failed"))
        else:
            wait_result = api.WaitForSingleObject(
                job, int(_JOB_CLEANUP_TIMEOUT_SECONDS * 1000)
            )
            if wait_result == 0xFFFFFFFF:
                errors.append(_windows_error("windows-wait-job-failed"))
            elif wait_result != _WAIT_OBJECT_0:
                errors.append(OSError("windows-job-members-still-running"))
            deadline = time.monotonic() + _JOB_CLEANUP_TIMEOUT_SECONDS
            while True:
                info = _JobObjectBasicAccountingInformation()
                returned = wintypes.DWORD()
                if not api.QueryInformationJobObject(
                    job,
                    _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                    ctypes.byref(returned),
                ):
                    errors.append(_windows_error("windows-query-job-failed"))
                    break
                if info.active_processes == 0:
                    break
                if time.monotonic() >= deadline:
                    errors.append(OSError("windows-job-members-still-running"))
                    break
                time.sleep(0.01)
    finally:
        try:
            _close_windows_handle(job)
            closed = True
        except BaseException as exc:
            errors.append(exc)
        if closed:
            setattr(process, "_beyin_job_handle", None)
    if errors:
        raise errors[0]


def _wait_for_process(process: Any, timeout: float) -> None:
    try:
        process.wait(timeout=timeout)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired as exc:
        raise OSError("process-tree-still-running") from exc


def _posix_group_members(pgid: int) -> list[int] | None:  # pragma: no cover — POSIX yolu; Windows CI/yerelde koşamaz.
    if not os.path.isdir("/proc"):
        return None
    members: list[int] = []
    try:
        entries = os.scandir("/proc")
    except OSError as exc:
        raise OSError("posix-process-group-unknown") from exc
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "stat"), encoding="ascii") as source:
                    _prefix, separator, fields = source.read().rpartition(") ")
                if not separator:
                    return None
                values = fields.split()
                if int(values[2]) == pgid:
                    members.append(int(entry.name))
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError, IndexError, ValueError) as exc:
                raise OSError("posix-process-group-unknown") from exc
    return members


def _posix_group_probe(pgid: int) -> bool:  # pragma: no cover — POSIX yolu; Windows CI/yerelde koşamaz.
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise OSError("posix-process-group-unknown") from exc
    return True


def _wait_for_posix_group_empty(pgid: int, timeout: float) -> list[int]:  # pragma: no cover — POSIX yolu; Windows CI/yerelde koşamaz.
    deadline = time.monotonic() + timeout
    while True:
        members = _posix_group_members(pgid)
        if members is None:
            if not _posix_group_probe(pgid):
                return []
            members = [pgid]
        if not members or time.monotonic() >= deadline:
            return members
        time.sleep(0.01)


def _terminate_unowned_windows_process(process: Any) -> None:
    metadata = getattr(process, "__dict__", {})
    identity = metadata.get("_beyin_process_identity")
    if not isinstance(identity, str) or not process_is_same(process.pid, identity):
        if "_beyin_command" not in metadata:
            # A bare Popen handle can authorize killing its own root, but it
            # carries no proof that descendants share the same ownership.
            try:
                _wait_for_process(process, 2)
            except OSError:
                try:
                    process.kill()
                except ProcessLookupError:
                    return
                _wait_for_process(process, 2)
            return
        raise OSError("windows-process-identity-unknown")
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError("windows-tree-kill-unverified") from exc
    if result.returncode != 0:
        raise OSError(f"windows-tree-kill-exit-{result.returncode}")
    _wait_for_process(process, 2)


def _terminate_posix_process(process: Any) -> None:  # pragma: no cover — POSIX yolu; Windows CI/yerelde koşamaz.
    if getattr(process, "_beyin_process_group", False) is True:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            _wait_for_process(process, 2)
        except OSError:
            pass
        members = _wait_for_posix_group_empty(process.pid, 2)
        if members:
            try:
                os.killpg(process.pid, getattr(signal, "SIGKILL", 9))
            except ProcessLookupError:
                pass
            try:
                _wait_for_process(process, 2)
            except OSError:
                pass
            members = _wait_for_posix_group_empty(process.pid, 2)
        if members:
            raise OSError("posix-process-group-still-running")
        return
    if process.poll() is None:
        process.terminate()
        _wait_for_process(process, 2)


def terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate only a tree whose ownership or process identity is known."""
    cleanup_error: BaseException | None = None
    metadata = getattr(process, "__dict__", {})
    job = metadata.get("_beyin_job_handle")
    has_job = os.name == "nt" and isinstance(job, int) and job > 0
    if has_job:
        try:
            _release_windows_job(process)
            _wait_for_process(process, 2)
        except BaseException as exc:
            cleanup_error = exc
    elif process.poll() is None or metadata.get("_beyin_process_group") is True:
        try:
            if os.name == "nt":
                _terminate_unowned_windows_process(process)
            else:
                _terminate_posix_process(process)
        except BaseException as exc:
            cleanup_error = exc
    if cleanup_error is not None:
        raise _cleanup_error(process, cleanup_error) from cleanup_error


def _posix_process_identity(pid: int) -> str | None:  # pragma: no cover — POSIX yolu; Windows CI/yerelde koşamaz.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as source:
            raw = source.read()
        _prefix, separator, fields = raw.rpartition(") ")
        if not separator:
            return None
        values = fields.split()
        start_time = values[19]
    except (OSError, UnicodeError, IndexError, ValueError):
        return None
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as source:
            boot_id = source.read().strip()
    except (OSError, UnicodeError):
        boot_id = ""
    return f"proc:{boot_id}:{start_time}"


def process_identity(pid: int) -> str | None:
    """Return a birth identity, or None when the OS cannot prove one.

    Windows uses GetProcessTimes' creation FILETIME; see Microsoft Learn:
    https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name != "nt":
        return _posix_process_identity(pid)
    try:
        api = _windows_kernel32()
        handle = api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        handle_value = _handle_value(handle)
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        try:
            if not api.GetProcessTimes(
                handle_value,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        finally:
            _close_windows_handle(handle_value)
    except (OSError, TypeError, ValueError):
        return None
    return f"win32:{ticks:016x}"


def process_is_same(pid: int, identity: str) -> bool:
    if not isinstance(identity, str) or not identity:
        return False
    current = process_identity(pid)
    return current is not None and current == identity


def pid_is_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            api = _windows_kernel32()
            handle = api.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
                False,
                pid,
            )
            if not handle:
                return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
            handle_value = _handle_value(handle)
            try:
                result = api.WaitForSingleObject(handle_value, 0)
                if result == _WAIT_OBJECT_0:
                    return False
                return True
            finally:
                _close_windows_handle(handle_value)
        except (OSError, TypeError, ValueError):
            return True
    else:  # pragma: no cover — POSIX dalı; Windows CI/yerelde koşamaz.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            return exc.errno != errno.ESRCH
        return True


def spawn_detached(
    command: Sequence[str],
    *,
    popen_factory: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Launch a process outside this caller's owned tree."""
    options = dict(kwargs)
    if os.name == "nt":
        options["creationflags"] = int(options.get("creationflags", 0)) | int(
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | _CREATE_BREAKAWAY_FROM_JOB
        )
    else:
        options.setdefault("start_new_session", True)
    return (popen_factory or subprocess.Popen)(list(command), **options)


def run_with_tree_timeout(
    command: Sequence[str],
    *,
    timeout: float,
    input: bytes | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    if timeout <= 0:
        raise ValueError("process-timeout-invalid")
    options = dict(kwargs)
    if input is not None:
        if "stdin" in options:
            raise ValueError("stdin and input arguments may not both be used")
        options["stdin"] = subprocess.PIPE
    if os.name == "nt":
        options["creationflags"] = int(options.get("creationflags", 0)) | int(
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        options.setdefault("start_new_session", True)
    process = _launch_process(command, options, owned=True)
    setattr(process, "_beyin_command", list(command))
    setattr(process, "_beyin_timeout", timeout)
    setattr(process, "_beyin_process_identity", process_identity(process.pid))
    setattr(process, "_beyin_process_group", os.name != "nt")
    failure: BaseException | None = None
    cleanup_called = False
    stdout: Any = None
    stderr: Any = None
    try:
        try:
            if input is None:
                stdout, stderr = process.communicate(timeout=timeout)
            else:
                stdout, stderr = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                terminate_process_tree(process)
                cleanup_called = True
            except BaseException as cleanup_error:
                failure = _cleanup_error(process, cleanup_error)
            else:
                failure = ProcessTreeTimeout(command, timeout, process.pid)
        except BaseException as exc:
            try:
                terminate_process_tree(process)
                cleanup_called = True
            except BaseException as cleanup_error:
                failure = _cleanup_error(process, cleanup_error)
            else:
                failure = exc
        else:
            if os.name != "nt":
                try:
                    terminate_process_tree(process)
                    cleanup_called = True
                except BaseException as cleanup_error:
                    failure = _cleanup_error(process, cleanup_error)
    finally:
        cleanup_errors: list[BaseException] = []
        if input is not None:
            stdin = getattr(process, "stdin", None)
            if stdin is not None:
                try:
                    stdin.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
        if os.name == "nt" and not cleanup_called:
            try:
                _release_windows_job(process)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors and failure is None:
            failure = _cleanup_error(process, cleanup_errors[0])
    if failure is not None:
        raise failure
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
