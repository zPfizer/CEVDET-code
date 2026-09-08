from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import codex_runner
import doctor
import flush
import process_control
import worker_supervisor as workers


class WorkerCleanupBoundaryTests(unittest.TestCase):
    def test_child_fence_is_promoted_before_result_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            workers.enqueue_job(
                state,
                'maintenance',
                {},
                start_supervisor=False,
                now=100,
            )
            running, job = workers._claim_next_job(state, now=100)
            workers.atomic_write_json(
                state / workers.TREE_CLEANUP_FENCE_NAME,
                {
                    'schema_version': 1,
                    'job_id': job['job_id'],
                    'pid': 4242,
                    'ts': 100,
                    'error': 'ProcessTreeCleanupError',
                    'retryable': False,
                },
            )
            with (
                mock.patch.object(
                    workers,
                    'run_with_tree_timeout',
                    return_value=subprocess.CompletedProcess([], 1),
                ),
                self.assertRaises(process_control.ProcessTreeCleanupError) as caught,
            ):
                workers._run_claimed_job(root, state, running, timeout=1)
            self.assertEqual(caught.exception.pid, 4242)

    def test_inner_flush_cleanup_failure_fences_before_worker_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            transcript = root / 'transcript.jsonl'
            transcript.write_text('{}\n', encoding='utf-8')
            hook_input = state / 'hookin-inner-cleanup.json'
            hook_input.parent.mkdir(parents=True)
            hook_input.write_text(
                json.dumps({'transcript_path': str(transcript)}),
                encoding='utf-8',
            )
            workers.enqueue_job(
                state,
                'flush',
                {
                    'hook_input': str(hook_input),
                    'reason': 'turnend',
                    'event_iso': '2026-09-09T12:00:00+03:00',
                },
                start_supervisor=False,
                now=100,
            )
            cleanup_failure = process_control.ProcessTreeCleanupError(
                ['codex'], 1, 4242, OSError('inner-tree-unverified')
            )

            def failing_flush(*args, **kwargs):
                del kwargs
                flush.run_codex('prompt', args[2])
                return 0

            with (
                mock.patch.object(flush, 'flush_once', side_effect=failing_flush),
                mock.patch.object(
                    codex_runner,
                    'run_with_tree_timeout',
                    side_effect=cleanup_failure,
                ),
            ):
                result = workers.run_supervisor(
                    root,
                    state,
                    job_runner=lambda vault, queue, running: workers.execute_job_file(
                        vault, queue, running
                    ),
                    now=lambda: 100,
                )

            self.assertEqual(result, 1)
            self.assertTrue((state / workers.TREE_CLEANUP_FENCE_NAME).is_file())
            self.assertIsNone(workers._claim_next_job(state, now=10**12))
            self.assertEqual(workers.recover_stale_jobs(state, now=10**12), 0)
            with self.assertRaisesRegex(ValueError, 'worker-tree-cleanup-unverified'):
                workers.ensure_supervisor(state, vault_root=root, launcher=mock.Mock())

    def test_unverified_tree_blocks_retries_even_after_child_terminal_move(self):
        for child_result in (None, 'succeeded', 'failed'):
            with self.subTest(child_result=child_result), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / '.codex/scripts/.state'
                for index in range(2):
                    workers.enqueue_job(state, 'maintenance', {'index': index}, start_supervisor=False, now=100)
                attempts = []

                def runner(_root, _state, running):
                    job = workers._load_job(running)
                    attempts.append((job['job_id'], job['attempt']))
                    if child_result:
                        workers._finish_job(state, running, job, status=child_result, now=100)
                    raise process_control.ProcessTreeCleanupError(['owned-child'], 1, 12345, OSError('unverified'))

                clock = iter(range(100, 100_000, 100))
                with mock.patch.object(workers, '_process_owner_is_active', return_value=True):
                    result = workers.run_supervisor(root, state, job_runner=runner, now=lambda: next(clock))
                self.assertEqual(result, 1)
                self.assertEqual(len(attempts), 1)
                report = workers.inspect_worker_queue(state)
                self.assertEqual(report['status'], 'error')
                self.assertTrue(report['cleanup_unverified'])
                self.assertIsNone(workers._claim_next_job(state, now=100_000))
                with mock.patch.object(workers, '_process_owner_is_active', return_value=False):
                    self.assertEqual(workers.recover_stale_jobs(state, now=100_000), 0)
                self.assertEqual(workers.run_supervisor(root, state, job_runner=runner), 1)
                self.assertEqual(len(attempts), 1)
                launch = mock.Mock()
                with self.assertRaisesRegex(ValueError, 'worker-tree-cleanup-unverified'):
                    workers.ensure_supervisor(state, vault_root=root, launcher=launch)
                launch.assert_not_called()
                check = doctor._worker_queue_check(types.SimpleNamespace(state_dir=state, now=100_000))
                self.assertEqual(check.status, 'FAIL')
                self.assertIn('cleanup-unverified', check.evidence)

    def test_verified_timeout_still_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workers.enqueue_job(root, 'maintenance', {}, start_supervisor=False, now=100)
            attempts = []

            def runner(_root, state, running):
                job = workers._load_job(running)
                attempts.append(job['attempt'])
                if len(attempts) == 1:
                    raise process_control.ProcessTreeTimeout(['owned-child'], 1, 12345)
                workers._finish_job(state, running, job, status='succeeded', now=1000)

            clock = iter(range(100, 100_000, 100))
            self.assertEqual(workers.run_supervisor(root, root, job_runner=runner, now=lambda: next(clock)), 0)
            self.assertEqual(attempts, [1, 2])
            self.assertEqual(workers.inspect_worker_queue(root)['status'], 'ok')

    def test_maintenance_fence_does_not_block_the_independent_save_lane(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            maintenance = state / 'maintenance'
            workers.enqueue_job(maintenance, 'maintenance', {}, start_supervisor=False)

            def runner(*_args):
                raise process_control.ProcessTreeCleanupError(['owned-child'], 1, 12345, OSError('unverified'))

            self.assertEqual(workers.run_supervisor(root, maintenance, job_runner=runner), 1)
            workers.enqueue_job(state, 'flush', {'hook_input': 'fixture'}, start_supervisor=False)
            self.assertIsNotNone(workers._claim_next_job(state, now=10**12))
            self.assertFalse(workers.inspect_worker_queue(state)['cleanup_unverified'])
            check = doctor._worker_queue_check(types.SimpleNamespace(state_dir=state, now=100_000))
            self.assertEqual(check.status, 'FAIL')
            self.assertIn('maintenance', check.evidence)


if __name__ == '__main__':
    unittest.main()
