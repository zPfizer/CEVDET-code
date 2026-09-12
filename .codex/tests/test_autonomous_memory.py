import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fixtures import CODEX_DIR
import compile_state
import flush
import worker_supervisor as worker
import compile as compiler
import companion_memory
import subprocess
from file_lock import LockUnavailable
import argparse
import json
from memory_ledger import mark_session_only
from test_second_brain_acceptance import _concept, _seed_vault


def _write_valid_compilation(stage: Path, *, invalidate: bool = False) -> None:
    daily = next((stage / 'daily').glob('*.md'))
    day = daily.stem
    concepts = stage / 'knowledge/concepts'
    concepts.mkdir(parents=True, exist_ok=True)
    names = (f'gunluk-{day}', f'ozet-{day}')
    for index, name in enumerate(names):
        related = names[1 - index]
        text = _concept(
            f'Günlük {day} {index}',
            (daily.name,),
            (f'- `gecerli` `cevo-cikarimi` `belirsiz` {day} '
             f'[[daily/{day}|Kaynak]] — Türetilmiş derleme {day} {index}.',),
            related,
        )
        if invalidate and index == 0:
            text = text.replace('schema: knowledge-v2', 'schema: invalid', 1)
        (concepts / f'{name}.md').write_text(text, encoding='utf-8')

    index_path = stage / 'knowledge/index.md'
    index_text = index_path.read_text(encoding='utf-8').rstrip() + '\n'
    rows = '\n'.join(
        f'| [[concepts/{name}\\|Günlük {day} {index}]] | Türetilmiş derleme. | '
        f'{daily.name} | {day} |'
        for index, name in enumerate(names)
    )
    index_path.write_text(index_text + rows + '\n', encoding='utf-8')

    log_path = stage / 'knowledge/log.md'
    log_text = log_path.read_text(encoding='utf-8').rstrip() + '\n\n'
    log_path.write_text(
        log_text
        + f'## [{day}T00:00:00+00:00] compile | {daily.name}\n'
        + f'Oluşturulan: {", ".join(names)}.\n'
        + 'Güncellenen: yok. Derleme tamamlandı.\n',
        encoding='utf-8',
    )


class AutonomousMemoryTests(unittest.TestCase):
    def test_invalid_pending_maintenance_does_not_block_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            path = worker.enqueue_maintenance(state, vault_root=root, start_supervisor=False)
            path.write_text('{broken', encoding='utf-8')
            replacement = worker.enqueue_maintenance(state, vault_root=root, start_supervisor=False)
            self.assertNotEqual(path, replacement)
            queue = worker.inspect_worker_queue(state / 'maintenance')
            self.assertEqual(queue['counts']['pending'], 1)
            self.assertEqual(queue['counts']['quarantined'], 1)

    def test_terminal_maintenance_failure_is_visible_to_existing_health_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            worker.enqueue_maintenance(state, vault_root=root, start_supervisor=False)
            real_lock = worker.locked
            def locks(path, **kwargs):
                if path == state / 'compile':
                    raise LockUnavailable('busy')
                return real_lock(path, **kwargs)
            with mock.patch.object(worker, 'locked', side_effect=locks), \
                 mock.patch.object(worker, '_retry_delay', return_value=0):
                worker.run_supervisor(root, state / 'maintenance',
                                      job_runner=lambda r, s, p: worker.execute_job_file(r, s, p))
            health = json.loads((state / 'health.json').read_text())
            self.assertEqual(health['components']['compile:global']['error'], 'maintenance:LockUnavailable')

    def test_input_cleanup_follows_durable_worker_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            state.mkdir()
            trace = root / 'source.jsonl'
            trace.write_text('{}', encoding='utf-8')
            transport = state / 'hookin-test.json'
            transport.write_text(json.dumps({'session_id': 's', 'transcript_path': str(trace)}))
            worker.enqueue_job(state, 'flush', {'hook_input': str(transport), 'reason': 'turnend',
                               'event_iso': '2026-09-06T12:00:00+03:00'}, start_supervisor=False)
            running, _ = worker._claim_next_job(state, now=100)
            with mock.patch.object(flush, 'flush_once', return_value=0), \
                 mock.patch.object(worker, '_finish_job', side_effect=OSError('receipt failed')):
                with self.assertRaises(OSError):
                    worker.execute_job_file(root, state, running)
            self.assertTrue(transport.exists())
            with mock.patch.object(flush, 'flush_once', return_value=0):
                self.assertEqual(worker.execute_job_file(root, state, running), 0)
            self.assertFalse(transport.exists())
            self.assertEqual(worker.inspect_worker_queue(state)['counts']['succeeded'], 1)

    def test_attachment_envelope_is_not_split_at_model_batch_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            state.mkdir()
            envelope = '# Files pasted by the user:\n' + 'a' * 24_000 + '\n## My request:\nKaynağı koru.'
            trace = root / 'source.jsonl'
            trace.write_text(json.dumps({'role': 'user', 'content': envelope}), encoding='utf-8')
            payload = {'session_id': 'envelope', 'transcript_path': str(trace)}
            args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
            with mock.patch.object(flush.attachment_memory, 'capture_sources', return_value=[]) as capture, \
                 mock.patch.object(flush, 'run_codex', return_value=('FLUSH_BOS', None)), \
                 mock.patch.object(worker, 'ensure_supervisor'):
                self.assertEqual(flush.flush_once(args, dt.datetime.now().astimezone(), root, state, hook_input=payload), 0)
            self.assertEqual(capture.call_args.args[0], [('user', envelope)])

    def test_exited_child_is_retried_and_unfinished_input_is_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            source = state / 'hookin-retained.json'
            source.write_text('{}', encoding='utf-8')
            worker.enqueue_job(state, 'flush', {'hook_input': str(source)}, start_supervisor=False, now=1)
            running, job = worker._claim_next_job(state, now=2)
            with mock.patch.object(worker, 'run_with_tree_timeout', return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaisesRegex(RuntimeError, 'worker-child-result-missing'):
                    worker._run_claimed_job(state, state, running)
            job['owner_pid'] = 999999999
            job['lease_until'] = 9999999999
            worker.atomic_write_json(running, job)
            self.assertEqual(worker.recover_stale_jobs(state, now=3), 1)
            pending = state / 'worker-jobs/pending' / running.name
            self.assertTrue(pending.is_file())
            job = json.loads(pending.read_text())
            job.update(status='dead-letter', finished_ts=4, attempt=3)
            worker.atomic_write_json(state / 'worker-jobs/dead-letter' / pending.name, job)
            pending.unlink()
            worker._sweep_stale_hook_inputs(state, source.stat().st_mtime + 7200)
            self.assertTrue(source.exists())

    def test_secret_spanning_transcript_batch_boundary_never_reaches_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            state.mkdir(parents=True)
            header = '-----BEGIN RSA PRIVATE KEY-----\n'
            body_marker = 'PEM_BODY_MUST_NOT_LEAK'
            prefix = 'x' * (12_000 - len(header) - len(body_marker))
            transcript = root / 'transcript.jsonl'
            transcript.write_text(json.dumps({
                'role': 'assistant',
                'content': prefix + header + body_marker + '\n-----END RSA PRIVATE KEY-----',
            }), encoding='utf-8')
            payload = {'session_id': 'pem-boundary', 'transcript_path': str(transcript)}
            args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
            prompts = []
            summary = '\n'.join(f'## {s}\nGüvenli parça.' for s in flush.EXPECTED_SECTIONS)

            def summarize(prompt, _root, **_kwargs):
                prompts.append(prompt)
                return summary, None

            with mock.patch.object(flush, 'run_codex', side_effect=summarize), \
                 mock.patch.object(flush, 'maybe_trigger_compile'), \
                 mock.patch.object(worker, 'ensure_supervisor'):
                flush.flush_once(args, dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc), root, state, hook_input=payload)
                flush.flush_once(args, dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc), root, state, hook_input=payload)

        self.assertTrue(prompts)
        self.assertTrue(all(body_marker not in prompt for prompt in prompts))

    def test_selected_index_read_failure_is_recorded_with_the_source_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            source = root / 'transcript.jsonl'
            source.write_text(json.dumps({'role': 'user', 'content': 'Durable'}) + '\n', encoding='utf-8')
            payload = {'session_id': 'selected-read-failure', 'transcript_path': str(source)}
            args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
            with mock.patch.object(
                flush.transcript_index,
                'read_selected_rows',
                side_effect=ValueError('transcript-index-privacy-drift'),
            ):
                result = flush.flush_once(
                    args,
                    dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc),
                    root,
                    state,
                    hook_input=payload,
                )
            health = json.loads((state / 'health.json').read_text(encoding='utf-8'))

        self.assertEqual(result, 1)
        self.assertEqual(health['error'], 'transcript-index-privacy-drift')

    def test_evidence_index_read_failure_is_recorded_with_the_source_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            source = root / 'transcript.jsonl'
            source.write_text(
                '\n'.join(
                    json.dumps({'role': role, 'content': text})
                    for role, text in (('assistant', 'Önceki bağlam'), ('user', 'Yapalım'))
                ) + '\n',
                encoding='utf-8',
            )
            payload = {'session_id': 'evidence-read-failure', 'transcript_path': str(source)}
            args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
            index = flush.transcript_index.open_or_update(
                state,
                payload['session_id'],
                source,
                hashes=frozenset(),
                parser=flush._message_parts_for_index,
                text_from_content=flush._text_from_content,
                max_line_bytes=flush.MAX_TRANSCRIPT_LINE_BYTES,
            )
            flush.atomic_write_json(
                state / f'flush-coverage-{flush._session_key(payload["session_id"])}.json',
                {
                    'schema_version': flush.transcript_index.COVERAGE_SCHEMA_VERSION,
                    'count': 1,
                    'digest': index.coverage_digest(1),
                },
            )
            real_read = flush.transcript_index.read_selected_rows
            calls = 0

            def read_once(*call_args, **call_kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ValueError('transcript-index-evidence-drift')
                return real_read(*call_args, **call_kwargs)

            with mock.patch.object(
                flush.transcript_index,
                'read_selected_rows',
                side_effect=read_once,
            ):
                result = flush.flush_once(
                    args,
                    dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc),
                    root,
                    state,
                    hook_input=payload,
                )
            health = json.loads((state / 'health.json').read_text(encoding='utf-8'))

        self.assertEqual(result, 1)
        self.assertEqual(health['error'], 'transcript-index-evidence-drift')
        self.assertEqual(calls, 2)

    def test_append_during_model_is_published_or_retried_without_losing_privacy(self):
        summary = '\n'.join(f'## {section}\nSaved.' for section in flush.EXPECTED_SECTIONS)
        cases = {
            'ordinary': json.dumps({'role': 'user', 'content': 'new durable'}) + '\n',
            'do-not-save': json.dumps({'role': 'user', 'content': 'Bunu kaydetme.'}) + '\n',
            'session-only': json.dumps({'role': 'user', 'content': 'Bu konuşmada kalsın.'}) + '\n',
        }
        for name, appended in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / '.state'
                source = root / 'transcript.jsonl'
                source.write_text(
                    json.dumps({'role': 'user', 'content': 'base'}) + '\n',
                    encoding='utf-8',
                )
                payload = {'session_id': f'append-{name}', 'transcript_path': str(source)}
                args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
                calls = []

                def summarize(prompt, _root, **_kwargs):
                    calls.append(prompt)
                    if len(calls) == 1:
                        source.write_text(source.read_text(encoding='utf-8') + appended, encoding='utf-8')
                    return summary, None

                with mock.patch.object(flush, 'run_codex', side_effect=summarize), \
                     mock.patch.object(flush, 'maybe_trigger_compile'), \
                     mock.patch.object(worker, 'ensure_supervisor'):
                    first = flush.flush_once(
                        args,
                        dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc),
                        root,
                        state,
                        hook_input=payload,
                    )
                    if name == 'ordinary':
                        self.assertEqual(first, 0)
                        self.assertEqual(len(calls), 1)
                        self.assertTrue((root / 'daily/2026-09-08.md').is_file())
                        coverage = json.loads(next(state.glob('flush-coverage-*.json')).read_text(encoding='utf-8'))
                        self.assertEqual(coverage['count'], 1)
                        continue
                    self.assertEqual(first, 1)
                    self.assertEqual(len(calls), 1)
                    with mock.patch.object(flush, 'run_codex') as retry_model:
                        second = flush.flush_once(
                            args,
                            dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc),
                            root,
                            state,
                            hook_input=payload,
                        )
                    retry_model.assert_not_called()
                    self.assertEqual(second, 0)
                    self.assertFalse((root / 'daily/2026-09-08.md').exists())

    def test_long_conversation_is_drained_oldest_first_without_reprocessing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            state.mkdir(parents=True)
            transcript = root / 'transcript.jsonl'
            transcript.write_text('\n'.join(json.dumps({'role': 'user', 'content': f'Kalıcı karar {n:02}.'})
                                            for n in range(45)), encoding='utf-8')
            payload = {'session_id': 'long', 'transcript_path': str(transcript)}
            args = argparse.Namespace(reason='turnend', hook_input=root / 'unused')
            prompts = []
            def summarize(prompt, _root, **_kwargs):
                prompts.append(prompt)
                return '\n'.join(f'## {s}\nParça {len(prompts)} kararı.' for s in flush.EXPECTED_SECTIONS), None
            with mock.patch.object(flush, 'run_codex', side_effect=summarize), \
                 mock.patch.object(flush, 'maybe_trigger_compile'), mock.patch.object(worker, 'ensure_supervisor'):
                for _ in range(3):
                    self.assertEqual(flush.flush_once(args, dt.datetime.now().astimezone(), root, state, hook_input=payload), 0)
            self.assertEqual(len(prompts), 2)
            self.assertIn('Kalıcı karar 00.', prompts[0])
            self.assertNotIn('Kalıcı karar 30.', prompts[0])
            self.assertIn('Kalıcı karar 44.', prompts[1])
            self.assertEqual(json.loads((state / f'flush-coverage-{flush._session_key("long")}.json').read_text())['count'], 45)
            index = json.loads((state / f'flush-index-{flush._session_key("long")}.json').read_text())
            self.assertEqual(index['counters']['json_decodes_total'], 45)

    def test_companion_retry_preserves_notes_and_newer_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            folder = root / '🔮 850-Companion'
            folder.mkdir()
            originals = {}
            for name in ('Last-Session.md', 'Journal.md', 'Threads.md'):
                originals[name] = f'---\r\ntype: companion-state\r\n---\r\n# {name}\r\n\r\nİnsan notu korunur.\r\n'
                (folder / name).write_bytes(originals[name].encode('utf-8'))
            summary = '\n'.join(f'## {s}\nSalı görüşmesi.' for s in flush.EXPECTED_SECTIONS)
            event = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)
            real_write = companion_memory.atomic_write_text

            def interrupt(path, text, **kwargs):
                if path.name == 'Journal.md':
                    raise OSError('interrupted')
                real_write(path, text, **kwargs)

            with mock.patch.object(companion_memory, 'atomic_write_text', side_effect=interrupt):
                with self.assertRaises(OSError):
                    companion_memory.publish(root, state, summary, event, 'a' * 64, 'one', frozenset())
            companion_memory.publish(root, state, summary, event, 'a' * 64, 'one', frozenset())
            companion_memory.publish(root, state, summary, event - dt.timedelta(days=1), 'b' * 64, 'one', frozenset())
            for name in originals:
                raw = (folder / name).read_bytes()
                text = raw.decode('utf-8')
                self.assertEqual(text.count(companion_memory.BEGIN), 1)
                self.assertIn('İnsan notu korunur.', text)
                self.assertIn(f'# {name}\r\n'.encode('utf-8'), raw)
                self.assertIn('İnsan notu korunur.\r\n'.encode('utf-8'), raw)
                self.assertNotIn(b'\r\r\n', raw)
                self.assertIn('2026-09-06', text)
                self.assertNotIn('2026-09-05', text)
                if name == 'Journal.md':
                    self.assertIn(
                        f'<!-- journal-latest: {event.isoformat()} -->'.encode('utf-8'),
                        raw,
                    )
                    self.assertIn(f'### {event.isoformat()}'.encode('utf-8'), raw)
            mark_session_only(state, 'private')
            with self.assertRaisesRegex(ValueError, 'memory-session-excluded'):
                companion_memory.publish(root, state, summary, event, 'c' * 64, 'private', frozenset())

    def test_daytime_save_queues_one_durable_maintenance_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'daily').mkdir()
            (root / 'daily/2026-09-06.md').write_text('Karar: salı.', encoding='utf-8')
            with mock.patch.object(worker, 'ensure_supervisor'):
                self.assertTrue(flush.maybe_trigger_compile(root, dt.datetime(2026, 9, 6, 12)))
                flush.maybe_trigger_compile(root, dt.datetime(2026, 9, 6, 12))
            queue = worker.inspect_worker_queue(root / '.codex/scripts/.state/maintenance')
            self.assertEqual(queue['counts']['pending'], 1)

    def test_maintenance_drains_more_than_one_batch_and_retries_without_a_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed_vault(root)
            state = root / '.codex/scripts/.state'
            for day in ('2026-09-01', '2026-09-02'):
                (root / f'daily/{day}.md').write_text('Karar.', encoding='utf-8')
            worker.enqueue_maintenance(state, vault_root=root, start_supervisor=False)

            calls = []

            def run_codex(prompt, stage, **_kwargs):
                if not calls:
                    calls.append('transient')
                    return 'transient'
                calls.append('generation')
                _write_valid_compilation(stage)
                return None

            with mock.patch.object(compiler, '_run_codex', side_effect=run_codex), \
                 mock.patch.object(compiler, '_checkpoint_machine_outputs'), \
                 mock.patch.object(worker.time, 'sleep'), \
                 mock.patch.object(worker, '_retry_delay', return_value=0):
                worker.run_supervisor(root, state / 'maintenance', job_runner=lambda r, s, p: worker.execute_job_file(r, s, p))
            self.assertEqual(compile_state.changed_dailies(root), [])
            queue = worker.inspect_worker_queue(state / 'maintenance')
            self.assertEqual(calls, ['transient', 'generation', 'generation', 'generation', 'generation'])
            self.assertEqual(queue['counts']['succeeded'], 2)
            self.assertEqual(queue['counts']['dead-letter'], 0)

    def test_maintenance_budget_allows_schema_repair_and_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed_vault(root)
            (root / 'daily/2026-09-04.md').unlink()
            state = root / '.codex/scripts/.state'
            worker.enqueue_maintenance(state, vault_root=root, start_supervisor=False)
            calls = []

            def run_codex(prompt, stage, **_kwargs):
                if prompt.startswith('BELLEK ŞEMASI ONARIMI'):
                    calls.append('repair')
                    target = next((stage / 'knowledge/concepts').glob('gunluk-*.md'))
                    target.write_text(
                        target.read_text(encoding='utf-8').replace(
                            'schema: invalid', 'schema: knowledge-v2', 1
                        ),
                        encoding='utf-8',
                    )
                    return None
                calls.append('generation')
                _write_valid_compilation(stage, invalidate=True)
                return None

            with mock.patch.object(compiler, '_run_codex', side_effect=run_codex), \
                 mock.patch.object(compiler, '_checkpoint_machine_outputs'), \
                 mock.patch.object(worker.time, 'sleep'), \
                 mock.patch.object(worker, '_retry_delay', return_value=0):
                worker.run_supervisor(root, state / 'maintenance', job_runner=lambda r, s, p: worker.execute_job_file(r, s, p))

            queue = worker.inspect_worker_queue(state / 'maintenance')
            changed = compile_state.changed_dailies(root)
            published_index = (root / 'knowledge/index.md').read_text(encoding='utf-8')

        self.assertEqual(calls, ['generation', 'repair'])
        self.assertEqual(changed, [])
        self.assertEqual(queue['counts']['succeeded'], 1)
        self.assertEqual(queue['counts']['dead-letter'], 0)
        self.assertIn('gunluk-2026-09-03', published_index)


if __name__ == '__main__':
    unittest.main()
