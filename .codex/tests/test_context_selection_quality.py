from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
import tempfile
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
            "how has the hook contract changed?": True,
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
            "my work profile 09/10/2024": False,
            "metal eskime testi": False,
            "geçmiş metal eskime testi": True,
            "past records": True,
            "before 2024": True,
            "before 2026": True,
            "before 2026-09-10": True,
            "before 31 February 2024": False,
            "before 31 February 2024 records": False,
            "compare records before 2024 with 2030 roadmap": True,
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
Hook sözleşmesi tamamlanmış uygulama kaydı; sözleşmesinde değişen kurallar burada tutulur.
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
- `gecmis` `kullanici-dusuncesi` `eski` 2026-09-01 [[daily/2026-09-01|Kaynak]] — Eski hook sözleşmesi (hook contract) geçmişte uygulanan kayıttır; sözleşmesinde eski kural vardı.
- `gecmis` `kullanici-dusuncesi` `eski` 2026-09-02 [[daily/2026-09-02|Kaynak]] — Eski hook sözleşmesi geçmişten devralınan kayıttır.
- `gecmis` `kullanici-dusuncesi` `eski` 2026-09-03 [[daily/2026-09-03|Kaynak]] — Eski hook sözleşmesi eskiden uygulanan kayıttır.
""",
            )
            entries = retrieval.build_vault_map(root, write_cache=False)
            for marker in (
                "geçmişte", "geçmişten", "geçmişe", "geçmişi", "geçmişin",
                "eskiden", "eskiye", "eskiyi", "öncekiler", "tarihi", "tarihte", "tarihten",
                "geçmişinde", "geçmişini", "geçmişimiz", "geçmişimde", "geçmişimizin",
                "tarihinde", "öncekilerin", "eskisi", "eskisini", "eskisinde", "öncekisi",
                "eskim", "eskimiz", "eskilerim", "tarihçesi", "tarihçesinde",
                "geçmişteki", "geçmiştekiler", "geçmiştekilerden", "tarihçesindekilere",
                "tarihindeki", "historic", "histories",
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
                "hook sözleşmesinde ne değişti?",
                "hook sözleşmesinde neler değişti?",
                "hook sözleşmesi nasıl değişti?",
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
