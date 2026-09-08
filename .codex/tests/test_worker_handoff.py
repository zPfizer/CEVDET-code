import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _fixtures
import flush
import worker_supervisor as workers


class WorkerHandoffTests(unittest.TestCase):
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
            self.assertEqual(json.loads(transport.read_text(encoding='utf-8')),
                             {'session_id': 's', 'transcript_path': 'source.jsonl'})
            args = enqueue.call_args.args
            self.assertEqual(args[:2], (state, 'flush'))
            self.assertEqual(args[2]['hook_input'], str(transport))
            self.assertEqual(args[2]['reason'], 'turnend')
            self.assertIsNotNone(dt.datetime.fromisoformat(args[2]['event_iso']).tzinfo)


if __name__ == '__main__':
    unittest.main()
