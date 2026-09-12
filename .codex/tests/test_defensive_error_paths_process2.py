"""process_control — dilim 2: sahiplenilmiş süreç, POSIX kolları ve temizlik hataları."""

from __future__ import annotations

import ctypes
import subprocess
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import process_control as pc


def _api(**overrides) -> mock.Mock:
    api = mock.Mock()
    api.CloseHandle.return_value = 1
    api.CreateJobObjectW.return_value = 7
    api.SetInformationJobObject.return_value = 1
    api.UpdateProcThreadAttribute.return_value = 1
    for name, value in overrides.items():
        setattr(api, name, value)
    return api


class OwnedLaunchArms(unittest.TestCase):
    def test_create_process_failure_raises(self) -> None:
        api = _api()
        api.CreateProcessW.return_value = 0
        with (
            mock.patch.object(pc, "_windows_kernel32", return_value=api),
            mock.patch.object(
                pc, "_attribute_list_for_job",
                return_value=(None, ctypes.c_void_p(), []),
            ),
            mock.patch.object(
                pc, "_native_startup_info", return_value=ctypes.c_int(0)
            ),
        ):
            with self.assertRaises(OSError):
                pc._create_process_with_job(
                    None, "cmd", None, None, 0, 0, None, None, None, job=7
                )
        api.DeleteProcThreadAttributeList.assert_called()

    def test_dispatch_passthrough_without_job_attribute(self) -> None:
        import _winapi

        original = mock.Mock(return_value=(10, 11, 12, 13))

        def fake_popen(command, **options):
            startup = types.SimpleNamespace(lpAttributeList={})
            _winapi.CreateProcess(
                None, "x", None, None, 0, 0, None, None, startup
            )
            return types.SimpleNamespace(pid=12, __dict__={})

        with (
            mock.patch.object(pc, "_create_windows_job", return_value=7),
            mock.patch.object(_winapi, "CreateProcess", original),
            mock.patch.object(pc.subprocess, "Popen", fake_popen),
        ):
            process = pc._launch_windows_owned(["cmd"], {})
        original.assert_called_once()
        self.assertEqual(process.pid, 12)

    def test_cleanup_error_chains_original(self) -> None:
        with (
            mock.patch.object(pc, "_create_windows_job", return_value=7),
            mock.patch.object(
                pc.subprocess, "Popen", side_effect=RuntimeError("patla")
            ),
            mock.patch.object(
                pc, "_close_windows_handle", side_effect=OSError("kapanmadı")
            ),
        ):
            with self.assertRaises(OSError):
                pc._launch_windows_owned(["cmd"], {})

    def test_unowned_launch_uses_plain_popen(self) -> None:
        sentinel = types.SimpleNamespace(pid=5)
        with mock.patch.object(
            pc.subprocess, "Popen", return_value=sentinel
        ):
            self.assertIs(
                pc._launch_process(["cmd"], {}, owned=False), sentinel
            )


class ReleaseJobArms(unittest.TestCase):
    def test_posix_release_is_noop(self) -> None:
        with mock.patch.object(pc.os, "name", "posix"):
            pc._release_windows_job(types.SimpleNamespace())

    def test_members_still_running_after_deadline(self) -> None:
        api = _api()
        api.TerminateJobObject.return_value = 1
        api.WaitForSingleObject.return_value = pc._WAIT_OBJECT_0

        def query(_job, _cls, info_ref, _size, _returned):
            info_ref._obj.active_processes = 1
            return 1

        api.QueryInformationJobObject.side_effect = query
        process = types.SimpleNamespace()
        process.__dict__["_beyin_job_handle"] = 7
        with (
            mock.patch.object(pc, "_windows_kernel32", return_value=api),
            mock.patch.object(pc, "_JOB_CLEANUP_TIMEOUT_SECONDS", 0.02),
        ):
            with self.assertRaises(OSError):
                pc._release_windows_job(process)


class TerminateArms(unittest.TestCase):
    def test_bare_process_kill_race(self) -> None:
        def kill() -> None:
            raise ProcessLookupError

        process = types.SimpleNamespace(pid=1, kill=kill)
        with mock.patch.object(
            pc, "_wait_for_process", side_effect=OSError("bekleme")
        ):
            pc._terminate_unowned_windows_process(process)

    def test_posix_tree_termination_arm(self) -> None:
        process = types.SimpleNamespace(poll=lambda: None)
        posix = mock.Mock()
        with (
            mock.patch.object(pc.os, "name", "posix"),
            mock.patch.object(pc, "_terminate_posix_process", posix),
        ):
            pc.terminate_process_tree(process)
        posix.assert_called_once_with(process)


class IdentityArms(unittest.TestCase):
    def test_posix_identity_arm(self) -> None:
        with (
            mock.patch.object(pc.os, "name", "posix"),
            mock.patch.object(
                pc, "_posix_process_identity", return_value="proc:x"
            ),
        ):
            self.assertEqual(pc.process_identity(5), "proc:x")

    def test_windows_handle_close_failure_returns_none(self) -> None:
        api = _api()
        api.OpenProcess.return_value = 5
        api.GetProcessTimes.return_value = 1
        api.CloseHandle.return_value = 0
        with mock.patch.object(pc, "_windows_kernel32", return_value=api):
            self.assertIsNone(pc.process_identity(5))


class DetachedAndRunnerArms(unittest.TestCase):
    def test_spawn_detached_posix_session_flag(self) -> None:
        factory = mock.Mock(return_value="süreç")
        with mock.patch.object(pc.os, "name", "posix"):
            result = pc.spawn_detached(["cmd"], popen_factory=factory)
        self.assertEqual(result, "süreç")
        self.assertTrue(factory.call_args.kwargs["start_new_session"])

    def _process(self, **overrides) -> types.SimpleNamespace:
        process = types.SimpleNamespace(
            pid=999_999,
            returncode=0,
            stdin=None,
            communicate=mock.Mock(return_value=(b"", b"")),
            poll=lambda: 0,
        )
        for name, value in overrides.items():
            setattr(process, name, value)
        return process

    def test_posix_runner_cleanup_success_and_failure(self) -> None:
        process = self._process()
        with (
            mock.patch.object(pc.os, "name", "posix"),
            mock.patch.object(pc, "_launch_process", return_value=process),
            mock.patch.object(
                pc, "_posix_process_identity", return_value=None
            ),
            mock.patch.object(pc, "terminate_process_tree") as terminate,
        ):
            result = pc.run_with_tree_timeout(["cmd"], timeout=5)
        self.assertEqual(result.returncode, 0)
        terminate.assert_called_once()

        process = self._process()
        with (
            mock.patch.object(pc.os, "name", "posix"),
            mock.patch.object(pc, "_launch_process", return_value=process),
            mock.patch.object(
                pc, "_posix_process_identity", return_value=None
            ),
            mock.patch.object(
                pc, "terminate_process_tree",
                side_effect=RuntimeError("temizlik"),
            ),
        ):
            with self.assertRaises(pc.ProcessTreeCleanupError):
                pc.run_with_tree_timeout(["cmd"], timeout=5)

    def test_generic_failure_with_cleanup_failure(self) -> None:
        process = self._process(
            communicate=mock.Mock(side_effect=RuntimeError("çöktü"))
        )
        with (
            mock.patch.object(pc, "_launch_process", return_value=process),
            mock.patch.object(
                pc, "terminate_process_tree",
                side_effect=RuntimeError("temizlik"),
            ),
            mock.patch.object(pc, "_release_windows_job"),
        ):
            with self.assertRaises(pc.ProcessTreeCleanupError):
                pc.run_with_tree_timeout(["cmd"], timeout=5)

    def test_stdin_close_and_release_failures_collected(self) -> None:
        stdin = mock.Mock()
        stdin.close.side_effect = RuntimeError("kapanmadı")
        process = self._process(
            stdin=stdin,
            communicate=mock.Mock(return_value=(b"", b"")),
        )
        with (
            mock.patch.object(pc, "_launch_process", return_value=process),
            mock.patch.object(pc, "_release_windows_job"),
        ):
            with self.assertRaises(pc.ProcessTreeCleanupError):
                pc.run_with_tree_timeout(["cmd"], timeout=5, input=b"girdi")

        process = self._process()
        with (
            mock.patch.object(pc, "_launch_process", return_value=process),
            mock.patch.object(
                pc, "_release_windows_job",
                side_effect=RuntimeError("bırakılamadı"),
            ),
        ):
            with self.assertRaises(pc.ProcessTreeCleanupError):
                pc.run_with_tree_timeout(["cmd"], timeout=5)


if __name__ == "__main__":
    unittest.main()
