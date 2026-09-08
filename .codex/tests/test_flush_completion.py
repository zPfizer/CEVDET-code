import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import attachment_memory
import flush
from knowledge_schema import parse_frontmatter
from memory_ledger import persistent_turns
import worker_supervisor


class FlushCompletionTests(unittest.TestCase):
    def test_incomplete_tail_publishes_prefix_then_retries_completed_tail_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            state.mkdir(parents=True)
            transcript = vault / 'rollout.jsonl'
            prefix = json.dumps({'role': 'user', 'content': 'complete prefix'})
            transcript.write_bytes(
                (prefix + '\n{"role":"user","content":"unfinished').encode('utf-8')
            )
            payload = {
                'session_id': 'partial-tail-retry',
                'transcript_path': str(transcript),
            }
            args = argparse.Namespace(hook_input=state / 'unused.json', reason='turnend')
            summaries = [
                '\n'.join(f'## {section}\nprefix' for section in flush.EXPECTED_SECTIONS),
                '\n'.join(f'## {section}\ntail' for section in flush.EXPECTED_SECTIONS),
            ]

            with (
                mock.patch.object(flush, 'run_codex', side_effect=[(summaries[0], None), (summaries[1], None)]) as model,
                mock.patch.object(flush, 'maybe_trigger_compile', return_value=False),
            ):
                self.assertEqual(
                    flush.flush_once(
                        args,
                        dt.datetime.fromisoformat('2026-09-08T03:00:00+03:00'),
                        vault,
                        state,
                        hook_input=payload,
                    ),
                    1,
                )
                coverage_path = state / f"flush-coverage-{flush._session_key('partial-tail-retry')}.json"
                self.assertEqual(json.loads(coverage_path.read_text(encoding='utf-8'))['count'], 1)

                transcript.write_bytes(
                    (prefix + '\n' + json.dumps(
                        {'role': 'user', 'content': 'unfinished'}, ensure_ascii=False,
                    )).encode('utf-8')
                )
                self.assertEqual(
                    flush.flush_once(
                        args,
                        dt.datetime.fromisoformat('2026-09-08T03:01:00+03:00'),
                        vault,
                        state,
                        hook_input=payload,
                    ),
                    0,
                )

            daily = (vault / 'daily/2026-09-08.md').read_text(encoding='utf-8')
            self.assertEqual(model.call_count, 2)
            self.assertEqual(daily.count('<!-- flush:'), 2)
            self.assertIn('\nprefix', daily)
            self.assertIn('\ntail', daily)
            self.assertIn('complete prefix', model.call_args_list[0].args[0])
            self.assertIn('unfinished', model.call_args_list[1].args[0])
            self.assertEqual(json.loads(coverage_path.read_text(encoding='utf-8'))['count'], 2)

    def test_retry_next_day_keeps_original_date_and_prepared_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            state.mkdir(parents=True)
            transcript = state / 'rollout.jsonl'
            transcript.write_text(json.dumps({
                'type': 'event_msg',
                'payload': {'type': 'user_message', 'message': 'Plan pazartesi.'},
            }), encoding='utf-8')
            payload = state / 'hookin-test.json'
            payload.write_text(json.dumps({
                'session_id': 'retry-next-day', 'transcript_path': str(transcript),
            }), encoding='utf-8')
            args = argparse.Namespace(hook_input=payload, reason='turnend')
            original_time = dt.datetime.fromisoformat('2026-09-05T23:59:00+03:00')
            retry_time = original_time + dt.timedelta(minutes=2)
            summary = '\n'.join(f'## {section}\nPlan pazartesi.' for section in flush.EXPECTED_SECTIONS)
            write_state = flush._write_flush_state

            def fail_completion(*values, **kwargs):
                if values[3:5] == ('ok', 'appended'):
                    raise OSError('completion interrupted')
                return write_state(*values, **kwargs)

            with (
                mock.patch.object(flush, 'run_codex', return_value=(summary, None)) as model,
                mock.patch.object(flush, 'maybe_trigger_compile', return_value=False) as trigger,
            ):
                with mock.patch.object(flush, '_write_flush_state', side_effect=fail_completion):
                    self.assertEqual(flush.flush_once(args, original_time, vault, state), 1)
                receipt_path = flush._session_state_path(state, 'retry-next-day')
                receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                self.assertEqual(receipt['status'], 'prepared')
                summary_path = flush._prepared_summary_path(state, receipt['idempotency_key'])
                self.assertTrue(summary_path.is_file())
                trigger.assert_not_called()
                self.assertEqual(flush.flush_once(args, retry_time, vault, state), 0)
                model.assert_called_once()
                trigger.assert_called_once_with(vault, original_time)
                self.assertFalse(summary_path.exists())
            content = (vault / 'daily/2026-09-05.md').read_text(encoding='utf-8')
            self.assertEqual(content.count('<!-- flush:'), 1)
            self.assertFalse((vault / 'daily/2026-09-06.md').exists())
            final = json.loads(receipt_path.read_text(encoding='utf-8'))
            self.assertEqual(final['status'], 'ok')

    def test_attachment_mapping_retries_note_gap_without_plaintext_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            home = vault / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Retryable attachment.', encoding='utf-8')
            envelope = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            transcript = vault / 'rollout.jsonl'
            transcript.write_text(json.dumps({
                'type': 'response_item',
                'payload': {'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': envelope},
                ]},
            }), encoding='utf-8')
            payload = {'session_id': 'attachment-gap', 'transcript_path': str(transcript)}
            args = argparse.Namespace(hook_input=vault / 'unused.json', reason='turnend')
            summary = '\n'.join(f'## {section}\nRetry summary.' for section in flush.EXPECTED_SECTIONS)
            calls = []
            real_write = attachment_memory.atomic_write_text
            failed = False

            def fail_note(path, value, **kwargs):
                nonlocal failed
                if not failed and Path(path).name.startswith('kaynak-'):
                    failed = True
                    raise OSError('note interrupted')
                return real_write(path, value, **kwargs)

            def run(prompt, _root, **_kwargs):
                calls.append(prompt)
                return summary, None

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(flush, 'run_codex', side_effect=run), \
                 mock.patch.object(flush, 'maybe_trigger_compile'), \
                 mock.patch.object(attachment_memory, 'atomic_write_text', side_effect=fail_note):
                self.assertEqual(flush.flush_once(args, dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc), vault, state, hook_input=payload), 1)
                mapping = attachment_memory._mapping_path(
                    state,
                    '11111111-1111-4111-8111-111111111111',
                    attachment_memory._attachment_digest(envelope.rstrip()),
                )
                mapping_text = mapping.read_text(encoding='utf-8')
                self.assertNotIn('Retryable attachment.', mapping_text)
                self.assertNotIn('summary', mapping_text)
                self.assertNotIn('versions', mapping_text)
                self.assertEqual(flush.flush_once(args, dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc), vault, state, hook_input=payload), 0)

            self.assertEqual(len(calls), 3)
            notes = list((vault / '📥 000-Inbox/Paylaşılan Kaynaklar').glob('*.md'))
            self.assertEqual(len(notes), 1)
            metadata = parse_frontmatter(notes[0].read_text(encoding='utf-8'))
            self.assertEqual(metadata['source_type'], 'local-text-attachment')
            self.assertTrue(metadata['dedupe_key'])
            self.assertEqual(metadata['updated'], '2026-09-05')
            self.assertIn('Üst indeks:', notes[0].read_text(encoding='utf-8'))
            self.assertEqual(len(list((vault / 'daily').glob('*.md'))), 1)

    def test_completed_newline_free_record_is_not_republished_after_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            transcript = vault / 'rollout.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content': 'old-prefix-unique'}), encoding='utf-8')
            payload = {'session_id': 'newline-completion', 'transcript_path': str(transcript)}
            args = argparse.Namespace(reason='turnend')
            summary = '\n'.join(f'## {section}\nOrtak özet.' for section in flush.EXPECTED_SECTIONS)
            now = dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)
            with mock.patch.object(flush, 'run_codex', return_value=(summary, None)) as model, \
                 mock.patch.object(flush, 'maybe_trigger_compile'):
                self.assertEqual(flush.flush_once(args, now, vault, state, hook_input=payload), 0)
                with transcript.open('a', encoding='utf-8') as output:
                    output.write('\n' + json.dumps({'role': 'user', 'content': 'new-tail-unique'}) + '\n')
                self.assertEqual(flush.flush_once(args, now, vault, state, hook_input=payload), 0)
            self.assertEqual(model.call_count, 2)
            self.assertIn('new-tail-unique', model.call_args_list[1].args[0])
            self.assertNotIn('old-prefix-unique', model.call_args_list[1].args[0])

    def test_legacy_coverage_and_batch_migrate_before_prepared_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            state.mkdir(parents=True)
            transcript = state / 'rollout.jsonl'
            transcript.write_text(
                '\n'.join(
                    json.dumps({'role': 'user', 'content': f'Legacy turn {index:02}'})
                    for index in range(65)
                ) + '\n',
                encoding='utf-8',
            )
            payload = {'session_id': 'legacy-progress', 'transcript_path': str(transcript)}
            args = argparse.Namespace(hook_input=state / 'unused', reason='turnend')
            event = dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc)
            turns, _ = flush.read_transcript_with_coverage(
                transcript, max_bytes=None, max_turns=None,
            )
            turns = [
                (role, flush.sanitize_text(text, max_chars=max(1, len(text)))[0])
                for role, text in persistent_turns(turns)
            ]
            chunks = [
                (role, text[offset:offset + 12_000])
                for role, text in turns
                for offset in range(0, len(text), 12_000)
            ]

            def legacy_digest(end):
                return hashlib.sha256(
                    json.dumps(chunks[:end], ensure_ascii=False).encode('utf-8')
                ).hexdigest()

            session_key = flush._session_key(payload['session_id'])
            coverage_path = state / f'flush-coverage-{session_key}.json'
            batch_path = state / f'flush-batch-{session_key}.json'
            coverage_path.write_text(
                json.dumps({'count': 30, 'digest': legacy_digest(30)}),
                encoding='utf-8',
            )
            batch_path.write_text(
                json.dumps({
                    'start': 30,
                    'end': 60,
                    'digest': legacy_digest(60),
                    'p07_receipt': 'preserve-me',
                }),
                encoding='utf-8',
            )
            selected, _ = flush.format_turns(chunks[30:60])
            transcript_digest = hashlib.sha256(selected.encode('utf-8')).hexdigest()
            summary = '\n'.join(
                f'## {section}\nLegacy prepared.' for section in flush.EXPECTED_SECTIONS
            )
            summary_digest = hashlib.sha256(summary.encode('utf-8')).hexdigest()
            idempotency_key = flush._flush_idempotency_key(
                payload['session_id'], 'turnend', transcript_digest, summary_digest,
            )
            summary_path = flush._prepared_summary_path(state, idempotency_key)
            flush.atomic_write_text(summary_path, summary)
            flush._write_flush_state(
                state,
                payload['session_id'],
                event.timestamp(),
                'prepared',
                'ready-to-append',
                reason='turnend',
                transcript_digest=transcript_digest,
                summary_digest=summary_digest,
                idempotency_key=idempotency_key,
                daily_file='2026-09-08.md',
                event_iso=event.isoformat(),
            )

            with mock.patch.object(flush, 'run_codex') as model, \
                 mock.patch.object(flush, 'append_daily', side_effect=OSError('append interrupted')), \
                 mock.patch.object(flush, 'maybe_trigger_compile'), \
                 mock.patch.object(worker_supervisor, 'enqueue_flush'):
                first = flush.flush_once(args, event, root, state, hook_input=payload)
            migrated_batch = json.loads(batch_path.read_text(encoding='utf-8'))
            migrated_coverage = json.loads(coverage_path.read_text(encoding='utf-8'))
            self.assertEqual(first, 1)
            model.assert_not_called()
            self.assertEqual(migrated_batch['p07_receipt'], 'preserve-me')
            self.assertNotEqual(migrated_batch['digest'], legacy_digest(60))
            self.assertEqual(migrated_coverage['schema_version'], flush.transcript_index.COVERAGE_SCHEMA_VERSION)
            self.assertEqual(migrated_coverage['count'], 30)

            with mock.patch.object(flush, 'run_codex') as retry_model, \
                 mock.patch.object(flush, 'maybe_trigger_compile'), \
                 mock.patch.object(worker_supervisor, 'enqueue_flush'):
                second = flush.flush_once(args, event, root, state, hook_input=payload)
            retry_model.assert_not_called()
            self.assertEqual(second, 0)
            self.assertEqual(json.loads(coverage_path.read_text(encoding='utf-8'))['count'], 60)
            self.assertFalse(batch_path.exists())

    def test_attachment_summary_rechecks_transcript_before_writing_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Attachment content.', encoding='utf-8')
            envelope = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            transcript = root / 'rollout.jsonl'
            transcript.write_text(json.dumps({
                'type': 'response_item',
                'payload': {'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': envelope},
                ]},
            }) + '\n', encoding='utf-8')
            payload = {'session_id': 'attachment-privacy-race', 'transcript_path': str(transcript)}
            args = argparse.Namespace(hook_input=root / 'unused.json', reason='turnend')
            summary = '\n'.join(f'## {section}\nAttachment summary.' for section in flush.EXPECTED_SECTIONS)
            calls = []

            def summarize(_prompt, *_args, **_kwargs):
                calls.append(True)
                transcript.write_text(
                    transcript.read_text(encoding='utf-8')
                    + json.dumps({'role': 'user', 'content': 'Bu konuşmada kalsın.'}) + '\n',
                    encoding='utf-8',
                )
                return summary, None

            with mock.patch.dict(os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(flush, 'run_codex', side_effect=summarize), \
                 mock.patch.object(flush, 'maybe_trigger_compile'), \
                 mock.patch.object(worker_supervisor, 'ensure_supervisor'):
                first = flush.flush_once(
                    args,
                    dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc),
                    root,
                    state,
                    hook_input=payload,
                )
                with mock.patch.object(flush, 'run_codex') as retry_model:
                    second = flush.flush_once(
                        args,
                        dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc),
                        root,
                        state,
                        hook_input=payload,
                    )
            notes = list((root / attachment_memory.SOURCE_DIR).glob('*.md'))

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(calls), 1)
        retry_model.assert_not_called()
        self.assertEqual(notes, [])


if __name__ == '__main__':
    unittest.main()
