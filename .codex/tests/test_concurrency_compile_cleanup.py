"""Inner compiler cleanup failures must stop the maintenance lane."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import codex_runner
import compile as compiler
import process_control
import worker_supervisor as workers
from test_second_brain_acceptance import _seed_vault


class CompilerCleanupTests(unittest.TestCase):
    def _failure(self):
        return process_control.ProcessTreeCleanupError(['synthetic-model'], 1, 4242, OSError('unverified-inner-tree'))

    def test_real_worker_compiler_chain_fences_inner_model_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed_vault(root)
            state = root / '.codex/scripts/.state'
            lane = state / 'maintenance'
            workers.enqueue_job(lane, 'maintenance', {}, start_supervisor=False, now=100)
            with mock.patch.object(codex_runner, 'find_codex', return_value=sys.executable), \
                 mock.patch.object(codex_runner, 'run_with_tree_timeout', side_effect=self._failure()) as model:
                result = workers.run_supervisor(root, lane, job_runner=lambda vault, queue, running: workers.execute_job_file(vault, queue, running), now=lambda: 100)
            self.assertEqual(result, 1)
            model.assert_called_once()
            self.assertTrue(workers.has_unverified_process_tree(lane))
            self.assertFalse(workers.has_unverified_process_tree(state))
            self.assertIsNone(workers._claim_next_job(lane, now=10**12))
            self.assertTrue(list(state.glob('compile-stage-*')))

    def test_direct_compiler_fences_and_does_not_start_a_second_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed_vault(root)
            state = root / '.codex/scripts/.state'
            with mock.patch.multiple(compiler, VAULT_ROOT=root, STATE_DIR=state), \
                 mock.patch.dict(os.environ), \
                 mock.patch.object(codex_runner, 'find_codex', return_value=sys.executable), \
                 mock.patch.object(codex_runner, 'run_with_tree_timeout', side_effect=self._failure()) as model:
                os.environ.pop('BEYIN_INVOKED_BY', None)
                self.assertEqual(compiler.main(['--strict']), 1)
                self.assertEqual(compiler.main(['--strict']), 1)
            model.assert_called_once()
            self.assertTrue(workers.has_unverified_process_tree(state / 'maintenance'))
            self.assertFalse(workers.has_unverified_process_tree(state))
