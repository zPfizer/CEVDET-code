"""A publication must preserve an atomic external replacement."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _fixtures  # noqa: F401

import compile as compiler
import compile_state
import state_store
from test_concurrency_publication import PublicationFixture


_WRITER_SCRIPT = """
import ctypes
from pathlib import Path
import sys
import time

target, start, done = map(Path, sys.argv[1:])
while not start.exists():
    time.sleep(0.005)
try:
    replacement = target.with_name(target.name + '.external')
    replacement.write_bytes(b'external writer content.\\n')
    if not ctypes.windll.kernel32.ReplaceFileW(str(target), str(replacement), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())
    done.write_text('done', encoding='ascii')
except BaseException as exc:
    done.write_text(f'error:{exc!r}', encoding='utf-8')
    raise
"""


class ExternalWriterProbeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires a real Windows replacement boundary")
    def test_native_failure_after_replace_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            destination = root / "knowledge" / "index.md"
            state = root / ".codex" / "scripts" / ".state"
            destination.parent.mkdir()
            state.mkdir(parents=True)
            source.write_bytes(b"generated\n")
            destination.write_bytes(b"baseline\n")
            before = state_store.sha256_file(destination)
            backup = state / "backup.bak"
            real_replace = state_store._windows_replace
            calls = 0

            def fail_after_replace(replaced: Path, replacement: Path, backup_path: Path | None) -> None:
                nonlocal calls
                calls += 1
                real_replace(replaced, replacement, backup_path)
                error = PermissionError("synthetic post-replace failure")
                error.winerror = 32
                raise error

            with mock.patch.object(state_store, "_windows_replace", side_effect=fail_after_replace):
                with self.assertRaises(PermissionError):
                    state_store.replace_with_retry(
                        source,
                        destination,
                        expected_digest=before,
                        backup=backup,
                        deadline=time.monotonic() + 5,
                    )
            self.assertEqual(calls, 1)
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"generated\n")
            self.assertEqual(backup.read_bytes(), b"baseline\n")

    @unittest.skipUnless(os.name == "nt", "requires Windows no-clobber rename")
    def test_external_creation_is_not_overwritten_for_absent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            destination = root / "knowledge" / "new.md"
            destination.parent.mkdir()
            source.write_bytes(b"generated\n")
            real_rename = state_store.os.rename
            inserted = False

            def rename(replaced: Path, target: Path) -> None:
                nonlocal inserted
                if not inserted:
                    inserted = True
                    Path(target).write_bytes(b"external writer content.\n")
                real_rename(replaced, target)

            with mock.patch.object(state_store.os, "rename", side_effect=rename):
                with self.assertRaises(compiler.ReplacementConflict):
                    compiler._atomic_copy(
                        source,
                        destination,
                        expected_destination_digest=None,
                    )
            self.assertEqual(destination.read_bytes(), b"external writer content.\n")
            self.assertFalse(list(destination.parent.glob(f".{destination.name}.*.tmp")))

    @unittest.skipUnless(os.name == "nt", "requires a real Windows replacement boundary")
    def test_writer_after_replace_remains_live_and_backup_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            destination = root / "knowledge" / "index.md"
            state = root / ".codex" / "scripts" / ".state"
            destination.parent.mkdir()
            state.mkdir(parents=True)
            source.write_bytes(b"generated\n")
            destination.write_bytes(b"baseline\n")
            before = state_store.sha256_file(destination)
            backup = state / "backup.bak"
            start = root / "writer.start"
            done = root / "writer.done"
            writer = subprocess.Popen(
                [sys.executable, "-c", _WRITER_SCRIPT, str(destination), str(start), str(done)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            real_replace = state_store._windows_replace
            triggered = False

            def replace(replaced: Path, replacement: Path, backup_path: Path | None) -> None:
                nonlocal triggered
                real_replace(replaced, replacement, backup_path)
                if not triggered:
                    triggered = True
                    start.write_text("start", encoding="ascii")
                    deadline = time.monotonic() + 5
                    while not done.exists() and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertEqual(done.read_text(encoding="utf-8"), "done")

            try:
                with mock.patch.object(state_store, "_windows_replace", side_effect=replace):
                    with self.assertRaises(state_store.ReplacementConflict):
                        state_store.replace_with_retry(
                            source,
                            destination,
                            expected_digest=before,
                            backup=backup,
                            deadline=time.monotonic() + 5,
                        )
            finally:
                if writer.poll() is None:
                    writer.kill()
                writer.wait(timeout=5)

            self.assertTrue(triggered)
            self.assertEqual(destination.read_bytes(), b"external writer content.\n")
            self.assertEqual(backup.read_bytes(), b"baseline\n")

    @unittest.skipUnless(os.name == "nt", "requires a real Windows replacement boundary")
    def test_atomic_writer_backup_preserves_user_bytes_and_publication_stays_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = PublicationFixture(root)
            destination = fixture.knowledge / "index.md"
            expected = b"external writer content.\n"
            start = root / "writer.start"
            done = root / "writer.done"
            writer = subprocess.Popen(
                [sys.executable, "-c", _WRITER_SCRIPT, str(destination), str(start), str(done)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            real_replace = state_store._windows_replace
            triggered = False

            def replace(replaced: Path, replacement: Path, backup: Path | None) -> None:
                nonlocal triggered
                if replaced.resolve() == destination.resolve() and not triggered:
                    triggered = True
                    start.write_text("start", encoding="ascii")
                    deadline = time.monotonic() + 5
                    while not done.exists() and time.monotonic() < deadline:
                        time.sleep(0.005)
                    if not done.exists():
                        raise AssertionError("external writer did not finish")
                    self.assertEqual(done.read_text(encoding="utf-8"), "done")
                    self.assertEqual(destination.read_bytes(), expected)
                real_replace(replaced, replacement, backup)

            try:
                with mock.patch.object(state_store, "_windows_replace", side_effect=replace):
                    with self.assertRaises(compiler.ReplacementConflict):
                        fixture.promote()
            finally:
                if writer.poll() is None:
                    writer.kill()
                writer.wait(timeout=5)

            self.assertTrue(triggered)
            journal = compile_state.load_publication(fixture.state)
            self.assertIsNotNone(journal)
            self.assertEqual(journal["status"], "pending")
            target = journal["targets"][0]
            self.assertFalse(target["completed"])
            self.assertTrue(target["conflict"])
            backup = root / target["backup_relative"]
            self.assertEqual(backup.read_bytes(), expected)
            self.assertTrue(fixture.stage.is_dir())
            with self.assertRaisesRegex(compiler.PolicyError, "publication-target-changed"):
                compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertEqual(backup.read_bytes(), expected)
            self.assertTrue(compile_state.publication_file(fixture.state).is_file())


if __name__ == "__main__":
    unittest.main()
