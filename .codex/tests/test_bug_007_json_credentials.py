from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
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

    def test_json_value_credentials_preserve_decoded_context(self) -> None:
        cases = (
            (
                "example 'authorization': 'Bearer AUTH_JSON_SYNTHETIC_SECRET'",
                "example 'authorization': \"Bearer <REDACTED>\"",
                "AUTH_JSON_SYNTHETIC_SECRET",
                "authorization",
            ),
            (
                "decision A; token=SCALAR_JSON_SYNTHETIC_SECRET; decision B",
                "decision A; token=<REDACTED> decision B",
                "SCALAR_JSON_SYNTHETIC_SECRET",
                "credential",
            ),
        )
        for message, expected, secret, category in cases:
            with self.subTest(secret=secret):
                original = json.dumps({"message": message, "keep": "ordinary"})
                sanitized, redactions = ledger.sanitize_text(original, max_chars=None)
                self.assertEqual(json.loads(sanitized), {"message": expected, "keep": "ordinary"})
                self.assertNotIn(secret, sanitized)
                self.assertIn(category, redactions)

    def test_authorization_keys_are_redacted_in_nested_json(self) -> None:
        cases = (
            (
                '{"authorization":"Bearer TOPSECRET","keep":"ordinary"}',
                {"authorization": "Bearer <REDACTED>", "keep": "ordinary"},
                "TOPSECRET",
                "authorization",
            ),
            (
                r'{"\u0061uthorization":"Bearer ESCAPED_AUTH_SECRET","keep":"ordinary"}',
                {"authorization": "Bearer <REDACTED>", "keep": "ordinary"},
                "ESCAPED_AUTH_SECRET",
                "authorization",
            ),
            (
                r'{"authorization":"Be\u0061rer\u0020ESCAPED_BEARER_SECRET","keep":"ordinary"}',
                {"authorization": "Bearer <REDACTED>", "keep": "ordinary"},
                "ESCAPED_BEARER_SECRET",
                "authorization",
            ),
            (
                r'{"authorization":"Bearer\tTAB_AUTH_SECRET","keep":"ordinary"}',
                {"authorization": "Bearer <REDACTED>", "keep": "ordinary"},
                "TAB_AUTH_SECRET",
                "authorization",
            ),
            (
                r'{"authorization":"Bearer\nLINE_AUTH_SECRET","keep":"ordinary"}',
                {"authorization": "Bearer <REDACTED>", "keep": "ordinary"},
                "LINE_AUTH_SECRET",
                "authorization",
            ),
            (
                r'{"authorization":"Bearer\u00a0NBSP_AUTH_SECRET","keep":"ordinary"}',
                {"authorization": "Bearer <REDACTED>", "keep": "ordinary"},
                "NBSP_AUTH_SECRET",
                "authorization",
            ),
            (
                json.dumps({"outer": [{"authorization": "Bearer NESTED_AUTH_SECRET"}], "keep": "ordinary"}),
                {"outer": [{"authorization": "Bearer <REDACTED>"}], "keep": "ordinary"},
                "NESTED_AUTH_SECRET",
                "authorization",
            ),
            (
                json.dumps({"book": "Bearer of good news", "keep": "ordinary"}),
                {"book": "Bearer of good news", "keep": "ordinary"},
                None,
                None,
            ),
        )
        for original, expected, secret, category in cases:
            with self.subTest(secret=secret):
                sanitized, redactions = ledger.sanitize_text(original, max_chars=None)
                self.assertEqual(json.loads(sanitized), expected)
                if secret is not None:
                    self.assertNotIn(secret, sanitized)
                if category is None:
                    self.assertEqual(redactions, ())
                else:
                    self.assertIn(category, redactions)

    def test_decoded_json_credential_names_are_redacted(self) -> None:
        nested = json.dumps({"api_key": "INNER_SYNTHETIC_SECRET"})
        fragment = "example " + json.dumps({"api_key": "PROSE_JSON_SYNTHETIC_SECRET"}) + " in docs"
        escaped_key = r'{"\u0061pi_key":"ESCAPED_KEY_SYNTHETIC_SECRET","keep":"ordinary"}'
        for original, expected, secret in (
            (
                json.dumps({"message": nested, "keep": "ordinary"}),
                {"message": json.dumps({"api_key": "<REDACTED>"}), "keep": "ordinary"},
                "INNER_SYNTHETIC_SECRET",
            ),
            (
                json.dumps({"message": fragment, "keep": "ordinary"}),
                {"message": "example " + json.dumps({"api_key": "<REDACTED>"}) + " in docs", "keep": "ordinary"},
                "PROSE_JSON_SYNTHETIC_SECRET",
            ),
            (escaped_key, {"api_key": "<REDACTED>", "keep": "ordinary"}, "ESCAPED_KEY_SYNTHETIC_SECRET"),
        ):
            with self.subTest(secret=secret):
                sanitized, redactions = ledger.sanitize_text(original, max_chars=None)
                self.assertEqual(json.loads(sanitized), expected)
                self.assertNotIn(secret, sanitized)
                self.assertIn("credential", redactions)

    def test_malformed_decoded_json_credentials_are_not_leaked(self) -> None:
        for inner, secret in (
            ('{"api_key":"MALFORMED_INNER_SECRET"', "MALFORMED_INNER_SECRET"),
            (r'{"\u0061pi_key":"ESCAPED_MALFORMED_SECRET"', "ESCAPED_MALFORMED_SECRET"),
        ):
            with self.subTest(secret=secret):
                original = json.dumps({"message": inner, "keep": "ordinary"})
                sanitized, redactions = ledger.sanitize_text(original, max_chars=None)
                self.assertNotIn(secret, sanitized)
                self.assertEqual(json.loads(sanitized)["keep"], "ordinary")
                self.assertIn("credential", redactions)

    def test_malformed_outer_container_does_not_hide_inner_credentials(self) -> None:
        for field in (r'"\u0061pi_key":"NESTED_SECRET"',
                      r'"\u0061uthorization":"Bearer NESTED_SECRET"'):
            for original in ('[{' + field + ',"keep":"ordinary"} BROKEN]',
                             '{' + field + ', BROKEN}',
                             '{BROKEN,' + field + '}',
                             '{BROKEN,"safe":{},' + field + '}'):
                with self.subTest(original=original), self.assertRaisesRegex(
                    ledger.MemoryPreferenceError, '^memory-credential-container-unverifiable$'
                ):
                    ledger.sanitize_text(original, max_chars=None)

    def test_top_level_and_malformed_embedded_json_strings_hide_credentials(self) -> None:
        for payload in ('{"api_key":"STRING_SECRET"}',
                        'password=STRING_SECRET',
                        'Authorization: Bearer STRING_SECRET'):
            original = json.dumps(payload).replace('=', r'\u003d').replace(':', r'\u003a')
            sanitized, redactions = ledger.sanitize_text(' ' + original + '\n', max_chars=None)
            self.assertNotIn('STRING_SECRET', sanitized)
            self.assertTrue(redactions)
            self.assertEqual(json.loads(sanitized), ledger.sanitize_text(payload, max_chars=None)[0])
            malformed = '{BROKEN,"message":' + original + '}'
            with self.assertRaisesRegex(ledger.MemoryPreferenceError, '^memory-credential-container-unverifiable$'):
                ledger.sanitize_text(malformed, max_chars=None)

    def test_deep_json_does_not_reparse_every_subtree(self) -> None:
        script = (
            'import sys; '
            f'sys.path.insert(0, {str(CODEX_DIR / "scripts")!r}); '
            'from memory_ledger import MemoryPreferenceError, sanitize_text; '
            'text = \'{"x":\' * 5000 + "0" + "}" * 5000; '
            'assert sanitize_text(text, max_chars=None) == (text, ())\n'
            'try: sanitize_text(text[:-5000], max_chars=None)\n'
            'except MemoryPreferenceError: pass\n'
            'else: raise AssertionError("unbounded malformed JSON retries")'
        )
        subprocess.run([sys.executable, '-X', 'utf8', '-c', script], check=True, timeout=5)

    def test_labeled_credentials_cross_complete_json_string_spans(self) -> None:
        for label in ('parolam şu ', 'Authorization: Bearer '):
            for payload in ({'value': 'LABEL_SECRET'}, ['LABEL_SECRET']):
                for indent in (None, 2):
                    with self.subTest(label=label, payload=payload, indent=indent):
                        text = label + json.dumps(payload, indent=indent) + '\nkeep ordinary'
                        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)
                        self.assertNotIn('LABEL_SECRET', sanitized)
                        self.assertIn('keep ordinary', sanitized)
                        self.assertTrue(redactions)
            with self.assertRaisesRegex(ledger.MemoryPreferenceError, '^memory-credential-container-unverifiable$'):
                ledger.sanitize_text(label + '{\n"value": "LABEL_SECRET"', max_chars=None)

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

    def test_environment_credential_assignments_are_non_persistent(self) -> None:
        text = (
            'DATABASE_PASSWORD="P02_DATABASE_PASSWORD_CANARY"\n'
            "MY_TOKEN='P02_MY_TOKEN_CANARY'\n"
            'AWS_SECRET_ACCESS_KEY=P02_AWS_SECRET_ACCESS_KEY_CANARY\n'
            'keep=ordinary'
        )

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(
            sanitized,
            'DATABASE_PASSWORD=<REDACTED>\n'
            'MY_TOKEN=<REDACTED>\n'
            'AWS_SECRET_ACCESS_KEY=<REDACTED>\n'
            'keep=ordinary',
        )
        self.assertEqual(redactions, ('credential',))
        self.assertTrue(ledger.contains_secret(text))
        self.assertEqual(ledger.memory_directive(text).kind, 'secret')
        self.assertEqual(
            ledger.persistent_turns(
                [('user', text), ('assistant', 'P02_MY_TOKEN_CANARY'), ('user', 'Sonraki karar.')]
            ),
            [('user', 'Sonraki karar.')],
        )

    def test_environment_credential_values_consume_multiline_quotes(self) -> None:
        text = (
            'DATABASE_PASSWORD="first line\nP02_DATABASE_PASSWORD_MULTILINE_CANARY\nlast line"\n'
            "MY_TOKEN='first line\nP02_MY_TOKEN_MULTILINE_CANARY\nlast line'\n"
            'AWS_SECRET_ACCESS_KEY="first line\r\nP02_AWS_SECRET_ACCESS_KEY_MULTILINE_CANARY\r\nlast line"\n'
            'keep=ordinary'
        )

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(
            sanitized,
            'DATABASE_PASSWORD=<REDACTED>\n'
            'MY_TOKEN=<REDACTED>\n'
            'AWS_SECRET_ACCESS_KEY=<REDACTED>\n'
            'keep=ordinary',
        )
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('MULTILINE_CANARY', sanitized)

    def test_unterminated_multiline_credential_quote_fails_closed(self) -> None:
        text = 'DATABASE_PASSWORD="first line\nP02_UNTERMINATED_CANARY\nlast line'

        with self.assertRaisesRegex(
            ledger.MemoryPreferenceError,
            '^memory-credential-container-unverifiable$',
        ):
            ledger.sanitize_text(text, max_chars=None)

    def test_windows_batch_credential_assignment_consumes_outer_quote(self) -> None:
        for value in ('FIRST_SECRET', 'FIRST_SECRET SECOND_SECRET'):
            with self.subTest(value=value):
                text = f'set "DATABASE_PASSWORD={value}"\nkeep=ordinary'

                sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

                self.assertEqual(
                    sanitized,
                    'set "DATABASE_PASSWORD=<REDACTED>"\nkeep=ordinary',
                )
                self.assertEqual(redactions, ('credential',))
                self.assertNotIn('FIRST_SECRET', sanitized)
                self.assertNotIn('SECOND_SECRET', sanitized)

    def test_unterminated_windows_batch_credential_quote_fails_closed(self) -> None:
        text = 'set "DATABASE_PASSWORD=FIRST_SECRET\nP02_UNTERMINATED_BATCH_CANARY'

        with self.assertRaisesRegex(
            ledger.MemoryPreferenceError,
            '^memory-credential-container-unverifiable$',
        ):
            ledger.sanitize_text(text, max_chars=None)

    def test_unquoted_windows_batch_credential_value_consumes_full_line(self) -> None:
        cases = (
            (
                'set DATABASE_PASSWORD=FIRST_SECRET SECOND_SECRET\nkeep=ordinary',
                'set DATABASE_PASSWORD=<REDACTED>\nkeep=ordinary',
            ),
            (
                'set DATABASE_PASSWORD=FIRST_SECRET SECOND_SECRET\r\nkeep=ordinary',
                'set DATABASE_PASSWORD=<REDACTED>\r\nkeep=ordinary',
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

                self.assertEqual(sanitized, expected)
                self.assertEqual(redactions, ('credential',))
                self.assertTrue(ledger.contains_secret(text))
                self.assertEqual(ledger.memory_directive(text).kind, 'secret')
                self.assertNotIn('FIRST_SECRET', sanitized)
                self.assertNotIn('SECOND_SECRET', sanitized)

    def test_windows_batch_credential_names_are_case_insensitive(self) -> None:
        cases = (
            (
                'set Database_Password=FIRST_SECRET SECOND_SECRET',
                'set Database_Password=<REDACTED>',
            ),
            (
                'set database_password=FIRST_SECRET SECOND_SECRET',
                'set database_password=<REDACTED>',
            ),
            (
                'set "Database_Password=FIRST_SECRET SECOND_SECRET"',
                'set "Database_Password=<REDACTED>"',
            ),
            (
                'set "database_password=FIRST_SECRET SECOND_SECRET"',
                'set "database_password=<REDACTED>"',
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                sanitized, redactions = ledger.sanitize_text(text, max_chars=None)
                self.assertEqual(sanitized, expected)
                self.assertEqual(redactions, ('credential',))
                self.assertTrue(ledger.contains_secret(text))
                self.assertEqual(ledger.memory_directive(text).kind, 'secret')
                self.assertNotIn('FIRST_SECRET', sanitized)
                self.assertNotIn('SECOND_SECRET', sanitized)

    def test_posix_credential_assignment_consumes_adjacent_quoted_segments(self) -> None:
        text = 'DATABASE_PASSWORD="FIRST_""SECOND SECRET"\nkeep=ordinary'

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(
            sanitized,
            'DATABASE_PASSWORD=<REDACTED>\nkeep=ordinary',
        )
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('FIRST_', sanitized)
        self.assertNotIn('SECOND SECRET', sanitized)

        with self.assertRaisesRegex(
            ledger.MemoryPreferenceError,
            '^memory-credential-container-unverifiable$',
        ):
            ledger.sanitize_text('DATABASE_PASSWORD="FIRST_""SECOND', max_chars=None)

    def test_unquoted_windows_batch_assignment_preserves_commands_and_escaped_separators(self) -> None:
        cases = (
            (
                'set DATABASE_PASSWORD=FIRST_SECRET && echo keep-decision',
                'set DATABASE_PASSWORD=<REDACTED> && echo keep-decision',
            ),
            (
                r'set DATABASE_PASSWORD=FIRST ^&^& SECOND_SECRET && echo keep-decision',
                'set DATABASE_PASSWORD=<REDACTED> && echo keep-decision',
            ),
            (
                'set DATABASE_PASSWORD=FIRST;SECOND_SECRET',
                'set DATABASE_PASSWORD=<REDACTED>',
            ),
            (
                'set DATABASE_PASSWORD="FIRST&SECOND_SECRET"',
                'set DATABASE_PASSWORD=<REDACTED>',
            ),
            (
                'set DATABASE_PASSWORD=FIRST_SECRET && set MY_TOKEN=SECOND_SECRET && echo keep-decision',
                'set DATABASE_PASSWORD=<REDACTED> && set MY_TOKEN=<REDACTED> && echo keep-decision',
            ),
            (
                '@set DATABASE_PASSWORD=FIRST_SECRET SECOND_SECRET',
                '@set DATABASE_PASSWORD=<REDACTED>',
            ),
            (
                'echo keep-decision && @set DATABASE_PASSWORD=FIRST_SECRET SECOND_SECRET',
                'echo keep-decision && @set DATABASE_PASSWORD=<REDACTED>',
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                sanitized, redactions = ledger.sanitize_text(text, max_chars=None)
                self.assertEqual(sanitized, expected)
                self.assertEqual(redactions, ('credential',))
                self.assertNotIn('FIRST_SECRET', sanitized)
                self.assertNotIn('SECOND_SECRET', sanitized)
                if 'keep-decision' in text:
                    self.assertIn('keep-decision', sanitized)

    def test_unverifiable_unquoted_windows_batch_assignment_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            ledger.MemoryPreferenceError,
            '^memory-credential-container-unverifiable$',
        ):
            ledger.sanitize_text('set DATABASE_PASSWORD=FIRST_SECRET^\nnext line', max_chars=None)

    def test_powershell_environment_credential_names_are_case_insensitive(self) -> None:
        cases = (
            (
                '$env:Database_Password="FIRST_SECRET SECOND_SECRET"',
                '$env:Database_Password=<REDACTED>',
            ),
            (
                "$ENV:database_password='FIRST_SECRET SECOND_SECRET'",
                '$ENV:database_password=<REDACTED>',
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                sanitized, redactions = ledger.sanitize_text(text, max_chars=None)
                self.assertEqual(sanitized, expected)
                self.assertEqual(redactions, ('credential',))
                self.assertTrue(ledger.contains_secret(text))
                self.assertEqual(ledger.memory_directive(text).kind, 'secret')
                self.assertNotIn('FIRST_SECRET', sanitized)
                self.assertNotIn('SECOND_SECRET', sanitized)

    def test_powershell_braced_environment_credential_names_are_redacted(self) -> None:
        text = '${env:Database_Password} = "FIRST_SECRET SECOND_SECRET"'

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(
            sanitized,
            '${env:Database_Password} = <REDACTED>',
        )
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('FIRST_SECRET', sanitized)
        self.assertNotIn('SECOND_SECRET', sanitized)

    def test_powershell_here_string_credential_body_is_redacted(self) -> None:
        text = "$env:DATABASE_PASSWORD=@'\nFIRST_SECRET SECOND_SECRET\n'@\nkeep=ordinary"

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(
            sanitized,
            '$env:DATABASE_PASSWORD=<REDACTED>\nkeep=ordinary',
        )
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('FIRST_SECRET', sanitized)
        self.assertNotIn('SECOND_SECRET', sanitized)

        with self.assertRaisesRegex(
            ledger.MemoryPreferenceError,
            '^memory-credential-container-unverifiable$',
        ):
            ledger.sanitize_text("$env:DATABASE_PASSWORD=@'\nFIRST_SECRET", max_chars=None)

    def test_powershell_quoted_value_honors_backtick_quote_escape(self) -> None:
        text = '$env:DATABASE_PASSWORD="FIRST`" SECOND_SECRET"'

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(sanitized, '$env:DATABASE_PASSWORD=<REDACTED>')
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('FIRST', sanitized)
        self.assertNotIn('SECOND_SECRET', sanitized)

    def test_powershell_unsupported_rhs_fails_closed_and_semicolon_keeps_tail(self) -> None:
        with self.assertRaisesRegex(
            ledger.MemoryPreferenceError,
            '^memory-credential-container-unverifiable$',
        ):
            ledger.sanitize_text(
                "$env:DATABASE_PASSWORD='FIRST_SECRET' + 'SECOND_SECRET'",
                max_chars=None,
            )

        sanitized, redactions = ledger.sanitize_text(
            "$env:DATABASE_PASSWORD='FIRST_SECRET'; echo keep-decision",
            max_chars=None,
        )

        self.assertEqual(
            sanitized,
            '$env:DATABASE_PASSWORD=<REDACTED>; echo keep-decision',
        )
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('FIRST_SECRET', sanitized)
        self.assertIn('keep-decision', sanitized)

    def test_posix_ansi_c_quoted_credential_value_is_redacted(self) -> None:
        text = "DATABASE_PASSWORD=$'FIRST_SECRET SECOND_SECRET'"

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(sanitized, 'DATABASE_PASSWORD=<REDACTED>')
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('FIRST_SECRET', sanitized)
        self.assertNotIn('SECOND_SECRET', sanitized)

        with self.assertRaisesRegex(
            ledger.MemoryPreferenceError,
            '^memory-credential-container-unverifiable$',
        ):
            ledger.sanitize_text("DATABASE_PASSWORD=$'FIRST_SECRET", max_chars=None)

    def test_batch_assignment_only_matches_command_positions(self) -> None:
        text = 'Please set DATABASE_PASSWORD=FIRST_SECRET then keep this decision'

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(
            sanitized,
            'Please set DATABASE_PASSWORD=<REDACTED> then keep this decision',
        )
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('FIRST_SECRET', sanitized)

    def test_batch_assignment_after_if_and_for_is_bounded(self) -> None:
        cases = (
            (
                'if 1==1 set DATABASE_PASSWORD=FIRST_SECRET SECOND_SECRET',
                'if 1==1 set DATABASE_PASSWORD=<REDACTED>',
            ),
            (
                'for %x in (1) do set DATABASE_PASSWORD=FIRST_SECRET SECOND_SECRET',
                'for %x in (1) do set DATABASE_PASSWORD=<REDACTED>',
            ),
            (
                'if 1==1 set DATABASE_PASSWORD=FIRST_SECRET&&echo keep-decision',
                'if 1==1 set DATABASE_PASSWORD=<REDACTED>&&echo keep-decision',
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                sanitized, redactions = ledger.sanitize_text(text, max_chars=None)
                self.assertEqual(sanitized, expected)
                self.assertEqual(redactions, ('credential',))
                self.assertNotIn('FIRST_SECRET', sanitized)
                self.assertNotIn('SECOND_SECRET', sanitized)

    def test_unterminated_windows_batch_quote_does_not_use_later_line_quote(self) -> None:
        text = 'set "DATABASE_PASSWORD=FIRST_SECRET\nkeep this decision "quoted" tail'

        with self.assertRaisesRegex(
            ledger.MemoryPreferenceError,
            '^memory-credential-container-unverifiable$',
        ):
            ledger.sanitize_text(text, max_chars=None)

    def test_windows_batch_quote_treats_backslash_literally(self) -> None:
        text = r'set "DATABASE_PASSWORD=C:\secret\"'

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(sanitized, 'set "DATABASE_PASSWORD=<REDACTED>"')
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('C:\\secret\\', sanitized)

    def test_unquoted_credential_value_consumes_escaped_whitespace(self) -> None:
        text = r'DATABASE_PASSWORD=FIRST\ SECOND_SECRET'

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(sanitized, 'DATABASE_PASSWORD=<REDACTED>')
        self.assertEqual(redactions, ('credential',))
        self.assertNotIn('SECOND_SECRET', sanitized)

    def test_batch_skip_keeps_buffered_context_from_earlier_credential(self) -> None:
        text = 'password=foo\nkeep this decision\nset "DATABASE_PASSWORD=bar"\nimportant tail'

        sanitized, redactions = ledger.sanitize_text(text, max_chars=None)

        self.assertEqual(
            sanitized,
            'password=<REDACTED>\n'
            'keep this decision\n'
            'set "DATABASE_PASSWORD=<REDACTED>"\n'
            'important tail',
        )
        self.assertEqual(redactions, ('credential',))

    def test_normal_suffixed_identifiers_are_not_secrets(self) -> None:
        for text in (
            'max_token=4096',
            'expected_claim_token=claim-value',
            'MAX_TOKEN=4096',
            'EXPECTED_CLAIM_TOKEN=claim-value',
            'max_token=4096',
            'expected_claim_token=claim-value',
            'Database_Password=FIRST_SECRET',
            'set "max_token=4096"',
            'set "expected_claim_token=claim-value"',
            'set "MAX_TOKEN=4096"',
            'set "EXPECTED_CLAIM_TOKEN=claim-value"',
        ):
            with self.subTest(text=text):
                self.assertEqual(ledger.sanitize_text(text, max_chars=None), (text, ()))
                self.assertFalse(ledger.contains_secret(text))
                self.assertEqual(ledger.memory_directive(text).kind, 'ordinary')
                self.assertEqual(
                    ledger.persistent_turns([('user', text), ('assistant', 'ordinary reply')]),
                    [('user', text), ('assistant', 'ordinary reply')],
                )

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
        for text in ('{"password":"LEAK_PROBE_SYNTHETIC"}',
                     r'{"\u0061pi_key":"LEAK_PROBE_SYNTHETIC"}',
                     json.dumps({'message': r'{"\u0061pi_key":"LEAK_PROBE_SYNTHETIC"}'}),
                     r'{BROKEN,"\u0061pi_key":"LEAK_PROBE_SYNTHETIC"}'):
            with self.subTest(text=text):
                self.assertEqual(ledger.memory_directive(text).kind, "secret")
                self.assertEqual(
                    ledger.persistent_turns(
                        [("user", text), ("assistant", "LEAK_PROBE_SYNTHETIC"), ("user", "Kalıcı karar.")]
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
                ), "fragment": "example " + json.dumps({"api_key": "PROSE_JSON_SYNTHETIC_SECRET"}) + " in docs",
                 "auth": "example 'authorization': 'Bearer AUTH_ATTACHMENT_SYNTHETIC_SECRET'",
                 "decision": "decision A; token=SCALAR_ATTACHMENT_SYNTHETIC_SECRET; decision B",
                 "password": "LEAK_PROBE_SYNTHETIC", "api_key": "ESCAPED_KEY_SYNTHETIC_SECRET",
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
                self.assertNotIn("PROSE_JSON_SYNTHETIC_SECRET", prompts[0])
                self.assertNotIn("AUTH_ATTACHMENT_SYNTHETIC_SECRET", prompts[0])
                self.assertNotIn("SCALAR_ATTACHMENT_SYNTHETIC_SECRET", prompts[0])
                self.assertNotIn("ESCAPED_KEY_SYNTHETIC_SECRET", prompts[0])
                self.assertNotIn("OBJECT_SECRET", prompts[0])
                self.assertNotIn("ARRAY_SECRET", prompts[0])
                self.assertIn('"message": "<REDACTED>"', prompts[0])
                self.assertIn("decision A", prompts[0])
                self.assertIn("decision B", prompts[0])
                note = root / (result[0][0] + ".md")
                saved = note.read_text(encoding="utf-8")
                self.assertNotIn("LEAK_PROBE_SYNTHETIC", saved)
                self.assertNotIn("key-secret", saved)
                self.assertNotIn("INNER_SYNTHETIC_SECRET", saved)
                self.assertNotIn("PROSE_JSON_SYNTHETIC_SECRET", saved)
                self.assertNotIn("AUTH_ATTACHMENT_SYNTHETIC_SECRET", saved)
                self.assertNotIn("SCALAR_ATTACHMENT_SYNTHETIC_SECRET", saved)
                self.assertNotIn("ESCAPED_KEY_SYNTHETIC_SECRET", saved)
                self.assertNotIn("OBJECT_SECRET", saved)
                self.assertNotIn("ARRAY_SECRET", saved)
                self.assertIn('"message": "<REDACTED>"', saved)
                self.assertIn("decision A", saved)
                self.assertIn("decision B", saved)
                self.assertIn('"keep": "ordinary"', saved)
                self.assertEqual(source.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
