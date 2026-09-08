import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import _fixtures
import daily_store
import doctor
import hook
import jsonl_tail
import memory_ledger
import process_control
import worker_supervisor as workers
import vault_retrieval
import compile as memory_compile
from test_second_brain_acceptance import _seed_vault


class AuditRegressionTests(unittest.TestCase):
    @staticmethod
    def _file_manifest(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_compile_dry_run_missing_state_keeps_state_lock_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state = vault / ".codex/scripts/missing-state"
            state_root = state.parent
            before = self._file_manifest(state_root)
            output = io.StringIO()
            with (
                mock.patch.object(memory_compile, "VAULT_ROOT", vault),
                mock.patch.object(memory_compile, "STATE_DIR", state),
                mock.patch.object(memory_compile.sys, "stdout", output),
                mock.patch.object(
                    memory_compile,
                    "_run_codex",
                    side_effect=AssertionError("dry-run called model"),
                ),
            ):
                result = memory_compile.main(
                    ["--dry-run", "--strict"]
                )
            after = self._file_manifest(state_root)

        self.assertEqual(result, 0)
        self.assertEqual(after, before)
        self.assertFalse(state.exists())
        self.assertEqual(output.getvalue().splitlines(), ["2026-09-03.md", "2026-09-04.md"])

    def test_compile_dry_run_malformed_state_keeps_failure_evidence_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state = vault / ".codex/scripts/.state"
            state_file = state / "compile-state.json"
            state_file.write_bytes(b"{malformed")
            (state / "health.json").write_bytes(b"health-before")
            claim = state / "compile-trigger-2026-09-04"
            claim.write_bytes(b"claim")
            before = self._file_manifest(state)
            with (
                mock.patch.object(memory_compile, "VAULT_ROOT", vault),
                mock.patch.object(memory_compile, "STATE_DIR", state),
            ):
                result = memory_compile.main(
                    ["--dry-run", "--strict", "--trigger-claim", str(claim)]
                )
            after = self._file_manifest(state)

        self.assertEqual(result, 1)
        self.assertEqual(after, before)

    def test_compile_normal_failure_preserves_unreadable_state_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state = vault / ".codex/scripts/.state"
            state_file = state / "compile-state.json"
            state_file.write_bytes(b"{malformed")
            claim = state / "compile-trigger-2026-09-04"
            claim.write_bytes(b"claim")
            with (
                mock.patch.object(memory_compile, "VAULT_ROOT", vault),
                mock.patch.object(memory_compile, "STATE_DIR", state),
            ):
                result = memory_compile.main(
                    ["--strict", "--trigger-claim", str(claim)]
                )

            self.assertEqual(state_file.read_bytes(), b"{malformed")
            self.assertFalse(claim.exists())
            self.assertTrue((state / "health.json").is_file())

        self.assertEqual(result, 1)

    def test_compile_dry_run_changed_daily_read_failure_keeps_state_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state = vault / ".codex/scripts/.state"
            (state / "compile-state.json").write_text(
                '{"ingested": {}, "cursor": "", "runs": []}',
                encoding="utf-8",
            )
            claim = state / "compile-trigger-2026-09-04"
            claim.write_bytes(b"claim")
            before = self._file_manifest(state)
            with (
                mock.patch.object(memory_compile, "VAULT_ROOT", vault),
                mock.patch.object(memory_compile, "STATE_DIR", state),
                mock.patch.object(
                    memory_compile,
                    "changed_dailies",
                    side_effect=OSError("daily read failed"),
                ),
            ):
                result = memory_compile.main(
                    ["--dry-run", "--strict", "--trigger-claim", str(claim)]
                )
            after = self._file_manifest(state)

        self.assertEqual(result, 1)
        self.assertEqual(after, before)

    def setUp(self):
        self._scope_guard = mock.patch.object(hook, '_validate_hook_scope')
        self._scope_guard.start()
        self.addCleanup(self._scope_guard.stop)

    def test_doctor_survives_transport_cleanup_during_inspection(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = state / ('hookin-' + 'a' * 32 + '.json')
            path.write_text(json.dumps({'session_id': 's', 'transcript_path': 'source.jsonl'}), encoding='utf-8')
            now = path.stat().st_mtime + 1
            read = Path.read_text
            def consume(candidate, *args, **kwargs):
                value = read(candidate, *args, **kwargs)
                if candidate == path:
                    candidate.unlink()
                return value
            with mock.patch.object(Path, 'read_text', consume):
                self.assertEqual(doctor._state_privacy_check(doctor.Context(state_dir=state, now=now)).status, 'OK')

    def test_flush_transport_does_not_copy_the_user_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(hook, 'STATE_DIR', state), mock.patch.object(workers, 'enqueue_job'):
                hook.enqueue_flush({'session_id': 's', 'transcript_path': 'source.jsonl',
                                    'prompt': 'Private user text'}, 'turnend')
            payload = json.loads(next(state.glob('hookin-*.json')).read_text(encoding='utf-8'))
            self.assertEqual(payload, {'session_id': 's', 'transcript_path': 'source.jsonl'})

    def test_doctor_distinguishes_bounded_transport_from_retained_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = state / ('hookin-' + 'a' * 32 + '.json')
            payload = {'session_id': 's', 'transcript_path': 'source.jsonl'}
            path.write_text(json.dumps(payload), encoding='utf-8')
            context = doctor.Context(state_dir=state, now=path.stat().st_mtime + 1)
            self.assertEqual(doctor._state_privacy_check(context).status, 'OK')
            payload['prompt'] = 'private'
            path.write_text(json.dumps(payload), encoding='utf-8')
            self.assertEqual(doctor._state_privacy_check(context).status, 'FAIL')
            path.write_text(json.dumps({'session_id': 's', 'transcript_path': 'source.jsonl'}), encoding='utf-8')
            self.assertEqual(doctor._state_privacy_check(doctor.Context(state_dir=state,
                             now=path.stat().st_mtime + 3601)).status, 'FAIL')

    def test_excluded_filename_cannot_be_read_through_full_source_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / 'Ankara.md'
            source.write_text('Levent başkentte yaşıyor.', encoding='utf-8')
            memory_ledger.suppress_derived_memory(vault / '.codex/private-memory', 'Ankara')
            hashes = memory_ledger.load_suppressed_hashes(vault / '.codex/private-memory')
            self.assertEqual(memory_ledger.read_memory_source(vault, source), '')
            views = memory_ledger.materialize_memory_views(vault, [('Ankara.md', 'İkamet')], hashes)
            self.assertEqual((vault / views['Ankara.md']).read_text(encoding='utf-8'), '')
            self.assertEqual(source.read_text(encoding='utf-8'), 'Levent başkentte yaşıyor.')

    def test_startup_wakes_recovery_for_interrupted_transition(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            with mock.patch.object(hook, 'VAULT_ROOT', vault), \
                 mock.patch.object(hook, 'STATE_DIR', vault / '.state'), \
                 mock.patch.object(hook, 'inspect_worker_queue', return_value={'counts': {}, 'invalid': 1}), \
                 mock.patch.object(hook, 'ensure_supervisor') as wake, \
                 mock.patch.object(hook.sys, 'stdin', io.StringIO('{}')), \
                 mock.patch.object(hook.sys, 'stdout', io.StringIO()):
                self.assertEqual(hook.main(['session-start', '--strict']), 0)
            wake.assert_called_once()

    def test_compile_accepts_appends_but_rejects_changed_source_snapshot(self):
        for append_only in (True, False):
            with self.subTest(append_only=append_only), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                _seed_vault(vault)
                source = vault / 'daily/2026-09-04.md'
                original = source.read_bytes()
                digest = memory_compile._sha256(source)
                def runner(prompt, stage):
                    source.write_bytes((original if append_only else b'changed\n') + b'new contribution\n')
                    log = stage / 'knowledge/log.md'
                    log.write_text(log.read_text(encoding='utf-8') + '\nSnapshot processed.\n', encoding='utf-8')
                reason, detail = memory_compile._compile_one(vault, vault / '.codex/scripts/.state',
                    source, digest, '2026-09-05T12:00:00+03:00', runner=runner)
                self.assertEqual(reason, None if append_only else 'source-changed', detail)
                self.assertTrue(source.read_bytes().endswith(b'new contribution\n'))

    def test_compile_rejects_live_knowledge_changes_after_staging(self):
        for relative in (
            'knowledge/index.md',
            'knowledge/concepts/example.md',
            'knowledge/connections/example.md',
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                target = vault / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('old', encoding='utf-8')
                (vault / 'knowledge/log.md').write_text('log', encoding='utf-8')
                (vault / 'knowledge/concepts').mkdir(parents=True, exist_ok=True)
                (vault / 'knowledge/connections').mkdir(parents=True, exist_ok=True)
                daily = vault / 'daily/2026-09-07.md'
                daily.parent.mkdir()
                daily.write_text('daily', encoding='utf-8')

                copy_source_file = memory_compile._copy_source_file

                def race(source, destination, vault_root):
                    copy_source_file(source, destination, vault_root)
                    if source == target:
                        source.write_text('new concurrent data', encoding='utf-8')

                with mock.patch.object(memory_compile, '_copy_source_file', side_effect=race):
                    stage, baseline = memory_compile._prepare_stage(
                        vault,
                        vault / '.codex/scripts/.state',
                        daily,
                    )
                (stage / relative).write_text('derived from old', encoding='utf-8')

                with self.assertRaisesRegex(memory_compile.PolicyError, 'live-target-changed'):
                    memory_compile._promote_changes(stage, vault, [relative], baseline)
                self.assertEqual(target.read_text(encoding='utf-8'), 'new concurrent data')

    def test_doctor_rejects_error_receipt_without_separate_health_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            (state / 'runtime-vault-retrieval.json').write_text(json.dumps(
                {'ts': 100, 'cwd': str(vault), 'outcome': 'error'}), encoding='utf-8')
            with mock.patch.object(doctor, 'build_vault_map', return_value=[object()]):
                result = doctor._vault_retrieval_check(doctor.Context(vault, vault, state, 101))
            self.assertEqual(result.status, 'FAIL')

    def test_doctor_accepts_optional_system_section_and_separate_topic_pointers(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / '🔮 850-Companion'
            companion.mkdir()
            (companion / 'Threads.md').write_text(
                '## Active Execution\n### Thread: Current\n**Status:** Active\n'
                '## Topic Pointers\n### Thread: Reference\n**Status:** Pointer\n'
                '## Waiting / Parked\n## Inactive History\n## Closed Threads\n', encoding='utf-8')
            result = doctor._thread_workload_check(doctor.Context(vault))
            self.assertEqual(result.status, 'OK')
            self.assertIn('1 active', result.evidence)

    def test_unreadable_note_is_not_reported_as_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            notes = vault / '🧠 500-Knowledge'
            notes.mkdir()
            (notes / 'broken.md').write_bytes(b'\xff\xfeinvalid')
            context = hook.handle_user_prompt({'session_id': 'read-error', 'prompt': 'Atlas bilgisi'},
                                             vault / '.codex/scripts/.state', vault_root=vault)
            self.assertIn('Vault Arama Sorunu', context)

    def test_fresh_private_retrieval_fits_with_behavior_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            notes = vault / '🧠 500-Knowledge'
            notes.mkdir()
            for number in range(3):
                (notes / f'atlas{number}.md').write_text('# ' + 'Atlas planı ' * 20 + '\n' + 'Atlas plan ayrıntısı. ' * 100, encoding='utf-8')
            memory_ledger.suppress_derived_memory(vault / '.codex/private-memory', 'Unutulmuş ayrı bilgi')
            def full_result(*args, **kwargs):
                budget = kwargs.get('max_chars', vault_retrieval.MAX_CONTEXT_CHARS)
                text = ('[Vault Retrieval]\n' + 'kaynak ' * 400)[:min(2485, budget)]
                return vault_retrieval.VaultContextResult('emitted', text, 3, 3, ('atlas.md',), budget)
            with mock.patch.object(hook, 'retrieve_vault_context_detailed', side_effect=full_result):
                context = hook.handle_user_prompt({'session_id': 'fresh', 'prompt': 'Atlas planı nedir?'},
                                                 vault / '.codex/scripts/.state', vault_root=vault)
            self.assertIn('[Vault Retrieval', context)
            self.assertLessEqual(len(context), hook.USER_PROMPT_CONTEXT_TARGET_CHARS)

    def test_startup_keeps_context_when_queue_inspection_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            output = io.StringIO()
            with mock.patch.object(hook, 'VAULT_ROOT', vault), \
                 mock.patch.object(hook, 'STATE_DIR', vault / '.state'), \
                 mock.patch.object(hook, 'inspect_worker_queue', side_effect=ValueError('bad queue')), \
                 mock.patch.object(hook.sys, 'stdin', io.StringIO('{}')), \
                 mock.patch.object(hook.sys, 'stdout', output):
                hook.main(['session-start', '--strict'])
            context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
            self.assertIn('Hafıza Protokolü', context)
            self.assertIn('doğrulanamadı', context)

    def test_hook_reads_utf8_json_independently_of_windows_codepage(self):
        payload = {'prompt': 'Şu 🧠 bilgiyi değerlendir.'}
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload, ensure_ascii=False).encode('utf-8')),
                                  encoding='cp1254', errors='surrogateescape')
        with mock.patch.object(hook.sys, 'stdin', stream):
            self.assertEqual(hook._load_payload(), payload)

    def test_session_only_still_applies_when_message_contains_secret(self):
        turns = [('user', 'Bu konuşmada kalsın. Şifrem: example-secret'),
                 ('user', 'Sonraki özel bilgi de saklanmamalı.')]
        self.assertEqual(memory_ledger.memory_directive(turns[0][1]).kind, 'session-only')
        self.assertEqual(memory_ledger.persistent_turns(turns), [])

    def test_oversize_line_ending_at_limit_does_not_consume_next_record(self):
        self.assertEqual(list(jsonl_tail.iter_records(io.BytesIO(b'12345678\n{}\n'),
                                                     max_line_bytes=8)), [(2, {})])

    def test_failed_soft_termination_reaches_kill(self):
        process = mock.Mock(pid=4242)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('child', 2), 0]
        with mock.patch.object(process_control.os, 'name', 'nt'), \
             mock.patch.object(process_control.subprocess, 'run', side_effect=OSError):
            process_control.terminate_process_tree(process)
        process.kill.assert_called_once()

    def test_committed_replay_after_another_append_preserves_both(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            now = dt.datetime(2026, 9, 5, 12)
            def publish(key, text):
                return daily_store.publish(vault, state, text, 'turnend', now,
                                           idempotency_key=key * 64)
            self.assertTrue(publish('a', 'Birinci bilgi'))
            self.assertTrue(publish('b', 'İkinci bilgi'))
            path = vault / 'daily/2026-09-05.md'
            before = path.read_bytes()
            self.assertFalse(publish('a', 'Birinci bilgi'))
            self.assertEqual(path.read_bytes(), before)

    def test_interrupted_queue_move_is_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = workers.enqueue_job(state, 'flush', {}, start_supervisor=False, now=100)
            real_replace = workers.os.replace
            def fail_move(source, destination):
                if Path(destination).parent.name == 'claimed':
                    raise RuntimeError('simulated process death')
                return real_replace(source, destination)
            with mock.patch.object(workers.os, 'replace', side_effect=fail_move):
                with self.assertRaises(RuntimeError):
                    workers._claim_next_job(state, now=100)
            with mock.patch.object(workers, 'pid_is_alive', return_value=False):
                workers.recover_stale_jobs(state, now=10000)
            self.assertFalse(list((state / 'worker-jobs/quarantined').glob('*.json')))
            claimed = workers._claim_next_job(state, now=20000)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed[1]['job_id'], path.stem.removeprefix('job-'))

    def test_final_stale_attempt_produces_valid_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(state, 'flush', {}, start_supervisor=False, now=100)
            path, job = workers._claim_next_job(state, now=100)
            job['attempt'] = workers.MAX_ATTEMPTS
            workers.atomic_write_json(path, job)
            with mock.patch.object(workers, 'pid_is_alive', return_value=False):
                workers.recover_stale_jobs(state, now=10000)
            report = workers.inspect_worker_queue(state)
            self.assertEqual(report['invalid'], 0)
            self.assertEqual(report['counts']['dead-letter'], 1)

    def test_failed_final_attempt_records_retry_exhaustion_for_doctor(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = workers.enqueue_job(
                state, 'flush', {'source': 'retained'},
                start_supervisor=False, now=100,
            )
            running, job = workers._claim_next_job(state, now=100)
            job['attempt'] = workers.MAX_ATTEMPTS
            workers.atomic_write_json(running, job)

            workers._finish_job(
                state, running, job, status='failed',
                error='RuntimeError', now=101,
            )

            dead = state / 'worker-jobs/dead-letter' / path.name
            final = json.loads(dead.read_text(encoding='utf-8'))
            check = doctor._worker_queue_check(
                doctor.Context(state_dir=state, now=101),
            )

        self.assertEqual(final['terminal_reason'], 'retry-exhausted')
        self.assertFalse(final['retryable'])
        self.assertEqual(final['last_error'], 'RuntimeError')
        self.assertEqual(final['payload'], {'source': 'retained'})
        self.assertEqual(check.status, 'FAIL')
        self.assertIn('retry-exhausted=1', check.evidence)
        self.assertIn('unclassified-terminal=0', check.evidence)
