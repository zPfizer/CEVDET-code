"""process_control'un Windows API hata dallarını sahte kernel32 ile sürer."""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import process_control as pc


class _Proc:
    def __init__(self, pid: int = 1234):
        self.pid = pid


def _api(**overrides) -> mock.Mock:
    api = mock.Mock()
    api.CloseHandle.return_value = 1
    api.CreateJobObjectW.return_value = 7
    api.SetInformationJobObject.return_value = 1
    api.InitializeProcThreadAttributeList.side_effect = None
    api.UpdateProcThreadAttribute.return_value = 1
    for name, value in overrides.items():
        setattr(api, name, value)
    return api


class HandleAndJobGuards(unittest.TestCase):
    def test_handle_value_rejects_non_positive(self) -> None:
        with self.assertRaises(OSError):
            pc._handle_value("x")
        with self.assertRaises(OSError):
            pc._handle_value(-1)

    def test_close_handle_failure_raises(self) -> None:
        api = _api()
        api.CloseHandle.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._close_windows_handle(5)

    def test_create_job_failure_paths(self) -> None:
        api = _api()
        api.CreateJobObjectW.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._create_windows_job()

        api = _api()
        api.SetInformationJobObject.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._create_windows_job()
        api.CloseHandle.assert_called()

        api = _api()
        api.SetInformationJobObject.return_value = 0
        api.CloseHandle.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._create_windows_job()

    def test_attribute_list_failure_paths(self) -> None:
        def size_zero(_p, _n, _f, size_ref):
            return 0

        api = _api()
        api.InitializeProcThreadAttributeList.side_effect = size_zero
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._attribute_list_for_job(7, None)

        calls = {"n": 0}

        def size_then_fail(_p, _n, _f, size_ref):
            calls["n"] += 1
            if calls["n"] == 1:
                size_ref._obj.value = 64
                return 0
            return 0

        api = _api()
        api.InitializeProcThreadAttributeList.side_effect = size_then_fail
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._attribute_list_for_job(7, None)

        def size_ok(_p, _n, _f, size_ref):
            size_ref._obj.value = 64
            return 1

        api = _api()
        api.InitializeProcThreadAttributeList.side_effect = size_ok
        api.UpdateProcThreadAttribute.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._attribute_list_for_job(7, None)
        api.DeleteProcThreadAttributeList.assert_called()

        api = _api()
        api.InitializeProcThreadAttributeList.side_effect = size_ok
        api.UpdateProcThreadAttribute.side_effect = [1, 0]
        startup = mock.Mock()
        startup.lpAttributeList = {"handle_list": [11]}
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._attribute_list_for_job(7, startup)

    def test_native_text_and_environment_guards(self) -> None:
        with self.assertRaises(TypeError):
            pc._validate_native_text(5, "alan")
        with self.assertRaises(ValueError):
            pc._validate_native_text("a\0b", "alan")
        with self.assertRaises(TypeError):
            pc._native_environment_buffer(5)
        with self.assertRaises(TypeError):
            pc._native_environment_buffer({"A": 1})
        self.assertIsNone(pc._native_environment_buffer(None))

    def test_launch_windows_owned_closes_job_on_failure(self) -> None:
        api = _api()
        api.CreateProcessW.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._launch_windows_owned(
                    [sys.executable, "-c", "pass"],
                    {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
                )
        api.CloseHandle.assert_called()


class ReleaseAndTerminateGuards(unittest.TestCase):
    def _job_process(self) -> _Proc:
        process = _Proc()
        process._beyin_job_handle = 9
        return process

    def test_release_windows_job_error_collection(self) -> None:
        api = _api()
        api.TerminateJobObject.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._release_windows_job(self._job_process())

        api = _api()
        api.TerminateJobObject.return_value = 1
        api.WaitForSingleObject.return_value = 0xFFFFFFFF
        api.QueryInformationJobObject.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._release_windows_job(self._job_process())

        api = _api()
        api.TerminateJobObject.return_value = 1
        api.WaitForSingleObject.return_value = pc._WAIT_OBJECT_0

        def query_ok(_job, _cls, info_ref, _size, _returned):
            info_ref._obj.active_processes = 0
            return 1

        api.QueryInformationJobObject.side_effect = query_ok
        api.CloseHandle.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with self.assertRaises(OSError):
                pc._release_windows_job(self._job_process())

    def test_wait_for_process_translates_timeout(self) -> None:
        process = mock.Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("x", 1)
        with self.assertRaises(OSError):
            pc._wait_for_process(process, 0.1)
        process.wait.side_effect = ProcessLookupError
        pc._wait_for_process(process, 0.1)

    def test_terminate_unowned_windows_paths(self) -> None:
        bare = mock.Mock(spec=["pid", "wait", "kill"])
        bare.pid = 1234
        bare.wait.side_effect = [subprocess.TimeoutExpired("x", 1), None]
        pc._terminate_unowned_windows_process(bare)
        bare.kill.assert_called_once()

        marked = _Proc()
        marked._beyin_command = ["x"]
        with self.assertRaises(OSError):
            pc._terminate_unowned_windows_process(marked)

        verified = _Proc()
        verified._beyin_process_identity = "kimlik"
        with mock.patch.object(pc, "process_is_same", return_value=True):
            with mock.patch.object(
                pc.subprocess, "run",
                return_value=mock.Mock(returncode=3),
            ):
                with self.assertRaises(OSError):
                    pc._terminate_unowned_windows_process(verified)
            with mock.patch.object(
                pc.subprocess, "run", side_effect=subprocess.TimeoutExpired("t", 1)
            ):
                with self.assertRaises(OSError):
                    pc._terminate_unowned_windows_process(verified)

    def test_terminate_tree_wraps_cleanup_failures(self) -> None:
        job_process = _Proc()
        job_process._beyin_job_handle = 9
        with mock.patch.object(pc, "_release_windows_job", side_effect=OSError("iş")):
            with self.assertRaises(pc.ProcessTreeCleanupError):
                pc.terminate_process_tree(job_process)


class IdentityAndAliveGuards(unittest.TestCase):
    def test_posix_identity_fails_closed_on_windows(self) -> None:
        self.assertIsNone(pc._posix_process_identity(1))

    def test_process_identity_failure_arms(self) -> None:
        self.assertIsNone(pc.process_identity(0))
        api = _api()
        api.OpenProcess.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            self.assertIsNone(pc.process_identity(1234))
        api = _api()
        api.OpenProcess.return_value = 5
        api.GetProcessTimes.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            self.assertIsNone(pc.process_identity(1234))

    def test_pid_is_alive_windows_arms(self) -> None:
        self.assertFalse(pc.pid_is_alive(0))
        api = _api()
        api.OpenProcess.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            with mock.patch.object(
                pc.ctypes, "get_last_error",
                return_value=pc._ERROR_INVALID_PARAMETER,
            ):
                self.assertFalse(pc.pid_is_alive(99999))
            with mock.patch.object(pc.ctypes, "get_last_error", return_value=5):
                self.assertTrue(pc.pid_is_alive(99999))
        api = _api()
        api.OpenProcess.return_value = 5
        api.WaitForSingleObject.return_value = pc._WAIT_OBJECT_0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            self.assertFalse(pc.pid_is_alive(99999))
        with mock.patch.object(pc, "_windows_kernel32", side_effect=TypeError):
            self.assertTrue(pc.pid_is_alive(99999))


class RunnerGuards(unittest.TestCase):
    def test_argument_guards(self) -> None:
        with self.assertRaises(ValueError):
            pc.run_with_tree_timeout(["x"], timeout=0)
        with self.assertRaises(ValueError):
            pc.run_with_tree_timeout(
                ["x"], timeout=1, input=b"a", stdin=subprocess.PIPE
            )

    def test_timeout_terminates_real_tree(self) -> None:
        with self.assertRaises(pc.ProcessTreeTimeout):
            pc.run_with_tree_timeout(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=0.3,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    unittest.main()
