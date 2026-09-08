import hashlib
from pathlib import Path
import tempfile
import unittest

import _fixtures  # noqa: F401
import compile as memory_compile
import memory_ledger
from test_second_brain_acceptance import _deterministic_compiler, _seed_vault


class CompilerSecretRedactionTests(unittest.TestCase):
    def test_stage_memory_redacts_with_no_or_unrelated_suppression(self):
        for hashes in (
            frozenset(),
            frozenset({memory_ledger.memory_text_hash("unrelated")}),
        ):
            with self.subTest(hashes=hashes), tempfile.TemporaryDirectory() as temporary:
                stage = Path(temporary)
                files = (
                    stage / "daily/2026-09-03.md",
                    stage / "knowledge/index.md",
                    stage / "knowledge/log.md",
                    stage / "knowledge/concepts/note.md",
                    stage / "knowledge/connections/link.md",
                )
                for path in files:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"{path.name}: password=synthetic\n", encoding="utf-8")

                self.assertTrue(memory_compile._project_stage_memory(stage, hashes))
                for path in files:
                    text = path.read_text(encoding="utf-8")
                    self.assertNotIn("password=synthetic", text)
                    self.assertIn("password=<REDACTED>", text)

    def test_compile_redacts_generation_and_repair_output_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            _seed_vault(vault)
            source = vault / "daily/2026-09-03.md"
            source.write_text(
                source.read_text(encoding="utf-8") + "\npassword=source-synthetic\n",
                encoding="utf-8",
            )
            original_source = source.read_bytes()
            calls = []
            visible_snapshots = []

            def runner(prompt, stage):
                repair = prompt.startswith("BELLEK ŞEMASI ONARIMI")
                calls.append(repair)
                visible = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in (*stage.glob("daily/*.md"), *stage.glob("knowledge/**/*.md"))
                )
                visible_snapshots.append((repair, prompt, visible))
                self.assertNotIn("synthetic", prompt)
                self.assertNotIn("synthetic", visible)

                _deterministic_compiler(prompt, stage)
                note = stage / "knowledge/concepts/yerel-hafiza.md"
                secret = "repair-synthetic" if repair else "generated-synthetic"
                note.write_text(
                    note.read_text(encoding="utf-8") + f"\npassword={secret}\n",
                    encoding="utf-8",
                )
                if not repair:
                    note.write_text(
                        note.read_text(encoding="utf-8").replace(
                            "schema: knowledge-v2", "schema: invalid"
                        ),
                        encoding="utf-8",
                    )

            reason, detail = memory_compile._compile_one(
                vault,
                vault / ".codex/scripts/.state",
                source,
                hashlib.sha256(original_source).hexdigest(),
                "2026-09-08T00:00:00+03:00",
                runner=runner,
            )

            self.assertIsNone(reason, detail)
            self.assertEqual(calls, [False, True])
            self.assertEqual(len(visible_snapshots), 2)
            self.assertEqual(source.read_bytes(), original_source)
            promoted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (vault / "knowledge").rglob("*.md")
            )
            self.assertNotIn("source-synthetic", promoted)
            self.assertNotIn("generated-synthetic", promoted)
            self.assertNotIn("repair-synthetic", promoted)
            self.assertIn("password=<REDACTED>", promoted)


if __name__ == "__main__":
    unittest.main()
