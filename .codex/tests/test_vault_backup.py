from pathlib import Path
import subprocess
import tempfile
import unittest

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

    def test_prune_keeps_newest_and_ignores_foreign_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary)
            names = [
                "vault-20260901-120000.bundle",
                "vault-20260902-120000.bundle",
                "vault-20260903-120000.bundle",
                "vault-20260903-120000-1.bundle",
            ]
            for name in names:
                (dest / name).write_bytes(b"x")
            foreign = dest / "vault-notlar.bundle"
            foreign.write_bytes(b"x")

            removed = vault_backup.prune_bundles(dest, keep=2)

            self.assertEqual(
                sorted(path.name for path in removed),
                ["vault-20260901-120000.bundle", "vault-20260902-120000.bundle"],
            )
            self.assertTrue((dest / "vault-20260903-120000.bundle").exists())
            self.assertTrue((dest / "vault-20260903-120000-1.bundle").exists())
            self.assertTrue(foreign.exists())

    def test_working_tree_summary_counts_uncommitted_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            _init_repo(vault)
            (vault / "not.md").write_text("değişti", encoding="utf-8")
            (vault / "yeni.md").write_text("izlenmiyor", encoding="utf-8")

            tracked, untracked = vault_backup.working_tree_summary(vault)

        self.assertEqual((tracked, untracked), (1, 1))


if __name__ == "__main__":
    unittest.main()
