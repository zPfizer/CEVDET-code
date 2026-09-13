from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from _fixtures import CODEX_DIR  # noqa: E402

import profile_guard  # noqa: E402


PROFILE_TEXT = """---
updated: 2026-09-05
---
# Levent Profili

## Oturum Portresi

Kısa ve doğal bir oturum özeti.

## Vault'ta çalışma ve yanıt tarzı

- Türkçe ve kısa yanıt ver Kaynak: [[knowledge/concepts/tercih-kisa#Kayıtlar|Kısa yanıt]] · kullanıcı tercihi · 2026-09-04.

[[#Oturum Portresi]]

## Diğer

Ek bölüm.
"""

CONCEPT_TEXT = """---
schema: knowledge-v2
title: Kısa Tercih
---
# Kısa Tercih

## Kayıtlar

- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 [[daily/2026-09-04|Kaynak]] — Türkçe ve kısa yanıt ver.

## Kaynaklar

- [[daily/2026-09-04|Kaynak]]
"""


def seed_profile(root: Path) -> Path:
    profile = root / profile_guard.PROFILE_RELATIVE
    concept = root / "knowledge" / "concepts" / "tercih-kisa.md"
    daily = root / "daily" / "2026-09-04.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    concept.parent.mkdir(parents=True, exist_ok=True)
    daily.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(PROFILE_TEXT, encoding="utf-8")
    concept.write_text(CONCEPT_TEXT, encoding="utf-8")
    daily.write_text("Kullanıcı tercihi kaydı.\n", encoding="utf-8")
    return profile


class ProfileGuardTests(unittest.TestCase):
    def test_source_free_trailing_text_and_alternative_list_markers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            additions = [
                PROFILE_TEXT.replace('· kullanıcı tercihi · 2026-09-04.',
                                     '· kullanıcı tercihi · 2026-09-04. Kaynaksız ikinci tercih.'),
                *(PROFILE_TEXT.replace('[[#Oturum Portresi]]',
                                       f'{marker} Kaynaksız ikinci tercih.\n[[#Oturum Portresi]]')
                  for marker in ('*', '+', '1.', '1)')),
            ]
            for text in additions:
                with self.subTest(text=text):
                    self.assertIn('profile-preference-format', profile_guard.check_profile(root, text))

    def test_valid_profile_has_no_diagnostics_and_portrait_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)

            self.assertEqual(profile_guard.check_profile(root), ())
            self.assertEqual(
                profile_guard.portrait(PROFILE_TEXT),
                "## Oturum Portresi\n\nKısa ve doğal bir oturum özeti.",
            )

    def test_crlf_snapshot_uses_the_same_portrait_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            crlf = PROFILE_TEXT.replace("\n", "\r\n")

            self.assertEqual(profile_guard.check_profile(root, crlf), ())
            self.assertIn("Kısa ve doğal", profile_guard.portrait(crlf))

    def test_fenced_headings_do_not_hide_or_end_the_portrait(self) -> None:
        text = PROFILE_TEXT.replace(
            "Kısa ve doğal bir oturum özeti.\n\n",
            "Kısa ve doğal bir oturum özeti.\n\n"
            "```markdown\n"
            "## Oturum Portresi\n"
            "örnek başlık\n"
            "```\n\n"
            "Portre içinde devam.\n\n",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)

            self.assertEqual(profile_guard.check_profile(root, text), ())

        self.assertEqual(
            profile_guard.portrait(text),
            "## Oturum Portresi\n\n"
            "Kısa ve doğal bir oturum özeti.\n\n"
            "```markdown\n"
            "## Oturum Portresi\n"
            "örnek başlık\n"
            "```\n\n"
            "Portre içinde devam.",
        )

    def test_fenced_structured_rows_cannot_supply_profile_provenance(self) -> None:
        fenced_concept = CONCEPT_TEXT.replace(
            "- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 "
            "[[daily/2026-09-04|Kaynak]] — Türkçe ve kısa yanıt ver.",
            '```markdown\n'
            "## Örnek kayıt\n"
            "- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 "
            "[[daily/2026-09-04|Kaynak]] — Türkçe ve kısa yanıt ver.\n"
            '```',
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            (root / "knowledge/concepts/tercih-kisa.md").write_text(
                fenced_concept,
                encoding="utf-8",
            )

            self.assertIn("profile-claim-provenance", profile_guard.check_profile(root))

    def test_empty_level_two_and_three_headings_end_style_section(self) -> None:
        for empty_heading in ("##", "###   "):
            for newline in ("\n", "\r\n"):
                with self.subTest(empty_heading=empty_heading, newline=repr(newline)), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    seed_profile(root)
                    text = PROFILE_TEXT.replace(
                        "## Vault'ta çalışma ve yanıt tarzı\n\n"
                        "- Türkçe ve kısa yanıt ver Kaynak: [[knowledge/concepts/tercih-kisa#Kayıtlar|Kısa yanıt]] · kullanıcı tercihi · 2026-09-04.\n",
                        "## Vault'ta çalışma ve yanıt tarzı\n\n"
                        f"{empty_heading}\n\n"
                        "- Türkçe ve kısa yanıt ver Kaynak: [[knowledge/concepts/tercih-kisa#Kayıtlar|Kısa yanıt]] · kullanıcı tercihi · 2026-09-04.\n",
                    ).replace("\n", newline)

                    self.assertIn("profile-preference-missing", profile_guard.check_profile(root, text))

    def test_section_masking_and_heading_discovery_agree_on_fence_indentation(self) -> None:
        preference = next(line for line in PROFILE_TEXT.splitlines() if line.startswith('- Türkçe'))
        for opening, closing in ((' ```markdown', ' ```'), ('```markdown', '```')):
            with self.subTest(opening=opening, closing=closing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                seed_profile(root)
                text = (
                    '---\nupdated: 2026-09-05\n---\n'
                    '## Oturum Portresi\n\nKısa portre.\n'
                    f'{opening}\nÖrnek.\n{closing}\n\n'
                    "## Vault'ta çalışma ve yanıt tarzı\n\n"
                    f'## Diğer\n\n{preference}\n'
                )
                self.assertIn('profile-preference-missing', profile_guard.check_profile(root, text))
                self.assertNotIn('## Diğer', profile_guard.portrait(text))
                self.assertNotIn("## Vault'ta", profile_guard.portrait(text))

    def test_frontmatter_literal_fences_cannot_hide_profile_headings(self) -> None:
        for marker in ('```', '~~~', '---\n  ```', '---\n  ~~~'):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                seed_profile(root)
                text = PROFILE_TEXT.replace(
                    'updated: 2026-09-05\n',
                    f'updated: 2026-09-05\nexample: |\n  {marker}\n  ## Oturum Portresi\n',
                )
                self.assertEqual(profile_guard.check_profile(root, text), ())
                self.assertEqual(profile_guard.portrait(text), profile_guard.portrait(PROFILE_TEXT))

    def test_real_duplicate_portrait_headings_remain_invalid(self) -> None:
        duplicate = PROFILE_TEXT.replace(
            "Kısa ve doğal bir oturum özeti.",
            "Kısa ve doğal bir oturum özeti.\n\n## Oturum Portresi\n\nİkinci gerçek başlık.",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)

            self.assertIn("profile-portrait", profile_guard.check_profile(root, duplicate))
            self.assertEqual(profile_guard.portrait(duplicate), "")

    def test_missing_and_overflowing_portrait_are_rejected(self) -> None:
        missing = PROFILE_TEXT.replace(
            "## Oturum Portresi\n\nKısa ve doğal bir oturum özeti.\n\n", ""
        )
        overflow = PROFILE_TEXT.replace("Kısa ve doğal bir oturum özeti.", "x" * 300)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            self.assertIn("profile-portrait", profile_guard.check_profile(root, missing))
            self.assertIn("profile-context-limit", profile_guard.check_profile(root, overflow))

    def test_wrong_provenance_and_future_claim_date_are_rejected(self) -> None:
        wrong_provenance = PROFILE_TEXT.replace(
            "· kullanıcı tercihi ·", "· cevo çıkarımı ·"
        )
        future_date = PROFILE_TEXT.replace("2026-09-04.", "2026-09-06.")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            self.assertIn(
                "profile-preference-format",
                profile_guard.check_profile(root, wrong_provenance),
            )
            self.assertIn("profile-claim-date", profile_guard.check_profile(root, future_date))

    def test_mismatched_and_duplicate_claims_are_rejected(self) -> None:
        mismatch = PROFILE_TEXT.replace("Türkçe ve kısa yanıt ver", "Uzun ve dolaylı yanıt ver")
        duplicate = PROFILE_TEXT.replace(
            "\n[[#Oturum Portresi]]",
            "\n- Türkçe ve kısa yanıt ver Kaynak: [[knowledge/concepts/tercih-kisa|Kısa]] · kullanıcı tercihi · 2026-09-04.\n\n[[#Oturum Portresi]]",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            self.assertIn("profile-claim-mismatch", profile_guard.check_profile(root, mismatch))
            self.assertIn("profile-claim-duplicate", profile_guard.check_profile(root, duplicate))

    def test_check_links_ignores_fenced_and_inline_example_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issues: list[str] = []

            profile_guard._check_links(
                '```text\n[[missing-example]]\n```\n'
                '`[[missing-inline]]`\n[[missing-real]]',
                root,
                issues,
            )

        self.assertEqual(issues, ['profile-link-broken'])

    def test_escaped_html_code_openers_keep_links_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issues: list[str] = []

            profile_guard._check_links(
                r'\<code>[[../secret]]</code>',
                root,
                issues,
            )

        self.assertEqual(issues, ['profile-link-traversal'])

    def test_check_links_keeps_nested_list_links_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issues: list[str] = []

            profile_guard._check_links(
                '- Ana madde\n    [[missing-nested]]',
                root,
                issues,
            )

        self.assertEqual(issues, ['profile-link-broken'])

    def test_check_links_keeps_paragraph_continuation_links_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issues: list[str] = []

            profile_guard._check_links(
                'Paragraf devam ediyor.\n    [[missing-continuation]]',
                root,
                issues,
            )

        self.assertEqual(issues, ['profile-link-broken'])

    def test_unclosed_container_fences_do_not_hide_following_links(self) -> None:
        for example in (
            '> ~~~\n> model example\n\n[[missing-after-quote-fence]]',
            '- ```\n  model example\n\n[[missing-after-list-fence]]',
            '- item\n  ```\n  model example\n\n[[missing-after-list-continuation-fence]]',
        ):
            with self.subTest(example=example), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                issues: list[str] = []

                profile_guard._check_links(example, root, issues)

            self.assertEqual(issues, ['profile-link-broken'])

    def test_unclosed_literal_html_containers_do_not_hide_following_links(self) -> None:
        for example in (
            '> <pre>\n> model example\n\n[[missing-after-quote-html]]',
            '- <pre>\n  model example\n\n[[missing-after-list-html]]',
        ):
            with self.subTest(example=example), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                issues: list[str] = []

                profile_guard._check_links(example, root, issues)

            self.assertEqual(issues, ['profile-link-broken'])

    def test_broken_and_outside_links_are_rejected_without_echoing_content(self) -> None:
        broken = PROFILE_TEXT.replace("tercih-kisa#Kayıtlar", "kayip#Kayıtlar")
        outside = PROFILE_TEXT.replace(
            "[[knowledge/concepts/tercih-kisa#Kayıtlar|Kısa yanıt]]",
            "[[../secret|Kısa yanıt]]",
        ).replace("Kısa ve doğal bir oturum özeti.", "GİZLİ-PROFIL-METNİ")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            broken_issues = profile_guard.check_profile(root, broken)
            outside_issues = profile_guard.check_profile(root, outside)
            self.assertIn("profile-link-broken", broken_issues)
            self.assertIn("profile-link-traversal", outside_issues)
            self.assertTrue(all("GİZLİ-PROFIL-METNİ" not in issue for issue in outside_issues))

    def test_missing_daily_and_unavailable_snapshot_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            (root / "daily" / "2026-09-04.md").unlink()
            self.assertIn("profile-daily-missing", profile_guard.check_profile(root))
            self.assertIn(
                "profile-unavailable",
                profile_guard.check_profile(root, read_source=lambda _path: None),
            )

    def test_read_source_callback_supplies_profile_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            seen: list[Path] = []

            def read_source(path: Path) -> str | None:
                seen.append(path)
                return path.read_text(encoding="utf-8")

            self.assertEqual(profile_guard.check_profile(root, read_source=read_source), ())
            self.assertEqual({path.name for path in seen}, {"Profile.md", "tercih-kisa.md", "2026-09-04.md"})

    def test_reparse_source_is_rejected_before_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_profile(root)
            source = root / "knowledge" / "concepts" / "tercih-kisa.md"
            target = root / "knowledge" / "concepts" / "actual.md"
            target.write_text(CONCEPT_TEXT, encoding="utf-8")
            source.unlink()
            try:
                source.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlink unavailable: {error}")
            seen: list[Path] = []

            def read_source(path: Path) -> str | None:
                seen.append(path)
                return path.read_text(encoding="utf-8")

            issues = profile_guard.check_profile(root, read_source=read_source)
            self.assertIn("profile-link-reparse", issues)
            self.assertIn("profile-source-unavailable", issues)
            self.assertNotIn(source, seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
