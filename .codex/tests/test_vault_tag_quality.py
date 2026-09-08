from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


VAULT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VAULT / ".codex" / "scripts"))

import tag_taxonomy  # noqa: E402
from knowledge_schema import parse_frontmatter  # noqa: E402
from vault_corpus import HUMAN_NOTE_ROOTS  # noqa: E402


TAXONOMY_PATH = VAULT / ".codex" / "tag-taxonomy.json"
TAG_REFERENCE = (
    VAULT
    / "🛠️ 600-Arsenal"
    / "Vault ve Yönetişim"
    / "Vault Etiket Sözlüğü.md"
)


class VaultTagQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomy = tag_taxonomy.load_taxonomy(TAXONOMY_PATH)

    def test_financial_valuation_is_not_a_real_estate_alias(self) -> None:
        taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))

        self.assertNotIn("değerleme", taxonomy["aliases"])
        self.assertNotIn("degerleme", taxonomy["aliases"])

    def test_inline_tag_audit_respects_obsidian_excluded_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obsidian = root / ".obsidian"
            archive = root / "Archive"
            obsidian.mkdir()
            archive.mkdir()
            (obsidian / "app.json").write_text(
                json.dumps({"userIgnoreFilters": ["Archive/"]}),
                encoding="utf-8",
            )
            (archive / "history.md").write_text(
                "Historical #A1CAP source label\n",
                encoding="utf-8",
            )

            violations = tag_taxonomy.audit_inline_tags(root)

        self.assertEqual(violations, [])

    def test_migration_patch_only_changes_tags_and_dry_run_keeps_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            source = (
                "---\n"
                "title: Deneme\n"
                "tags: [decision-making, search]\n"
                "---\n"
                "# Başlık\n"
                "Kullanıcı gövdesi.\n"
            )
            note.write_bytes(source.encode("utf-8"))
            before = note.read_bytes()
            notes = tag_taxonomy.vault_notes(root)

            result = tag_taxonomy.migrate_vault(
                root,
                self.taxonomy,
                apply=False,
                notes=notes,
            )

            self.assertEqual(result.changed, ())
            self.assertEqual(result.conflicts, ())
            self.assertEqual(note.read_bytes(), before)
            self.assertEqual(len(result.patches), 1)
            patch = result.patches[0]
            self.assertEqual(patch.path, note)
            self.assertEqual(
                patch.source_sha256,
                hashlib.sha256(before).hexdigest(),
            )
            self.assertIn("tags: [karar-verme, arama]", patch.normalized)
            self.assertIn("Kullanıcı gövdesi.", patch.normalized)
            self.assertEqual(
                patch.normalized.replace(
                    "tags: [karar-verme, arama]",
                    "tags: [decision-making, search]",
                ),
                patch.source,
            )

    def test_migration_preserves_drifted_body_and_reports_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            planned_source = "---\ntags: [search]\n---\nEski gövde\n"
            live_source = "---\ntags: [search]\n---\nYeni kullanıcı gövdesi\n"
            note.write_text(planned_source, encoding="utf-8")
            notes = tag_taxonomy.vault_notes(root)
            note.write_text(live_source, encoding="utf-8")

            result = tag_taxonomy.migrate_vault(
                root,
                self.taxonomy,
                apply=True,
                notes=notes,
            )

            self.assertEqual(result.changed, ())
            self.assertEqual(len(result.conflicts), 1)
            conflict = result.conflicts[0]
            self.assertEqual(conflict.path, note)
            self.assertEqual(
                conflict.expected_sha256,
                hashlib.sha256(planned_source.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                conflict.current_sha256,
                hashlib.sha256(live_source.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(note.read_text(encoding="utf-8"), live_source)

    def test_migration_does_not_call_writer_after_live_race_at_lock_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            planned_source = "---\ntags: [search]\n---\nEski gövde\n"
            live_source = "---\ntags: [search]\n---\nYarışan gövde\n"
            note.write_text(planned_source, encoding="utf-8")
            notes = tag_taxonomy.vault_notes(root)

            @contextmanager
            def racing_lock(_path: Path):
                note.write_text(live_source, encoding="utf-8")
                yield None

            with (
                mock.patch.object(tag_taxonomy, "locked", racing_lock),
                mock.patch.object(tag_taxonomy, "atomic_write_text") as writer,
            ):
                result = tag_taxonomy.migrate_vault(
                    root,
                    self.taxonomy,
                    apply=True,
                    notes=notes,
                )

            writer.assert_not_called()
            self.assertEqual(result.changed, ())
            self.assertEqual(len(result.conflicts), 1)
            self.assertEqual(note.read_text(encoding="utf-8"), live_source)

    def test_live_migration_only_returns_patch_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            source = "---\ntags: [search]\n---\nGövde\n"
            note.write_text(source, encoding="utf-8")
            before = note.read_bytes()

            with mock.patch.object(tag_taxonomy, "atomic_write_text") as writer:
                result = tag_taxonomy.migrate_vault(
                    root,
                    self.taxonomy,
                    apply=True,
                )

            writer.assert_not_called()
            self.assertEqual(result.changed, ())
            self.assertEqual(result.conflicts, ())
            self.assertEqual(len(result.patches), 1)
            self.assertEqual(note.read_bytes(), before)

    def test_unknown_tag_keeps_migration_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            source = "---\ntags: [sozlukte-yok]\n---\nGövde\n"
            note.write_text(source, encoding="utf-8")
            with mock.patch.object(tag_taxonomy, "atomic_write_text") as writer:
                result = tag_taxonomy.migrate_vault(
                    root,
                    self.taxonomy,
                    apply=True,
                )

            writer.assert_not_called()
            self.assertEqual(result.changed, ())
            self.assertEqual(len(result.unknown), 1)
            self.assertEqual(note.read_text(encoding="utf-8"), source)

    def test_cli_apply_prints_reviewable_patch_without_migrated_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            note.write_text(
                "---\ntags: [search]\n---\nGövde\n",
                encoding="utf-8",
            )
            before = note.read_bytes()
            output = io.StringIO()
            with mock.patch.object(tag_taxonomy.sys, "stdout", output):
                exit_code = tag_taxonomy.main(
                    [
                        "--root",
                        str(root),
                        "--taxonomy",
                        str(TAXONOMY_PATH),
                        "--apply",
                    ]
                )
                after = note.read_bytes()

        rendered = output.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn(f"PATCH\t{note}", rendered)
        self.assertIn("source_sha256=", rendered)
        self.assertIn("tags: [arama]", rendered)
        self.assertIn("PATCH_READY\t1", rendered)
        self.assertNotIn("MIGRATED\t", rendered)
        self.assertEqual(after, before)

    def test_cli_conflict_is_nonzero_and_does_not_claim_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            note.write_text(
                "---\ntags: [search]\n---\nEski gövde\n",
                encoding="utf-8",
            )
            live_source = "---\ntags: [arama]\n---\nYeni gövde\n"

            @contextmanager
            def racing_lock(_path: Path):
                note.write_text(live_source, encoding="utf-8")
                yield None

            output = io.StringIO()
            with (
                mock.patch.object(tag_taxonomy, "locked", racing_lock),
                mock.patch.object(tag_taxonomy, "atomic_write_text") as writer,
                mock.patch.object(tag_taxonomy.sys, "stdout", output),
            ):
                exit_code = tag_taxonomy.main(
                    [
                        "--root",
                        str(root),
                        "--taxonomy",
                        str(TAXONOMY_PATH),
                        "--apply",
                    ]
                )
                after = note.read_text(encoding="utf-8")

        rendered = output.getvalue()
        self.assertEqual(exit_code, 1)
        writer.assert_not_called()
        self.assertIn("CONFLICTS\t1", rendered)
        self.assertNotIn("MIGRATED\t", rendered)
        self.assertEqual(after, live_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
