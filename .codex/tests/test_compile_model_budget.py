import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import compile as compiler
import compile_state
import memory_ledger
from test_second_brain_acceptance import _deterministic_compiler, _seed_vault


class CompileModelBudgetTests(unittest.TestCase):
    def test_run_codex_uses_explicit_active_usage_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active_state = root / 'active'
            stage = active_state / 'compile-stage-test'
            stage.mkdir(parents=True)
            fallback_state = root / 'fallback'

            with patch.object(compiler, 'STATE_DIR', fallback_state), \
                    patch.object(compiler.codex_runner, '_bounded_exec', return_value=(None, None)):
                self.assertIsNone(compiler._run_codex('prompt', stage, state_dir=active_state))

            self.assertTrue(list(active_state.glob('model-usage-*.jsonl')))
            self.assertEqual(list(fallback_state.glob('model-usage-*.jsonl')), [])

    def test_run_codex_accounts_for_file_backed_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            state_dir = stage / '.state'
            prompts = ('short', 'long prompt ' * 100)
            wrapper = (
                'Read .__alf4_compile_prompt.md. Follow its instructions.'
                ' Do not modify that file.'
            )

            with patch.object(compiler, 'STATE_DIR', state_dir), \
                    patch.object(compiler.codex_runner, '_bounded_exec', return_value=(None, None)) as execute:
                for prompt in prompts:
                    self.assertIsNone(compiler._run_codex(prompt, stage))

            entries = [
                json.loads(line)
                for path in state_dir.glob('model-usage-*.jsonl')
                for line in path.read_text(encoding='utf-8').splitlines()
            ]

        self.assertEqual([item['prompt_chars'] for item in entries], [len(item) for item in prompts])
        self.assertEqual([item['purpose'] for item in entries], ['compile', 'compile'])
        self.assertEqual([item['outcome'] for item in entries], ['ok', 'ok'])
        self.assertEqual([item.args[0] for item in execute.call_args_list], [wrapper, wrapper])

    def test_repair_counts_against_budget_and_leaves_invalid_daily_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state_dir = vault / '.codex/scripts/.state'
            calls = []

            def invalid_generation(_prompt, stage, **_kwargs):
                calls.append('model')
                (stage / 'knowledge/index.md').write_text('broken-index\n', encoding='utf-8')

            with patch.object(compiler, '_run_codex', side_effect=invalid_generation), \
                    patch.object(compiler, '_checkpoint_machine_outputs', return_value=('clean', '')):
                failed = compiler._run_locked(vault, state_dir, False, 1, None)

            state = compile_state.load(state_dir)
            pending = [path.name for path, _digest in compiler.changed_dailies(vault, state)]
            health = (state_dir / 'health.json').read_text(encoding='utf-8')
            index_valid = (vault / 'knowledge/index.md').read_text(encoding='utf-8').startswith(
                '# Bilgi Tabanı: İndeks'
            )

        self.assertTrue(failed)
        self.assertEqual(calls, ['model'])
        self.assertEqual(state.ingested, {})
        self.assertEqual(pending, ['2026-09-03.md', '2026-09-04.md'])
        self.assertTrue(index_valid)
        self.assertIn('model-call-budget-exhausted', health)

    def test_strict_main_reports_budget_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state_dir = vault / '.codex/scripts/.state'

            def invalid_generation(_prompt, stage, **_kwargs):
                (stage / 'knowledge/index.md').write_text('broken-index\n', encoding='utf-8')

            with patch.object(compiler, 'VAULT_ROOT', vault), \
                    patch.object(compiler, 'STATE_DIR', state_dir), \
                    patch.object(compiler, '_run_codex', side_effect=invalid_generation):
                exit_code = compiler.main(['--strict', '--max-calls', '1'])

            state = compile_state.load(state_dir)

        self.assertEqual(exit_code, 1)
        self.assertEqual(state.last_status, 'fail:schema-repair')

    def test_clean_budget_boundary_leaves_later_daily_for_next_run_without_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state_dir = vault / '.codex/scripts/.state'
            calls = []

            def valid_generation(prompt, stage, **_kwargs):
                calls.append(prompt)
                return _deterministic_compiler(prompt, stage)

            with patch.object(compiler, '_run_codex', side_effect=valid_generation), \
                    patch.object(compiler, '_checkpoint_machine_outputs', return_value=('clean', '')):
                failed = compiler._run_locked(vault, state_dir, False, 1, None)

            state = compile_state.load(state_dir)
            pending = [path.name for path, _digest in compiler.changed_dailies(vault, state)]

        self.assertFalse(failed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(tuple(state.ingested), ('2026-09-03.md',))
        self.assertEqual(pending, ['2026-09-04.md'])

    def test_excluded_daily_does_not_consume_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            (vault / 'daily/2026-09-04.md').unlink()
            (vault / 'daily/2026-09-02.md').write_text('# Günlük\n', encoding='utf-8')
            memory_ledger.suppress_derived_memory(
                vault / '.codex/private-memory', '2026-09-02.md'
            )
            state_dir = vault / '.codex/scripts/.state'
            calls = []

            def valid_generation(prompt, stage, **_kwargs):
                calls.append(prompt)
                return _deterministic_compiler(prompt, stage)

            with patch.object(compiler, '_run_codex', side_effect=valid_generation), \
                    patch.object(compiler, '_checkpoint_machine_outputs', return_value=('clean', '')):
                failed = compiler._run_locked(vault, state_dir, False, 1, None)

            state = compile_state.load(state_dir)

        self.assertFalse(failed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(state.ingested), {'2026-09-02.md', '2026-09-03.md'})


if __name__ == '__main__':
    unittest.main()
