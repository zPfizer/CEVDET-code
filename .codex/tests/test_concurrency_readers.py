from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import vault_retrieval
import doctor
import hook
import intake_contract
import memory_ledger


class PromptMemorySnapshotTests(unittest.TestCase):
    def test_profile_and_warning_share_the_checked_preference_snapshot(self) -> None:
        for before, after in ((frozenset(), frozenset()),
                              (frozenset({'hidden'}), frozenset({'hidden'})),
                              (frozenset(), frozenset({'hidden'})),
                              (frozenset({'hidden'}), frozenset())):
            with self.subTest(before=before, after=after), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with (
                    mock.patch.object(memory_ledger, 'load_suppressed_hashes',
                                      side_effect=[before, after]) as preferences,
                    mock.patch.object(hook, '_profile_card', return_value='synthetic-profile'),
                ):
                    result = hook.handle_user_prompt(
                        {'session_id': 'snapshot-test', 'prompt': ''},
                        root / '.state', vault_root=root,
                    )
                self.assertEqual(preferences.call_count, 2)
                if before != after:
                    self.assertIn('[Hafıza Tercihi Sorunu]', result)
                    self.assertNotIn('synthetic-profile', result)
                else:
                    self.assertIn('synthetic-profile', result)
                    self.assertEqual(hook.MEMORY_READ_RULE in result, bool(before))


class RetrievalRaceTests(unittest.TestCase):
    def _note(self, root: Path, name: str, body: str) -> Path:
        path = root / "🧠 500-Knowledge" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_source_change_retries_and_keeps_cache_metadata_with_final_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changing = self._note(root, "degisen.md", "# Eski Kanıt\neski içerik\n")
            stable = self._note(root, "sabit.md", "# Sabit Kanıt\nsabit içerik\n")
            changed = False

            def reader(vault_root: Path, path: Path):
                nonlocal changed
                entry = vault_retrieval.entry_from_file(vault_root, path)
                if path == changing and not changed:
                    changed = True
                    path.write_text("# Yeni Kanıt\nyeni içerik\n", encoding="utf-8")
                return entry

            entries = vault_retrieval.build_vault_map(root, read_entry=reader)
            cache = json.loads(
                (root / vault_retrieval.CACHE_RELATIVE_PATH).read_text(encoding="utf-8")
            )
            final_stat = changing.stat()
            expected_content_hash = vault_retrieval._source_snapshot(root, changing)[2]
            expected_source_hash = vault_retrieval._source_sha256(changing)
            self.assertEqual(stable.read_text(encoding="utf-8"), "# Sabit Kanıt\nsabit içerik\n")

        self.assertEqual({entry.title for entry in entries}, {"Yeni Kanıt", "Sabit Kanıt"})
        cached = cache["files"]["🧠 500-Knowledge/degisen.md"]
        self.assertNotIn("size", cached)
        self.assertNotIn("mtime_ns", cached)
        self.assertNotIn("ctime_ns", cached)
        self.assertEqual(cached["dev"], final_stat.st_dev)
        self.assertEqual(cached["ino"], final_stat.st_ino)
        self.assertEqual(cached["content_sha256"], expected_content_hash)
        self.assertEqual(cached["source_sha256"], expected_source_hash)

    def test_disappearing_selected_source_keeps_other_fresh_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disappearing = self._note(root, "a.md", "# Ortak Kanıt A\nortak yarış kanıtı\n")
            surviving = self._note(root, "b.md", "# Ortak Kanıt B\nortak yarış kanıtı\n")
            candidates = vault_retrieval.build_vault_map(root, write_cache=False)
            original = vault_retrieval.entry_from_file
            removed = False

            def reader(vault_root: Path, path: Path, *, memory=None):
                nonlocal removed
                if path == disappearing and not removed:
                    removed = True
                    path.unlink()
                return original(vault_root, path, memory=memory)

            with mock.patch.object(vault_retrieval, "entry_from_file", side_effect=reader):
                hits = vault_retrieval._fresh_hits(
                    root,
                    candidates,
                    "ortak yarış kanıtı",
                    3,
                    vault_retrieval.MemoryRead(root, frozenset()),
                )

        self.assertTrue(removed)
        self.assertEqual([hit.entry.path for hit in hits], [surviving.relative_to(root).as_posix()])

    def test_post_read_disappearance_keeps_other_fresh_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disappearing = self._note(root, "a.md", "# Ortak Kanıt A\npoststat yarış kanıtı\n")
            surviving = self._note(root, "b.md", "# Ortak Kanıt B\npoststat yarış kanıtı\n")
            candidates = vault_retrieval.build_vault_map(root, write_cache=False)
            original = vault_retrieval.entry_from_file
            removed = False

            def reader(vault_root: Path, path: Path, *, memory=None):
                nonlocal removed
                entry = original(vault_root, path, memory=memory)
                if path == disappearing and not removed:
                    removed = True
                    path.unlink()
                return entry

            with mock.patch.object(vault_retrieval, "entry_from_file", side_effect=reader):
                hits = vault_retrieval._fresh_hits(
                    root,
                    candidates,
                    "poststat yarış kanıtı",
                    3,
                    vault_retrieval.MemoryRead(root, frozenset()),
                )

        self.assertTrue(removed)
        self.assertEqual([hit.entry.path for hit in hits], [surviving.relative_to(root).as_posix()])

    def test_source_missing_at_initial_stat_keeps_other_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = self._note(root, "missing.md", "# Kaybolan\nilk stat yarış kanıtı\n")
            surviving = self._note(root, "sabit.md", "# Sabit\nilk stat sabit kanıt\n")
            missing.unlink()
            with mock.patch.object(
                vault_retrieval,
                "markdown_paths",
                return_value=iter((missing, surviving)),
            ):
                entries = vault_retrieval.build_vault_map(root, write_cache=False)

        self.assertEqual([entry.path for entry in entries], [surviving.relative_to(root).as_posix()])

    def test_missing_only_source_is_reported_as_incomplete_instead_of_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disappearing = self._note(root, "a.md", "# Tek Kanıt\ntek kaynak yarış kanıtı\n")
            candidates = vault_retrieval.build_vault_map(root, write_cache=False)
            original = vault_retrieval.entry_from_file

            def reader(vault_root: Path, path: Path, *, memory=None):
                entry = original(vault_root, path, memory=memory)
                if path == disappearing:
                    path.unlink()
                return entry

            with mock.patch.object(vault_retrieval, "entry_from_file", side_effect=reader):
                with self.assertRaisesRegex(OSError, "vault-retrieval-incomplete"):
                    vault_retrieval._fresh_hits(
                        root,
                        candidates,
                        "tek kaynak yarış kanıtı",
                        3,
                        vault_retrieval.MemoryRead(root, frozenset()),
                    )

    def test_repeated_source_change_keeps_stable_entries_and_does_not_rewrite_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changing = self._note(root, "degisen.md", "# Değişen\nkararsız kanıt\n")
            self._note(root, "sabit.md", "# Sabit\nsabit kanıt\n")
            original_cache = root / vault_retrieval.CACHE_RELATIVE_PATH
            attempts = 0

            def reader(vault_root: Path, path: Path):
                nonlocal attempts
                entry = vault_retrieval.entry_from_file(vault_root, path)
                if path == changing:
                    attempts += 1
                    path.unlink()
                    path.write_text(f"# Değişen {attempts}\nkararsız kanıt\n", encoding="utf-8")
                return entry

            entries = vault_retrieval.build_vault_map(root, read_entry=reader)

        self.assertGreaterEqual(attempts, vault_retrieval.SOURCE_READ_ATTEMPTS)
        self.assertEqual([entry.title for entry in entries], ["Sabit"])
        self.assertFalse(original_cache.exists())

    def test_helper_boundary_race_does_not_pair_old_entry_with_new_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changing = self._note(root, "ara.md", "# Ara\nalphaold\n")
            original = vault_retrieval._source_snapshot
            calls = 0

            def snapshot(vault_root: Path, path: Path, *, memory=None):
                nonlocal calls
                calls += 1
                result = original(vault_root, path, memory=memory)
                if path == changing and calls == 2:
                    path.write_text("# Ara\nbetanewx\n", encoding="utf-8")
                return result

            with mock.patch.object(vault_retrieval, "_source_snapshot", side_effect=snapshot):
                entries = vault_retrieval.build_vault_map(
                    root, read_entry=vault_retrieval.entry_from_file
                )

            cache = json.loads(
                (root / vault_retrieval.CACHE_RELATIVE_PATH).read_text(encoding="utf-8")
            )
            expected_source_hash = vault_retrieval._source_sha256(changing)

        self.assertIn("betanewx", entries[0].body_terms)
        self.assertNotIn("alphaold", entries[0].body_terms)
        self.assertEqual(
            cache["files"][changing.relative_to(root).as_posix()]["source_sha256"],
            expected_source_hash,
        )

    def test_permission_error_is_not_treated_as_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = self._note(root, "izin.md", "# İzin\nkanıt\n")

            def denied(_vault_root: Path, path: Path):
                if path == note:
                    raise PermissionError("permission denied")
                return vault_retrieval.entry_from_file(_vault_root, path)

            with self.assertRaises(PermissionError):
                vault_retrieval.build_vault_map(root, write_cache=False, read_entry=denied)

    def test_active_memory_projection_keeps_other_entries_when_one_source_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disappearing = self._note(root, "a.md", "# Projeksiyon A\naktif yarış kanıtı\n")
            surviving = self._note(root, "b.md", "# Projeksiyon B\naktif yarış kanıtı\n")
            candidates = vault_retrieval.build_vault_map(root, write_cache=False)
            original = vault_retrieval.entry_from_file
            removed = False
            memory = vault_retrieval.MemoryRead(root, frozenset({"0" * 64}))

            def reader(vault_root: Path, path: Path, *, memory=None):
                nonlocal removed
                entry = original(vault_root, path, memory=memory)
                if path == disappearing and not removed:
                    removed = True
                    path.unlink()
                return entry

            with mock.patch.object(vault_retrieval, "entry_from_file", side_effect=reader):
                projected = vault_retrieval._apply_memory_suppressions(
                    root,
                    candidates,
                    memory,
                )

        self.assertTrue(removed)
        self.assertEqual(
            [entry.path for entry in projected],
            [surviving.relative_to(root).as_posix()],
        )
        self.assertEqual(
            projected.unstable_paths,
            frozenset({disappearing.relative_to(root).as_posix()}),
        )

    def test_hook_does_not_offer_raw_search_after_incomplete_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".codex/scripts/.state"
            state.mkdir(parents=True)
            with (
                mock.patch.object(
                    memory_ledger,
                    "load_suppressed_hashes",
                    return_value=frozenset({"0" * 64}),
                ),
                mock.patch.object(
                    hook,
                    "retrieve_vault_context_detailed",
                    side_effect=OSError("vault-retrieval-incomplete"),
                ),
            ):
                context = hook.handle_user_prompt(
                    {"session_id": "race", "prompt": "Atlas kararı"},
                    state,
                    vault_root=root,
                )

        self.assertIn("Vault Arama Sorunu", context)
        self.assertIn("kaynak tutarlılığı doğrulanmadan", context)
        self.assertIn("Ham bilgi dosyalarına veya eski önbelleğe geçme", context)
        self.assertNotIn("Mevcut dosya aramasıyla ilgili kaynaklara ulaşmayı dene", context)


class DiagnosticBoundaryTests(unittest.TestCase):
    def test_git_branch_timeout_is_a_controlled_failure(self) -> None:
        with mock.patch.object(
            doctor.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            check = doctor._git_branch_check(doctor.Context(Path("vault")))

        self.assertEqual(check.status, "FAIL")
        self.assertEqual(check.evidence, "repo yok")

    def test_derived_state_gitignore_timeout_is_a_controlled_failure(self) -> None:
        with mock.patch.object(
            doctor.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            check = doctor._derived_state_gitignore_check(doctor.Context(Path("vault")))

        self.assertEqual(check.status, "FAIL")
        self.assertEqual(check.evidence, "git-ls-files-failed")

    def test_derived_state_gitignore_probe_timeout_is_a_controlled_failure(self) -> None:
        tracked = subprocess.CompletedProcess(
            ["git"],
            0,
            stdout=".codex/scripts/.state/.gitkeep\n",
        )
        with mock.patch.object(
            doctor.subprocess,
            "run",
            side_effect=[tracked, subprocess.TimeoutExpired(["git"], 5)],
        ):
            check = doctor._derived_state_gitignore_check(doctor.Context(Path("vault")))

        self.assertEqual(check.status, "FAIL")
        self.assertEqual(check.evidence, "git-check-ignore-failed")

    def test_profile_preference_race_is_a_controlled_failure(self) -> None:
        with mock.patch.object(
            doctor,
            "memory_read",
            side_effect=memory_ledger.MemoryPreferenceError("memory-preferences-changed"),
        ):
            check = doctor._profile_maintenance_check(doctor.Context(Path("vault")))

        self.assertEqual(check.status, "FAIL")
        self.assertEqual(check.evidence, "memory-preferences-changed")

    def test_profile_programming_error_is_not_reported_as_success(self) -> None:
        with mock.patch.object(doctor, "memory_read", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                doctor._profile_maintenance_check(doctor.Context(Path("vault")))

    def test_run_checks_preserves_controlled_memory_failure_boundary(self) -> None:
        def failed(_context):
            raise memory_ledger.MemoryPreferenceError("memory-preferences-changed")

        with mock.patch.object(doctor, "CHECKS", (("synthetic", failed),)):
            checks = doctor.run_checks(Path("vault"))

        self.assertEqual(checks, [
            doctor.Check("synthetic", "FAIL", "memory-preferences-changed"),
        ])

    def test_intake_git_timeout_has_a_bounded_error_contract(self) -> None:
        with mock.patch.object(
            intake_contract.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            with self.assertRaisesRegex(ValueError, "intake-git-status-failed"):
                intake_contract._git_new_paths(Path("vault"))

    def test_intake_git_decode_error_is_normalized_to_the_canonical_failure(self) -> None:
        result = subprocess.CompletedProcess(["git"], 0, stdout=b"\xff")
        with mock.patch.object(intake_contract.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "intake-git-status-failed"):
                intake_contract._git_new_paths(Path("vault"))

    def test_intake_cli_reports_git_failure_as_json_and_exit_one(self) -> None:
        with mock.patch.object(
            intake_contract,
            "_git_new_paths",
            side_effect=ValueError("intake-git-status-failed"),
        ), mock.patch("builtins.print") as print_call:
            exit_code = intake_contract.main([], vault_root=Path("vault"))

        report = json.loads(print_call.call_args.args[0])
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["violations"], [
            {"path": "<git>", "issues": ["intake-git-status-failed"]},
        ])


if __name__ == "__main__":
    unittest.main()
