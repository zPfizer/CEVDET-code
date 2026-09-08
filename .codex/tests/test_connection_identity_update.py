from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import knowledge_schema


class ConnectionIdentityUpdateTests(unittest.TestCase):
    def test_connection_links_require_exact_canonical_targets(self):
        wrong_targets = '''---
connects: [alpha, beta]
---
## Bağlantı
[[wrong/alpha|Alfa]] ↔ [[../beta|Beta]]
## Ana Fikir
Bağ.
'''
        canonical_targets = wrong_targets.replace(
            '[[wrong/alpha|Alfa]] ↔ [[../beta|Beta]]',
            '[[knowledge/concepts/alpha|Alfa]] ↔ [[knowledge/concepts/beta|Beta]]',
        )

        self.assertFalse(
            knowledge_schema._connection_links_ok(
                Path('alpha--beta.md'), wrong_targets
            )
        )
        self.assertTrue(
            knowledge_schema._connection_links_ok(
                Path('alpha--beta.md'), canonical_targets
            )
        )

    def test_connects_rejects_non_slug_targets(self):
        for value in ('../alpha', 'Alpha', 'alpha.md', 'alpha beta'):
            with self.subTest(value=value):
                text = f'---\nconnects: [{value}, beta]\n---\n'
                self.assertIsNone(knowledge_schema._connects(text))

    def test_legacy_connection_can_gain_provenance_without_renaming(self):
        text = '''---
schema: knowledge-v2
connects: [zeta, alpha]
sources: [2026-09-05.md]
updated: 2026-09-05
---
## Bağlantı
[[knowledge/concepts/zeta]] ve [[knowledge/concepts/alpha]] ilişkisi.
## Ana Fikir
Eski durum tarihsel, yeni durum kaynaklıdır.
## Kaynaklar
- [[daily/2026-09-05|Kaynak]]
'''
        issues = []
        path = Path('zeta--alpha.md')
        knowledge_schema._validate_derived_connection(path, text, None, issues)
        self.assertEqual(issues, [])
        self.assertTrue(knowledge_schema._connection_path_ok(path, text))
        self.assertFalse(knowledge_schema._connection_path_ok(Path('alpha--zeta.md'), text))


if __name__ == '__main__':
    unittest.main()
