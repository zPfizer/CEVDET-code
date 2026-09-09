import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _fixtures
import flush
import worker_supervisor as workers


class WorkerHandoffTests(unittest.TestCase):
    @staticmethod
    def _hold_lock_in_process(state: Path, resource: Path, seconds: float) -> subprocess.Popen[bytes]:
        ready = state / f"ready-{resource.name}"
        script = (
            "import sys,time;"
            "from pathlib import Path;"
            "sys.path.insert(0,sys.argv[1]);"
            "from file_lock import locked;"
            "resource=Path(sys.argv[2]);ready=Path(sys.argv[3]);"
            "guard=locked(resource);guard.__enter__();ready.write_text('ready');"
            "time.sleep(float(sys.argv[4]));guard.__exit__(None,None,None)"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(_fixtures.SCRIPTS_DIR),
                str(resource),
                str(ready),
                str(seconds),
            ],
            cwd=state,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            process.kill()
            process.wait(timeout=5)
            raise AssertionError(f"lock holder did not start: {process.returncode}")
        return process

    def test_archive_move_between_dispatch_check_and_read_is_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            codex_home = vault / 'codex'
            sessions, archive = codex_home / 'sessions', codex_home / 'archived_sessions'
            sessions.mkdir(parents=True)
            archive.mkdir()
            session_id = '01a06eb1-6102-74f0-afc3-77414554f236'
            transcript = sessions / f'rollout-2026-09-05-{session_id}.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content': 'Son karar korunmalı.'}), encoding='utf-8')
            transport = state / 'hookin-moving.json'
            transport.write_text(json.dumps({'session_id': session_id, 'transcript_path': str(transcript)}), encoding='utf-8')
            job = {'kind': 'flush', 'payload': {'hook_input': str(transport),
                'reason': 'sessionend', 'event_iso': '2026-09-05T12:00:00+03:00'}}
            real_flush = flush.flush_once

            def archive_before_read(*args, **kwargs):
                if transcript.exists():
                    transcript.rename(archive / transcript.name)
                return real_flush(*args, **kwargs)

            summary = '\n'.join(f'## {section}\nSon karar korunmalı.' for section in flush.EXPECTED_SECTIONS)
            with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), mock.patch.object(
                flush, 'flush_once', side_effect=archive_before_read
            ), mock.patch.object(workers, 'load_hook_input', wraps=workers.load_hook_input) as load, mock.patch.object(
                flush, 'run_codex', return_value=(summary, None)
            ), mock.patch.object(flush, 'maybe_trigger_compile'):
                workers._dispatch_job(vault, state, job)
                load.assert_called_once_with(transport)
            self.assertIn('Son karar korunmalı.', (vault / 'daily/2026-09-05.md').read_text(encoding='utf-8'))
            self.assertTrue(transport.exists(), "Only the final success publisher may remove the input")

    def test_dispatch_reads_transport_once_and_preserves_success_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            transcript = vault / 'source.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content': 'Karar: yerel kayıt.'}), encoding='utf-8')
            transport = state / 'hookin-once.json'
            transport.write_text(json.dumps({'session_id': 'once', 'transcript_path': str(transcript)}), encoding='utf-8')
            job = {'kind': 'flush', 'payload': {'hook_input': str(transport),
                'reason': 'turnend', 'event_iso': '2026-09-05T12:00:00+03:00'}}
            read = Path.read_text
            reads = []

            def read_once(path, *args, **kwargs):
                if path == transport:
                    reads.append(path)
                    if len(reads) > 1:
                        raise OSError('transport was already consumed')
                return read(path, *args, **kwargs)

            with mock.patch.object(Path, 'read_text', read_once), mock.patch.object(
                flush, 'run_codex', return_value=('FLUSH_BOS', None)
            ):
                workers._dispatch_job(vault, state, job)
            self.assertEqual(len(reads), 1)
            self.assertTrue(transport.exists(), "Only the final success publisher may remove the input")

    def test_worker_owns_bounded_transport_and_queue_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            with mock.patch.object(workers, 'enqueue_job', return_value=state / 'job.json') as enqueue:
                result = workers.enqueue_flush(state, {'session_id': 's', 'transcript_path': 'source.jsonl',
                    'prompt': 'Do not copy user text'}, 'turnend', vault_root=vault)
            self.assertEqual(result, state / 'job.json')
            transport = next(state.glob('hookin-*.json'))
            args = enqueue.call_args.args
            transport_payload = json.loads(transport.read_text(encoding='utf-8'))
            self.assertEqual(
                transport_payload,
                {
                    'delivery_schema_version': 1,
                    'session_id': 's',
                    'transcript_path': 'source.jsonl',
                    'reason': 'turnend',
                    'event_iso': args[2]['event_iso'],
                },
            )
            self.assertEqual(args[:2], (state, 'flush'))
            self.assertEqual(args[2]['hook_input'], str(transport))
            self.assertEqual(args[2]['reason'], 'turnend')
            self.assertIsNotNone(dt.datetime.fromisoformat(args[2]['event_iso']).tzinfo)

    def test_queue_deadline_leaves_recoverable_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "source.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            holder = self._hold_lock_in_process(state, state / "worker-queue", 1.0)
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(RuntimeError, "worker-queue-deadline"):
                    workers.enqueue_flush(
                        state,
                        {"session_id": "bounded-queue", "transcript_path": str(transcript)},
                        "turnend",
                        vault_root=state,
                        deadline=time.monotonic() + 0.2,
                    )
            finally:
                holder.wait(timeout=5)
            elapsed = time.monotonic() - started
            inputs = list(state.glob("hookin-*.json"))
            self.assertLess(elapsed, 1.5)
            self.assertEqual(len(inputs), 1)
            seen: list[dict[str, object]] = []

            def run_without_model(_vault: Path, _state: Path, running: Path) -> None:
                job = json.loads(running.read_text(encoding="utf-8"))
                seen.append(job)
                workers._finish_job(state, running, job, status="succeeded", now=100)

            self.assertEqual(
                workers.run_supervisor(state, state, now=lambda: 100, job_runner=run_without_model),
                0,
            )
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["payload"]["hook_input"], str(inputs[0]))

    def test_replay_after_recovery_claim_reuses_the_same_running_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "source.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            transport = state / "hookin-race.json"
            transport.write_text(
                json.dumps(
                    {
                        "delivery_schema_version": 1,
                        "session_id": "recovery-race",
                        "transcript_path": str(transcript),
                        "reason": "turnend",
                        "event_iso": "2026-09-09T12:00:00+03:00",
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "hook_input": str(transport),
                "reason": "turnend",
                "event_iso": "2026-09-09T12:00:00+03:00",
            }
            self.assertEqual(workers.recover_orphan_hook_inputs(state, now=100), 1)
            running, job = workers._claim_next_job(state, now=100)
            replay = workers.enqueue_job(
                state,
                "flush",
                payload,
                start_supervisor=False,
                now=100,
            )
            self.assertEqual(replay, running)
            self.assertEqual(list((state / "worker-jobs" / "pending").glob("*.json")), [])
            workers._finish_job(state, running, job, status="succeeded", now=101)
            replay_after_success = workers.enqueue_job(
                state,
                "flush",
                payload,
                start_supervisor=False,
                now=101,
            )
            succeeded = state / "worker-jobs" / "succeeded" / running.name

        self.assertEqual(replay_after_success, succeeded)

    def test_orphan_recovery_orders_same_scope_transports_by_event_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "source.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            older = state / "hookin-zzzz.json"
            newer = state / "hookin-aaaa.json"
            for path, event_iso in (
                (older, "2026-09-09T12:00:00+03:00"),
                (newer, "2026-09-09T12:05:00+03:00"),
            ):
                path.write_text(
                    json.dumps(
                        {
                            "delivery_schema_version": 1,
                            "session_id": "chronology",
                            "transcript_path": str(transcript),
                            "reason": "turnend",
                            "event_iso": event_iso,
                        }
                    ),
                    encoding="utf-8",
                )

            self.assertEqual(workers.recover_orphan_hook_inputs(state, now=100), 2)
            pending = list((state / "worker-jobs" / "pending").glob("*.json"))
            job = json.loads(pending[0].read_text(encoding="utf-8"))
            newer_exists = newer.exists()
            older_exists = older.exists()

        self.assertEqual(job["payload"]["hook_input"], str(newer))
        self.assertEqual(job["payload"]["event_iso"], "2026-09-09T12:05:00+03:00")
        self.assertTrue(newer_exists)
        self.assertFalse(older_exists)

    def test_replay_lookup_does_not_quarantine_recoverable_success_transition(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transport = state / "hookin-transition.json"
            transport.write_text(
                json.dumps(
                    {
                        "delivery_schema_version": 1,
                        "session_id": "transition",
                        "transcript_path": str(state / "source.jsonl"),
                        "reason": "turnend",
                        "event_iso": "2026-09-09T12:00:00+03:00",
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "hook_input": str(transport),
                "reason": "turnend",
                "event_iso": "2026-09-09T12:00:00+03:00",
            }
            workers.enqueue_job(state, "flush", payload, start_supervisor=False, now=100)
            running, job = workers._claim_next_job(state, now=100)
            transition = json.loads(running.read_text(encoding="utf-8"))
            transition.update(
                {
                    "status": "succeeded",
                    "finished_ts": 101,
                    "lease_until": 0,
                }
            )
            for field in ("claim_token", "owner_pid", "owner_identity"):
                transition.pop(field, None)
            workers.atomic_write_json(running, transition)
            before = running.read_bytes()
            replay = workers.enqueue_job(
                state,
                "flush",
                payload,
                start_supervisor=False,
                now=101,
            )
            self.assertEqual(replay, running)
            self.assertEqual(running.read_bytes(), before)
            self.assertEqual(list((state / "worker-jobs" / "quarantined").glob("*.json")), [])
            self.assertEqual(workers.recover_stale_jobs(state, now=101), 0)
            self.assertFalse(running.exists())
            self.assertTrue((state / "worker-jobs" / "succeeded" / running.name).is_file())

    def test_active_supervisor_reclaims_orphan_arriving_after_a_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(
                state,
                "maintenance",
                {"seed": True},
                start_supervisor=False,
                now=100,
            )
            transcript = state / "source.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            processed: list[str] = []

            def runner(_vault: Path, _state: Path, running: Path) -> None:
                job = workers._load_job(running)
                processed.append(str(job["kind"]))
                if job["kind"] == "maintenance":
                    transport = state / "hookin-arrived.json"
                    transport.write_text(
                        json.dumps(
                            {
                                "delivery_schema_version": 1,
                                "session_id": "arrived",
                                "transcript_path": str(transcript),
                                "reason": "turnend",
                                "event_iso": "2026-09-09T12:00:00+03:00",
                            }
                        ),
                        encoding="utf-8",
                    )
                workers._finish_job(state, running, job, status="succeeded", now=100)

            self.assertEqual(
                workers.run_supervisor(state, state, now=lambda: 100, job_runner=runner),
                0,
            )

        self.assertEqual(processed, ["maintenance", "flush"])

    def test_full_terminal_queue_leaves_orphan_for_later_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(workers, "MAX_UNRESOLVED_JOBS", 1):
                workers.enqueue_job(
                    state,
                    "maintenance",
                    {"terminal": True},
                    start_supervisor=False,
                    now=100,
                )
                running, job = workers._claim_next_job(state, now=100)
                workers._finish_job(
                    state,
                    running,
                    job,
                    status="dead-letter",
                    error="terminal",
                    now=100,
                )
                transcript = state / "source.jsonl"
                transcript.write_text("{}", encoding="utf-8")
                transport = state / "hookin-capacity.json"
                transport.write_text(
                    json.dumps(
                        {
                            "delivery_schema_version": 1,
                            "session_id": "capacity",
                            "transcript_path": str(transcript),
                            "reason": "turnend",
                            "event_iso": "2026-09-09T12:00:00+03:00",
                        }
                    ),
                    encoding="utf-8",
                )
                started = time.monotonic()
                self.assertEqual(workers.run_supervisor(state, state, now=lambda: 100), 0)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertTrue(transport.exists())
                dead = state / "worker-jobs" / "dead-letter" / running.name
                dead.unlink()
                processed: list[str] = []

                def runner(_vault: Path, _state: Path, running_path: Path) -> None:
                    current = workers._load_job(running_path)
                    processed.append(str(current["kind"]))
                    workers._finish_job(state, running_path, current, status="succeeded", now=101)

                self.assertEqual(
                    workers.run_supervisor(state, state, now=lambda: 101, job_runner=runner),
                    0,
                )

        self.assertEqual(processed, ["flush"])
        self.assertFalse(transport.exists())

    def test_terminal_job_transport_is_not_counted_as_an_orphan(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transport = state / "hookin-terminal.json"
            transport.write_text(
                json.dumps(
                    {
                        "delivery_schema_version": 1,
                        "session_id": "terminal",
                        "transcript_path": str(state / "source.jsonl"),
                        "reason": "turnend",
                        "event_iso": "2026-09-09T12:00:00+03:00",
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "hook_input": str(transport),
                "reason": "turnend",
                "event_iso": "2026-09-09T12:00:00+03:00",
            }
            workers.enqueue_job(state, "flush", payload, start_supervisor=False, now=100)
            running, job = workers._claim_next_job(state, now=100)
            workers._finish_job(
                state,
                running,
                job,
                status="dead-letter",
                error="terminal",
                now=100,
            )

            count = workers.count_orphan_hook_inputs(state)
            report = workers.inspect_worker_queue(state)

        self.assertEqual(count, 0)
        self.assertEqual(report["orphan_hook_inputs"], 0)

    def test_admission_deadline_leaves_durable_job_and_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "source.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            holder = self._hold_lock_in_process(state, state / "worker-admission", 1.0)
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(RuntimeError, "worker-admission-deadline"):
                    workers.enqueue_flush(
                        state,
                        {"session_id": "bounded-admission", "transcript_path": str(transcript)},
                        "turnend",
                        vault_root=state,
                        launcher=mock.Mock(),
                        deadline=time.monotonic() + 0.2,
                    )
            finally:
                holder.wait(timeout=5)
            elapsed = time.monotonic() - started
            pending = list((state / "worker-jobs" / "pending").glob("*.json"))
            inputs = list(state.glob("hookin-*.json"))
            self.assertLess(elapsed, 1.5)
            self.assertEqual(len(pending), 1)
            self.assertEqual(len(inputs), 1)

    def test_succeeded_transport_is_consumed_before_receipt_prune(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "source.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            with mock.patch.object(workers, "ensure_supervisor"):
                workers.enqueue_flush(
                    state,
                    {"session_id": "already-done", "transcript_path": str(transcript)},
                    "turnend",
                    vault_root=state,
                )
            running, job = workers._claim_next_job(state, now=100)
            workers._finish_job(state, running, job, status="succeeded", now=100)
            transport = Path(job["payload"]["hook_input"])
            self.assertFalse(transport.exists())
            self.assertEqual(workers.recover_orphan_hook_inputs(state, now=101), 0)
            pending = list((state / "worker-jobs" / "pending").glob("*.json"))

        self.assertEqual(pending, [])

    def test_failed_success_cleanup_pins_receipt_until_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "source.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            with mock.patch.object(workers, "ensure_supervisor"):
                workers.enqueue_flush(
                    state,
                    {"session_id": "cleanup-retry", "transcript_path": str(transcript)},
                    "turnend",
                    vault_root=state,
                )
            running, job = workers._claim_next_job(state, now=100)
            transport = Path(job["payload"]["hook_input"])
            real_unlink = Path.unlink

            def fail_transport(path: Path, *args: object, **kwargs: object):
                if path == transport:
                    raise OSError("busy")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_transport):
                workers._finish_job(state, running, job, status="succeeded", now=100)
                workers.prune_succeeded_jobs(state, now=10**12)
            receipt = next((state / "worker-jobs" / "succeeded").glob("*.json"))
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertTrue(saved["hook_input_cleanup_pending"])
            self.assertTrue(transport.exists())
            workers.prune_succeeded_jobs(state, now=10**12)
            self.assertFalse(transport.exists())
            self.assertFalse(receipt.exists())

    def test_cleanup_pin_is_checked_per_succeeded_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript = state / "source.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            with mock.patch.object(workers, "ensure_supervisor"):
                workers.enqueue_flush(
                    state,
                    {"session_id": "pinned", "transcript_path": str(transcript)},
                    "turnend",
                    vault_root=state,
                )
            pinned_path, pinned_job = workers._claim_next_job(state, now=100)
            pinned_transport = Path(pinned_job["payload"]["hook_input"])
            clean_path = workers.enqueue_job(
                state,
                "flush",
                {"value": "clean"},
                start_supervisor=False,
                now=100,
            )
            clean_running, clean_job = workers._claim_next_job(state, now=100)
            real_unlink = Path.unlink

            def fail_pinned(path: Path, *args: object, **kwargs: object):
                if path == pinned_transport:
                    raise OSError("busy")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(workers, "MAX_SUCCEEDED_RECEIPTS", 0), mock.patch.object(
                Path, "unlink", autospec=True, side_effect=fail_pinned
            ):
                workers._finish_job(state, pinned_path, pinned_job, status="succeeded", now=100)
                workers._finish_job(state, clean_running, clean_job, status="succeeded", now=100)
                workers.prune_succeeded_jobs(state, now=10**12)
                succeeded = {
                    path.name
                    for path in (state / "worker-jobs" / "succeeded").glob("*.json")
                }

            self.assertIn(pinned_path.name, succeeded)
            self.assertNotIn(clean_path.name, succeeded)
            workers.prune_succeeded_jobs(state, now=10**12)
            self.assertFalse(pinned_transport.exists())
            self.assertFalse((state / "worker-jobs" / "succeeded" / pinned_path.name).exists())


if __name__ == '__main__':
    unittest.main()
