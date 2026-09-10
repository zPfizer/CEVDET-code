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

    def test_session_start_surfaces_unresolved_terminal_flush(self):
        import worker_supervisor
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            worker_supervisor.enqueue_job(state, 'flush', {'source': 'retained'},
                                          start_supervisor=False, now=0)
            running, job = worker_supervisor._claim_next_job(state, now=0)
            worker_supervisor._finish_job(
                state, running, job, status='dead-letter',
                terminal_reason='retry-exhausted', now=1,
            )
            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'build_session_context', return_value=''),
                mock.patch.object(flush, 'maybe_trigger_compile'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'reopened'}))),
                mock.patch.object(sys, 'stdout', output),
            ):
                result = hook.main(['session-start', '--strict'])

            context = (
                json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
                if output.getvalue() else ''
            )

        self.assertEqual(result, 0)
        self.assertIn('sonucu doğrulanamadı', context)
        self.assertIn('içeriği kayıp veya bilgi yok sayma', context)

    def test_session_start_surfaces_current_flush_health_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            state.mkdir(parents=True)
            (state / 'health.json').write_text(json.dumps({
                'components': {
                    'flush:session': {
                        'component': 'flush',
                        'status': 'error',
                        'error': 'codex-timeout',
                    },
                },
            }), encoding='utf-8')
            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'build_session_context', return_value=''),
                mock.patch.object(flush, 'maybe_trigger_compile'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'reopened'}))),
                mock.patch.object(sys, 'stdout', output),
            ):
                result = hook.main(['session-start', '--strict'])

            context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']

        self.assertEqual(result, 0)
        self.assertIn('sonucu doğrulanamadı', context)

    def test_session_start_surfaces_quarantined_capture(self):
        import worker_supervisor

        def quarantine_one(state, kind):
            broken = worker_supervisor.enqueue_job(
                state, kind, {}, start_supervisor=False, now=0,
            )
            broken.write_text('{broken', encoding='utf-8')
            replacement = worker_supervisor.enqueue_job(
                state, kind, {}, start_supervisor=False, now=1,
            )
            replacement.unlink()

        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            quarantine_one(state, 'flush')
            quarantine_one(state / 'maintenance', 'maintenance')
            queue = worker_supervisor.inspect_worker_queue(state)
            maintenance = worker_supervisor.inspect_worker_queue(state / 'maintenance')
            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'build_session_context', return_value=''),
                mock.patch.object(flush, 'maybe_trigger_compile'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'reopened'}))),
                mock.patch.object(sys, 'stdout', output),
            ):
                result = hook.main(['session-start', '--strict'])

            context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']

        self.assertEqual(queue['counts']['quarantined'], 1)
        self.assertEqual(maintenance['counts']['quarantined'], 1)
        self.assertEqual(queue['terminal']['unresolved'], 1)
        self.assertEqual(maintenance['terminal']['unresolved'], 1)
        self.assertEqual(result, 0)
        self.assertIn('sonucu doğrulanamadı', context)
        self.assertIn('içeriği kayıp veya bilgi yok sayma', context)

    def test_session_start_keeps_verified_recovered_terminal_flush_quiet(self):
        import worker_supervisor
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            worker_supervisor.enqueue_job(state, 'flush', {'source': 'same'},
                                          start_supervisor=False, now=0)
            succeeded_path, succeeded = worker_supervisor._claim_next_job(state, now=0)
            worker_supervisor._finish_job(state, succeeded_path, succeeded,
                                          status='succeeded', now=1)
            worker_supervisor.enqueue_job(state, 'flush', {'source': 'same'},
                                          start_supervisor=False, now=1)
            dead_path, dead = worker_supervisor._claim_next_job(state, now=1)
            worker_supervisor._finish_job(
                state, dead_path, dead, status='dead-letter',
                terminal_reason='recovered-by-successor', now=2,
            )
            dead_path = state / 'worker-jobs/dead-letter' / dead_path.name
            dead_record = json.loads(dead_path.read_text(encoding='utf-8'))
            dead_record['recovery_job_id'] = succeeded['job_id']
            worker_supervisor.atomic_write_json(dead_path, dead_record)
            inspection = worker_supervisor.inspect_worker_queue(state)
            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'build_session_context', return_value=''),
                mock.patch.object(flush, 'maybe_trigger_compile'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'reopened'}))),
                mock.patch.object(sys, 'stdout', output),
            ):
                result = hook.main(['session-start', '--strict'])

            context = (
                json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
                if output.getvalue() else ''
            )

        self.assertEqual(inspection['terminal'], {'recovered': 1, 'unresolved': 0})
        self.assertEqual(result, 0)
        self.assertNotIn('sonucu doğrulanamadı', context)

    def test_session_start_uses_current_maintenance_health_without_stale_warning(self):
        import worker_supervisor
        from state_store import clear_health
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            maintenance = state / 'maintenance'
            worker_supervisor.enqueue_job(maintenance, 'maintenance', {},
                                          start_supervisor=False, now=0)
            running, job = worker_supervisor._claim_next_job(maintenance, now=0)
            worker_supervisor._finish_job(
                maintenance, running, job, status='dead-letter',
                error='LockUnavailable', terminal_reason='retry-exhausted', now=1,
            )
            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'build_session_context', return_value=''),
                mock.patch.object(flush, 'maybe_trigger_compile'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'reopened'}))),
                mock.patch.object(sys, 'stdout', output),
            ):
                result = hook.main(['session-start', '--strict'])

            context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']

            self.assertEqual(result, 0)
            self.assertEqual(context.count('Bilgi düzenleme henüz tamamlanamadı'), 1)
            self.assertNotIn('Önceki arka plan kayıtlarından birinin sonucu doğrulanamadı', context)

            worker_supervisor.enqueue_maintenance(
                state, vault_root=vault, start_supervisor=False,
            )
            later_running, later_job = worker_supervisor._claim_next_job(maintenance, now=2)
            worker_supervisor._finish_job(
                maintenance, later_running, later_job, status='succeeded', now=3,
            )
            clear_health(state, component='compile')
            self.assertEqual(
                worker_supervisor.inspect_worker_queue(maintenance)['counts']['dead-letter'], 1,
            )

            later_output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'build_session_context', return_value=''),
                mock.patch.object(flush, 'maybe_trigger_compile'),
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': 'reopened-again'}))),
                mock.patch.object(sys, 'stdout', later_output),
            ):
                later_result = hook.main(['session-start', '--strict'])

            later_context = (
                json.loads(later_output.getvalue())['hookSpecificOutput']['additionalContext']
                if later_output.getvalue() else ''
            )

        self.assertEqual(later_result, 0)
        self.assertNotIn('Bilgi düzenleme henüz tamamlanamadı', later_context)
        self.assertNotIn('Önceki arka plan kayıtlarından birinin sonucu doğrulanamadı', later_context)

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

    def test_polite_do_not_save_excludes_previous_exchange_from_flush_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            transcript = vault / 'source.jsonl'
            transcript.write_text('\n'.join(json.dumps(record) for record in (
                {'role': 'user', 'content': 'Önceki karar kalıcı olmamalı.'},
                {'role': 'assistant', 'content': 'Önceki yanıt da dışarıda kalmalı.'},
                {'role': 'user', 'content': 'Bunu kaydetme - lütfen.'},
                {'role': 'user', 'content': 'Yeni kalıcı karar.'},
            )) + '\n', encoding='utf-8')
            payload = vault / 'input.json'
            payload.write_text(json.dumps({
                'session_id': 'polite-control',
                'transcript_path': str(transcript),
            }), encoding='utf-8')
            summary = '\n'.join(f'## {s}\nYeni kalıcı karar.' for s in flush.EXPECTED_SECTIONS)
            with (
                mock.patch.object(flush, 'run_codex', return_value=(summary, None)) as model,
                mock.patch.object(flush, 'maybe_trigger_compile'),
            ):
                result = flush.flush_once(
                    argparse.Namespace(hook_input=payload, reason='turnend'),
                    dt.datetime.fromisoformat('2026-09-05T10:00:00+03:00'),
                    vault,
                    state,
                )
            prompt = model.call_args.args[0]
            daily = (vault / 'daily/2026-09-05.md').read_text(encoding='utf-8')

        self.assertEqual(result, 0)
        self.assertNotIn('Önceki karar kalıcı olmamalı.', prompt)
        self.assertNotIn('Önceki yanıt da dışarıda kalmalı.', prompt)
        self.assertNotIn('Bunu kaydetme - lütfen.', prompt)
        self.assertIn('Yeni kalıcı karar.', prompt)
        self.assertIn('Yeni kalıcı karar.', daily)

    def test_flush_respects_control_outside_a_quoted_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            transcript = vault / 'source.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content':
                '> Makaledeki örnek cümle.\nBunu kaydetme - lütfen.'}), encoding='utf-8')
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
                 mock.patch.object(hook, 'enqueue_flush', side_effect=lambda p, r, **kwargs:
                    real_enqueue(p, r, popen_factory=mock.Mock(), **kwargs)),
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
        for prompt in (
            'Ok yap.',
            'Projedeki ayarı güncelle.',
            'Değiştirebilirsin.',
            'Düzenleyebilirsin.',
            'Uygulayabilirsin.',
        ):
            with self.subTest(prompt=prompt):
                self._check_explicit_write_prompt_reopens_read_only_scope(prompt)

    def test_natural_action_question_reopens_read_only_scope(self):
        for prompt in (
            'Düzeltebilir misin?',
            'Dosyaları değiştirebilir misin?',
            'Dosyaları değiştirebilir misiniz?',
        ):
            with self.subTest(prompt=prompt):
                self._check_explicit_write_prompt_reopens_read_only_scope(prompt)

    def test_targeted_fix_command_reopens_read_only_scope(self):
        for prompt in (
            'BIB projesindeki hatayı düzelt.',
            'Atlas projesindeki hatayı düzelt.',
            'src/app.py dosyasını düzelt.',
            'Atlas modülündeki hatayı düzelt.',
            'Borsa.md dosyasını düzelt.',
            'unut.py dosyasını düzelt.',
            'saklama.py dosyasını düzelt.',
            r"'C:\saklama.py'yi düzelt.",
            r"'C:\unut.py'yi düzelt.",
            r"'C:\read-only.py'yi düzelt.",
            '"C:\\saklama.py"\'yi düzelt.',
            '“C:\\saklama.py”\'yi düzelt.',
            'src klasöründeki hatayı düzelt.',
            r'C:\R&D\app.py dosyasını düzelt.',
            'Lütfen src/app.py dosyasını düzelt.',
            'src/app.py dosyasındaki hatayı düzelt.',
            'parse.py dosyasını düzelt.',
            'ne.py dosyasını düzelt.',
            'app.v1.py dosyasını düzelt.',
            '"Dockerfile" dosyasını düzelt.',
            '"Dockerfile" dosyasındaki hatayı düzelt.',
            '"src" klasöründeki hatayı düzelt.',
            '“app.py” dosyasını düzelt.',
            '‘app.py’ dosyasını düzelt.',
            '‘C:\\Users\\Me\\My Project\\app.py’yi düzelt.',
            'Sonra dosyayı düzelt.',
            'Sonra src/app.py dosyasını düzelt.',
            'src/app.py dosyasını düzelt, lütfen.',
            "src/app.py'yi düzelt.",
            'src/app.py dosyasını düzelt ve testleri çalıştır.',
            'src/app.py dosyasını düzelt. Sonra testleri çalıştır.',
            r'\\server\share\app.py dosyasını düzelt.',
            r'\\server-name\share.name\nested\app.py dosyasını düzelt.',
            'Tamam, src/app.py dosyasını düzelt.',
            'Okay, src/app.py dosyasını düzelt.',
            'Lütfen, src/app.py dosyasını düzelt.',
            '"C:\\Users\\Me\\My Project\\app.py"\'yi düzelt.',
            r"'C:\Users\Me\My Project\app.py'yi düzelt.",
            '"C:\\Users\\Me\\My Project\\app.py" dosyasını düzelt.',
            '"app.py" dosyasını düzelt.',
            '"My File.py" dosyasını düzelt.',
        ):
            with self.subTest(prompt=prompt):
                self._check_explicit_write_prompt_reopens_read_only_scope(
                    prompt, expect_prompt_enqueue=True
                )

    def test_targeted_case_suffix_write_prompt_reopens_read_only_scope(self):
        for prompt in (
            "README.md'yi düzenle.",
            "pyproject.toml'u değiştir.",
            '"C:\\Users\\Me\\My Project" klasörünü değiştir.',
            "src klasörü değiştir.",
            ".gitignore dosyasını düzenle.",
            ".env dosyasını değiştir.",
            "src/app.py dosyasını düzeltin.",
            "Lütfen dosyayı değiştiriniz.",
            '"LICENSE" dosyasını düzenle.',
            '"C:\\Program Files (x86)\\Project / R&D" klasörünü değiştir.',
            '"\\\\server\\share" klasörünü değiştir.',
            '"src" klasörünü değiştir.',
            '“My Project” klasörünü güncelle.',
            r'C:\ klasörünü değiştir.',
            r'\\server\share klasörünü değiştir.',
            r'C:\src\ klasörünü değiştir.',
            r'src/ klasörünü değiştir.',
            r'\\server\share\src\ klasörünü değiştir.',
        ):
            with self.subTest(prompt=prompt):
                self._check_explicit_write_prompt_reopens_read_only_scope(prompt)

    def test_conditional_targeted_commands_keep_read_only_scope(self):
        for prompt in (
            'Onay verirsem BIB projesindeki hatayı düzelt.',
            'Onay verdiysem BIB projesindeki hatayı düzelt.',
            'Onayım varsa BIB projesindeki hatayı düzelt.',
            'Onaylıysa BIB projesindeki hatayı düzelt.',
            'Onaylıysa projesindeki hatayı düzelt.',
            'Gerekirse modüldeki hatayı düzelt.',
            'Onay olduğu takdirde BIB projesindeki hatayı düzelt.',
            'Onay gelince BIB projesindeki hatayı düzelt.',
            'Onaydan sonra BIB projesindeki hatayı düzelt.',
            'Onay gelene kadar BIB projesindeki hatayı düzelt.',
            'Onay yokken BIB projesindeki hatayı düzelt.',
            'Onaylıysa dosyayı düzelt.',
            'Onaylamadan düzeltme; sadece açıklama yap.',
            'Hangi dosyayı düzeltebilir misin?',
            'Hangi dosyayı düzelt.',
            'Sakın dosyayı düzelt.',
            'Asla dosyadaki hatayı düzelt.',
            'SAKIN dosyayı düzelt.',
            'SANIRIM dosyayı düzelt.',
            'Onaylıysa... dosyayı düzelt.',
            'Gerekirse... dosyayı düzelt.',
            'Sakın... dosyayı düzelt.',
            'Hiçbir dosyayı düzelt.',
            'Lütfen hiçbir dosyayı düzelt.',
            'Hiç dosyayı düzelt.',
            'Onaylanmadıkça dosyayı düzelt.',
            'Gerekmedikçe dosyayı düzelt.',
            'Onaysızsa dosyayı düzelt.',
            'Onaylanmadıkça BIB projesindeki hatayı düzelt.',
            'Gerekmedikçe Atlas modülündeki hatayı düzelt.',
            'Onaysızsa src/app.py dosyasını düzelt.',
            'Çalışmazsa dosyayı düzelt.',
            'Gelmezse dosyayı düzelt.',
            'Çalışmazsam BIB projesindeki hatayı düzelt.',
            'Çalışmazsanız dosyayı düzelt.',
            'Yoksa dosyayı düzelt.',
            'Varsa dosyayı düzelt.',
            'Onaylansa dosyayı düzelt.',
            'Onaylansa projedeki ayarı güncelle.',
            'Hangi projedeki ayarı güncelle.',
            'Gelse dosyayı düzelt.',
            'Onaylanmasa dosyayı düzelt.',
            'Onaylansam dosyayı düzelt.',
            'Onaylanmasam dosyayı düzelt.',
            'Galiba dosyayı düzelt.',
            'Muhtemelen dosyayı düzelt.',
            'Kaç dosyayı düzeltebilir misin?',
            'Borsa dosyasındaki hatayı düzelt.',
            'Borsa dosyasını düzelt.',
            'Onaydan sonra src/app.py dosyasını düzelt.',
            'Onay yoksa dosyayı düzelt.',
            'Onay varsa dosyayı düzelt.',
            'Yoksa BIB projesindeki hatayı düzelt.',
            'Varsa Atlas modülündeki hatayı düzelt.',
            'Yoksa klasörünü değiştir.',
            'Onaylıysa klasörünü değiştir.',
            'Sakın klasörünü değiştir.',
            r'Onaylanmadıkça \\server\share\app.py dosyasını düzelt.',
            r'Sakın \\server\share\app.py dosyasını düzelt.',
            'Lütfen read-only/modda dosyayı düzelt.',
            r'R&D\read-only dosyayı düzelt.',
            r'Onaylansa C:\ klasörünü değiştir.',
            r"'C:\saklama.py'yi düzelt. Do not modify files or settings.",
            '"C:\\saklama.py"\'yi düzelt. Do not modify files or settings.',
            r'src/ dosyasını düzelt.',
            r'Onaylansa C:\src\ klasörünü değiştir.',
            'Onaylansa "src" klasöründeki hatayı düzelt.',
            'src/app.py dosyasını düzelt ve testleri çalıştır. Do not modify files or settings.',
        ):
            with self.subTest(prompt=prompt):
                self._check_write_prompt_keeps_read_only_scope(prompt)

    def test_unbounded_natural_language_targets_keep_read_only_scope(self):
        for prompt in (
            'Yanıtındaki kodu düzelt.',
            'Bu cümledeki hatayı düzelt.',
            'Komut örneği olarak Atlas projesindeki hatayı düzelt.',
            '"C:\\Users\\Me\\My Project\\app.py dosyasını düzelt."',
            '"Dockerfile dosyasını düzelt."',
            '"LICENSE dosyasını düzenle."',
            '“src/app.py dosyasını düzelt ve testleri çalıştır.”',
            '‘src/app.py dosyasını düzelt. Sonra testleri çalıştır.’',
            '"Bunu düzelt."',
            '"Dockerfile"ı düzelt.',
            '“Merhaba”yı düzenle.',
            '"app.py dosyasını düzelt."',
        ):
            with self.subTest(prompt=prompt):
                self._check_write_prompt_keeps_read_only_scope(prompt)

    def test_chat_output_requests_keep_read_only_scope(self):
        for prompt in (
            'Yanıtı buraya yaz.',
            'Bana kısa bir şiir yaz.',
            'Yazabilirsin.',
        ):
            with self.subTest(prompt=prompt):
                self._check_write_prompt_keeps_read_only_scope(prompt)

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

    def test_same_prompt_read_only_wins_over_action_question(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            memory_ledger.mark_read_only_turn(state, 'audit')
            payload = {
                'session_id': 'audit',
                'prompt': 'Düzeltebilir misin? Do not modify files or settings.',
            }
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
            self.assertTrue(memory_ledger.is_read_only_turn(state, 'audit'))
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
            '```text\n"C:\\Users\\Me\\My Project" klasörünü değiştir.\n```',
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

    def _check_explicit_write_prompt_reopens_read_only_scope(
        self, prompt, *, expect_prompt_enqueue=False
    ):
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
            if expect_prompt_enqueue:
                enqueue.assert_called_once()
                self.assertEqual(enqueue.call_args.args, (payload, 'precompact'))
                self.assertIn('deadline', enqueue.call_args.kwargs)
            else:
                enqueue.assert_not_called()

            output = io.StringIO()
            with (
                mock.patch.object(hook, 'VAULT_ROOT', vault),
                mock.patch.object(hook, 'STATE_DIR', state),
                mock.patch.object(hook, 'enqueue_flush') as enqueue_end,
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, 'stdout', output),
            ):
                self.assertEqual(hook.main(['turn-end', '--strict']), 0)

        enqueue_end.assert_called_once()
        self.assertEqual(enqueue_end.call_args.args, (payload, 'turnend'))
        self.assertIn('deadline', enqueue_end.call_args.kwargs)

    def _check_write_prompt_keeps_read_only_scope(self, prompt):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.codex/scripts/.state'
            memory_ledger.mark_read_only_turn(state, 'audit')
            payload = {'session_id': 'audit', 'prompt': prompt}
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
            self.assertTrue(memory_ledger.is_read_only_turn(state, 'audit'))
            enqueue.assert_not_called()

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
