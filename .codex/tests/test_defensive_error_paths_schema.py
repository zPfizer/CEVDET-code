"""Profil, taksonomi ve bilgi şeması doğrulayıcılarının savunma dalları.

%100 kapsam sözleşmesinin üçüncü dilimi. Girdi ile üretilemeyen tek sınıf
işletim sistemi/reparse hataları; yalnız oralarda dar kapsamlı mock var.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import knowledge_schema
import profile_guard
import tag_taxonomy
from vault_corpus import NoteIndex


ANCHOR = "a" * 64
CLAIM_ROW_TEXT = (
    "- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-01 "
    f"[[daily/2026-09-01#user-{ANCHOR}|Kullanıcı dayanağı]] — Hızlı yanıt tercihi."
)


def _junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


def _taxonomy_file(tmp: Path, payload: dict) -> Path:
    path = tmp / "taxonomy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class ProfileGuardEdges(unittest.TestCase):
    def test_iso_rejects_impossible_calendar_date(self) -> None:
        self.assertIsNone(profile_guard._iso("2026-13-99"))

    def test_frontmatter_without_closing_fence_is_none(self) -> None:
        self.assertIsNone(profile_guard._frontmatter_updated("---\nupdated: 2026-09-11"))

    def test_read_maps_failures_to_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "not.md"
            target.write_text("içerik", encoding="utf-8")

            with mock.patch.object(profile_guard, "_reparse", side_effect=OSError):
                self.assertEqual(
                    profile_guard._read(root, target, None, "yok", "kapalı"),
                    (None, "kapalı"),
                )

            def kirik_reader(_path: Path) -> str:
                raise ValueError("okunamadı")

            self.assertEqual(
                profile_guard._read(root, target, kirik_reader, "yok", "kapalı"),
                (None, "kapalı"),
            )

            bozuk = root / "bozuk.md"
            bozuk.write_bytes(bytes([255, 254, 250]))
            self.assertEqual(
                profile_guard._read(root, bozuk, None, "yok", "kapalı"),
                (None, "kapalı"),
            )

    def test_reparse_handles_foreign_and_broken_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(profile_guard._reparse(Path("C:/baska/yer"), root))
            inner = root / "ic"
            inner.mkdir()
            # is_symlink OSError'ı yutar; gerçek OS hatası dar mock ile enjekte edilir.
            with mock.patch("pathlib.Path.is_symlink", side_effect=OSError):
                self.assertTrue(profile_guard._reparse(inner / "alt.md", root))

    def test_check_links_flags_each_violation_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issues: list[str] = []
            profile_guard._check_links(
                "[[mailto:x@y]] [[dosya.txt]] [[../dışarı]] [[yok-not]]",
                root,
                issues,
            )
        self.assertIn("profile-link-external", issues)
        self.assertIn("profile-link-format", issues)
        self.assertIn("profile-link-traversal", issues)
        self.assertIn("profile-link-broken", issues)

    def test_check_links_survives_resolve_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issues: list[str] = []
            with mock.patch("pathlib.Path.resolve", side_effect=OSError):
                profile_guard._check_links("[[a]]", root, issues)
            self.assertEqual(issues, ["profile-link-invalid"])

            issues = []
            real = Path(root).resolve()
            with mock.patch(
                "pathlib.Path.resolve", side_effect=[real, OSError("kırık")]
            ):
                profile_guard._check_links("[[a]]", root, issues)
            self.assertEqual(issues, ["profile-link-invalid"])

    def test_resolved_escape_without_dotdot_is_traversal(self) -> None:
        # ".." içermeyen ama çözümlemesi kök dışına düşen bağlantı (ör. reparse
        # sonrası) girdiyle üretilemez; sınır dar mock ile doğrulanır.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "not.md").write_text("x", encoding="utf-8")
            issues: list[str] = []
            with mock.patch("pathlib.Path.is_relative_to", return_value=False):
                profile_guard._check_links("[[not]]", root, issues)
        self.assertEqual(issues, ["profile-link-traversal"])

    def test_portrait_and_claim_rows_require_single_headings(self) -> None:
        self.assertEqual(profile_guard.portrait("başlıksız"), "")
        self.assertEqual(profile_guard._claim_rows("başlıksız"), [])

    def test_check_profile_handles_unresolvable_root_and_empty_text(self) -> None:
        with mock.patch("pathlib.Path.resolve", side_effect=OSError):
            self.assertEqual(
                profile_guard.check_profile(Path("her/yer")), ("profile-unavailable",)
            )
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                profile_guard.check_profile(Path(temporary), text=""),
                ("profile-empty",),
            )

    def test_style_section_without_bullets_is_flagged(self) -> None:
        text = (
            "---\nupdated: 2026-09-10\n---\n"
            "## Oturum Portresi\nKısa portre.\n\n"
            "## Vault'ta çalışma ve yanıt tarzı\nMadde yok.\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            issues = profile_guard.check_profile(Path(temporary), text=text)
        self.assertIn("profile-preference-missing", issues)

    def test_preference_source_dates_and_evidence_are_verified(self) -> None:
        profile = (
            "---\nupdated: 2026-09-10\n---\n"
            "## Oturum Portresi\nKısa portre.\n\n"
            "## Vault'ta çalışma ve yanıt tarzı\n"
            "- Hızlı yanıt tercihi. Kaynak: [[knowledge/concepts/hiz|Hız]]"
            " · kullanıcı tercihi · 2026-09-01.\n"
        )
        future_row = CLAIM_ROW_TEXT.replace(
            "[[daily/2026-09-01", "[[daily/2027-01-01"
        ).replace("2026-09-01 [[", "2026-09-01 [[")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "knowledge" / "concepts"
            concepts.mkdir(parents=True)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-01.md").write_text("kanıtsız", encoding="utf-8")

            # Kaynak günü profil güncellemesinden sonra: profile-source-date.
            (concepts / "hiz.md").write_text(
                "## Kayıtlar\n" + future_row + "\n", encoding="utf-8"
            )
            issues = profile_guard.check_profile(root, text=profile)
            self.assertIn("profile-source-date", issues)

            # Gün yerinde ama daily'de kanıt yok: profile-user-evidence.
            (concepts / "hiz.md").write_text(
                "## Kayıtlar\n" + CLAIM_ROW_TEXT + "\n", encoding="utf-8"
            )
            issues = profile_guard.check_profile(root, text=profile)
            self.assertIn("profile-user-evidence", issues)


class TagTaxonomyEdges(unittest.TestCase):
    def _base(self) -> dict:
        return {"canonical": ["sistem"], "aliases": {}, "scoped": {}, "retired": []}

    def test_load_taxonomy_rejects_each_schema_violation(self) -> None:
        cases = [
            {**self._base(), "canonical": ["BÜYÜK"]},
            {**self._base(), "canonical": ["a", "a"]},
            {**self._base(), "scoped": {"a/../b": ["x"]}},
            {**self._base(), "scoped": {"alan": ["BÜYÜK"]}},
            {**self._base(), "scoped": {"alan": ["x", "x"]}},
            {**self._base(), "scoped": {"alan": ["sistem"]}},
            {**self._base(), "aliases": {"BÜYÜK": "sistem"}},
            {**self._base(), "aliases": {"eski": "bilinmez"}},
            {**self._base(), "aliases": {"sistem": "sistem"}},
            {**self._base(), "retired": ["BÜYÜK"]},
            {**self._base(), "retired": ["sistem"]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    path = _taxonomy_file(Path(temporary), payload)
                    with self.assertRaises(tag_taxonomy.TaxonomyError):
                        tag_taxonomy.load_taxonomy(path)

    def test_inline_helpers_cover_quote_and_scalar_forms(self) -> None:
        self.assertEqual(tag_taxonomy._strip_quotes('"alıntı"'), "alıntı")
        self.assertEqual(tag_taxonomy._inline_tags("[]"), ())
        self.assertEqual(tag_taxonomy._inline_tags("tek-etiket"), ("tek-etiket",))
        self.assertEqual(tag_taxonomy._inline_tags('""'), ())

    def test_find_tag_block_parses_block_style_and_open_frontmatter(self) -> None:
        _lines, _nl, _tnl, block = tag_taxonomy._find_tag_block("---\nkapanmadı")
        self.assertIsNone(block)
        _lines, _nl, _tnl, block = tag_taxonomy._find_tag_block(
            "---\ntags:\n  - bir\n  - iki\n---\ngövde\n"
        )
        self.assertEqual(block.tags, ("bir", "iki"))
        self.assertEqual(block.style, "block")
        # Blok, etiket olmayan ilk satırda durmalı; sonraki alan yutulmamalı.
        _lines, _nl, _tnl, block = tag_taxonomy._find_tag_block(
            "---\ntags:\n  - bir\nbaska: deger\n---\ngövde\n"
        )
        self.assertEqual(block.tags, ("bir",))

    def test_normalize_markdown_rewrites_block_style(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            taxonomy = tag_taxonomy.load_taxonomy(
                _taxonomy_file(
                    Path(temporary),
                    {"canonical": ["yeni"], "aliases": {"eski": "yeni"},
                     "scoped": {}, "retired": []},
                )
            )
        updated, unknown = tag_taxonomy.normalize_markdown(
            "---\ntags:\n  - eski\n---\ngövde\n", taxonomy
        )
        self.assertEqual(unknown, [])
        self.assertIn("  - yeni", updated)

    def test_broken_obsidian_config_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".obsidian").mkdir()
            (root / ".obsidian" / "app.json").write_text("{bozuk", encoding="utf-8")
            self.assertEqual(tag_taxonomy.audit_inline_tags(root, notes=[]), [])

    def test_migrate_apply_reports_unreadable_source_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy = tag_taxonomy.load_taxonomy(
                _taxonomy_file(
                    root,
                    {"canonical": ["yeni"], "aliases": {"eski": "yeni"},
                     "scoped": {}, "retired": []},
                )
            )
            ghost = NoteIndex(
                root / "silinmis.md",
                PurePosixPath("silinmis.md"),
                "---\ntags:\n  - eski\n---\ngövde\n",
                {"tags": ["eski"]},
                "",
            )
            result = tag_taxonomy.migrate_vault(root, taxonomy, apply=True, notes=[ghost])
        self.assertEqual(len(result.conflicts), 1)
        self.assertIsNone(result.conflicts[0].current_sha256)

    def test_cli_apply_stops_on_unknown_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            (root / "🧠 500-Knowledge").mkdir(parents=True)
            (root / "🧠 500-Knowledge" / "not.md").write_text(
                "---\ntags:\n  - yok-boyle-etiket\n---\ngövde\n", encoding="utf-8"
            )
            taxonomy_path = _taxonomy_file(Path(temporary), self._base())
            exit_code = tag_taxonomy.main(
                ["--root", str(root), "--taxonomy", str(taxonomy_path), "--apply"]
            )
        self.assertEqual(exit_code, 1)


class KnowledgeSchemaEdges(unittest.TestCase):
    def test_section_with_reversed_headings_is_empty(self) -> None:
        text = "## Son\nx\n## İlk\ny\n"
        self.assertEqual(knowledge_schema._section(text, "## İlk", "## Son"), "")

    def test_normalizers_leave_headingless_or_invalid_text_alone(self) -> None:
        derived = (
            "---\nschema: knowledge-v2\n---\nBaşlıksız gövdede kayıt:\n"
            + CLAIM_ROW_TEXT + "\n"
        )
        self.assertEqual(knowledge_schema.normalize_claim_order(derived), derived)
        bad_sources = (
            "---\nschema: knowledge-v2\nsources:\n  - bozuk-kaynak\n---\ngövde\n"
        )
        self.assertEqual(knowledge_schema.normalize_source_links(bad_sources), bad_sources)

    def test_structural_helpers_defer_to_owning_rules(self) -> None:
        path = Path("kavram.md")
        self.assertTrue(knowledge_schema._concept_points_ok(path, "sırasız"))
        self.assertTrue(knowledge_schema._concept_related_ok(path, "sırasız"))
        self.assertTrue(knowledge_schema._connection_links_ok(path, "connects yok"))

    def test_linked_path_reports_escape_reparse_and_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(
                knowledge_schema._linked_path(Path("C:/baska/yer.md"), root),
                "source-escape",
            )
            self.assertEqual(
                knowledge_schema._linked_path(root / "yok.md", root),
                "source-unreadable",
            )
            target = root / "hedef"
            target.mkdir()
            junction = root / "kestirme"
            _junction(junction, target)
            # Reparse bayrağı junction dışı biçimlerde de yakalanmalı (0x400).
            with mock.patch("pathlib.Path.is_junction", return_value=False):
                self.assertEqual(
                    knowledge_schema._linked_path(junction, root), "source-symlink"
                )
            note = root / "not.md"
            note.write_text("x", encoding="utf-8")
            with mock.patch("pathlib.Path.is_relative_to", return_value=False):
                self.assertEqual(
                    knowledge_schema._linked_path(note, root), "source-escape"
                )

    def test_unlistable_knowledge_directory_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "knowledge" / "concepts"
            concepts.mkdir(parents=True)
            issues: list[str] = []
            with mock.patch("pathlib.Path.iterdir", side_effect=OSError):
                sources = knowledge_schema._knowledge_sources(concepts, root, issues)
        self.assertEqual(sources, [])
        self.assertTrue(any("source-unreadable" in issue for issue in issues))

    def test_validate_concept_flags_field_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "kavram.md"
            text = (
                "---\ntitle:\naliases: tek\ntags: tek\nsources: tek\n"
                "created: dun\nupdated: 2026-09-02\n---\ngövde\n"
            )
            path.write_text(text, encoding="utf-8")
            issues: list[str] = []
            knowledge_schema._validate_concept(path, issues)
        joined = "\n".join(issues)
        self.assertIn(":aliases", joined)
        self.assertIn(":tags", joined)
        self.assertIn(":sources", joined)
        self.assertIn(":title", joined)
        self.assertIn(":created-format", joined)

    def test_validate_concept_tolerates_unclosed_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "kavram.md"
            path.write_text("---\ntitle: Yarım", encoding="utf-8")
            issues: list[str] = []
            knowledge_schema._validate_concept(path, issues)
        self.assertTrue(issues)

    def test_validate_connection_stops_after_identity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Bozuk İsim.md"
            path.write_text("connects alanı yok", encoding="utf-8")
            issues: list[str] = []
            knowledge_schema._validate_connection(path, issues)
        self.assertTrue(issues)

    def test_derived_concept_source_rules(self) -> None:
        path = Path("kavram.md")
        issues: list[str] = []
        result = knowledge_schema._validate_derived_concept(
            path, "---\nschema: knowledge-v2\nsources: tek\n---\n", None, issues
        )
        self.assertEqual(result, [])

        issues = []
        text = (
            "---\nschema: knowledge-v2\nsources:\n  - 2026-09-02.md\n  - 2026-09-01.md\n"
            "updated: 2026-08-01\n---\n\n## Kayıtlar\n" + CLAIM_ROW_TEXT + "\n"
        )
        knowledge_schema._validate_derived_concept(path, text, None, issues)
        joined = "\n".join(issues)
        self.assertIn(":source-order", joined)
        self.assertIn(":source-links", joined)
        self.assertIn(":updated-before-claim", joined)

        issues = []
        previous = "---\nsources:\n  - 2025-01-01.md\n---\n"
        current = (
            "---\nschema: knowledge-v2\nsources:\n  - 2026-09-01.md\n"
            "updated: 2026-09-02\n---\n\n## Kayıtlar\n" + CLAIM_ROW_TEXT + "\n"
        )
        knowledge_schema._validate_derived_concept(path, current, previous, issues)
        self.assertIn(":source-history", "\n".join(issues))

    def test_derived_connection_rules(self) -> None:
        path = Path("baglanti.md")
        cases = [
            ("---\nschema: eski\n---\n", ":derived-schema"),
            ("---\nschema: knowledge-v2\n---\n", ":sources"),
            (
                "---\nschema: knowledge-v2\nsources:\n  - bozuk.md\n"
                "updated: 2026-09-02\n---\n",
                ":sources-format",
            ),
            (
                "---\nschema: knowledge-v2\nsources:\n  - 2026-09-02.md\n"
                "  - 2026-09-01.md\nupdated: 2026-09-03\n---\n",
                ":source-order",
            ),
            (
                "---\nschema: knowledge-v2\nsources:\n  - 2026-09-01.md\n"
                "updated: bozuk\n---\n",
                ":updated",
            ),
            (
                "---\nschema: knowledge-v2\nsources:\n  - 2026-09-01.md\n"
                "updated: 2026-08-01\n---\n",
                ":updated-before-source",
            ),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                issues: list[str] = []
                knowledge_schema._validate_derived_connection(path, text, None, issues)
                self.assertIn(expected, "\n".join(issues))

    def test_unreadable_concept_makes_tree_report_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "knowledge" / "concepts"
            concepts.mkdir(parents=True)
            (concepts / "bozuk.md").write_bytes(bytes([255, 254, 250]))
            report = knowledge_schema.validate_knowledge_tree(root)
        self.assertIn("knowledge:source-unreadable", report.issues)

    def test_duplicate_claims_and_connections_are_flagged(self) -> None:
        body = (
            "---\nschema: knowledge-v2\ntitle: T\naliases:\n  - takma\n"
            "tags:\n  - etiket\nsources:\n  - 2026-09-01.md\n"
            "created: 2026-09-01\nupdated: 2026-09-01\n---\n\n"
            "## Kayıtlar\n" + CLAIM_ROW_TEXT + "\n"
        )
        connection = (
            "---\nschema: knowledge-v2\nconnects:\n  - bir\n  - iki\n"
            "sources:\n  - 2026-09-01.md\nupdated: 2026-09-01\n---\n\n"
            "## Bağlantı\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "knowledge" / "concepts"
            connections = root / "knowledge" / "connections"
            concepts.mkdir(parents=True)
            connections.mkdir()
            (concepts / "bir.md").write_text(body, encoding="utf-8")
            (concepts / "iki.md").write_text(body, encoding="utf-8")
            (connections / "bir-iki.md").write_text(connection, encoding="utf-8")
            (connections / "iki-bir.md").write_text(connection, encoding="utf-8")
            report = knowledge_schema.validate_knowledge_tree(root)
        joined = "\n".join(report.issues)
        self.assertIn(":claim-duplicate", joined)
        self.assertIn(":connection-duplicate", joined)


if __name__ == "__main__":
    unittest.main()
