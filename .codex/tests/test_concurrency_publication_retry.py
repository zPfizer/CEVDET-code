"""A retried publication must not adopt an intervening writer's content."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import compile as compiler
import compile_state
import state_store
from test_concurrency_publication import PublicationFixture


class PublicationRetryTests(unittest.TestCase):
    def _assert_changed_target_is_preserved(self, recovery):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            if recovery:
                with mock.patch.object(compiler, '_atomic_copy', side_effect=OSError('seed pending journal')):
                    with self.assertRaises(OSError):
                        fixture.promote()
            destination = fixture.knowledge / 'index.md'
            replacement = 'Intervening writer content.\n'
            real_replace = state_store.os.replace
            attempts = 0

            def sharing_conflict(source, target):
                nonlocal attempts
                if Path(target) == destination:
                    attempts += 1
                    if attempts == 1:
                        destination.write_text(replacement, encoding='utf-8')
                        error = PermissionError('synthetic sharing conflict')
                        error.winerror = 32
                        raise error
                return real_replace(source, target)

            operation = (
                lambda: compiler._recover_pending_publication(fixture.root, fixture.state)
            ) if recovery else fixture.promote
            with mock.patch.object(state_store.os, 'replace', side_effect=sharing_conflict), \
                 mock.patch.object(state_store, '_is_windows_share_error', side_effect=lambda exc: getattr(exc, 'winerror', None) == 32):
                with self.assertRaisesRegex(compiler.PolicyError, 'live-target-changed'):
                    operation()
            self.assertEqual(attempts, 1)
            self.assertEqual(destination.read_text(encoding='utf-8'), replacement)
            self.assertTrue(fixture.stage.is_dir())
            self.assertEqual(compile_state.load_publication(fixture.state)['status'], 'pending')

    def test_promotion_stops_when_target_changes_during_retry(self):
        self._assert_changed_target_is_preserved(False)

    def test_recovery_stops_when_target_changes_during_retry(self):
        self._assert_changed_target_is_preserved(True)
