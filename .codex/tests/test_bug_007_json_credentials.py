from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import attachment_memory
import flush
import memory_ledger as ledger


MARKERS = {
    "plain-secret",
    "json-secret",
    "dict-secret",
    "key-secret",
    "token-secret",
    "LEAK_PROBE_ESCAPED",
}


class Bug007JsonCredentialTests(unittest.TestCase):
    def test_structured_credentials_hide_whole_value_and_keep_siblings(self) -> None:
        for value in ({"value": "OBJECT_SECRET", "nested": ["ARRAY_SECRET", {"text": "} ]", "authorization": "Bearer HEADER_SECRET"}]},
                      ["ARRAY_SECRET", {"value": "OBJECT_SECRET"}]):
            for indent in (None, 2):
                with self.subTest(value=value, indent=indent):
                    original = json.dumps({"token": value, "keep": "ordinary", "password": "NEXT_SECRET"}, indent=indent,
                                          separators=(',', ':') if indent is None else None)
                    sanitized, redactions = ledger.sanitize_text(original, max_chars=None)
                    for marker in ("OBJECT_SECRET", "ARRAY_SECRET", "NEXT_SECRET", "HEADER_SECRET"):
                        self.assertNotIn(marker, sanitized)
                    self.assertEqual(json.loads(sanitized), {"token": "<REDACTED>", "keep": "ordinary", "password": "<REDACTED>"})
                    self.assertIn("credential", redactions)
                    self.assertEqual(ledger.sanitize_text(sanitized, max_chars=None)[0], sanitized)

    def test_unreadable_credential_container_does_not_leak_the_remaining_value(self) -> None:
        for value in ('{\n"value": "BROKEN_SECRET"',):
            with self.subTest(value=value[:30]), self.assertRaisesRegex(ledger.MemoryPreferenceError, '^memory-credential-container-unverifiable$'):
                ledger.sanitize_text('keep before; token=' + value, max_chars=None)
        for value in ('{"safe":1}TRAILING_SECRET', '{"safe": 1}TRAILING_SECRET', '[1]TRAILING_SECRET',
                      '{"safe":1},TRAILING_SECRET', '{"safe": 1};TRAILING_SECRET'):
            with self.subTest(value=value[:30]):
                sanitized, redactions = ledger.sanitize_text('keep before; token=' + value, max_chars=None)
                self.assertEqual(sanitized, 'keep before; token=<REDACTED>')
                self.assertEqual(redactions, ("credential",))

    def test_plain_container_keeps_content_after_the_credential_whitespace_boundary(self) -> None:
        for value in ('{"safe":1};', '{"safe": 1},TRAILING_SECRET', '[1]TRAILING_SECRET'):
            with self.subTest(value=value):
                sanitized, _ = ledger.sanitize_text('token=' + value + ' important decision', max_chars=None)
                self.assertEqual(sanitized, 'token=<REDACTED> important decision')

    def test_quoted_container_does_not_trust_a_delimiter_in_invalid_json(self) -> None:
        for ending in (',TRAILING_SECRET}', '}TRAILING_SECRET', ']TRAILING_SECRET', ' TRAILING_SECRET}',
                       "}'TRAILING_SECRET", '}"TRAILING_SECRET'):
            with self.subTest(ending=ending), self.assertRaisesRegex(ledger.MemoryPreferenceError, '^memory-credential-container-unverifiable$'):
                ledger.sanitize_text('{"token":{"safe":1}' + ending + ' important decision', max_chars=None)

    def test_quoted_python_container_is_rejected_without_consuming_siblings(self) -> None:
        with self.assertRaisesRegex(ledger.MemoryPreferenceError, '^memory-credential-container-unverifiable$'):
            ledger.sanitize_text("{'token': {'a': 1},'keep':'ordinary'} after", max_chars=None)

    def test_json_string_credential_snippet_keeps_outer_siblings(self) -> None:
        original = json.dumps(
            {"message": "example 'token': {'a': 1} in docs", "keep": "ordinary"},
            separators=(",", ":"),
        )
        sanitized, redactions = ledger.sanitize_text(original, max_chars=None)
        self.assertEqual(
            sanitized,
            json.dumps({"message": "<REDACTED>", "keep": "ordinary"}, separators=(",", ":")),
        )
        self.assertIn("credential", redactions)

    def test_decoded_json_credential_names_are_redacted(self) -> None:
        nested = json.dumps({"api_key": "INNER_SYNTHETIC_SECRET"})
        escaped_key = r'{"\u0061pi_key":"ESCAPED_KEY_SYNTHETIC_SECRET","keep":"ordinary"}'
        for original, expected, secret in (
            (
                json.dumps({"message": nested, "keep": "ordinary"}),
                {"message": json.dumps({"api_key": "<REDACTED>"}), "keep": "ordinary"},
                "INNER_SYNTHETIC_SECRET",
            ),
            (escaped_key, {"api_key": "<REDACTED>", "keep": "ordinary"}, "ESCAPED_KEY_SYNTHETIC_SECRET"),
        ):
            with self.subTest(secret=secret):
                sanitized, redactions = ledger.sanitize_text(original, max_chars=None)
                self.assertEqual(json.loads(sanitized), expected)
                self.assertNotIn(secret, sanitized)
                self.assertIn("credential", redactions)

    def test_json_credential_like_keys_are_rejected_without_collisions(self) -> None:
        first_key = "example 'token': {'a': 1}"
        second_key = "example 'token': {'b': 2}"
        text = f'{{{json.dumps(first_key)}:"first",{json.dumps(second_key)}:"second","keep":"ordinary"}}'
        with self.assertRaisesRegex(ledger.MemoryPreferenceError, '^memory-credential-container-unverifiable$'):
            ledger.sanitize_text(text, max_chars=None)

    def test_valid_json_fragments_keep_siblings_and_surrounding_prose(self) -> None:
        payload = '{"token":{"value":"OBJECT_SECRET"},"keep":"ordinary"}'
        safe = '{"token":"<REDACTED>","keep":"ordinary"}'
        for prefix, suffix in (('before ', '\nafter'), ('before {not-json}\n```json\n', '\n```\nafter'),
                               ('before ', '; after'), ('before ', '. after'),
                               ('inline `', '` after'), ('before (', ') after'),
                               ("example '", "' after"), ('example "', '" after')):
            with self.subTest(prefix=prefix):
                sanitized, _ = ledger.sanitize_text(prefix + payload + suffix, max_chars=None)
                self.assertEqual(sanitized, prefix + safe + suffix)

    def test_balanced_non_json_values_keep_the_following_text(self) -> None:
        for value in ('{not-json}', '[placeholder]', "{'value': 'DICT_SECRET', 'nested': [1, '}']}",
                      "{'label': 'satır\u2028iki',\n 'value': 'UNICODE_SECRET'}",
                      "{'label': 'satır',\r\n 'value': 'UNICODE_SECRET'}",
                      "{'value': '" + 'LONG_SECRET' * 100 + "'}",
                      '[' * 1100 + '"DEEP_SECRET"' + ']' * 1100):
            with self.subTest(value=value):
                sanitized, _ = ledger.sanitize_text('token=' + value + ' important decision', max_chars=None)
                self.assertEqual(sanitized, 'token=<REDACTED> important decision')

    def test_ambiguous_attachment_is_rejected_without_model_or_note_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            original = b'{"token":{"safe":1} AMBIGUOUS_SECRET} important decision'
            source.write_bytes(original)
            envelope = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summarize = mock.Mock()
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                with self.assertRaisesRegex(ledger.MemoryPreferenceError, '^memory-credential-container-unverifiable$'):
                    attachment_memory.capture_sources(
                        [('user', envelope)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize)
            summarize.assert_not_called()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(root.rglob('*.md')), [])

    def test_sanitizer_redacts_plain_quoted_and_escaped_credentials(self) -> None:
        text = (
            'plain password=plain-secret; '
            '{"password":"json-secret","api_key":"key-secret",'
            '"token":"token-secret"}; '
            "{'password': 'dict-secret'}; "
            r'{"password":"prefix\"LEAK_PROBE_ESCAPED"}; '
            'keep "password" as an ordinary quoted field name.'
        )

        sanitized, redactions = ledger.sanitize_text(text)

        for marker in MARKERS:
            self.assertNotIn(marker, sanitized)
        self.assertIn('keep "password" as an ordinary quoted field name.', sanitized)
        self.assertIn("credential", redactions)

    def test_quoted_authorization_keys_use_the_existing_authorization_redaction(self) -> None:
        text = (
            'Authorization: Bearer plain-auth; '
            '"authorization": "Bearer json-auth"; '
            "'authorization': 'Bearer dict-auth'; "
            '"authorization": "Bearer FIRST_SECRET token=SECOND_SECRET"'
        )

        sanitized, redactions = ledger.sanitize_text(text)

        self.assertNotIn("plain-auth", sanitized)
        self.assertNotIn("json-auth", sanitized)
        self.assertNotIn("dict-auth", sanitized)
        self.assertNotIn("FIRST_SECRET", sanitized)
        self.assertNotIn("SECOND_SECRET", sanitized)
        self.assertIn("authorization", redactions)

    def test_quoted_json_credential_is_secret_and_does_not_persist(self) -> None:
        text = '{"password":"LEAK_PROBE_SYNTHETIC"}'

        self.assertEqual(ledger.memory_directive(text).kind, "secret")
        self.assertEqual(
            ledger.persistent_turns(
                [("user", text), ("assistant", "Parolanı gördüm."), ("user", "Kalıcı karar.")]
            ),
            [("user", "Kalıcı karar.")],
        )

    def test_attachment_model_and_saved_source_receive_sanitized_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            source = home / "attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt"
            source.parent.mkdir(parents=True)
            original = "example '" + json.dumps(
                {"message": "example 'token': {'a': 1} in docs", "nested": json.dumps(
                    {"api_key": "INNER_SYNTHETIC_SECRET"}
                ), "password": "LEAK_PROBE_SYNTHETIC", "api_key": "ESCAPED_KEY_SYNTHETIC_SECRET",
                 "keep": "ordinary", "token": {"value": "OBJECT_SECRET", "items": ["ARRAY_SECRET"]}},
                ensure_ascii=False,
            ) + "' after"
            original = original.replace('"api_key":', r'"\u0061pi_key":', 1)
            original_bytes = original.encode("utf-8")
            source.write_bytes(original_bytes)
            envelope = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = "\n\n".join(f"## {section}\nGüvenli kaynak özeti." for section in flush.EXPECTED_SECTIONS)
            prompts: list[str] = []

            def summarize(prompt: str) -> str:
                prompts.append(prompt)
                return summary

            with mock.patch.dict("os.environ", {"CODEX_HOME": str(home)}):
                result = attachment_memory.capture_sources(
                    [("user", envelope)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize
                )
                self.assertTrue(result)
                self.assertTrue(prompts)
                self.assertNotIn("LEAK_PROBE_SYNTHETIC", prompts[0])
                self.assertNotIn("key-secret", prompts[0])
                self.assertNotIn("INNER_SYNTHETIC_SECRET", prompts[0])
                self.assertNotIn("ESCAPED_KEY_SYNTHETIC_SECRET", prompts[0])
                self.assertNotIn("OBJECT_SECRET", prompts[0])
                self.assertNotIn("ARRAY_SECRET", prompts[0])
                self.assertIn('"message": "<REDACTED>"', prompts[0])
                note = root / (result[0][0] + ".md")
                saved = note.read_text(encoding="utf-8")
                self.assertNotIn("LEAK_PROBE_SYNTHETIC", saved)
                self.assertNotIn("key-secret", saved)
                self.assertNotIn("INNER_SYNTHETIC_SECRET", saved)
                self.assertNotIn("ESCAPED_KEY_SYNTHETIC_SECRET", saved)
                self.assertNotIn("OBJECT_SECRET", saved)
                self.assertNotIn("ARRAY_SECRET", saved)
                self.assertIn('"message": "<REDACTED>"', saved)
                self.assertIn('"keep": "ordinary"', saved)
                self.assertEqual(source.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
