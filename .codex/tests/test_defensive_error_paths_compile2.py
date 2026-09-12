"""compile.py savunma dalları — dilim 2: manifest/yayın/checkpoint kolları."""

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


DIGEST = "a" * 64


def _git_script(rules):
    """Kurallı git taklidi: (yüklem, sonuç) listesinde ilk eşleşen döner."""

    def runner(_root, *args, **_k):
        for predicate, result in rules:
            if predicate(args):
                return result
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return runner


def _r(code=0, out=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr="")


class SnapshotFlowArms(unittest.TestCase):
    def _base_rules(self, **overrides):
      def factory(index_path):
        rules = {
            "gpgsign": _r(1, ""),
            "hooks": _r(0, "hooks"),
            "index": _r(0, str(index_path)),
            "symbolic": _r(0, "refs/heads/main"),
            "verify": _r(0, "b" * 40),
            "diff_cached": _r(0, ""),
            "add": _r(0, ""),
            "diff_names": _r(0, "daily/x.md\0"),
            "ls_files": _r(0, "daily/x.md\0"),
            "write_tree": _r(0, "c" * 40),
            "commit_tree": _r(0, "d" * 40),
            "update_ref": _r(0, ""),
        }
        rules.update(overrides)

        def match(args):
            joined = " ".join(str(a) for a in args)
            if "commit.gpgsign" in joined:
                return "gpgsign"
            if "--git-path" in joined and "hooks" in joined:
                return "hooks"
            if "--git-path" in joined and "index" in joined:
                return "index"
            if "symbolic-ref" in joined:
                return "symbolic"
            if "rev-parse" in joined and "--verify" in joined:
                return "verify"
            if "diff" in joined and "--cached" in joined and "-z" in args:
                return "diff_names"
            if "diff" in joined and "--cached" in joined:
                return "diff_cached"
            if joined.startswith("add") or " add " in f" {joined} ":
                return "add"
            if "ls-files" in joined:
                return "ls_files"
            if "write-tree" in joined:
                return "write_tree"
            if "commit-tree" in joined:
                return "commit_tree"
            if "update-ref" in joined:
                return "update_ref"
            return None

        def runner(_root, *args, **_k):
            key = match(args)
            return rules.get(key, _r(0, ""))

        return runner
      return factory

    def _snapshot(self, runner_factory):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            index = Path(temporary) / "index"
            index.write_bytes(b"indeks-icerigi")
            runner = runner_factory(index)
            with mock.patch.object(memory_compile, "_git", side_effect=runner):
                return memory_compile._commit_machine_snapshot(vault, "etiket")

    def test_deferred_arms(self) -> None:
        cases = [
            ({"diff_cached": _r(1, "")}, "staged-index-unreadable"),
            ({"symbolic": _r(0, "refs/heads/dal")}, "main-branch-required"),
            ({"diff_names": _r(0, "")}, "machine-stage-empty-or-unreadable"),
            ({"diff_names": _r(0, "baska/x.md\0")}, "machine-stage-boundary"),
            ({"ls_files": _r(1, "")}, "machine-stage-snapshot-unreadable"),
            ({"write_tree": _r(1, "")}, "machine-tree-failed"),
        ]
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                status, reason = self._snapshot(self._base_rules(**overrides))
                self.assertEqual((status, reason), ("deferred", expected))

    def test_secret_label_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            index = Path(temporary) / "index"
            index.write_bytes(b"i")
            with mock.patch.object(
                memory_compile, "_git", side_effect=self._base_rules()(index)
            ):
                outcome = memory_compile._commit_machine_snapshot(
                    vault, "api_key=abc12345"
                )
        self.assertEqual(outcome, ("deferred", "source-path-contains-secret"))

    def test_checkpoint_outer_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / "state"
            state.mkdir()

            def non_main(_root, *args, **_k):
                joined = " ".join(str(a) for a in args)
                if "--is-inside-work-tree" in joined:
                    return _r(0, "true")
                if "--show-current" in joined:
                    return _r(0, "baska-dal")
                return _r(0, "")

            with mock.patch.object(memory_compile, "_git", side_effect=non_main):
                self.assertEqual(
                    memory_compile._checkpoint_machine_outputs(vault, state, "e"),
                    ("deferred", "main-branch-required"),
                )

            with mock.patch.object(
                memory_compile, "_git", side_effect=OSError("git koptu")
            ):
                self.assertEqual(
                    memory_compile._checkpoint_machine_outputs(vault, state, "e"),
                    ("deferred", "machine-checkpoint-error"),
                )


class StageAndManifestArms(unittest.TestCase):
    def test_copy_tree_walks_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            source = vault / "knowledge"
            (source / "concepts").mkdir(parents=True)
            (source / "concepts" / "not.md").write_text("x", encoding="utf-8")
            destination = Path(temporary) / "hedef"
            memory_compile._copy_source_tree(source, destination, vault)
            copied = (destination / "concepts" / "not.md").is_file()
        self.assertTrue(copied)

    def test_stage_collision_then_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            real_mkdir = Path.mkdir
            calls = {"n": 0}

            def flaky(self, *args, **kwargs):
                if self.name.startswith("compile-stage-") and calls["n"] == 0:
                    calls["n"] += 1
                    raise FileExistsError(self)
                return real_mkdir(self, *args, **kwargs)

            with mock.patch.object(Path, "mkdir", flaky):
                stage = memory_compile._create_stage_directory(state)
        self.assertTrue(stage.name.startswith("compile-stage-"))

    def test_connection_link_policy_wrap(self) -> None:
        from graph_integrity import GraphPolicyError
        with mock.patch.object(
            memory_compile, "normalize_connection_links",
            side_effect=GraphPolicyError("bozuk"),
        ):
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._normalize_connection_links(Path("."))

    def test_manifest_directory_arms(self) -> None:
        import subprocess as sp
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            outside = Path(temporary) / "dis"
            outside.mkdir()
            sp.run(["cmd", "/c", "mklink", "/J", str(stage / "kestirme"), str(outside)],
                   check=True, capture_output=True)
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._manifest(stage)

        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            original = stage / "a.md"
            original.write_text("x", encoding="utf-8")
            os.link(original, stage / "b.md")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._manifest(stage)

    def test_output_path_predicates(self) -> None:
        self.assertFalse(memory_compile._is_allowed_output_file("knowledge/dosya.txt"))
        self.assertFalse(memory_compile._is_allowed_output_file("baska/n.md"))

    def test_manifest_diff_arms(self) -> None:
        before = {"knowledge/index.md": ("file", "1"), "knowledge/eski.md": ("file", "2")}
        with self.assertRaises(memory_compile.PolicyError):
            memory_compile._validate_manifest_diff(before, {"knowledge/index.md": ("file", "1")})
        with self.assertRaises(memory_compile.PolicyError):
            memory_compile._validate_manifest_diff(
                {"knowledge/index.md": ("file", "1")},
                {"knowledge/index.md": ("dir", "")},
            )
        with self.assertRaises(memory_compile.PolicyError):
            memory_compile._validate_manifest_diff(
                {}, {"baska/dizin": ("dir", "")}
            )
        with self.assertRaises(memory_compile.PolicyError):
            memory_compile._validate_manifest_diff(
                {}, {"baska/dosya.md": ("file", "1")}
            )
        with self.assertRaises(memory_compile.NoChangesError):
            memory_compile._validate_manifest_diff(
                {"knowledge/index.md": ("file", "1")},
                {"knowledge/index.md": ("file", "1"),
                 "knowledge/concepts": ("dir", "")},
            )


class LiveDestinationArms(unittest.TestCase):
    def test_validate_live_destination_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "knowledge").mkdir()

            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._validate_live_destination(vault, "baska/x.md", None)

            created = memory_compile._validate_live_destination(
                vault, "knowledge/concepts/yeni.md", None
            )
            self.assertEqual(created.name, "yeni.md")
            self.assertTrue((vault / "knowledge" / "concepts").is_dir())

            hedef = vault / "gercek-parent"
            hedef.mkdir()
            os.symlink(hedef, vault / "knowledge" / "linkli", target_is_directory=True)
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._validate_live_destination(
                    vault, "knowledge/linkli/n.md", None
                )

            var = vault / "knowledge" / "mevcut.md"
            var.write_text("içerik", encoding="utf-8")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._validate_live_destination(
                    vault, "knowledge/mevcut.md", DIGEST
                )
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._validate_live_destination(
                    vault, "knowledge/yok.md", DIGEST
                )
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._validate_live_destination(
                    vault, "knowledge/mevcut.md", None
                )

    def test_live_digest_rejects_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "knowledge").mkdir()
            gercek = vault / "gercek.md"
            gercek.write_text("x", encoding="utf-8")
            os.symlink(gercek, vault / "knowledge" / "index.md")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._live_digest(vault, "knowledge/index.md")

    def test_snapshot_matcher_arms(self) -> None:
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = vault / "daily"
            daily.mkdir()
            note = daily / "2026-09-11.md"
            note.write_text("içerik", encoding="utf-8")
            payload = note.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()

            gercek = vault / "gercek.md"
            gercek.write_text("içerik", encoding="utf-8")
            link = daily / "linkli.md"
            os.symlink(gercek, link)
            self.assertFalse(
                memory_compile._source_snapshot_matches(link, digest, len(payload))
            )
            self.assertFalse(
                memory_compile._source_snapshot_matches(
                    note, digest, len(payload) + 5
                )
            )
            self.assertFalse(
                memory_compile._source_snapshot_matches(
                    note, hashlib.sha256(b"baska").hexdigest(), len(payload)
                )
            )
            self.assertTrue(
                memory_compile._source_snapshot_matches(note, digest, len(payload))
            )


class PublicationValidatorArms(unittest.TestCase):
    def _journal(self, **overrides) -> dict:
        journal = {
            "schema_version": compile_state.PUBLICATION_SCHEMA_VERSION,
            "status": "pending",
            "operation_id": "f" * 32,
            "timestamp": "2026-09-11T10:00:00",
            "source_relative": "daily/2026-09-11.md",
            "source_digest": DIGEST,
            "source_size": 5,
            "suppression_digest": DIGEST,
            "stage": "compile-stage-" + "0" * 32,
            "targets": [
                {"relative": "knowledge/index.md", "before_sha256": None,
                 "after_sha256": DIGEST, "completed": False}
            ],
        }
        journal.update(overrides)
        return journal

    def test_publication_paths_and_journal_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._publication_source_path(vault, "daily\\ters.md")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._publication_source_path(vault, "daily/alt/derin.md")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._validate_publication_source_relative("daily/../k.md")

            state = vault / "state"
            state.mkdir()
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._publication_stage_path(state, "bozuk ad")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._publication_stage_path(
                    state, "compile-stage-" + "0" * 32, required=True
                )
            optional = memory_compile._publication_stage_path(
                state, "compile-stage-" + "0" * 32, required=False
            )
            self.assertFalse(optional.exists())
            (state / ("compile-stage-" + "1" * 32)).write_text("dosya", encoding="utf-8")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._publication_stage_path(
                    state, "compile-stage-" + "1" * 32
                )

            cases = [
                {"schema_version": 9},
                {"status": "zombi"},
                {"operation_id": "KISA"},
                {"timestamp": ""},
                {"source_digest": "KISA"},
                {"targets": []},
                {"targets": [5]},
                {"targets": [{"relative": "baska/x.md",
                              "after_sha256": DIGEST, "completed": False}]},
                {"targets": [{"relative": "knowledge/index.md",
                              "before_sha256": "KISA", "after_sha256": DIGEST,
                              "completed": False}]},
                {"targets": [{"relative": "knowledge/index.md",
                              "before_sha256": None, "after_sha256": DIGEST,
                              "completed": "hayır"}]},
            ]
            for overrides in cases:
                with self.subTest(overrides=str(overrides)[:50]):
                    with self.assertRaises(memory_compile.PolicyError):
                        memory_compile._validate_publication_journal(
                            state, self._journal(**overrides)
                        )
            validated = memory_compile._validate_publication_journal(
                state, self._journal()
            )
        self.assertEqual(validated["status"], "pending")


if __name__ == "__main__":
    unittest.main()
