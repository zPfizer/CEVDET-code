import argparse
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import flush
import hook
import knowledge_schema as schema
import memory_ledger as ledger
import profile_guard
import user_evidence as evidence
import vault_retrieval as retrieval
from test_user_evidence import decision, sections, STAMP
from test_profile_guard import seed_profile, PROFILE_TEXT


def concept(row):
    return f'''---
schema: knowledge-v2
title: Tercih
aliases: []
tags: [hafıza]
sources: [2026-09-06.md]
created: 2026-09-06
updated: 2026-09-06
---
# Tercih
Kaynaklı tercih.
## Önemli Noktalar
- Bir
- İki
- Üç
## Detaylar
Kullanıcı dayanağı korunur.
## İlgili Kavramlar
- [[knowledge/concepts/bir|Bir]]
- [[knowledge/concepts/iki|İki]]
## Kaynaklar
- [[daily/2026-09-06|Kaynak]]
## Kayıtlar
{row}
'''


def proof_fixture(root, request='Bundan sonra kısa yanıt ver.', scope='general'):
    claim = 'Kısa yanıt tercih ediliyor.'
    bound = evidence.bind_evidence(sections(decision(claim, request, scope)), [('user', request)], STAMP)
    daily = flush.SessionSummary(bound).render()
    record = json.loads(evidence.EVIDENCE.search(daily)[1])
    day = root / 'daily/2026-09-06.md'
    day.parent.mkdir(parents=True, exist_ok=True)
    day.write_text(daily, encoding='utf-8')
    row = f'- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-06 [[daily/2026-09-06#user-{record["id"]}|Kaynak]] — {claim}'
    note = root / 'knowledge/concepts/tercih-kisa.md'
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(concept(row), encoding='utf-8')
    (root / 'knowledge/index.md').write_text('# Bilgi Tabanı: İndeks\n\n' + schema.INDEX_HEADER + '\n| --- | --- | --- | --- |\n'
        '| [[concepts/tercih-kisa\\|Tercih]] | Kaynaklı tercih. | 2026-09-06.md | 2026-09-06 |\n', encoding='utf-8')
    (root / 'knowledge/log.md').write_text('# Derleme Günlüğü\n', encoding='utf-8')
    return record, row, note


class QualityPipelineTests(unittest.TestCase):
    def test_uncertain_user_claim_needs_new_evidence_to_become_current(self):
        for anchored in (False, True):
            with self.subTest(anchored=anchored), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                record, row, note = proof_fixture(root)
                current = note.read_text(encoding='utf-8')
                if not anchored:
                    current = current.replace('#user-' + record['id'], '')
                previous = current.replace('`kullanici-dusuncesi` `guncel`', '`kullanici-dusuncesi` `belirsiz`')
                note.write_text(current, encoding='utf-8')
                relative = 'knowledge/concepts/tercih-kisa.md'
                report = schema.validate_knowledge_tree(root, changed_paths=[relative], previous_texts={relative: previous})
                self.assertTrue(any('user-evidence' in issue for issue in report.issues))

    def test_unchanged_proof_can_cross_a_bounded_stage_but_not_be_reactivated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, row, note = proof_fixture(root)
            previous = note.read_text(encoding='utf-8')
            relative = 'knowledge/concepts/tercih-kisa.md'
            # A later compiler stage contains its new daily input, not every old day.
            day = root / 'daily/2026-09-06.md'
            day_text = day.read_text(encoding='utf-8')
            day.unlink()
            note.write_text(previous + '\n', encoding='utf-8')
            report = schema.validate_knowledge_tree(root, changed_paths=[relative], previous_texts={relative: previous})
            self.assertFalse(any('user-evidence' in issue for issue in report.issues))
            day.write_text(day_text, encoding='utf-8')
            past = previous.replace('`gecerli` `kullanici-dusuncesi`', '`gecmis` `kullanici-dusuncesi`')
            note.write_text(previous, encoding='utf-8')
            report = schema.validate_knowledge_tree(root, changed_paths=[relative], previous_texts={relative: past})
            self.assertTrue(any('user-evidence' in issue for issue in report.issues))

    def test_retiring_a_claim_cannot_remove_its_evidence_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, row, note = proof_fixture(root)
            previous = note.read_text(encoding='utf-8')
            retired = previous.replace('#user-' + record['id'], '').replace('`gecerli` `kullanici-dusuncesi`', '`gecmis` `kullanici-dusuncesi`')
            note.write_text(retired, encoding='utf-8')
            relative = 'knowledge/concepts/tercih-kisa.md'
            report = schema.validate_knowledge_tree(root, changed_paths=[relative], previous_texts={relative: previous})
            self.assertTrue(any('user-evidence' in issue for issue in report.issues))

    def test_cached_knowledge_is_not_emitted_after_its_daily_proof_disappears(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, row, note = proof_fixture(root)
            first = retrieval.retrieve_vault_context_detailed(root, 'Kısa yanıt tercih ediliyor')
            self.assertIn('Kısa yanıt tercih ediliyor.', first.text)
            day = root / 'daily/2026-09-06.md'
            day.write_text(evidence.EVIDENCE.sub('', day.read_text(encoding='utf-8')), encoding='utf-8')
            second = retrieval.retrieve_vault_context_detailed(root, 'Kısa yanıt tercih ediliyor')
            self.assertNotIn('Kısa yanıt tercih ediliyor.', second.text)

    def test_long_claim_excerpt_keeps_its_status_without_asserting_a_clipped_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, row, note = proof_fixture(root)
            long_claim = 'Karar A uygulanacak. ' + 'ayrıntı ' * 90 + 'Henüz onaylanmadı.'
            note.write_text(concept('- `gecerli` `cevo-cikarimi` `belirsiz` 2026-09-06 [[daily/2026-09-06|Kaynak]] — ' + long_claim), encoding='utf-8')
            result = retrieval.retrieve_vault_context_detailed(root, 'Karar A uygulanacak', write_cache=False)
            self.assertNotIn('Karar A uygulanacak.', result.text)
            self.assertIn('belirsiz', result.text)
            self.assertIn('tam kaynağı oku', result.text)

    def test_an_existing_evidence_anchor_cannot_be_stripped_as_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, row, note = proof_fixture(root, 'Bu cevapta ayrıntılı anlat.')
            previous = note.read_text(encoding='utf-8')
            note.write_text(previous.replace('#user-' + record['id'], ''), encoding='utf-8')
            relative = 'knowledge/concepts/tercih-kisa.md'
            report = schema.validate_knowledge_tree(root, changed_paths=[relative], previous_texts={relative: previous})
            self.assertTrue(any('user-evidence' in issue for issue in report.issues))

    def test_new_user_claim_requires_matching_immutable_daily_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, row, note = proof_fixture(root)
            changed = ['knowledge/concepts/tercih-kisa.md']
            report = schema.validate_knowledge_tree(root, changed_paths=changed)
            self.assertEqual(report.issues, ())
            for invalid in (row.replace('#user-' + record['id'], ''), row.replace('Kısa yanıt tercih ediliyor.', 'Uzun yanıt tercih ediliyor.')):
                note.write_text(concept(invalid), encoding='utf-8')
                self.assertTrue(any('user-evidence' in issue for issue in schema.validate_knowledge_tree(root, changed_paths=changed).issues))

    def test_unchanged_legacy_claim_is_preserved_but_new_unbacked_claim_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, row, note = proof_fixture(root)
            legacy = concept(row.replace('#user-' + record['id'], ''))
            note.write_text(legacy, encoding='utf-8')
            relative = 'knowledge/concepts/tercih-kisa.md'
            report = schema.validate_knowledge_tree(root, changed_paths=[relative], previous_texts={relative: legacy})
            self.assertEqual(report.issues, ())
            note.write_text(legacy.replace('Kısa yanıt tercih ediliyor.', 'Uzun yanıt tercih ediliyor.'), encoding='utf-8')
            self.assertTrue(any('user-evidence' in issue for issue in schema.validate_knowledge_tree(root, changed_paths=[relative], previous_texts={relative: legacy}).issues))

    def test_profile_accepts_general_proof_but_not_session_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            record, row, note = proof_fixture(root)
            profile = PROFILE_TEXT.replace('2026-09-05', '2026-09-06').replace('2026-09-04.', '2026-09-06.').replace('Türkçe ve kısa yanıt ver', 'Kısa yanıt tercih ediliyor.')
            path.write_text(profile, encoding='utf-8')
            self.assertEqual(profile_guard.check_profile(root), ())
            proof_fixture(root, 'Bu cevapta ayrıntılı anlat.', 'general')
            self.assertIn('profile-claim-scope', profile_guard.check_profile(root))

    def test_forget_removes_encoded_evidence_and_dependent_paraphrase_on_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = 'Bundan sonra "kısa ve net" yanıt ver.'
            record, row, note = proof_fixture(root, request)
            originals = {p: p.read_bytes() for p in (note, root / 'daily/2026-09-06.md')}
            ledger.suppress_derived_memory(root / '.codex/private-memory', request)
            self.assertNotIn('Kısa yanıt tercih ediliyor.', ledger.read_memory_source(root, note))
            self.assertNotIn('user-evidence:', ledger.read_memory_source(root, root / 'daily/2026-09-06.md'))
            self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def test_overflow_never_keeps_a_decision_without_its_qualifier(self):
        text = 'Karar A uygulanacak. ' + 'ayrıntı ' * 90 + 'Henüz onaylanmadı.' + ' gerekçe' * 90
        bounded = hook._bound_session_section('Son Oturum', text)
        self.assertNotIn('Karar A uygulanacak.', bounded)
        self.assertIn('Tam son oturum', bounded)
        self.assertLessEqual(len(bounded), hook.SESSION_SECTION_TARGET_CHARS['Son Oturum'])

    def test_original_user_evidence_survives_prepared_retry_without_second_model_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.codex/scripts/.state'
            state.mkdir(parents=True)
            request = 'Bundan sonra kısa yanıt ver.'
            transcript = state / 'transcript.jsonl'
            transcript.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': request}}), encoding='utf-8')
            payload = {'session_id': 'evidence-retry', 'transcript_path': str(transcript)}
            args = argparse.Namespace(reason='turnend', hook_input=state / 'unused.json')
            raw = flush.SessionSummary(sections(decision('Kısa yanıt tercih ediliyor.', request))).render()
            append = flush.append_daily
            def committed_then_interrupted(*args, **kwargs):
                append(*args, **kwargs)
                raise OSError('injected-after-commit')
            with (mock.patch.object(flush, 'run_codex', return_value=(raw, None)) as model,
                  mock.patch.object(flush, 'maybe_trigger_compile', return_value=False)):
                with mock.patch.object(flush, 'append_daily', side_effect=committed_then_interrupted):
                    self.assertEqual(flush.flush_once(args, dt.datetime.fromisoformat(STAMP), root, state, hook_input=payload), 1)
                self.assertEqual(flush.flush_once(args, dt.datetime.fromisoformat(STAMP) + dt.timedelta(days=1), root, state, hook_input=payload), 0)
                self.assertEqual(model.call_count, 1)
            day = (root / 'daily/2026-09-06.md').read_text(encoding='utf-8')
            self.assertEqual(len(evidence.EVIDENCE.findall(day)), 1)
            self.assertEqual(day.count('<!-- flush:'), 1)


if __name__ == '__main__':
    unittest.main()
