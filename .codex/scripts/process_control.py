from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
from typing import Any, Callable, Sequence


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_CREATE_SUSPENDED = 0x00000004
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


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


class _ThreadEntry32(ctypes.Structure):
    _fields_ = (
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
        ("owner_process_id", wintypes.DWORD),
        ("base_priority", wintypes.LONG),
        ("delta_priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
    )


# Loaded ctypes members stay dynamic even after runtime argtypes/restype setup.
_WINDOWS_KERNEL32: Any | None = None


def _windows_kernel32() -> Any:
    global _WINDOWS_KERNEL32
    if _WINDOWS_KERNEL32 is not None:
        return _WINDOWS_KERNEL32
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenThread.restype = wintypes.HANDLE
    api.ResumeThread.argtypes = [wintypes.HANDLE]
    api.ResumeThread.restype = wintypes.DWORD
    api.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    api.Thread32First.restype = wintypes.BOOL
    api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    api.Thread32Next.restype = wintypes.BOOL
    _WINDOWS_KERNEL32 = api
    return api


def _windows_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _close_windows_handle(handle: int) -> None:
    if not handle:
        return
    try:
        _windows_kernel32().CloseHandle(handle)
    except OSError:
        pass


def _create_windows_job() -> int:
    api = _windows_kernel32()
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise _windows_error()
    try:
        limits = _JobObjectExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not api.SetInformationJobObject(
            job,
            _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise _windows_error()
        return int(job)
    except BaseException:
        _close_windows_handle(int(job))
        raise


def _assign_windows_job(job: int, process: subprocess.Popen[Any]) -> None:
    process_handle = getattr(process, "_handle", None)
    if not isinstance(process_handle, int):
        raise OSError("windows-process-handle-missing")
    if not _windows_kernel32().AssignProcessToJobObject(job, process_handle):
        raise _windows_error()


def _resume_windows_process(pid: int) -> None:
    api = _windows_kernel32()
    snapshot = api.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        raise _windows_error()
    try:
        entry = _ThreadEntry32()
        entry.size = ctypes.sizeof(entry)
        has_entry = api.Thread32First(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.owner_process_id == pid:
                thread = api.OpenThread(
                    _THREAD_SUSPEND_RESUME,
                    False,
                    entry.thread_id,
                )
                if thread:
                    try:
                        if api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise _windows_error()
                        return
                    finally:
                        _close_windows_handle(int(thread))
            has_entry = api.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        _close_windows_handle(int(snapshot))
    raise ProcessLookupError(f"windows-primary-thread-missing: {pid}")


def _kill_unresumed_process(process: subprocess.Popen[Any]) -> None:
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass


def _launch_windows_owned(
    command: Sequence[str], options: dict[str, Any]
) -> subprocess.Popen[Any]:
    # Keep user code suspended until the kill-on-close job owns the process.
    job = _create_windows_job()
    launch_options = dict(options)
    launch_options["creationflags"] = int(
        launch_options.get("creationflags", 0)
    ) | _CREATE_SUSPENDED
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(list(command), **launch_options)
        _assign_windows_job(job, process)
        setattr(process, "_beyin_job_handle", job)
        _resume_windows_process(process.pid)
        return process
    except BaseException:
        if process is not None:
            _kill_unresumed_process(process)
        _close_windows_handle(job)
        raise


def _launch_process(
    command: Sequence[str], options: dict[str, Any], *, owned: bool
) -> subprocess.Popen[Any]:
    # Popen keyword options are platform-specific and remain dynamic here.
    if os.name == "nt" and owned:
        return _launch_windows_owned(command, options)
    return subprocess.Popen(list(command), **options)


def _release_windows_job(process: subprocess.Popen[Any]) -> None:
    if os.name != "nt":
        return
    job = getattr(process, "_beyin_job_handle", None)
    if not isinstance(job, int) or job <= 0:
        return
    setattr(process, "_beyin_job_handle", None)
    _close_windows_handle(job)


class ProcessTreeTimeout(subprocess.TimeoutExpired):
    def __init__(self, command: Sequence[str], timeout: float, pid: int) -> None:
        super().__init__(command, timeout)
        self.pid = pid


def pid_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information | synchronize,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            wait_timeout = 0x00000102
            return bool(ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    _release_windows_job(process)
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            # Windows stubs omit these POSIX-only stdlib members.
            getattr(os, "killpg")(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
        process.wait(timeout=2)


def spawn_detached(
    command: Sequence[str],
    *,
    popen_factory: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Launch a process that outlives its parent's process tree.

    Same roof as ``terminate_process_tree``: a caller that walks away must not
    be reachable by the parent's ``taskkill /T`` or process-group signal.
    ``popen_factory`` is the test seam; production passes nothing.
    """
    options = dict(kwargs)
    if os.name == "nt":
        options["creationflags"] = int(options.get("creationflags", 0)) | int(
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
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
    # Only stdin-fed runs own the tree; detached worker launches keep their old owner.
    process = _launch_process(command, options, owned=input is not None)
    try:
        if input is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            terminate_process_tree(process)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise ProcessTreeTimeout(command, timeout, process.pid) from exc
    except OSError:
        try:
            terminate_process_tree(process)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise
    finally:
        _release_windows_job(process)
        if input is not None:
            stdin = getattr(process, "stdin", None)
            if stdin is not None:
                try:
                    stdin.close()
                except OSError:
                    pass
    return subprocess.CompletedProcess(
        list(command),
        process.returncode,
        stdout,
        stderr,
    )
