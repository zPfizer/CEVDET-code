from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import compile as compiler


class CompileBatchRepairTests(unittest.TestCase):
    def test_one_repair_can_fix_all_reported_changed_files_but_not_unrelated_files(self):
        for touch_unrelated in (False, True):
            with self.subTest(touch_unrelated=touch_unrelated), tempfile.TemporaryDirectory() as tmp:
                stage = Path(tmp)
                notes = stage / 'knowledge/concepts'
                notes.mkdir(parents=True)
                for name in ('first', 'second', 'unrelated'):
                    (notes / f'{name}.md').write_text('original', encoding='utf-8')
                before = compiler._manifest(stage)
                for name in ('first', 'second'):
                    (notes / f'{name}.md').write_text('needs repair', encoding='utf-8')

                def repair(prompt, _stage):
                    self.assertIn('knowledge/concepts/second.md:derived-schema', prompt)
                    for name in ('first', 'second') + (('unrelated',) if touch_unrelated else ()):
                        (notes / f'{name}.md').write_text('repaired', encoding='utf-8')

                with patch.object(compiler, '_normalize_and_validate_stage', side_effect=[
                    compiler.PolicyError('knowledge-schema:knowledge/concepts/first.md:derived-schema'), None
                ]), patch.object(compiler, 'validate_knowledge_tree', return_value=SimpleNamespace(issues=(
                    'knowledge/concepts/first.md:derived-schema', 'knowledge/concepts/second.md:derived-schema', 'knowledge/concepts/unrelated.md:derived-schema'
                ))), patch.object(compiler, '_run_codex', side_effect=repair):
                    if touch_unrelated:
                        with self.assertRaisesRegex(compiler.PolicyError, 'schema-repair-forbidden-write:knowledge/concepts/unrelated.md'):
                            compiler._normalize_validate_with_single_repair(stage, Path('taxonomy'), before)
                    else:
                        self.assertIsNone(compiler._normalize_validate_with_single_repair(stage, Path('taxonomy'), before))


if __name__ == '__main__':
    unittest.main()
