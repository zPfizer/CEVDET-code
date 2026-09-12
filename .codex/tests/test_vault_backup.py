from contextlib import contextmanager, redirect_stdout
from io import StringIO
import os
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

    def test_git_repository_env_cannot_redirect_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            other = root / "other"
            dest = root / "yedek"
            _init_repo(vault)
            _init_repo(other)
            (other / "not.md").write_text("başka depo", encoding="utf-8")
            _git(other, "add", "not.md")
            _git(
                other,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "başka",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "GIT_DIR": str(other / ".git"),
                    "GIT_WORK_TREE": str(vault),
                    "GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects"),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git" / "objects"),
                },
                clear=False,
            ):
                bundle = vault_backup.create_bundle(vault, dest)

            restored = root / "restored"
            _git(root, "clone", "-q", str(bundle), str(restored))
            self.assertEqual(
                (restored / "not.md").read_text(encoding="utf-8"),
                "kalıcı içerik",
            )

    def test_recreated_vault_uses_a_distinct_backup_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            old_bundle = vault_backup.create_bundle(vault, dest)
            old_namespace = old_bundle.parent

            vault.rename(root / "old-vault")
            _init_repo(vault)
            (vault / "not.md").write_text("yeni depo", encoding="utf-8")
            _git(vault, "add", "not.md")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "yeni",
            )
            new_bundle = vault_backup.create_bundle(vault, dest)
            removed = vault_backup.prune_bundles(dest, keep=1, vault=vault)

            self.assertNotEqual(old_namespace, new_bundle.parent)
            self.assertEqual(removed, [])
            self.assertTrue(old_bundle.exists())
            self.assertTrue(new_bundle.exists())

    def test_same_root_clone_at_reused_path_gets_distinct_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "template"
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(template)
            _git(root, "clone", "-q", template.as_uri(), str(vault))
            old_bundle = vault_backup.create_bundle(vault, dest)
            old_namespace = old_bundle.parent

            vault.rename(root / "old-vault")
            _git(root, "clone", "-q", template.as_uri(), str(vault))
            new_bundle = vault_backup.create_bundle(vault, dest)
            removed = vault_backup.prune_bundles(dest, keep=1, vault=vault)

            self.assertNotEqual(old_namespace, new_bundle.parent)
            self.assertEqual(removed, [])
            self.assertTrue(old_bundle.exists())
            self.assertTrue(new_bundle.exists())

    def test_namespace_stays_stable_when_root_set_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            first = vault_backup.create_bundle(vault, dest, now=1_758_000_000)

            tree = _git(vault, "rev-parse", "HEAD^{tree}").stdout.strip()
            orphan = _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit-tree", tree, "-m", "orphan",
            ).stdout.strip()
            _git(vault, "update-ref", "refs/heads/orphan", orphan)

            second = vault_backup.create_bundle(vault, dest, now=1_758_000_001)

            self.assertEqual(first.parent, second.parent)

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

    def test_linked_worktree_heads_are_included_in_bundle_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            linked = root / "linked"
            dest = root / "yedek"
            _init_repo(vault)
            _git(vault, "worktree", "add", "-q", str(linked))

            bundle = vault_backup.create_bundle(vault, dest)

            heads = _git(vault, "bundle", "list-heads", str(bundle)).stdout.splitlines()
            self.assertTrue(any(" worktrees/" in line and line.endswith("/HEAD") for line in heads))

    def test_shallow_repository_is_rejected_before_bundle_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            vault = root / "shallow"
            dest = root / "yedek"
            _init_repo(source)
            (source / "not.md").write_text("ikinci içerik", encoding="utf-8")
            _git(source, "add", "not.md")
            _git(
                source,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "ikinci",
            )
            _git(
                root,
                "-c", "protocol.file.allow=always",
                "clone", "-q", "--depth", "1", source.as_uri(), str(vault),
            )

            self.assertEqual(
                _git(vault, "rev-parse", "--is-shallow-repository").stdout.strip(),
                "true",
            )
            with self.assertRaises(vault_backup.BackupError):
                vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))

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

            with mock.patch.object(vault_backup, "_verify_bundle"):
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

            with mock.patch.object(vault_backup, "_verify_bundle"):
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

    def test_prune_ignores_bundle_named_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            owned = _owned_dest(vault, dest)
            directory = owned / "vault-20200101-000000.bundle"
            directory.mkdir()

            bundle = vault_backup.create_bundle(vault, dest, now=1_758_000_000)
            removed = vault_backup.prune_bundles(dest, keep=1, vault=vault)

            self.assertEqual(removed, [])
            self.assertTrue(bundle.exists())
            self.assertTrue(directory.is_dir())

    def test_prune_rejects_bundle_named_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            outside = root / "outside.bundle"
            _init_repo(vault)
            owned = _owned_dest(vault, dest)
            real_bundle = owned / "vault-20200101-000000.bundle"
            real_bundle.write_bytes(b"bundle")
            outside.write_bytes(b"outside")
            link = owned / "vault-20990101-000000-999.bundle"
            try:
                link.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"file symlink unavailable: {error}")

            with self.assertRaises(vault_backup.BackupError):
                vault_backup.prune_bundles(dest, keep=1, vault=vault)

            self.assertTrue(real_bundle.exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue(outside.exists())

    def test_prune_fails_closed_on_corrupt_newer_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            valid = vault_backup.create_bundle(vault, dest, now=1_758_000_000)
            corrupt = valid.parent / "vault-20990101-000000.bundle"
            bundle_bytes = valid.read_bytes()
            corrupt.write_bytes(bundle_bytes[:-1])

            self.assertEqual(
                _git(vault, "bundle", "verify", str(corrupt)).returncode,
                0,
            )

            with self.assertRaises(vault_backup.BackupError):
                vault_backup.prune_bundles(dest, keep=1, vault=vault)

            self.assertTrue(valid.exists())
            self.assertTrue(corrupt.exists())

    def test_prune_fails_closed_on_foreign_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            foreign_vault = root / "foreign"
            dest = root / "yedek"
            foreign_dest = root / "foreign-yedek"
            _init_repo(vault)
            _init_repo(foreign_vault)
            (foreign_vault / "not.md").write_text("başka depo", encoding="utf-8")
            _git(foreign_vault, "add", "not.md")
            _git(
                foreign_vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "başka",
            )

            valid = vault_backup.create_bundle(vault, dest, now=1_758_000_000)
            foreign = vault_backup.create_bundle(foreign_vault, foreign_dest)
            copied = valid.parent / "vault-20990101-000000.bundle"
            copied.write_bytes(foreign.read_bytes())

            with self.assertRaises(vault_backup.BackupError):
                vault_backup.prune_bundles(dest, keep=1, vault=vault)

            self.assertTrue(valid.exists())
            self.assertTrue(copied.exists())

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

    def test_clock_rollback_retains_latest_created_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)

            first = vault_backup.create_bundle(vault, dest, now=1_900_000_000)
            second, removed_second, _ = vault_backup._create_and_prune(
                vault, dest, keep=2, now=1_600_000_000,
            )
            third, removed_third, _ = vault_backup._create_and_prune(
                vault, dest, keep=2, now=1_600_000_001,
            )

            self.assertEqual(removed_second, [])
            self.assertEqual(removed_third, [first])
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(third.exists())

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
                "oid sha256:0123456789abcdef0123456789abcdef"
                "0123456789abcdef0123456789abcdef\n"
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

    def test_lfs_literal_in_source_is_not_a_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            (vault / "script.py").write_text(
                'POINTER_HEADER = "version https://git-lfs.github.com/spec/v1\\n"\n',
                encoding="utf-8",
            )
            _git(vault, "add", "script.py")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "literal",
            )

            real_git = vault_backup._git

            def successful_lfs_probe(
                repo: Path, *args: str,
            ) -> subprocess.CompletedProcess[str]:
                if args == ("lfs", "ls-files", "--all"):
                    return subprocess.CompletedProcess(["git"], 0, "", "")
                return real_git(repo, *args)

            with mock.patch.object(vault_backup, "_git", side_effect=successful_lfs_probe):
                bundle = vault_backup.create_bundle(vault, dest)

            self.assertTrue(bundle.exists())

    def test_lfs_probe_failure_is_rejected_without_full_history_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            real_git = vault_backup._git

            def failed_lfs_command(
                repo: Path, *args: str,
            ) -> subprocess.CompletedProcess[str]:
                if args == ("lfs", "ls-files", "--all"):
                    raise vault_backup.BackupError("LFS probe failed")
                return real_git(repo, *args)

            with mock.patch.object(vault_backup, "_git", side_effect=failed_lfs_command):
                with self.assertRaises(vault_backup.BackupError):
                    vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))

    def test_tracked_submodule_is_rejected_before_bundle_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            head = _git(vault, "rev-parse", "HEAD").stdout.strip()
            _git(vault, "update-index", "--add", "--cacheinfo", f"160000,{head},submodule")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "submodule",
            )
            _git(vault, "update-index", "--force-remove", "submodule")

            with self.assertRaises(vault_backup.BackupError):
                vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))

    def test_submodule_on_other_ref_is_rejected_before_bundle_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            _git(vault, "checkout", "-q", "-b", "with-submodule")
            head = _git(vault, "rev-parse", "HEAD").stdout.strip()
            _git(vault, "update-index", "--add", "--cacheinfo", f"160000,{head},submodule")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "submodule",
            )
            _git(vault, "checkout", "-q", "-")

            with self.assertRaises(vault_backup.BackupError):
                vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))

    def test_status_failure_after_publish_is_reported_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            output = StringIO()

            with mock.patch.object(
                vault_backup,
                "_working_tree_summary",
                side_effect=vault_backup.BackupError("status okunamadı"),
            ), redirect_stdout(output):
                exit_code = vault_backup.main(
                    ["--vault", str(vault), "--dest", str(dest), "--keep", "1"],
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("Yedek alındı:", output.getvalue())
            self.assertIn("çalışma ağacı özeti alınamadı", output.getvalue())
            self.assertNotIn("YEDEK BAŞARISIZ", output.getvalue())

    def test_prune_failure_after_publish_is_reported_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            output = StringIO()

            with mock.patch.object(
                vault_backup,
                "_prune_bundles_locked",
                side_effect=PermissionError("yedek kilitli"),
            ), redirect_stdout(output):
                exit_code = vault_backup.main(
                    ["--vault", str(vault), "--dest", str(dest), "--keep", "1"],
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("Yedek alındı:", output.getvalue())
            self.assertIn("budaması tamamlanamadı", output.getvalue())
            self.assertNotIn("YEDEK BAŞARISIZ", output.getvalue())
            self.assertTrue(list(dest.rglob("vault-*.bundle")))

    def test_source_ref_change_before_publish_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            real_validate = vault_backup._validate_bundle_artifact

            def validate_then_commit(source: Path, bundle: Path) -> None:
                real_validate(source, bundle)
                (source / "not.md").write_text("snapshot sonrası", encoding="utf-8")
                _git(source, "add", "not.md")
                _git(
                    source,
                    "-c", "user.name=test",
                    "-c", "user.email=test@example.invalid",
                    "commit", "-q", "-m", "snapshot sonrası",
                )

            with mock.patch.object(
                vault_backup,
                "_validate_bundle_artifact",
                side_effect=validate_then_commit,
            ):
                with self.assertRaises(vault_backup.BackupError):
                    vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))

    def test_late_final_bundle_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            real_unique = vault_backup._unique_bundle_path
            real_validate = vault_backup._validate_bundle_artifact
            final_path: Path | None = None

            def capture_final(namespace: Path, stamp: str) -> Path:
                nonlocal final_path
                final_path = real_unique(namespace, stamp)
                return final_path

            def validate_then_publish(source: Path, partial: Path) -> None:
                real_validate(source, partial)
                assert final_path is not None
                _git(source, "bundle", "create", str(final_path), "--all")

            with mock.patch.object(
                vault_backup, "_unique_bundle_path", side_effect=capture_final,
            ), mock.patch.object(
                vault_backup,
                "_validate_bundle_artifact",
                side_effect=validate_then_publish,
            ):
                with self.assertRaises(vault_backup.BackupError):
                    vault_backup.create_bundle(vault, dest)

            assert final_path is not None
            self.assertTrue(final_path.exists())
            self.assertFalse(list(dest.rglob("*.tmp")))

    def test_detached_head_change_before_publish_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            head = _git(vault, "rev-parse", "HEAD").stdout.strip()
            _git(vault, "checkout", "-q", head)
            real_validate = vault_backup._validate_bundle_artifact

            def validate_then_commit(source: Path, bundle: Path) -> None:
                real_validate(source, bundle)
                (source / "not.md").write_text("detached sonrası", encoding="utf-8")
                _git(source, "add", "not.md")
                _git(
                    source,
                    "-c", "user.name=test",
                    "-c", "user.email=test@example.invalid",
                    "commit", "-q", "-m", "detached sonrası",
                )

            with mock.patch.object(
                vault_backup,
                "_validate_bundle_artifact",
                side_effect=validate_then_commit,
            ):
                with self.assertRaises(vault_backup.BackupError):
                    vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))

    def test_source_identity_is_rechecked_after_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)

            @contextmanager
            def replace_source(_lock_path: Path):
                vault.rename(root / "old-vault")
                _init_repo(vault)
                yield

            with mock.patch.object(vault_backup, "locked", replace_source):
                with self.assertRaises(vault_backup.BackupError):
                    vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))

    def test_source_identity_is_rechecked_before_bundle_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            real_validate = vault_backup._validate_locked_source
            calls = 0

            def validate_then_replace(source: Path, owned: Path) -> None:
                nonlocal calls
                real_validate(source, owned)
                calls += 1
                if calls == 1:
                    vault.rename(root / "old-vault")
                    _init_repo(vault)

            with mock.patch.object(
                vault_backup,
                "_validate_locked_source",
                side_effect=validate_then_replace,
            ):
                with self.assertRaises(vault_backup.BackupError):
                    vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(dest.rglob("vault-*.bundle")))

    def test_namespace_replacement_is_rejected_before_temp_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            outside = root / "outside"
            outside.mkdir()
            _init_repo(vault)
            real_unique = vault_backup._unique_bundle_path

            def unique_then_replace(namespace: Path, stamp: str) -> Path:
                result = real_unique(namespace, stamp)
                namespace.rmdir()
                try:
                    namespace.symlink_to(outside, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"directory symlink unavailable: {error}")
                return result

            with mock.patch.object(
                vault_backup,
                "_unique_bundle_path",
                side_effect=unique_then_replace,
            ):
                with self.assertRaises(vault_backup.BackupError):
                    vault_backup.create_bundle(vault, dest)

            self.assertFalse(list(outside.glob("vault-*.bundle")))

    def test_main_reports_ignored_files_outside_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            dest = root / "yedek"
            _init_repo(vault)
            (vault / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
            _git(vault, "add", ".gitignore")
            _git(
                vault,
                "-c", "user.name=test",
                "-c", "user.email=test@example.invalid",
                "commit", "-q", "-m", "ignore",
            )
            (vault / "ignored.md").write_text("gizli", encoding="utf-8")
            output = StringIO()

            with redirect_stdout(output):
                exit_code = vault_backup.main(
                    ["--vault", str(vault), "--dest", str(dest), "--keep", "1"],
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("yok sayılan 1 dosya", output.getvalue())

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
