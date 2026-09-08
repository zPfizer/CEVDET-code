from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import memory_ledger
import vault_retrieval


class RetrievalViewSafetyTests(unittest.TestCase):
    def test_missing_projected_view_fails_closed_without_emitted_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / "knowledge/concepts/profil.md"
            source.parent.mkdir(parents=True)
            source.write_text(
                "# Profil\n\nLevent haftalık planını pazartesi yapıyor.\n",
                encoding="utf-8",
            )
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "forget-me", now=1
            )

            with mock.patch.object(memory_ledger.MemoryRead, "views", return_value={}):
                with self.assertRaisesRegex(
                    memory_ledger.MemoryPreferenceError,
                    "memory-view-unavailable",
                ):
                    vault_retrieval.retrieve_vault_context_detailed(
                        vault, "Levent haftalık plan pazartesi", write_cache=False
                    )

    def test_emission_budget_uses_projected_path_and_keeps_whole_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / "knowledge/concepts/profil.md"
            source.parent.mkdir(parents=True)
            source.write_text(
                "# Profil\n\nLevent haftalık planını pazartesi yapıyor.\n",
                encoding="utf-8",
            )
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "forget-me", now=1
            )
            full = vault_retrieval.retrieve_vault_context_detailed(
                vault, "Levent haftalık plan pazartesi", max_chars=10_000,
                write_cache=False,
            )

            exact = vault_retrieval.retrieve_vault_context_detailed(
                vault, "Levent haftalık plan pazartesi", max_chars=len(full.text),
                write_cache=False,
            )
            under = vault_retrieval.retrieve_vault_context_detailed(
                vault, "Levent haftalık plan pazartesi", max_chars=len(full.text) - 1,
                write_cache=False,
            )

        self.assertEqual(exact.text, full.text)
        self.assertEqual(exact.paths, full.paths)
        self.assertEqual(under.paths, ())
        self.assertEqual(under.text, "")

    def test_suppressed_no_cache_retrieval_emits_view_but_keeps_source_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / "knowledge/concepts/profil.md"
            source.parent.mkdir(parents=True)
            original = (
                "# Profil\n\n"
                "Levent Ankara'da yaşıyor.\n"
                "Levent haftalık planını pazartesi yapıyor.\n"
            )
            source.write_text(original, encoding="utf-8")
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "Levent Ankara'da yaşıyor", now=1
            )

            result = vault_retrieval.retrieve_vault_context_detailed(
                vault, "Levent haftalık plan pazartesi", write_cache=False
            )

            self.assertEqual(result.paths, ("knowledge/concepts/profil.md",))
            self.assertNotIn("knowledge/concepts/profil.md", result.text)
            self.assertIn(".codex/private-memory/views/", result.text)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertFalse(
                (vault / vault_retrieval.CACHE_RELATIVE_PATH).exists()
            )

    def test_active_suppression_materializes_only_selected_hits(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            notes = vault / "🧠 500-Knowledge"
            notes.mkdir(parents=True)
            for index in range(1000):
                body = "needle unique-target\n" if index == 0 else "noise\n"
                (notes / f"note-{index:04d}.md").write_text(
                    f"# Note {index}\n{body}", encoding="utf-8"
                )
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "forget-me", now=1
            )
            writes = 0
            original_write = memory_ledger.atomic_write_text

            def count_write(*args, **kwargs):
                nonlocal writes
                writes += 1
                return original_write(*args, **kwargs)

            with mock.patch.object(memory_ledger, "atomic_write_text", count_write):
                result = vault_retrieval.retrieve_vault_context_detailed(
                    vault, "needle", top_k=3, write_cache=False
                )

            self.assertEqual(result.paths, ("🧠 500-Knowledge/note-0000.md",))
            self.assertEqual(writes, 1)
            self.assertFalse(
                (vault / vault_retrieval.CACHE_RELATIVE_PATH).exists()
            )

    def test_selected_view_links_resolve_and_unseen_links_stay_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            notes = vault / "🧠 500-Knowledge"
            notes.mkdir(parents=True)
            first = notes / "first.md"
            second = notes / "second.md"
            unseen = notes / "unseen.md"
            unrelated = notes / "unrelated.md"
            first.write_text(
                "# First\nneedle alpha [[second|Second]] [[unseen|Unseen]]\n",
                encoding="utf-8",
            )
            second.write_text("# Second\nneedle beta\n", encoding="utf-8")
            unseen.write_text("# Unseen\nneedle gamma\n", encoding="utf-8")
            unrelated.write_text("# Unrelated\nbaşka kayıt\n", encoding="utf-8")
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "forget-me", now=1
            )

            result = vault_retrieval.retrieve_vault_context_detailed(
                vault, "needle alpha beta", top_k=2, write_cache=False
            )

            self.assertEqual(len(result.paths), 2)
            view_dir = vault / ".codex/private-memory/views"
            views = list(view_dir.glob("*.md"))
            self.assertEqual(len(views), 3)
            first_view = next(
                view for view in views if "# First" in view.read_text(encoding="utf-8")
            )
            projected = first_view.read_text(encoding="utf-8")
            self.assertIn(".codex/private-memory/views/", projected)
            self.assertIn("Unseen", projected)
            self.assertNotIn("[[unseen", projected)
            self.assertNotIn("knowledge/concepts", projected)
            self.assertNotIn("Unrelated", "\n".join(
                view.read_text(encoding="utf-8") for view in views
            ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
