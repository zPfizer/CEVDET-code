from pathlib import Path
import io
import json
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
from test_profile_guard import seed_profile, PROFILE_TEXT, CONCEPT_TEXT
import doctor
import hook
import memory_ledger as ledger
import vault_retrieval as retrieval


class ProfileEnforcementTests(unittest.TestCase):
    def setUp(self):
        self._scope_guard = mock.patch.object(hook, '_validate_hook_scope')
        self._scope_guard.start()
        self.addCleanup(self._scope_guard.stop)

    def test_invalid_profile_is_not_injected_or_read_and_original_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            state = root / '.codex/scripts/.state'
            self.assertIn('Kısa ve doğal', hook.build_session_context(root, state))
            bad = PROFILE_TEXT.replace('Kısa ve doğal bir oturum özeti.', 'YANLIS-PORTRE ' * 100)
            path.write_text(bad, encoding='utf-8')
            context = hook.build_session_context(root, state)
            self.assertIn('Profil kontrolü başarısız', context)
            self.assertNotIn('YANLIS-PORTRE', context)
            self.assertEqual(ledger.read_memory_source(root, path), '')
            self.assertEqual(path.read_text(encoding='utf-8'), bad)

    def test_every_prompt_checks_the_updated_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            payload = {'session_id': 'profile-prompt', 'prompt': 'merhaba'}
            state = root / '.codex/scripts/.state'
            self.assertIn('Kısa ve doğal', hook.handle_user_prompt(payload, state, vault_root=root))
            path.write_text(PROFILE_TEXT.replace('Türkçe ve kısa yanıt ver', 'Kaynağı olmayan istek'), encoding='utf-8')
            context = hook.handle_user_prompt(payload, state, vault_root=root)
            self.assertIn('profile-claim-mismatch', context)
            self.assertNotIn('Kısa ve doğal', context)

    def test_cached_profile_is_rechecked_when_only_its_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            original = path.read_bytes()
            entries = retrieval.build_vault_map(root)
            self.assertIn('🔮 850-Companion/Profile.md', [entry.path for entry in entries])
            source = root / 'knowledge/concepts/tercih-kisa.md'
            source.write_text(CONCEPT_TEXT.replace('kullanici-dusuncesi', 'cevo-cikarimi'), encoding='utf-8')
            entries = retrieval.build_vault_map(root)
            self.assertNotIn('🔮 850-Companion/Profile.md', [entry.path for entry in entries])
            self.assertEqual(ledger.read_memory_source(root, path), '')
            self.assertEqual(path.read_bytes(), original)
            source.write_text(CONCEPT_TEXT, encoding='utf-8')
            self.assertIn('🔮 850-Companion/Profile.md', [entry.path for entry in retrieval.build_vault_map(root)])

    def test_suppressed_source_cannot_validate_profile_and_raw_link_is_not_reintroduced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            ledger.suppress_derived_memory(root / '.codex/private-memory', 'Türkçe ve kısa yanıt ver')
            context = hook.build_session_context(root, root / '.codex/scripts/.state')
            self.assertNotIn('Kısa ve doğal', context)
            self.assertIn('Profil kontrolü başarısız', context)
            self.assertEqual(ledger.read_memory_source(root, path), '')

    def test_valid_profile_card_stays_compact_with_self_link_in_disk_and_pure_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            path.write_text(
                PROFILE_TEXT.replace(
                    'Kısa ve doğal bir oturum özeti.',
                    'Kısa ve doğal bir oturum özeti [[🔮 850-Companion/Profile|Profil]].',
                ),
                encoding='utf-8',
            )
            ledger.suppress_derived_memory(root / '.codex/private-memory', 'Ek bölüm.')
            state = root / '.codex/scripts/.state'
            disk = hook.build_session_context(root, state)
            pure = hook.build_session_context(root, state, write_views=False)

        for context in (disk, pure):
            with self.subTest(context=context):
                self.assertIn('[Hafıza: Profil]', context)
                self.assertIn('Kısa ve doğal bir oturum özeti', context)
                self.assertNotIn('Profil kontrolü başarısız', context)
        self.assertEqual(disk, pure)

    def test_stop_requires_correction_once_then_reports_persistent_failure_and_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            state = root / '.codex/scripts/.state'
            payload = {'session_id': 'profile-stop', 'transcript_path': str(root / 'transcript.jsonl')}

            def stop(active=False):
                output = io.StringIO()
                with (mock.patch.object(hook, 'VAULT_ROOT', root),
                      mock.patch.object(hook, 'STATE_DIR', state),
                      mock.patch.object(hook, '_load_payload', return_value=dict(payload, stop_hook_active=active)),
                      mock.patch.object(hook, 'enqueue_flush'),
                      mock.patch('sys.stdout', output)):
                    result = hook.main(['turn-end', '--strict'])
                return result, json.loads(output.getvalue())

            self.assertEqual(stop(), (0, {'continue': True}))
            path.write_text(PROFILE_TEXT.replace('2026-09-05', 'not-a-date'), encoding='utf-8')
            code, response = stop()
            self.assertEqual(code, 1)
            self.assertEqual(response['decision'], 'block')
            self.assertIn('profile-frontmatter', response['reason'])
            code, response = stop(active=True)
            self.assertEqual(code, 1)
            self.assertIn('systemMessage', response)
            self.assertNotIn('decision', response)
            self.assertEqual(json.loads((state / 'hook-health.json').read_text())['status'], 'error')
            path.write_text(PROFILE_TEXT, encoding='utf-8')
            self.assertEqual(stop(), (0, {'continue': True}))
            self.assertEqual(json.loads((state / 'hook-health.json').read_text())['status'], 'ok')

    def test_stop_on_read_only_turn_reports_profile_failure_without_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            state = root / '.codex/scripts/.state'
            path.write_text(PROFILE_TEXT.replace('2026-09-05', 'not-a-date'), encoding='utf-8')
            ledger.mark_read_only_turn(state, 'profile-stop-ro')
            payload = {'session_id': 'profile-stop-ro', 'transcript_path': str(root / 'transcript.jsonl')}
            output = io.StringIO()
            with (mock.patch.object(hook, 'VAULT_ROOT', root),
                  mock.patch.object(hook, 'STATE_DIR', state),
                  mock.patch.object(hook, '_load_payload', return_value=dict(payload)),
                  mock.patch.object(hook, 'enqueue_flush') as enqueue,
                  mock.patch('sys.stdout', output)):
                result = hook.main(['turn-end', '--strict'])
            response = json.loads(output.getvalue())
            marked = ledger.is_read_only_turn(state, 'profile-stop-ro')
            health = json.loads((state / 'hook-health.json').read_text())['status']
        self.assertEqual(result, 1)
        self.assertIn('systemMessage', response)
        self.assertIn('Salt okunur', response['systemMessage'])
        self.assertIn('profile-frontmatter', response['systemMessage'])
        self.assertNotIn('decision', response)
        enqueue.assert_not_called()
        self.assertTrue(marked)
        self.assertEqual(health, 'error')

    def test_doctor_reports_profile_failure_and_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = seed_profile(root)
            ctx = doctor.Context(root, state_dir=root / '.codex/scripts/.state')
            self.assertEqual(doctor._profile_maintenance_check(ctx).status, 'OK')
            path.write_text(PROFILE_TEXT.replace('Türkçe ve kısa yanıt ver', 'Başka tercih'), encoding='utf-8')
            self.assertEqual(doctor._profile_maintenance_check(ctx).status, 'FAIL')
            path.write_text(PROFILE_TEXT, encoding='utf-8')
            self.assertEqual(doctor._profile_maintenance_check(ctx).status, 'OK')


if __name__ == '__main__':
    unittest.main()
