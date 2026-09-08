import argparse
import datetime as dt
import io
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from _fixtures import CODEX_DIR
import flush
import hook
import memory_ledger


class ConversationContinuityTests(unittest.TestCase):
    def setUp(self):
        self._scope_guard = mock.patch.object(hook, '_validate_hook_scope')
        self._scope_guard.start()
        self.addCleanup(self._scope_guard.stop)

    def test_session_start_wakes_saved_work_without_an_extra_user_message(self):
        import worker_supervisor
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            worker_supervisor.enqueue_job(state, 'flush', {'example': True},
                                          start_supervisor=False, now=100)
            output = io.StringIO()
            with (mock.patch.object(hook, 'VAULT_ROOT', vault),
                  mock.patch.object(hook, 'STATE_DIR', state),
                  mock.patch.object(hook, 'ensure_supervisor') as wake,
                  mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'reopened'}))),
                  mock.patch.object(sys, 'stdout', output)):
                result = hook.main(['session-start', '--strict'])
            self.assertEqual(result, 0)
            wake.assert_called_once_with(state, vault_root=vault)
            self.assertIn('Hafıza Devamlılığı', json.loads(output.getvalue())['hookSpecificOutput']['additionalContext'])

    def test_session_start_preserves_read_only_scope_without_waking_memory_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            memory_ledger.mark_read_only_turn(state, 'audit')
            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'build_session_context', return_value=''),
                mock.patch.object(hook, 'inspect_worker_queue') as inspect,
                mock.patch.object(hook, 'ensure_supervisor') as wake,
                mock.patch.object(flush, 'maybe_trigger_compile') as compile_memory,
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'audit'}))),
                mock.patch.object(sys, 'stdout', output),
            ):
                result = hook.main(['session-start', '--strict'])

            context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
            marked = memory_ledger.is_read_only_turn(state, 'audit')

        self.assertEqual(result, 0)
        self.assertTrue(marked)
        self.assertIn('Salt okunur kapsam korunuyor', context)
        inspect.assert_not_called()
        wake.assert_not_called()
        compile_memory.assert_not_called()

    def test_privacy_request_is_read_before_long_turn_is_shortened(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            transcript = vault / 'source.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content':
                'Bunu kaydetme. ' + 'Geçici ayrıntı. ' * 3000}), encoding='utf-8')
            payload = vault / 'input.json'
            payload.write_text(json.dumps({'session_id': 'long-private',
                'transcript_path': str(transcript)}), encoding='utf-8')
            summary = '\n'.join(f'## {s}\nGeçici ayrıntı.' for s in flush.EXPECTED_SECTIONS)
            with mock.patch.object(flush, 'run_codex', return_value=(summary, None)) as model:
                result = flush.flush_once(argparse.Namespace(hook_input=payload, reason='turnend'),
                    dt.datetime.fromisoformat('2026-09-05T10:00:00+03:00'), vault, state)
            self.assertEqual(result, 0)
            self.assertEqual(model.call_count, 0)
            self.assertFalse((vault / 'daily/2026-09-05.md').exists())

    def test_flush_respects_control_outside_a_quoted_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            transcript = vault / 'source.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content':
                '> Makaledeki örnek cümle.\nBunu kaydetme.'}), encoding='utf-8')
            payload = vault / 'input.json'
            payload.write_text(json.dumps({'session_id': 'quoted-boundary',
                'transcript_path': str(transcript)}), encoding='utf-8')
            with mock.patch.object(flush, 'run_codex') as model:
                result = flush.flush_once(argparse.Namespace(hook_input=payload, reason='turnend'),
                    dt.datetime.fromisoformat('2026-09-05T10:00:00+03:00'), vault, state)
            self.assertEqual(result, 0)
            model.assert_not_called()
            self.assertFalse((vault / 'daily/2026-09-05.md').exists())

    def test_old_short_conversation_noop_does_not_prevent_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            text = 'Poyraz hazırlığı 08:10 olacak.'
            transcript = vault / 'source.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content': text}), encoding='utf-8')
            payload = vault / 'input.json'
            payload.write_text(json.dumps({'session_id': 'old-short',
                                           'transcript_path': str(transcript)}), encoding='utf-8')
            rendered, _ = flush.format_turns([('user', text)])
            digest = hashlib.sha256(rendered.encode('utf-8')).hexdigest()
            flush._session_state_path(state, 'old-short').write_text(json.dumps({
                'receipts': {'old': {'status': 'ok', 'detail': 'below-minimum-turns',
                                    'reason': 'precompact', 'transcript_digest': digest}}
            }), encoding='utf-8')
            summary = '\n'.join(f'## {s}\n{text}' for s in flush.EXPECTED_SECTIONS)
            with mock.patch.object(flush, 'run_codex', return_value=(summary, None)), mock.patch.object(flush, 'maybe_trigger_compile'):
                result = flush.flush_once(argparse.Namespace(hook_input=payload, reason='turnend'),
                    dt.datetime.fromisoformat('2026-09-05T10:00:00+03:00'), vault, state)
            daily = (vault / 'daily/2026-09-05.md').read_text(encoding='utf-8')
        self.assertEqual(result, 0)
        self.assertIn(text, daily)

    def test_parallel_sessions_keep_both_contributions(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            inputs = []
            for name in ('birinci-katki', 'ikinci-katki'):
                transcript = vault / f'{name}.jsonl'
                transcript.write_text(json.dumps({'role': 'user', 'content': name}), encoding='utf-8')
                payload = vault / f'{name}.json'
                payload.write_text(json.dumps({'session_id': name, 'transcript_path': str(transcript)}), encoding='utf-8')
                inputs.append(argparse.Namespace(hook_input=payload, reason='turnend'))
            barrier = Barrier(2)

            def summarize(prompt, _vault):
                name = 'birinci-katki' if 'birinci-katki' in prompt else 'ikinci-katki'
                barrier.wait(timeout=10)
                return '\n'.join(f'## {s}\n{name if s == "Bağlam" else "-"}' for s in flush.EXPECTED_SECTIONS), None

            def capture(args):
                return flush.flush_once(args, dt.datetime.fromisoformat('2026-09-05T10:00:00+03:00'), vault, state)

            with mock.patch.object(flush, 'run_codex', side_effect=summarize), mock.patch.object(flush, 'maybe_trigger_compile'):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(capture, inputs))
            content = (vault / 'daily/2026-09-05.md').read_text(encoding='utf-8')
        self.assertEqual(results, [0, 0])
        self.assertEqual(content.count('birinci-katki'), 1)
        self.assertEqual(content.count('ikinci-katki'), 1)

    def test_short_conversation_is_saved_once_across_lifecycle_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            transcript = vault / 'source.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content':
                'Poyraz kararı: 08:10; gerekçe sakin hazırlık.'}), encoding='utf-8')
            payload = vault / 'input.json'
            payload.write_text(json.dumps({'session_id': 'short',
                'transcript_path': str(transcript)}), encoding='utf-8')
            summary = '\n'.join(f'## {s}\nPoyraz 08:10 kararı.' for s in flush.EXPECTED_SECTIONS)
            with (
                mock.patch.object(flush, 'run_codex', return_value=(summary, None)) as runner,
                mock.patch.object(flush, 'maybe_trigger_compile'),
            ):
                for reason in ('precompact', 'turnend', 'sessionend'):
                    self.assertEqual(flush.flush_once(
                        argparse.Namespace(hook_input=payload, reason=reason),
                        dt.datetime.fromisoformat('2026-09-05T10:00:00+03:00'), vault, state), 0)
            daily = (vault / 'daily/2026-09-05.md').read_text(encoding='utf-8')
        self.assertEqual(daily.count('## Bağlam'), 1)
        self.assertEqual(runner.call_count, 1)

    def test_turn_end_queues_capture_without_continuing_the_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            from test_profile_guard import seed_profile
            seed_profile(vault)
            state = vault / '.codex/scripts/.state'
            payload = {'session_id': 'turn-end', 'transcript_path': str(vault / 'source.jsonl'),
                       'last_assistant_message': 'Karar ve gerekçe açıklandı.', 'stop_hook_active': False}
            real_enqueue = hook.enqueue_flush
            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'enqueue_flush', side_effect=lambda p, r:
                    real_enqueue(p, r, popen_factory=mock.Mock())),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, 'stdout', output),
            ):
                result = hook.main(['turn-end', '--strict'])
            jobs = list((state / 'worker-jobs/pending').glob('*.json'))
            queued = json.loads(jobs[0].read_text(encoding='utf-8')) if jobs else {}
        self.assertEqual(result, 0)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(queued['payload']['reason'], 'turnend')
        self.assertNotEqual(json.loads(output.getvalue()).get('decision'), 'block')

    def test_turn_end_respects_session_only_and_reports_queue_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            from test_profile_guard import seed_profile
            seed_profile(vault)
            memory_ledger.mark_session_only(state, 'private')
            for session_id in ('private', 'ordinary'):
                output = io.StringIO()
                payload = {'session_id': session_id, 'transcript_path': 'synthetic.jsonl'}
                with (
                    mock.patch.object(hook, 'VAULT_ROOT', vault),
                    mock.patch.object(hook, 'STATE_DIR', state),
                    mock.patch.object(hook, 'enqueue_flush', side_effect=OSError('queue unavailable')) as enqueue,
                    mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, 'stdout', output),
                ):
                    result = hook.main(['turn-end', '--strict'])
                message = json.loads(output.getvalue())
                if session_id == 'private':
                    self.assertEqual(result, 0)
                    enqueue.assert_not_called()
                    self.assertNotIn('systemMessage', message)
                else:
                    self.assertEqual(result, 1)
                    self.assertIn('systemMessage', message)

    def test_turn_end_keeps_read_only_scope_until_explicit_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            from test_profile_guard import seed_profile
            seed_profile(vault)
            state = vault / '.codex/scripts/.state'
            memory_ledger.mark_read_only_turn(state, 'audit')
            payload = {'session_id': 'audit', 'transcript_path': str(vault / 'source.jsonl')}
            results = []
            for _ in range(2):
                output = io.StringIO()
                with (
                    mock.patch.object(hook, 'VAULT_ROOT', vault),
                    mock.patch.object(hook, 'STATE_DIR', state),
                    mock.patch.object(hook, 'enqueue_flush') as enqueue,
                    mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, 'stdout', output),
                ):
                    result = hook.main(['turn-end', '--strict'])
                results.append((result, json.loads(output.getvalue()), enqueue.call_count,
                                memory_ledger.is_read_only_turn(state, 'audit')))
        # Salt okunur kapsam tur sonuyla kalkmaz; sıradan takip turu da kayıt başlatmaz.
        self.assertEqual(results[0], (0, {'continue': True}, 0, True))
        self.assertEqual(results[1], (0, {'continue': True}, 0, True))

    def test_explicit_write_prompt_reopens_read_only_scope(self):
        self._check_explicit_write_prompt_reopens_read_only_scope('Ok yap.')

    def test_natural_ordered_write_prompt_reopens_read_only_scope(self):
        self._check_explicit_write_prompt_reopens_read_only_scope('Sırayla hepsini yap')

    def test_short_ordered_write_prompt_reopens_read_only_scope(self):
        self._check_explicit_write_prompt_reopens_read_only_scope('Sırayla yap')

    def test_quoted_read_only_rule_does_not_reopen_scope(self):
        for prompt in ('"Sadece incele" uygula.', '"Dosyaları değiştirme" uygula.'):
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                state = vault / '.codex/scripts/.state'
                memory_ledger.mark_read_only_turn(state, 'quoted-rule')
                payload = {'session_id': 'quoted-rule', 'prompt': prompt}
                with (
                    mock.patch.object(hook, 'VAULT_ROOT', vault),
                    mock.patch.object(hook, 'STATE_DIR', state),
                    mock.patch.object(hook, 'handle_user_prompt', return_value=''),
                    mock.patch.object(hook, 'enqueue_flush') as enqueue,
                    mock.patch.object(hook, 'record_hook_runtime'),
                    mock.patch.object(hook, 'clear_hook_health'),
                    mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, 'stdout', io.StringIO()),
                ):
                    self.assertEqual(hook.main(['user-prompt']), 0)
                self.assertTrue(memory_ledger.is_read_only_turn(state, 'quoted-rule'))
                enqueue.assert_not_called()

    def test_full_message_fences_are_read_only_but_embedded_or_quoted_text_is_not(self):
        fenced = (
            '```text\nAudit only. Do not modify files or settings.\n```',
            '```text\r\nAudit only. Do not modify files or settings.\r\n```',
            '~~~\r\nAudit only. Do not modify files or settings.\r\n~~~',
        )
        for prompt in fenced:
            with self.subTest(prompt=prompt):
                self.assertTrue(memory_ledger.is_read_only_request(prompt))
                self.assertEqual(memory_ledger.memory_directive(prompt).kind, 'read-only')

        ordinary = (
            'Review this:\n```text\nDo not modify files or settings.\n```',
            '```text\n"Do not modify files or settings."\n```',
            '~~~\r\n```text\r\nDo not modify files or settings.\r\n```\r\n~~~',
            '```text\n```text\nDo not modify files or settings.\n```\n```',
            '"Do not modify files or settings." Explain this.',
            '```text\nOk yap.\n```',
            '~~~\r\nSırayla hepsini yap\r\n~~~',
            '```text\nŞunu unut: Ankara.\n```',
            '```text\n"Ok yap."\n```',
            '~~~\r\n"Şunu unut: Ankara."\r\n~~~',
        )
        for prompt in ordinary:
            with self.subTest(prompt=prompt):
                self.assertFalse(memory_ledger.is_read_only_request(prompt))
                self.assertEqual(memory_ledger.memory_directive(prompt).kind, 'ordinary')

    def test_indented_fence_closings_preserve_inside_and_outside_restrictions(self):
        for fence in ('```', '~~~', '````'):
            for width in (1, 2, 3):
                closing = ' ' * width + fence
                restriction = 'Do not modify files or settings.'
                for newline in ('\n', '\r\n'):
                    single = newline.join((fence + 'text', restriction, closing))
                    multiple = newline.join((fence + 'text', 'quoted sample', closing,
                                             restriction, fence + 'text', 'second sample', fence))
                    for prompt in (single, multiple):
                        with self.subTest(fence=fence, width=width, newline=newline, prompt=prompt):
                            self.assertTrue(memory_ledger.is_read_only_request(prompt))
                            self.assertEqual(memory_ledger.memory_directive(prompt).kind, 'read-only')

    def test_embedded_multiline_fence_remains_data_for_memory_controls(self):
        for marker in ('```', '~~~'):
            for quoted in ('Bu konuşmada kalsın', 'Bunu kaydetme', 'Şunu unut: Ankara.',
                           'Bunu düzelt', 'Do not modify files or settings.'):
                prompt = f'Example: {marker}text\n{quoted}\n{marker}'
                with self.subTest(marker=marker, quoted=quoted):
                    self.assertEqual(memory_ledger.memory_directive(prompt).kind, 'ordinary')
                    self.assertFalse(memory_ledger.is_explicit_write_intent(prompt))

    def test_separate_fenced_samples_leave_outside_read_only_text_unquoted(self):
        prompts = (
            '```text\nfirst quoted sample\n```\nDo not modify files or settings.\n'
            '```text\nsecond quoted sample\n```',
            '```text\nAudit only. Do not modify files or settings.\n````',
            '~~~text\r\nfirst quoted sample\r\n~~~\r\nDo not modify files or settings.\r\n'
            '~~~text\r\nsecond quoted sample\r\n~~~',
            '````text\nfirst quoted sample\n````\nDo not modify files or settings.\n'
            '````text\nsecond quoted sample\n````',
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(memory_ledger.is_read_only_request(prompt))
                self.assertEqual(memory_ledger.memory_directive(prompt).kind, 'read-only')

        shorter_inner_close = (
            '````text\nfirst quoted sample\n```\nDo not modify files or settings.\n````'
        )
        self.assertFalse(memory_ledger.is_read_only_request(shorter_inner_close))
        self.assertEqual(memory_ledger.memory_directive(shorter_inner_close).kind, 'ordinary')

    def test_full_message_fenced_read_only_scope_blocks_all_lifecycle_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            from test_profile_guard import seed_profile
            seed_profile(vault)
            state = vault / '.codex/scripts/.state'
            payload = {
                'session_id': 'fenced-audit',
                'prompt': '```text\r\nAudit only. Do not modify files or settings.\r\n```',
                'transcript_path': str(vault / 'source.jsonl'),
                'reason': 'other',
            }

            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'handle_user_prompt', return_value=''),
                mock.patch.object(hook, 'enqueue_flush') as prompt_enqueue,
                mock.patch.object(hook, 'record_hook_runtime'),
                mock.patch.object(hook, 'clear_hook_health'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, 'stdout', io.StringIO()),
            ):
                self.assertEqual(hook.main(['user-prompt']), 0)

            self.assertTrue(memory_ledger.is_read_only_turn(state, 'fenced-audit'))
            prompt_enqueue.assert_not_called()

            for event in ('pre-compact', 'turn-end', 'session-end'):
                with self.subTest(event=event):
                    output = io.StringIO()
                    with (
                        mock.patch.object(hook, 'VAULT_ROOT', vault),
                        mock.patch.object(hook, 'STATE_DIR', state),
                        mock.patch.object(hook, 'enqueue_flush') as enqueue,
                        mock.patch.object(hook, '_mark_reflection_if_needed') as reflect,
                        mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                        mock.patch.object(sys, 'stdout', output),
                    ):
                        result = hook.main([event, '--strict'])
                    self.assertEqual(result, 0)
                    enqueue.assert_not_called()
                    reflect.assert_not_called()

    def _check_explicit_write_prompt_reopens_read_only_scope(self, prompt):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            from test_profile_guard import seed_profile
            seed_profile(vault)
            state = vault / '.codex/scripts/.state'
            memory_ledger.mark_read_only_turn(state, 'audit')
            payload = {
                'session_id': 'audit',
                'prompt': prompt,
                'transcript_path': str(vault / 'source.jsonl'),
            }
            followup = dict(payload, prompt='devam')
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'handle_user_prompt', return_value=''),
                mock.patch.object(hook, 'enqueue_flush') as followup_enqueue,
                mock.patch.object(hook, 'record_hook_runtime'),
                mock.patch.object(hook, 'clear_hook_health'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(followup))),
                mock.patch.object(sys, 'stdout', io.StringIO()),
            ):
                self.assertEqual(hook.main(['user-prompt']), 0)
            self.assertTrue(memory_ledger.is_read_only_turn(state, 'audit'))
            followup_enqueue.assert_not_called()

            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'handle_user_prompt', return_value=''),
                mock.patch.object(hook, 'enqueue_flush') as enqueue,
                mock.patch.object(hook, 'record_hook_runtime'),
                mock.patch.object(hook, 'clear_hook_health'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, 'stdout', io.StringIO()),
            ):
                self.assertEqual(hook.main(['user-prompt']), 0)
            self.assertFalse(memory_ledger.is_read_only_turn(state, 'audit'))

            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'enqueue_flush') as enqueue_end,
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, 'stdout', output),
            ):
                self.assertEqual(hook.main(['turn-end', '--strict']), 0)

        enqueue.assert_not_called()
        enqueue_end.assert_called_once_with(payload, 'turnend')

    def test_read_only_scope_blocks_mixed_forget_write_but_keeps_direct_forget_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            from test_profile_guard import seed_profile
            seed_profile(vault)
            state = vault / '.codex/scripts/.state'
            memory_ledger.mark_read_only_turn(state, 'audit')
            mixed = {
                'session_id': 'audit',
                'prompt': 'Şunu unut: Ankara. Do not modify files or settings.',
            }
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'handle_user_prompt', return_value=''),
                mock.patch.object(hook, 'suppress_derived_memory') as suppress,
                mock.patch.object(hook, 'record_hook_runtime'),
                mock.patch.object(hook, 'clear_hook_health'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(mixed))),
                mock.patch.object(sys, 'stdout', io.StringIO()),
            ):
                self.assertEqual(hook.main(['user-prompt']), 0)
            suppress.assert_not_called()
            self.assertTrue(memory_ledger.is_read_only_turn(state, 'audit'))

            direct = dict(mixed, prompt='Şunu unut: Ankara.')
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'handle_user_prompt', return_value=''),
                mock.patch.object(hook, 'suppress_derived_memory') as direct_suppress,
                mock.patch.object(hook, 'record_hook_runtime'),
                mock.patch.object(hook, 'clear_hook_health'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(direct))),
                mock.patch.object(sys, 'stdout', io.StringIO()),
            ):
                self.assertEqual(hook.main(['user-prompt']), 0)

            marked_after_direct = memory_ledger.is_read_only_turn(state, 'audit')

        direct_suppress.assert_called_once()
        self.assertTrue(marked_after_direct)

    def test_pre_compact_and_session_end_skip_flush_for_read_only_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            memory_ledger.mark_read_only_turn(state, 'audit')
            payload = {'session_id': 'audit', 'transcript_path': 'synthetic.jsonl', 'reason': 'other'}
            with (
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'enqueue_flush') as enqueue,
                mock.patch.object(hook, '_mark_reflection_if_needed') as reflect,
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, 'stdout', io.StringIO()),
            ):
                compact = hook.main(['pre-compact', '--strict'])
                still_marked = memory_ledger.is_read_only_turn(state, 'audit')
            with (
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'enqueue_flush') as enqueue_end,
                mock.patch.object(hook, '_mark_reflection_if_needed') as reflect_end,
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, 'stdout', io.StringIO()),
            ):
                ended = hook.main(['session-end', '--strict'])
            marked_after_end = memory_ledger.is_read_only_turn(state, 'audit')
        self.assertEqual((compact, ended), (0, 0))
        enqueue.assert_not_called()
        reflect.assert_not_called()
        enqueue_end.assert_not_called()
        reflect_end.assert_not_called()
        self.assertTrue(still_marked)
        self.assertTrue(marked_after_end)


if __name__ == '__main__':
    unittest.main()
