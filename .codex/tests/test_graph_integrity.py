from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from graph_integrity import (  # noqa: E402
    DAILY_GRAPH_LINK,
    GraphPolicyError,
    daily_with_graph_link,
    ensure_daily_graph_link,
    graph_summary,
    normalize_connection_links,
)
from markdown_boundary import markdown_body, markdown_link_spans  # noqa: E402
from vault_corpus import vault_notes  # noqa: E402


class GraphIntegrityTests(unittest.TestCase):
    def test_inline_link_metadata_uses_destination_and_title_boundaries(self) -> None:
        for text in (
            '[x](url "title ) value")',
            "[x](url 'title ( value')",
            '[x](url (parenthesized title))',
            '[x](<a)b> "title")',
            '[x](a(b)c "title")',
        ):
            with self.subTest(text=text):
                self.assertEqual(markdown_link_spans(text), ((0, len(text)),))
                self.assertEqual(markdown_link_spans(text, include_labels=False), ((3, len(text)),))
        for text in (
            "[x](url 'title\n\nvisible')",
            "[x](url 'title'\n\n)",
            '[x](<a<b> "title")',
            "[x](url 'unterminated)",
        ):
            with self.subTest(text=text):
                self.assertEqual(markdown_link_spans(text), ())

    def test_unmatched_inline_backtick_is_literal_and_does_not_hang(self) -> None:
        text = "# Günlük Log: 2026-09-08\ntext`"
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "from graph_integrity import daily_with_graph_link\n"
            f"print(repr(daily_with_graph_link({text!r}, '2026-09-08', '\\n')))\n"
        )

        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=2,
            check=True,
        )

        self.assertIn("text`", result.stdout)
        self.assertIn(DAILY_GRAPH_LINK, result.stdout)

    def test_multiline_inline_code_does_not_hide_block_connection_heading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "knowledge" / "concepts"
            connection = root / "knowledge" / "connections" / "alpha--beta.md"
            concepts.mkdir(parents=True)
            connection.parent.mkdir()
            for slug in ("alpha", "beta"):
                (concepts / f"{slug}.md").write_text(
                    f"---\ntitle: {slug}\n---\n", encoding="utf-8"
                )
            original = (
                "---\nconnects: [alpha, beta]\n---\n"
                "# Alfa ve Beta\n\n`\n## Bağlantı\n`\n"
            )
            connection.write_text(original, encoding="utf-8")

            self.assertEqual(normalize_connection_links(root), 1)
            updated = connection.read_text(encoding="utf-8")
            self.assertIn("[[knowledge/concepts/alpha|alpha]]", updated)
            self.assertIn("[[knowledge/concepts/beta|beta]]", updated)

    def test_multiline_inline_code_does_not_hide_a_block_heading_link(self) -> None:
        for example in (
            "`\n## [[../secret]]\n`",
            "> `\n> ## [[../secret]]\n> `",
        ):
            with self.subTest(example=example):
                body = markdown_body(example)

                self.assertIn("[[../secret]]", body)

        paragraph = markdown_body("> `foo\n> bar`")
        self.assertNotIn("foo", paragraph)
        self.assertNotIn("bar", paragraph)

    def test_graph_summary_keeps_observable_property_wikilinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A.md").write_text(
                '---\nrelated: "[[B]]"\n---\n# A\n', encoding="utf-8"
            )
            (root / "B.md").write_text("# B\n", encoding="utf-8")

            total, isolated = graph_summary(vault_notes(root))

        self.assertEqual(total, 2)
        self.assertEqual(isolated, [])

    def test_daily_graph_link_masks_exact_backtick_runs_and_backslash_closers(self) -> None:
        cases = (
            "# Günlük Log: 2026-09-08\n"
            "`foo `` [[knowledge/index|Bilgi Tabanı]] `\n",
            "# Günlük Log: 2026-09-08\n"
            "`[[knowledge/index|Bilgi Tabanı]]\\`\n",
        )
        for text in cases:
            with self.subTest(text=text):
                updated = daily_with_graph_link(text, "2026-09-08", "\n")

                self.assertEqual(updated.count(DAILY_GRAPH_LINK), 2)
                self.assertIn(
                    f"# Günlük Log: 2026-09-08\n\n{DAILY_GRAPH_LINK}\n",
                    updated,
                )

    def test_graph_summary_ignores_links_inside_exact_or_backslash_closed_spans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A.md").write_text(
                "# A\n"
                "`foo `` [[B]] `\n"
                "`[[B]]\\`\n",
                encoding="utf-8",
            )
            (root / "B.md").write_text("# B\n", encoding="utf-8")

            total, isolated = graph_summary(vault_notes(root))

        self.assertEqual(total, 2)
        self.assertEqual(isolated, ["A.md", "B.md"])

    def test_escaped_opening_backtick_does_not_hide_a_real_link(self) -> None:
        text = (
            "# Günlük Log: 2026-09-08\n"
            "\\`[[knowledge/index|Bilgi Tabanı]]`\n"
        )

        self.assertEqual(daily_with_graph_link(text, "2026-09-08", "\n"), text)

    def test_escaped_opening_wikilink_is_not_an_existing_daily_link(self) -> None:
        text = (
            "# Günlük Log: 2026-09-08\n"
            "\\[[knowledge/index|Bilgi Tabanı]]\n"
        )

        updated = daily_with_graph_link(text, "2026-09-08", "\n")

        self.assertEqual(updated.count(DAILY_GRAPH_LINK), 2)
        self.assertIn(
            f"# Günlük Log: 2026-09-08\n\n{DAILY_GRAPH_LINK}\n",
            updated,
        )

    def test_double_backslash_opening_wikilink_remains_real(self) -> None:
        text = (
            "# Günlük Log: 2026-09-08\n"
            "\\\\[[knowledge/index|Bilgi Tabanı]]\n"
        )

        self.assertEqual(daily_with_graph_link(text, "2026-09-08", "\n"), text)

    def test_graph_summary_ignores_escaped_opening_wikilinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A.md").write_text(
                "# A\n\\[[B]]\n", encoding="utf-8"
            )
            (root / "B.md").write_text("# B\n", encoding="utf-8")

            total, isolated = graph_summary(vault_notes(root))

        self.assertEqual(total, 2)
        self.assertEqual(isolated, ["A.md", "B.md"])

    def test_daily_graph_link_ignores_literal_links_in_fences_and_inline_code(self) -> None:
        for fence in ("```text", "~~~text"):
            with self.subTest(fence=fence):
                text = (
                    "---\n"
                    "title: `[[knowledge/index|Bilgi Tabanı]]`\n"
                    "---\n"
                    "# Günlük Log: 2026-09-08\n"
                    "\n"
                    f"{fence}\n"
                    f"{DAILY_GRAPH_LINK}\n"
                    f"{fence[0] * len(fence.rstrip('text'))}\n"
                )

                updated = daily_with_graph_link(text, "2026-09-08", "\n")

                self.assertEqual(updated.count(DAILY_GRAPH_LINK), 3)
                self.assertIn(
                    f"# Günlük Log: 2026-09-08\n\n{DAILY_GRAPH_LINK}\n\n{fence}",
                    updated,
                )
                self.assertIn("title: `[[knowledge/index|Bilgi Tabanı]]`", updated)

    def test_daily_graph_link_rejects_literal_only_heading_without_writing(self) -> None:
        text = "```markdown\n# Günlük Log: 2026-09-08\n```\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "2026-09-08.md"
            path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(
                GraphPolicyError, "daily-heading-invalid:2026-09-08.md"
            ):
                ensure_daily_graph_link(path)

            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_connection_normalizer_uses_real_heading_and_links_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "knowledge" / "concepts"
            connection = root / "knowledge" / "connections" / "alpha--beta.md"
            concepts.mkdir(parents=True)
            connection.parent.mkdir()
            (concepts / "alpha.md").write_text(
                "---\ntitle: Alfa\n---\n", encoding="utf-8"
            )
            (concepts / "beta.md").write_text(
                "---\ntitle: Beta\n---\n", encoding="utf-8"
            )
            original = (
                "---\n"
                "title: ## Bağlantı\n"
                "connects: [alpha, beta]\n"
                "---\n"
                "# Alfa ve Beta\n\n"
                "```text\n"
                "## Bağlantı\n"
                "[[knowledge/concepts/alpha|Alfa]]\n"
                "```\n\n"
                "## Bağlantı\n\n"
                "Kullanıcı metni korunmalı.\n\n"
                "## Ana Fikir\n\n"
                "Bağ.\n"
            )
            connection.write_text(original, encoding="utf-8")

            self.assertEqual(normalize_connection_links(root), 1)
            updated = connection.read_text(encoding="utf-8")

        self.assertTrue(updated.startswith("---\ntitle: ## Bağlantı\n"))
        self.assertIn("Kullanıcı metni korunmalı.", updated)
        self.assertIn(
            "## Bağlantı\n\n"
            "[[knowledge/concepts/alpha|Alfa]] ↔ [[knowledge/concepts/beta|Beta]]\n\n"
            "Kullanıcı metni korunmalı.",
            updated,
        )
        self.assertEqual(updated.count("[[knowledge/concepts/alpha|Alfa]]"), 2)

    def test_connection_normalizer_does_not_accept_escaped_opening_wikilinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "knowledge" / "concepts"
            connection = root / "knowledge" / "connections" / "alpha--beta.md"
            concepts.mkdir(parents=True)
            connection.parent.mkdir()
            for slug, title in (("alpha", "Alpha"), ("beta", "Beta")):
                (concepts / f"{slug}.md").write_text(
                    f"---\ntitle: {title}\n---\n", encoding="utf-8"
                )
            original = (
                "---\nconnects: [alpha, beta]\n---\n"
                "## Bağlantı\n\n"
                "\\[[knowledge/concepts/alpha|Alpha]]\n\n"
                "## Ana Fikir\n\nBağ.\n"
            )
            connection.write_text(original, encoding="utf-8")

            self.assertEqual(normalize_connection_links(root), 1)
            updated = connection.read_text(encoding="utf-8")

        self.assertIn("\\[[knowledge/concepts/alpha|Alpha]]", updated)
        self.assertIn(
            "[[knowledge/concepts/alpha|Alpha]] ↔ [[knowledge/concepts/beta|Beta]]",
            updated,
        )

    def test_connection_normalizer_rejects_linked_connections_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "vault"
            concepts = root / "knowledge" / "concepts"
            connections = root / "knowledge" / "connections"
            outside = base / "outside"
            concepts.mkdir(parents=True)
            outside.mkdir()
            outside_file = outside / "alpha--beta.md"
            sentinel = b"OUTSIDE\r\n"
            outside_file.write_bytes(sentinel)
            try:
                if os.name == "nt":
                    result = subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(connections), str(outside)],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode:
                        self.skipTest(
                            f"junction unavailable: {result.stdout} {result.stderr}"
                        )
                else:
                    connections.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"linked directory unavailable: {exc}")

            try:
                with self.assertRaisesRegex(
                    GraphPolicyError,
                    r"source-symlink:knowledge[\\/]connections",
                ):
                    normalize_connection_links(root)
                self.assertEqual(outside_file.read_bytes(), sentinel)
            finally:
                try:
                    if connections.is_symlink() or getattr(
                        connections, "is_junction", lambda: False
                    )():
                        connections.unlink()
                except OSError:
                    pass

    def test_connection_normalizer_rejects_literal_only_heading_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "knowledge" / "concepts"
            connections = root / "knowledge" / "connections"
            concepts.mkdir(parents=True)
            connections.mkdir(parents=True)
            for slug in ("alpha", "beta", "gamma"):
                (concepts / f"{slug}.md").write_text(
                    f"---\ntitle: {slug}\n---\n", encoding="utf-8"
                )
            valid = connections / "alpha--beta.md"
            valid_text = (
                "---\nconnects: [alpha, beta]\n---\n"
                "## Bağlantı\n\nKullanıcı.\n"
            )
            valid.write_text(valid_text, encoding="utf-8")
            invalid = connections / "beta--gamma.md"
            invalid_text = (
                "---\n"
                "title: ## Bağlantı\n"
                "connects: [beta, gamma]\n"
                "---\n"
                "~~~markdown\n## Bağlantı\n~~~\n"
            )
            invalid.write_text(invalid_text, encoding="utf-8")

            with self.assertRaisesRegex(
                GraphPolicyError, "connection-heading-missing:beta--gamma.md"
            ):
                normalize_connection_links(root)

            self.assertEqual(valid.read_text(encoding="utf-8"), valid_text)
            self.assertEqual(invalid.read_text(encoding="utf-8"), invalid_text)

    def test_graph_markdown_repairs_preserve_lf_and_crlf(self) -> None:
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    concepts = root / "knowledge" / "concepts"
                    connection = root / "knowledge" / "connections" / "alpha--beta.md"
                    concepts.mkdir(parents=True)
                    connection.parent.mkdir()
                    for slug in ("alpha", "beta"):
                        (concepts / f"{slug}.md").write_text(
                            f"---{newline}title: {slug}{newline}---{newline}",
                            encoding="utf-8",
                            newline="",
                        )
                    original = (
                        f"---{newline}connects: [alpha, beta]{newline}---{newline}"
                        f"## Bağlantı{newline}{newline}Kullanıcı.{newline}"
                    )
                    connection.write_text(original, encoding="utf-8", newline="")

                    self.assertEqual(normalize_connection_links(root), 1)
                    self.assertEqual(
                        connection.read_bytes().count(newline.encode("ascii")),
                        original.count(newline) + 2,
                    )
                    raw = connection.read_bytes()
                    if newline == "\r\n":
                        self.assertNotRegex(raw, b"(?<!\\r)\\n")
                    else:
                        self.assertNotIn(b"\r\n", raw)

    def test_connection_normalizer_repairs_noncanonical_targets_in_connects_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / 'knowledge' / 'concepts'
            connection = root / 'knowledge' / 'connections' / 'beta--alpha.md'
            concepts.mkdir(parents=True)
            connection.parent.mkdir()
            (concepts / 'alpha.md').write_text('---\ntitle: Alfa\n---\n', encoding='utf-8')
            (concepts / 'beta.md').write_text('---\ntitle: Beta\n---\n', encoding='utf-8')
            original = '''---
connects: [beta, alpha]
---
# Beta ve Alfa

## Bağlantı

[[wrong/beta|Beta]] ↔ [[../alpha|Alfa]]

Kullanıcı metni korunmalı.

## Ana Fikir

Bağ.
'''
            connection.write_text(original, encoding='utf-8')

            self.assertEqual(normalize_connection_links(root), 1)
            updated = connection.read_text(encoding='utf-8')

        self.assertIn('[[wrong/beta|Beta]] ↔ [[../alpha|Alfa]]', updated)
        self.assertIn('Kullanıcı metni korunmalı.', updated)
        self.assertIn(
            '[[knowledge/concepts/beta|Beta]] ↔ [[knowledge/concepts/alpha|Alfa]]',
            updated,
        )

    def test_connection_normalizer_rejects_missing_real_concept_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / 'knowledge' / 'concepts'
            connection = root / 'knowledge' / 'connections' / 'alpha--missing.md'
            concepts.mkdir(parents=True)
            connection.parent.mkdir()
            (concepts / 'alpha.md').write_text('---\ntitle: Alfa\n---\n', encoding='utf-8')
            connection.write_text(
                '''---
connects: [alpha, missing]
---
## Bağlantı
[[knowledge/concepts/alpha|Alfa]] ↔ [[knowledge/concepts/missing|Eksik]]
## Ana Fikir
Bağ.
''',
                encoding='utf-8',
            )

            with self.assertRaisesRegex(GraphPolicyError, 'connection-concept-missing:alpha--missing.md:missing'):
                normalize_connection_links(root)

    def test_agents_infrastructure_markdown_is_not_a_vault_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "normal-not.md").write_text("# Normal", encoding="utf-8")

            for relative in (
                Path(".agents/skills/test-skill/SKILL.md"),
                Path("nested/.agents/skills/nested-skill/SKILL.md"),
                Path(".scratch/feature/issues/01-ticket.md"),
                Path("docs/adr/0001-decision.md"),
                Path("docs/agents/issue-tracker.md"),
                Path("tasks/plan.md"),
            ):
                skill = vault / relative
                skill.parent.mkdir(parents=True)
                skill.write_text(
                    "---\nname: test-skill\ndescription: Infrastructure\n---\n# Skill",
                    encoding="utf-8",
                )

            total, isolated = graph_summary(vault_notes(vault))

        self.assertEqual(total, 1)
        self.assertEqual(isolated, ["normal-not.md"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
