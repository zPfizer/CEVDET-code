from __future__ import annotations

from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import doctor  # noqa: E402
import graph_integrity  # noqa: E402
import tag_taxonomy  # noqa: E402
import vault_corpus  # noqa: E402


NOTE = """---
title: {title}
created: 2026-09-02
updated: 2026-09-02
tags: [doğrulama]
---
# {title}

{body}
"""


def _build_vault(root: Path) -> None:
    knowledge = root / "🧠 500-Knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "Hedef.md").write_text(
        NOTE.format(title="Hedef", body="# Hedef"),
        encoding="utf-8",
    )
    (knowledge / "Kaynak.md").write_text(
        NOTE.format(title="Kaynak", body="[[🧠 500-Knowledge/Hedef|Hedef]]"),
        encoding="utf-8",
    )
    daily = root / "daily"
    daily.mkdir()
    (daily / "2026-09-02.md").write_text(
        "# Günlük Log: 2026-09-02\n\n[[🧠 500-Knowledge/Hedef|Hedef]]\n",
        encoding="utf-8",
    )


class VaultCorpusTests(unittest.TestCase):
    def test_dotted_link_targets_never_resolve_to_truncated_filename(self) -> None:
        for exists in (False, True):
            with self.subTest(target_exists=exists), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                knowledge = vault / "knowledge"
                knowledge.mkdir()
                (knowledge / "v1.md").write_text("# Unrelated", encoding="utf-8")
                (knowledge / "index.md").write_text("[[knowledge/v1.2]]", encoding="utf-8")
                if exists:
                    (knowledge / "v1.2.md").write_text("# Target", encoding="utf-8")
                notes = vault_corpus.vault_notes(vault)
                result = vault_corpus.resolve_link(
                    "knowledge/v1.2", vault_corpus.by_key(notes), vault_corpus.by_stem(notes),
                )
                self.assertEqual(result.key if result else None, "knowledge/v1.2.md" if exists else None)
                self.assertEqual(doctor._vault_link_check(doctor.Context(vault)).status,
                                 "OK" if exists else "FAIL")
                _total, isolated = graph_integrity.graph_summary(notes)
                self.assertIn("knowledge/v1.md", isolated)
                self.assertEqual("knowledge/index.md" in isolated, not exists)

    def test_short_links_with_or_without_md_reach_the_same_note(self) -> None:
        for target in ("Hedef", "Hedef.md", "v1.2", "v1.2.md"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                knowledge = vault / "knowledge"
                knowledge.mkdir()
                name = "v1.2.md" if target.startswith("v1.2") else "Hedef.md"
                (knowledge / name).write_text("# Target", encoding="utf-8")
                (knowledge / "index.md").write_text(f"[[{target}]]", encoding="utf-8")
                notes = vault_corpus.vault_notes(vault)
                result = vault_corpus.resolve_link(target, vault_corpus.by_key(notes),
                                                  vault_corpus.by_stem(notes))
                self.assertEqual(result.key if result else None, f"knowledge/{name}")
                self.assertEqual(doctor._vault_link_check(doctor.Context(vault)).status, "OK")
                self.assertEqual(graph_integrity.graph_summary(notes), (2, []))

    def test_link_fallback_keeps_ambiguity_and_vault_boundary_guards(self) -> None:
        notes = tuple(vault_corpus.NoteIndex(Path(key), PurePosixPath(key), "")
                      for key in ("knowledge/Hedef.md", "daily/Hedef.md"))
        keyed, stems = vault_corpus.by_key(notes), vault_corpus.by_stem(notes)
        for target in ("Hedef", "Hedef.md", "../Hedef.md", "/Hedef.md", "C:/Hedef.md"):
            with self.subTest(target=target):
                self.assertIsNone(vault_corpus.resolve_link(target, keyed, stems))
        self.assertIs(vault_corpus.resolve_link("knowledge/Hedef.md", keyed, stems), notes[0])

    def test_companion_sources_are_not_duplicate_notes_but_other_sources_remain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            relatives = (
                "🔮 850-Companion/Last-Session.md",
                "🔮 850-Companion/Sources/Last-Session.md",
                "🧠 500-Knowledge/Sources/Real.md",
            )
            for relative in relatives:
                path = vault / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(NOTE.format(title=path.stem, body="Note"), encoding="utf-8")
            self.assertEqual(
                {note.key for note in vault_corpus.vault_notes(vault)},
                {relatives[0], relatives[2]},
            )

    def test_root_tmp_copies_are_not_notes_but_nested_user_tmp_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            for relative in ("tmp/audit/Copy.md", "🧠 500-Knowledge/tmp/Real.md"):
                path = vault / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(NOTE.format(title=path.stem, body="Note"), encoding="utf-8")

            self.assertEqual(
                [note.key for note in vault_corpus.vault_notes(vault)],
                ["🧠 500-Knowledge/tmp/Real.md"],
            )
            self.assertEqual(
                list(vault_corpus.markdown_paths(vault / "🧠 500-Knowledge")),
                [vault / "🧠 500-Knowledge/tmp/Real.md"],
            )
            (vault / ".gitignore").write_text("/tmp/\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(vault)], check=True, capture_output=True)
            ignored = subprocess.run(
                ["git", "-C", str(vault), "check-ignore", "-z", "--stdin"],
                input="tmp/audit/Copy.md\0🧠 500-Knowledge/tmp/Real.md\0".encode("utf-8"),
                check=True, capture_output=True,
            )
            self.assertEqual(ignored.stdout, b"tmp/audit/Copy.md\0")

    def test_code_review_graph_vendor_markdown_is_not_a_note_but_human_vendor_note_is(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            relatives = (
                ".code-review-graph/venv/Lib/site-packages/beartype/README.md",
                "🧠 500-Knowledge/vendor/Human.md",
            )
            for relative in relatives:
                path = vault / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(NOTE.format(title=path.stem, body="Note"), encoding="utf-8")

            self.assertEqual(
                [note.key for note in vault_corpus.vault_notes(vault)],
                [relatives[1]],
            )

    def test_retrieval_corpus_admits_inbox_and_command_center(self) -> None:
        # 2026-09-05: kullanıcı bütün notların aranabilir olmasını istedi.
        self.assertIn("🎯 100-Command-Center", vault_corpus.RETRIEVAL_CONTENT_ROOTS)
        self.assertIn("📥 000-Inbox", vault_corpus.RETRIEVAL_CONTENT_ROOTS)
        self.assertEqual(
            vault_corpus.RETRIEVAL_EXCLUDED_ROOTS,
            (),
        )
        for included in ("📦 900-Archive", "📋 Templates"):
            self.assertIn(included, vault_corpus.RETRIEVAL_CONTENT_ROOTS)

    def test_daily_note_link_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _build_vault(vault)
            (vault / "daily" / "2026-09-01.md").write_text(
                "# Günlük Log: 2026-09-01\n\n[[Olmayan Not|Kırık]]\n",
                encoding="utf-8",
            )

            checks = doctor.run_checks(vault, project_root=vault)

        links = next(check for check in checks if check.name == "Vault bağlantıları")
        self.assertEqual(links.status, "FAIL")
        self.assertIn("daily/2026-09-01.md -> Olmayan Not", links.evidence)

    def test_four_vault_checks_share_one_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _build_vault(vault)
            (vault / "Views.base").write_text("views: []\n", encoding="utf-8")

            with mock.patch.object(
                vault_corpus.os,
                "walk",
                wraps=vault_corpus.os.walk,
            ) as walk, mock.patch.object(
                doctor,
                "build_vault_map",
                return_value=(),
            ), mock.patch.object(
                doctor,
                "vault_notes",
                wraps=vault_corpus.vault_notes,
            ) as traversal, mock.patch.object(
                tag_taxonomy,
                "vault_notes",
                side_effect=AssertionError("tag scan walked the vault again"),
            ):
                checks = doctor.run_checks(vault, project_root=vault)

        self.assertEqual(traversal.call_count, 1)
        self.assertEqual(walk.call_count, 1)
        for name in (
            "Vault bağlantıları",
            "Vault grafiği",
            "Metadata şeması",
            "Etiket sözlüğü",
        ):
            self.assertTrue(any(check.name == name for check in checks), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
