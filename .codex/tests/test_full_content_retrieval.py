from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import memory_ledger
import vault_retrieval as retrieval


class FullContentRetrievalTests(unittest.TestCase):
    def test_filtered_retrieval_does_not_materialize_views_when_budget_cannot_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / '🧠 500-Knowledge/hit.md'
            note.parent.mkdir(parents=True)
            note.write_text('# Hit\nGizli.\nBenzersiz iz.\n', encoding='utf-8')
            memory_ledger.suppress_derived_memory(
                root / '.codex' / 'private-memory', 'Gizli.'
            )

            result = retrieval.retrieve_vault_context_detailed(
                root, 'Benzersiz iz', max_chars=1, write_cache=False
            )
            view_dir = root / '.codex' / 'private-memory' / 'views'
            view_exists = view_dir.exists()

        self.assertEqual(result.outcome, 'empty')
        self.assertEqual(result.paths, ())
        self.assertFalse(view_exists)

    def test_filtered_retrieval_materializes_only_hits_that_fit_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / '🧠 500-Knowledge'
            notes.mkdir(parents=True)
            (notes / 'a-hit.md').write_text(
                '# Birinci\nGizli.\nBenzersiz iz.\n', encoding='utf-8'
            )
            (notes / 'z-hit.md').write_text(
                '# İkinci\nGizli.\nBenzersiz iz.\n' + 'uzun ' * 600,
                encoding='utf-8',
            )
            memory_ledger.suppress_derived_memory(
                root / '.codex' / 'private-memory', 'Gizli.'
            )

            result = retrieval.retrieve_vault_context_detailed(
                root, 'Benzersiz iz', max_chars=800, write_cache=False
            )
            view_dir = root / '.codex' / 'private-memory' / 'views'
            views = list(view_dir.glob('*.md'))
            rendered = [path.read_text(encoding='utf-8') for path in views]

        self.assertEqual(result.paths, ('🧠 500-Knowledge/a-hit.md',))
        self.assertEqual(len(views), 1)
        self.assertTrue(any('Benzersiz iz.' in text for text in rendered))
        self.assertNotIn('z-hit.md', '\n'.join(rendered))

    def test_filtered_retrieval_rejects_a_forget_that_races_view_emission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / '🧠 500-Knowledge/hit.md'
            note.parent.mkdir(parents=True)
            note.write_text('# Hit\nBenzersiz iz.\n', encoding='utf-8')
            memory_ledger.suppress_derived_memory(
                root / '.codex' / 'private-memory', 'başlangıç kuralı.'
            )
            original = retrieval._json_text
            changed = False

            def forget_after_render(value):
                nonlocal changed
                rendered = original(value)
                if not changed:
                    changed = True
                    memory_ledger.suppress_derived_memory(
                        root / '.codex' / 'private-memory', 'Benzersiz iz.'
                    )
                return rendered

            with mock.patch.object(retrieval, '_json_text', side_effect=forget_after_render):
                with self.assertRaisesRegex(memory_ledger.MemoryPreferenceError, 'memory-preferences-changed'):
                    retrieval.retrieve_vault_context_detailed(
                        root, 'Benzersiz iz', write_cache=False
                    )
            view_text = ''.join(
                path.read_text(encoding='utf-8')
                for path in (root / '.codex' / 'private-memory' / 'views').glob('*.md')
            )

        self.assertTrue(changed)
        self.assertNotIn('Benzersiz iz', view_text)

    def test_filtered_retrieval_writes_only_hits_and_linked_targets_when_cache_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / '🧠 500-Knowledge'
            notes.mkdir(parents=True)
            hit = notes / 'hit.md'
            linked = notes / 'linked.md'
            unrelated = notes / 'unrelated.md'
            hit.write_text(
                '# Hit\nGizli karar.\nBenzersiz iz.\n[[linked|Destek]].\n',
                encoding='utf-8',
            )
            linked.write_text('# Destek\nDestek açıklaması.\n', encoding='utf-8')
            unrelated.write_text('# Alakasız\nBaşka kayıt.\n', encoding='utf-8')
            memory_ledger.suppress_derived_memory(
                root / '.codex' / 'private-memory', 'Gizli karar.'
            )

            result = retrieval.retrieve_vault_context_detailed(
                root, 'Benzersiz iz', write_cache=False
            )
            view_dir = root / '.codex' / 'private-memory' / 'views'
            views = sorted(view_dir.glob('*.md'))
            hit_view = next(path for path in views if 'Benzersiz iz' in path.read_text(encoding='utf-8'))
            rendered_views = [path.read_text(encoding='utf-8') for path in views]
            hit_text = hit_view.read_text(encoding='utf-8')

        self.assertEqual(result.paths, ('🧠 500-Knowledge/hit.md',))
        self.assertEqual(len(views), 2)
        self.assertNotIn('unrelated', '\n'.join(rendered_views))
        self.assertNotIn('Gizli karar', hit_text)
        self.assertIn('private-memory/views', result.text)
        self.assertNotIn('🧠 500-Knowledge/hit.md', result.text)
        self.assertTrue(any('Destek açıklaması.' in text for text in rendered_views))

    def test_filtered_retrieval_keeps_ambiguous_alias_as_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hit = root / '🧠 500-Knowledge/hit.md'
            first = root / '🏰 300-Projects/one/same.md'
            second = root / '🏰 300-Projects/two/same.md'
            for path in (hit, first, second):
                path.parent.mkdir(parents=True, exist_ok=True)
            hit.write_text('# Hit\nGizli.\nBenzersiz iz.\n[[same]]\n', encoding='utf-8')
            first.write_text('# Bir\nBir hedef.\n', encoding='utf-8')
            second.write_text('# İki\nİki hedef.\n', encoding='utf-8')
            memory_ledger.suppress_derived_memory(
                root / '.codex' / 'private-memory', 'Gizli.'
            )

            result = retrieval.retrieve_vault_context_detailed(
                root, 'Benzersiz iz', write_cache=False
            )
            views = list((root / '.codex' / 'private-memory' / 'views').glob('*.md'))
            rendered = next(path.read_text(encoding='utf-8') for path in views if 'Benzersiz iz' in path.read_text(encoding='utf-8'))

        self.assertEqual(result.paths, ('🧠 500-Knowledge/hit.md',))
        self.assertEqual(len(views), 1)
        self.assertIn('same', rendered)
        self.assertNotIn('[[same]]', rendered)
        self.assertNotIn('one', rendered)
        self.assertNotIn('two', rendered)

    def test_filtered_retrieval_keeps_valid_user_evidence_daily_target_safe(self):
        from test_quality_pipeline import proof_fixture

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _record, _row, note = proof_fixture(root)
            memory_ledger.suppress_derived_memory(
                root / '.codex' / 'private-memory', 'irrelevant'
            )

            result = retrieval.retrieve_vault_context_detailed(
                root, 'Kısa yanıt tercih ediliyor', write_cache=False
            )
            view_dir = root / '.codex' / 'private-memory' / 'views'
            views = list(view_dir.glob('*.md'))
            rendered_views = [path.read_text(encoding='utf-8') for path in views]
            note_view = next(text for text in rendered_views if '# Tercih' in text)
            daily_view = next(text for text in rendered_views if '## Bağlam' in text)

        self.assertEqual(result.paths, (note.relative_to(root).as_posix(),))
        self.assertEqual(len(views), 2)
        self.assertIn('private-memory/views', note_view)
        self.assertIn('private-memory/views', daily_view)
        self.assertNotIn('[[daily/2026-09-06', note_view)
        self.assertNotIn('daily/2026-09-06.md', result.text)

    def test_nested_infrastructure_is_excluded_from_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / '🧠 500-Knowledge'
            for name in ('visible.md', '.scratch/private.md', '.CoDeX/private.md', 'node_modules/private.md'):
                path = notes / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# UniqueEvidence\n', encoding='utf-8')
            entries = retrieval.build_vault_map(root, write_cache=False)
            self.assertEqual([e.path for e in entries], ['🧠 500-Knowledge/visible.md'])

    def test_cache_invalidates_when_content_changes_with_same_stat_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / '🧠 500-Knowledge/note.md'
            note.parent.mkdir(parents=True)
            note.write_text('# note\nAlphaunique', encoding='utf-8')
            retrieval.build_vault_map(root)
            before = note.stat()
            note.write_text('# note\nOmegaunique', encoding='utf-8')
            os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = note.stat()

            self.assertEqual((before.st_size, before.st_mtime_ns, before.st_ctime_ns),
                             (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
            refreshed = retrieval.build_vault_map(root)
            result = retrieval.retrieve_vault_context_detailed(
                root, 'Omegaunique', write_cache=False,
            )

        self.assertEqual(result.paths, ('🧠 500-Knowledge/note.md',))
        self.assertEqual(retrieval.search_vault(refreshed, 'Omegaunique')[0].entry.path,
                         '🧠 500-Knowledge/note.md')
        self.assertEqual(retrieval.search_vault(refreshed, 'Alphaunique'), [])

    def test_short_terms_and_ratios_are_not_dismissed_as_empty_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / '🧠 500-Knowledge/finans.md'
            note.parent.mkdir()
            note.write_text('# Kavramlar\nFX döviz piyasasıdır.\nKur değişimi izlenir.\nF/K fiyat kâr oranıdır.\nMem0 hafıza aracı, H4 dört saatlik grafiktir.\n', encoding='utf-8')
            for query in ('FX nedir?', 'kur nedir?', 'F/K nedir?', 'Mem0 nedir?', 'H4 nedir?'):
                with self.subTest(query=query):
                    result = retrieval.retrieve_vault_context_detailed(root, query, write_cache=False)
                    self.assertEqual(result.paths, ('🧠 500-Knowledge/finans.md',))

    def test_audit_does_not_treat_disposable_backups_as_live_notes(self):
        from vault_corpus import vault_notes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['🧠 500-Knowledge/current.md', '.scratch/before/current.md']:
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_text('# Not\n', encoding='utf-8')
            self.assertEqual([note.key for note in vault_notes(root)], ['🧠 500-Knowledge/current.md'])

    def test_source_url_is_searchable_without_credentials_or_query_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / '🧠 500-Knowledge/kaynak.md'
            note.parent.mkdir()
            note.write_text('---\nsource_url: https://hiddenpassword@uniqueexampledomain.test/simex-paper?key=secrettoken\n---\n# Kaynak\n', encoding='utf-8')
            entries = retrieval.build_vault_map(root)
            self.assertEqual(len(retrieval.search_vault(entries, 'uniqueexampledomain')), 1)
            self.assertNotIn('secrettoken', entries[0].all_terms)
            self.assertNotIn('hiddenpassword', entries[0].all_terms)

    def test_repeated_large_paragraph_keeps_excerpt_memory_bounded(self):
        import tracemalloc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / '🧠 500-Knowledge/uzun.md'
            note.parent.mkdir()
            note.write_text('# Not\n' + 'SIMEX ' * 100_000, encoding='utf-8')
            entries = retrieval.build_vault_map(root, write_cache=False)
            tracemalloc.start()
            try:
                hits = retrieval.search_vault(entries, 'SIMEX')
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertIn('SIMEX', hits[0].excerpt)
            self.assertLess(peak, 4_000_000)

    def test_tail_and_long_line_are_searchable_and_visible_in_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / '🧠 500-Knowledge/uzun.md'
            note.parent.mkdir()
            note.write_text('# Not\n' + 'giriş satırı\n' * 600
                + 'açıklama ' * 200 + 'SIMEX ölçüm hatasını düzeltir.\n', encoding='utf-8')
            hits = retrieval.search_vault(retrieval.build_vault_map(root), 'SIMEX nedir?')
            self.assertEqual([h.entry.path for h in hits], ['🧠 500-Knowledge/uzun.md'])
            self.assertIn('SIMEX', hits[0].excerpt)
            self.assertIn('tam kaynağı oku', hits[0].excerpt)
            self.assertNotIn('SIMEX ölçüm hatasını düzeltir', hits[0].excerpt)
            self.assertEqual(retrieval.search_vault(retrieval.build_vault_map(root),
                'simex nedir?')[0].entry.path, hits[0].entry.path)

    def test_archive_remains_searchable_with_historical_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / '📦 900-Archive/karar.md'
            note.parent.mkdir()
            note.write_text('# Eski karar\nRambachan Roth duyarlılık yöntemi.\n', encoding='utf-8')
            hits = retrieval.search_vault(retrieval.build_vault_map(root), 'Rambachan Roth')
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].entry.status, 'historical')

    def test_distinctive_body_evidence_is_not_hidden_by_generic_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / '🧠 500-Knowledge'
            notes.mkdir()
            (notes / 'yontem.md').write_text('# Ekonometri\nRambachan Roth paralel trend duyarlılığı.\n', encoding='utf-8')
            for i in range(6):
                (notes / f'diger-{i}.md').write_text('# Paralel trend duyarlılığı\nGenel açıklama.\n', encoding='utf-8')
            hits = retrieval.search_vault(retrieval.build_vault_map(root), 'Rambachan Roth paralel trend duyarlılığı')
            self.assertEqual(hits[0].entry.path, '🧠 500-Knowledge/yontem.md')

if __name__ == '__main__':
    unittest.main()
