import argparse
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import attachment_memory
import companion_memory
import flush
import hook
import memory_ledger as ledger


EVENT = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)
SUMMARY = '\n'.join(f'## {section}\nKaynaklı karar.' for section in flush.EXPECTED_SECTIONS)


class ReadOnlyPublicationTests(unittest.TestCase):
    def test_early_returns_do_not_compile_after_scope_changes_during_read(self):
        for branch in ('completed', 'recent'):
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / '.codex/scripts/.state'
                trace = root / 'transcript.jsonl'
                trace.write_text(json.dumps({'role': 'user', 'content': 'Kaynakları koruma kararı.'}), encoding='utf-8')
                payload = {'session_id': 'audit', 'transcript_path': str(trace)}
                args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
                original_read = flush.transcript_index.open_or_update

                def restrict_after_read(*args, **kwargs):
                    result = original_read(*args, **kwargs)
                    ledger.mark_read_only_turn(state, 'audit')
                    return result

                with mock.patch.object(flush, 'run_codex', return_value=(SUMMARY, None)), \
                     mock.patch.object(flush, 'maybe_trigger_compile') as compile_call:
                    if branch == 'completed':
                        self.assertEqual(flush.flush_once(args, EVENT, root, state, hook_input=payload), 0)
                        compile_call.reset_mock()
                    with mock.patch.object(flush.transcript_index, 'open_or_update', side_effect=restrict_after_read), \
                         mock.patch.object(flush, '_is_recent_duplicate', return_value=branch == 'recent'):
                        self.assertEqual(flush.flush_once(args, EVENT, root, state, hook_input=payload), 0)
                    compile_call.assert_not_called()

    def test_pending_flush_waits_for_authorization_without_consuming_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            trace = root / 'transcript.jsonl'
            trace.write_text(json.dumps({'role': 'user', 'content': 'Kalıcı karar: kaynakları tarihleriyle koruyalım.'}), encoding='utf-8')
            payload = {'session_id': 'audit', 'transcript_path': str(trace)}
            args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
            ledger.mark_read_only_turn(state, 'audit')
            with mock.patch.object(flush, 'run_codex', return_value=(SUMMARY, None)) as model, \
                 mock.patch.object(flush, 'maybe_trigger_compile'):
                self.assertEqual(flush.flush_once(args, EVENT, root, state, hook_input=payload), 0)
                model.assert_not_called()
                self.assertEqual(list((root / 'daily').glob('*.md')), [])
                self.assertEqual(list(state.glob('flush-coverage-*')), [])
                ledger.clear_read_only_turn(state, 'audit')
                self.assertEqual(flush.flush_once(args, EVENT, root, state, hook_input=payload), 0)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(len(list((root / 'daily').glob('*.md'))), 1)

    def test_fenced_read_only_prompt_defers_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            trace = root / 'transcript.jsonl'
            trace.write_text(json.dumps({'role': 'user', 'content': 'Kalıcı karar.'}), encoding='utf-8')
            payload = {
                'session_id': 'fenced-audit',
                'prompt': '~~~\r\nAudit only. Do not modify files or settings.\r\n~~~',
                'transcript_path': str(trace),
            }
            with (
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, '_validate_hook_scope'),
                mock.patch.object(hook, 'handle_user_prompt', return_value=''),
                mock.patch.object(hook, 'enqueue_flush') as enqueue,
                mock.patch.object(hook, 'record_hook_runtime'),
                mock.patch.object(hook, 'clear_hook_health'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, 'stdout', io.StringIO()),
            ):
                self.assertEqual(hook.main(['user-prompt']), 0)

            self.assertTrue(ledger.is_read_only_turn(state, 'fenced-audit'))
            enqueue.assert_not_called()
            args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
            with mock.patch.object(flush, 'run_codex', return_value=(SUMMARY, None)) as model:
                self.assertEqual(flush.flush_once(args, EVENT, root, state, hook_input=payload), 0)
            model.assert_not_called()
            self.assertEqual(list((root / 'daily').glob('*.md')), [])

    def test_read_only_during_summary_defers_publication_and_keeps_prepared_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            trace = root / 'transcript.jsonl'
            trace.write_text(json.dumps({'role': 'user', 'content': 'Kalıcı karar: haftalık kontrol yapalım.'}), encoding='utf-8')
            payload = {'session_id': 'audit', 'transcript_path': str(trace)}
            args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')

            def summarize(*_args, **_kwargs):
                ledger.mark_read_only_turn(state, 'audit')
                return SUMMARY, None

            with mock.patch.object(flush, 'run_codex', side_effect=summarize) as model, \
                 mock.patch.object(flush, 'maybe_trigger_compile'):
                self.assertEqual(flush.flush_once(args, EVENT, root, state, hook_input=payload), 0)
                self.assertEqual(list((root / 'daily').glob('*.md')), [])
                self.assertEqual(list(state.glob('flush-coverage-*')), [])
                ledger.clear_read_only_turn(state, 'audit')
                self.assertEqual(flush.flush_once(args, EVENT, root, state, hook_input=payload), 0)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(len(list((root / 'daily').glob('*.md'))), 1)

    def test_daily_guard_is_session_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'state'
            ledger.mark_read_only_turn(state, 'audit')
            with mock.patch.object(flush.daily_store, 'publish', return_value=True) as publish:
                with self.assertRaisesRegex(ValueError, 'memory-read-only'):
                    flush.append_daily(root, state, SUMMARY, 'turnend', EVENT, session_id='audit')
                publish.assert_not_called()
                self.assertTrue(flush.append_daily(root, state, SUMMARY, 'turnend', EVENT, session_id='other'))

    def test_companion_guard_preserves_existing_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'state'
            companion = root / '🔮 850-Companion'
            companion.mkdir()
            note = companion / 'Journal.md'
            note.write_text('Existing user note.', encoding='utf-8')
            ledger.mark_read_only_turn(state, 'audit')
            with self.assertRaisesRegex(ValueError, 'memory-read-only'):
                companion_memory.publish(root, state, SUMMARY, EVENT, 'a' * 64, 'audit', frozenset())
            self.assertEqual(note.read_text(encoding='utf-8'), 'Existing user note.')

    def test_attachment_guard_checks_scope_after_model_returns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'state'
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Useful source content.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'

            def summarize(_):
                ledger.mark_read_only_turn(state, 'audit')
                return SUMMARY

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                with self.assertRaisesRegex(ValueError, 'memory-read-only'):
                    attachment_memory.capture_sources([('user', text)], root, EVENT, frozenset(), summarize,
                                                      state_dir=state, session_id='audit')
            self.assertEqual(list((root / attachment_memory.SOURCE_DIR).glob('*.md')), [])


if __name__ == '__main__':
    unittest.main()
