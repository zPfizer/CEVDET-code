from pathlib import Path
import tempfile
import unittest

from _fixtures import CODEX_DIR
import vault_retrieval as retrieval


class RetrievalFrontmatterTests(unittest.TestCase):
    def test_quoted_status_has_the_same_ranking(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, status in [('plain', 'archived'), ('quoted', '"archived"')]:
                (root / f'{name}.md').write_text(
                    f'---\ntitle: Deneme\nstatus: {status}\n---\nOrkide plan kararı.\n',
                    encoding='utf-8',
                )
            entries = retrieval.build_vault_map(root, write_cache=False)
            hits = retrieval.search_vault(entries, 'orkide')
            self.assertEqual(len(hits), 1)
            individual = [retrieval.search_vault([entry], 'orkide')[0].score for entry in entries]
            self.assertEqual(individual[0], individual[1])
            self.assertEqual({entry.status for entry in entries}, {'archived'})

    def test_metadata_lists_case_and_long_headers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'note.md'
            path.write_text(
                '---\nTitle: "Orkide"\naliases: ["bahçe", bitki]\n'
                'tags:\n  - bakım\n  - sulama\n' + '# açıklama\n' * 80 +
                'Status: "historical"\n---\nGövde korunur.\n', encoding='utf-8',
            )
            entry = retrieval.entry_from_file(root, path)
            self.assertEqual(entry.title, 'Orkide')
            self.assertEqual(entry.status, 'historical')
            self.assertTrue({'bahce', 'bitki', 'bakim', 'sulama'} <= entry.tag_terms)
            self.assertIn('Gövde korunur.', entry.safe_lines)

    def test_unclosed_header_does_not_supply_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'note.md'
            path.write_text('---\nstatus: archived\n# Başlık\nOrkide.', encoding='utf-8')
            entry = retrieval.entry_from_file(root, path)
            self.assertEqual(entry.status, 'active')
            self.assertEqual(entry.title, 'Başlık')


if __name__ == '__main__':
    unittest.main()
