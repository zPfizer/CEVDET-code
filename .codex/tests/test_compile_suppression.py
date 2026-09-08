import hashlib
from pathlib import Path
import tempfile
import unittest

from _fixtures import CODEX_DIR
import compile as memory_compile
import memory_ledger
from test_second_brain_acceptance import _seed_vault


class CompileSuppressionTests(unittest.TestCase):
    def test_compile_skips_suppressed_daily_source_by_full_path_and_basename(self):
        for target in ("daily/2026-09-03.md", "2026-09-03.md"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                _seed_vault(vault)
                source = vault / "daily/2026-09-03.md"
                original = source.read_bytes()
                memory_ledger.suppress_derived_memory(
                    vault / ".codex/private-memory", target
                )
                calls = []

                def runner(_prompt, _stage):
                    calls.append(True)
                    raise AssertionError("suppressed source reached model runner")

                reason, detail = memory_compile._compile_one(
                    vault,
                    vault / ".codex/scripts/.state",
                    source,
                    hashlib.sha256(original).hexdigest(),
                    "2026-09-08T00:00:00+03:00",
                    runner=runner,
                )

                self.assertIsNone(reason)
                self.assertEqual(detail, "memory-source-excluded")
                self.assertEqual(calls, [])
                self.assertEqual(source.read_bytes(), original)

    def test_project_stage_memory_excludes_suppressed_daily_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            daily = stage / "daily/2026-09-03.md"
            daily.parent.mkdir()
            daily.write_text("Kaynak günlük.", encoding="utf-8")
            hashes = frozenset({memory_ledger.memory_text_hash("daily/2026-09-03.md")})

            self.assertTrue(memory_compile._project_stage_memory(stage, hashes))
            self.assertFalse(daily.exists())


if __name__ == "__main__":
    unittest.main()
