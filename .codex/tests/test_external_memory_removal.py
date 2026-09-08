from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from _fixtures import CODEX_DIR

import memory_ledger  # noqa: E402
import worker_supervisor  # noqa: E402


class ExternalMemoryRemovalTests(unittest.TestCase):
    def test_legacy_forensic_and_external_memory_files_are_absent(self) -> None:
        removed = (
            CODEX_DIR / "forensics.toml",
            CODEX_DIR / "global-hooks.template.json",
            CODEX_DIR / "memory.toml",
            *(CODEX_DIR / "scripts").glob("forensic_*.py"),
            *(CODEX_DIR / "scripts").glob("*forensic*.ps1"),
            *(
                CODEX_DIR / "scripts" / name
                for name in (
                    "checkpoint_pipeline.py",
                    "mem0_client.py",
                    "memory_context.py",
                    "read_codex_thread.py",
                    "render_global_hooks.py",
                    "secret_store.py",
                )
            ),
        )

        self.assertEqual(tuple(path for path in removed if path.exists()), ())

    def test_active_hook_and_worker_expose_only_vault_local_memory(self) -> None:
        hook_source = (CODEX_DIR / "hooks" / "hook.py").read_text(encoding="utf-8")

        for removed in ("global-", "forensic", "mem0"):
            self.assertNotIn(removed, hook_source.casefold())
        self.assertEqual(worker_supervisor.JOB_KINDS, {"flush", "maintenance"})

    def test_user_docs_have_no_active_forensic_or_external_memory_setup(self) -> None:
        active_docs = "\n".join(
            (CODEX_DIR.parent / name).read_text(encoding="utf-8")
            for name in ("AGENTS.md", "README.md")
        ).casefold()

        for removed in ("forensic", "worm", "mem0", "global hook", "global kanca"):
            self.assertNotIn(removed, active_docs)

    def test_local_forget_tombstone_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            target = "Levent Ankara'da yaşıyor"
            path = memory_ledger.suppress_derived_memory(private_root, target, now=1.0)

            stored = path.read_text(encoding="utf-8")
            hashes = memory_ledger.load_suppressed_hashes(private_root)

        self.assertNotIn(target, stored)
        self.assertIn(memory_ledger.memory_text_hash(target), hashes)

    def test_private_state_remains_ignored(self) -> None:
        ignore = (CODEX_DIR.parent / ".gitignore").read_text(encoding="utf-8")
        for protected in (
            "**/private-memory/",
            "**/private-forensics/",
            "**/private-secrets/",
        ):
            self.assertIn(protected, ignore)


if __name__ == "__main__":
    unittest.main(verbosity=2)
