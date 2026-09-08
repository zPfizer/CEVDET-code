import datetime as dt
from pathlib import Path
import tempfile
import unittest

import _fixtures  # noqa: F401  # sys.path seam
import hook
import memory_ledger as ledger
from test_quality_pipeline import proof_fixture


NOW = dt.datetime(2026, 9, 8)


class StartupMemoryReadTests(unittest.TestCase):
    def test_session_start_sanitizes_each_unsuppressed_memory_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / "🔮 850-Companion"
            knowledge = vault / "knowledge"
            daily = vault / "daily"
            state = vault / ".codex/scripts/.state"
            for directory in (companion, knowledge, daily, state):
                directory.mkdir(parents=True)

            sources = {
                companion / "Core.md": "password: BUG001-CORE\n",
                companion / "Kurallar.md": "password: BUG001-RULES\n",
                companion / "Last-Session.md": (
                    "# Last Session\n## Session: 2026-09-06\n"
                    "password: BUG001-LAST\n"
                ),
                companion / "Journal.md": (
                    "# Journal\n## Güncel\n### Son kayıt\n"
                    "password: BUG001-JOURNAL\n"
                ),
                knowledge / "index.md": "# Bilgi Tabanı\npassword: BUG001-INDEX\n",
                daily / "2026-09-08.md": (
                    "# Daily\n### Oturum (10:00)\npassword: BUG001-DAILY\n"
                ),
            }
            for path, text in sources.items():
                path.write_text(text, encoding="utf-8")
            originals = {path: path.read_bytes() for path in sources}

            context = hook.build_session_context(vault, state, now=NOW)
            self.assertEqual(
                {path: path.read_bytes() for path in sources}, originals
            )

        for secret in ("BUG001-CORE", "BUG001-RULES", "BUG001-LAST",
                       "BUG001-JOURNAL", "BUG001-INDEX", "BUG001-DAILY"):
            self.assertNotIn(secret, context)
        self.assertIn("<REDACTED>", context)

    def test_session_start_keeps_filtered_view_pointer_when_suppression_is_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = vault / "daily"
            state = vault / ".codex/scripts/.state"
            daily.mkdir(parents=True)
            state.mkdir(parents=True)
            secret = "password: BUG001-SUPPRESSED"
            source = daily / "2026-09-08.md"
            source.write_text(
                "# Daily\n### Oturum (10:00)\n"
                + secret
                + "\n"
                + ("safe startup context. " * 50),
                encoding="utf-8",
            )
            original = source.read_bytes()
            ledger.suppress_derived_memory(vault / ".codex/private-memory", secret)

            context = hook.build_session_context(vault, state, now=NOW)
            self.assertEqual(source.read_bytes(), original)

        self.assertNotIn("BUG001-SUPPRESSED", context)
        self.assertIn("private-memory/views", context)

    def test_session_start_drops_claim_when_its_daily_proof_disappears(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / "🔮 850-Companion"
            state = vault / ".codex/scripts/.state"
            companion.mkdir(parents=True)
            state.mkdir(parents=True)
            _record, row, _note = proof_fixture(vault)
            last_session = companion / "Last-Session.md"
            last_session.write_text(
                "# Last Session\n## Session: 2026-09-06\n" + row + "\n",
                encoding="utf-8",
            )
            (vault / "daily/2026-09-06.md").write_text(
                "# Removed evidence\n", encoding="utf-8"
            )

            context = hook.build_session_context(vault, state, now=NOW)
            visible = ledger.read_memory_source(vault, last_session)

        self.assertNotIn("Kısa yanıt tercih ediliyor.", context)
        self.assertNotIn("Kısa yanıt tercih ediliyor.", visible)


if __name__ == "__main__":
    unittest.main()
