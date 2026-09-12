"""Kilit protokolünün tek sahibi: yol türetme, açma, edinim, bırakma.

Çağrı yerleri yalnız `locked(path, timeout=...)` bilir; `.lock` sidecar adı,
handle ömrü ve platform farkı burada kalır.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import time
from contextlib import contextmanager
from typing import IO, Iterator


POLL_SECONDS = 0.05


class LockUnavailable(RuntimeError):
    pass


def timeout_for_deadline(
    deadline: float | None,
    *,
    cap: float | None = None,
) -> float | None:
    """Return the remaining lock timeout, optionally bounded by ``cap``."""
    if deadline is None:
        return cap
    remaining = max(0.0, deadline - time.monotonic())
    if cap is not None:
        remaining = min(remaining, max(0.0, cap))
    return remaining


@contextmanager
def locked(path: Path, *, timeout: float | None = None) -> Iterator[IO[str]]:
    """Blok boyunca `path`in `.lock` sidecar'ı üzerinde dışlayıcı kilit tutar.

    `timeout=None` sahibi bırakana kadar bekler (iki platformda da).
    `timeout=0` bir kez dener. Pozitif timeout deadline'a kadar dener.
    Deadline kilit başkasındayken dolarsa `LockUnavailable`; başka her edinim
    hatası olduğu gibi yükselir.
    """
    lock_path = Path(path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        _acquire(handle, timeout)
        yield handle


def _acquire(handle: IO[str], timeout: float | None) -> None:
    if os.name == "nt":
        # Kilitlenecek en az bir bayt olmalı; edinim bayt 0 üzerinden yürür.
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write("\0")
            handle.flush()
        handle.seek(0)
    deadline = None if timeout is None else time.monotonic() + timeout
    while not _try_acquire(handle):
        if deadline is not None and time.monotonic() >= deadline:
            raise LockUnavailable("lock-busy")
        time.sleep(POLL_SECONDS)


def _try_acquire(handle: IO[str]) -> bool:
    """Kilit alındıysa True, başkası tutuyorsa False; gerçek hata yükselir."""
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno == errno.EACCES:
                return False
            raise
        return True
    else:  # pragma: no cover — POSIX dalı Windows'ta (yerel + CI) koşamaz.
        import fcntl

        flock = getattr(fcntl, "flock", None)
        lock_ex = getattr(fcntl, "LOCK_EX", None)
        lock_nb = getattr(fcntl, "LOCK_NB", None)
        if not callable(flock) or not isinstance(lock_ex, int) or not isinstance(lock_nb, int):
            raise OSError(errno.ENOSYS, "fcntl locking is unavailable")

        try:
            flock(handle.fileno(), lock_ex | lock_nb)
        except BlockingIOError:
            return False
        return True
