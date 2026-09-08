from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import tag_taxonomy


class SemanticTagRepairTests(unittest.TestCase):
    def test_retired_label_is_removed_without_changing_body_or_unknown_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'taxonomy.json'
            path.write_text(json.dumps({'canonical': ['karar-verme'],
                'aliases': {}, 'retired': ['ajanlar']}), encoding='utf-8')
            taxonomy = tag_taxonomy.load_taxonomy(path)
            source = '---\r\ntags: [ajanlar, karar-verme]\r\n---\r\nAjanlar ve özgün kaynak.\r\n'
            updated, unknown = tag_taxonomy.normalize_markdown(source, taxonomy)
            self.assertEqual(unknown, [])
            self.assertEqual(updated, source.replace('[ajanlar, karar-verme]', '[karar-verme]'))
            self.assertEqual(tag_taxonomy.normalize_markdown(updated, taxonomy), (updated, []))
            bad = source.replace('karar-verme', 'bilinmeyen')
            self.assertEqual(tag_taxonomy.normalize_markdown(bad, taxonomy), (bad, ['bilinmeyen']))


if __name__ == '__main__':
    unittest.main()
