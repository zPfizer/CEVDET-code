from pathlib import Path
import tempfile
import unittest

import _fixtures  # noqa: F401
import hook


class StartupIndexBoundaryTests(unittest.TestCase):
    def test_index_qualifier_beyond_the_previous_line_limit_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            index = vault / 'knowledge/index.md'
            index.parent.mkdir()
            text = ('# Bilgi\nAtlas onaylandı.\n' + '\n' * 148
                    + 'Ancak kullanıcı onay vermedi.')
            index.write_text(text, encoding='utf-8')

            context = hook.build_session_context(vault, vault / '.codex/scripts/.state')

            self.assertIn('Ancak kullanıcı onay vermedi.', context)
            self.assertEqual(index.read_text(encoding='utf-8'), text)

    def test_long_index_prose_defers_the_whole_claim_to_its_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            index = vault / 'knowledge/index.md'
            index.parent.mkdir()
            text = ('# Bilgi Tabanı\n\nAtlas planı uygulanabilir. '
                    + 'Gerekçe ve seçenekler. ' * 30
                    + 'Ancak kullanıcı henüz onay vermedi.\n')
            index.write_text(text, encoding='utf-8')

            context = hook.build_session_context(vault, vault / '.codex/scripts/.state')

            self.assertNotIn('Atlas planı uygulanabilir.', context)
            self.assertIn('Tam bilgi indeksi', context)
            self.assertEqual(index.read_text(encoding='utf-8'), text)

    def test_short_index_prose_keeps_its_complete_qualifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            index = vault / 'knowledge/index.md'
            index.parent.mkdir()
            text = '# Bilgi Tabanı\nAtlas planı uygulanabilir; ancak henüz onaylanmadı.'
            index.write_text(text, encoding='utf-8')

            context = hook.build_session_context(vault, vault / '.codex/scripts/.state')

            self.assertIn(text, context)


if __name__ == '__main__':
    unittest.main()
