from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import unittest


CODEX_DIR = Path(__file__).resolve().parents[1]
HOOKS_DIR = CODEX_DIR / "hooks"
SCRIPTS_DIR = CODEX_DIR / "scripts"
VAULT_ROOT = CODEX_DIR.parent
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import intake_contract  # noqa: E402
import tag_taxonomy  # noqa: E402


class VaultIngestionSliceATests(unittest.TestCase):
    @staticmethod
    def _note(note_type: str, tags: tuple[str, ...], *, upper_link: bool = True) -> str:
        upper = "Üst yüzey: [[🎯 100-Command-Center/Active Work|Aktif İşler]]\n\n"
        return (
            "---\n"
            "title: Blok Etiketli Kayıt\n"
            "created: 2026-09-02\n"
            "updated: 2026-09-02\n"
            f"type: {note_type}\n"
            "status: active\n"
            "tags:\n"
            + "".join(f"  - {tag}\n" for tag in tags)
            + "---\n\n"
            "# Blok Etiketli Kayıt\n\n"
            + (upper if upper_link else "")
            + "Gövde.\n"
        )

    @staticmethod
    def _taxonomy(vault: Path, payload: dict[str, object]) -> Path:
        taxonomy_path = vault / ".codex" / "tag-taxonomy.json"
        taxonomy_path.parent.mkdir(parents=True, exist_ok=True)
        taxonomy_path.write_text(json.dumps(payload), encoding="utf-8")
        return taxonomy_path

    @staticmethod
    def _visual_note() -> str:
        return (
            "---\n"
            "title: Kanıtlı Görsel Kaydı\n"
            "created: 2026-09-01\n"
            "updated: 2026-09-01\n"
            "type: source-note\n"
            "status: active\n"
            "tags: [vault]\n"
            "dedupe_key: kanitli-gorsel-kaydi\n"
            "source_type: user-provided-image\n"
            "---\n\n"
            "# Kanıtlı Görsel Kaydı\n\n"
            "Üst kayıt: [[Proje İndeksi]]\n\n"
            "![Açıklayıcı grafik alt metni](assets/kanitli-gorsel/01-grafik.png)\n\n"
            "## Görselde açıkça görülenler\n\n- Kaynakta yazılı değer.\n\n"
            "## Görselde verilmeyenler\n\n- Veri kaynağı belirtilmemiş.\n"
        )

    def test_visual_source_note_requires_tansu_evidence_and_asset_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            path = vault / "🏰 300-Projects" / "Demo" / "kanitli-gorsel.md"
            path.parent.mkdir(parents=True)
            self._taxonomy(vault, {"canonical": ["vault"], "scoped": {}, "aliases": {}})

            valid = intake_contract.validate_note(vault, path, self._visual_note())
            invalid_text = (
                self._visual_note()
                .replace("dedupe_key: kanitli-gorsel-kaydi\n", "")
                .replace("![Açıklayıcı grafik alt metni]", "![]")
                .replace("assets/kanitli-gorsel/", "01-grafik/")
                .replace("## Görselde verilmeyenler", "## Notlar")
                .replace("Üst kayıt: [[Proje İndeksi]]\n\n", "")
            )
            invalid = intake_contract.validate_note(vault, path, invalid_text)

        self.assertEqual(valid, ())
        self.assertIn("dedupe-key-missing", invalid)
        self.assertIn("image-alt-missing", invalid)
        self.assertIn("image-assets-folder-invalid", invalid)
        self.assertIn("image-unprovided-section-missing", invalid)
        self.assertIn("upper-index-link-missing", invalid)

    def test_markdown_examples_cannot_supply_intake_links_or_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            path = vault / "🏰 300-Projects" / "Demo" / "örnek.md"
            path.parent.mkdir(parents=True)
            self._taxonomy(vault, {"canonical": ["vault"], "scoped": {}, "aliases": {}})
            text = (
                self._note("source-note", ("vault",), upper_link=False)
                .replace(
                    "status: active\n",
                    "status: active\n"
                    "dedupe_key: ornek-kaydi\n"
                    "source_type: user-provided-text\n",
                )
                + "\n```markdown\n"
                + "Üst kayıt: [[Proje İndeksi]]\n"
                + "![Örnek görsel](not-assets/example.png)\n"
                + "## Görselde açıkça görülenler\n"
                + "## Görselde verilmeyenler\n"
                + "```\n"
            )

            issues = intake_contract.validate_note(vault, path, text)

        self.assertEqual(
            issues,
            ("upper-index-link-missing",),
        )

    def test_type_routes_and_project_marker_fields_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            project_marker = (
                "---\n"
                "title: Demo Proje\ncreated: 2026-09-01\nupdated: 2026-09-01\n"
                "type: project-marker\nstatus: active\ntags: [vault]\n"
                "canonical_link: C:/repo/PROJECT.md\n"
                "last_decision_summary: [[Son Karar]]\n"
                "decision_summary_mirroring: waiting-user\n"
                "---\n\n# Demo Proje\n\nÜst kayıt: [[Projeler]]\n"
            )
            correct = vault / "🏰 300-Projects" / "Demo.md"
            wrong = vault / "🛠️ 600-Arsenal" / "Demo.md"
            self._taxonomy(vault, {"canonical": ["vault"], "scoped": {}, "aliases": {}})

            self.assertEqual(
                intake_contract.validate_note(vault, correct, project_marker),
                (),
            )
            wrong_issues = intake_contract.validate_note(vault, wrong, project_marker)
            missing_issues = intake_contract.validate_note(
                vault,
                correct,
                project_marker.replace("canonical_link: C:/repo/PROJECT.md\n", ""),
            )

        self.assertIn("intake-target-invalid:project-marker", wrong_issues)
        self.assertIn("project-canonical-link-missing", missing_issues)

    def test_machine_managed_paths_are_exempt_but_human_content_is_not(self) -> None:
        exempt = (
            Path("daily/2026-09-01.md"),
            Path("knowledge/connections/a.md"),
            Path("🔮 850-Companion/Journal.md"),
        )
        for path in exempt:
            with self.subTest(path=path):
                self.assertFalse(intake_contract.should_validate_path(path))
        self.assertTrue(
            intake_contract.should_validate_path(
                Path("🛠️ 600-Arsenal/Vault ve Yönetişim/Yeni.md")
            )
        )

    def test_new_note_tags_must_be_canonical_for_its_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            taxonomy_path = vault / ".codex" / "tag-taxonomy.json"
            taxonomy_path.parent.mkdir(parents=True)
            taxonomy_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "language": "tr",
                        "format": "lowercase-kebab-case",
                        "canonical": ["vault"],
                        "scoped": {},
                        "aliases": {"governance": "vault"},
                    }
                ),
                encoding="utf-8",
            )
            path = vault / "🏰 300-Projects" / "Demo" / "kaynak.md"
            text = self._visual_note().replace("tags: [vault]", "tags: [governance]")

            issues = intake_contract.validate_note(vault, path, text)

        self.assertIn("tag-not-canonical:governance", issues)

    def test_tagged_human_note_requires_readable_taxonomy(self) -> None:
        cases = (
            ("missing", None, "taxonomy-unreadable:"),
            ("malformed", "file", "taxonomy-schema-invalid"),
            ("unreadable", "directory", "taxonomy-unreadable:"),
        )
        for label, shape, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                path = vault / "🏰 300-Projects" / "Demo" / "kaynak.md"
                path.parent.mkdir(parents=True)
                taxonomy_path = vault / ".codex" / "tag-taxonomy.json"
                taxonomy_path.parent.mkdir(parents=True)
                if shape == "file":
                    taxonomy_path.write_text("{}", encoding="utf-8")
                elif shape == "directory":
                    taxonomy_path.mkdir()

                issues = intake_contract.validate_note(vault, path, self._visual_note())

            self.assertTrue(any(issue.startswith(expected) for issue in issues), issues)

    def test_machine_managed_note_paths_remain_exempt_without_taxonomy(self) -> None:
        text = self._visual_note()
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            for relative in (
                Path("daily/2026-09-01.md"),
                Path("knowledge/connections/a.md"),
                Path("🔮 850-Companion/Journal.md"),
            ):
                self.assertEqual(intake_contract.validate_note(vault, vault / relative, text), ())

    def test_block_style_tags_are_read_as_a_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            self._taxonomy(
                vault,
                {"canonical": ["vault", "bilgi-mimarisi"], "scoped": {}, "aliases": {}},
            )
            path = vault / "🎯 100-Command-Center" / "Completed Work.md"

            issues = intake_contract.validate_note(
                vault,
                path,
                self._note("work-index", ("vault", "bilgi-mimarisi")),
            )

        self.assertEqual(issues, ())

    def test_dashboard_note_with_block_tags_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            self._taxonomy(
                vault,
                {"canonical": ["vault", "bilgi-mimarisi"], "scoped": {}, "aliases": {}},
            )
            path = vault / "🎯 100-Command-Center" / "Dashboard.md"

            issues = intake_contract.validate_note(
                vault,
                path,
                self._note("dashboard", ("vault", "bilgi-mimarisi"), upper_link=False),
            )

        self.assertEqual(issues, ())

    def test_intake_tag_verdict_matches_public_tag_violations(self) -> None:
        samples = (
            ("🏰 300-Projects/Demo/kaynak.md", ("vault", "demo/kapsam")),
            ("🏰 300-Projects/Başka/kaynak.md", ("demo/kapsam",)),
            ("🛠️ 600-Arsenal/rehber.md", ("governance", "vault")),
            ("🛠️ 600-Arsenal/temiz.md", ("vault",)),
        )
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            taxonomy_path = self._taxonomy(
                vault,
                {
                    "canonical": ["vault"],
                    "scoped": {"🏰 300-Projects/Demo": ["demo/kapsam"]},
                    "aliases": {"governance": "vault"},
                },
            )
            taxonomy = tag_taxonomy.load_taxonomy(taxonomy_path)
            for relative, tags in samples:
                with self.subTest(path=relative):
                    issues = intake_contract.validate_note(
                        vault,
                        vault / relative,
                        self._note("reference", tags),
                    )
                    expected = tuple(
                        f"tag-not-canonical:{tag}"
                        for tag in tag_taxonomy.tag_violations(
                            taxonomy, Path(relative), tags
                        )
                    )

                    self.assertEqual(
                        tuple(
                            issue
                            for issue in issues
                            if issue.startswith("tag-not-canonical:")
                        ),
                        expected,
                    )

if __name__ == "__main__":
    unittest.main(verbosity=2)
