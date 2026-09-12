"""compile.py politika/checkpoint/yayın korkuluklarını sürer (dilim 1)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import compile as memory_compile
import compile_state
import state_store


def _git_result(returncode: int = 0, stdout: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class HealthAndPathGuards(unittest.TestCase):
    def test_health_writes_swallow_os_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            state_store.write_health(state, component="compile", error="önceki")
            before = json.loads((state / "health.json").read_text(encoding="utf-8"))

            with mock.patch.object(
                state_store, "atomic_write_json", side_effect=OSError("disk")
            ) as atomic_write:
                self.assertIsNone(memory_compile.write_health(state, "hata"))
            atomic_write.assert_called_once()
            self.assertEqual(
                json.loads((state / "health.json").read_text(encoding="utf-8")),
                before,
            )

            with mock.patch.object(
                state_store, "atomic_write_json", side_effect=OSError("disk")
            ) as atomic_write:
                self.assertIsNone(memory_compile.clear_health(state, "compile"))
            atomic_write.assert_called_once()
            self.assertEqual(
                json.loads((state / "health.json").read_text(encoding="utf-8")),
                before,
            )

    def test_secret_path_guard(self) -> None:
        with self.assertRaises(memory_compile.PolicyError):
            memory_compile._check_secret_path("daily/api_key=abc12345.md")
        with self.assertRaises(memory_compile.PolicyError):
            memory_compile.build_compile_prompt(
                "indeks", "daily/password=gizli123.md", "gövde",
                "2026-09-11", ("etiket",),
            )

    def test_source_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            hedef = Path(temporary) / "dis.md"
            hedef.write_text("x", encoding="utf-8")
            link = vault / "link.md"
            os.symlink(hedef, link)
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._check_source(link, vault, False)
            klasor = vault / "klasor"
            klasor.mkdir()
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._check_source(klasor, vault, False)
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._check_source(Path(temporary) / "dis.md", vault, False)

    def test_copy_source_tree_missing_creates_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            destination = Path(temporary) / "hedef"
            memory_compile._copy_source_tree(vault / "yok", destination, vault)
            self.assertTrue(destination.is_dir())
            self.assertEqual(list(destination.iterdir()), [])

    def test_stage_directory_collision_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(
                memory_compile.Path, "mkdir", side_effect=FileExistsError
            ):
                with self.assertRaises(FileExistsError):
                    memory_compile._create_stage_directory(state)

    def test_taxonomy_error_becomes_policy_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            from tag_taxonomy import TaxonomyError
            with mock.patch.object(
                memory_compile, "load_taxonomy", side_effect=TaxonomyError("bozuk")
            ):
                with self.assertRaises(memory_compile.PolicyError):
                    memory_compile._normalize_stage_tags(stage, stage / "yok.json")


class ManifestAndPublicationGuards(unittest.TestCase):
    def test_manifest_rejects_links_and_secret_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            hedef = Path(temporary) / "gercek.md"
            hedef.write_text("x", encoding="utf-8")
            os.symlink(hedef, stage / "link.md")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._manifest(stage)
            (stage / "link.md").unlink()
            (stage / "api_key=abc12345.md").write_text("x", encoding="utf-8")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._manifest(stage)

    def test_publication_source_relative_guards(self) -> None:
        with self.assertRaises(ValueError):
            memory_compile._validate_publication_source_relative(123)
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            with self.assertRaises(ValueError):
                memory_compile._publication_source_path(vault, "../kacak.md")

    def test_live_digest_skips_unsupported_and_stat_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            self.assertIsNone(memory_compile._live_digest(vault, "baska/dosya.txt"))
            self.assertIsNone(memory_compile._live_digest(vault, "daily/yok.md"))

    def test_source_snapshot_matches_argument_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            note = Path(temporary) / "2026-09-11.md"
            note.write_text("x", encoding="utf-8")
            self.assertFalse(
                memory_compile._source_snapshot_matches(note, 5, 1)
            )
            with mock.patch.object(
                memory_compile, "_MAX_SOURCE_SNAPSHOT_BYTES", 0
            ):
                self.assertFalse(
                    memory_compile._source_snapshot_matches(note, "a" * 64, 1)
                )


class CheckpointGuards(unittest.TestCase):
    def _run_snapshot(self, git_mock) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            with mock.patch.object(memory_compile, "_git", side_effect=git_mock):
                return memory_compile._commit_machine_snapshot(vault, "test")

    def test_snapshot_defers_on_policy_and_state_failures(self) -> None:
        def policy_unreadable(_root, *args, **_k):
            if "commit.gpgsign" in args:
                return _git_result(2)
            return _git_result(0, "false")

        status, reason = self._run_snapshot(policy_unreadable)
        self.assertEqual((status, reason[:10]), ("deferred", "checkpoint"))

        def staged_dirty(_root, *args, **_k):
            if "diff" in args and "--cached" in args and "--name-only" in args:
                return _git_result(0, "daily/x.md")
            if "commit.gpgsign" in args:
                return _git_result(1, "")
            if "core.hooksPath" in args:
                return _git_result(1, "")
            return _git_result(0, "")

        status, reason = self._run_snapshot(staged_dirty)
        self.assertEqual(status, "deferred")

    def test_checkpoint_skips_non_git_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)

            def not_git(_root, *args, **_k):
                if "--is-inside-work-tree" in args:
                    return _git_result(128, "")
                return _git_result(0, "")

            with mock.patch.object(memory_compile, "_git", side_effect=not_git):
                status, reason = memory_compile._checkpoint_machine_outputs(
                    vault, vault / "state", "etiket"
                )
        self.assertEqual((status, reason), ("skipped", "not-a-git-worktree"))

    def test_checkpoint_reports_unreadable_and_clean_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / "state"
            state.mkdir()

            def status_fail(_root, *args, **_k):
                if "--is-inside-work-tree" in args:
                    return _git_result(0, "true")
                if "--show-current" in args:
                    return _git_result(0, "main")
                if "status" in args:
                    return _git_result(1, "")
                return _git_result(0, "")

            with mock.patch.object(memory_compile, "_git", side_effect=status_fail):
                status, _reason = memory_compile._checkpoint_machine_outputs(
                    vault, state, "etiket"
                )
            self.assertEqual(status, "deferred")

            def status_clean(_root, *args, **_k):
                if "--is-inside-work-tree" in args:
                    return _git_result(0, "true")
                if "--show-current" in args:
                    return _git_result(0, "main")
                if "status" in args:
                    return _git_result(0, "")
                return _git_result(0, "")

            with mock.patch.object(memory_compile, "_git", side_effect=status_clean):
                status, reason = memory_compile._checkpoint_machine_outputs(
                    vault, state, "etiket"
                )
        self.assertEqual((status, reason), ("clean", "no-machine-changes"))


class RepairAndRunnerGuards(unittest.TestCase):
    def test_run_codex_wraps_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            with mock.patch.object(
                memory_compile.codex_runner, "run_exec", side_effect=OSError("disk")
            ):
                self.assertEqual(
                    memory_compile._run_codex("istem", stage), "codex-exec-error"
                )


if __name__ == "__main__":
    unittest.main()
