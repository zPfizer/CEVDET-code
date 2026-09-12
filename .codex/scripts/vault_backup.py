"""Vault git deposunun tam geçmişini taşınabilir bundle olarak yedekler.

Yedek `git bundle --all` ile üretilir: bütün ref'ler ve commit geçmişi tek
dosyada taşınır, `git clone <bundle>` ile eksiksiz geri yüklenir. Bundle yalnız
commit edilmiş durumu kapsar; çalışma ağacındaki commit'lenmemiş değişiklikler
özette ayrıca raporlanır ki kullanıcı yedeğin neyi kapsamadığını görsün. Kaynak
ref'leri yayın öncesi yakalanan point-in-time snapshot ile bağlanır; karşılaştırma
sonrası yazılan commit bir sonraki yedeğin kapsamındadır. Canlı Git yazıcıları
için nanosaniye düzeyinde atomik snapshot garantisi verilmez.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Sequence

from file_lock import locked


BUNDLE_NAME = re.compile(r"vault-(\d{8}-\d{6})(?:-(\d+))?\.bundle$")
DEFAULT_KEEP = 14
GIT_TIMEOUT_SECONDS = 600
GIT_REPOSITORY_ENV = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
)


class BackupError(RuntimeError):
    """Yedek üretilemedi; mesaj kullanıcıya gösterilebilir."""


class _PruneError(BackupError):
    def __init__(self, removed: list[Path], cause: OSError) -> None:
        super().__init__(f"eski yedek silinemedi: {cause}")
        self.removed = removed


class _CompletedBackupWarning(RuntimeError):
    def __init__(
        self, bundle: Path, removed: list[Path], bundle_size: int, cause: BaseException,
    ) -> None:
        super().__init__(f"bundle yayımlandı ancak budama başarısız: {cause}")
        self.bundle = bundle
        self.removed = removed
        self.bundle_size = bundle_size
        self.cause = cause


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo), *args]
    environment = os.environ.copy()
    for key in GIT_REPOSITORY_ENV:
        environment.pop(key, None)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            env=environment,
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


def _ensure_no_lfs(vault: Path) -> None:
    try:
        lfs_files = _git(vault, "lfs", "ls-files", "--all")
    except BackupError as error:
        raise BackupError("Git LFS tam geçmişi doğrulanamadı; bundle üretilmedi") from error
    if lfs_files.stdout.strip():
        raise BackupError("Git LFS dosyaları desteklenmiyor; bundle üretilmedi")


def _ensure_no_submodules(vault: Path) -> None:
    history = _git(
        vault, "log", "--all", "--raw", "--format=", "--no-renames", "--root", "-m",
    ).stdout.splitlines()
    if any(
        len(fields) >= 2
        and (fields[0][1:] == "160000" or fields[1] == "160000")
        for line in history
        if (fields := line.split()) and line.startswith(":")
    ):
        raise BackupError("Git submodule dosyaları desteklenmiyor; bundle üretilmedi")


def _ensure_full_history(vault: Path) -> None:
    shallow = _git(vault, "rev-parse", "--is-shallow-repository").stdout.strip()
    if shallow != "false":
        raise BackupError("shallow Git deposu tam geçmiş bundle'ı desteklemiyor")


def _ref_snapshot(repo: Path) -> dict[str, str]:
    lines = _git(
        repo, "for-each-ref", "--format=%(refname) %(objectname)",
    ).stdout.splitlines()
    snapshot: dict[str, str] = {}
    for line in lines:
        ref, object_name = line.split(maxsplit=1)
        snapshot[ref] = object_name
    snapshot["HEAD"] = _git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
    return snapshot


def _bundle_ref_snapshot(repo: Path, bundle: Path) -> dict[str, str]:
    lines = _git(repo, "bundle", "list-heads", str(bundle)).stdout.splitlines()
    snapshot: dict[str, str] = {}
    try:
        for line in lines:
            object_name, ref = line.split(maxsplit=1)
            snapshot[ref] = object_name
    except ValueError as error:
        raise BackupError(f"bundle ref'leri okunamadı: {bundle}") from error
    return snapshot


def _validate_bundle_artifact(vault: Path, bundle: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=".bundle-check-", dir=bundle.parent) as temporary:
        mirror = Path(temporary) / "mirror.git"
        _git(vault, "clone", "-q", "--mirror", str(bundle), str(mirror))
        _ensure_full_history(mirror)
        _ensure_no_submodules(mirror)
        _ensure_no_lfs(mirror)


def _require_repo(vault: Path) -> None:
    if not vault.is_dir():
        raise BackupError(f"vault dizini yok: {vault}")
    top_level = _git(vault, "rev-parse", "--show-toplevel").stdout.strip()
    if not top_level:
        raise BackupError(f"vault Git kökü okunamadı: {vault}")
    discovered = Path(top_level).resolve()
    if os.path.normcase(str(discovered)) != os.path.normcase(str(vault)):
        raise BackupError(f"vault Git deposunun kökü olmalı: {vault}")


def _repository_identity(vault: Path) -> str:
    git_dir_text = _git(vault, "rev-parse", "--git-dir").stdout.strip()
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = vault / git_dir
    try:
        git_stat = git_dir.resolve().stat()
    except OSError as error:
        raise BackupError(f"vault Git dizini kimliği okunamadı: {git_dir}") from error
    birthtime = (
        getattr(git_stat, "st_birthtime_ns", git_stat.st_ctime_ns)
        if os.name == "nt" else ""
    )
    return (
        f"git-dir={git_dir.resolve()}"
        f"\nst_dev={git_stat.st_dev}\nst_ino={git_stat.st_ino}"
        f"\nst_birthtime_ns={birthtime}"
    )


def _owned_destination(dest: Path, vault: Path) -> Path:
    source_key = (
        os.path.normcase(str(vault)) + "\0" + _repository_identity(vault)
    ).encode("utf-8")
    source_id = hashlib.sha256(source_key).hexdigest()
    return dest / f".vault-{source_id}"


def _lock_target(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.guard")


def _namespace_is_link(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(junction_check) and junction_check())


def _bundle_files(dest: Path) -> list[Path]:
    files: list[Path] = []
    for path in Path(dest).glob("vault-*.bundle"):
        if not BUNDLE_NAME.fullmatch(path.name):
            continue
        if _namespace_is_link(path):
            raise BackupError(f"bundle adayı link olamaz: {path}")
        if path.is_file():
            files.append(path)
    return files


def _validate_owned_destination(dest: Path, owned_dest: Path) -> None:
    try:
        if _namespace_is_link(owned_dest):
            raise BackupError(f"yedek namespace'i link olamaz: {owned_dest}")
        owned_dest.mkdir(exist_ok=True)
        if _namespace_is_link(owned_dest):
            raise BackupError(f"yedek namespace'i link olamaz: {owned_dest}")
        if not owned_dest.is_dir():
            raise BackupError(f"yedek namespace'i dizin olmalı: {owned_dest}")
        resolved = owned_dest.resolve()
        if resolved != owned_dest or resolved.parent != dest:
            raise BackupError(f"yedek namespace'i hedef dışına taşamaz: {owned_dest}")
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"yedek namespace'i doğrulanamadı: {owned_dest}") from exc


def _validate_locked_source(vault: Path, owned_dest: Path) -> None:
    current_owned = _owned_destination(owned_dest.parent, vault)
    if current_owned != owned_dest:
        raise BackupError("vault kimliği lock edinildikten sonra değişti")
    _validate_owned_destination(owned_dest.parent, owned_dest)


def _prepare_dest(vault: Path, dest: Path) -> tuple[Path, Path]:
    vault = Path(vault).resolve()
    dest = Path(dest).resolve()
    _require_repo(vault)
    if dest == vault or vault in dest.parents:
        raise BackupError(f"yedek hedefi vault içinde olamaz: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    owned_dest = _owned_destination(dest, vault)
    _validate_owned_destination(dest, owned_dest)
    return vault, owned_dest


def _unique_bundle_path(dest: Path, stamp: str) -> Path:
    bundles = _bundle_files(dest)
    if not bundles:
        return dest / f"vault-{stamp}.bundle"
    suffix = max(_bundle_sort_key(path)[0] for path in bundles) + 1
    return dest / f"vault-{stamp}-{suffix}.bundle"


def _create_bundle_locked(vault: Path, dest: Path, *, now: float | None = None) -> Path:
    _ensure_full_history(vault)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    final = _unique_bundle_path(dest, stamp)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{final.name}-", suffix=".tmp", dir=dest,
    )
    os.close(descriptor)
    partial = Path(temporary_name)
    try:
        _git(vault, "bundle", "create", str(partial), "--all")
        _git(vault, "bundle", "verify", str(partial))
        _validate_bundle_artifact(vault, partial)
        bundle_refs = _bundle_ref_snapshot(vault, partial)
        source_refs = _ref_snapshot(vault)
        # This comparison binds the published artifact to one source snapshot;
        # a later writer belongs to a subsequent point-in-time backup.
        if bundle_refs != source_refs:
            raise BackupError("Vault ref'leri bundle snapshot'ı sırasında değişti")
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
    return final


def create_bundle(vault: Path, dest: Path, *, now: float | None = None) -> Path:
    """Bundle'ı geçici ada yazar, doğrular, sonra son adına taşır."""
    vault, owned_dest = _prepare_dest(vault, dest)
    with locked(_lock_target(owned_dest)):
        _validate_locked_source(vault, owned_dest)
        return _create_bundle_locked(vault, owned_dest, now=now)


def _bundle_sort_key(path: Path) -> tuple[int, str]:
    match = BUNDLE_NAME.fullmatch(path.name)
    if match is None:
        raise ValueError(f"geçersiz bundle adı: {path.name}")
    return int(match.group(2) or "0"), match.group(1)


def _validate_keep(keep: int) -> None:
    if keep < 1:
        raise BackupError(f"keep en az 1 olmalı: {keep}")


def _verify_bundle(vault: Path, bundle: Path) -> None:
    try:
        _validate_bundle_artifact(vault, bundle)
    except BackupError as error:
        raise BackupError(f"yedek bundle doğrulanamadı: {bundle}") from error


def _prune_bundles_locked(
    vault: Path, dest: Path, keep: int, *, preserve: Path | None = None,
) -> list[Path]:
    _validate_keep(keep)
    bundles = sorted(_bundle_files(dest), key=_bundle_sort_key, reverse=True)
    for bundle in bundles:
        _verify_bundle(vault, bundle)
    if preserve is None:
        retained = set(bundles[:keep])
    else:
        if preserve not in bundles:
            raise BackupError(f"güncel bundle bulunamadı: {preserve}")
        retained = {preserve}
        for path in bundles:
            if path != preserve and len(retained) < keep:
                retained.add(path)
    removed: list[Path] = []
    for stale in bundles:
        if stale in retained:
            continue
        try:
            stale.unlink()
        except OSError as error:
            raise _PruneError(removed, error) from error
        removed.append(stale)
    return removed


def prune_bundles(dest: Path, keep: int, *, vault: Path) -> list[Path]:
    """Yalnız `vault` kaynak namespace'inin en yeni bundle'larını tutar."""
    _vault, owned_dest = _prepare_dest(vault, dest)
    with locked(_lock_target(owned_dest)):
        _validate_locked_source(_vault, owned_dest)
        return _prune_bundles_locked(_vault, owned_dest, keep)


def _create_and_prune(
    vault: Path, dest: Path, keep: int, *, now: float | None = None,
) -> tuple[Path, list[Path], int]:
    _validate_keep(keep)
    vault, owned_dest = _prepare_dest(vault, dest)
    with locked(_lock_target(owned_dest)):
        _validate_locked_source(vault, owned_dest)
        bundle = _create_bundle_locked(vault, owned_dest, now=now)
        try:
            removed = _prune_bundles_locked(
                vault, owned_dest, keep, preserve=bundle,
            )
        except _PruneError as error:
            raise _CompletedBackupWarning(
                bundle, error.removed, bundle.stat().st_size, error,
            ) from error
        except OSError as error:
            raise _CompletedBackupWarning(
                bundle, [], bundle.stat().st_size, error,
            ) from error
        except BackupError as error:
            raise _CompletedBackupWarning(
                bundle, [], bundle.stat().st_size, error,
            ) from error
        bundle_size = bundle.stat().st_size
    return bundle, removed, bundle_size


def working_tree_summary(vault: Path) -> tuple[int, int]:
    """(izlenen değişiklik, izlenmeyen dosya) sayıları; bundle bunları kapsamaz."""
    tracked, untracked, _ignored = _working_tree_summary(vault)
    return tracked, untracked


def _working_tree_summary(vault: Path) -> tuple[int, int, int]:
    status = _git(
        vault,
        "status", "--porcelain", "--untracked-files=all", "--ignored",
    )
    tracked = untracked = 0
    ignored = 0
    for line in status.stdout.splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line.startswith("!!"):
            ignored += 1
        elif line.strip():
            tracked += 1
    return tracked, untracked, ignored


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
    prune_warning: _CompletedBackupWarning | None = None
    try:
        bundle, removed, bundle_size = _create_and_prune(vault, dest, args.keep)
    except _CompletedBackupWarning as warning:
        bundle, removed, bundle_size = (
            warning.bundle, warning.removed, warning.bundle_size,
        )
        prune_warning = warning
    except BackupError as error:
        print(f"YEDEK BAŞARISIZ: {error}")
        return 1

    try:
        tracked, untracked, ignored = _working_tree_summary(vault)
    except BackupError as error:
        tracked = untracked = 0
        status_error = error
    else:
        status_error = None

    size_mb = bundle_size / (1024 * 1024)
    print(f"Yedek alındı: {bundle} ({size_mb:.1f} MB)")
    if removed:
        print(f"Budanan eski yedek: {len(removed)}")
    if prune_warning is not None:
        print(f"UYARI: eski yedek budaması tamamlanamadı — {prune_warning.cause}")
    if status_error is not None:
        print(f"UYARI: çalışma ağacı özeti alınamadı — {status_error}")
    elif tracked or untracked or ignored:
        print(
            "UYARI: commit'lenmemiş değişiklikler yedeğin dışında — "
            f"izlenen {tracked}, izlenmeyen {untracked}, yok sayılan {ignored} dosya."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
