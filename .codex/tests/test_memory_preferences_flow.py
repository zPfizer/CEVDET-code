import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import compile as memory_compile
import daily_store
import flush
import hook
import memory_ledger
from test_second_brain_acceptance import _seed_vault, _deterministic_compiler


NOW = dt.datetime.fromisoformat('2026-09-05T10:00:00+03:00')
FACT = "Levent Ankara'da yaşıyor."
VARIANT = "Levent'in yaşadığı şehir Ankara."


class MemoryPreferencesFlowTests(unittest.TestCase):
    def test_read_views_are_invalidated_then_rebuilt_from_original_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            a, b = vault / 'a.md', vault / 'b.md'
            linked_fact = 'Ankara ikamet adresimdir [[b|kaynak]].'
            a.write_text(linked_fact + '\nPlan pazartesi.\n[Diğer not](b.md)\n', encoding='utf-8')
            b.write_text(VARIANT + '\n', encoding='utf-8')
            originals = (a.read_bytes(), b.read_bytes())
            views = memory_ledger.materialize_memory_views(vault, [('a.md', 'A'), ('b.md', 'B')], frozenset())
            self.assertIn((vault / views['b.md']).as_posix(), (vault / views['a.md']).read_text(encoding='utf-8'))
            for unit in (linked_fact, VARIANT):
                memory_ledger.suppress_derived_memory(vault / '.codex/private-memory', unit)
            visible = ''.join((vault / path).read_text(encoding='utf-8') for path in views.values())
            self.assertNotIn('Ankara', visible)
            memory_ledger.materialize_memory_views(vault, [('a.md', 'A'), ('b.md', 'B')],
                memory_ledger.load_suppressed_hashes(vault / '.codex/private-memory'))
            visible = ''.join((vault / path).read_text(encoding='utf-8') for path in views.values())
            self.assertNotIn('Ankara', visible)
            self.assertIn('pazartesi', visible)
            self.assertEqual((a.read_bytes(), b.read_bytes()), originals)

    def test_view_directory_cannot_redirect_writes_outside_the_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault, outside = root / 'vault', root / 'outside'
            private = vault / '.codex/private-memory'
            private.mkdir(parents=True)
            outside.mkdir()
            source = vault / 'source.md'
            source.write_text('Plan pazartesi.', encoding='utf-8')
            (private / 'views').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(memory_ledger.MemoryPreferenceError):
                memory_ledger.materialize_memory_views(vault, [('source.md', 'Plan')], frozenset())
            self.assertEqual(list(outside.iterdir()), [])

    def test_model_stage_omits_sensitive_filenames_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            concepts = stage / 'knowledge/concepts'
            concepts.mkdir(parents=True)
            (concepts / 'Ankara.md').write_text('# Ankara\nGizli.\n', encoding='utf-8')
            kept = concepts / 'plan.md'
            kept.write_text('# Plan\nPlan pazartesi.\n[[Ankara]]\n', encoding='utf-8')
            hashes = frozenset({memory_ledger.memory_text_hash('Ankara')})
            self.assertTrue(memory_compile._project_stage_memory(stage, hashes))
            self.assertFalse((concepts / 'Ankara.md').exists())
            self.assertEqual(kept.read_text(encoding='utf-8'), '# Plan\nPlan pazartesi.\n')

    def test_resolved_variants_are_hidden_at_startup_publish_and_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / 'vault'
            _seed_vault(vault)
            private = vault / '.codex/private-memory'
            sources = list((vault / '🔮 850-Companion').glob('*.md')) + list((vault / 'daily').glob('*.md'))
            for path in sources:
                path.write_text(path.read_text(encoding='utf-8') + '\n' + FACT + '\n' + VARIANT + '\n', encoding='utf-8')
            original = {path: path.read_bytes() for path in sources}
            # Semantic resolution is an agent responsibility; these are its exact units.
            for unit in (FACT, VARIANT):
                memory_ledger.suppress_derived_memory(private, unit)
            state = vault / '.codex/scripts/.state'
            context = hook.build_session_context(vault, state, now=NOW)
            self.assertNotIn('Ankara', context)
            daily_store.publish(vault, state, FACT + '\nHaftalık plan pazartesi.\n',
                'turnend', NOW, idempotency_key='a' * 64)
            published = (vault / 'daily/2026-09-05.md').read_text(encoding='utf-8')
            self.assertNotIn('Ankara', published)
            self.assertIn('pazartesi', published)

            def projected_compiler(prompt, stage):
                self.assertNotIn('Ankara', prompt)
                self.assertNotIn('Ankara', ''.join(p.read_text(encoding='utf-8') for p in (stage / 'daily').glob('*.md')))
                return _deterministic_compiler(prompt, stage)

            # The publication above is tested separately; the original fixture has two
            # schema-valid compiler days. Use only those two for the rebuild runner.
            (vault / 'daily/2026-09-05.md').unlink()
            result = memory_compile.rebuild_knowledge(vault, root / 'rebuilt', runner=projected_compiler)
            self.assertEqual(result, 'created')
            self.assertNotIn('Ankara', ''.join(p.read_text(encoding='utf-8') for p in (root / 'rebuilt').rglob('*.md')))
            self.assertEqual({path: path.read_bytes() for path in sources}, original)

    def test_changed_preference_stops_a_model_result_then_retry_excludes_it(self):
        for control in ('forget', 'session-only'):
            with self.subTest(control=control), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                state = vault / '.codex/scripts/.state'
                state.mkdir(parents=True)
                transcript = vault / 'source.jsonl'
                transcript.write_text(json.dumps({'role': 'user', 'content': FACT}), encoding='utf-8')
                payload = vault / 'input.json'
                payload.write_text(json.dumps({'session_id': 'late-control', 'transcript_path': str(transcript)}), encoding='utf-8')
                args = argparse.Namespace(hook_input=payload, reason='turnend')

                def late_result(_prompt, _vault):
                    if control == 'forget':
                        memory_ledger.suppress_derived_memory(vault / '.codex/private-memory', FACT)
                    else:
                        memory_ledger.mark_session_only(state, 'late-control')
                    return '\n'.join(f'## {section}\n{VARIANT}' for section in flush.EXPECTED_SECTIONS), None

                with mock.patch.object(flush, 'run_codex', side_effect=late_result):
                    self.assertEqual(flush.flush_once(args, NOW, vault, state), 1)
                self.assertFalse((vault / 'daily/2026-09-05.md').exists())
                with mock.patch.object(flush, 'run_codex') as model:
                    self.assertEqual(flush.flush_once(args, NOW, vault, state), 0)
                    model.assert_not_called()
                self.assertFalse((vault / 'daily/2026-09-05.md').exists())

    def test_forget_during_compile_prevents_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            source = vault / 'daily/2026-09-03.md'
            original_index = (vault / 'knowledge/index.md').read_bytes()

            def late_control(_prompt, _stage):
                memory_ledger.suppress_derived_memory(vault / '.codex/private-memory', FACT)
                return None

            reason, _ = memory_compile._compile_one(vault, vault / '.codex/scripts/.state',
                source, hashlib.sha256(source.read_bytes()).hexdigest(), NOW.isoformat(), runner=late_control)
            self.assertEqual(reason, 'memory-preferences-changed')
            self.assertEqual((vault / 'knowledge/index.md').read_bytes(), original_index)


if __name__ == '__main__':
    unittest.main()
