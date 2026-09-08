from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import hook
import compile as memory_compile
from test_second_brain_acceptance import _seed_vault


class NaturalRecallTests(unittest.TestCase):
    def test_topicless_followups_request_context_without_unrelated_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            noise = vault / '🧠 500-Knowledge/Ilgisiz.md'
            noise.parent.mkdir(parents=True, exist_ok=True)
            noise.write_text('# İlgisiz\nBunlar nasıl çözülür bunu nasıl uygulayacaksın.\n', encoding='utf-8')
            for prompt in ('Bunlar nasıl çözülür?', 'Bunu nasıl uygulayacaksın?',
                           'Bunları nasıl düzeltmeyi düşünüyosun',
                           'nasıl çözmeyi planlıyorsun',
                           'Başka zorlandığın neler var',
                           'Şunları nasıl uygulamayı düşünüyorsun?',
                           'Bunları nasıl çözmeyi planlıyorsun?',
                           'Nasıl düzeltmeyi planlıyorsun?',
                           'Bu dediklerini yap o zaman',
                           'devam et',
                           'nasıl daha verimli kullanbilirsin sence',
                           'nasıl daha verimli kullanabilirsin sence',
                           'nasıl daha verimli kullanabilirsin',
                           'bunları nasıl implemente etmeyi düşünüyosun',
                           'Bunları nasıl implemente etmeyi düşünüyorsun?',
                           'Bunu nasıl yapmayı planlıyorsun?',
                           'BUNLARI NASIL YAPARIZ?!', 'Peki, bunu nasıl yapacaksın?'):
                with self.subTest(prompt=prompt):
                    context = hook.handle_user_prompt(
                        {'session_id': 'followup', 'prompt': prompt},
                        vault / '.codex/scripts/.state', vault_root=vault)
                    self.assertNotIn('Ilgisiz.md', context)
                    self.assertIn('[Konuşma Bağlamı]', context)
                    self.assertIn('açık bir sorguyla', context)
                    self.assertNotIn('İlk aramada aday bulunamadı', context)
                    # Search eligibility must not change early persistence eligibility.
                    self.assertTrue(hook.is_meaningful_query(prompt))

    def test_followup_filter_keeps_named_topics_quotes_and_unknown_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            for prompt in ('BIB?', 'MPG fiyatı?', 'Bunu Atlas için nasıl uygularız?',
                           'Pakmaya?', 'Bunu BIB için nasıl uygularız?',
                           'Bunları Atlas için nasıl implemente etmeyi düşünüyosun?',
                           'Bunları nasıl implemente etmeyi düşünmüyorsun?',
                           'Buna daha önce ne karar vermiştik?',
                           'Bunu 2026 için nasıl yaparız?', '"Bunlar" nasıl bulunur?',
                           'bu indexleme sence nasıl',
                           'edge caseler ne olab,ilir', 'Bu çözümleri nasıl uygulayacaksın?'):
                with self.subTest(prompt=prompt):
                    with mock.patch.object(hook, 'retrieve_vault_context_detailed',
                                           side_effect=OSError('search probe')) as retrieve:
                        context = hook.handle_user_prompt(
                            {'session_id': 'topics', 'prompt': prompt},
                            vault / '.codex/scripts/.state', vault_root=vault)
                    retrieve.assert_called_once()
                    self.assertNotIn('[Konuşma Bağlamı]', context)

    def test_leading_local_skill_invocation_is_removed_from_retrieval_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            prompt = (r'[$ponytail](C:\workspace\.codex\skills\ponytail\SKILL.md) '
                      'BIB?')
            with mock.patch.object(hook, 'retrieve_vault_context_detailed',
                                   side_effect=OSError('search probe')) as retrieve:
                hook.handle_user_prompt(
                    {'session_id': 'skill-prefix', 'prompt': prompt},
                    vault / '.codex/scripts/.state', vault_root=vault)
        self.assertEqual(retrieve.call_args.args[1], 'BIB?')

    def test_skill_invocation_with_followup_uses_conversation_and_keeps_capture_eligibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            prompt = (r'[$thinking-model-router](C:\workspace\.codex\skills\thinking-model-router\SKILL.md) '
                      'Bunları nasıl düzeltmeyi düşünüyosun')
            with mock.patch.object(hook, 'retrieve_vault_context_detailed') as retrieve:
                context = hook.handle_user_prompt({'session_id': 'combined', 'prompt': prompt},
                                                 vault / '.codex/scripts/.state', vault_root=vault)
            retrieve.assert_not_called()
            self.assertIn('[Konuşma Bağlamı]', context)
            self.assertTrue(hook.is_meaningful_query(prompt))

    def test_skill_link_stripping_requires_leading_local_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            prompts = (
                ('[$skill](https://example.com/SKILL.md) BIB?',
                 '[$skill](https://example.com/SKILL.md) BIB?'),
                ('Transcript: [$skill](C:\\workspace\\.codex\\skills\\x\\SKILL.md) BIB?',
                 'Transcript: [$skill](C:\\workspace\\.codex\\skills\\x\\SKILL.md) BIB?'),
            )
            for prompt, expected in prompts:
                with self.subTest(prompt=prompt):
                    with mock.patch.object(hook, 'retrieve_vault_context_detailed',
                                           side_effect=OSError('search probe')) as retrieve:
                        hook.handle_user_prompt(
                            {'session_id': 'skill-boundary', 'prompt': prompt},
                            vault / '.codex/scripts/.state', vault_root=vault)
                    self.assertEqual(retrieve.call_args.args[1], expected)

    def test_trailing_local_skill_invocation_is_removed_from_retrieval_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            prompt = (r'BIB? [$ponytail](C:\workspace\.codex\skills\ponytail\SKILL.md)')
            with mock.patch.object(hook, 'retrieve_vault_context_detailed',
                                   side_effect=OSError('search probe')) as retrieve:
                hook.handle_user_prompt(
                    {'session_id': 'skill-tail', 'prompt': prompt},
                    vault / '.codex/scripts/.state', vault_root=vault)
        self.assertEqual(retrieve.call_args.args[1], 'BIB?')

    def test_skill_links_inside_literals_and_middle_text_remain_searchable(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            prompts = (
                ('"[$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md) BIB?"',
                 '"[$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md) BIB?"'),
                ('```[$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md) BIB?```',
                 '```[$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md) BIB?```'),
                ('> BIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)',
                 '> BIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)'),
                ('BIB? `[$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)',
                 'BIB? `[$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)'),
                ('```\nBIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)',
                 '```\nBIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)'),
                ('~~~\nBIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)',
                 '~~~\nBIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)'),
                ('“BIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)',
                 '“BIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md)'),
                ('BIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md) karar?',
                 'BIB? [$skill](C:\\Users\\demo\\.codex\\skills\\x\\SKILL.md) karar?'),
            )
            for prompt, expected in prompts:
                with self.subTest(prompt=prompt):
                    with mock.patch.object(hook, 'retrieve_vault_context_detailed',
                                           side_effect=OSError('search probe')) as retrieve:
                        hook.handle_user_prompt(
                            {'session_id': 'skill-boundary', 'prompt': prompt},
                            vault / '.codex/scripts/.state', vault_root=vault)
                    self.assertEqual(retrieve.call_args.args[1], expected)

    def test_failed_compilation_is_not_a_successful_strict_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            with (
                mock.patch.object(memory_compile, 'VAULT_ROOT', vault),
                mock.patch.object(memory_compile, 'STATE_DIR', vault / '.codex/scripts/.state'),
                mock.patch.object(memory_compile, '_run_codex', return_value='codex-timeout'),
            ):
                result = memory_compile.main(['--strict', '--max-calls', '1'])
                status = memory_compile.compile_state.load(vault / '.codex/scripts/.state').last_status
        self.assertEqual(result, 1)
        self.assertEqual(status, 'fail:codex-timeout')

    def test_empty_first_search_requests_expansion_on_later_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state = vault / '.codex/scripts/.state'
            payload = {'session_id': 'recall', 'prompt':
                       'Bağlantı kesilince de devam etmek için ne seçmiştik?'}
            hook.handle_user_prompt(payload, state, vault_root=vault)
            context = hook.handle_user_prompt(payload, state, vault_root=vault)
        self.assertIn('İlk aramada aday bulunamadı', context)
        self.assertIn('farklı ifadeler', context)
        self.assertIn('bilgi yok demek değildir', context)

    def test_search_failure_is_visible_even_on_first_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            with mock.patch.object(hook, 'retrieve_vault_context_detailed',
                                   side_effect=OSError('synthetic failure')):
                context = hook.handle_user_prompt(
                    {'session_id': 'failure', 'prompt': 'Atlas kararını hatırlat'},
                    vault / '.codex/scripts/.state', vault_root=vault)
        self.assertIn('Vault araması tamamlanamadı', context)
        self.assertIn('bilgi yok', context)
        self.assertLessEqual(len(context), hook.USER_PROMPT_CONTEXT_TARGET_CHARS)

    def test_search_failure_stays_visible_when_health_storage_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            with (
                mock.patch.object(hook, 'retrieve_vault_context_detailed',
                                  side_effect=OSError('search unavailable')),
                mock.patch.object(hook, 'atomic_write',
                                  side_effect=OSError('health disk unavailable')),
            ):
                context = hook.handle_user_prompt(
                    {'session_id': 'failure', 'prompt': 'Atlas kararını hatırlat'},
                    vault / '.codex/scripts/.state', vault_root=vault)
        self.assertIn('Vault araması tamamlanamadı', context)

    def test_found_sources_survive_health_storage_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            with mock.patch.object(hook, 'atomic_write',
                                   side_effect=OSError('health disk unavailable')):
                context = hook.handle_user_prompt(
                    {'session_id': 'recall', 'prompt': 'Atlas yerel önbellek kararı'},
                    vault / '.codex/scripts/.state', vault_root=vault)
        self.assertIn('Atlas.md', context)
        self.assertNotIn('Vault araması tamamlanamadı', context)


if __name__ == '__main__':
    unittest.main()
