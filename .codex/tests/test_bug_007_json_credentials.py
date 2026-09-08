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
            "'authorization': 'Bearer dict-auth'"
        )

        sanitized, redactions = ledger.sanitize_text(text)

        self.assertNotIn("plain-auth", sanitized)
        self.assertNotIn("json-auth", sanitized)
        self.assertNotIn("dict-auth", sanitized)
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
            original = json.dumps(
                {"password": "LEAK_PROBE_SYNTHETIC", "api_key": "key-secret", "keep": "ordinary"},
                ensure_ascii=False,
            )
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
                note = root / (result[0][0] + ".md")
                saved = note.read_text(encoding="utf-8")
                self.assertNotIn("LEAK_PROBE_SYNTHETIC", saved)
                self.assertNotIn("key-secret", saved)
                self.assertIn('"keep": "ordinary"', saved)
                self.assertEqual(source.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
