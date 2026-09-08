from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import compile as compiler


class CheckpointConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / '.state'
        self.native_run = subprocess.run
        environment = mock.patch.dict(os.environ, {
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_COUNT': '0',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Concurrency test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        (self.root / 'empty-hooks').mkdir()
        self.git('config', 'core.hooksPath', str(self.root / 'empty-hooks'))
        (self.root / 'daily').mkdir()
        (self.root / 'knowledge').mkdir()
        self.daily = self.root / 'daily/day.md'
        self.human = self.root / 'human.md'
        self.daily.write_text('before\n', encoding='utf-8')
        self.human.write_text('human before\n', encoding='utf-8')
        (self.root / 'knowledge/index.md').write_text('index\n', encoding='utf-8')
        self.git('add', '--', 'daily', 'knowledge', 'human.md')
        self.git('commit', '-qm', 'baseline')
        self.daily.write_text('machine update\n', encoding='utf-8')

    def git(self, *args, check=True):
        return self.native_run(
            ['git', '-C', str(self.root), *args],
            check=check, capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

    def test_late_unrelated_stage_is_never_captured_or_lost(self):
        attempts = []
        self.human.write_text('human pending\n', encoding='utf-8')

        def race(command, **kwargs):
            if 'commit' in command or 'commit-tree' in command:
                attempts.append(self.git('add', '--', 'human.md', check=False))
            return self.native_run(command, **kwargs)

        with mock.patch.object(compiler.subprocess, 'run', side_effect=race):
            outcome, commit = compiler._checkpoint_machine_outputs(self.root, self.state, 'race')
        self.assertEqual(outcome, 'committed')
        self.assertTrue(attempts)
        paths = self.git('diff-tree', '--no-commit-id', '--name-only', '-r', commit).stdout.splitlines()
        self.assertEqual(paths, ['daily/day.md'])
        self.assertEqual(self.human.read_text(encoding='utf-8'), 'human pending\n')
        if attempts[-1].returncode:
            self.assertIn('index.lock', attempts[-1].stderr)
            self.git('add', '--', 'human.md')
        self.assertEqual(self.git('diff', '--cached', '--name-only').stdout.strip(), 'human.md')

    def test_failed_checkpoint_never_restores_a_concurrent_machine_stage(self):
        attempts = []

        def race(command, **kwargs):
            if 'commit' in command or 'commit-tree' in command:
                self.daily.write_text('user staged\n', encoding='utf-8')
                attempts.append(self.git('add', '--', 'daily/day.md', check=False))
                self.daily.write_text('user unstaged\n', encoding='utf-8')
                return subprocess.CompletedProcess(command, 1, '', 'injected commit failure')
            return self.native_run(command, **kwargs)

        with mock.patch.object(compiler.subprocess, 'run', side_effect=race):
            outcome, _ = compiler._checkpoint_machine_outputs(self.root, self.state, 'race')
        self.assertEqual(outcome, 'deferred')
        self.assertTrue(attempts)
        expected = 'user staged\n' if attempts[-1].returncode == 0 else 'before\n'
        self.assertEqual(self.git('show', ':daily/day.md').stdout, expected)
        self.assertEqual(self.daily.read_text(encoding='utf-8'), 'user unstaged\n')
        if attempts[-1].returncode:
            self.assertIn('index.lock', attempts[-1].stderr)
        self.assertFalse((self.root / '.git/index.lock').exists())

    def test_new_and_deleted_machine_files_leave_a_clean_index(self):
        (self.root / 'knowledge/index.md').unlink()
        (self.root / 'knowledge/new note.md').write_text('new\n', encoding='utf-8')
        outcome, commit = compiler._checkpoint_machine_outputs(self.root, self.state, 'changes')
        self.assertEqual(outcome, 'committed')
        changed = self.git('diff-tree', '--no-commit-id', '--name-status', '-r', commit).stdout
        self.assertIn('A\tknowledge/new note.md', changed)
        self.assertIn('D\tknowledge/index.md', changed)
        self.assertEqual(self.git('diff', '--cached', '--name-only').stdout, '')
        self.assertEqual(self.git('diff', '--name-only', '--', 'daily', 'knowledge').stdout, '')

    def test_foreign_index_lock_is_preserved(self):
        lock_path = self.root / '.git/index.lock'
        lock_path.write_bytes(b'foreign owner')
        outcome, _ = compiler._checkpoint_machine_outputs(self.root, self.state, 'busy')
        self.assertEqual(outcome, 'deferred')
        self.assertEqual(lock_path.read_bytes(), b'foreign owner')
        self.assertEqual(self.git('show', 'HEAD:daily/day.md').stdout, 'before\n')

    def test_ref_compare_and_swap_does_not_overwrite_an_intervening_commit(self):
        parent = self.git('rev-parse', 'HEAD').stdout.strip()
        tree = self.git('rev-parse', 'HEAD^{tree}').stdout.strip()
        other = self.git('commit-tree', tree, '-p', parent, '-m', 'external commit').stdout.strip()

        def race(command, **kwargs):
            if 'update-ref' in command:
                self.git('update-ref', 'refs/heads/main', other, parent)
            return self.native_run(command, **kwargs)

        with mock.patch.object(compiler.subprocess, 'run', side_effect=race):
            outcome, detail = compiler._checkpoint_machine_outputs(self.root, self.state, 'race')
        self.assertEqual((outcome, detail), ('deferred', 'machine-head-changed'))
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), other)
        self.assertEqual(self.git('show', ':daily/day.md').stdout, 'before\n')
        self.assertEqual(self.daily.read_text(encoding='utf-8'), 'machine update\n')
        self.assertFalse((self.root / '.git/index.lock').exists())

    def test_timeout_after_ref_update_reports_the_verified_commit(self):
        def race(command, **kwargs):
            result = self.native_run(command, **kwargs)
            if 'update-ref' in command:
                self.assertEqual(result.returncode, 0)
                raise subprocess.TimeoutExpired(command, 30)
            return result

        with mock.patch.object(compiler.subprocess, 'run', side_effect=race):
            outcome, commit = compiler._checkpoint_machine_outputs(self.root, self.state, 'race')
        self.assertEqual(outcome, 'committed')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), commit)
        self.assertEqual(self.git('diff', '--cached', '--name-only').stdout, '')
        self.assertFalse((self.root / '.git/index.lock').exists())

    def test_timeout_after_a_successor_commit_never_stages_an_undo(self):
        published = []

        def race(command, **kwargs):
            result = self.native_run(command, **kwargs)
            if 'update-ref' in command:
                self.assertEqual(result.returncode, 0)
                checkpoint = self.git('rev-parse', 'HEAD').stdout.strip()
                published.append(checkpoint)
                tree = self.git('rev-parse', 'HEAD^{tree}').stdout.strip()
                successor = self.git('commit-tree', tree, '-p', checkpoint, '-m', 'successor').stdout.strip()
                self.git('update-ref', 'refs/heads/main', successor, checkpoint)
                raise subprocess.TimeoutExpired(command, 30)
            return result

        with mock.patch.object(compiler.subprocess, 'run', side_effect=race):
            outcome, commit = compiler._checkpoint_machine_outputs(self.root, self.state, 'race')
        self.assertEqual((outcome, commit), ('committed', published[0]))
        self.assertEqual(self.git('diff', '--cached', '--name-only').stdout, '')
        self.assertEqual(self.git('show', ':daily/day.md').stdout, 'machine update\n')

    def test_ambiguous_ref_error_keeps_the_machine_snapshot_staged(self):
        parent = self.git('rev-parse', 'HEAD').stdout
        for failure in (OSError('spawn failed'), subprocess.TimeoutExpired(['git'], 30)):
            with self.subTest(failure=type(failure).__name__):
                def race(command, **kwargs):
                    if 'update-ref' in command:
                        raise failure
                    return self.native_run(command, **kwargs)

                with mock.patch.object(compiler.subprocess, 'run', side_effect=race):
                    outcome, detail = compiler._checkpoint_machine_outputs(self.root, self.state, 'race')
                self.assertEqual((outcome, detail), ('deferred', 'machine-commit-uncertain'))
                self.assertEqual(self.git('rev-parse', 'HEAD').stdout, parent)
                self.assertEqual(self.git('show', ':daily/day.md').stdout, 'machine update\n')
                self.assertFalse((self.root / '.git/index.lock').exists())
                self.git('restore', '--staged', '--', 'daily/day.md')

    def test_commit_hook_policy_cannot_be_bypassed(self):
        hook = self.root / 'empty-hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        hook.chmod(0o755)
        parent = self.git('rev-parse', 'HEAD').stdout
        outcome, _ = compiler._checkpoint_machine_outputs(self.root, self.state, 'policy')
        self.assertEqual(outcome, 'deferred')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout, parent)
        self.assertEqual(self.git('diff', '--cached', '--name-only').stdout, '')

    def test_signing_policy_cannot_be_bypassed(self):
        self.git('config', 'commit.gpgsign', 'true')
        self.git('config', 'gpg.program', str(self.root / 'missing-signer'))
        parent = self.git('rev-parse', 'HEAD').stdout
        outcome, _ = compiler._checkpoint_machine_outputs(self.root, self.state, 'policy')
        self.assertEqual(outcome, 'deferred')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout, parent)
        self.assertEqual(self.git('diff', '--cached', '--name-only').stdout, '')


if __name__ == '__main__':
    unittest.main()
