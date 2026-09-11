"""Vault git deposunun tam geçmişini taşınabilir bundle olarak yedekler.

Yedek `git bundle --all` ile üretilir: bütün ref'ler ve commit geçmişi tek
dosyada taşınır, `git clone <bundle>` ile eksiksiz geri yüklenir. Bundle yalnız
commit edilmiş durumu kapsar; çalışma ağacındaki commit'lenmemiş değişiklikler
özette ayrıca raporlanır ki kullanıcı yedeğin neyi kapsamadığını görsün.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Sequence

BUNDLE_NAME = re.compile(r"vault-\d{8}-\d{6}(?:-\d+)?\.bundle$")
DEFAULT_KEEP = 14
GIT_TIMEOUT_SECONDS = 600


class BackupError(RuntimeError):
    """Yedek üretilemedi; mesaj kullanıcıya gösterilebilir."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo), *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"git çalıştırılamadı: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise BackupError(
            "git komutu başarısız: " + " ".join(args)
            + (f" — {detail[-1]}" if detail else "")
        )
    return result


def _require_repo(vault: Path) -> None:
    if not vault.is_dir():
        raise BackupError(f"vault dizini yok: {vault}")
    _git(vault, "rev-parse", "--git-dir")


def _unique_bundle_path(dest: Path, stamp: str) -> Path:
    candidate = dest / f"vault-{stamp}.bundle"
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = dest / f"vault-{stamp}-{counter}.bundle"
    return candidate


def create_bundle(vault: Path, dest: Path, *, now: float | None = None) -> Path:
    """Bundle'ı geçici ada yazar, doğrular, sonra son adına taşır."""
    vault = Path(vault).resolve()
    dest = Path(dest).resolve()
    _require_repo(vault)
    if dest == vault or vault in dest.parents:
        raise BackupError(f"yedek hedefi vault içinde olamaz: {dest}")
    dest.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    final = _unique_bundle_path(dest, stamp)
    partial = final.with_name(final.name + ".tmp")
    try:
        _git(vault, "bundle", "create", str(partial), "--all")
        _git(vault, "bundle", "verify", str(partial))
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
    return final


def prune_bundles(dest: Path, keep: int) -> list[Path]:
    """En yeni `keep` bundle kalır; desene uymayan dosyalara dokunulmaz."""
    if keep < 1:
        raise BackupError(f"keep en az 1 olmalı: {keep}")
    bundles = sorted(
        (path for path in Path(dest).glob("vault-*.bundle") if BUNDLE_NAME.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    removed: list[Path] = []
    for stale in bundles[keep:]:
        stale.unlink()
        removed.append(stale)
    return removed


def working_tree_summary(vault: Path) -> tuple[int, int]:
    """(izlenen değişiklik, izlenmeyen dosya) sayıları; bundle bunları kapsamaz."""
    status = _git(vault, "status", "--porcelain")
    tracked = untracked = 0
    for line in status.stdout.splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line.strip():
            tracked += 1
    return tracked, untracked


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_vault = Path(__file__).resolve().parents[2]
    parser.add_argument("--vault", type=Path, default=default_vault)
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="yedek dizini (varsayılan: vault'un yanında <vault>-yedek)",
    )
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    args = parser.parse_args(argv)

    vault = Path(args.vault).resolve()
    dest = args.dest if args.dest is not None else vault.parent / f"{vault.name}-yedek"
    try:
        bundle = create_bundle(vault, dest)
        removed = prune_bundles(dest, args.keep)
        tracked, untracked = working_tree_summary(vault)
    except BackupError as error:
        print(f"YEDEK BAŞARISIZ: {error}")
        return 1

    size_mb = bundle.stat().st_size / (1024 * 1024)
    print(f"Yedek alındı: {bundle} ({size_mb:.1f} MB)")
    if removed:
        print(f"Budanan eski yedek: {len(removed)}")
    if tracked or untracked:
        print(
            "UYARI: commit'lenmemiş değişiklikler yedeğin dışında — "
            f"izlenen {tracked}, izlenmeyen {untracked} dosya."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
