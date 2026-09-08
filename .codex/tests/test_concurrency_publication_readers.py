from __future__ import annotations

from pathlib import Path
from contextlib import chdir
import subprocess
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import companion_memory
import compile_state
import flush
import hook
import knowledge_schema
import memory_ledger as ledger
import vault_retrieval as retrieval
import worker_supervisor


class PublicationReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / '.codex/scripts/.state'
        self.source = self.root / 'knowledge/index.md'
        self.source.parent.mkdir()
        self.source.write_text('# Bilgi Tabani\nyayin tutarlilik kaniti\n', encoding='utf-8')
        (self.root / 'knowledge/log.md').write_text('# Derleme Günlüğü\n', encoding='utf-8')

    def pending(self):
        compile_state.save_publication(self.state, {'schema_version': 1, 'status': 'pending'})

    def completed(self):
        self.pending()
        compile_state.save_publication_token(self.state, 'a' * 32)
        compile_state.clear_publication(self.state)

    def test_pending_blocks_direct_knowledge_read(self):
        self.pending()
        with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-pending'):
            ledger.MemoryRead(self.root, frozenset()).read_source(self.source)

    def test_pending_preserves_independent_companion_read(self):
        companion = self.root / '🔮 850-Companion/Last-Session.md'
        companion.parent.mkdir()
        companion.write_text('# Bağımsız oturum kaydı\n', encoding='utf-8')
        self.pending()
        self.assertIn('Bağımsız', ledger.read_memory_source(self.root, companion))
        self.assertEqual(companion_memory.previous_summary(self.root, 'A'), '')

    def test_completed_publication_between_read_and_return_is_rejected(self):
        with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-changed'):
            with ledger.memory_read(self.root) as memory:
                memory.read_source(self.source)
                self.completed()

    def test_direct_source_checks_after_read_without_context_manager(self):
        read_text = Path.read_text

        def read(path, *args, **kwargs):
            value = read_text(path, *args, **kwargs)
            if path == self.source:
                self.completed()
            return value

        with mock.patch.object(Path, 'read_text', new=read):
            with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-changed'):
                ledger.MemoryRead(self.root, frozenset()).read_source(self.source)

    def test_filtered_view_wrappers_preserve_publication_error(self):
        private = self.root / '.codex/private-memory'
        ledger.suppress_derived_memory(private, 'synthetic suppressed topic')
        hashes = ledger.load_suppressed_hashes(private)
        self.pending()
        for method in ('views', 'render_views'):
            with self.subTest(method=method):
                memory = ledger.MemoryRead(self.root, hashes)
                with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'memory-publication-pending'):
                    getattr(memory, method)([('knowledge/index.md', 'Index')])

    def test_pending_blocks_filtered_view_materialization_and_read(self):
        private = self.root / '.codex/private-memory'
        ledger.suppress_derived_memory(private, 'gizli konu')
        hashes = ledger.load_suppressed_hashes(private)
        views = ledger.materialize_memory_views(self.root, [('knowledge/index.md', 'Bilgi')], hashes)
        self.pending()
        for read in (
            lambda: ledger.materialize_memory_views(self.root, [('knowledge/index.md', 'Bilgi')], hashes),
            lambda: ledger.read_memory_source(self.root, self.root / views['knowledge/index.md']),
        ):
            with self.subTest(read=read):
                with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-pending'):
                    read()

    def test_session_context_checks_raw_index_generation(self):
        compact = hook._compact_knowledge_index

        def read(*args, **kwargs):
            value = compact(*args, **kwargs)
            self.completed()
            return value

        with mock.patch.object(hook, '_compact_knowledge_index', side_effect=read):
            with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-changed'):
                hook.build_session_context(self.root, self.state)

    def test_pending_session_context_and_retrieval_fail_closed(self):
        self.pending()
        for read in (
            lambda: hook.build_session_context(self.root, self.state),
            lambda: retrieval.retrieve_vault_context_detailed(self.root, 'yayin tutarlilik kaniti'),
        ):
            with self.subTest(read=read):
                with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-pending'):
                    read()

    def test_session_start_warns_and_keeps_authorized_recovery_reachable(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, capture_output=True)
        self.pending()
        for read_only in (False, True):
            session_id = f'session-{read_only}'
            if read_only:
                ledger.mark_read_only_turn(self.state, session_id)
            with chdir(self.root), self.subTest(read_only=read_only), \
                 mock.patch.multiple(hook, VAULT_ROOT=self.root, STATE_DIR=self.state), \
                 mock.patch.object(hook, '_load_payload', return_value={'session_id': session_id, 'cwd': str(self.root.resolve())}), \
                 mock.patch.object(hook, '_emit_context') as emit, \
                 mock.patch.object(hook, 'ensure_supervisor'), \
                 mock.patch.object(flush, 'maybe_trigger_compile', return_value=False) as trigger:
                self.assertEqual(hook.main(['session-start', '--strict']), 0)
                self.assertEqual(trigger.call_count, 0 if read_only else 1)
                emit.assert_called_once()
                self.assertIn('Bilgi yayınının tutarlılığı', emit.call_args.args[1])
                self.assertNotIn('yayin tutarlilik kaniti', emit.call_args.args[1])

    def test_pending_publication_queues_recovery_without_changed_daily(self):
        self.pending()
        self.assertFalse(compile_state.has_changes(self.root))
        with mock.patch.object(worker_supervisor, 'enqueue_maintenance') as enqueue:
            self.assertTrue(flush.maybe_trigger_compile(self.root))
        enqueue.assert_called_once()

    def test_completed_publication_before_fresh_hits_rejects_result(self):
        fresh = retrieval._fresh_hits

        def read(*args, **kwargs):
            self.completed()
            return fresh(*args, **kwargs)

        with mock.patch.object(retrieval, '_fresh_hits', side_effect=read):
            with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-changed'):
                retrieval.retrieve_vault_context_detailed(self.root, 'yayin tutarlilik kaniti')

    def test_map_does_not_cache_a_mixed_publication(self):
        retrieval.build_vault_map(self.root)
        cache = self.root / retrieval.CACHE_RELATIVE_PATH
        before = cache.read_bytes()
        self.source.write_text('# Bilgi Tabani\nyeni yayin kaniti\n', encoding='utf-8')

        def read(root, path):
            entry = retrieval.entry_from_file(root, path)
            if path == self.source:
                self.completed()
            return entry

        with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-changed'):
            retrieval.build_vault_map(self.root, read_entry=read)
        self.assertEqual(cache.read_bytes(), before)

    def test_schema_reports_pending_or_invalid_journal(self):
        self.pending()
        report = knowledge_schema.validate_knowledge_tree(self.root)
        self.assertIn('knowledge:publication-pending', report.issues)
        compile_state.publication_file(self.state).write_text('{', encoding='utf-8')
        report = knowledge_schema.validate_knowledge_tree(self.root)
        self.assertIn('knowledge:publication-journal-unreadable', report.issues)

    def test_schema_rejects_generation_changed_during_validation(self):
        apply_rules = knowledge_schema._apply_rules

        def validate(*args, **kwargs):
            apply_rules(*args, **kwargs)
            self.completed()

        with mock.patch.object(knowledge_schema, '_apply_rules', side_effect=validate):
            report = knowledge_schema.validate_knowledge_tree(self.root)
        self.assertIn('knowledge:publication-changed', report.issues)

    def test_malformed_publication_fails_as_memory_error(self):
        self.state.mkdir(parents=True)
        compile_state.publication_file(self.state).write_text('{', encoding='utf-8')
        with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'publication-journal-unreadable'):
            ledger.read_memory_source(self.root, self.source)


if __name__ == '__main__':
    unittest.main()
