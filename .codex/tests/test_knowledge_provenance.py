from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from _fixtures import CODEX_DIR  # sys.path seam

import compile as memory_compile  # noqa: E402
import knowledge_schema  # noqa: E402


CLAIMS = """- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-01 [[daily/2026-09-01|Kaynak]] — Levent sade bir ikinci beyin istiyor.
- `gecerli` `dis-gorus` `belirsiz` 2026-09-02 [[daily/2026-09-02|Kaynak]] — Bir dış kaynak yöntemin etkili olduğunu ileri sürüyor.
- `gecerli` `dogrulanmis-bilgi` `guncel` 2026-09-03 [[daily/2026-09-03|Kaynak]] — Canlı doğrulama beklenen davranışı gösterdi.
- `gecerli` `cevo-cikarimi` `belirsiz` 2026-09-04 [[daily/2026-09-04|Kaynak]] — Bu kanıtların birlikte değerlendirilmesi riski azaltabilir."""


def _concept(claims: str = CLAIMS) -> str:
    return f"""---
schema: knowledge-v2
title: Örnek
aliases: []
tags: [doğrulama]
sources: [2026-09-01.md, 2026-09-02.md, 2026-09-03.md, 2026-09-04.md]
created: 2026-09-01
updated: 2026-09-04
---
# Örnek

Kaynaklı türev bilgi.

## Önemli Noktalar

- Bir
- İki
- Üç

## Detaylar

Detay.

## Kayıtlar

{claims}

## İlgili Kavramlar

- [[bir]] ilişkisi.
- [[iki]] ilişkisi.

## Kaynaklar

- [[daily/2026-09-01|2026-09-01 kaydı]]
- [[daily/2026-09-02|2026-09-02 kaydı]]
- [[daily/2026-09-03|2026-09-03 kaydı]]
- [[daily/2026-09-04|2026-09-04 kaydı]]
"""


def _write_tree(root: Path, concept: str = _concept()) -> Path:
    concepts = root / "knowledge" / "concepts"
    (root / "knowledge" / "connections").mkdir(parents=True)
    concepts.mkdir()
    path = concepts / "ornek.md"
    path.write_text(concept, encoding="utf-8")
    (root / "knowledge" / "index.md").write_text(
        "# Bilgi Tabanı: İndeks\n\n"
        "| Makale | Özet | Kaynak | Güncellendi |\n"
        "| --- | --- | --- | --- |\n"
        "| [[concepts/ornek\\|Örnek]] | Özet. | 2026-09-04.md | 2026-09-04 |\n",
        encoding="utf-8",
    )
    (root / "knowledge" / "log.md").write_text(
        "# Derleme Günlüğü\n",
        encoding="utf-8",
    )
    return path


class KnowledgeProvenanceTests(unittest.TestCase):
    def test_connection_target_must_exist_in_the_knowledge_tree(self) -> None:
        connection = '''---
connects: [ornek, missing]
---
# Örnek ve Eksik

## Bağlantı

[[knowledge/concepts/ornek|Örnek]] ↔ [[knowledge/concepts/missing|Eksik]]

## Ana Fikir

Bağ.
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_tree(root)
            path = root / 'knowledge' / 'connections' / 'ornek--missing.md'
            path.write_text(connection, encoding='utf-8')

            issues = knowledge_schema.validate_knowledge_tree(root).issues

        self.assertIn(
            'knowledge/connections/ornek--missing.md:concept-targets', issues
        )

    def test_nested_stage_output_is_rejected_before_live_promotion(self) -> None:
        from test_second_brain_acceptance import _seed_vault

        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            source = vault / "daily/2026-09-04.md"
            digest = memory_compile._sha256(source)

            def runner(_prompt: str, stage: Path) -> None:
                nested = stage / "knowledge/concepts/nested"
                nested.mkdir()
                (nested / "note.md").write_text(
                    "# Model output\n",
                    encoding="utf-8",
                )

            reason, detail = memory_compile._compile_one(
                vault,
                vault / ".codex/scripts/.state",
                source,
                digest,
                "2026-09-07T12:00:00+03:00",
                runner=runner,
            )

        self.assertEqual(reason, "policy")
        self.assertIn("forbidden-directory:knowledge/concepts/nested", detail)
        self.assertFalse((vault / "knowledge/concepts/nested").exists())

    def test_inline_backtick_code_line_does_not_hide_following_headings(self) -> None:
        text = "```örnek```\n" + "\n".join(knowledge_schema.CONCEPT_HEADINGS)

        headings = knowledge_schema.markdown_headings(text)

        self.assertEqual(
            [(level, title) for level, title, _start, _end in headings],
            [("##", heading.removeprefix("## ")) for heading in knowledge_schema.CONCEPT_HEADINGS],
        )

        tilde_text = "~~~python `örnek`\n## Gizli\n~~~\n## Görünür\n"
        self.assertEqual(
            [(level, title) for level, title, _start, _end in knowledge_schema.markdown_headings(tilde_text)],
            [("##", "Görünür")],
        )

    def test_heading_schema_ignores_fenced_headings_and_inline_tokens(self) -> None:
        fenced = "```markdown\n" + "\n".join(knowledge_schema.CONCEPT_HEADINGS) + "\n```\n"
        self.assertFalse(knowledge_schema._ordered(fenced, knowledge_schema.CONCEPT_HEADINGS))

        text = _concept().replace(
            "Detay.\n",
            "Metin içinde ## İlgili Kavramlar ifadesi.\n````markdown\n"
            "## İlgili Kavramlar\n- [[sahte]]\n````\nDetay.\n",
        )
        details = knowledge_schema._section(
            text,
            knowledge_schema.CONCEPT_HEADINGS[1],
            knowledge_schema.CONCEPT_HEADINGS[2],
        )

        self.assertIn("Metin içinde ## İlgili Kavramlar ifadesi.", details)
        self.assertIn("[[sahte]]", details)

    def test_connection_footer_is_repaired_but_extra_sources_are_not_silently_removed(self):
        from test_second_brain_acceptance import _write_derived_tree
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_derived_tree(root, latest=True)
            path = root / 'knowledge/connections/kaynakli-sentez--yerel-hafiza.md'
            text = path.read_text(encoding='utf-8')
            source = knowledge_schema.parse_frontmatter(text)['sources'][-1].removesuffix('.md')
            text = text.replace(f'- [[daily/{source}|Kaynak]]\n', '')
            path.write_text(text, encoding='utf-8')
            memory_compile._normalize_and_validate_stage(root, CODEX_DIR / 'tag-taxonomy.json')
            self.assertIn(f'[[daily/{source}|Kaynak]]', path.read_text(encoding='utf-8'))
            path.write_text(path.read_text(encoding='utf-8') + '\n- [[daily/2026-09-05|Kaynak]]\n', encoding='utf-8')
            with self.assertRaisesRegex(memory_compile.PolicyError, 'source-links'):
                memory_compile._normalize_and_validate_stage(root, CODEX_DIR / 'tag-taxonomy.json')
            self.assertIn('[[daily/2026-09-05|Kaynak]]', path.read_text(encoding='utf-8'))

    def test_missing_footer_links_are_filled_from_declared_sources_without_new_claims(self) -> None:
        original = _concept().replace('- [[daily/2026-09-04|2026-09-04 kaydı]]\n', '')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_tree(root, original)
            memory_compile._normalize_and_validate_stage(root, CODEX_DIR / 'tag-taxonomy.json')
            once = path.read_text(encoding='utf-8')
            self.assertIn('[[daily/2026-09-04|Kaynak]]', once)
            self.assertIn(CLAIMS, once)
            self.assertIn('[[daily/2026-09-01|2026-09-01 kaydı]]', once)
            memory_compile._normalize_and_validate_stage(root, CODEX_DIR / 'tag-taxonomy.json')
            self.assertEqual(path.read_text(encoding='utf-8'), once)

    def test_compile_stage_orders_claims_without_changing_their_contents(self) -> None:
        original = _concept('\n'.join(reversed(CLAIMS.splitlines())))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_tree(root, original)
            before = memory_compile._manifest(root)
            path.write_text(original + '\nYeni ayrıntı.\n', encoding='utf-8')
            memory_compile._normalize_and_validate_stage(
                root, CODEX_DIR / 'tag-taxonomy.json', before,
                {'knowledge/concepts/ornek.md': original})
            once = path.read_text(encoding='utf-8')
            self.assertEqual(once, _concept() + '\nYeni ayrıntı.\n')
            memory_compile._normalize_and_validate_stage(
                root, CODEX_DIR / 'tag-taxonomy.json', before,
                {'knowledge/concepts/ornek.md': original})
            self.assertEqual(path.read_text(encoding='utf-8'), once)

    def test_claim_order_normalization_preserves_mixed_line_endings_and_blanks(self) -> None:
        rows = CLAIMS.splitlines()
        rows[1] = rows[1].replace('Bir dış', 'Bir  dış')
        original = _concept(
            rows[3] + '\r\n\r\n' + rows[2] + '\u2028' + rows[1] + '\r\n' + rows[0]
        )

        normalized = knowledge_schema.normalize_claim_order(original)

        expected = rows[0] + '\r\n\r\n' + rows[1] + '\u2028' + rows[2] + '\r\n' + rows[3] + '\n'
        self.assertIn(expected, normalized)
        original_claims, original_malformed = knowledge_schema._claims(original)
        normalized_claims, normalized_malformed = knowledge_schema._claims(normalized)
        self.assertFalse(original_malformed or normalized_malformed)
        self.assertEqual(len(normalized_claims), len(original_claims))
        self.assertCountEqual(
            [knowledge_schema._claim_identity(claim) for claim in normalized_claims],
            [knowledge_schema._claim_identity(claim) for claim in original_claims],
        )
        self.assertCountEqual(
            [claim.raw_line for claim in normalized_claims],
            [claim.raw_line for claim in original_claims],
        )

        eof = '---\nschema: knowledge-v2\n---\n## Kayıtlar\n' + '\n'.join(reversed(rows))
        normalized_eof = knowledge_schema.normalize_claim_order(eof)
        eof_claims, eof_malformed = knowledge_schema._claims(normalized_eof)
        self.assertFalse(eof_malformed)
        self.assertEqual(len(eof_claims), len(rows))
        self.assertFalse(normalized_eof.endswith('\n'))

    def test_changed_concept_accepts_four_claim_kinds_with_freshness_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_tree(root)
            from test_second_brain_acceptance import _decision_proof
            proof, identity = _decision_proof('2026-09-01', 'Levent sade bir ikinci beyin istiyor.')
            day = root / 'daily/2026-09-01.md'
            day.parent.mkdir(parents=True)
            day.write_text(proof, encoding='utf-8')
            path = root / 'knowledge/concepts/ornek.md'
            path.write_text(path.read_text(encoding='utf-8').replace(
                '[[daily/2026-09-01|Kaynak]] —', f'[[daily/2026-09-01#user-{identity}|Kaynak]] —'), encoding='utf-8')

            report = knowledge_schema.validate_knowledge_tree(
                root,
                changed_paths=("knowledge/concepts/ornek.md",),
            )

        self.assertEqual(report.issues, ())

    def test_claim_order_normalization_keeps_invalid_and_legacy_data_visible(self) -> None:
        for body, issue in (
            (_concept(CLAIMS + '\nnot a valid claim'), 'knowledge/concepts/ornek.md:claims'),
            (_concept(CLAIMS + '\n' + CLAIMS.splitlines()[0]), 'knowledge/concepts/ornek.md:claim-duplicate'),
        ):
            with self.subTest(issue=issue), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                normalized = knowledge_schema.normalize_claim_order(body)
                _write_tree(root, normalized)
                issues = knowledge_schema.validate_knowledge_tree(
                    root, changed_paths=('knowledge/concepts/ornek.md',)).issues
                self.assertIn(issue, issues)
                self.assertEqual(sorted(body.splitlines()), sorted(normalized.splitlines()))
        legacy = _concept('\n'.join(reversed(CLAIMS.splitlines()))).replace('schema: knowledge-v2\n', '')
        self.assertEqual(knowledge_schema.normalize_claim_order(legacy), legacy)

    def test_changed_concept_rejects_normalized_duplicate_and_unstable_order(self) -> None:
        claims = """- `gecerli` `dogrulanmis-bilgi` `guncel` 2026-09-03 [[daily/2026-09-03|Kaynak]] — Aynı  İddia.
- `gecerli` `dis-gorus` `belirsiz` 2026-09-02 [[daily/2026-09-02|Kaynak]] — Önce gelmeliydi.
- `gecerli` `dogrulanmis-bilgi` `guncel` 2026-09-04 [[daily/2026-09-04|Kaynak]] — aynı iddia."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_tree(root, _concept(claims))

            issues = knowledge_schema.validate_knowledge_tree(
                root,
                changed_paths=("knowledge/concepts/ornek.md",),
            ).issues

        self.assertIn("knowledge/concepts/ornek.md:claim-duplicate", issues)
        self.assertIn("knowledge/concepts/ornek.md:claim-order", issues)

    def test_changed_concept_preserves_old_claim_when_new_information_conflicts(self) -> None:
        old_claim = (
            "- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-01 "
            "[[daily/2026-09-01|Kaynak]] — Tercih A'dır."
        )
        history = (
            "- `gecmis` `kullanici-dusuncesi` `eski` 2026-09-01 "
            "[[daily/2026-09-01|Kaynak]] — Tercih A'dır.\n"
            "- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 "
            "[[daily/2026-09-04|Kaynak]] — Tercih B'dir."
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_tree(root, _concept(history))
            relative = "knowledge/concepts/ornek.md"

            preserved = knowledge_schema.validate_knowledge_tree(
                root,
                changed_paths=(relative,),
                previous_texts={relative: _concept(old_claim)},
            ).issues
            path.write_text(_concept(history.splitlines()[-1]), encoding="utf-8")
            lost = knowledge_schema.validate_knowledge_tree(
                root,
                changed_paths=(relative,),
                previous_texts={relative: _concept(old_claim)},
            ).issues

        self.assertNotIn("knowledge/concepts/ornek.md:claim-history", preserved)
        self.assertIn("knowledge/concepts/ornek.md:claim-history", lost)

    def test_changed_connection_requires_canonical_pair_and_daily_source_links(self) -> None:
        connection = """---
schema: knowledge-v2
connects: [ornek, ikinci]
sources: [2026-09-04.md]
updated: 2026-09-04
---
# Örnek ve İkinci

## Bağlantı

[[knowledge/concepts/ornek|Örnek]] ↔ [[knowledge/concepts/ikinci|İkinci]]

## Ana Fikir

Bağ.

## Kaynaklar

- 2026-09-04.md
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_tree(root)
            path = root / "knowledge" / "connections" / "ornek--ikinci.md"
            path.write_text(connection, encoding="utf-8")

            issues = knowledge_schema.validate_knowledge_tree(
                root,
                changed_paths=("knowledge/connections/ornek--ikinci.md",),
                previous_texts={
                    "knowledge/connections/ornek--ikinci.md": connection.replace(
                        "sources: [2026-09-04.md]",
                        "sources: [2026-09-03.md, 2026-09-04.md]",
                    )
                },
            ).issues

        self.assertNotIn("knowledge/connections/ornek--ikinci.md:connection-order", issues)
        self.assertIn("knowledge/connections/ornek--ikinci.md:source-links", issues)
        self.assertIn("knowledge/connections/ornek--ikinci.md:source-history", issues)

    def test_compile_prompt_declares_publish_time_provenance_contract(self) -> None:
        prompt = memory_compile.build_compile_prompt(
            "# Index",
            "2026-09-04.md",
            "Kalıcı karar",
            "2026-09-04T12:00:00+03:00",
            ["hafıza"],
        )

        self.assertIn("kullanici-dusuncesi", prompt)
        self.assertIn("dogrulanmis-bilgi", prompt)
        self.assertIn("`belirsiz`", prompt)
        self.assertIn("[[daily/2026-09-04|Kaynak]]", prompt)
        self.assertIn("eski kaydı koru", prompt)
        self.assertIn("knowledge/concepts/*.md", prompt)
        self.assertNotIn("knowledge/concepts/**/*.md", prompt)
        repair_prompt = memory_compile.build_schema_repair_prompt(
            "knowledge-schema:knowledge/concepts/ornek.md:derived-schema"
        )
        self.assertIn("knowledge/connections/*.md", repair_prompt)
        self.assertNotIn("knowledge/connections/**/*.md", repair_prompt)

    def test_compile_stage_keeps_legacy_notes_valid_until_they_change(self) -> None:
        legacy = _concept().replace("schema: knowledge-v2\n", "")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_tree(root, legacy)
            before = memory_compile._manifest(root)
            relative = "knowledge/concepts/ornek.md"
            previous = {relative: legacy}

            memory_compile._normalize_and_validate_stage(
                root,
                CODEX_DIR / "tag-taxonomy.json",
                before,
                previous,
            )
            path.write_text(legacy + "\nYeni ayrıntı.\n", encoding="utf-8")

            with self.assertRaisesRegex(
                memory_compile.PolicyError,
                "knowledge/concepts/ornek.md:derived-schema",
            ):
                memory_compile._normalize_and_validate_stage(
                    root,
                    CODEX_DIR / "tag-taxonomy.json",
                    before,
                    previous,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
