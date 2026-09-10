from __future__ import annotations

from datetime import date
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vault_retrieval as retrieval


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ContextSelectionQualityTests(unittest.TestCase):
    def test_wayfinding_queries_keep_sources_in_top_three(self) -> None:
        # Synthetic sources: no exported Vault notes, identifiers, or quotations.
        cases = (
            ('🧠 500-Knowledge/n001.md',
             '---\ntitle: Merkez Eğilim\naliases: [Ortanca göstergesi]\n---\n'
             'Medyan tabanlı gösterge uç değer etkisini azaltır.\n',
             (('Ortanca göstergesi ne öneriyordu?', 'ortanca göstergesi'),
              ('Medyan uç değer etkisi', 'medyan değer'))),
            ('🧠 500-Knowledge/n002.md',
             '# Oynaklık Tahmini\nKoşullu varyans modeli risk bütçesini ayarlamayı tartışır.\n',
             (('Koşullu varyans risk bütçesi', 'varyans risk'),
              ('Oynaklık tahmini ne işe yarar?', 'oynaklık tahmini'))),
            ('📥 000-Inbox/n003.md',
             '# Değerleme Görseli\n![[example.png]]\n'
             'Görsel açıklaması: indirgenmiş nakit akışı ağırlıkları A ve B senaryolarını birleştirir.\n',
             (('Nakit akışı ağırlıkları görseli', 'nakit akışı'),
              ('Değerleme görseli', 'değerleme görseli'))),
            ('knowledge/concepts/n004.md',
             '---\ntitle: Gerçekleşmeyen Adaylar\n'
             'aliases: [Nonfill, İşleme dönüşmeyen adaylar]\n---\n'
             'Nonfill, adayın işleme dönüşmemesidir. '
             'Kazanma oranı ile aday sayısı ayrı raporlanır.\n',
             (('İşleme dönüşmeyen adaylar', 'işleme adaylar'),
              ('Nonfill adaylar', 'nonfill adaylar'),
              ('Kazanma oranı aday sayısı', 'kazanma oranı'))),
            ('knowledge/concepts/n005.md',
             '---\ntitle: Cynefin\naliases: [Karmaşıklık çerçevesi]\n---\n'
             'Karmaşık durumda küçük deneylerle öğrenme yaklaşımı.\n',
             (('Karmaşıklık çerçevesi', 'karmaşıklık çerçevesi'),
              ('Cynefin küçük deneyler', 'cynefin küçük'))),
            ('📦 900-Archive/n006.md',
             '---\ntitle: Sınıflandırma Denetimi\nstatus: archived\n---\n'
             'Eski etiket denetimi sınıflandırma kararlarının geçmiş gerekçelerini korur.\n',
             (('Eski etiket denetimi', 'eski etiket'),
              ('Sınıflandırma Denetimi', 'sınıflandırma denetimi'),
              ('Geçmiş sınıflandırma gerekçeleri', 'geçmiş sınıflandırma'))),
            ('🧠 500-Knowledge/n007.md',
             '# Uzun Deney Kaydı\n' + ('Hazırlık aşamasında genel gözlemler tutuldu.\n\n' * 200)
             + '## Teknik Ek\nMor pusula protokolü örneklem dışı doğrulamada sabit veri aralığı kullanır.\n',
             (('Mor pusula protokolü', 'mor pusula'),
              ('Örneklem dışı sabit veri aralığı', 'örneklem sabit'),
              ('Uzun Deney Kaydı teknik ek', 'deney teknik'))),
            ('🏰 300-Projects/Atlas.md',
             '# Atlas\nKarar: yerel önbellek. Gerekçe: çevrimdışı kullanım. '
             'Açık soru: veri güncelliği nasıl korunacak?\n',
             (('Atlas için ne seçtik?', 'atlas seçtik'),
              ('Atlas kararının gerekçesi', 'atlas kararının'),
              ('Atlas çevrimdışı kullanım', 'atlas kullanım'))),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path, text, _queries in cases:
                _write(root, path, text)
            for path, source_text, queries in cases:
                for query, competing_terms in queries:
                    with self.subTest(query=query):
                        # Comparable-length body matches keep this a ranking test,
                        # rather than a competition with keyword-only stubs.
                        for index in range(4):
                            status = '---\nstatus: archived\n---\n' if 'status: archived' in source_text else ''
                            _write(root, f'🧠 500-Knowledge/distractor-{index}.md',
                                   status + f'# Yan Araştırma {index}\n' + competing_terms + '\n'
                                   + ('Ayrı konunun genel açıklaması ve çalışma ayrıntıları.\n'
                                      * max(1, len(source_text) // 50)))
                        entries = retrieval.build_vault_map(root, write_cache=False)
                        hits = retrieval.search_vault(entries, query, top_k=20)
                        self.assertGreaterEqual(len(hits), 4, 'Ranking requires eligible competitors')
                        top_hits = retrieval.search_vault(entries, query)
                        self.assertIn(path, [hit.entry.path for hit in top_hits])

    def test_vault_system_priority_keeps_the_fully_named_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root, '🧠 500-Knowledge/atlas.md',
                   '# Atlas Kaynak Seçimi\nKarar: yerel dosya; gerekçe: çevrimdışı kullanım.\n')
            for index in range(3):
                _write(root, f'🧠 500-Knowledge/general-{index}.md',
                       f'# Genel not {index}\nVault sisteminde bağlantılar.\n')
            hits = retrieval.search_vault(retrieval.build_vault_map(root, write_cache=False),
                'Vault sisteminde Atlas Kaynak Seçimi için hangi kararı aldık?')
            self.assertEqual(hits[0].entry.title, 'Atlas Kaynak Seçimi')

    def test_explicit_vault_system_topic_outranks_incidental_conversation_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root, '🧠 500-Knowledge/vault.md',
                   '# Hafıza düzeni\nVault sisteminde kaynaklı kararları koruyoruz.\n')
            _write(root, '🏰 300-Projects/Tansu/noise.md',
                   '# Momentum\nSisteminde yani senin yaşam alanında başka eksik gördüğün '
                   'neler var hangi kararları aldık.\n')
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query in ('vault sisteminde yani senin yaşam alanında başka eksik gördüğün neler var',
                          'Vault sisteminde hangi kararları aldık?'):
                with self.subTest(query=query):
                    hits = retrieval.search_vault(entries, query)
                    self.assertEqual(hits[0].entry.path, '🧠 500-Knowledge/vault.md')
                    self.assertFalse(retrieval.is_topicless_followup(query))

    def test_named_note_survives_conversational_recall_without_answer_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root, '🏰 300-Projects/Atlas.md',
                   '# Atlas\nKarar: yerel önbellek.\nGerekçe: çevrimdışı devamlılık.\n')
            _write(root, '🧠 500-Knowledge/noise.md',
                   '# Seyahat\nAtlas taşıyarak gezdik.\n')
            _write(root, '🎯 100-Command-Center/old.md',
                   '---\ntype: work-packet\nstatus: completed\n---\n# Atlas\nEski görev.\n')
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query in ('Atlas için ne seçtik ve gerekçesi neydi?',
                          'Atlas kararını hatırlat', 'Atlas hakkında ne biliyoruz?'):
                with self.subTest(query=query):
                    hits = retrieval.search_vault(entries, query)
                    self.assertEqual([hit.entry.path for hit in hits], ['🏰 300-Projects/Atlas.md'])
                    self.assertIn('yerel önbellek', hits[0].excerpt)

    def test_followup_words_do_not_retrieve_unrelated_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root, '🧠 500-Knowledge/noise.md',
                   '# Başka konu\nBunları yapmayı düşünüyorsun; çözmeyi planlıyorsun.\n')
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query in ('bunları nasıl çözebilirsin', 'nasıl çözmeyi planlıyorsun',
                          'nasıl implemantasyon yapmayı düşünüyosun', 'yap bakalım o zaman'):
                with self.subTest(query=query):
                    self.assertEqual(retrieval.search_vault(entries, query), [])
                    result = retrieval.retrieve_vault_context_detailed(root, query, write_cache=False)
                    self.assertEqual(result.paths, ())
            # Retrieval eligibility must not suppress conversation capture.
            self.assertTrue(retrieval.is_meaningful_query('bunları nasıl çözebilirsin'))

    def test_followup_filter_preserves_named_topics_and_short_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root, '🧠 500-Knowledge/noise.md', '# Başka konu\nBunları yapmayı planlıyorsun.\n')
            for topic in ('Pakmaya', 'BIB', 'Tansu', 'Zaman'):
                _write(root, f'🧠 500-Knowledge/{topic}.md', f'# {topic}\nÖzgün konu kaydı.\n')
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query, topic in (('Pakmaya?', 'Pakmaya'), ('BIB', 'BIB'),
                                 ('Bunları BIB için yapmayı planlıyorsun', 'BIB'),
                                 ('Peki Tansu?', 'Tansu'), ('Zaman', 'Zaman')):
                with self.subTest(query=query):
                    self.assertEqual([hit.entry.title for hit in retrieval.search_vault(entries, query)], [topic])

    def test_query_only_english_function_words_do_not_outvote_named_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(20):
                _write(
                    root,
                    f'🧠 500-Knowledge/noise-{index}.md',
                    '# Genel not\nWhat is this decision about?\n',
                )
            _write(root, '🧠 500-Knowledge/apollo.md', '# Apollo kararı\nTürkçe karar kaydı.\n')
            query = 'What is this decision about Apollo?'
            hits = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                query,
            )

        self.assertEqual(hits[0].entry.title, 'Apollo kararı')
        self.assertIn('decision', retrieval._retrieval_terms(query))
        self.assertTrue(retrieval.is_meaningful_query(query))

    def test_explicit_uppercase_acronym_survives_query_only_filter(self) -> None:
        self.assertEqual(retrieval._retrieval_terms('THE IS'), frozenset({'is'}))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root, '🧠 500-Knowledge/is.md', '# IS\nÖzgün acronym kararı.\n')
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query in ('IS', 'IS hakkında ne biliyoruz?'):
                with self.subTest(query=query):
                    hits = retrieval.search_vault(entries, query)
                    self.assertEqual([hit.entry.title for hit in hits], ['IS'])

    def test_out_of_route_growth_does_not_invert_rare_and_common_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root, '🏰 300-Projects/Tansu X Veri Havuzu/a.md', '---\ntitle: alfa\n---\nbeta\n')
            _write(root, '🏰 300-Projects/Tansu X Veri Havuzu/b.md', '---\ntitle: beta\n---\nalfa\n')
            for index in range(100):
                _write(root, f'🧠 500-Knowledge/bib/{index}.md', f'# Kaynak {index}\nalfa\n')
            hits = retrieval.search_vault(retrieval.build_vault_map(root, write_cache=False), 'alfa beta', route='TANSU')
            self.assertEqual(Path(hits[0].entry.path).name, 'b.md')

    def test_completed_operational_record_requires_history_or_explicit_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = '🎯 100-Command-Center/old.md'
            _write(root, old, '---\ntitle: Hook Contract Conformance\ntype: work-packet\nstatus: completed\n---\n# Hook Contract Conformance\nhook sözleşmesi eski davranış.\n')
            _write(root, '🧠 500-Knowledge/new.md', '# Güncel hook sözleşmesi\nhook sözleşmesi güncel davranış.\n')
            entries = retrieval.build_vault_map(root, write_cache=False)
            self.assertNotIn(old, [hit.entry.path for hit in retrieval.search_vault(entries, 'hook sözleşmesi')])
            self.assertEqual(retrieval.search_vault(entries, 'Hook Contract Conformance')[0].entry.path, old)
    def test_html_comments_are_not_indexed_as_note_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "🧠 500-Knowledge/comments.md",
                "# Görünür Not\n\n<!-- private-marker -->\nvisible kanıt.\n",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)

            hidden = retrieval.search_vault(entries, "private-marker")
            visible = retrieval.search_vault(entries, "visible kanıt")

        self.assertEqual(hidden, [])
        self.assertEqual(visible[0].entry.path, "🧠 500-Knowledge/comments.md")

    def test_exact_short_acknowledgements_skip_retrieval_but_topic_questions_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "🧠 500-Knowledge/plan.md",
                "# Plan\n\nyapalım planı için bağımsız kanıt.\n",
            )

            skipped = retrieval.retrieve_vault_context_detailed(
                root,
                "Yapalım",
                write_cache=False,
            )
            searched = retrieval.retrieve_vault_context_detailed(
                root,
                "Yapalım planı",
                write_cache=False,
            )

        self.assertEqual(skipped.outcome, "skipped")
        self.assertEqual(searched.paths, ("🧠 500-Knowledge/plan.md",))

    def test_personal_queries_prefer_canonical_profile_without_promoting_it_for_finance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "🔮 850-Companion/Profile.md",
                """---
title: Levent Profili
updated: 2026-09-05
type: memory
status: active
tags: [hafıza]
---
# Levent Profili

## Oturum Portresi

Kısa ve doğal bir oturum özeti.

## Vault'ta çalışma ve yanıt tarzı

- Türkçe ve kısa yanıt ver Kaynak: [[knowledge/concepts/tercih-kisa#Kayıtlar|Kısa yanıt]] · kullanıcı tercihi · 2026-09-04.

[[#Oturum Portresi]]

## Aktif alanlar

Çalışma tercihleri ve yanıt tarzı: Türkçe, kısa, doğrudan ve kopyalanabilir.
Finansal kontrol ve yatırım araştırması aktif alanlardır.
""",
            )
            _write(
                root,
                "knowledge/concepts/tercih-kisa.md",
                """---
schema: knowledge-v2
title: Kısa Tercih
---
# Kısa Tercih

## Kayıtlar

- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 [[daily/2026-09-04|Kaynak]] — Türkçe ve kısa yanıt ver.

## Kaynaklar

- [[daily/2026-09-04|Kaynak]]
""",
            )
            _write(root, "daily/2026-09-04.md", "Kullanıcı tercihi kaydı.\n")
            _write(
                root,
                "🧠 500-Knowledge/Levent AI Çalışma Profili Baseline.md",
                """---
title: Levent AI Çalışma Profili Baseline
type: baseline-analysis
status: historical
---
# Levent AI Çalışma Profili Baseline

Çalışma tercihleri ve yanıt tarzı Türkçe, kısa, doğrudan ve kopyalanabilir.
Çalışma tercihleri ve yanıt tarzı tekrar edilen baseline analizidir.
""",
            )
            _write(
                root,
                "🧠 500-Knowledge/finansal-model.md",
                """---
title: Finansal Model Kanıt Eşiği
type: note
status: active
---
# Finansal Model Kanıt Eşiği

Finansal model kanıt eşiği, bağımsız test ve risk ölçümü ister.
""",
            )

            personal = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "çalışma tercihleri ve yanıt tarzı",
            )
            finance = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "finansal model kanıt eşiği",
            )
            personal_finance = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "kişisel finansal model kanıt eşiği",
            )
            named_finance = retrieval.search_vault(retrieval.build_vault_map(root, write_cache=False),
                                                    'Levent finansal model kanıt eşiği')
            dated = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "28 Ağustos 2026 Levent çalışma profili",
            )

        self.assertEqual(personal[0].entry.path, "🔮 850-Companion/Profile.md")
        self.assertEqual(finance[0].entry.path, "🧠 500-Knowledge/finansal-model.md")
        self.assertEqual(personal_finance[0].entry.path, "🧠 500-Knowledge/finansal-model.md")
        self.assertEqual(named_finance[0].entry.path, "🧠 500-Knowledge/finansal-model.md")
        self.assertEqual(
            dated[0].entry.path,
            "🧠 500-Knowledge/Levent AI Çalışma Profili Baseline.md",
        )

    def test_default_context_uses_current_uncertain_claim_and_history_query_keeps_old_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "knowledge/concepts/tercih.md",
                """---
schema: knowledge-v2
title: Çalışma Tercihi
aliases: []
tags: [hafıza]
sources: [2026-09-01.md, 2026-09-06.md]
created: 2026-09-01
updated: 2026-09-06
---
# Çalışma Tercihi

## Detaylar

Legacy çalışma tercihi değişim kaydını tekrar eder.

## Kayıtlar

- `gecmis` `kullanici-dusuncesi` `eski` 2026-09-01 [[daily/2026-09-01|Kaynak]] — Eski çalışma tercihi değişim kaydıdır.
- `gecerli` `kullanici-dusuncesi` `belirsiz` 2026-09-06 [[daily/2026-09-06|Kaynak]] — Güncel çalışma tercihi belirsizdir.
""",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            current = retrieval.search_vault(entries, "çalışma tercihi")
            history = retrieval.search_vault(entries, "geçmiş değişim çalışma tercihi")

        self.assertEqual(len(current), 1)
        self.assertIn("Güncel çalışma tercihi", current[0].excerpt)
        self.assertNotIn("Eski çalışma tercihi", current[0].excerpt)
        self.assertIn("Eski çalışma tercihi", history[0].excerpt)
        self.assertIn("gecmis", history[0].excerpt)

    def test_explicit_history_query_does_not_penalize_related_archived_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "🏰 300-Projects/Tansu/Python-eski.md",
                """---
title: Eski Python Denetim Kaydı
status: archived
type: research-analysis
---
# Eski Python Denetim Kaydı
6 Eylül 7 Eylül eski etiket denetimi; tarihsel araştırma bulguları.
""",
            )
            _write(
                root,
                "🏰 300-Projects/Tansu/Python-guncel.md",
                """---
title: Güncel Python Protokolleri
status: active
type: note
---
# Güncel Python Protokolleri
Eylül etiket denetimi için güncel protokol. Eylül etiket denetimi.
Eylül etiket denetimi.
""",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            history = retrieval.search_vault(
                entries,
                "6 Eylül 7 Eylül eski etiket denetimi",
                top_k=2,
            )
            current = retrieval.search_vault(
                entries,
                "Eylül etiket denetimi",
                top_k=2,
            )

        self.assertEqual(history[0].entry.path, "🏰 300-Projects/Tansu/Python-eski.md")
        self.assertEqual(current[0].entry.path, "🏰 300-Projects/Tansu/Python-guncel.md")

    def test_current_mixed_query_prefers_active_duplicate_and_keeps_history_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_body = "# Hook Deployment Checklist\nHook deployment checklist current release.\n"
            for name, status in (("a-archived", "archived"), ("z-active", "active")):
                _write(
                    root,
                    f"{name}.md",
                    f"---\ntitle: Hook Deployment Checklist\nstatus: {status}\ntype: note\n---\n"
                    + current_body,
                )
            for name in ("y-active", "x-active"):
                _write(
                    root,
                    f"{name}.md",
                    f"---\ntitle: Hook Deployment Checklist\nstatus: active\ntype: note\n---\n"
                    + current_body.replace("current release", f"current {name} release"),
                )
            _write(
                root,
                "b-archived.md",
                "---\ntitle: Hook Deployment Checklist\nstatus: archived\ntype: note\n---\n"
                "# Hook Deployment Checklist\nHook deployment checklist stale release.\n",
            )
            history = "history.md"
            _write(
                root,
                history,
                "---\ntitle: Hook Deployment History\nstatus: historical\ntype: research-analysis\n---\n"
                "# Hook Deployment History\nHook history records prior deployment checklist.\n",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query in (
                "compare hook history with current deployment checklist",
                "current deployment checklist compare hook history",
                "current deployment checklist; historical hook records",
                "current deployment checklist, historical hook records",
            ):
                with self.subTest(query=query):
                    paths = [
                        hit.entry.path
                        for hit in retrieval.search_vault(entries, query, top_k=3)
                    ]
                    self.assertIn("z-active.md", paths)
                    self.assertIn(history, paths)
                    self.assertNotIn("a-archived.md", paths)
                    self.assertNotIn("b-archived.md", paths)
                    self.assertLess(paths.index("z-active.md"), paths.index(history))

    def test_explicit_selection_exclusions_filter_current_and_history_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, status in (
                ("active.md", "active"),
                ("historical.md", "historical"),
                ("archived.md", "archived"),
                ("superseded.md", "superseded-v1"),
            ):
                _write(
                    root,
                    name,
                    f"---\ntitle: Hook Records\nstatus: {status}\ntype: note\n---\n"
                    "# Hook Records\nHook records historical evidence.\n",
                )
            entries = retrieval.build_vault_map(root, write_cache=False)
            cases = {
                "show current hook records, not historical records": {"active.md"},
                "show current hook records and exclude historical records": {"active.md"},
                "show current hook records and exclude only historical records": {"active.md"},
                "historical records, not current records": {
                    "historical.md", "archived.md", "superseded.md",
                },
                "historical records and exclude current records": {
                    "historical.md", "archived.md", "superseded.md",
                },
            }
            for query, expected in cases.items():
                with self.subTest(query=query):
                    exclusion = retrieval._split_selection_exclusion(query)
                    self.assertIsNotNone(exclusion)
                    paths = {
                        hit.entry.path
                        for hit in retrieval.search_vault(entries, query, top_k=5)
                    }
                    self.assertEqual(paths, expected)

            not_only = "show current hook records and not only historical records"
            self.assertIsNone(retrieval._split_selection_exclusion(not_only))
            self.assertEqual(
                {hit.entry.path for hit in retrieval.search_vault(entries, not_only, top_k=5)},
                {"active.md", "historical.md", "archived.md", "superseded.md"},
            )
            long_query = "show current hook records " + ("and topic " * 2000) + "and not historical records"
            started = time.perf_counter()
            self.assertIsNotNone(retrieval._split_selection_exclusion(long_query))
            self.assertLess(time.perf_counter() - started, 5.0)

    def test_current_history_scope_ignores_inner_connectors(self) -> None:
        cases = {
            "compare current checklist with past design and history notes": (
                "compare current checklist",
                "past design and history notes",
            ),
            "compare current design and implementation with hook history": (
                "compare current design and implementation",
                "hook history",
            ),
            "current checklist before deployment with hook history": (
                "current checklist before deployment",
                "hook history",
            ),
            "current checklist with records before and after the 2024 migration history": (
                "current checklist",
                "records before and after the 2024 migration history",
            ),
            "compare current design and 2030 implementation with hook history": (
                "compare current design and 2030 implementation",
                "hook history",
            ),
            "compare current checklist to hook history": (
                "compare current checklist",
                "hook history",
            ),
            "compare current work profile against historical work profile": (
                "compare current work profile",
                "historical work profile",
            ),
            "compare historical and current work profiles": (
                "current work profiles",
                "compare historical work profiles",
            ),
            "compare the current and historical work profiles": (
                "compare the current work profiles",
                "historical work profiles",
            ),
            "compare the historical and the current work profiles": (
                "the current work profiles",
                "compare the historical work profiles",
            ),
            "compare historical and current IS": (
                "current IS",
                "compare historical IS",
            ),
            "compare the historical and current WorkProfile": (
                "current WorkProfile",
                "compare the historical WorkProfile",
            ),
            "compare the current and historical WorkProfile": (
                "compare the current WorkProfile",
                "historical WorkProfile",
            ),
            "current deployment checklist with records September 10, 2030 history": (
                "current deployment checklist",
                "records September 10, 2030 history",
            ),
            "güncel checklist ile geçmiş kayıtlar": (
                "güncel checklist",
                "geçmiş kayıtlar",
            ),
            "latest checklist with hook history": (
                "latest checklist",
                "hook history",
            ),
            "active checklist with hook history": (
                "active checklist",
                "hook history",
            ),
            "compare current IS with historical IS": (
                "compare current IS",
                "historical IS",
            ),
            "compare current work profile with my work profile 2024": (
                "compare current work profile",
                "my work profile 2024",
            ),
            "compare current and historical work profiles": (
                "compare current work profiles",
                "historical work profiles",
            ),
            "show current and historical work profiles": (
                "show current work profiles",
                "historical work profiles",
            ),
            "compare historical and current": None,
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(retrieval._split_current_history_query(query), expected)

    def test_shared_topic_anchor_recovers_current_profile_without_current_body_term(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "profile-active.md",
                "---\ntitle: Work Profiles\nstatus: active\ntype: note\n---\n"
                "# Work Profiles\nWork profiles tercihleri.\n",
            )
            _write(
                root,
                "profile-history.md",
                "---\ntitle: Work Profiles Analysis\nstatus: historical\n"
                "type: research-analysis\n---\n# Work Profiles Analysis\n"
                "Work profiles önceki tercihleri.\n",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query in (
                "compare current and historical work profiles",
                "compare historical and current work profiles",
                "compare the current and historical work profiles",
                "compare the historical and the current work profiles",
            ):
                with self.subTest(query=query):
                    hits = retrieval.search_vault(entries, query, top_k=3)

                    paths = [hit.entry.path for hit in hits]
                    self.assertIn("profile-active.md", paths)
                    self.assertIn("profile-history.md", paths)
                    self.assertLess(paths.index("profile-active.md"), paths.index("profile-history.md"))

    def test_current_qualifier_keeps_history_object_unsplit_and_unpenalized(self) -> None:
        object_queries = (
            "show the history of the current hook contract",
            "show the previous version of the current hook contract",
        )
        qualified_object_queries = object_queries + (
            "güncel hook sözleşmesinin geçmişi",
            "güncel hook sözleşmesinin geçmişini göster",
            "güncel modülün geçmişi",
            "güncel sürümünün geçmişi",
        )
        for query in qualified_object_queries:
            with self.subTest(query=query):
                self.assertIsNone(retrieval._split_current_history_query(query))
                self.assertFalse(retrieval._has_independent_current_cue(query))
        self.assertTrue(retrieval._has_independent_current_cue("current work profile history"))
        self.assertTrue(retrieval._has_independent_current_cue("güncel work profile history"))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "hook-current.md",
                "---\ntitle: Hook Contract\nstatus: active\ntype: note\n---\n"
                "# Hook Contract\nCurrent hook contract rules for deployment.\n",
            )
            _write(
                root,
                "hook-history.md",
                "---\ntitle: Hook Contract History\nstatus: historical\n"
                "type: research-analysis\n---\n# Hook Contract History\n"
                "Hook contract history records and previous version from completed changes.\n",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query in object_queries:
                with self.subTest(query=query):
                    hits = retrieval.search_vault(entries, query, top_k=2)
                    self.assertEqual(hits[0].entry.path, "hook-history.md")

    def test_mixed_scope_preserves_uppercase_acronym_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "is-active.md",
                "---\ntitle: IS\nstatus: active\ntype: note\n---\n# IS\nIS current protocol.\n",
            )
            _write(
                root,
                "is-history.md",
                "---\ntitle: Historical IS\nstatus: historical\ntype: research-analysis\n---\n"
                "# Historical IS\nIS historical protocol.\n",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            hits = retrieval.search_vault(
                entries,
                "compare current IS with historical IS",
                top_k=3,
            )

        paths = [hit.entry.path for hit in hits]
        self.assertIn("is-active.md", paths)
        self.assertIn("is-history.md", paths)

    def test_against_scope_keeps_canonical_profile_with_historical_analyses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "profile-active.md",
                "---\ntitle: Work Profile\nstatus: active\ntype: note\n---\n"
                "# Work Profile\nWork profile tercihleri.\n",
            )
            for index in range(3):
                _write(
                    root,
                    f"profile-history-{index}.md",
                    "---\n"
                    f"title: Historical Work Profile Analysis {index}\n"
                    "status: historical\n"
                    "type: research-analysis\n"
                    "---\n"
                    f"# Historical Work Profile Analysis {index}\n"
                    f"Historical work profile analysis {index} bulguları.\n",
                )
            entries = retrieval.build_vault_map(root, write_cache=False)
            hits = retrieval.search_vault(
                entries,
                "compare current work profile against historical work profile",
                top_k=3,
            )

        paths = [hit.entry.path for hit in hits]
        self.assertIn("profile-active.md", paths)
        self.assertGreaterEqual(
            sum(path.startswith("profile-history-") for path in paths),
            2,
        )

    def test_unsplit_mixed_personal_query_keeps_canonical_profile_ahead_of_analyses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                retrieval.PROFILE_RELATIVE,
                "---\ntitle: Levent Profili\nupdated: 2026-09-05\ntype: memory\nstatus: active\n"
                "tags: [hafıza]\n---\n# Levent Profili\n\n"
                "## Oturum Portresi\n\nWork profile özeti.\n\n"
                "## Vault'ta çalışma ve yanıt tarzı\n\n"
                "- Türkçe ve kısa yanıt ver Kaynak: [[knowledge/concepts/tercih-kisa#Kayıtlar|Kısa yanıt]] · kullanıcı tercihi · 2026-09-04.\n",
            )
            _write(
                root,
                "knowledge/concepts/tercih-kisa.md",
                "---\nschema: knowledge-v2\ntitle: Kısa Tercih\n---\n# Kısa Tercih\n\n"
                "## Kayıtlar\n\n"
                "- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 [[daily/2026-09-04|Kaynak]] — Türkçe ve kısa yanıt ver.\n",
            )
            _write(root, "daily/2026-09-04.md", "Kullanıcı tercihi kaydı.\n")
            for index in range(3):
                _write(
                    root,
                    f"analysis-{index}.md",
                    "---\n"
                    f"title: Historical Work Profile Analysis {index}\n"
                    "status: historical\n"
                    "type: research-analysis\n"
                    "---\n"
                    f"# Historical Work Profile Analysis {index}\n"
                    f"Historical work profile history analysis {index}.\n",
                )
            entries = retrieval.build_vault_map(root, write_cache=False)
            hits = retrieval.search_vault(entries, "current work profile history", top_k=3)

        self.assertEqual(hits[0].entry.path, retrieval.PROFILE_RELATIVE)

    def test_identifier_scan_skips_long_unterminated_hash_run(self) -> None:
        scripts_root = str(Path(__file__).resolve().parents[1] / "scripts")
        for suffix in ("", "x"):
            script = (
                f"import sys\n"
                f"sys.path.insert(0, {scripts_root!r})\n"
                f"import vault_retrieval\n"
                f"assert vault_retrieval._date_references('past records ' + ('# ' * 100000) + {suffix!r}) == ()\n"
            )
            started = time.perf_counter()
            subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                timeout=5,
            )
            self.assertLess(time.perf_counter() - started, 5.0)

    def test_ordinary_history_prefix_match_keeps_active_record_ahead_of_archived_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "🏰 300-Projects/Tansu/a-archived.md",
                """---
title: Değişken Seçimi Eski
status: archived
type: note
---
# Değişken Seçimi Eski
Değişken seçimi için ortak karar ve kanıt.
""",
            )
            _write(
                root,
                "🏰 300-Projects/Tansu/z-active.md",
                """---
title: Değişken Seçimi Güncel
status: active
type: note
---
# Değişken Seçimi Güncel
Değişken seçimi için ortak karar ve kanıt.
""",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            hits = retrieval.search_vault(entries, "değişken seçimi", top_k=2)

        for query in ("değişken seçimi", "eskiz seçimi", "pastane seçimi"):
            with self.subTest(query=query):
                self.assertFalse(
                    retrieval._is_history_query(retrieval._retrieval_terms(query))
                )
        self.assertEqual(
            [hit.entry.path for hit in hits],
            [
                "🏰 300-Projects/Tansu/z-active.md",
                "🏰 300-Projects/Tansu/a-archived.md",
            ],
        )

    def test_comparison_terms_keep_active_record_ahead_of_archived_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "🏰 300-Projects/Tansu/a-archived.md",
                """---
title: Python Rust Performans Eski
status: archived
type: note
---
# Python Rust Performans Eski
Python Rust performance için ortak benchmark kararı.
""",
            )
            _write(
                root,
                "🏰 300-Projects/Tansu/z-active.md",
                """---
title: Python Rust Performans Güncel
status: active
type: note
---
# Python Rust Performans Güncel
Python Rust performance için ortak benchmark kararı.
""",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            for marker in ("compare", "change", "karşılaştır", "değişim"):
                with self.subTest(marker=marker):
                    query = f"{marker} Python Rust performance"
                    self.assertFalse(
                        retrieval._is_history_query(retrieval._retrieval_terms(query))
                    )
                    hits = retrieval.search_vault(entries, query, top_k=2)
                    self.assertEqual(
                        [hit.entry.path for hit in hits],
                        [
                            "🏰 300-Projects/Tansu/z-active.md",
                            "🏰 300-Projects/Tansu/a-archived.md",
                        ],
                    )

    def test_prospective_before_query_keeps_active_record_ahead_of_archived_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "🏰 300-Projects/Tansu/a-archived.md",
                """---
title: Hook Checklist Eski
status: archived
type: note
---
# Hook Checklist Eski
Changed files için deployment hook checklist ortak çalışma kaydı.
""",
            )
            _write(
                root,
                "🏰 300-Projects/Tansu/z-active.md",
                """---
title: Hook Checklist Güncel
status: active
type: note
---
# Hook Checklist Güncel
Changed files için deployment hook checklist ortak çalışma kaydı.
""",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            for query in (
                "before deployment show hook checklist",
                "before 9999 deployment show hook checklist",
                "how to lint changed files",
                "show changed files",
                "what changed files should we deploy?",
                "changed",
            ):
                with self.subTest(query=query):
                    self.assertFalse(
                        retrieval._is_history_query(retrieval._retrieval_terms(query), query)
                    )
                    self.assertEqual(
                        [hit.entry.path for hit in retrieval.search_vault(entries, query, top_k=2)],
                        [
                            "🏰 300-Projects/Tansu/z-active.md",
                            "🏰 300-Projects/Tansu/a-archived.md",
                        ],
                    )

    def test_history_markers_require_historical_context_and_past_dates(self) -> None:
        cases = {
            "what changed files should we deploy?": False,
            "son değişiklik": True,
            "son değişim": True,
            "son değişiklikler": True,
            "son değişiklikleri": True,
            "son değişimleri": True,
            "son değişikliği": True,
            "önceki değişiklikleri göster": True,
            "değişiklik planı": False,
            "değişim dosyası": False,
            "dosyanın tarihi nedir?": False,
            "dosyanın tarih nedir?": False,
            "dosyanın tarihini göster": False,
            "tarihi hook sözleşmesi": False,
            "tarihte hook sözleşmesi": False,
            "tarihten hook sözleşmesi": False,
            "tarihinde hook sözleşmesi": False,
            "tarihim hook sözleşmesi": False,
            "tarihindeki hook sözleşmesi": False,
            "doğum tarihim nedir?": False,
            "doğum tarihimiz nedir?": False,
            "tarihçe kayıtlarını göster": True,
            "geçmiş tarihi hook sözleşmesi": True,
            "hook contract changed yesterday": True,
            "recent changes to the hook contract": True,
            "hook sözleşmesi dün değişti": True,
            "geçen hafta hook sözleşmesi değişti": True,
            "what changed yesterday in the hook contract?": True,
            "what changed last week in the hook contract?": True,
            "how has the hook contract changed?": True,
            "how did the hook contract change?": True,
            "why did the hook contract change?": True,
            "previous page, show current hook checklist": False,
            "previous records": True,
            "previous versions": True,
            "show previous release of hook": True,
            "show previous releases of hook": True,
            "show previous revision of hook": True,
            "show previous revisions of hook": True,
            "previous decisions": True,
            "önceki sayfa, güncel hook checklist göster": False,
            "önceki kayıtlar": True,
            "geçmişte önceki sayfa": True,
            "before December 2026": False,
            "before December 2026 decisions": False,
            "before": False,
            "get past the hook failure": False,
            "past": False,
            "my work profile 2030 plan": False,
            "my work profile September 2026": False,
            "my work profile 2026 plan": False,
            "my work profile 09/30/2024": True,
            "my work profile 09/09/2024": True,
            "my work profile 09/10/2024": True,
            "my work profile 09/10/2026": False,
            "metal eskime testi": False,
            "geçmiş metal eskime testi": True,
            "past records": True,
            "past records for ticket #2030": True,
            "past records for port 8080": True,
            "past records for issue #2030": True,
            "past records for bug 2030": True,
            "past records for issue 2030-09-10": True,
            "past hook records, 2030 roadmap": True,
            "my work profile issue 2024": False,
            "my work profile RFC 2024": False,
            "my work profile issue 2024-09-10": False,
            "show work that is past due": False,
            "show projects past deadline": False,
            "show projects past their deadline": False,
            "show work past its deadline": False,
            "past due projects": False,
            "past due previous versions": True,
            "past hook records and 2030 roadmap": True,
            "past 2024 records and 2030 roadmap": True,
            "past 2030 records": False,
            "past records for 2030": False,
            "past records September 10, 2030": False,
            "past research and development projects": True,
            "önceki kayıtları göster": True,
            "önceki kaydı göster": True,
            "önceki sürümü göster": True,
            "past performance hook": True,
            "past experience hook": True,
            "past work hook": True,
            "past results hook": True,
            "past projects hook": True,
            "before 2024": True,
            "before 2026": True,
            "before 2026-09-10": True,
            "before yesterday, show hook records": True,
            "before today": True,
            "before last year": True,
            "before last month": True,
            "before last week": True,
            "before today, compare with 2030 roadmap": True,
            "before 31 February 2024": False,
            "before 31 February 2024 records": False,
            "before tomorrow, review records": False,
            "before next Monday": False,
            "before today's meeting, show records": False,
            "before yesterday's meeting, show records": True,
            "before deployment, show past records": True,
            "before tomorrow's meeting, show records": False,
            "when was the hook contract changed?": True,
            "why was the hook changed?": True,
            "when were the hook contracts changed?": True,
            "has the hook contract changed?": True,
            "was the hook contract changed?": True,
            "were the hook contracts changed?": True,
            "did the hook contract change?": True,
            "when did the hook contract change?": True,
            "hook sözleşmesi neden değişti?": True,
            "records from before the year 2024": True,
            "compare records before 2024 with 2030 roadmap": True,
            "compare hook versions before and after the 2024 migration": True,
            "compare hook versions before and after the 2030 migration": False,
            "28 Ağustos 2024 Levent çalışma profili": True,
        }
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 9, 10)

        with mock.patch.object(retrieval, "date", FixedDate):
            for query, expected in cases.items():
                with self.subTest(query=query):
                    terms = retrieval._retrieval_terms(query)
                    self.assertEqual(retrieval._is_history_query(terms, query), expected)

    def test_inflected_history_terms_retrieve_completed_and_historical_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = "🎯 100-Command-Center/hook.md"
            concept = "knowledge/concepts/hook.md"
            _write(
                root,
                packet,
                """---
title: Hook Contract Conformance
type: work-packet
status: completed
---
# Hook Contract Conformance
Hook sözleşmesi tamamlanmış uygulama kaydı; sözleşmesindeki son değişiklikleri gösteren paket; sözleşmesinde değişen kurallar burada tutulur.
""",
            )
            _write(
                root,
                concept,
                """---
schema: knowledge-v2
title: Hook Protokolü
---
# Hook Protokolü

## Kayıtlar
- `gecmis` `kullanici-dusuncesi` `eski` 2026-09-01 [[daily/2026-09-01|Kaynak]] — Eski hook sözleşmesi (hook contract) geçmişte uygulanan kayıttır; sözleşmesindeki son değişiklikleri gösteren eski kural sözleşmesinde tutulur.
- `gecmis` `kullanici-dusuncesi` `eski` 2026-09-02 [[daily/2026-09-02|Kaynak]] — Eski hook sözleşmesi geçmişten devralınan kayıttır.
- `gecmis` `kullanici-dusuncesi` `eski` 2026-09-03 [[daily/2026-09-03|Kaynak]] — Eski hook sözleşmesi eskiden uygulanan kayıttır.
""",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            for marker in (
                "geçmişte", "geçmişten", "geçmişe", "geçmişi", "geçmişin",
                "eskiden", "eskiye", "eskiyi", "öncekiler",
                "geçmişinde", "geçmişini", "geçmişimiz", "geçmişimde", "geçmişimizin",
                "öncekilerin", "eskisi", "eskisini", "eskisinde", "öncekisi",
                "eskim", "eskimiz", "eskilerim", "tarihçesi", "tarihçesinde",
                "geçmişteki", "geçmiştekiler", "geçmiştekilerden", "tarihçesindekilere",
                "historic", "histories",
                "previously", "historically", "before 2024",
            ):
                with self.subTest(marker=marker):
                    query = f"{marker} hook sözleşmesi"
                    self.assertTrue(
                        retrieval._is_history_query(retrieval._retrieval_terms(query))
                    )
                    hits = retrieval.search_vault(entries, query, top_k=3)
                    paths = [hit.entry.path for hit in hits]
                    self.assertIn(packet, paths)
                    historical = next(hit for hit in hits if hit.entry.path == concept)
                    self.assertIn("Eski hook sözleşmesi", historical.excerpt)
            for query in (
                "what changed in the hook contract?",
                "how has the hook contract changed?",
                "how did the hook contract change?",
                "why did the hook contract change?",
                "hook sözleşmesinde ne değişti?",
                "hook sözleşmesinde neler değişti?",
                "hook sözleşmesi nasıl değişti?",
                "hook sözleşmesindeki son değişiklikleri göster",
            ):
                with self.subTest(query=query):
                    terms = retrieval._retrieval_terms(query)
                    self.assertTrue(retrieval._is_history_query(terms, query))
                    hits = retrieval.search_vault(entries, query, top_k=3)
                    paths = [hit.entry.path for hit in hits]
                    self.assertIn(packet, paths)
                    historical = next(hit for hit in hits if hit.entry.path == concept)
                    self.assertIn("Eski hook sözleşmesi", historical.excerpt)

    def test_identical_copies_do_not_fill_top_three_when_an_independent_source_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = "# Ortak Kaynak\n\nbenzersiz sorgu kanıtı.\n"
            for name in ("copy-a.md", "copy-b.md", "copy-c.md"):
                _write(root, f"🧠 500-Knowledge/{name}", duplicate)
            _write(
                root,
                "🧠 500-Knowledge/independent.md",
                "# Bağımsız Kaynak\n\nbenzersiz sorgu kanıtı; ayrı gözlem.\n",
            )
            hits = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "benzersiz sorgu kanıtı",
                top_k=3,
            )

        paths = {hit.entry.path for hit in hits}
        self.assertIn("🧠 500-Knowledge/independent.md", paths)
        self.assertLessEqual(
            sum(path.endswith(("copy-a.md", "copy-b.md", "copy-c.md")) for path in paths),
            1,
        )

    def test_history_mode_prefers_historical_representative_and_ties(self) -> None:
        for history_status in ("historical", "archived", "superseded"):
            for active_name, history_name in (
                ("a-active.md", "z-history.md"),
                ("a-history.md", "z-active.md"),
            ):
                with self.subTest(
                    history_status=history_status,
                    active_name=active_name,
                    history_name=history_name,
                ):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        body = "# Hook Contract\nHook contract history records.\n"
                        _write(
                            root,
                            active_name,
                            "---\ntitle: Hook Contract\nstatus: active\ntype: note\n---\n" + body,
                        )
                        _write(
                            root,
                            history_name,
                            f"---\ntitle: Hook Contract\nstatus: {history_status}\n"
                            "type: note\n---\n" + body,
                        )
                        entries = retrieval.build_vault_map(root, write_cache=False)
                        history_queries = (
                            "show the history of the current hook contract",
                            "show the previous version of the current hook contract",
                        )
                        history = [
                            retrieval.search_vault(entries, query, top_k=2)
                            for query in history_queries
                        ]
                        current = retrieval.search_vault(entries, "current hook contract", top_k=2)
                        default = retrieval.search_vault(entries, "hook contract", top_k=2)

                    for hits in history:
                        self.assertEqual(hits[0].entry.path, history_name)
                    self.assertEqual(current[0].entry.path, active_name)
                    self.assertEqual(default[0].entry.path, active_name)

    def test_history_tie_keeps_a_relevant_record_in_top_three(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(3):
                _write(
                    root,
                    f"active-{index}.md",
                    "---\ntitle: Hook Records\nstatus: active\ntype: note\n---\n"
                    f"# Hook Records\nHook records evidence marker {index}.\n",
                )
            _write(
                root,
                "history-record.md",
                "---\ntitle: Hook Records\nstatus: historical\n"
                "type: research-analysis\n---\n# Hook Records\n"
                "Hook records evidence marker archive.\n",
            )
            hits = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "past hook records",
                top_k=3,
            )

        paths = [hit.entry.path for hit in hits]
        self.assertIn("history-record.md", paths)
        self.assertEqual(sum(path.startswith("active-") for path in paths), 2)

    def test_history_mode_scans_past_limit_for_same_content_representative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clone_body = "# Hook Records\nHook records evidence marker clone.\n"
            _write(
                root,
                "a-active-clone.md",
                "---\ntitle: Past Hook Records\nstatus: active\ntype: note\n---\n" + clone_body,
            )
            for index in (1, 2):
                _write(
                    root,
                    f"b-active-{index}.md",
                    "---\ntitle: Past Hook Records\nstatus: active\ntype: note\n---\n"
                    f"# Hook Records\nHook records evidence marker other-{index}.\n",
                )
            _write(
                root,
                "z-history-clone.md",
                "---\ntitle: Hook Records Archive\nstatus: historical\ntype: note\n---\n" + clone_body,
            )
            hits = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "past hook records",
                top_k=3,
            )

        paths = [hit.entry.path for hit in hits]
        self.assertIn("z-history-clone.md", paths)
        self.assertNotIn("a-active-clone.md", paths)

    def test_ambiguous_history_feature_keeps_active_representative(self) -> None:
        for query in (
            "show browser history retention policy",
            "show browser history retention policy of Chrome",
            "show history settings",
        ):
            with self.subTest(query=query):
                self.assertTrue(
                    retrieval._is_ambiguous_history_feature_query(
                        query,
                        retrieval._retrieval_terms(query),
                    )
                )
        for query in ("show hook history records", "show deployment history records"):
            with self.subTest(query=query):
                self.assertFalse(
                    retrieval._is_ambiguous_history_feature_query(
                        query,
                        retrieval._retrieval_terms(query),
                    )
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = "# Browser History Retention Policy\nBrowser history retention policy settings.\n"
            _write(
                root,
                "active.md",
                "---\ntitle: Browser History Retention Policy\nstatus: active\ntype: note\n---\n" + body,
            )
            _write(
                root,
                "archived.md",
                "---\ntitle: Browser History Retention Policy\nstatus: archived\ntype: note\n---\n" + body,
            )
            _write(
                root,
                "privacy.md",
                "---\ntitle: Privacy Settings\nstatus: active\ntype: note\n---\n"
                "# Privacy Settings\nCurrent privacy settings.\n",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            feature = retrieval.search_vault(
                entries,
                "show browser history retention policy",
                top_k=2,
            )
            retro = retrieval.search_vault(
                entries,
                "show the history of the current browser retention policy",
                top_k=2,
            )
            mixed = retrieval.search_vault(
                entries,
                "compare current privacy settings and browser history retention policy",
                top_k=3,
            )

        self.assertEqual(feature[0].entry.path, "active.md")
        self.assertEqual(retro[0].entry.path, "archived.md")
        mixed_paths = [hit.entry.path for hit in mixed]
        self.assertIn("privacy.md", mixed_paths)
        self.assertIn("active.md", mixed_paths)
        self.assertNotIn("archived.md", mixed_paths)

    def test_selection_exclusion_projects_knowledge_v2_claim_rows(self) -> None:
        historical_claim = (
            "- `gecmis` `kullanici-dusuncesi` `eski` 2024-01-01 "
            "[[daily/2024-01-01|Kaynak]] — Historical hook records evidence."
        )
        current_claim = (
            "- `gecerli` `kullanici-dusuncesi` `guncel` 2024-02-01 "
            "[[daily/2024-02-01|Kaynak]] — Current hook records evidence."
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "mixed.md",
                "---\nschema: knowledge-v2\ntitle: Hook Records\nstatus: active\n---\n"
                f"# Hook Records\n\n## Kayıtlar\n{historical_claim}\n{current_claim}\n",
            )
            _write(
                root,
                "current-only.md",
                "---\nschema: knowledge-v2\ntitle: Hook Records\nstatus: active\n---\n"
                f"# Hook Records\n\n## Kayıtlar\n{current_claim}\n",
            )
            _write(
                root,
                "archived.md",
                "---\nschema: knowledge-v2\ntitle: Hook Records\nstatus: archived\n---\n"
                f"# Hook Records\n\n## Kayıtlar\n{current_claim}\n",
            )
            policy_historical_claim = (
                "- `gecmis` `kullanici-dusuncesi` `eski` 2024-01-01 "
                "[[daily/2024-01-01|Kaynak]] — Browser history retention policy old value."
            )
            policy_current_claim = (
                "- `gecerli` `kullanici-dusuncesi` `guncel` 2024-02-01 "
                "[[daily/2024-02-01|Kaynak]] — Retention is now disabled."
            )
            _write(
                root,
                "policy.md",
                "---\nschema: knowledge-v2\ntitle: Browser History Retention Policy\n"
                "status: active\n---\n# Browser History Retention Policy\n\n## Kayıtlar\n"
                f"{policy_historical_claim}\n{policy_current_claim}\n",
            )
            _write(
                root,
                "privacy.md",
                "---\ntitle: Privacy Settings\nstatus: active\ntype: note\n---\n"
                "# Privacy Settings\nCurrent privacy settings.\n",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            history = retrieval.search_vault(
                entries,
                "historical hook records, not current records",
                top_k=5,
            )
            current = retrieval.search_vault(
                entries,
                "current hook records, not historical records",
                top_k=5,
            )

        self.assertEqual(
            {hit.entry.path for hit in history},
            {"mixed.md", "archived.md"},
        )
        self.assertIn("Historical hook records evidence", history[0].excerpt)
        self.assertNotIn("Current hook records evidence", history[0].excerpt)
        archived_history = next(hit for hit in history if hit.entry.path == "archived.md")
        self.assertIn("Current hook records evidence", archived_history.excerpt)
        self.assertEqual(
            {hit.entry.path for hit in current},
            {"mixed.md", "current-only.md"},
        )
        mixed_current = next(hit for hit in current if hit.entry.path == "mixed.md")
        self.assertIn("Current hook records evidence", mixed_current.excerpt)
        self.assertNotIn("Historical hook records evidence", mixed_current.excerpt)
        history_without_marker = retrieval.search_vault(
            entries,
            "hook records, not current records",
            top_k=5,
        )
        self.assertEqual(
            {hit.entry.path for hit in history_without_marker},
            {"mixed.md", "archived.md"},
        )
        self.assertIn(
            "Historical hook records evidence",
            next(hit for hit in history_without_marker if hit.entry.path == "mixed.md").excerpt,
        )

        feature = retrieval.search_vault(
            entries,
            "compare current privacy settings and browser history retention policy",
            top_k=3,
        )
        policy_feature = next(hit for hit in feature if hit.entry.path == "policy.md")
        self.assertIn("Retention is now disabled", policy_feature.excerpt)
        self.assertNotIn("old value", policy_feature.excerpt)
        policy_history = retrieval.search_vault(
            entries,
            "show the history of the current browser history retention policy",
            top_k=3,
        )
        policy_history_hit = next(hit for hit in policy_history if hit.entry.path == "policy.md")
        self.assertIn("old value", policy_history_hit.excerpt)

    def test_unrelated_corpus_growth_does_not_change_target_selection_or_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root,
                "🧠 500-Knowledge/target.md",
                "# Hedef\n\nözel sinyal doğrulama kanıtı.\n",
            )
            _write(
                root,
                "🧠 500-Knowledge/distractor.md",
                "# Dikkat\n\ngenel açıklama ve başka kanıt.\n",
            )
            before = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "özel sinyal doğrulama",
            )
            for index in range(100):
                _write(
                    root,
                    f"🧠 500-Knowledge/unrelated-{index}.md",
                    f"# Alakasız {index}\n\nbaşka konu {index}.\n",
                )
            after = retrieval.search_vault(
                retrieval.build_vault_map(root, write_cache=False),
                "özel sinyal doğrulama",
            )

        self.assertEqual(before[0].entry.path, "🧠 500-Knowledge/target.md")
        self.assertEqual(after[0].entry.path, before[0].entry.path)
        self.assertEqual(after[0].excerpt, before[0].excerpt)
        # Corpus IDF can change; the selected source and its meaning must not.


if __name__ == "__main__":
    unittest.main(verbosity=2)
