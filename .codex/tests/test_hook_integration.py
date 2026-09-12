"""SessionStart deadline and read-only scope integration boundaries."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _fixtures import SCRIPTS_DIR
import flush
import hook
import memory_ledger
import worker_supervisor as workers


def _queue_report(*, pending: int = 0) -> dict[str, object]:
    return {
        "counts": {
            "pending": pending,
            "claimed": 0,
            "running": 0,
            "succeeded": 0,
            "dead-letter": 0,
            "quarantined": 0,
        },
        "invalid": 0,
        "orphan_hook_inputs": 0,
        "terminal": {"recovered": 0, "unresolved": 0},
    }


class HookIntegrationTests(unittest.TestCase):
    def test_session_start_propagates_one_deadline_to_context_queue_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            deadline = time.monotonic() + 30
            reports = [_queue_report(), _queue_report(pending=1)]
            context = "[Hafıza: Kimlik]\nCevo"
            payload = {"session_id": "deadline-session", "cwd": str(vault)}
            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                mock.patch.object(hook, "build_session_context", return_value=context) as build,
                mock.patch.object(flush, "maybe_trigger_compile", return_value=False) as trigger,
                mock.patch.object(hook, "inspect_worker_queue", side_effect=reports) as inspect,
                mock.patch.object(hook, "ensure_supervisor") as ensure,
                mock.patch.object(hook, "_emit_context"),
                mock.patch.object(hook, "record_hook_runtime") as runtime,
                mock.patch.object(hook, "clear_hook_health") as health,
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                result = hook.main(["session-start", "--strict"])

        self.assertEqual(result, 0)
        build.assert_called_once_with(
            vault,
            state,
            write_views=True,
            deadline=deadline,
        )
        trigger.assert_called_once_with(vault, deadline=deadline)
        self.assertEqual(inspect.call_args_list, [
            mock.call(state / "maintenance", deadline=deadline),
            mock.call(state, deadline=deadline),
        ])
        ensure.assert_called_once_with(state, vault_root=vault, deadline=deadline)
        self.assertEqual(runtime.call_args.kwargs["deadline"], deadline)
        self.assertEqual(health.call_args.kwargs["deadline"], deadline)

    def test_session_start_read_only_snapshot_skips_all_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            memory_ledger.mark_read_only_turn(state, "audit")
            deadline = time.monotonic() + 30
            payload = {"session_id": "audit", "cwd": str(vault)}
            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                mock.patch.object(hook, "build_session_context", return_value="ctx") as build,
                mock.patch.object(flush, "maybe_trigger_compile") as trigger,
                mock.patch.object(hook, "inspect_worker_queue") as inspect,
                mock.patch.object(hook, "ensure_supervisor") as ensure,
                mock.patch.object(hook, "_emit_context"),
                mock.patch.object(hook, "record_hook_runtime"),
                mock.patch.object(hook, "clear_hook_health"),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                result = hook.main(["session-start", "--strict"])

        self.assertEqual(result, 0)
        build.assert_called_once_with(
            vault,
            state,
            write_views=False,
            deadline=deadline,
        )
        trigger.assert_not_called()
        inspect.assert_not_called()
        ensure.assert_not_called()

    def test_session_start_scope_lock_contention_emits_safe_context_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            session_id = "scope-contention"
            memory_ledger.mark_read_only_turn(state, session_id)
            marker = memory_ledger._read_only_path(state, session_id)
            ready = state / "scope-lock-ready"
            holder_script = (
                "import sys,time;"
                "from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);"
                "from file_lock import locked;"
                "resource=Path(sys.argv[2]);ready=Path(sys.argv[3]);"
                "guard=locked(resource);guard.__enter__();"
                "ready.write_text('ready', encoding='ascii');"
                "time.sleep(float(sys.argv[4]));"
                "guard.__exit__(None,None,None)"
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_script,
                    str(SCRIPTS_DIR),
                    str(marker),
                    str(ready),
                    "1.0",
                ],
                cwd=vault,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                limit = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < limit:
                    time.sleep(0.01)
                if not ready.exists():
                    raise AssertionError("scope lock holder did not start")
                output = io.StringIO()
                payload = {"session_id": session_id, "cwd": str(vault)}
                with (
                    mock.patch.object(hook, "VAULT_ROOT", vault),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "_hook_deadline", return_value=time.monotonic() + 0.2),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                ):
                    result = hook.main(["session-start", "--strict"])
            finally:
                child.wait(timeout=5)

            emitted = json.loads(output.getvalue())

        self.assertEqual(result, 1)
        context = emitted["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bağlamı güvenli biçimde doğrulanamadı", context)
        self.assertIn("Ham notlara veya eski önbelleğe geçme", context)

    def test_session_start_receipt_failure_does_not_emit_second_context_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            deadline = time.monotonic() + 30
            payload = {"session_id": "receipt-failure", "cwd": str(vault)}
            output = io.StringIO()
            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                mock.patch.object(hook, "build_session_context", return_value="ctx"),
                mock.patch.object(flush, "maybe_trigger_compile", return_value=False),
                mock.patch.object(hook, "inspect_worker_queue", side_effect=[_queue_report(), _queue_report()]),
                mock.patch.object(
                    hook,
                    "record_hook_runtime",
                    side_effect=hook.LockUnavailable("receipt-busy"),
                ),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", output),
            ):
                result = hook.main(["session-start", "--strict"])

        lines = output.getvalue().splitlines()
        self.assertEqual(result, 1)
        self.assertEqual(len(lines), 1)
        emitted = json.loads(lines[0])
        self.assertEqual(
            emitted["hookSpecificOutput"]["hookEventName"],
            "SessionStart",
        )
        self.assertEqual(
            emitted["hookSpecificOutput"]["additionalContext"],
            "ctx",
        )

    def test_session_start_pre_emit_failures_emit_one_safe_context_warning(self) -> None:
        failures = (
            workers.LockUnavailable("scope-busy"),
            OSError("context-unavailable"),
            memory_ledger.MemoryPreferenceError("memory-preferences-changed"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with tempfile.TemporaryDirectory() as temporary:
                    vault = Path(temporary)
                    state = vault / ".codex/scripts/.state"
                    deadline = time.monotonic() + 30
                    payload = {"session_id": "pre-emit-failure", "cwd": str(vault)}
                    output = io.StringIO()
                    with (
                        mock.patch.object(hook, "VAULT_ROOT", vault),
                        mock.patch.object(hook, "STATE_DIR", state),
                        mock.patch.object(hook, "_validate_hook_scope"),
                        mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                        mock.patch.object(hook, "build_session_context", side_effect=failure),
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                        mock.patch.object(sys, "stdout", output),
                    ):
                        result = hook.main(["session-start", "--strict"])

                lines = output.getvalue().splitlines()
                self.assertEqual(result, 1)
                self.assertEqual(len(lines), 1)
                context = json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]
                self.assertIn("bağlamı güvenli biçimde doğrulanamadı", context)
                self.assertIn("Ham notlara veya eski önbelleğe geçme", context)

    def test_session_start_holds_scope_lock_until_maintenance_admission(self) -> None:
        """A marker completing after the snapshot must wait for SessionStart writes."""
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            attempt = state / "marker-attempted"
            done = state / "marker-completed"
            snapshot = state / "snapshot-taken"
            session_id = "racing-session"
            child = None
            observed: list[bool] = []
            marker_script = (
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from memory_ledger import mark_read_only_turn\n"
                "state = Path(sys.argv[2])\n"
                "Path(sys.argv[3]).write_text('1', encoding='ascii')\n"
                "mark_read_only_turn(state, sys.argv[5])\n"
                "Path(sys.argv[4]).write_text('1', encoding='ascii')\n"
            )

            def build(_vault: Path, _state: Path, **_kwargs: object) -> str:
                nonlocal child
                _state.mkdir(parents=True, exist_ok=True)
                snapshot.write_text("1", encoding="ascii")
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        marker_script,
                        str(SCRIPTS_DIR),
                        str(state),
                        str(attempt),
                        str(done),
                        session_id,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                limit = time.monotonic() + 5
                while not attempt.exists() and time.monotonic() < limit:
                    time.sleep(0.01)
                if not attempt.exists():
                    raise AssertionError("marker writer did not reach the snapshot boundary")
                time.sleep(0.15)
                return "ctx"

            deadline = time.monotonic() + 30
            payload = {"session_id": session_id, "cwd": str(vault)}
            snapshot_exists = False
            done_exists = False
            try:
                with (
                    mock.patch.object(hook, "VAULT_ROOT", vault),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                    mock.patch.object(hook, "build_session_context", side_effect=build),
                    mock.patch.object(
                        flush,
                        "maybe_trigger_compile",
                        side_effect=lambda *_args, **_kwargs: observed.append(done.exists()) or False,
                    ),
                    mock.patch.object(hook, "inspect_worker_queue", return_value=_queue_report()),
                    mock.patch.object(hook, "_emit_context"),
                    mock.patch.object(hook, "record_hook_runtime"),
                    mock.patch.object(hook, "clear_hook_health"),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", io.StringIO()),
                ):
                    result = hook.main(["session-start", "--strict"])
            finally:
                if child is not None:
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                snapshot_exists = snapshot.is_file()
                done_exists = done.is_file()

        self.assertEqual(result, 0)
        self.assertEqual(observed, [False])
        self.assertTrue(snapshot_exists)
        self.assertTrue(done_exists)

    def test_final_receipts_bound_locks_and_persist_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            deadline = time.monotonic() + 30
            seen: list[tuple[Path, float | None]] = []
            real_locked = hook.locked

            @contextmanager
            def capture(path: Path, **kwargs: object):
                seen.append((path, kwargs.get("timeout")))
                with real_locked(path, **kwargs):
                    yield

            payload = {"session_id": "receipt-session", "cwd": "synthetic"}
            context = "[Hafıza: Kimlik]\nCevo"
            with mock.patch.object(hook, "locked", side_effect=capture):
                hook.record_hook_runtime(
                    "session-start",
                    payload,
                    state,
                    now=1234,
                    context=context,
                    deadline=deadline,
                )
                hook.write_hook_health(
                    state,
                    payload,
                    status="error",
                    error="synthetic-failure",
                    deadline=deadline,
                )

            runtime = json.loads((state / "runtime-session-start.json").read_text(encoding="utf-8"))
            health = json.loads((state / "hook-health.json").read_text(encoding="utf-8"))

        self.assertEqual(runtime["outcome"], "emitted")
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["error"], "synthetic-failure")
        self.assertGreaterEqual(len(seen), 4)
        self.assertTrue(all(timeout is not None for _path, timeout in seen))
        self.assertTrue(all(0 <= timeout <= 30 for _path, timeout in seen if timeout is not None))

    def test_maintenance_and_queue_admission_use_the_shared_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            deadline = time.monotonic() + 30
            seen: list[tuple[Path, float | None]] = []

            @contextmanager
            def capture(path: Path, **kwargs: object):
                seen.append((path, kwargs.get("timeout")))
                yield

            with (
                mock.patch.object(workers, "locked", side_effect=capture),
                mock.patch.object(workers, "enqueue_job", return_value=state / "job.json") as enqueue,
                mock.patch.object(workers, "ensure_supervisor") as ensure,
            ):
                result = workers.enqueue_maintenance(
                    state,
                    vault_root=root,
                    deadline=deadline,
                )

        lane = state / "maintenance"
        self.assertEqual(result, state / "job.json")
        self.assertEqual(enqueue.call_args.args[:3], (lane, "maintenance", {}))
        self.assertEqual(enqueue.call_args.kwargs["deadline"], deadline)
        ensure.assert_called_once_with(
            lane,
            vault_root=root,
            launcher=mock.ANY,
            deadline=deadline,
        )
        self.assertEqual([path for path, _timeout in seen], [
            lane / "maintenance-admission",
            lane / "worker-queue",
        ])
        self.assertTrue(all(timeout is not None for _path, timeout in seen))

    def test_enqueue_transport_sequence_and_job_writes_share_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            transcript = root / "transcript.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            deadline = time.monotonic() + 30
            seen: list[tuple[Path, float | None]] = []
            real_atomic_write_json = workers.atomic_write_json

            def capture(path: Path, payload: object, **kwargs: object) -> None:
                seen.append((path, kwargs.get("deadline")))
                real_atomic_write_json(path, payload, **kwargs)

            with (
                mock.patch.object(workers, "atomic_write_json", side_effect=capture),
                mock.patch.object(workers, "ensure_supervisor"),
            ):
                result = workers.enqueue_flush(
                    state,
                    {"session_id": "enqueue-deadline", "transcript_path": str(transcript)},
                    "turnend",
                    vault_root=root,
                    deadline=deadline,
                )

        self.assertEqual(result.parent.name, "pending")
        self.assertGreaterEqual(len(seen), 3)
        self.assertTrue(all(value == deadline for _path, value in seen))

    def test_supervisor_launch_failure_receipt_writes_share_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            deadline = time.monotonic() + 30
            seen: list[tuple[str | None, float | None]] = []
            real_atomic_write_json = workers.atomic_write_json

            def capture(path: Path, payload: object, **kwargs: object) -> None:
                if path == state / "worker-supervisor.json":
                    seen.append((payload.get("status") if isinstance(payload, dict) else None,
                                 kwargs.get("deadline")))
                real_atomic_write_json(path, payload, **kwargs)

            with (
                mock.patch.object(workers, "atomic_write_json", side_effect=capture),
                mock.patch.object(
                    workers,
                    "spawn_detached",
                    side_effect=OSError("synthetic-launch-failure"),
                ),
            ):
                with self.assertRaises(OSError):
                    workers.ensure_supervisor(
                        state,
                        vault_root=root,
                        deadline=deadline,
                    )

        self.assertEqual([status for status, _value in seen], ["launching", "failed"])
        self.assertTrue(all(value == deadline for _status, value in seen))

    def test_maybe_trigger_compile_passes_deadline_to_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deadline = time.monotonic() + 30
            launcher = mock.Mock()
            with (
                mock.patch.object(flush.compile_state, "load_publication", return_value=None),
                mock.patch.object(flush.compile_state, "has_changes", return_value=True),
                mock.patch.object(workers, "enqueue_maintenance") as enqueue,
            ):
                result = flush.maybe_trigger_compile(
                    root,
                    popen_factory=launcher,
                    deadline=deadline,
                )

        self.assertTrue(result)
        enqueue.assert_called_once_with(
            root / ".codex/scripts/.state",
            vault_root=root,
            launcher=launcher,
            deadline=deadline,
        )

    def test_queue_inspection_bounds_its_read_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            deadline = time.monotonic() + 30
            seen: list[tuple[Path, float | None]] = []

            @contextmanager
            def capture(path: Path, **kwargs: object):
                seen.append((path, kwargs.get("timeout")))
                yield

            with mock.patch.object(workers, "locked", side_effect=capture):
                report = workers.inspect_worker_queue(state, deadline=deadline)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(seen[0][0], state / "worker-queue")
        self.assertIsNotNone(seen[0][1])


if __name__ == "__main__":
    unittest.main()
