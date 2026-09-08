from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from contextlib import contextmanager
import unittest
from unittest import mock

import _fixtures  # noqa: F401

import process_control
import worker_supervisor as workers
from file_lock import locked


class SupervisorHandoffTests(unittest.TestCase):
    def test_idle_settlement_releases_supervisor_before_admission_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            receipt_path = state / "worker-supervisor.json"
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "running",
                        "generation": 1,
                        "owner_pid": 123,
                        "lease_until": 200,
                    }
                ),
                encoding="utf-8",
            )
            released = mock.Mock()

            should_exit = workers._settle_supervisor(
                state,
                receipt_path,
                owner_pid=123,
                now=100,
                release_supervisor=released,
            )

        self.assertTrue(should_exit)
        released.assert_called_once_with()


class ClaimFenceTests(unittest.TestCase):
    def _claimed_job(self, root: Path) -> tuple[Path, dict[str, object]]:
        workers.enqueue_job(root, "flush", {"value": "payload"}, start_supervisor=False)
        claimed = workers._claim_next_job(root, now=100)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        return claimed

    def test_child_command_carries_the_claim_token_from_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            running, job = self._claimed_job(state)
            command: list[str] = []

            def launch(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                command.extend(argv)
                return subprocess.CompletedProcess(argv, 1)

            with (
                mock.patch.object(workers, "run_with_tree_timeout", side_effect=launch),
                mock.patch.object(workers, "recover_stale_jobs", return_value=0),
                self.assertRaisesRegex(RuntimeError, "worker-child-result-missing"),
            ):
                workers._run_claimed_job(state, state, running)

        self.assertIn("--claim-token", command)
        self.assertEqual(command[command.index("--claim-token") + 1], job["claim_token"])

    def test_adoption_rejects_a_claim_token_from_an_older_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            running, job = self._claimed_job(state)
            before = running.read_bytes()

            with self.assertRaisesRegex(ValueError, "worker-job-claim-drift"):
                workers._adopt_job(
                    state,
                    running,
                    now=101,
                    expected_claim_token="b" * 32,
                )

            after = running.read_bytes()

        self.assertEqual(after, before)

    def test_execute_job_cli_requires_a_claim_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            running, _job = self._claimed_job(state)

            with self.assertRaisesRegex(ValueError, "worker-claim-token-required"), mock.patch.object(
                workers.sys,
                "argv",
                [
                    "worker_supervisor.py",
                    "--vault",
                    str(root),
                    "--state-dir",
                    str(state),
                    "--execute-job",
                    str(running),
                ],
            ):
                workers.main()


class HookInputSweepTests(unittest.TestCase):
    def test_reference_scan_and_unlink_run_under_the_queue_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            hook_input = state / "hookin-stale.json"
            hook_input.write_text("{}", encoding="utf-8")
            stale = 1_800_000_000 - workers.STALE_HOOK_INPUT_SECONDS - 1
            hook_input.touch()
            import os

            os.utime(hook_input, (stale, stale))
            queue_locked = False
            observed: list[bool] = []
            real_lock = workers.locked
            real_unlink = Path.unlink

            @contextmanager
            def tracking_lock(path: Path, **kwargs: object):
                nonlocal queue_locked
                is_queue = path == state / "worker-queue"
                if is_queue:
                    queue_locked = True
                try:
                    with real_lock(path, **kwargs):
                        yield
                finally:
                    if is_queue:
                        queue_locked = False

            def unlink(path: Path, *args: object, **kwargs: object):
                if path == hook_input:
                    observed.append(queue_locked)
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(workers, "locked", side_effect=tracking_lock), mock.patch.object(
                Path, "unlink", autospec=True, side_effect=unlink
            ):
                workers._sweep_stale_hook_inputs(state, 1_800_000_000)

        self.assertEqual(observed, [True])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink support required")
    def test_stale_hook_input_symlink_outside_state_is_not_unlinked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            candidate = state / "hookin-outside.json"
            state.mkdir()
            candidate.symlink_to(outside)
            workers._sweep_stale_hook_inputs(
                state,
                candidate.lstat().st_mtime + workers.STALE_HOOK_INPUT_SECONDS + 1,
            )
            candidate_exists = candidate.exists()
            outside_exists = outside.exists()

        self.assertTrue(candidate_exists)
        self.assertTrue(outside_exists)

    def test_quarantined_job_payload_keeps_an_unresolved_hook_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            hook_input = state / "hookin-quarantined.json"
            hook_input.write_text("{}", encoding="utf-8")
            stale = 1_800_000_000 - workers.STALE_HOOK_INPUT_SECONDS - 1
            import os

            os.utime(hook_input, (stale, stale))
            queued = workers.enqueue_job(
                state,
                "flush",
                {"hook_input": str(hook_input)},
                start_supervisor=False,
            )
            invalid = json.loads(queued.read_text(encoding="utf-8"))
            invalid["kind"] = "unknown"
            queued.write_text(json.dumps(invalid), encoding="utf-8")
            workers._claim_next_job(state, now=100)

            workers._sweep_stale_hook_inputs(state, 1_800_000_000)
            preserved = hook_input.exists()

        self.assertTrue(preserved)


class QueueAdmissionTests(unittest.TestCase):
    @staticmethod
    def _hook_input(state: Path, name: str, session: str, transcript: Path) -> Path:
        path = state / f"hookin-{name}.json"
        path.write_text(
            json.dumps({"session_id": session, "transcript_path": str(transcript)}),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _job_payload(hook_input: Path, reason: str, **extra: object) -> dict[str, object]:
        return {
            "hook_input": str(hook_input),
            "reason": reason,
            "event_iso": "2026-09-08T12:00:00+03:00",
            **extra,
        }

    def test_same_session_pending_flush_is_updated_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "transcript.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            first_input = self._hook_input(state, "first", "session", transcript)
            second_input = self._hook_input(state, "second", "session", transcript)
            first = workers.enqueue_job(
                state,
                "flush",
                self._job_payload(first_input, "turnend"),
                start_supervisor=False,
            )
            second = workers.enqueue_job(
                state,
                "flush",
                self._job_payload(
                    second_input,
                    "sessionend",
                    continuation=True,
                    continuation_reason="tail",
                ),
                start_supervisor=False,
            )
            pending = list((state / "worker-jobs" / "pending").glob("*.json"))
            job = json.loads(pending[0].read_text(encoding="utf-8"))

        self.assertEqual(second, first)
        self.assertEqual(len(pending), 1)
        self.assertEqual(job["payload"]["reason"], "sessionend")
        self.assertTrue(job["payload"]["continuation"])
        self.assertEqual(job["payload"]["continuation_reason"], "tail")

    def test_new_flush_does_not_mutate_a_working_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "transcript.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            first_input = self._hook_input(state, "first", "session", transcript)
            second_input = self._hook_input(state, "second", "session", transcript)
            workers.enqueue_job(
                state,
                "flush",
                self._job_payload(first_input, "turnend"),
                start_supervisor=False,
            )
            running, original = workers._claim_next_job(state, now=100)
            workers.enqueue_job(
                state,
                "flush",
                self._job_payload(second_input, "turnend"),
                start_supervisor=False,
            )
            running_after = json.loads(running.read_text(encoding="utf-8"))
            pending_count = len(list((state / "worker-jobs" / "pending").glob("*.json")))

        self.assertEqual(running_after, original)
        self.assertEqual(pending_count, 1)

    def test_queue_capacity_includes_unresolved_jobs_and_preserves_rejected_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            payloads = []
            with mock.patch.object(workers, "MAX_UNRESOLVED_JOBS", 2), mock.patch.object(
                workers, "ensure_supervisor"
            ):
                for index in range(3):
                    transcript = state / f"transcript-{index}.jsonl"
                    transcript.write_text("{}", encoding="utf-8")
                    payloads.append(
                        {"session_id": f"session-{index}", "transcript_path": str(transcript)}
                    )
                workers.enqueue_flush(state, payloads[0], "turnend", vault_root=state)
                workers.enqueue_flush(state, payloads[1], "turnend", vault_root=state)
                with self.assertRaisesRegex(RuntimeError, "worker-queue-backpressure"):
                    workers.enqueue_flush(state, payloads[2], "turnend", vault_root=state)
            inputs = list(state.glob("hookin-*.json"))
            pending_count = len(list((state / "worker-jobs" / "pending").glob("*.json")))

        self.assertEqual(len(inputs), 3)
        self.assertEqual(pending_count, 2)

    def test_capacity_counts_dead_letter_and_quarantine_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(workers, "MAX_UNRESOLVED_JOBS", 1):
                queued = workers.enqueue_job(
                    state,
                    "flush",
                    {"value": "first"},
                    start_supervisor=False,
                )
                running, job = workers._claim_next_job(state, now=100)
                workers._finish_job(
                    state,
                    running,
                    job,
                    status="dead-letter",
                    error="unrecoverable",
                    now=100,
                )
                with self.assertRaisesRegex(RuntimeError, "worker-queue-backpressure"):
                    workers.enqueue_job(
                        state,
                        "flush",
                        {"value": "second"},
                        start_supervisor=False,
                    )
            dead = list((state / "worker-jobs" / "dead-letter").glob("*.json"))

        self.assertEqual(len(dead), 1)

    def test_thousand_same_scope_events_leave_one_pending_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "transcript.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            payload = {"session_id": "burst", "transcript_path": str(transcript)}
            with mock.patch.object(workers, "ensure_supervisor"):
                for _index in range(1000):
                    workers.enqueue_flush(state, payload, "turnend", vault_root=state)
            pending = list((state / "worker-jobs" / "pending").glob("*.json"))
            inputs = list(state.glob("hookin-*.json"))

        self.assertEqual(len(pending), 1)
        self.assertEqual(len(inputs), 1)


class TerminalRetentionTests(unittest.TestCase):
    def test_succeeded_receipts_are_bounded_by_count_and_age(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_ids: list[str] = []
            with mock.patch.object(workers, "MAX_SUCCEEDED_RECEIPTS", 2), mock.patch.object(
                workers, "SUCCEEDED_RECEIPT_MAX_AGE_SECONDS", 10
            ):
                for index in range(4):
                    workers.enqueue_job(
                        state,
                        "flush",
                        {"value": index},
                        start_supervisor=False,
                    )
                    running, job = workers._claim_next_job(state, now=100 + index)
                    workers._finish_job(
                        state,
                        running,
                        job,
                        status="succeeded",
                        now=100 + index,
                    )
                    job_ids.append(job["job_id"])
                bounded = list((state / "worker-jobs" / "succeeded").glob("*.json"))
                bounded_ids = [json.loads(path.read_text(encoding="utf-8"))["job_id"] for path in bounded]
                workers.prune_succeeded_jobs(state, now=114)
            receipts = list((state / "worker-jobs" / "succeeded").glob("*.json"))

        self.assertEqual(len(bounded), 2)
        self.assertEqual(set(bounded_ids), set(job_ids[-2:]))
        self.assertEqual(len(receipts), 0)

    def test_recovered_successor_receipt_is_pinned_during_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(workers, "MAX_SUCCEEDED_RECEIPTS", 1), mock.patch.object(
                workers, "SUCCEEDED_RECEIPT_MAX_AGE_SECONDS", 1
            ):
                workers.enqueue_job(
                    state,
                    "flush",
                    {"value": "successor"},
                    start_supervisor=False,
                )
                succeeded_path, succeeded = workers._claim_next_job(state, now=100)
                workers._finish_job(
                    state,
                    succeeded_path,
                    succeeded,
                    status="succeeded",
                    now=100,
                )
                workers.enqueue_job(
                    state,
                    "flush",
                    {"value": "recovered"},
                    start_supervisor=False,
                )
                dead_path, dead = workers._claim_next_job(state, now=100)
                workers._finish_job(
                    state,
                    dead_path,
                    dead,
                    status="dead-letter",
                    error="recovered",
                    terminal_reason="recovered-by-successor",
                    now=100,
                )
                dead_path = state / "worker-jobs" / "dead-letter" / dead_path.name
                dead_receipt = json.loads(dead_path.read_text(encoding="utf-8"))
                dead_receipt["recovery_job_id"] = succeeded["job_id"]
                dead_receipt["kind"] = succeeded["kind"]
                dead_receipt["payload"] = succeeded["payload"]
                workers.atomic_write_json(dead_path, dead_receipt)
                with mock.patch.object(workers, "MAX_SUCCEEDED_RECEIPTS", 0):
                    workers.prune_succeeded_jobs(state, now=200)
            retained = state / "worker-jobs" / "succeeded" / f"job-{succeeded['job_id']}.json"
            retained_exists = retained.exists()
            dead_exists = dead_path.exists()

        self.assertTrue(retained_exists)
        self.assertTrue(dead_exists)

    def test_child_dispatch_does_not_create_per_job_lock_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(
                state,
                "flush",
                {"value": "payload"},
                start_supervisor=False,
            )
            running, _job = workers._claim_next_job(state, now=100)
            with (
                mock.patch.object(
                    workers,
                    "run_with_tree_timeout",
                    return_value=subprocess.CompletedProcess([], 1),
                ),
                mock.patch.object(workers, "recover_stale_jobs", return_value=0),
                self.assertRaisesRegex(RuntimeError, "worker-child-result-missing"),
            ):
                workers._run_claimed_job(state, state, running)
            sidecars = list(state.glob(f"worker-child-*.lock"))

        self.assertEqual(sidecars, [])


class ProcessRecoveryFenceTests(unittest.TestCase):
    def test_live_owner_with_unknown_birth_identity_is_not_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(state, "flush", {"value": "payload"}, start_supervisor=False)
            running, job = workers._claim_next_job(state, now=100)
            job["owner_identity"] = "win32:old"
            job["lease_until"] = 1
            workers.atomic_write_json(running, job)
            with (
                mock.patch.object(workers, "pid_is_alive", return_value=True),
                mock.patch.object(process_control, "process_identity", create=True, return_value=None),
            ):
                recovered = workers.recover_stale_jobs(state, now=200)
            running_exists = running.exists()

        self.assertEqual(recovered, 0)
        self.assertTrue(running_exists)

    def test_reused_pid_with_a_different_birth_identity_can_be_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(state, "flush", {"value": "payload"}, start_supervisor=False)
            running, job = workers._claim_next_job(state, now=100)
            job["owner_identity"] = "win32:old"
            job["lease_until"] = 1
            workers.atomic_write_json(running, job)
            with (
                mock.patch.object(workers, "pid_is_alive", return_value=True),
                mock.patch.object(process_control, "process_identity", create=True, return_value="win32:new"),
                mock.patch.object(process_control, "process_is_same", create=True, return_value=False),
            ):
                recovered = workers.recover_stale_jobs(state, now=200)
            pending = state / "worker-jobs" / "pending" / running.name
            pending_exists = pending.exists()

        self.assertEqual(recovered, 1)
        self.assertTrue(pending_exists)

    def test_owner_identity_is_cleared_when_adoption_changes_the_owner_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(state, "flush", {"value": "payload"}, start_supervisor=False)
            running, job = workers._claim_next_job(state, now=100)
            job["owner_pid"] = 1234
            job["owner_identity"] = "win32:supervisor"
            workers.atomic_write_json(running, job)
            with mock.patch.object(workers, "_process_owner_identity", return_value=None):
                adopted = workers._adopt_job(
                    state,
                    running,
                    now=101,
                    expected_claim_token=job["claim_token"],
                )
            with (
                mock.patch.object(workers, "pid_is_alive", return_value=True),
                mock.patch.object(process_control, "process_identity", create=True, return_value=None),
            ):
                recovered = workers.recover_stale_jobs(state, now=200)
            running_exists = running.exists()

        self.assertNotIn("owner_identity", adopted)
        self.assertEqual(recovered, 0)
        self.assertTrue(running_exists)

    def test_recovery_reads_birth_identity_once_before_comparing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(state, "flush", {"value": "payload"}, start_supervisor=False)
            running, job = workers._claim_next_job(state, now=100)
            job["owner_identity"] = "win32:old"
            workers.atomic_write_json(running, job)
            with (
                mock.patch.object(workers, "pid_is_alive", return_value=True),
                mock.patch.object(
                    process_control,
                    "process_identity",
                    create=True,
                    side_effect=["win32:new", "must-not-read-twice"],
                ) as identity,
            ):
                active = workers._process_owner_is_active(job)

        self.assertFalse(active)
        self.assertEqual(identity.call_count, 1)

    def test_coalescing_merges_continuation_from_every_matching_pending_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "transcript.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            first_input = QueueAdmissionTests._hook_input(state, "first", "session", transcript)
            stronger_input = QueueAdmissionTests._hook_input(state, "stronger", "session", transcript)
            incoming_input = QueueAdmissionTests._hook_input(state, "incoming", "session", transcript)
            first = workers.enqueue_job(
                state,
                "flush",
                QueueAdmissionTests._job_payload(first_input, "turnend", continuation="first"),
                start_supervisor=False,
            )
            duplicate = json.loads(first.read_text(encoding="utf-8"))
            duplicate["job_id"] = "b" * 32
            duplicate["enqueue_sequence"] = 2
            duplicate["payload"] = QueueAdmissionTests._job_payload(
                stronger_input,
                "sessionend",
                continuation="closing",
                coverage={"end": 42},
            )
            duplicate_path = state / "worker-jobs" / "pending" / f"job-{'b' * 32}.json"
            workers.atomic_write_json(duplicate_path, duplicate)

            retained = workers.enqueue_job(
                state,
                "flush",
                QueueAdmissionTests._job_payload(incoming_input, "turnend", continuation="latest"),
                start_supervisor=False,
            )
            pending = list((state / "worker-jobs" / "pending").glob("*.json"))
            merged = json.loads(pending[0].read_text(encoding="utf-8"))

        self.assertEqual(retained, first)
        self.assertEqual(len(pending), 1)
        self.assertEqual(merged["payload"]["reason"], "sessionend")
        self.assertEqual(merged["payload"]["continuation"], "latest")
        self.assertEqual(merged["payload"]["coverage"], {"end": 42})

    def test_startup_running_receipt_is_published_under_admission_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            receipt_path = state / "worker-supervisor.json"
            admission_locked = False
            running_writes: list[bool] = []
            real_lock = workers.locked
            real_write = workers.atomic_write_json

            @contextmanager
            def tracking_lock(path: Path, **kwargs: object):
                nonlocal admission_locked
                is_admission = path == state / "worker-admission"
                if is_admission:
                    admission_locked = True
                try:
                    with real_lock(path, **kwargs):
                        yield
                finally:
                    if is_admission:
                        admission_locked = False

            def tracking_write(path: Path, value: object, *args: object, **kwargs: object):
                if path == receipt_path and isinstance(value, dict) and value.get("status") == "running":
                    running_writes.append(admission_locked)
                return real_write(path, value, *args, **kwargs)

            with mock.patch.object(workers, "locked", side_effect=tracking_lock), mock.patch.object(
                workers, "atomic_write_json", side_effect=tracking_write
            ):
                workers.run_supervisor(state, state, now=lambda: 100)

        self.assertEqual(running_writes, [True])

    def test_old_token_starter_cannot_race_new_starter_into_stranded_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(
                state,
                "flush",
                {"value": "pending"},
                start_supervisor=False,
                now=100,
            )
            receipt_path = state / "worker-supervisor.json"
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "launching",
                        "generation": 1,
                        "launch_token": "a" * 32,
                        "owner_pid": 0,
                        "lease_until": 100,
                        "updated_ts": 100,
                    }
                ),
                encoding="utf-8",
            )
            lifetime_entered = threading.Event()
            admission_requested = threading.Event()
            release_lifetime = threading.Event()
            release_job = threading.Event()
            real_lock = workers.locked
            real_launches: list[list[str]] = []
            old_error: list[BaseException] = []
            new_result: list[bool] = []

            @contextmanager
            def hold_lifetime(path: Path, **kwargs: object):
                if path == state / "worker-admission" and lifetime_entered.is_set():
                    admission_requested.set()
                with real_lock(path, **kwargs):
                    if path == state / "worker-supervisor":
                        lifetime_entered.set()
                        self.assertTrue(release_lifetime.wait(5))
                    yield

            def launch(command: list[str], **_kwargs: object) -> object:
                real_launches.append(command)
                return object()

            def finish(_vault: Path, _state: Path, path: Path) -> None:
                job = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(release_job.wait(5))
                workers._finish_job(state, path, job, status="succeeded", now=200)

            def start_old() -> None:
                try:
                    workers.run_supervisor(
                        state,
                        state,
                        launch_token="a" * 32,
                        now=lambda: 200,
                        job_runner=finish,
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    old_error.append(exc)

            with mock.patch.object(workers, "locked", side_effect=hold_lifetime):
                old = threading.Thread(target=start_old)
                old.start()
                self.assertTrue(lifetime_entered.wait(5))
                new = threading.Thread(
                    target=lambda: new_result.append(
                        workers.ensure_supervisor(
                            state,
                            vault_root=state,
                            launcher=launch,
                            now=200,
                        )
                    )
                )
                new.start()
                self.assertTrue(admission_requested.wait(5))
                release_lifetime.set()
                new.join(5)
                release_job.set()
                old.join(5)
                pending = list((state / "worker-jobs" / "pending").glob("*.json"))

        self.assertFalse(old.is_alive())
        self.assertFalse(new.is_alive())
        self.assertEqual(old_error, [])
        self.assertEqual(new_result, [False])
        self.assertEqual(real_launches, [])
        self.assertEqual(pending, [])

    def test_rejected_old_token_does_not_take_lifetime_from_correct_starter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(
                state,
                "flush",
                {"value": "pending"},
                start_supervisor=False,
                now=100,
            )
            receipt_path = state / "worker-supervisor.json"
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "launching",
                        "generation": 1,
                        "launch_token": "b" * 32,
                        "owner_pid": 0,
                        "lease_until": 100,
                        "updated_ts": 100,
                    }
                ),
                encoding="utf-8",
            )
            lifetime_acquisitions: list[int] = []
            real_lock = workers.locked
            old_result: list[int] = []
            correct_result: list[int] = []

            @contextmanager
            def track_lifetime(path: Path, **kwargs: object):
                with real_lock(path, **kwargs):
                    if path == state / "worker-supervisor":
                        lifetime_acquisitions.append(os.getpid())
                    yield

            def finish(_vault: Path, _state: Path, path: Path) -> None:
                job = json.loads(path.read_text(encoding="utf-8"))
                workers._finish_job(state, path, job, status="succeeded", now=200)

            with mock.patch.object(workers, "locked", side_effect=track_lifetime):
                old = threading.Thread(
                    target=lambda: old_result.append(
                        workers.run_supervisor(
                            state,
                            state,
                            launch_token="a" * 32,
                            now=lambda: 200,
                            job_runner=finish,
                        )
                    )
                )
                old.start()
                old.join(5)
                correct = threading.Thread(
                    target=lambda: correct_result.append(
                        workers.run_supervisor(
                            state,
                            state,
                            launch_token="b" * 32,
                            now=lambda: 200,
                            job_runner=finish,
                        )
                    )
                )
                correct.start()
                correct.join(5)
                pending = list((state / "worker-jobs" / "pending").glob("*.json"))

        self.assertFalse(old.is_alive())
        self.assertFalse(correct.is_alive())
        self.assertEqual(old_result, [0])
        self.assertEqual(correct_result, [0])
        self.assertEqual(len(lifetime_acquisitions), 1)
        self.assertEqual(pending, [])

    def test_startup_failure_releases_lifetime_before_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            receipt_path = state / "worker-supervisor.json"
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "launching",
                        "generation": 1,
                        "launch_token": "a" * 32,
                        "owner_pid": 0,
                        "lease_until": 100,
                        "updated_ts": 100,
                    }
                ),
                encoding="utf-8",
            )
            events: list[str] = []
            write_started = threading.Event()
            real_lock = workers.locked
            real_write = workers.atomic_write_json
            old_error: list[BaseException] = []
            new_result: list[bool] = []

            @contextmanager
            def track_locks(path: Path, **kwargs: object):
                name = (
                    "admission"
                    if path == state / "worker-admission"
                    else "lifetime"
                    if path == state / "worker-supervisor"
                    else "other"
                )
                try:
                    with real_lock(path, **kwargs):
                        if name != "other":
                            events.append(name + "_enter")
                        yield
                finally:
                    if name != "other":
                        events.append(name + "_exit")

            def fail_running_receipt(path: Path, value: object, *args: object, **kwargs: object):
                if path == receipt_path and isinstance(value, dict) and value.get("status") == "running":
                    write_started.set()
                    raise OSError("injected-startup-write-failure")
                return real_write(path, value, *args, **kwargs)

            def launch(_command: list[str], **_kwargs: object) -> object:
                return object()

            def start_old() -> None:
                try:
                    workers.run_supervisor(
                        state,
                        state,
                        launch_token="a" * 32,
                        now=lambda: 200,
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    old_error.append(exc)

            with (
                mock.patch.object(workers, "locked", side_effect=track_locks),
                mock.patch.object(workers, "atomic_write_json", side_effect=fail_running_receipt),
            ):
                old = threading.Thread(target=start_old)
                old.start()
                self.assertTrue(write_started.wait(5))
                new = threading.Thread(
                    target=lambda: new_result.append(
                        workers.ensure_supervisor(
                            state,
                            vault_root=state,
                            launcher=launch,
                            now=200,
                        )
                    )
                )
                new.start()
                old.join(5)
                new.join(5)

        self.assertFalse(old.is_alive())
        self.assertFalse(new.is_alive())
        self.assertEqual(len(old_error), 1)
        self.assertEqual(new_result, [True])
        self.assertLess(events.index("lifetime_exit"), events.index("admission_exit"))

    def test_owner_receipt_drift_still_releases_the_supervisor_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            receipt_path = state / "worker-supervisor.json"
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "running",
                        "generation": 1,
                        "owner_pid": 123,
                        "lease_until": 200,
                    }
                ),
                encoding="utf-8",
            )
            released = mock.Mock()
            result = workers._settle_supervisor(
                state,
                receipt_path,
                owner_pid=456,
                now=100,
                release_supervisor=released,
            )

        self.assertTrue(result)
        released.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
