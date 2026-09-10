from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # sys.path seam

import hook  # noqa: E402
import memory_ledger  # noqa: E402
import vault_retrieval  # noqa: E402


class MemoryDirectiveTests(unittest.TestCase):
    def test_long_write_intent_whitespace_is_bounded(self) -> None:
        script = (
            "import sys; sys.path.insert(0, '.codex/scripts'); "
            "import memory_ledger as ledger; "
            "assert not ledger.is_explicit_write_intent('Lütfen' + ' ' * 12000 + '?'); "
            "assert not ledger.is_explicit_write_intent('src/app.py dosyasını düzelt' + ' ' * 12000 + '?'); "
            "assert ledger.is_explicit_write_intent('src/app.py dosyasını düzelt, ' + ' ' * 12000 + 'lütfen.')"
        )
        subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script],
            cwd=CODEX_DIR.parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_quoted_controls_are_content_but_outer_controls_still_apply(self) -> None:
        ordinary = [
            'Makalede “bu konuşmada kalsın” yazıyor. Bunu değerlendir.',
            'Yazar "bunu kaydetme" diyor; bu görüşü araştır.',
            'Yazar "bunu kaydetme, lütfen" diyor; bu görüşü araştır.',
            'Yazar "bunu kaydetme! Lütfen." diyor; bu görüşü araştır.',
            'Alıntı: `Şunu unut: Ankara`',
            '> Bunu unut.\nBu cümleyi açıkla.',
            '```text\nBu konuşmada kalsın.\n```\nMetni özetle.',
            "Yazar 'bunu kaydetme' demiş.",
            "Yazar 'Ankara'da bunu kaydetme' demiş; görüşünü yorumla.",
            'Metinde «bu konuşmada kalsın» deniyor.',
            '    Bunu unut.\nBu kod örneğini açıkla.',
        ]
        for text in ordinary:
            with self.subTest(text=text):
                self.assertEqual(memory_ledger.memory_directive(text).kind, 'ordinary')
                self.assertEqual(memory_ledger.persistent_turns([('user', text)]), [('user', text)])
        target = 'Şunu unut: "Levent Ankara’da yaşıyor"'
        self.assertEqual(memory_ledger.memory_directive(target).kind, 'forget')
        for quoted_target in (
            '"Geçici bilgi". Bunu kaydetme.',
            "'Geçici bilgi'. Bunu kaydetme.",
            '```text\nGeçici bilgi\n```\nBunu kaydetme.',
        ):
            with self.subTest(quoted_target=quoted_target):
                self.assertEqual(memory_ledger.memory_directive(quoted_target).kind, 'do-not-save')
                self.assertEqual(memory_ledger.memory_directive(quoted_target).target, quoted_target)
                self.assertEqual(
                    memory_ledger.persistent_turns([
                        ('user', 'Eski karar.'),
                        ('assistant', 'Eski yanıt.'),
                        ('user', quoted_target),
                    ]),
                    [('user', 'Eski karar.'), ('assistant', 'Eski yanıt.')],
                )

    def test_natural_memory_controls_are_deterministic(self) -> None:
        cases = {
            "Tercihimi B olarak düzelt.": ("correct", ""),
            "Bunu unut.": ("forget-ambiguous", ""),
            "Şunu unut: Levent Ankara'da yaşıyor.": (
                "forget",
                "Levent Ankara'da yaşıyor",
            ),
            "Bunu kaydetme.": ("do-not-save", ""),
            "Bunu kaydetme, lütfen.": ("do-not-save", ""),
            "Lütfen, bunu kaydetme!": ("do-not-save", ""),
            "Bunu kaydetme! Lütfen.": ("do-not-save", ""),
            "Bunu kaydetme? Lütfen…": ("do-not-save", ""),
            "Bunu kaydetme - lütfen.": ("do-not-save", ""),
            "Bunu kaydetme—lütfen.": ("do-not-save", ""),
            "—Bunu kaydetme.": ("do-not-save", ""),
            "Bunu kaydetme, lütfen bunu ayrıca açıkla.": (
                "do-not-save",
                "Bunu kaydetme, lütfen bunu ayrıca açıkla.",
            ),
            "Bunu kaydetme! Lütfen bunu ayrıca açıkla.": (
                "do-not-save",
                "Bunu kaydetme! Lütfen bunu ayrıca açıkla.",
            ),
            "Bu konuşmada kalsın.": ("session-only", ""),
            "Bu sohbet aramızda kalsın.": ("session-only", ""),
            "Bu oturumda kalsın.": ("session-only", ""),
            "Aramızda kalsın.": ('session-only', ''),
            "Şehir bilgisini hafızandan çıkar.": ('forget-ambiguous', 'Şehir bilgisini hafızandan çıkar.'),
            "Bu bilgiyi hatırlamanı istemiyorum.": ('forget-ambiguous', 'Bu bilgiyi hatırlamanı istemiyorum.'),
            "Bunu hafızana alma.": ("do-not-save", ""),
            "Bu bilgiyi saklama.": ("do-not-save", ""),
            "Bunu kaydetmeni istemiyorum.": ("do-not-save", ""),
            "Şunu unut: “Levent Ankara'da yaşıyor.”": ("forget", "Levent Ankara'da yaşıyor"),
            "Şunu unut: 'Ankara'da yaşıyorum'": ('forget', "Ankara'da yaşıyorum"),
            "Şunu unut: «Ankara'da yaşıyorum»": ('forget', "Ankara'da yaşıyorum"),
            "Benim hakkımda ne biliyorsun?": ("what-known", ""),
            "Unutulmuş bağlantı riskini incele.": ("ordinary", ""),
            "Bu bir salt okunur denetim, hiçbir dosyayı değiştirme.": ("read-only", ""),
            "Read-only audit please, don't touch files.": ("read-only", ""),
            "Salt okunur incele. Sorun nerede?": ("read-only", ""),
            "Do not modify files or settings. What is wrong?": ("read-only", ""),
            "Dosyaları değiştirme, hata nerede?": ("read-only", ""),
            "Salt-okunur modda incele.": ("read-only", ""),
            "Sadece incele; dosyaları değiştirme.": ("read-only", ""),
            "Do not modify files or settings.": ("read-only", ""),
            "Dosya değiştirme kuralı nedir?": ("ordinary", ""),
            "devam": ("ordinary", ""),
            "Nasıl düzeltilir?": ("ordinary", ""),
            "Düzeltme nasıl yapılır?": ("ordinary", ""),
            "Dosyaları değiştirme kuralı nedir?": ("ordinary", ""),
            "Ok yap.": ("write-intent", ""),
            "Uygula.": ("write-intent", ""),
            "Gerekli değişiklikleri yap.": ("write-intent", ""),
            "Önerdiğin değişiklikleri uygula.": ("write-intent", ""),
            "Sırayla hepsini yap": ("write-intent", ""),
            "Sırayla yap": ("write-intent", ""),
            "Sırayla uygula.": ("write-intent", ""),
            "Hepsini sırayla uygula.": ("write-intent", ""),
            "Tamam, sırayla hepsini yap.": ("write-intent", ""),
            "Lütfen sırayla hepsini yap.": ("write-intent", ""),
            "Tamam, lütfen hepsini sırayla uygula!": ("write-intent", ""),
            "SIRAYLA HEPSİNİ YAP": ("write-intent", ""),
            "Bunu düzelt.": ("correct", ""),
            "Değiştirebilirsin.": ("write-intent", ""),
            "Düzenleyebilirsin.": ("write-intent", ""),
            "Uygulayabilirsin.": ("write-intent", ""),
            "Düzeltebilir misin?": ("write-intent", ""),
            "Dosyaları değiştirebilir misin?": ("write-intent", ""),
            "Dosyaları değiştirebilir misiniz?": ("write-intent", ""),
            "BIB projesindeki hatayı düzelt.": ("correct", ""),
            "Atlas projesindeki hatayı düzelt.": ("correct", ""),
            "Atlas modülündeki hatayı düzelt.": ("correct", ""),
            "Borsa.md dosyasını düzelt.": ("correct", ""),
            "src/app.py dosyasını düzelt.": ("correct", ""),
            "Lütfen src/app.py dosyasını düzelt.": ("correct", ""),
            "parse.py dosyasını düzelt.": ("correct", ""),
            "ne.py dosyasını düzelt.": ("correct", ""),
            '"C:\\Users\\Me\\My Project\\app.py" dosyasını düzelt.': ("correct", ""),
            '"app.py" dosyasını düzelt.': ("correct", ""),
            '"My File.py" dosyasını düzelt.': ("correct", ""),
            "Bunları değiştir.": ("write-intent", ""),
            "BIB projesindeki hatayı düzeltebilir misin?": ("write-intent", ""),
            "Acaba düzeltebilir misin?": ("write-intent", ""),
        }

        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                directive = memory_ledger.memory_directive(prompt)
                self.assertEqual((directive.kind, directive.target), expected)

        for prompt in (
            'Alıntıda "Ok yap" yazıyor; bunu açıkla.',
            "Yazar 'Uygula' demiş; ne anlama geliyor?",
            "Belgelerde ‘Bunu düzelt’ geçiyor.",
            "Ok yap?",
            "Dosyaları değiştirme kuralı nedir?",
            "Sırayla hepsini yap?",
            "Sırayla yap?",
            "Sırayla yapma.",
            'Yazar "Sırayla yap" demiş.',
            '"Sadece incele" uygula.',
            '"Dosyaları değiştirme" uygula.',
            "Sırayla hepsini yapma.",
            "Sırayla hepsini yap demiş.",
            'Alıntıda "Sırayla hepsini yap" yazıyor.',
            "Sadece incele; sırayla hepsini yap.",
            "> Sırayla hepsini yap",
            '"Düzeltebilir misin?"',
            "BIB projesindeki hatayı düzeltme.",
            "Yanıtı buraya yaz.",
            "Bana kısa bir şiir yaz.",
            "Bana kısa bir şiiri düzelt.",
            "Hangi dosyayı düzeltebilir misin?",
            "Hangi dosyayı düzelt.",
            "Sakın dosyayı düzelt.",
            "Asla dosyadaki hatayı düzelt.",
            "SAKIN dosyayı düzelt.",
            "SANIRIM dosyayı düzelt.",
            "Hiçbir dosyayı düzelt.",
            "Lütfen hiçbir dosyayı düzelt.",
            "Hiç dosyayı düzelt.",
            "Onaylanmadıkça dosyayı düzelt.",
            "Gerekmedikçe dosyayı düzelt.",
            "Onaysızsa dosyayı düzelt.",
            "Onaylanmadıkça BIB projesindeki hatayı düzelt.",
            "Gerekmedikçe Atlas modülündeki hatayı düzelt.",
            "Onaysızsa src/app.py dosyasını düzelt.",
            "Çalışmazsa dosyayı düzelt.",
            "Gelmezse dosyayı düzelt.",
            "Çalışmazsam BIB projesindeki hatayı düzelt.",
            "Çalışmazsanız dosyayı düzelt.",
            "Yoksa dosyayı düzelt.",
            "Varsa dosyayı düzelt.",
            "Onaylansa dosyayı düzelt.",
            "Gelse dosyayı düzelt.",
            "Onaylanmasa dosyayı düzelt.",
            "Onaylansam dosyayı düzelt.",
            "Onaylanmasam dosyayı düzelt.",
            "Galiba dosyayı düzelt.",
            "Muhtemelen dosyayı düzelt.",
            "Kaç dosyayı düzeltebilir misin?",
            "Borsa dosyasındaki hatayı düzelt.",
            "Borsa dosyasını düzelt.",
            "Onay yoksa dosyayı düzelt.",
            "Onay varsa dosyayı düzelt.",
            "Yoksa BIB projesindeki hatayı düzelt.",
            "Varsa Atlas modülündeki hatayı düzelt.",
            "Yoksa klasörünü değiştir.",
            "Onaylıysa klasörünü değiştir.",
            "Sakın klasörünü değiştir.",
            r'Onaylanmadıkça \\server\share\app.py dosyasını düzelt.',
            r'Sakın \\server\share\app.py dosyasını düzelt.',
            "Onaylıysa... dosyayı düzelt.",
            "Gerekirse... dosyayı düzelt.",
            "Sakın... dosyayı düzelt.",
            "Yanıtındaki kodu düzelt.",
            "Bu cümledeki hatayı düzelt.",
            "Komut örneği olarak Atlas projesindeki hatayı düzelt.",
            '"C:\\Users\\Me\\My Project\\app.py dosyasını düzelt."',
            '"README.md\'yi düzenle."',
            '```text\n"C:\\Users\\Me\\My Project" klasörünü değiştir.\n```',
            '"Dockerfile dosyasını düzelt."',
            '"LICENSE dosyasını düzenle."',
            '"Bunu düzelt."',
            '"app.py dosyasını düzelt."',
            "Yazabilirsin.",
            "Eğer uygunsa BIB projesindeki hatayı düzelt.",
            "Belki BIB projesindeki hatayı düzelt.",
            "Onay verirsem BIB projesindeki hatayı düzelt.",
            "Onay verirseniz BIB projesindeki hatayı düzelt.",
            "Onay verdiysem BIB projesindeki hatayı düzelt.",
            "Onayım varsa BIB projesindeki hatayı düzelt.",
            "Onaylıysa BIB projesindeki hatayı düzelt.",
            "Onaylıysa projesindeki hatayı düzelt.",
            "Gerekirse modüldeki hatayı düzelt.",
            "Onay olduğu takdirde BIB projesindeki hatayı düzelt.",
            "Onay gelince BIB projesindeki hatayı düzelt.",
            "Onaydan sonra BIB projesindeki hatayı düzelt.",
            "Onay gelene kadar BIB projesindeki hatayı düzelt.",
            "Onay yokken BIB projesindeki hatayı düzelt.",
            "Onaylamadan düzeltme; sadece açıklama yap.",
            "Nasıl düzeltilir?",
            "BIB projesindeki hatayı nasıl düzeltebilir misin?",
            "BIB projesindeki hatayı düzeltebilir miyim?",
            "BIB projesindeki hatayı düzeltebilir misin, olur mu?",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(memory_ledger.is_explicit_write_intent(prompt))

        self.assertTrue(memory_ledger.is_explicit_write_intent("Bunu düzelt."))
        for prompt in (
            "Atlas modülündeki hatayı düzelt.",
            "Borsa.md dosyasını düzelt.",
            "src/app.py dosyasını düzelt.",
            "src/app.py dosyasındaki hatayı düzelt.",
            "src/app.py dosyasını düzeltin.",
            "Lütfen src/app.py dosyasını düzelt.",
            "Lütfen dosyayı değiştiriniz.",
            "parse.py dosyasını düzelt.",
            "ne.py dosyasını düzelt.",
            "app.v1.py dosyasını düzelt.",
            "src/app.py dosyasını düzelt, lütfen.",
            r'\\server\share\app.py dosyasını düzelt.',
            r'\\server-name\share.name\nested\app.py dosyasını düzelt.',
            "Tamam, src/app.py dosyasını düzelt.",
            "Okay, src/app.py dosyasını düzelt.",
            "Lütfen, src/app.py dosyasını düzelt.",
            "README.md'yi düzenle.",
            "pyproject.toml'u değiştir.",
            "src/app.py'yi düzelt.",
            "src klasörünü değiştir.",
            "src klasörü değiştir.",
            ".gitignore dosyasını düzenle.",
            ".env dosyasını değiştir.",
            ".env'yi düzenle.",
            "Dockerfile dosyasını düzelt.",
            '"Dockerfile" dosyasını düzelt.',
            '"LICENSE" dosyasını düzenle.',
            '"C:\\Users\\Me\\My Project" klasörünü değiştir.',
            '"C:\\Users\\Me\\My Project\\app.py"\'yi düzelt.',
            '"C:\\Users\\Me\\My Project\\app.py" dosyasını düzelt.',
            '"app.py" dosyasını düzelt.',
            '"My File.py" dosyasını düzelt.',
        ):
            with self.subTest(explicit_target=prompt):
                self.assertTrue(memory_ledger.is_explicit_write_intent(prompt))

    def test_secret_value_is_non_persistent(self) -> None:
        for text in ('API anahtarım sk-ABCDEFGHIJKLMNOPQRSTUV',
                     'Şifrem: uzun gizli örnek', 'parolam=örnek-değer'):
            with self.subTest(text=text):
                self.assertEqual(memory_ledger.memory_directive(text).kind, 'secret')
                sanitized, _ = memory_ledger.sanitize_text(text)
                self.assertNotIn('gizli örnek', sanitized)
                self.assertNotIn('örnek-değer', sanitized)

    def test_ambiguous_forget_requests_clarification_without_retrieval(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(hook, "_increment_prompt_count", return_value=2),
            mock.patch.object(hook, "retrieve_vault_context_detailed") as retrieve,
        ):
            context = hook.handle_user_prompt(
                {"session_id": "ambiguous", "prompt": "Bunu unut."},
                Path(temporary),
                vault_root=Path(temporary),
            )

        self.assertIn("Neyi unutmamı istediğini", context)
        retrieve.assert_not_called()

    def test_read_only_forget_combination_defers_the_preference_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = hook.handle_user_prompt(
                {
                    "session_id": "mixed-read-only",
                    "prompt": "Şunu unut: Ankara. Do not modify files or settings.",
                },
                Path(temporary),
                vault_root=Path(temporary),
            )

        self.assertIn("unutma kaydı bu turda uygulanmadı", context)
        self.assertNotIn("suppress_derived_memory", context)

    def test_what_known_uses_profile_query_and_requires_sources(self) -> None:
        result = vault_retrieval.VaultContextResult(
            "emitted",
            'path: "🔮 850-Companion/Profile.md"',
            1,
            1,
            ("🔮 850-Companion/Profile.md",),
            2600,
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(hook, "_increment_prompt_count", return_value=2),
            mock.patch.object(
                hook,
                "retrieve_vault_context_detailed",
                return_value=result,
            ) as retrieve,
        ):
            context = hook.handle_user_prompt(
                {
                    "session_id": "what-known",
                    "cwd": str(CODEX_DIR.parent),
                    "prompt": "Benim hakkımda ne biliyorsun?",
                },
                Path(temporary),
                vault_root=Path(temporary),
            )

        self.assertIn("her iddiada", context)
        self.assertIn("Profile.md", context)
        self.assertIn("Levent profili", retrieve.call_args.args[1])


class TranscriptPrivacyTests(unittest.TestCase):
    def test_sanitizer_redacts_credentials_before_persistence(self) -> None:
        tokens = (
            "sk-ABCDEFGHIJKLMNOPQRSTUV",
            "sk_ABCDEFGHIJKLMNOPQRSTUV",
            "ghp_ABCDEFGHIJKLMNOPQRSTUV",
            "github_pat_ABCDEFGHIJKLMNOPQRSTUV",
            "AKIAABCDEFGHIJKLMNOPQRSTUV",
        )
        sanitized, redactions = memory_ledger.sanitize_text(
            "Authorization: Bearer hidden-token\n"
            "api_key=sk-ABCDEFGHIJKLMNOPQRSTUV\n"
            + "\n".join(f"sample {token}" for token in tokens)
        )

        self.assertNotIn("hidden-token", sanitized)
        for token in tokens:
            self.assertNotIn(token, sanitized)
        self.assertIn("authorization", redactions)
        self.assertIn("credential", redactions)

    def test_secret_turn_and_reply_are_removed_before_flush(self) -> None:
        turns = [
            ("user", "Haftalık planı pazartesi yapacağım."),
            ("assistant", "Not ettim."),
            ("user", "password: super-secret-value"),
            ("assistant", "Parolanı gördüm."),
            ("user", "Plan kararını özetle."),
        ]

        filtered = memory_ledger.persistent_turns(turns)

        self.assertEqual(
            filtered,
            [
                ("user", "Haftalık planı pazartesi yapacağım."),
                ("assistant", "Not ettim."),
                ("user", "Plan kararını özetle."),
            ],
        )

    def test_session_only_control_removes_the_whole_conversation(self) -> None:
        turns = [
            ("user", "Kalıcı olabilecek karar."),
            ("assistant", "Anladım."),
            ("user", "Bu konuşmada kalsın."),
        ]

        self.assertEqual(memory_ledger.persistent_turns(turns), [])

    def test_do_not_save_removes_only_the_referenced_exchange(self) -> None:
        turns = [
            ("user", "Kalıcı karar."),
            ("assistant", "Not ettim."),
            ("user", "Geçici ayrıntı."),
            ("assistant", "Anladım."),
            ("user", "Bunu kaydetme! Lütfen."),
            ("assistant", "Kaydetmeyeceğim."),
            ("user", "Kalıcı karar devam ediyor."),
        ]

        self.assertEqual(
            memory_ledger.persistent_turns(turns),
            [
                ("user", "Kalıcı karar."),
                ("assistant", "Not ettim."),
                ("user", "Kalıcı karar devam ediyor."),
            ],
        )


class SuppressionTests(unittest.TestCase):
    def test_preference_reads_need_no_write_lock_and_failed_update_preserves_old_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            ledger = memory_ledger.suppress_derived_memory(private, 'Önceki bilgi')
            before = ledger.read_bytes()
            with mock.patch.object(memory_ledger, 'atomic_write_text', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    memory_ledger.suppress_derived_memory(private, 'Yeni bilgi')
            self.assertEqual(ledger.read_bytes(), before)
            with mock.patch.object(memory_ledger, 'locked', side_effect=AssertionError('read wants a write lock')):
                hashes = memory_ledger.load_suppressed_hashes(private)
            self.assertEqual(hashes, frozenset({memory_ledger.memory_text_hash('Önceki bilgi')}))

    def test_exact_units_are_hidden_through_markdown_metadata_and_paths(self):
        hashes = frozenset({memory_ledger.memory_text_hash('Ankara')})
        for text in ('# Ankara\n', 'title: Ankara\n', 'aliases: [Ankara]\n',
                     '[[knowledge/concepts/ankara|Ankara]]\n', '[Ankara](notes/ankara.md)\n',
                     'knowledge/concepts/Ankara.md\n'):
            with self.subTest(text=text):
                self.assertEqual(memory_ledger.filter_suppressed_text(text, hashes), '')
        self.assertEqual(memory_ledger.filter_suppressed_text('Plan pazartesi.\n', hashes),
                         'Plan pazartesi.\n')

    def test_forgotten_filename_cannot_recur_as_title_or_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / "🧠 500-Knowledge/Levent Ankara'da yaşıyor.md"
            source.parent.mkdir()
            source.write_text("Levent Ankara'da yaşıyor", encoding='utf-8')
            memory_ledger.suppress_derived_memory(vault / '.codex/private-memory',
                                                 "Levent Ankara'da yaşıyor")
            result = vault_retrieval.retrieve_vault_context_detailed(vault,
                'Levent Ankara yaşıyor', write_cache=False)
            self.assertEqual(result.paths, ())
            self.assertTrue(source.exists())

    def test_broken_preferences_do_not_route_the_agent_to_raw_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            ledger = vault / '.codex/private-memory/controls/suppressions.jsonl'
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{torn record', encoding='utf-8')
            context = hook.handle_user_prompt({'session_id': 'invalid-preferences',
                'prompt': 'Levent hangi şehirde yaşıyor?'}, vault / '.state', vault_root=vault)
            self.assertIn('Ham notlara veya eski önbelleğe geçme', context)
            self.assertNotIn('Mevcut dosya aramasıyla', context)

    def test_forget_tombstone_stores_only_target_hash(self) -> None:
        target = "Levent Ankara'da yaşıyor"
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)

            created = memory_ledger.suppress_derived_memory(private_root, target, now=1.0)
            stored = created.read_text(encoding="utf-8")
            hashes = memory_ledger.load_suppressed_hashes(private_root)

        self.assertNotIn(target, stored)
        self.assertIn(memory_ledger.memory_text_hash(target), hashes)

    def test_session_only_marker_keeps_raw_session_id_out_of_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            session_id = "raw-private-session"

            memory_ledger.mark_session_only(state, session_id)
            marked = memory_ledger.is_session_only(state, session_id)
            names = [path.name for path in state.iterdir()]

        self.assertTrue(marked)
        self.assertNotIn(session_id, "\n".join(names))

    def test_read_only_marker_is_turn_scoped_and_keeps_raw_session_id_out_of_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            session_id = "raw-audit-session"

            memory_ledger.mark_read_only_turn(state, session_id)
            marked = memory_ledger.is_read_only_turn(state, session_id)
            names = [path.name for path in state.iterdir()]
            memory_ledger.clear_read_only_turn(state, session_id)
            cleared = memory_ledger.is_read_only_turn(state, session_id)
            memory_ledger.clear_read_only_turn(state, session_id)  # idempotent

        self.assertTrue(marked)
        self.assertFalse(cleared)
        self.assertNotIn(session_id, "\n".join(names))
        self.assertEqual(memory_ledger.persistent_turns(
            [("user", "Salt okunur denetim yap."), ("assistant", "Rapor.")]),
            [("user", "Salt okunur denetim yap."), ("assistant", "Rapor.")])

    def test_retrieval_suppresses_only_matching_derived_line(self) -> None:
        forgotten = "Levent Ankara'da yaşıyor"
        retained = "Levent haftalık planını pazartesi yapıyor"
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            derived = vault / "knowledge" / "concepts"
            human = vault / "🧠 500-Knowledge"
            derived.mkdir(parents=True)
            human.mkdir()
            (derived / "profil.md").write_text(
                "# Profil\n\n"
                "## Kayıtlar\n\n"
                "- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 "
                "[[daily/2026-09-04|Kaynak]] — "
                + forgotten
                + "\n- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 "
                "[[daily/2026-09-04|Kaynak]] — "
                + retained
                + "\n",
                encoding="utf-8",
            )
            (human / "Profil Notum.md").write_text(
                "# Profil Notum\n\n" + forgotten + "\n",
                encoding="utf-8",
            )
            memory_ledger.suppress_derived_memory(
                vault / ".codex" / "private-memory",
                forgotten,
                now=1.0,
            )

            result = vault_retrieval.retrieve_vault_context_detailed(
                vault,
                "Levent Ankara yaşıyor",
                write_cache=False,
            )
            retained_result = vault_retrieval.retrieve_vault_context_detailed(
                vault,
                "Levent haftalık plan pazartesi",
                write_cache=False,
            )

        self.assertEqual(result.paths, ())
        self.assertNotIn(forgotten, retained_result.text)
        self.assertIn(retained, retained_result.text)

    def test_projection_hides_a_sentence_without_editing_its_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / 'source.md'
            original = 'Ankara\'da yaşıyorum. Haftalık plan pazartesi yapılır.\n'
            source.write_text(original, encoding='utf-8')
            memory_ledger.suppress_derived_memory(vault / '.codex/private-memory',
                                                 "Ankara'da yaşıyorum")
            visible = memory_ledger.read_memory_source(vault, source)
            self.assertNotIn('Ankara', visible)
            self.assertIn('pazartesi', visible)
            self.assertEqual(source.read_text(encoding='utf-8'), original)

    def test_read_only_followup_keeps_cache_and_views_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            notes = vault / "🧠 500-Knowledge"
            notes.mkdir(parents=True)
            (notes / "normal.md").write_text(
                "# Normal kaynak\nH08 ortak kanıtı normal kaynakta.\n",
                encoding="utf-8",
            )
            (notes / "hidden.md").write_text(
                "# Gizli kaynak\nH08 ortak kanıtı gizli kaynakta.\n"
                "H08-SIZDIRILMAMALI-AYRINTI.\n",
                encoding="utf-8",
            )
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory",
                "H08-SIZDIRILMAMALI-AYRINTI.",
                now=1,
            )

            # Warm one disposable projected view and the normal index before the
            # read-only turns; the turns must leave both byte-for-byte unchanged.
            vault_retrieval.retrieve_vault_context_detailed(
                vault, "H08 ortak kanıtı", write_cache=False
            )
            vault_retrieval.build_vault_map(vault)
            cache = vault / vault_retrieval.CACHE_RELATIVE_PATH
            view_dir = vault / ".codex/private-memory/views"
            cache_before = cache.read_bytes()
            views_before = {
                path.name: path.read_bytes() for path in view_dir.glob("*.md")
            }

            real_handle = hook.handle_user_prompt
            contexts: list[str] = []

            def handle(payload: dict[str, object], state_dir: Path) -> str:
                context = real_handle(
                    payload,
                    state_dir,
                    vault_root=vault,
                    now=1234,
                )
                contexts.append(context)
                return context

            payloads = iter(
                (
                    {
                        "session_id": "h08-read-only",
                        "cwd": str(vault),
                        "prompt": "Salt okunur inceleme yap.",
                    },
                    {
                        "session_id": "h08-read-only",
                        "cwd": str(vault),
                        "prompt": "H08 ortak kanıtı",
                    },
                )
            )
            with (
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "handle_user_prompt", side_effect=handle),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_load_payload", side_effect=lambda: next(payloads)),
                mock.patch.object(hook.sys, "stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(hook.main(["user-prompt"]), 0)
                self.assertEqual(hook.main(["user-prompt"]), 0)

            self.assertTrue(memory_ledger.is_read_only_turn(state, "h08-read-only"))
            self.assertEqual(len(contexts), 2)
            self.assertIn("Normal kaynak", contexts[1])
            self.assertIn("source_id", contexts[1])
            self.assertIn("memory_ledger.read_memory_source", contexts[1])
            self.assertNotIn("🧠 500-Knowledge/normal.md", contexts[1])
            self.assertNotIn("🧠 500-Knowledge/hidden.md", contexts[1])
            self.assertNotIn("H08-SIZDIRILMAMALI-AYRINTI", contexts[1])
            self.assertEqual(cache.read_bytes(), cache_before)
            self.assertEqual(
                {path.name: path.read_bytes() for path in view_dir.glob("*.md")},
                views_before,
            )

    def test_read_only_cold_retrieval_keeps_long_source_readable_without_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            source = vault / "🧠 500-Knowledge/long.md"
            source.parent.mkdir(parents=True)
            linked = source.parent / "linked-note.md"
            linked.write_text("# Linked kaynak\nH08 linked kanıtı.\n", encoding="utf-8")
            hidden = "H08-SIZDIRILMAMALI-AYRINTI."
            source.write_text(
                "# Uzun kaynak\n"
                "H08-LONG-SENTINEL " + ("uzun içerik. " * 600) + "tail-kanıtı.\n"
                "[[linked-note|Ham hedef etiketi]]\n"
                + hidden + "\n",
                encoding="utf-8",
            )
            original = source.read_bytes()
            memory_ledger.suppress_derived_memory(
                vault / ".codex/private-memory", hidden, now=1
            )
            cache = vault / vault_retrieval.CACHE_RELATIVE_PATH
            view_dir = vault / ".codex/private-memory/views"

            result = vault_retrieval.retrieve_vault_context_detailed(
                vault,
                "H08-LONG-SENTINEL",
                write_cache=False,
                write_views=False,
            )
            filtered = memory_ledger.read_memory_source(vault, source)
            opaque_source = vault / memory_ledger.memory_view_relative_path(
                source.relative_to(vault).as_posix()
            )
            filtered_from_opaque_id = memory_ledger.read_memory_source(
                vault, opaque_source
            )
            linked_relative = linked.relative_to(vault).as_posix()
            linked_opaque = vault / memory_ledger.memory_view_relative_path(linked_relative)
            linked_filtered = memory_ledger.read_memory_source(vault, linked_opaque)
            with self.assertRaisesRegex(
                memory_ledger.MemorySourceError, "memory-view-source-unavailable"
            ):
                memory_ledger.read_memory_source(
                    vault, vault / memory_ledger.memory_view_relative_path("missing.md")
                )
            source_after = source.read_bytes()
            cache_exists = cache.exists()
            view_dir_exists = view_dir.exists()

        self.assertEqual(result.outcome, "emitted")
        self.assertIn("source_id", result.text)
        self.assertNotIn("🧠 500-Knowledge/long.md", result.text)
        self.assertIn("memory_ledger.read_memory_source(vault_root, vault_root / source_id)", result.text)
        self.assertIn("Metin bütçeye sığmıyor; tam kaynağı oku", result.text)
        self.assertNotIn(hidden, result.text)
        self.assertNotIn(hidden, filtered)
        self.assertIn("tail-kanıtı", filtered)
        self.assertGreater(len(filtered), 5000)
        self.assertIn("Ham hedef etiketi", filtered_from_opaque_id)
        self.assertIn(
            memory_ledger.memory_view_relative_path(
                linked.relative_to(vault).as_posix()
            ),
            filtered_from_opaque_id,
        )
        self.assertNotIn(hidden, filtered_from_opaque_id)
        self.assertIn("tail-kanıtı", filtered_from_opaque_id)
        self.assertIn("H08 linked kanıtı.", linked_filtered)
        self.assertEqual(source_after, original)
        self.assertFalse(cache_exists)
        self.assertFalse(view_dir_exists)


if __name__ == "__main__":
    unittest.main(verbosity=2)
