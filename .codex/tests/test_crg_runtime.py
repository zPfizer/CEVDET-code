from pathlib import Path
import importlib.util
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/crg_runtime.py"
spec = importlib.util.spec_from_file_location("crg_runtime", SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def test_canonical_junction_is_rejected_before_resolution_or_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            canonical = Path(temporary)
            original_lstat = Path.lstat
            def reparse(path, *args, **kwargs):
                actual = original_lstat(path, *args, **kwargs)
                if path == canonical:
                    class Junction:
                        st_mode = actual.st_mode
                        st_file_attributes = 0x400
                    return Junction()
                return actual
            with patch.object(Path, "lstat", reparse), patch.object(runtime, "git") as git:
                with self.assertRaisesRegex(ValueError, "CANONICAL_ROOT_REPARSE"):
                    runtime.code_root(canonical, canonical)
                git.assert_not_called()

    def test_interrupted_acknowledgment_replays_newer_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "root.json"
            path.write_bytes(b"newer-generation")
            with patch.object(runtime.os, "link", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    runtime.acknowledge(directory, path.name, b"older-generation")
            self.assertFalse(path.exists())
            self.assertEqual(len(list(directory.glob("*.claim"))), 1)
            self.assertEqual(runtime.requests(directory), {path.name: b"newer-generation"})
            self.assertEqual(list(directory.glob("*.claim")), [])

    def test_more_than_256_completed_requests_drain_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            canonical = Path(temporary)
            directory = canonical / ".code-review-graph/refresh-requests"
            directory.mkdir(parents=True)
            for index in range(257):
                (directory / (str(index) + ".json")).write_text('{"repo":"removed"}')
            processed = {}
            with patch.object(runtime, "code_root", side_effect=FileNotFoundError), patch.object(runtime, "receipt"):
                first = runtime.requests(directory)
                self.assertEqual(len(first), 256)
                runtime.consume_requests(first, processed, canonical)
                second = runtime.requests(directory)
                self.assertEqual(len(second), 1)
                runtime.consume_requests(second, processed, canonical)
                self.assertEqual(runtime.requests(directory), {})
                self.assertLessEqual(len(processed), 256)

    def test_removed_worktree_request_does_not_stop_valid_requests_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            canonical, removed, valid = base / "code", base / "code-old", base / "code-valid"
            for path in (canonical, removed, valid):
                path.mkdir()
            runtime.request(removed, "post-commit", canonical)
            runtime.request(valid, "post-commit", canonical)
            removed.rmdir()
            snapshot = runtime.requests(canonical / ".code-review-graph/refresh-requests")
            for _restart in range(2):
                processed = {}
                with patch.object(runtime, "git", side_effect=[str(canonical / ".git"), str(valid)]), \
                     patch.object(runtime, "graph_path", return_value=valid / "graph.db"), \
                     patch.object(runtime, "build") as build, patch.object(runtime, "receipt") as receipt:
                    runtime.consume_requests(snapshot, processed, canonical)
                    self.assertEqual(build.call_count, 1)
                    self.assertEqual(receipt.call_args.args[0], "request_rejected")
                    self.assertEqual(processed, snapshot)

    @unittest.skipUnless(importlib.util.find_spec("code_review_graph"), "CRG integration runs with canonical runtime")
    def test_registered_worktree_commit_request_builds_its_actual_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            canonical, worktree = base / "code", base / "code-task"
            canonical.mkdir()
            def git(root, *args):
                return subprocess.check_output(["git", "-c", "core.hooksPath=NUL", "-c",
                    "commit.gpgsign=false", "-c", "user.name=CRG Fixture", "-c",
                    "user.email=fixture@example.invalid", "-C", str(root), *args],
                    stderr=subprocess.PIPE, text=True).strip()
            git(canonical, "init", "-q")
            (canonical / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            git(canonical, "add", "calc.py")
            git(canonical, "commit", "-qm", "fixture")
            git(canonical, "worktree", "add", "-qb", "fixture-task", str(worktree))
            source = worktree / "calc.py"
            source.write_text("def add(a, b):\n    return a + b + 1\n")
            git(worktree, "add", "calc.py")
            git(worktree, "commit", "-qm", "changed fixture")
            runtime.request(worktree, "post-commit", canonical)
            snapshot = runtime.requests(canonical / ".code-review-graph/refresh-requests")
            runtime.consume_requests(snapshot, {}, canonical)
            db = runtime.graph_path(worktree)
            ready = json.loads(runtime.publication(db))
            self.assertTrue(ready["ready"])
            with runtime.readonly_store_type()(db) as store:
                self.assertEqual(store.get_metadata("git_head_sha"), git(worktree, "rev-parse", "HEAD"))
                row = store._conn.execute("SELECT file_hash FROM nodes WHERE kind='File'").fetchone()
                self.assertEqual(row[0], hashlib.sha256(source.read_bytes()).hexdigest())
            with runtime.native_store_type(worktree, db)(db) as store:
                store.remove_file_data(str(source))
            self.assertFalse(json.loads(runtime.publication(db))["ready"])
            runtime.build(worktree, db)
            self.assertTrue(json.loads(runtime.publication(db))["ready"])
            runtime.publish(worktree, db, ready=False)
            self.assertFalse(json.loads(runtime.publication(db))["ready"])
            self.assertNotEqual(ready["generation"], json.loads(runtime.publication(db))["generation"])
            with patch("code_review_graph.postprocessing.run_post_processing", return_value={"warnings": ["failure"]}):
                with self.assertRaisesRegex(RuntimeError, "POSTPROCESS_INCOMPLETE"):
                    runtime.build(worktree, db)
            self.assertFalse(json.loads(runtime.publication(db))["ready"])

    @unittest.skipUnless(importlib.util.find_spec("code_review_graph"), "CRG integration runs with canonical runtime")
    def test_native_watch_normal_exit_releases_owner_without_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("code_review_graph.incremental.full_build", return_value={}), \
                 patch("code_review_graph.incremental.watch") as watched, \
                 patch("code_review_graph.postprocessing.run_post_processing", return_value={}), \
                 patch.object(runtime, "git", return_value="head"):
                runtime.watch(root)
            self.assertEqual(watched.call_count, 1)
            self.assertFalse((root / ".code-review-graph/crg-freshness.lock").exists())

    @unittest.skipUnless(importlib.util.find_spec("code_review_graph"), "CRG integration runs with canonical runtime")
    def test_native_callback_warning_invalidates_ready_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                subprocess.run(["git", "-c", "core.hooksPath=NUL", "-c", "commit.gpgsign=false",
                    "-c", "user.name=CRG Fixture", "-c", "user.email=fixture@example.invalid",
                    "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            git("add", "calc.py")
            git("commit", "-qm", "callback fixture")
            def updated(_root, store, on_files_updated, stop_event):
                self.assertTrue(json.loads(runtime.publication(runtime.graph_path(root)))["ready"])
                on_files_updated(store)
            with patch("code_review_graph.incremental.watch", side_effect=updated), \
                 patch("code_review_graph.postprocessing.run_post_processing", side_effect=[{}, {"warnings": ["failure"]}]):
                runtime.watch(root)
            self.assertFalse(json.loads(runtime.publication(runtime.graph_path(root)))["ready"])

    @unittest.skipUnless(importlib.util.find_spec("code_review_graph"), "CRG integration runs with canonical runtime")
    def test_only_verified_empty_parser_result_can_explain_missing_file_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, yaml = root / "calc.py", root / "ordinary.yaml"
            code.write_text("def add(a, b):\n    return a + b\n")
            yaml.write_text("name: example\nvalue: 42\n")
            hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (code, yaml)}
            source = {"root": str(root), "head": "a", "branch": "b", "files": hashes}
            graph = {"head": "a", "branch": "b", "files": {str(code): hashes[str(code)]}}
            self.assertTrue(runtime.native_matches(source, graph))
            graph["files"] = {str(yaml): hashes[str(yaml)]}
            self.assertFalse(runtime.native_matches(source, graph))
            graph["files"] = {str(code): hashes[str(code)]}
            yaml.write_text("name: changed\n")
            self.assertFalse(runtime.native_matches(source, graph))

    @unittest.skipUnless(importlib.util.find_spec("code_review_graph"), "CRG integration runs with canonical runtime")
    def test_native_query_store_is_readonly_and_does_not_create_missing_database(self):
        from code_review_graph.graph import GraphStore
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "graph.db"
            with GraphStore(db):
                pass
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            with runtime.readonly_store_type()(db) as store:
                self.assertEqual(store.get_metadata("schema_version"), "9")
                store._conn.execute("CREATE TEMP TABLE impact_probe (value TEXT)")
                store._conn.execute("INSERT INTO impact_probe VALUES ('local')")
                with self.assertRaises(sqlite3.OperationalError):
                    store._conn.execute("DELETE FROM nodes")
            self.assertEqual(before, hashlib.sha256(db.read_bytes()).hexdigest())
            missing = db.with_name("missing.db")
            with self.assertRaises(sqlite3.OperationalError):
                runtime.readonly_store_type()(missing)
            self.assertFalse(missing.exists())

    def test_vault_rejected_before_git_or_content_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            code, vault = parent / "code", parent / "Vault"
            code.mkdir()
            vault.mkdir()
            with patch.object(runtime, "git", side_effect=AssertionError("must not run")):
                with self.assertRaisesRegex(ValueError, "PROTECTED_SOURCE"):
                    runtime.code_root(vault, code)

    def test_code_worktree_requires_same_common_git_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = Path(temporary) / "code"
            worktree = Path(temporary) / "code-task"
            code.mkdir()
            worktree.mkdir()
            with patch.object(runtime, "git", return_value=str(code / ".git")):
                with self.assertRaisesRegex(ValueError, "EXACT_ROOT_REQUIRED"):
                    runtime.code_root(worktree, code)
            with patch.object(runtime, "git", side_effect=[str(code / ".git"), str(worktree)]):
                self.assertEqual(runtime.code_root(worktree, code), worktree.resolve())

    def test_hook_only_requests_and_coalesces_without_graph(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime.request(root, "pre-commit", root)
            directory = root / ".code-review-graph/refresh-requests"
            path = next(directory.glob("*.json"))
            before = runtime.requests(directory)
            signal = runtime.RefreshRequested(directory, before)
            self.assertFalse(signal.wait(0))
            runtime.request(root, "post-commit", root)
            self.assertTrue(signal.wait(0))
            self.assertEqual(json.loads(path.read_text())["hook"], "post-commit")
            self.assertFalse((path.parent / "graph.db").exists())

    def test_different_worktree_requests_are_consumed_without_losing_later_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            canonical = Path(temporary)
            first, second = canonical / "first", canonical / "second"
            first.mkdir()
            second.mkdir()
            runtime.request(first, "post-commit", canonical)
            runtime.request(second, "post-commit", canonical)
            directory = canonical / ".code-review-graph/refresh-requests"
            initial = runtime.requests(directory)
            self.assertEqual(len(initial), 2)
            processed = {}
            def newer(root, db):
                if root == first:
                    runtime.request(first, "post-rewrite", canonical)
            with patch.object(runtime, "code_root", side_effect=lambda value, canonical: Path(value)), \
                 patch.object(runtime, "graph_path", side_effect=lambda root: root / "graph.db"), \
                 patch.object(runtime, "build", side_effect=newer) as built:
                runtime.consume_requests(initial, processed, canonical)
                self.assertEqual(built.call_count, 2)
                self.assertNotEqual(runtime.requests(directory), processed)
                runtime.consume_requests(runtime.requests(directory), processed, canonical)
                self.assertEqual(built.call_count, 3)

    def test_owner_excludes_second_writer_and_does_not_steal_orphan(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "graph.db"
            with runtime.owner(db):
                with self.assertRaises(FileExistsError):
                    with runtime.owner(db):
                        self.fail("second owner")
            marker = db.with_name("crg-freshness.lock")
            self.assertFalse(marker.exists())
            marker.write_text("orphan")
            with self.assertRaises(FileExistsError):
                with runtime.owner(db):
                    self.fail("orphan stolen")
            self.assertEqual(marker.read_text(), "orphan")

    def test_protected_status_never_loads_crg_or_scans_vault(self):
        p = subprocess.run([sys.executable, "-B", str(SCRIPT), "protected-status",
                            "--repo", "Z:/nonexistent/protected-vault"],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["event"], "vault_protected")


if __name__ == "__main__":
    unittest.main()
