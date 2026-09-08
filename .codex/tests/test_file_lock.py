"""file_lock kilit protokolünün tek sahibinin testleri.

T01: `locked()` yol türetmeyi, açmayı, edinimi ve bırakmayı tek yerde tutar.
Süre assert'i yok — bekleme davranışı sahte saat + sayaçla kanıtlanır.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (sys.path seam)

import file_lock  # noqa: E402


PRIMITIVE = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"


class LockedContextTests(unittest.TestCase):
    def test_lock_file_is_the_resource_path_with_lock_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "nested" / "health.json"
            with file_lock.locked(resource) as handle:
                self.assertEqual(Path(handle.name), resource.with_suffix(".lock"))
            self.assertTrue(resource.with_suffix(".lock").is_file())
            self.assertFalse(resource.exists())

    def test_lock_path_argument_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "worker-queue.lock"
            with file_lock.locked(lock_path) as handle:
                self.assertEqual(Path(handle.name), lock_path)

    def test_timeout_zero_raises_while_another_handle_holds_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "guarded.json"
            with file_lock.locked(resource):
                with self.assertRaises(file_lock.LockUnavailable):
                    with file_lock.locked(resource, timeout=0):
                        pass

    def test_lock_is_acquirable_again_after_the_holder_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "guarded.json"
            with file_lock.locked(resource):
                pass
            with file_lock.locked(resource, timeout=0) as handle:
                self.assertFalse(handle.closed)

    def test_positive_timeout_retries_then_raises_at_the_deadline(self) -> None:
        clock = [0.0]
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "guarded.json"
            with file_lock.locked(resource):
                with (
                    mock.patch.object(file_lock.time, "monotonic", lambda: clock[0]),
                    mock.patch.object(file_lock.time, "sleep", fake_sleep),
                ):
                    with self.assertRaises(file_lock.LockUnavailable):
                        with file_lock.locked(resource, timeout=1.0):
                            pass
        self.assertGreater(len(sleeps), 1)
        self.assertGreaterEqual(clock[0], 1.0)

    def test_default_timeout_waits_past_the_windows_ten_second_cliff(self) -> None:
        """R05: timeout=None iki platformda da bekler; 10 denemede pes etmez."""
        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "guarded.json"
            holder = file_lock.locked(resource)
            holder.__enter__()
            attempts: list[float] = []

            def release_after_twelve(seconds: float) -> None:
                attempts.append(seconds)
                if len(attempts) == 12:
                    holder.__exit__(None, None, None)

            with mock.patch.object(file_lock.time, "sleep", release_after_twelve):
                with file_lock.locked(resource) as handle:
                    self.assertFalse(handle.closed)

        self.assertEqual(len(attempts), 12)

    def test_non_contention_os_error_is_not_reported_as_lock_busy(self) -> None:
        """R06: izin/bad-handle hatası LockUnavailable'a dönüşmez, yükselir."""
        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "guarded.json"
            failure = OSError(errno.EBADF, "bad file descriptor")
            with mock.patch(PRIMITIVE, side_effect=failure):
                with self.assertRaises(OSError) as caught:
                    with file_lock.locked(resource, timeout=0):
                        pass
        self.assertNotIsInstance(caught.exception, file_lock.LockUnavailable)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_contention_error_still_classifies_as_lock_busy(self) -> None:
        contention = (
            OSError(errno.EACCES, "locking violation")
            if os.name == "nt"
            else BlockingIOError(errno.EAGAIN, "resource temporarily unavailable")
        )
        with tempfile.TemporaryDirectory() as temporary:
            resource = Path(temporary) / "guarded.json"
            with mock.patch(PRIMITIVE, side_effect=contention):
                with self.assertRaises(file_lock.LockUnavailable):
                    with file_lock.locked(resource, timeout=0):
                        pass


if __name__ == "__main__":
    unittest.main()
