import hashlib
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _fixtures  # noqa: F401
import compile as memory_compile
import memory_ledger
from test_second_brain_acceptance import _deterministic_compiler, _seed_vault


class CompilerSecretRedactionTests(unittest.TestCase):
    def test_secret_path_failure_receipt_does_not_block_next_valid_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            safe = vault / "daily/2026-09-03.md"
            unsafe = vault / "daily/password=synthetic.md"
            unsafe.write_bytes(safe.read_bytes())
            safe.unlink()
            (vault / "daily/2026-09-04.md").unlink()
            state_dir = vault / ".codex/scripts/.state"

            def unexpected_model(_prompt, _stage):
                raise AssertionError("secret path reached model")

            with patch.object(memory_compile, "VAULT_ROOT", vault), \
                    patch.object(memory_compile, "STATE_DIR", state_dir), \
                    patch.object(memory_compile, "_run_codex", side_effect=unexpected_model), \
                    patch.object(memory_compile, "_checkpoint_machine_outputs"):
                self.assertEqual(memory_compile.main(["--strict", "--max-calls", "1"]), 1)

            failed_state = memory_compile.compile_state.load(state_dir)
            self.assertEqual(failed_state.runs[-1]["daily_file"], "<redacted-path>")
            unsafe.rename(safe)

            with patch.object(memory_compile, "VAULT_ROOT", vault), \
                    patch.object(memory_compile, "STATE_DIR", state_dir), \
                    patch.object(memory_compile, "_run_codex", side_effect=_deterministic_compiler), \
                    patch.object(memory_compile, "_checkpoint_machine_outputs", return_value=("clean", "")):
                result = memory_compile.main(["--strict", "--max-calls", "1"])

            state = memory_compile.compile_state.load(state_dir)
            self.assertEqual(result, 0)
            self.assertIn("2026-09-03.md", state.ingested)

    def test_cli_rejects_unsafe_legacy_state_without_rewriting_or_model(self):
        for field in ("ingested", "cursor", "runs"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                _seed_vault(vault)
                state_dir = vault / ".codex/scripts/.state"
                state_path = state_dir / "compile-state.json"
                ingested = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (vault / "daily").glob("*.md")
                }
                state = {
                    "ingested": ingested,
                    "cursor": "2026-09-04.md",
                    "last_run": "",
                    "last_status": "ok",
                    "runs": [],
                }
                if field == "ingested":
                    state["ingested"]["password=synthetic.md"] = "0" * 64
                elif field == "cursor":
                    state["cursor"] = "password=synthetic.md"
                else:
                    state["runs"] = [{"daily_file": "password=synthetic.md", "status": "ok"}]
                state_path.write_text(json.dumps(state), encoding="utf-8")
                original_state = state_path.read_bytes()
                original_source = (vault / "daily/2026-09-03.md").read_bytes()
                calls = []

                def unexpected_model(_prompt, _stage):
                    calls.append("model")
                    raise AssertionError("unsafe legacy state reached model")

                with patch.object(memory_compile, "VAULT_ROOT", vault), \
                        patch.object(memory_compile, "STATE_DIR", state_dir), \
                        patch.object(memory_compile, "_run_codex", side_effect=unexpected_model), \
                        patch.object(memory_compile, "_checkpoint_machine_outputs") as checkpoint:
                    result = memory_compile.main(["--strict", "--max-calls", "1"])

                health = (state_dir / "health.json").read_text(encoding="utf-8")
                self.assertEqual(result, 1)
                self.assertEqual(calls, [])
                checkpoint.assert_not_called()
                self.assertEqual(state_path.read_bytes(), original_state)
                self.assertEqual((vault / "daily/2026-09-03.md").read_bytes(), original_source)
                self.assertIn("compile-state-path-contains-secret", health)
                self.assertNotIn("password=synthetic.md", health)

    def test_dry_run_rejects_secret_source_before_printing_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            source = vault / "daily/2026-09-03.md"
            unsafe = vault / "daily/password=synthetic.md"
            unsafe.write_bytes(source.read_bytes())
            source.unlink()
            (vault / "daily/2026-09-04.md").unlink()
            state_dir = vault / ".codex/scripts/.state"
            output = io.StringIO()

            with redirect_stdout(output), \
                    patch.object(memory_compile, "VAULT_ROOT", vault), \
                    patch.object(memory_compile, "STATE_DIR", state_dir):
                result = memory_compile.main(["--strict", "--dry-run", "--max-calls", "1"])

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")

    def test_rebuild_path_errors_do_not_expose_source_names(self):
        for name, expected in (
            ("password=synthetic.md", "source-path-contains-secret"),
            ("not-a-date.md", "rebuild-daily-name-invalid"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                vault = root / "vault"
                _seed_vault(vault)
                for path in (vault / "daily").glob("*.md"):
                    path.unlink()
                (vault / "daily" / name).write_text("# synthetic\n", encoding="utf-8")

                with self.assertRaises(memory_compile.PolicyError) as raised:
                    memory_compile.rebuild_knowledge(vault, root / "rebuilt")

            self.assertEqual(str(raised.exception), expected)
            self.assertNotIn(name, str(raised.exception))

    def test_cli_and_worker_reject_secret_source_path_without_model_or_raw_log(self):
        for entrypoint in ("cli", "worker"):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as temporary:
                vault = Path(temporary)
                _seed_vault(vault)
                source = vault / "daily/2026-09-03.md"
                unsafe = vault / "daily/password=synthetic.md"
                unsafe.write_bytes(source.read_bytes())
                source.unlink()
                (vault / "daily/2026-09-04.md").unlink()
                original = unsafe.read_bytes()
                state_dir = vault / ".codex/scripts/.state"
                calls = []

                def unexpected_model(_prompt, _stage):
                    calls.append("model")
                    raise AssertionError("secret path reached model")

                with patch.object(memory_compile, "_run_codex", side_effect=unexpected_model), \
                        patch.object(memory_compile, "_checkpoint_machine_outputs", return_value=("clean", "")):
                    if entrypoint == "cli":
                        with patch.object(memory_compile, "VAULT_ROOT", vault), \
                                patch.object(memory_compile, "STATE_DIR", state_dir):
                            result = memory_compile.main(["--strict", "--max-calls", "1"])
                    else:
                        result = memory_compile._run_locked(vault, state_dir, False, 1, None)

                state = memory_compile.compile_state.load(state_dir)
                health = (state_dir / "health.json").read_text(encoding="utf-8")
                persisted = health + str(state.as_dict())
                self.assertEqual(result, 1 if entrypoint == "cli" else True)
                self.assertEqual(calls, [])
                self.assertEqual(unsafe.read_bytes(), original)
                self.assertEqual(state.ingested, {})
                self.assertNotIn("password=synthetic.md", persisted)
                self.assertNotIn("synthetic", persisted)

    def test_secret_knowledge_filename_stays_source_and_never_reaches_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            source = vault / "daily/2026-09-03.md"
            unsafe = vault / "knowledge/concepts/password=synthetic.md"
            unsafe.write_text("source content\n", encoding="utf-8")
            original = unsafe.read_bytes()
            calls = []

            def unexpected_model(_prompt, _stage):
                calls.append("model")
                raise AssertionError("secret path reached model")

            reason, detail = memory_compile._compile_one(
                vault,
                vault / ".codex/scripts/.state",
                source,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                "2026-09-08T00:00:00+03:00",
                runner=unexpected_model,
            )
            self.assertEqual((reason, detail), ("policy", "source-path-contains-secret"))
            self.assertEqual(calls, [])
            self.assertEqual(unsafe.read_bytes(), original)

    def test_generated_secret_filename_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            source = vault / "daily/2026-09-03.md"
            original_source = source.read_bytes()
            calls = []

            def generated_path(prompt, stage):
                calls.append(prompt)
                _deterministic_compiler(prompt, stage)
                generated = stage / "knowledge/concepts/password=generated.md"
                generated.write_text("model output\n", encoding="utf-8")

            reason, detail = memory_compile._compile_one(
                vault,
                vault / ".codex/scripts/.state",
                source,
                hashlib.sha256(original_source).hexdigest(),
                "2026-09-08T00:00:00+03:00",
                runner=generated_path,
            )
            promoted = tuple((vault / "knowledge").rglob("password=generated.md"))
            self.assertEqual((reason, detail), ("policy", "source-path-contains-secret"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(source.read_bytes(), original_source)
            self.assertEqual(promoted, ())

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
