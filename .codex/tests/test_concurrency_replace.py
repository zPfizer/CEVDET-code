from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest import mock

import _fixtures  # noqa: F401

import compile as compiler
import state_store


def _share_error() -> OSError:
    error = PermissionError("sharing violation")
    error.winerror = 32
    return error


_READER_SCRIPT = """
from pathlib import Path
import sys
import time

target, ready, release = map(Path, sys.argv[1:])
with target.open("rb"):
    ready.write_text("ready", encoding="ascii")
    while not release.exists():
        time.sleep(0.01)
"""


def _start_reader(root: Path, target: Path) -> tuple[subprocess.Popen[str], Path]:
    ready = root / "reader.ready"
    release = root / "reader.release"
    process = subprocess.Popen(
        [sys.executable, "-c", _READER_SCRIPT, str(target), str(ready), str(release)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        process.kill()
        process.wait(timeout=5)
        raise AssertionError("reader process did not acquire target")
    return process, release


def _release_reader(process: subprocess.Popen[str], release: Path) -> None:
    release.write_text("release", encoding="ascii")
    process.wait(timeout=5)


class ReplaceRetryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires a real Windows sharing boundary")
    def test_real_windows_reader_reports_win5_then_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            replacement = root / "replacement.json"
            target.write_text("old", encoding="utf-8")
            replacement.write_text("new", encoding="utf-8")
            process, release = _start_reader(root, target)
            try:
                with self.assertRaises(PermissionError) as caught:
                    state_store.replace_with_retry(replacement, target, timeout=0)
                self.assertEqual(caught.exception.errno, 13)
                self.assertEqual(getattr(caught.exception, "winerror", None), 5)
                _release_reader(process, release)
                state_store.replace_with_retry(replacement, target, timeout=0)
                self.assertEqual(target.read_text(encoding="utf-8"), "new")
            finally:
                if process.poll() is None:
                    _release_reader(process, release)

    @unittest.skipUnless(os.name == "nt", "requires a real Windows sharing boundary")
    def test_real_windows_reader_release_allows_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            target.write_text("old", encoding="utf-8")
            process, release = _start_reader(root, target)
            releaser = Thread(
                target=lambda: (
                    time.sleep(0.15),
                    release.write_text("release", encoding="ascii"),
                ),
                daemon=True,
            )
            try:
                releaser.start()
                state_store.atomic_write_text(target, "new", newline="")
                releaser.join(timeout=5)
                process.wait(timeout=5)
                self.assertEqual(target.read_text(encoding="utf-8"), "new")
                self.assertEqual(list(root.glob(".state.json.*.tmp")), [])
            finally:
                releaser.join(timeout=5)
                if process.poll() is None:
                    _release_reader(process, release)

    @unittest.skipUnless(os.name == "nt", "requires a real Windows sharing boundary")
    def test_real_windows_reader_permanent_conflict_is_bounded_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            target.write_text("old", encoding="utf-8")
            process, release = _start_reader(root, target)
            started = time.monotonic()
            try:
                with self.assertRaises(PermissionError):
                    state_store.atomic_write_text(target, "new", newline="")
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(target.read_text(encoding="utf-8"), "old")
                self.assertEqual(list(root.glob(".state.json.*.tmp")), [])
            finally:
                _release_reader(process, release)

    def test_windows_share_error_retries_until_reader_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            target.write_text("old", encoding="utf-8")
            calls = 0
            real_replace = state_store.os.replace

            def replace(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise _share_error()
                real_replace(source, destination)

            with mock.patch.object(state_store.os, "name", "nt"), \
                 mock.patch.object(state_store.os, "replace", side_effect=replace), \
                 mock.patch.object(state_store.time, "sleep"):
                state_store.atomic_write_text(target, "new", newline="")

            self.assertEqual(calls, 3)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(list(root.glob(".state.json.*.tmp")), [])

    def test_permanent_share_error_keeps_target_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            target.write_text("old", encoding="utf-8")
            def always_share(*_: object) -> None:
                raise _share_error()
            with mock.patch.object(state_store.os, "name", "nt"), \
                 mock.patch.object(state_store.os, "replace", side_effect=always_share), \
                 mock.patch.object(state_store.time, "sleep"):
                with self.assertRaises(PermissionError):
                    state_store.atomic_write_bytes(target, b"new")

            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(root.glob(".state.json.*.tmp")), [])

    def test_non_share_permission_error_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.md"
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(state_store.os, "name", "nt"), \
                 mock.patch.object(state_store.os, "replace", side_effect=PermissionError("denied")) as replace:
                with self.assertRaises(PermissionError):
                    state_store.atomic_write_text(target, "new", newline="")

            replace.assert_called_once()
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_nonfinite_retry_limits_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.write_text("new", encoding="utf-8")
            for kwargs in ({"timeout": float("inf")}, {"deadline": float("nan")}):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        state_store.replace_with_retry(source, target, **kwargs)

    def test_compile_atomic_copy_uses_shared_replace_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            target = root / "target.md"
            source.write_text("new", encoding="utf-8")
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(compiler, "replace_with_retry") as replace:
                compiler._atomic_copy(source, target)

            replace.assert_called_once()
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_compile_atomic_copy_cleans_temp_after_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            target = root / "target.md"
            source.write_text("new", encoding="utf-8")
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(
                compiler,
                "replace_with_retry",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaises(PermissionError):
                    compiler._atomic_copy(source, target)

            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(root.glob(".target.md.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
