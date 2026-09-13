import argparse
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import companion_memory as companion
import flush
import hook
import user_evidence
import daily_store
from memory_ledger import mark_session_only, memory_read, suppress_derived_memory, load_suppressed_hashes


def summary(goal, pending):
    parts = {name: 'Yok.' for name in flush.EXPECTED_SECTIONS}
    parts['Bağlam'] = goal
    parts['Yapılacaklar'] = pending
    return flush.SessionSummary(parts).render()


def seed(root):
    folder = root / '🔮 850-Companion'
    folder.mkdir()
    for name in ('Last-Session', 'Threads', 'Journal'):
        (folder / f'{name}.md').write_text(f'# {name}\n\nİnsan notu.\n', encoding='utf-8')
    return folder


class SessionSnapshotTests(unittest.TestCase):
    def test_quoted_claim_cannot_retain_a_previously_verified_evidence_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            claim = 'Atlas hızlı olacak.'
            parts = flush.SessionSummary.parse(summary('Atlas', 'Test açık')).sections
            parts['Alınan Kararlar'] = '- ' + claim + ' <!-- user-source: {"quote":"Hız önemli","scope":"project"} -->'
            bound = user_evidence.bind_evidence(parts, [('user', 'Hız önemli')], time.isoformat())
            previous = flush.SessionSummary(bound).render()
            daily_store.publish(root, root / '.state', previous, 'turnend', time, idempotency_key='a'*64)
            for body, trusted in (
                ('> quoted lead\n' + claim, False),
                ('1.    item\n      > quoted lead\n      ' + claim, False),
                ('```\n' + claim + '\n```', False),
                ("[hidden]: /url 'title\n" + claim + "\n'", False),
                ('<span title="\n' + claim + '\n">label</span>', False),
                ('- ' + claim, True),
            ):
                with self.subTest(body=body):
                    current = dict(parts, **{'Alınan Kararlar': body})
                    carried = user_evidence.bind_evidence(
                        current, [], (time + dt.timedelta(days=1)).isoformat(),
                        previous_summary=previous, vault_root=root, memory_reader=memory_read,
                    )
                    self.assertEqual(bool(carried['Alınan Kararlar']), trusted)
                    self.assertEqual(bool(user_evidence.EVIDENCE.search(carried['Önemli Konuşmalar'])), trusted)

    def test_forgetting_original_daily_hides_carried_claim_and_quote(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed(root)
            state = root / '.state'
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            parts = flush.SessionSummary.parse(summary('Atlas', 'Test açık')).sections
            parts['Alınan Kararlar'] = '- Atlas hızlı olacak. <!-- user-source: {"quote":"Hız önemli","scope":"project"} -->'
            value = flush.SessionSummary(user_evidence.bind_evidence(parts, [('user', 'Hız önemli')], time.isoformat())).render()
            daily_store.publish(root, state, value, 'turnend', time, idempotency_key='a'*64)
            companion.publish(root, state, value, time, 'a'*64, 'atlas', frozenset())
            previous = companion.previous_summary(root, 'atlas')
            next_day = time + dt.timedelta(days=1)
            carried = flush.SessionSummary(user_evidence.bind_evidence(
                flush.SessionSummary.parse(previous).sections, [], next_day.isoformat(),
                previous_summary=previous, vault_root=root,
                memory_reader=memory_read)).render()
            daily_store.publish(root, state, carried, 'turnend', next_day, idempotency_key='b'*64)
            suppress_derived_memory(root / '.codex/private-memory', 'daily/2026-09-07.md')
            with memory_read(root) as memory:
                _path, text = memory.read_source(root / 'daily/2026-09-08.md')
            self.assertNotIn('Hız önemli', text)
            self.assertNotIn('Atlas hızlı olacak.', text)

    def test_prepared_daily_rebase_keeps_one_copy_of_carried_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            parts = flush.SessionSummary.parse(summary('Atlas', 'Test açık')).sections
            parts['Alınan Kararlar'] = '- Atlas hızlı olacak. <!-- user-source: {"quote":"Hız önemli","scope":"project"} -->'
            value = flush.SessionSummary(user_evidence.bind_evidence(parts, [('user', 'Hız önemli')], time.isoformat())).render()
            with self.assertRaisesRegex(RuntimeError, 'daily-injected:prepared'):
                daily_store.publish(root, state, value, 'turnend', time, idempotency_key='a'*64, _fail_after='prepared')
            daily_store.publish(root, state, value, 'turnend', time, idempotency_key='b'*64)
            daily_store.publish(root, state, value, 'turnend', time, idempotency_key='a'*64)
            self.assertFalse(daily_store.publish(root, state, value, 'turnend', time, idempotency_key='a'*64))
            text = (root / 'daily/2026-09-07.md').read_text(encoding='utf-8')
            self.assertEqual(len(user_evidence.EVIDENCE.findall(text)), 1)
            self.assertEqual(text.count('<!-- flush:'), 2)

    def test_forgotten_quote_is_not_hidden_in_escaped_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = seed(root)
            state = root / '.state'
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            parts = flush.SessionSummary.parse(summary('Rapor hazırlanıyor.', 'Test açık')).sections
            parts['Alınan Kararlar'] = '- Atlas raporu pazartesi. <!-- user-source: {"quote":"Atlas hızlı olsun","scope":"project"} -->'
            bound = flush.SessionSummary(user_evidence.bind_evidence(parts, [('user', 'Atlas hızlı olsun')], time.isoformat())).render()
            flush.append_daily(root, state, bound, 'turnend', time, idempotency_key='a'*64)
            companion.publish(root, state, bound, time, 'a'*64, 'one', frozenset())
            # Old releases escaped metadata; migration must remove that hidden copy too.
            path = folder / 'Last-Session.md'
            raw = path.read_text(encoding='utf-8')
            metadata = user_evidence.EVIDENCE.search(bound)[0].replace('<!--', '&lt;!--')
            path.write_text(raw.replace('## Önemli Konuşmalar\n', '## Önemli Konuşmalar\n' + metadata + '\n'), encoding='utf-8')
            suppress_derived_memory(root / '.codex/private-memory', 'Atlas hızlı olsun')
            previous = companion.previous_summary(root, 'one')
            self.assertNotIn('Atlas hızlı olsun', previous)
            self.assertNotIn('user-evidence:', previous)
            self.assertNotIn('Atlas raporu pazartesi.', previous)

    def test_compacted_transcript_retains_verified_identity_across_days_without_duplicate_proofs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed(root)
            state = root / '.state'
            state.mkdir()
            transcript = root / 'source.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content': 'Atlas raporu pazartesi hazır olsun.'}), encoding='utf-8')
            payload = root / 'input.json'
            payload.write_text(json.dumps({'session_id': 'atlas', 'transcript_path': str(transcript)}), encoding='utf-8')
            args = argparse.Namespace(hook_input=payload, reason='turnend')
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            parts = flush.SessionSummary.parse(summary('Atlas raporu', 'Test açık')).sections
            parts['Alınan Kararlar'] = '- Atlas raporu pazartesi hazır olacak. <!-- user-source: {"quote":"Atlas raporu pazartesi hazır olsun.","scope":"project"} -->'
            with mock.patch.object(flush, 'run_codex', return_value=(flush.SessionSummary(parts).render(), None)), mock.patch.object(flush, 'maybe_trigger_compile'):
                self.assertEqual(flush.flush_once(args, time, root, state), 0)
            previous = companion.previous_summary(root, 'atlas')
            original_link = user_evidence.USER_LINK.search(previous)[0]
            parts = flush.SessionSummary.parse(previous).sections
            parts['Yapılacaklar'] = 'Yayın iptal. Test açık.'
            for offset, message in ((1, 'Yayın iptal.'), (1440, 'Test tamamlandı.')):
                transcript.write_text(json.dumps({'role': 'user', 'content': message}), encoding='utf-8')
                with mock.patch.object(flush, 'run_codex', return_value=(flush.SessionSummary(parts).render(), None)), mock.patch.object(flush, 'maybe_trigger_compile'):
                    self.assertEqual(flush.flush_once(args, time + dt.timedelta(minutes=offset), root, state), 0)
                self.assertIn(original_link, companion.previous_summary(root, 'atlas'))
            for day in ('2026-09-07', '2026-09-08'):
                daily = (root / f'daily/{day}.md').read_text(encoding='utf-8')
                self.assertEqual(len(user_evidence.EVIDENCE.findall(daily)), 1)
            suppress_derived_memory(root / '.codex/private-memory', 'Atlas raporu pazartesi hazır olsun.')
            with memory_read(root) as memory:
                _path, hidden = memory.read_source(root / 'daily/2026-09-08.md')
            self.assertNotIn('Atlas raporu pazartesi hazır olacak.', hidden)

    def test_legacy_snapshot_is_preserved_without_becoming_new_session_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = seed(root)
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            value = summary('Eski amaç', 'Eski açık iş').replace('## ', '### ')
            legacy = f'{companion.BEGIN}{time.timestamp()} {"a"*64} -->\n## Session: {time.isoformat()}\n\n{value}\n\n## Previous Sessions\n{companion.END}'
            with (folder / 'Last-Session.md').open('a', encoding='utf-8') as stream:
                stream.write(legacy)
            companion.publish(root, root / '.state', summary('Yeni amaç', 'Yeni iş'), time + dt.timedelta(minutes=1), 'b'*64, 'new', frozenset())
            self.assertIn('Eski amaç', (folder / 'Last-Session.md').read_text(encoding='utf-8'))
            self.assertNotIn('Eski amaç', companion.previous_summary(root, 'new'))

    def test_forgetting_filters_carried_context_and_other_session_surfaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = seed(root)
            state = root / '.state'
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            companion.publish(root, state, summary('Silinecek ayrıntı', 'Korunacak iş'), time, 'a'*64, 'one', frozenset())
            private = root / '.codex/private-memory'
            suppress_derived_memory(private, 'Silinecek ayrıntı')
            self.assertNotIn('Silinecek ayrıntı', companion.previous_summary(root, 'one'))
            companion.publish(root, state, summary('İkinci amaç', 'İkinci iş'), time, 'b'*64, 'two', load_suppressed_hashes(private))
            self.assertNotIn('Silinecek ayrıntı', (folder / 'Last-Session.md').read_text(encoding='utf-8'))
            self.assertIn('Korunacak iş', (folder / 'Threads.md').read_text(encoding='utf-8'))

    def test_sessions_survive_interleaved_updates_and_stale_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = seed(root)
            state = root / '.state'
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            companion.publish(root, state, summary('Atlas amacı', 'Atlas testi açık'), time, 'a'*64, 'atlas', frozenset())
            companion.publish(root, state, summary('Bora amacı', 'Bora hesabı açık'), time + dt.timedelta(minutes=1), 'b'*64, 'bora', frozenset())
            self.assertIn('Atlas amacı', companion.previous_summary(root, 'atlas'))
            companion.publish(root, state, summary('Atlas amacı korunur; gerekçe hız.', 'Atlas testi tamamlandı; yayın iptal.'), time + dt.timedelta(minutes=2), 'c'*64, 'atlas', frozenset())
            companion.publish(root, state, summary('Eski Atlas', 'Atlas testi açık'), time, 'a'*64, 'atlas', frozenset())
            last = (folder / 'Last-Session.md').read_text(encoding='utf-8')
            threads = (folder / 'Threads.md').read_text(encoding='utf-8')
            self.assertIn('Bora amacı', last)
            self.assertIn('Atlas amacı korunur', last)
            self.assertNotIn('Eski Atlas', last)
            self.assertNotIn('Atlas testi açık', threads)
            self.assertIn('Bora hesabı açık', threads)
            self.assertIn('yayın iptal', threads)
            self.assertIn('İnsan notu.', last)
            self.assertIn('Bora amacı', hook._last_session(folder / 'Last-Session.md'))

    def test_new_batch_receives_only_its_own_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed(root)
            state = root / '.state'
            state.mkdir()
            transcript = root / 'source.jsonl'
            transcript.write_text(json.dumps({'role': 'user', 'content': 'Atlas amacı: hız.'}), encoding='utf-8')
            payload = root / 'input.json'
            payload.write_text(json.dumps({'session_id': 'atlas', 'transcript_path': str(transcript)}), encoding='utf-8')
            args = argparse.Namespace(hook_input=payload, reason='turnend')
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            with mock.patch.object(flush, 'run_codex', return_value=(summary('Atlas amacı: hız.', 'Atlas testi açık'), None)), mock.patch.object(flush, 'maybe_trigger_compile'):
                self.assertEqual(flush.flush_once(args, time, root, state), 0)
            companion.publish(root, state, summary('Bora özel bağlamı', 'Bora işi'), time, 'b'*64, 'bora', frozenset())
            with transcript.open('a', encoding='utf-8') as stream:
                stream.write('\n' + json.dumps({'role': 'user', 'content': 'Test tamamlandı, yayından vazgeçtim.'}))
            with mock.patch.object(flush, 'run_codex', return_value=(summary('Atlas amacı: hız.', 'Test tamamlandı; yayın iptal.'), None)) as model, mock.patch.object(flush, 'maybe_trigger_compile'):
                self.assertEqual(flush.flush_once(args, time + dt.timedelta(minutes=1), root, state), 0)
            prompt = model.call_args.args[0]
            self.assertIn('Atlas amacı: hız.', prompt)
            self.assertIn('Test tamamlandı, yayından vazgeçtim.', prompt)
            self.assertNotIn('Bora özel bağlamı', prompt)

    def test_contextual_need_keeps_previous_proposal_at_batch_boundary_and_in_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed(root)
            state = root / '.state'
            transcript = root / 'source.jsonl'
            proposal = 'Bu işte tercihleri her yanıtta bağlama göre uygulayacağım.'
            quote = 'Bunun gerçekten olması lazım.'
            records = [
                json.dumps({'role': 'user' if index % 2 == 0 else 'assistant', 'content': 'x' * 520})
                for index in range(28)
            ]
            records.extend((
                json.dumps({'role': 'assistant', 'content': proposal}),
                json.dumps({'role': 'user', 'content': quote + ' ' + ('B' * 200)}),
            ))
            transcript.write_text('\n'.join(records), encoding='utf-8')
            payload = root / 'input.json'
            payload.write_text(json.dumps({'session_id': 'atlas', 'transcript_path': str(transcript)}), encoding='utf-8')
            args = argparse.Namespace(hook_input=payload, reason='turnend')
            time = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
            second = summary('Uyarlama gerekliliği', 'Test açık').replace(
                '## Alınan Kararlar\n',
                '## Alınan Kararlar\n- Bağlama uygun uyarlama gerekli. <!-- user-source: '
                + json.dumps({'quote': quote, 'scope': 'unspecified'}, ensure_ascii=False)
                + ' -->\n',
            )
            with mock.patch.object(flush, 'run_codex', side_effect=[
                (summary('İlk öneri', 'İkinci parça bekliyor'), None),
                (second, None),
            ]) as model, mock.patch.object(flush, 'maybe_trigger_compile'), mock.patch(
                'worker_supervisor.enqueue_flush',
            ):
                self.assertEqual(flush.flush_once(args, time, root, state), 0)
                self.assertEqual(flush.flush_once(args, time + dt.timedelta(minutes=1), root, state), 0)
            prompt = model.call_args_list[1].args[0]
            self.assertIn(proposal[:100], prompt)
            self.assertIn(quote, prompt)
            daily = (root / 'daily/2026-09-07.md').read_text(encoding='utf-8')
            record = json.loads(user_evidence.EVIDENCE.search(daily)[1])
            self.assertEqual(record['previous_assistant'], proposal)
            self.assertEqual(record['scope'], 'session')

    def test_session_only_does_not_read_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed(root)
            state = root / '.codex/scripts/.state'
            companion.publish(root, state, summary('Özel hedef', 'Bekleyen iş'), dt.datetime.now(dt.timezone.utc), 'a'*64, 'private', frozenset())
            mark_session_only(state, 'private')
            self.assertEqual(companion.previous_summary(root, 'private', state=state), '')


if __name__ == '__main__':
    unittest.main()
