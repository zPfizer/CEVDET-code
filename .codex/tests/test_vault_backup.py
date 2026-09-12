from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import vault_backup


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    (repo / "not.md").write_text("kalıcı içerik", encoding="utf-8")
    _git(repo, "add", "not.md")
    _git(
        repo,
        "-c", "user.name=test",
        "-c", "user.email=test@example.invalid",
        "commit", "-q", "-m", "ilk",
    )


def _owned_dest(vault: Path, dest: Path) -> Path:
    owned = vault_backup._owned_destination(dest.resolve(), vault.resolve())
    owned.mkdir(parents=True, exist_ok=True)
    return owned


class VaultBackupTests(unittest.TestCase):
    def test_bundle_clone_restores_committed_content(self) -> None:
        # Yedeğin gerçek garantisi doğrulama değil geri yüklenebilirlik:
        # bundle'dan clone alınıp içerik birebir okunabilmeli.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            _init_repo(vault)

            bundle = vault_backup.create_bundle(vault, root / "yedek")
            restored = root / "restored"
            _git(root, "clone", "-q", str(bundle), str(restored))

            self.assertEqual(
                (restored / "not.md").read_text(encoding="utf-8"),
                "kalıcı içerik",
            )
            self.assertRegex(bundle.name, r"vault-\d{8}-\d{6}\.bundle")
            self.assertFalse(list((root / "yedek").glob("*.tmp")))

    def test_dest_inside_vault_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            _init_repo(vault)
            with self.assertRaises(vault_backup.BackupError):
                vault_backup.create_bundle(vault, vault / "yedek")

    def test_repo_without_commits_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            _git(vault, "init", "-q")
            with self.assertRaises(vault_backup.BackupError):
                vault_backup.create_bundle(vault, root / "yedek")

    def test_subdirectory_vault_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            _init_repo(repository)
            vault = repository / "subdir"
            vault.mkdir()

            with self.assertRaises(vault_backup.BackupError):
                vault_backup.create_bundle(vault, root / "yedek")

    def test_linked_source_namespace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            outside = root / "outside"
            _init_repo(vault)
            outside.mkdir()
            namespace = _owned_dest(vault, dest)
            namespace.rmdir()
            try:
                namespace.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlink unavailable: {error}")

            with self.assertRaises(vault_backup.BackupError):
                vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(outside.glob("vault-*.bundle")))

    def test_prune_keeps_newest_and_ignores_foreign_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            _init_repo(vault)
            dest = root / "yedek"
            owned = _owned_dest(vault, dest)
            names = [
                "vault-20260901-120000.bundle",
                "vault-20260902-120000.bundle",
                "vault-20260903-120000.bundle",
                "vault-20260903-120000-1.bundle",
            ]
            for name in names:
                (owned / name).write_bytes(b"x")
            foreign = owned / "vault-notlar.bundle"
            foreign.write_bytes(b"x")

            removed = vault_backup.prune_bundles(dest, keep=2, vault=vault)

            self.assertEqual(
                sorted(path.name for path in removed),
                ["vault-20260901-120000.bundle", "vault-20260902-120000.bundle"],
            )
            self.assertTrue((owned / "vault-20260903-120000.bundle").exists())
            self.assertTrue((owned / "vault-20260903-120000-1.bundle").exists())
            self.assertTrue(foreign.exists())

    def test_prune_orders_same_second_suffixes_numerically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            _init_repo(vault)
            dest = root / "yedek"
            owned = _owned_dest(vault, dest)
            names = [
                "vault-20260903-120000.bundle",
                "vault-20260903-120000-1.bundle",
                "vault-20260903-120000-9.bundle",
                "vault-20260903-120000-10.bundle",
            ]
            for name in names:
                (owned / name).write_bytes(b"x")

            removed = vault_backup.prune_bundles(dest, keep=1, vault=vault)

            self.assertEqual(
                sorted(path.name for path in removed),
                [
                    "vault-20260903-120000-1.bundle",
                    "vault-20260903-120000-9.bundle",
                    "vault-20260903-120000.bundle",
                ],
            )
            self.assertTrue((owned / "vault-20260903-120000-10.bundle").exists())

    def test_clock_rollback_keeps_current_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)

            future = vault_backup.create_bundle(vault, dest, now=1_900_000_000)
            current, removed, bundle_size = vault_backup._create_and_prune(
                vault, dest, keep=1, now=1_600_000_000,
            )

            self.assertEqual(removed, [future])
            self.assertFalse(future.exists())
            self.assertTrue(current.exists())
            self.assertGreater(bundle_size, 0)

    def test_same_second_newer_commit_survives_prune(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            stamp = 1_758_000_000

            first = vault_backup.create_bundle(vault, dest, now=stamp)
            (vault / "not.md").write_text("güncel içerik", encoding="utf-8")
            _git(vault, "add", "not.md")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "ikinci",
            )
            second = vault_backup.create_bundle(vault, dest, now=stamp)

            removed = vault_backup.prune_bundles(dest, keep=1, vault=vault)
            restored = root / "restored"
            _git(root, "clone", "-q", str(second), str(restored))

            self.assertIn(first, removed)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(
                (restored / "not.md").read_text(encoding="utf-8"),
                "güncel içerik",
            )

    def test_same_second_suffixes_continue_after_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            stamp = 1_758_000_000

            first = vault_backup.create_bundle(vault, dest, now=stamp)
            vault_backup.prune_bundles(dest, keep=1, vault=vault)
            (vault / "not.md").write_text("ikinci içerik", encoding="utf-8")
            _git(vault, "add", "not.md")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "ikinci",
            )
            second = vault_backup.create_bundle(vault, dest, now=stamp)
            vault_backup.prune_bundles(dest, keep=1, vault=vault)
            (vault / "not.md").write_text("üçüncü içerik", encoding="utf-8")
            _git(vault, "add", "not.md")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "üçüncü",
            )
            third = vault_backup.create_bundle(vault, dest, now=stamp)
            removed = vault_backup.prune_bundles(dest, keep=1, vault=vault)

            stamp_name = first.name[len("vault-"):-len(".bundle")]
            self.assertEqual(second.name, f"vault-{stamp_name}-1.bundle")
            self.assertEqual(third.name, f"vault-{stamp_name}-2.bundle")
            self.assertEqual(removed, [second])
            self.assertTrue(third.exists())

    def test_lfs_pointer_is_rejected_before_bundle_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            (vault / ".gitattributes").write_text(
                "large.bin filter=lfs diff=lfs merge=lfs -text\n",
                encoding="utf-8",
            )
            (vault / "large.bin").write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:0123456789abcdef\n"
                "size 42\n",
                encoding="utf-8",
            )
            _git(vault, "add", ".gitattributes", "large.bin")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "lfs",
            )

            with self.assertRaises(vault_backup.BackupError):
                vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))
            self.assertFalse(list(dest.rglob("*.tmp")))

    def test_invalid_keep_does_not_publish_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)

            for keep in (0, -1):
                with self.subTest(keep=keep):
                    self.assertEqual(
                        vault_backup.main(
                            [
                                "--vault", str(vault),
                                "--dest", str(dest),
                                "--keep", str(keep),
                            ],
                        ),
                        1,
                    )
                    self.assertFalse(list(dest.rglob("vault-*.bundle")))

    def test_main_captures_bundle_size_before_a_second_backup_prunes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            real_create_and_prune = vault_backup._create_and_prune
            results = []

            def create_and_prune_then_race(
                source: Path, target: Path, keep: int, *, now: float | None = None,
            ):
                result = real_create_and_prune(source, target, keep, now=now)
                results.append(result)
                if len(results) == 1:
                    results.append(real_create_and_prune(source, target, keep, now=now))
                return result

            with mock.patch.object(
                vault_backup,
                "_create_and_prune",
                side_effect=create_and_prune_then_race,
            ):
                exit_code = vault_backup.main(
                    ["--vault", str(vault), "--dest", str(dest), "--keep", "1"],
                )

            self.assertEqual(exit_code, 0)
            self.assertFalse(results[0][0].exists())
            self.assertTrue(results[1][0].exists())

    def test_shared_destination_prunes_only_source_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_vault = root / "first"
            second_vault = root / "second"
            dest = root / "shared-yedek"
            _init_repo(first_vault)
            _init_repo(second_vault)

            first_old = vault_backup.create_bundle(first_vault, dest, now=1_758_000_000)
            (first_vault / "not.md").write_text("ilk güncel", encoding="utf-8")
            _git(first_vault, "add", "not.md")
            _git(
                first_vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "ikinci",
            )
            first_new = vault_backup.create_bundle(first_vault, dest, now=1_758_000_000)
            second_bundle = vault_backup.create_bundle(second_vault, dest, now=1_758_000_000)

            removed = vault_backup.prune_bundles(dest, keep=1, vault=first_vault)

            self.assertEqual(removed, [first_old])
            self.assertFalse(first_old.exists())
            self.assertTrue(first_new.exists())
            self.assertTrue(second_bundle.exists())

    def test_concurrent_bundles_use_distinct_owned_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            child = """
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import vault_backup
real_git = vault_backup._git
def slow_git(repo, *args):
    if args[:2] == ("bundle", "create"):
        time.sleep(0.2)
    return real_git(repo, *args)
vault_backup._git = slow_git
print(vault_backup.create_bundle(Path(sys.argv[2]), Path(sys.argv[3]), now=1758000000), flush=True)
"""
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        child,
                        str(CODEX_DIR / "scripts"),
                        str(vault),
                        str(dest),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(2)
            ]

            results = [process.communicate(timeout=20) for process in processes]
            self.assertTrue(all(process.returncode == 0 for process in processes), results)
            bundles = [Path(stdout.decode("utf-8").strip()) for stdout, _ in results]

            self.assertEqual(len(set(bundles)), 2)
            self.assertTrue(all(bundle.exists() for bundle in bundles))
            self.assertFalse(list((dest).rglob("*.tmp")))

    def test_working_tree_summary_counts_uncommitted_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            _init_repo(vault)
            (vault / "not.md").write_text("değişti", encoding="utf-8")
            (vault / "yeni.md").write_text("izlenmiyor", encoding="utf-8")
            _git(vault, "config", "status.showUntrackedFiles", "no")

            tracked, untracked = vault_backup.working_tree_summary(vault)

        self.assertEqual((tracked, untracked), (1, 1))


if __name__ == "__main__":
    unittest.main()
