from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
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

    def test_taxonomy_keeps_generic_policy_entries(self) -> None:
        taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))

        canonical = set(taxonomy["canonical"])
        scoped = {
            tag
            for tags in taxonomy["scoped"].values()
            for tag in tags
        }
        self.assertEqual(scoped & canonical, set())
        self.assertNotIn("ajanlar", taxonomy["canonical"])
        self.assertIn("kod-inceleme", taxonomy["canonical"])
        self.assertNotEqual(
            taxonomy["aliases"].get("code-review"),
            "sürüm-kontrolü",
        )

    def test_scoped_tags_are_allowed_only_below_their_project_path(self) -> None:
        project_name = "Project Alpha"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(
                json.dumps(
                    {
                        "canonical": ["finans"],
                        "scoped": {project_name: ["sinyal-stratejisi"]},
                        "aliases": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            project = root / project_name
            other = root / "Other"
            project.mkdir()
            other.mkdir()
            note = "---\ntags: [finans, sinyal-stratejisi]\n---\n# Note\n"
            (project / "allowed.md").write_text(note, encoding="utf-8")
            (other / "blocked.md").write_text(note, encoding="utf-8")

            taxonomy = tag_taxonomy.load_taxonomy(taxonomy_path)
            violations = tag_taxonomy.audit_vault(root, taxonomy)

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].path.name, "blocked.md")
        self.assertEqual(violations[0].tag, "sinyal-stratejisi")

    def test_project_scoped_tags_can_use_obsidian_hierarchy(self) -> None:
        project_name = "Project Alpha"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(
                json.dumps(
                    {
                        "canonical": ["finans"],
                        "scoped": {project_name: ["project/technical/formasyon"]},
                        "aliases": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            project = root / project_name
            project.mkdir()
            (project / "note.md").write_text(
                "---\ntags: [project/technical/formasyon]\n---\n# Note\n",
                encoding="utf-8",
            )

            taxonomy = tag_taxonomy.load_taxonomy(taxonomy_path)
            violations = tag_taxonomy.audit_vault(root, taxonomy)

        self.assertEqual(violations, [])

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

    def test_inline_tag_audit_respects_fence_char_and_length_boundaries(self) -> None:
        note = tag_taxonomy.NoteIndex(
            Path('note.md'),
            PurePosixPath('note.md'),
            '````markdown\n'
            '#fake-four\n'
            '```\n'
            '#fake-short-closer\n'
            '````\n'
            '~~~markdown\n'
            '#fake-tilde\n'
            '```\n'
            '#fake-mixed\n'
            '~~~\n'
            'Gerçek #gercek\n',
        )

        violations = tag_taxonomy._inline_tag_violations(note)

        self.assertEqual([violation.tag for violation in violations], ['gercek'])

        nested = tag_taxonomy.NoteIndex(
            Path('nested.md'),
            PurePosixPath('nested.md'),
            '- Ana madde\n'
            '    #nested-tag\n'
            'Gerçek #gercek\n',
        )
        self.assertEqual(
            [violation.tag for violation in tag_taxonomy._inline_tag_violations(nested)],
            ['nested-tag', 'gercek'],
        )

        continuation = tag_taxonomy.NoteIndex(
            Path('continuation.md'),
            PurePosixPath('continuation.md'),
            'Paragraf devam ediyor.\n'
            '    #continuation-tag\n',
        )
        self.assertEqual(
            [violation.tag for violation in tag_taxonomy._inline_tag_violations(continuation)],
            ['continuation-tag'],
        )

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

    def test_cli_revalidates_captured_notes_before_reporting(self) -> None:
        original_audit = tag_taxonomy.audit_vault
        for mutation in ("added", "removed", "changed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                note = root / "note.md"
                note.write_text("---\ntags: [arama]\n---\n", encoding="utf-8")

                def audit_then_change(*args):
                    result = original_audit(*args)
                    if mutation == "added":
                        (root / "new.md").write_text(
                            "---\ntags: [bilinmeyen]\n---\n#yenietiket\n",
                            encoding="utf-8",
                        )
                    elif mutation == "removed":
                        note.unlink()
                    else:
                        note.write_text(
                            "---\ntags: [bilinmeyen]\n---\n#yenietiket\n",
                            encoding="utf-8",
                        )
                    return result

                output = io.StringIO()
                with (
                    mock.patch.object(tag_taxonomy, "vault_notes", wraps=tag_taxonomy.vault_notes) as scan,
                    mock.patch.object(tag_taxonomy, "audit_vault", side_effect=audit_then_change),
                    mock.patch.object(tag_taxonomy.sys, "stdout", output),
                ):
                    exit_code = tag_taxonomy.main(
                        ["--root", str(root), "--taxonomy", str(TAXONOMY_PATH)]
                    )

                self.assertEqual(exit_code, 1)
                self.assertEqual(scan.call_count, 2)
                self.assertIn(
                    "ERROR\tnote-set-changed" if mutation != "changed" else "ERROR\tnote-changed:note.md",
                    output.getvalue(),
                )
                self.assertNotIn("CANONICAL\t", output.getvalue())
                if mutation == "added":
                    self.assertTrue((root / "new.md").is_file())
                elif mutation == "removed":
                    self.assertFalse(note.exists())
                else:
                    self.assertIn("bilinmeyen", note.read_text(encoding="utf-8"))

    def test_cli_fails_closed_on_unreadable_note_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unreadable = tag_taxonomy.NoteIndex(
                root / "note.md",
                PurePosixPath("note.md"),
                "",
                {},
                "PermissionError",
            )
            output = io.StringIO()
            with (
                mock.patch.object(tag_taxonomy, "vault_notes", return_value=(unreadable,)),
                mock.patch.object(tag_taxonomy.sys, "stdout", output),
            ):
                exit_code = tag_taxonomy.main(
                    ["--root", str(root), "--taxonomy", str(TAXONOMY_PATH)]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("ERROR\tnote-unreadable:note.md:PermissionError", output.getvalue())
        self.assertNotIn("CANONICAL\t", output.getvalue())

    def test_cli_fails_closed_when_final_note_snapshot_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            note.write_text("---\ntags: [arama]\n---\n", encoding="utf-8")
            captured = tag_taxonomy.vault_notes(root)
            unreadable = tag_taxonomy.NoteIndex(
                note,
                PurePosixPath("note.md"),
                "",
                {},
                "PermissionError",
            )
            output = io.StringIO()
            with (
                mock.patch.object(
                    tag_taxonomy,
                    "vault_notes",
                    side_effect=[captured, (unreadable,)],
                ) as scan,
                mock.patch.object(tag_taxonomy.sys, "stdout", output),
            ):
                exit_code = tag_taxonomy.main(
                    ["--root", str(root), "--taxonomy", str(TAXONOMY_PATH)]
                )

        self.assertEqual(exit_code, 1)
        self.assertEqual(scan.call_count, 2)
        self.assertIn("ERROR\tnote-unreadable:note.md:PermissionError", output.getvalue())
        self.assertNotIn("CANONICAL\t", output.getvalue())

    def test_cli_conflict_is_nonzero_and_does_not_claim_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "note.md"
            note.write_text(
                "---\ntags: [search]\n---\nEski gövde\n",
                encoding="utf-8",
            )
            live_source = "---\ntags: [arama]\n---\nYeni gövde #yarisan\n"

            @contextmanager
            def racing_lock(_path: Path):
                note.write_text(live_source, encoding="utf-8")
                yield None

            output = io.StringIO()
            with (
                mock.patch.object(tag_taxonomy, "locked", racing_lock),
                mock.patch.object(tag_taxonomy, "atomic_write_text") as writer,
                mock.patch.object(tag_taxonomy, "vault_notes", wraps=tag_taxonomy.vault_notes) as scan,
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
        self.assertEqual(scan.call_count, 3)
        self.assertIn("VIOLATIONS\t0", rendered.splitlines())
        self.assertIn("INLINE_VIOLATIONS\t1", rendered.splitlines())
        self.assertIn("yarisan", rendered)
        self.assertNotIn("MIGRATED\t", rendered)
        self.assertEqual(after, live_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
