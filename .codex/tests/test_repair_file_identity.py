from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _fixtures import CODEX_DIR
import compile as compiler
from test_knowledge_provenance import _write_tree


class RepairFileIdentityTests(unittest.TestCase):
    def test_index_repair_cannot_change_same_named_concept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concept = _write_tree(root)
            concept = concept.rename(concept.with_name('index.md'))
            index = root / 'knowledge/index.md'
            valid_index = index.read_text(encoding='utf-8').replace('concepts/ornek', 'concepts/index')
            index.write_text(valid_index, encoding='utf-8')
            before = compiler._manifest(root)
            index.write_text('\n'.join(line for line in valid_index.splitlines()
                                       if 'concepts/index' not in line) + '\n', encoding='utf-8')

            def repair(_prompt, _stage):
                index.write_text(valid_index, encoding='utf-8')
                concept.write_text(concept.read_text(encoding='utf-8').replace('Detay.', 'Başka detay.'), encoding='utf-8')

            # Use the real validators and file writes; only the model is replaced.
            with patch.object(compiler, '_run_codex', side_effect=repair):
                with self.assertRaisesRegex(compiler.PolicyError,
                        'schema-repair-forbidden-write:knowledge/concepts/index.md'):
                    compiler._normalize_validate_with_single_repair(
                        root, CODEX_DIR / 'tag-taxonomy.json', before)


if __name__ == '__main__':
    unittest.main()
