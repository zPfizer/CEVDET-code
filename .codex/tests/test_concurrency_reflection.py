from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import flush
import hook
import memory_ledger
import worker_supervisor


EVENT = dt.datetime(2026, 9, 8, 1, tzinfo=dt.timezone.utc)


class ReflectionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / '.codex/scripts/.state'
        self.state.mkdir(parents=True)
        self.companion = self.root / '🔮 850-Companion'
        self.companion.mkdir()
        for name in ('Last-Session.md', 'Threads.md', 'Journal.md'):
            (self.companion / name).write_text('# Note\n', encoding='utf-8')
        os.utime(self.companion / 'Last-Session.md', (1, 1))
        self.patches = mock.patch.multiple(hook, STATE_DIR=self.state, MEMORY_DIR=self.companion)
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def request(self, session):
        record = self.state / f'conversation-{hook.session_key(session)}.json'
        previous = json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
        record.write_text(json.dumps({
            'prompt_count': previous.get('prompt_count', 4) + 1,
            'last_session_mtime': 2,
            'meaningful_prompt_seen': True,
        }), encoding='utf-8')
        hook._mark_reflection_if_needed({'session_id': session})

    def has_warning(self):
        return '[Hafıza Uyarısı]' in hook.build_session_context(self.root, self.state, now=EVENT)

    def trace(self, session, messages):
        path = self.root / f'{session}.jsonl'
        path.write_text(''.join(json.dumps({'role': 'user', 'content': text}) + '\n'
                                for text in messages), encoding='utf-8')
        return path

    def finish(self, session, trace, *, before_summary=None, expected=0):
        def summarize(*_args, **_kwargs):
            if before_summary is not None:
                before_summary()
            return '\n'.join(f'## {heading}\n' + (
                'Kalıcı çalışma kararı kayda bağlandı.' if heading == 'Bağlam' else '-'
            ) for heading in flush.EXPECTED_SECTIONS), None

        with mock.patch.object(flush, 'run_codex', side_effect=summarize), \
             mock.patch.object(flush, 'maybe_trigger_compile'), \
             mock.patch.object(worker_supervisor, 'enqueue_flush'):
            result = flush.flush_once(
                argparse.Namespace(reason='sessionend', hook_input=self.root / 'unused'),
                EVENT, self.root, self.state,
                hook_input={'session_id': session, 'transcript_path': str(trace)},
            )
        self.assertEqual(result, expected)
        return result

    def test_finishing_b_does_not_clear_unfinished_a(self):
        self.request('A')
        self.request('B')
        self.finish('B', self.trace('B', ['B için kalıcı çalışma kararı.']))
        self.assertTrue(self.has_warning())
        self.finish('A', self.trace('A', ['A için kalıcı çalışma kararı.']))
        self.assertFalse(self.has_warning())

    def test_old_flush_cannot_clear_a_new_request_for_the_same_session(self):
        self.request('A')
        trace = self.trace('A', ['A için kalıcı çalışma kararı.'])
        self.finish('A', trace, before_summary=lambda: self.request('A'))
        self.assertTrue(self.has_warning())
        self.finish('A', trace)
        self.assertFalse(self.has_warning())

    def test_request_created_during_transcript_index_read_is_preserved(self):
        self.request('A')
        trace = self.trace('A', ['A için kalıcı çalışma kararı.'])
        original = flush.transcript_index.open_or_update

        def read_then_request(*args, **kwargs):
            result = original(*args, **kwargs)
            self.request('A')
            return result

        with mock.patch.object(
            flush.transcript_index,
            'open_or_update',
            side_effect=read_then_request,
        ):
            self.finish('A', trace)
        self.assertTrue(self.has_warning())

    def test_incomplete_tail_does_not_acknowledge_reflection(self):
        self.request('A')
        trace = self.root / 'A.jsonl'
        prefix = json.dumps({'role': 'user', 'content': 'A için kalıcı çalışma kararı.'})
        trace.write_bytes(
            (prefix + '\n{"role":"user","content":"yarım').encode('utf-8')
        )
        self.assertEqual(
            self.finish('A', trace, expected=1),
            1,
        )
        self.assertTrue(self.has_warning())
        trace.write_bytes(
            (prefix + '\n' + json.dumps(
                {'role': 'user', 'content': 'yarım'}, ensure_ascii=False,
            )).encode('utf-8')
        )
        self.assertEqual(self.finish('A', trace), 0)
        self.assertFalse(self.has_warning())

    def test_other_session_snapshot_cannot_satisfy_this_sessions_request(self):
        self.finish('B', self.trace('B', ['B için kalıcı çalışma kararı.']))
        self.request('A')
        self.assertTrue(self.has_warning())
        self.finish('A', self.trace('A', ['A için kalıcı çalışma kararı.']))
        self.assertFalse(self.has_warning())

    def test_first_batch_keeps_warning_until_final_coverage(self):
        self.request('A')
        trace = self.trace('A', ['Birinci kalıcı çalışma kararı.', 'İkinci kalıcı çalışma kararı.'])
        with mock.patch.object(flush, 'MAX_TURNS', 1):
            self.finish('A', trace)
            self.assertTrue(self.has_warning())
            self.finish('A', trace)
        self.assertFalse(self.has_warning())

    def test_request_is_visible_before_the_flush_is_enqueued(self):
        record = self.state / f'conversation-{hook.session_key("A")}.json'
        record.write_text(json.dumps({'prompt_count': 5, 'last_session_mtime': 2}), encoding='utf-8')

        def enqueue(*_args, **_kwargs):
            self.assertTrue(self.has_warning())

        with mock.patch.object(hook, 'enqueue_flush', side_effect=enqueue):
            hook._run_session_end_cleanup({'session_id': 'A'}, state_dir=self.state)

    def test_known_legacy_request_is_acknowledged_after_coverage(self):
        (self.state / 'needs-reflection').write_text(
            f'meaningful-sessionend: session={hook.session_key("A")}\n', encoding='utf-8')
        self.finish('A', self.trace('A', ['A için kalıcı çalışma kararı.']))
        self.assertFalse(self.has_warning())

    def test_unknown_legacy_request_is_preserved(self):
        marker = self.state / 'needs-reflection'
        marker.write_text('legacy owner unknown\n', encoding='utf-8')
        self.finish('A', self.trace('A', ['A için kalıcı çalışma kararı.']))
        self.assertEqual(marker.read_text(encoding='utf-8'), 'legacy owner unknown\n')
        self.assertTrue(self.has_warning())

    def test_ambiguous_legacy_owners_are_not_silently_cleared(self):
        marker = self.state / 'needs-reflection'
        payload = f'session={hook.session_key("A")} session={hook.session_key("B")}\n'
        marker.write_text(payload, encoding='utf-8')
        self.finish('A', self.trace('A', ['A için kalıcı çalışma kararı.']))
        self.assertEqual(marker.read_text(encoding='utf-8'), payload)

    def test_read_only_transition_does_not_acknowledge_pending_memory(self):
        self.request('A')
        self.finish('A', self.trace('A', ['A için kalıcı çalışma kararı.']),
                    before_summary=lambda: memory_ledger.mark_read_only_turn(self.state, 'A'))
        self.assertTrue(self.has_warning())


if __name__ == '__main__':
    unittest.main()
