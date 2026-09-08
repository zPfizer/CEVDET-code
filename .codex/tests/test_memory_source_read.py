from pathlib import Path
import hashlib
import tempfile
import unittest

from _fixtures import CODEX_DIR
import companion_memory
import flush
import memory_ledger as ledger
import vault_retrieval as retrieval


class MemorySourceReadTests(unittest.TestCase):
    def test_read_source_sanitizes_previous_summary_retrieval_and_cache_without_editing_sources(self):
        marker = 'BUG006_LEAK_SENTINEL'
        session = 'probe-session'
        session_hash = hashlib.sha256(session.encode()).hexdigest()
        key = 'a' * 64
        summary = '\n\n'.join(
            f'## {section}\npassword={marker}' for section in flush.EXPECTED_SECTIONS
        )
        snapshot = (
            f'<!-- cevo-auto-session 1.0 {key} -->\n'
            f'<!-- cevo-session {session_hash} 2026-09-08T10:00:00+00:00 {key} -->\n'
            f'{summary}\n<!-- /cevo-session -->\n<!-- /cevo-auto-session -->\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / '🔮 850-Companion/Last-Session.md'
            companion.parent.mkdir(parents=True)
            companion.write_text(snapshot, encoding='utf-8')
            note = vault / '🧠 500-Knowledge/secret-note.md'
            note.parent.mkdir(parents=True)
            original = '---\ntitle: Probe\ntype: source-note\n---\npassword=' + marker + '\n'
            note.write_text(original, encoding='utf-8')

            with ledger.memory_read(vault) as memory:
                _relative, text = memory.read_source(note)
            self.assertNotIn(marker, text)
            self.assertEqual(note.read_text(encoding='utf-8'), original)

            previous = companion_memory.previous_summary(vault, session)
            self.assertNotIn(marker, previous)
            result = retrieval.retrieve_vault_context_detailed(
                vault, 'Probe password', write_cache=True,
            )
            self.assertEqual(result.outcome, 'emitted')
            self.assertNotIn(marker, result.text)
            cache = vault / '.codex/scripts/.state/vault-retrieval-cache.json'
            self.assertNotIn(marker, cache.read_text(encoding='utf-8'))
            self.assertEqual(companion.read_text(encoding='utf-8'), snapshot)

    def test_read_source_keeps_full_source_when_redaction_expands_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / 'note.md'
            original = 'password=x\n' + ('uzun içerik.\n' * 6000)
            source.write_text(original, encoding='utf-8')

            with ledger.memory_read(vault) as memory:
                _relative, text = memory.read_source(source)

            self.assertNotIn('password=x', text)
            self.assertIn('password=<REDACTED>', text)
            self.assertTrue(text.endswith('uzun içerik.\n'))
            self.assertGreater(len(text), ledger.MAX_EVENT_CHARS)
            self.assertEqual(source.read_text(encoding='utf-8'), original)

    def test_sanitize_text_keeps_bounded_default_and_rejects_invalid_limits(self):
        self.assertEqual(ledger.sanitize_text('safe', max_chars=None), ('safe', ()))
        with self.assertRaisesRegex(ValueError, 'max-event-chars-invalid'):
            ledger.sanitize_text('safe', max_chars=0)
        with self.assertRaisesRegex(ValueError, 'max-event-chars-invalid'):
            ledger.sanitize_text('safe', max_chars=-1)

    def test_snapshot_reads_full_filtered_source_and_consumers_keep_their_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / 'note.md'
            original = ('---\ntitle: Plan\nstatus: historical\naliases: [Orkide]\n---\n'
                        'Ankara ikamet adresimdir.\npassword=private-example\n'
                        + 'Plan pazartesi.\n' * 5000 + 'Son bilgi.\n')
            source.write_text(original, encoding='utf-8')
            ledger.suppress_derived_memory(vault / '.codex/private-memory', 'Ankara ikamet adresimdir')
            with ledger.memory_read(vault) as memory:
                relative, text = memory.read_source(source)
                self.assertEqual(relative, Path('note.md'))
                self.assertNotIn('Ankara', text)
                self.assertTrue(text.endswith('Son bilgi.\n'))
                # Index metadata and ranking retain the existing unsanitized projection.
                entry = retrieval.entry_from_file(vault, source, memory=memory)
                self.assertEqual((entry.title, entry.status), ('Plan', 'historical'))
                self.assertIn('orkide', entry.tag_terms)
                self.assertEqual(entry.body_terms['pazartesi'], 5000)
                self.assertNotIn('ankara', entry.all_terms)
                views = memory.views([('note.md', 'Plan')])
            direct = ledger.read_memory_source(vault, source)
            self.assertEqual(direct, (vault / views['note.md']).read_text(encoding='utf-8'))
            self.assertNotIn('private-example', direct)
            self.assertTrue(direct.endswith('Son bilgi.\n'))
            self.assertEqual(source.read_text(encoding='utf-8'), original)

    def test_excluded_path_is_distinct_from_an_empty_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            hidden, empty = vault / 'Ankara.md', vault / 'empty.md'
            hidden.write_text('Korunacak kaynak.', encoding='utf-8')
            empty.touch()
            ledger.suppress_derived_memory(vault / '.codex/private-memory', 'Ankara')
            with ledger.memory_read(vault) as memory:
                self.assertEqual(memory.read_source(hidden), (Path('Ankara.md'), None))
                self.assertEqual(memory.read_source(empty), (Path('empty.md'), ''))
                self.assertIsNone(retrieval.entry_from_file(vault, hidden, memory=memory))
                self.assertIsNotNone(retrieval.entry_from_file(vault, empty, memory=memory))
            self.assertEqual(ledger.read_memory_source(vault, hidden), '')

    def test_consumer_path_and_identity_policies_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            notes = vault / 'Ankara'
            notes.mkdir()
            source = notes / 'note.md'
            source.write_text('Plan pazartesi.\n', encoding='utf-8')
            alias = vault / 'alias'
            alias.symlink_to(notes, target_is_directory=True)
            linked = alias / 'note.md'
            ledger.suppress_derived_memory(vault / '.codex/private-memory', 'Ankara')
            hashes = ledger.load_suppressed_hashes(vault / '.codex/private-memory')
            self.assertEqual(ledger.read_memory_source(vault, linked), '')
            self.assertIsNone(retrieval.entry_from_file(vault, linked))
            views = ledger.materialize_memory_views(vault, [('alias/note.md', 'Plan')], hashes)
            self.assertEqual((vault / views['alias/note.md']).read_text(encoding='utf-8'), 'Plan pazartesi.\n')
            file_link = vault / 'link.md'
            file_link.symlink_to(source)
            self.assertIsNone(retrieval.entry_from_file(vault, file_link))
            with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'memory-view-source-invalid'):
                ledger.materialize_memory_views(vault, [('link.md', 'Plan')], hashes)

    def test_outside_missing_and_invalid_utf8_keep_their_error_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / 'vault'
            vault.mkdir()
            outside = root / 'outside.md'
            outside.write_text('Dış kaynak.', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'memory-source-outside-vault'):
                ledger.read_memory_source(vault, outside)
            self.assertIsNone(retrieval.entry_from_file(vault, outside))
            with self.assertRaisesRegex(ledger.MemoryPreferenceError, 'memory-view-source-invalid'):
                ledger.materialize_memory_views(vault, [('../outside.md', 'Dış')], frozenset())
            missing, invalid = vault / 'missing.md', vault / 'invalid.md'
            invalid.write_bytes(b'\xff')
            for path, error in ((missing, FileNotFoundError), (invalid, UnicodeDecodeError)):
                with self.subTest(path=path.name):
                    with self.assertRaises(error):
                        ledger.read_memory_source(vault, path)
                    with self.assertRaises(error):
                        retrieval.entry_from_file(vault, path)
                    with self.assertRaises(error):
                        ledger.materialize_memory_views(vault, [(path.name, 'Not')], frozenset())


if __name__ == '__main__':
    unittest.main()
