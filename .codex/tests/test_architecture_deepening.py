from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import compile as compiler
import hook
import memory_ledger
import vault_retrieval
from test_second_brain_acceptance import _seed_vault, _deterministic_compiler


class ArchitectureDeepeningTests(unittest.TestCase):
    def test_rebuild_uses_its_runner_for_generation_and_schema_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault, output = root / 'vault', root / 'rebuilt'
            _seed_vault(vault)
            calls = []

            def runner(prompt, stage):
                repair = prompt.startswith('BELLEK ŞEMASI ONARIMI')
                calls.append(repair)
                _deterministic_compiler(prompt, stage)
                if not repair:
                    note = stage / 'knowledge/concepts/yerel-hafiza.md'
                    note.write_text(note.read_text(encoding='utf-8').replace(
                        'schema: knowledge-v2', 'schema: invalid'), encoding='utf-8')

            with mock.patch.object(compiler, '_run_codex', side_effect=AssertionError('unexpected model')):
                self.assertEqual(compiler.rebuild_knowledge(vault, output, runner=runner), 'created')
            self.assertEqual(calls, [False, True, False, True])
            self.assertIn('hafıza B düzenini', (output / 'concepts/yerel-hafiza.md').read_text(encoding='utf-8'))

    def test_forget_during_read_prevents_stale_result_on_every_read_surface(self):
        fact = 'Atlas için yerel önbellek seçildi; gerekçe çevrimdışı devamlılık.'
        for surface in ('startup', 'retrieval', 'source'):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                _seed_vault(vault)
                source = vault / '🔮 850-Companion/Last-Session.md'
                original = source.read_bytes()

                def forget():
                    memory_ledger.suppress_derived_memory(vault / '.codex/private-memory', fact)

                if surface == 'startup':
                    target, name = hook, '_bound_session_section'
                    operation = lambda: hook.build_session_context(vault, vault / '.codex/scripts/.state')
                elif surface == 'retrieval':
                    target, name = vault_retrieval, '_json_text'
                    operation = lambda: vault_retrieval.retrieve_vault_context_detailed(
                        vault, 'Atlas önbellek', write_cache=False)
                else:
                    target, name = Path, 'read_text'
                    operation = lambda: memory_ledger.read_memory_source(vault, source)
                original_call = getattr(target, name)
                changed = False

                def change_after_read(*args, **kwargs):
                    nonlocal changed
                    value = original_call(*args, **kwargs)
                    if not changed and (surface != 'source' or args[0] == source):
                        changed = True
                        forget()
                    return value

                with mock.patch.object(target, name, change_after_read):
                    with self.assertRaisesRegex(memory_ledger.MemoryPreferenceError, 'memory-preferences-changed'):
                        operation()
                self.assertTrue(changed)
                self.assertEqual(source.read_bytes(), original)
                self.assertNotIn(fact, memory_ledger.read_memory_source(vault, source))

    def test_startup_does_not_fall_back_to_a_source_created_after_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            source = vault / '🔮 850-Companion/Profile.md'
            source.unlink()
            fact = 'Ankara ikamet adresimdir.'
            memory_ledger.suppress_derived_memory(vault / '.codex/private-memory', fact)
            project = memory_ledger.materialize_memory_views

            def late_source(*args, **kwargs):
                views = project(*args, **kwargs)
                source.write_text(fact, encoding='utf-8')
                return views

            with mock.patch.object(memory_ledger, 'materialize_memory_views', side_effect=late_source):
                context = hook.build_session_context(vault, vault / '.codex/scripts/.state')
            self.assertNotIn('Ankara', context)
            self.assertEqual(source.read_text(encoding='utf-8'), fact)


if __name__ == '__main__':
    unittest.main()
