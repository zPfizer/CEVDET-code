"""C06 process ownership, cleanup, breakaway, and birth identity checks."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (sys.path seam)

import codex_runner
import process_control


def _wait_dead(pid: int, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while process_control.pid_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not process_control.pid_is_alive(pid)


def _read_pid(path: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = path.read_text(encoding="ascii").strip()
        except OSError:
            value = ""
        if value.isdigit():
            return int(value)
        time.sleep(0.05)
    raise AssertionError(f"pid file was not ready: {path}")


@unittest.skipUnless(os.name == "nt", "Windows Job Object contract")
class WindowsProcessControlTests(unittest.TestCase):
    def test_owned_launch_works_inside_a_non_breakaway_host_job(self):
        original_create_job = process_control._create_windows_job

        def restricted_host_job():
            job = original_create_job()
            limits = process_control._JobObjectExtendedLimitInformation()
            limits.basic_limit_information.limit_flags = process_control._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            api = process_control._windows_kernel32()
            if not api.SetInformationJobObject(job, process_control._JOBOBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)):
                process_control._close_windows_handle(job)
                raise OSError('could not configure synthetic host job')
            return job

        child = (
            "import sys,subprocess; sys.path.insert(0,sys.argv[1]); import process_control as pc; "
            "r=pc.run_with_tree_timeout([sys.executable,'-c','print(12345)'],timeout=5,stdout=subprocess.PIPE,text=True); "
            "print(r.stdout,end=''); sys.exit(r.returncode)"
        )
        with mock.patch.object(process_control, '_create_windows_job', side_effect=restricted_host_job):
            result = process_control.run_with_tree_timeout(
                [sys.executable, '-c', child, str(Path(process_control.__file__).parent)],
                timeout=10, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(result.stdout.strip(), b'12345')

    def _kill_test_pid(self, pid: int | None) -> None:
        if pid is not None and process_control.pid_is_alive(pid):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            _wait_dead(pid)

    def test_timeout_kills_real_parent_and_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid = root / "child.pid"
            parent = (
                "import subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)'], creationflags=subprocess.CREATE_NO_WINDOW); "
                "Path(sys.argv[1]).write_text(str(child.pid), encoding=\"ascii\"); "
                "time.sleep(30)"
            )
            with self.assertRaises(process_control.ProcessTreeTimeout):
                process_control.run_with_tree_timeout(
                    [sys.executable, "-c", parent, str(child_pid)],
                    timeout=0.5,
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            child = _read_pid(child_pid)
            self.assertTrue(_wait_dead(child), child)

    def test_wrapper_exit_closes_non_detached_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid = root / "child.pid"
            wrapper = (
                "import subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)'], creationflags=subprocess.CREATE_NO_WINDOW); "
                "Path(sys.argv[1]).write_text(str(child.pid), encoding=\"ascii\"); "
                "time.sleep(.05)"
            )
            process_control.run_with_tree_timeout(
                [sys.executable, "-c", wrapper, str(child_pid)],
                timeout=5,
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child = _read_pid(child_pid)
            self.assertTrue(_wait_dead(child), child)

    def test_explicit_breakaway_child_survives_owned_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid_path = root / "child.pid"
            stop_path = root / "stop"
            ready_path = root / "ready"
            heartbeat_path = root / "heartbeat"
            scripts = Path(process_control.__file__).parent
            child_code = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "stop=Path(sys.argv[1])\n"
                "ready=Path(sys.argv[2])\n"
                "heartbeat=Path(sys.argv[3])\n"
                "ready.write_text('ready', encoding='ascii')\n"
                "count=0\n"
                "while not stop.exists():\n"
                "    count += 1\n"
                "    heartbeat.write_text(str(count), encoding='ascii')\n"
                "    time.sleep(.05)\n"
            )
            wrapper = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "sys.path.insert(0,sys.argv[1])\n"
                "import process_control\n"
                "child=process_control.spawn_detached([sys.executable,'-c',sys.argv[3],sys.argv[4],sys.argv[5],sys.argv[6]],\n"
                "    cwd=sys.argv[2], stdin=process_control.subprocess.DEVNULL,\n"
                "    stdout=process_control.subprocess.DEVNULL, stderr=process_control.subprocess.DEVNULL)\n"
                "deadline=time.monotonic()+4\n"
                "while not Path(sys.argv[5]).exists() and time.monotonic()<deadline:\n"
                "    time.sleep(.01)\n"
                "Path(sys.argv[7]).write_text(str(child.pid), encoding='ascii')\n"
            )
            child: int | None = None
            try:
                process_control.run_with_tree_timeout(
                    [
                        sys.executable,
                        "-c",
                        wrapper,
                        str(scripts),
                        str(root),
                        child_code,
                        str(stop_path),
                        str(ready_path),
                        str(heartbeat_path),
                        str(child_pid_path),
                    ],
                    timeout=5,
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                child = _read_pid(child_pid_path)
                self.assertTrue(process_control.pid_is_alive(child), child)
                self.assertEqual(ready_path.read_text(encoding="ascii"), "ready")
                first_heartbeat = heartbeat_path.read_text(encoding="ascii")
                time.sleep(.2)
                self.assertNotEqual(
                    heartbeat_path.read_text(encoding="ascii"), first_heartbeat
                )
            finally:
                if child is not None:
                    stop_path.write_text("stop", encoding="ascii")
                    if not _wait_dead(child):
                        self._kill_test_pid(child)

    def test_owner_process_crash_closes_its_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid_path = root / "child.pid"
            owner = (
                "import os,sys,time; sys.path.insert(0,sys.argv[1]); "
                "import process_control; "
                "process_control.run_with_tree_timeout([sys.executable,'-c',"
                "'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid()), encoding=\"ascii\"); time.sleep(30)',"
                "sys.argv[2]], timeout=30, cwd=sys.argv[3], stdin=process_control.subprocess.DEVNULL, "
                "stdout=process_control.subprocess.DEVNULL, stderr=process_control.subprocess.DEVNULL)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", owner, str(Path(process_control.__file__).parent), str(child_pid_path), str(root)],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                child = _read_pid(child_pid_path)
                process.kill()
                process.wait(timeout=5)
                self.assertTrue(_wait_dead(child), child)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if child_pid_path.exists():
                    try:
                        self._kill_test_pid(_read_pid(child_pid_path, timeout=0.2))
                    except AssertionError:
                        pass

    def test_owner_crash_after_popen_return_has_no_unassigned_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owned_pid_path = root / "owned.pid"
            release_path = root / "release"
            scripts = Path(process_control.__file__).parent
            owner = (
                "import subprocess,sys,time\n"
                "from pathlib import Path\n"
                "sys.path.insert(0,sys.argv[1])\n"
                "import process_control\n"
                "real_popen=subprocess.Popen\n"
                "def hold(*args,**kwargs):\n"
                "    child=real_popen(*args,**kwargs)\n"
                "    Path(sys.argv[2]).write_text(str(child.pid),encoding='ascii')\n"
                "    while not Path(sys.argv[3]).exists(): time.sleep(.01)\n"
                "    return child\n"
                "subprocess.Popen=hold\n"
                "process_control.run_with_tree_timeout([sys.executable,'-c','import time; time.sleep(30)'],"
                "timeout=30,cwd=sys.argv[4],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
                "stderr=subprocess.DEVNULL)\n"
            )
            owner_process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    owner,
                    str(scripts),
                    str(owned_pid_path),
                    str(release_path),
                    str(root),
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            owned_pid: int | None = None
            try:
                owned_pid = _read_pid(owned_pid_path)
                owner_process.kill()
                owner_process.wait(timeout=5)
                self.assertTrue(_wait_dead(owned_pid), owned_pid)
            finally:
                if owner_process.poll() is None:
                    owner_process.kill()
                owner_process.wait(timeout=5)
                if owned_pid is not None and not _wait_dead(owned_pid, timeout=0.5):
                    self._kill_test_pid(owned_pid)

    def test_detached_flags_request_a_real_breakaway(self) -> None:
        captured: dict[str, object] = {}

        def fake_popen(command: list[str], **kwargs: object) -> str:
            captured["command"] = command
            captured["kwargs"] = kwargs
            return "handle"

        process_control.spawn_detached(
            [sys.executable, "-c", "pass"],
            popen_factory=fake_popen,
            stdin=subprocess.DEVNULL,
        )
        flags = captured["kwargs"]["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(flags & subprocess.DETACHED_PROCESS)
        self.assertTrue(flags & process_control._CREATE_BREAKAWAY_FROM_JOB)

    def test_owned_launch_uses_atomic_native_job_attribute(self) -> None:
        with mock.patch.object(
            process_control,
            "_create_process_with_job",
            wraps=process_control._create_process_with_job,
        ) as create:
            result = process_control.run_with_tree_timeout(
                [sys.executable, "-c", "pass"],
                timeout=5,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.assertEqual(result.returncode, 0)
        create.assert_called_once()

    def test_native_launch_rejects_embedded_nul_before_createprocess(self) -> None:
        with (
            mock.patch.object(process_control, "_attribute_list_for_job") as attrs,
            self.assertRaises(ValueError),
        ):
            process_control._create_process_with_job(
                None,
                "before\0after",
                None,
                None,
                0,
                0,
                None,
                None,
                object(),
                job=1,
            )
        attrs.assert_not_called()

    def test_native_launch_preserves_environment_type_validation(self) -> None:
        with self.assertRaises(TypeError):
            process_control._create_process_with_job(
                None,
                "child",
                None,
                None,
                0,
                0,
                {"VALUE": 1},
                None,
                object(),
                job=1,
            )


class ProcessIdentityTests(unittest.TestCase):
    def test_current_process_has_a_stable_birth_identity(self) -> None:
        identity = process_control.process_identity(os.getpid())
        self.assertIsInstance(identity, str)
        self.assertTrue(identity)
        self.assertTrue(process_control.process_is_same(os.getpid(), identity))

    def test_unknown_identity_fails_closed(self) -> None:
        with mock.patch.object(process_control, "process_identity", return_value=None):
            self.assertFalse(process_control.process_is_same(4242, "win32:unknown"))
        self.assertFalse(process_control.process_is_same(os.getpid(), ""))
        self.assertFalse(process_control.process_is_same(os.getpid(), None))  # type: ignore[arg-type]

    def test_pid_reuse_simulation_does_not_authorize_old_identity(self) -> None:
        with mock.patch.object(process_control, "process_identity", return_value="win32:new"):
            self.assertFalse(process_control.process_is_same(4242, "win32:old"))
            self.assertTrue(process_control.process_is_same(4242, "win32:new"))

    def test_tree_ownership_does_not_depend_on_stdin(self) -> None:
        process = mock.Mock(pid=4242, returncode=0)
        process.communicate.return_value = (b"", b"")
        with (
            mock.patch.object(process_control, "_launch_process", return_value=process) as launch,
            mock.patch.object(process_control, "process_identity", return_value=None),
        ):
            process_control.run_with_tree_timeout(["child"], timeout=1)
        self.assertTrue(launch.call_args.kwargs["owned"])

    def test_access_denied_is_not_treated_as_a_dead_owner(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = 0
        with (
            mock.patch.object(process_control.os, "name", "nt"),
            mock.patch.object(process_control, "_windows_kernel32", return_value=api),
            mock.patch.object(ctypes, "get_last_error", return_value=5),
        ):
            self.assertTrue(process_control.pid_is_alive(4242))

    def test_invalid_pid_error_is_the_only_open_failure_proving_missing(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = 0
        with (
            mock.patch.object(process_control.os, "name", "nt"),
            mock.patch.object(process_control, "_windows_kernel32", return_value=api),
            mock.patch.object(ctypes, "get_last_error", return_value=87),
        ):
            self.assertFalse(process_control.pid_is_alive(4242))

    def test_wait_failure_is_unknown_and_kept_alive(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = 1
        api.WaitForSingleObject.return_value = 0xFFFFFFFF
        api.CloseHandle.return_value = True
        with (
            mock.patch.object(process_control.os, "name", "nt"),
            mock.patch.object(process_control, "_windows_kernel32", return_value=api),
        ):
            self.assertTrue(process_control.pid_is_alive(4242))

    def test_posix_permission_error_is_alive_but_esrch_is_dead(self) -> None:
        with mock.patch.object(process_control.os, "name", "posix"):
            with mock.patch.object(
                process_control.os,
                "kill",
                side_effect=PermissionError(errno.EPERM, "permission denied"),
            ):
                self.assertTrue(process_control.pid_is_alive(4242))
            with mock.patch.object(
                process_control.os,
                "kill",
                side_effect=ProcessLookupError(errno.ESRCH, "missing"),
            ):
                self.assertFalse(process_control.pid_is_alive(4242))


class CleanupFailureTests(unittest.TestCase):
    def test_successful_close_clears_job_ownership_after_query_failure(self) -> None:
        process = mock.Mock(pid=4242)
        process._beyin_job_handle = 7
        api = mock.Mock()
        api.TerminateJobObject.return_value = True
        api.QueryInformationJobObject.return_value = False
        with (
            mock.patch.object(process_control.os, "name", "nt"),
            mock.patch.object(process_control, "_windows_kernel32", return_value=api),
            mock.patch.object(process_control, "_close_windows_handle") as close,
            self.assertRaises(OSError),
        ):
            process_control._release_windows_job(process)
        close.assert_called_once_with(7)
        self.assertIsNone(process.__dict__.get("_beyin_job_handle"))

    def test_tree_cleanup_failure_is_not_reported_as_a_plain_timeout(self) -> None:
        class StuckProcess:
            pid = 4242
            returncode = None

            def communicate(self, timeout: float) -> tuple[None, None]:
                raise subprocess.TimeoutExpired(["child"], timeout)

            def poll(self) -> None:
                return None

            def wait(self, timeout: float) -> None:
                raise subprocess.TimeoutExpired(["child"], timeout)

        process = StuckProcess()
        with (
            mock.patch.object(process_control, "_launch_process", return_value=process),
            mock.patch.object(process_control, "process_identity", return_value="win32:old"),
            mock.patch.object(process_control, "process_is_same", return_value=True),
            mock.patch.object(process_control.subprocess, "run", side_effect=OSError("kill unavailable")),
            mock.patch.object(process_control.os, "name", "nt"),
            self.assertRaises(process_control.ProcessTreeCleanupError) as caught,
        ):
            process_control.run_with_tree_timeout(["child"], timeout=1)
        self.assertIsInstance(caught.exception, process_control.ProcessTreeTimeout)
        self.assertIsInstance(caught.exception.cleanup_error, OSError)

    def test_codex_runner_keeps_cleanup_failure_distinct_from_timeout(self) -> None:
        failure = process_control.ProcessTreeCleanupError(
            ["codex"], 1, 4242, OSError("tree still running")
        )
        with (
            mock.patch.object(codex_runner, "find_codex", return_value="codex.exe"),
            mock.patch.object(codex_runner, "run_with_tree_timeout", side_effect=failure),
        ):
            text, reason = codex_runner.run_exec("prompt", sandbox="read-only", timeout=1)
        self.assertIsNone(text)
        self.assertEqual(reason, "codex-cleanup-error")

    def test_codex_runner_can_propagate_cleanup_failure_to_worker(self) -> None:
        failure = process_control.ProcessTreeCleanupError(
            ["codex"], 1, 4242, OSError("tree still running")
        )
        with (
            mock.patch.object(codex_runner, "find_codex", return_value="codex.exe"),
            mock.patch.object(codex_runner, "run_with_tree_timeout", side_effect=failure),
            self.assertRaises(process_control.ProcessTreeCleanupError),
        ):
            codex_runner.run_exec(
                "prompt",
                sandbox="read-only",
                timeout=1,
                propagate_cleanup_error=True,
            )


class PosixCleanupContractTests(unittest.TestCase):
    def test_group_cleanup_escalates_when_a_grandchild_ignores_sigterm(self) -> None:
        process = mock.Mock(pid=4242)
        process._beyin_process_group = True
        process.wait.side_effect = [subprocess.TimeoutExpired(["child"], 2), None]
        with (
            mock.patch.object(process_control.os, "killpg", create=True) as killpg,
            mock.patch.object(
                process_control,
                "_wait_for_posix_group_empty",
                side_effect=[[4242, 4343], []],
            ),
            mock.patch.object(process_control.time, "sleep"),
        ):
            process_control._terminate_posix_process(process)
        self.assertEqual(killpg.call_args_list, [
            mock.call(4242, process_control.signal.SIGTERM),
            mock.call(4242, getattr(process_control.signal, "SIGKILL", 9)),
        ])

    def test_no_proc_already_gone_group_is_verified_empty_by_killpg(self) -> None:
        process = mock.Mock(pid=4242)
        process._beyin_process_group = True
        with (
            mock.patch.object(process_control.os, "killpg", create=True) as killpg,
            mock.patch.object(process_control.os.path, "isdir", return_value=False),
        ):
            killpg.side_effect = [None, ProcessLookupError(errno.ESRCH, "gone")]
            process_control._terminate_posix_process(process)
        self.assertEqual(killpg.call_args_list, [
            mock.call(4242, process_control.signal.SIGTERM),
            mock.call(4242, 0),
        ])

    def test_no_proc_live_group_remains_unverified_after_bounded_probe(self) -> None:
        with (
            mock.patch.object(process_control.os.path, "isdir", return_value=False),
            mock.patch.object(process_control.os, "killpg", create=True),
            mock.patch.object(process_control.time, "monotonic", side_effect=[0.0, 3.0]),
        ):
            self.assertEqual(process_control._wait_for_posix_group_empty(4242, 2), [4242])

    def test_no_proc_permission_probe_is_cleanup_failure(self) -> None:
        with (
            mock.patch.object(process_control.os.path, "isdir", return_value=False),
            mock.patch.object(
                process_control.os,
                "killpg",
                side_effect=PermissionError(errno.EPERM, "denied"),
                create=True,
            ),
        ):
            with self.assertRaisesRegex(OSError, "posix-process-group-unknown"):
                process_control._wait_for_posix_group_empty(4242, 2)


if __name__ == "__main__":
    unittest.main()
