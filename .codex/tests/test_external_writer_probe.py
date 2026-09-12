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


def _wait_for_text(path: Path, expected: str, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if path.read_text(encoding="ascii") == expected:
                return
        except OSError:
            pass
        time.sleep(0.005)
    raise AssertionError(f"{path.name} did not contain {expected!r}")


class ExternalWriterProbeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires a real Windows marker boundary")
    def test_marker_failure_before_rename_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = PublicationFixture(root)
            relative = "knowledge/concepts/new.md"
            staged = fixture.stage / relative
            staged.parent.mkdir()
            staged.write_text("yeni dosya\n", encoding="utf-8")
            destination = root / relative
            real_marker = state_store._create_replacement_marker
            calls = 0

            def marker_then_fail(source: Path, marker: Path | None) -> bool:
                nonlocal calls
                calls += 1
                created = real_marker(source, marker)
                if calls == 1:
                    raise PermissionError("synthetic pre-rename failure")
                return created

            with mock.patch.object(
                state_store,
                "_create_replacement_marker",
                side_effect=marker_then_fail,
            ):
                with self.assertRaisesRegex(PermissionError, "pre-rename failure"):
                    compiler._promote_changes(
                        fixture.stage,
                        root,
                        [relative],
                        {relative: None},
                        state_dir=fixture.state,
                        source_relative=f"daily/{fixture.source.name}",
                        source_digest=state_store.sha256_file(fixture.source),
                        source_size=fixture.source.stat().st_size,
                        timestamp="2026-09-08T12:00:00+03:00",
                        suppression_digest=compiler._suppression_digest(frozenset()),
                    )
                journal = compile_state.load_publication(fixture.state)
                target = journal["targets"][0]
                marker = root / target["backup_relative"]
                temporary_path = root / target["temporary_relative"]
                self.assertFalse(destination.exists())
                self.assertEqual(
                    target["temporary_file_id"],
                    [temporary_path.stat().st_dev, temporary_path.stat().st_ino],
                )
                self.assertTrue(os.path.samefile(marker, temporary_path))

                recovered = compiler._recover_pending_publication(root, fixture.state)
                self.assertEqual(recovered["status"], "complete")
                self.assertEqual(calls, 2)
                self.assertEqual(destination.read_text(encoding="utf-8"), "yeni dosya\n")
                self.assertFalse(marker.exists())
                self.assertFalse(temporary_path.exists())

    @unittest.skipUnless(os.name == "nt", "requires a real Windows rename boundary")
    def test_rename_failure_after_marker_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = PublicationFixture(root)
            relative = "knowledge/concepts/new.md"
            staged = fixture.stage / relative
            staged.parent.mkdir()
            staged.write_text("yeni dosya\n", encoding="utf-8")
            destination = root / relative
            real_rename = state_store.os.rename
            calls = 0

            def rename_then_fail(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("synthetic pre-rename failure")
                real_rename(source, target)

            with mock.patch.object(state_store.os, "rename", side_effect=rename_then_fail):
                with self.assertRaisesRegex(OSError, "pre-rename failure"):
                    compiler._promote_changes(
                        fixture.stage,
                        root,
                        [relative],
                        {relative: None},
                        state_dir=fixture.state,
                        source_relative=f"daily/{fixture.source.name}",
                        source_digest=state_store.sha256_file(fixture.source),
                        source_size=fixture.source.stat().st_size,
                        timestamp="2026-09-08T12:00:00+03:00",
                        suppression_digest=compiler._suppression_digest(frozenset()),
                    )

            journal = compile_state.load_publication(fixture.state)
            target = journal["targets"][0]
            marker = root / target["backup_relative"]
            temporary_path = root / target["temporary_relative"]
            self.assertFalse(destination.exists())
            self.assertTrue(os.path.samefile(marker, temporary_path))

            recovered = compiler._recover_pending_publication(root, fixture.state)
            self.assertEqual(recovered["status"], "complete")
            self.assertEqual(calls, 1)
            self.assertEqual(destination.read_text(encoding="utf-8"), "yeni dosya\n")
            self.assertFalse(marker.exists())
            self.assertFalse(temporary_path.exists())

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
                    _wait_for_text(done, "done")

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
                    _wait_for_text(done, "done")
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
