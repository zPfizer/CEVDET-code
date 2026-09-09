"""doctor.py kontrollerinin ve kontrol kayıt defterinin testleri.

T13: test_codex_brain god-file'ından saf taşıma — davranış değişikliği yok.
doctor.py beynin bir modülü değil, sağlık raporunun sahibi; kendi rafında durur.
"""

from __future__ import annotations

import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # sys.path seam

import doctor  # noqa: E402
import codex_runner  # noqa: E402
import compile as memory_compile  # noqa: E402
import worker_supervisor as workers  # noqa: E402
import memory_ledger  # noqa: E402


class DoctorTests(unittest.TestCase):
    def test_git_attributes_are_expected_but_unknown_root_files_still_warn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.gitattributes').write_text('* text=auto\n', encoding='utf-8')
            self.assertEqual(doctor._root_hygiene_check(doctor.Context(root)).status, 'OK')
            (root / 'unexpected.tmp').write_text('test', encoding='utf-8')
            check = doctor._root_hygiene_check(doctor.Context(root))
            self.assertEqual(check.status, 'WARN')
            self.assertIn('unexpected.tmp', check.evidence)

    def test_historical_hook_errors_are_separate_and_current_errors_still_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            now = doctor.HOOK_RUNTIME_MAX_AGE_SECONDS * 2
            (state / 'hook-health-old.json').write_text(json.dumps(
                {'generation': 1, 'status': 'error', 'error': 'ValueError', 'ts': 0}), encoding='utf-8')
            for current_error in (False, True):
                with self.subTest(current_error=current_error):
                    (state / 'hook-health-current.json').write_text(json.dumps(
                        {'generation': 1, 'status': 'error' if current_error else 'ok',
                         'error': 'RuntimeError', 'ts': now}), encoding='utf-8')
                    checks = {c.name: c for c in doctor.run_checks(
                        state, state_dir=state, now=now, only='Hook sağlığı')}
                    self.assertEqual(checks['Hook sağlığı'].status, 'FAIL' if current_error else 'OK')
                    self.assertEqual(checks['Geçmiş oturum hataları'].status, 'WARN')
                    self.assertIn('historical session failure=1', checks['Geçmiş oturum hataları'].evidence)

    def test_only_verified_successors_are_historical_worker_successes(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            dead = state / 'worker-jobs/dead-letter'
            dead.mkdir(parents=True)
            dead_pending = workers.enqueue_job(
                state, 'flush', {}, start_supervisor=False, now=0,
            )
            dead_running, dead_job = workers._claim_next_job(state, now=0)
            self.assertIsNotNone(dead_running)
            self.assertIsNotNone(dead_job)
            workers._finish_job(
                state, dead_running, dead_job,
                status='dead-letter',
                terminal_reason='recovered-by-successor',
                now=100,
            )
            replacement = workers.enqueue_job(
                state, 'flush', {}, start_supervisor=False, now=0,
            )
            replacement_id = replacement.stem.removeprefix('job-')
            dead_path = dead / dead_pending.name
            dead_record = json.loads(dead_path.read_text(encoding='utf-8'))
            dead_record['recovery_job_id'] = replacement_id
            dead_path.write_text(json.dumps(dead_record), encoding='utf-8')
            self.assertEqual(workers.inspect_worker_queue(state)['invalid'], 0)
            check = doctor._worker_queue_check(doctor.Context(state_dir=state, now=100))
            self.assertEqual(check.status, 'FAIL')
            replacement_running, replacement_job = workers._claim_next_job(state, now=100)
            self.assertIsNotNone(replacement_running)
            self.assertIsNotNone(replacement_job)
            workers._finish_job(
                state, replacement_running, replacement_job,
                status='succeeded', now=101,
            )
            checks = {c.name: c for c in doctor.run_checks(state, state_dir=state, now=100, only='Worker kuyruğu')}
            self.assertEqual(checks['Worker kuyruğu'].status, 'OK')
            self.assertEqual(checks['Geçmiş iş sonuçları'].status, 'OK')
            self.assertIn('recovered-by-successor=1', checks['Geçmiş iş sonuçları'].evidence)

    def test_invalid_configured_cli_keeps_stable_reason_without_raw_path(self) -> None:
        with mock.patch.object(
            codex_runner,
            "find_codex",
            side_effect=FileNotFoundError("codex-cli-path-invalid: C:/private/bin"),
        ):
            text, reason = codex_runner.run_exec(
                "probe",
                sandbox="read-only",
                timeout=1,
            )

        self.assertIsNone(text)
        self.assertEqual(reason, "codex-cli-path-invalid")

    def test_doctor_warns_when_archive_note_is_not_in_archive_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            archive = vault / "📦 900-Archive"
            archive.mkdir()
            indexed = archive / "Indexed.md"
            missing = archive / "Missing.md"
            indexed.write_text("# Indexed\n", encoding="utf-8")
            missing.write_text("# Missing\n", encoding="utf-8")
            index = archive / "Archive.md"
            index.write_text(
                "# Arşiv\n\n- [[📦 900-Archive/Indexed|Indexed]]\n",
                encoding="utf-8",
            )

            warning = doctor._archive_index_check(doctor.Context(vault))
            index.write_text(
                index.read_text(encoding="utf-8")
                + "- [[📦 900-Archive/Missing|Missing]]\n",
                encoding="utf-8",
            )
            clean = doctor._archive_index_check(doctor.Context(vault))

        self.assertEqual(warning.status, "WARN")
        self.assertIn("Missing.md", warning.evidence)
        self.assertEqual(clean.status, "OK")

    def test_doctor_reports_state_retention_metrics_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            cache = state / "vault-retrieval-cache.json"
            cache.write_text("{}", encoding="utf-8")
            (state / "session-a.json").write_text("{}", encoding="utf-8")
            (state / "runtime-a.json").write_text("{}", encoding="utf-8")
            (state / "sample.lock").write_text("", encoding="utf-8")
            before = {path.name for path in state.iterdir()}

            check = doctor._state_retention_check(doctor.Context(state_dir=state, now=time.time()))
            after = {path.name for path in state.iterdir()}

        self.assertIn(check.status, {"OK", "WARN"})
        self.assertIn("registry=1", check.evidence)
        self.assertIn("runtime=1", check.evidence)
        self.assertIn("locks=1", check.evidence)
        self.assertEqual(before, after)

    def test_doctor_fails_stale_running_worker_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(
                state, 'flush', {}, start_supervisor=False, now=0,
            )
            running_path, running_job = workers._claim_next_job(state, now=0)
            self.assertIsNotNone(running_path)
            self.assertIsNotNone(running_job)
            running_job['lease_until'] = 1
            running_job['owner_pid'] = 999_999_999
            running_path.write_text(json.dumps(running_job), encoding='utf-8')
            self.assertEqual(workers.inspect_worker_queue(state)['invalid'], 0)

            check = doctor._worker_queue_check(doctor.Context(state_dir=state, now=100))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("stale-running=1", check.evidence)

    def test_doctor_fails_when_derived_state_gitignore_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex" / "scripts" / ".state"
            state.mkdir(parents=True)
            (state / ".gitkeep").write_text("", encoding="utf-8")
            gitignore = vault / ".gitignore"
            gitignore.write_text(
                ".codex/scripts/.state/*\n"
                "!.codex/scripts/.state/.gitkeep\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=vault,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "add", ".gitignore", ".codex/scripts/.state/.gitkeep"],
                cwd=vault,
                check=True,
            )

            healthy = doctor._derived_state_gitignore_check(doctor.Context(vault))
            gitignore.write_text(
                "!.codex/scripts/.state/.gitkeep\n",
                encoding="utf-8",
            )
            drifted = doctor._derived_state_gitignore_check(doctor.Context(vault))

        self.assertEqual(healthy.status, "OK")
        self.assertEqual(drifted.status, "FAIL")
        self.assertIn("ignore-drift", drifted.evidence)

    def test_doctor_warns_when_ready_pending_job_has_idle_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            pending = state / "worker-jobs" / "pending"
            pending.mkdir(parents=True)
            (pending / "job-ready.json").write_text(
                json.dumps(
                    {
                        "status": "pending",
                        "next_attempt_ts": 90,
                    }
                ),
                encoding="utf-8",
            )
            (state / "worker-supervisor.json").write_text(
                json.dumps(
                    {
                        "status": "idle",
                        "owner_pid": 999_999_999,
                        "lease_until": 0,
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._worker_delayed_job_check(doctor.Context(state_dir=state, now=100))

        self.assertEqual(check.status, "WARN")
        self.assertIn("ready-pending=1", check.evidence)
        self.assertIn("supervisor=idle", check.evidence)

    def test_doctor_warns_for_terminal_unrecoverable_input_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers.enqueue_job(
                state, 'flush', {}, start_supervisor=False, now=0,
            )
            running, job = workers._claim_next_job(state, now=0)
            self.assertIsNotNone(running)
            self.assertIsNotNone(job)
            workers._finish_job(
                state, running, job,
                status='dead-letter',
                terminal_reason='unrecoverable-input',
                now=100,
            )
            self.assertEqual(workers.inspect_worker_queue(state)['invalid'], 0)

            checks = {c.name: c for c in doctor.run_checks(state, state_dir=state, now=100, only='Worker kuyruğu')}

        self.assertEqual(checks['Worker kuyruğu'].status, 'OK')
        self.assertEqual(checks['Geçmiş iş sonuçları'].status, 'WARN')
        self.assertIn('unrecoverable-input=1', checks['Geçmiş iş sonuçları'].evidence)

    def test_doctor_enforces_active_execution_limit_and_open_thread_uniqueness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / "🔮 850-Companion"
            companion.mkdir(parents=True)
            threads = companion / "Threads.md"
            threads.write_text(
                "# Threads\n\n## Active Execution\n"
                + "\n".join(
                    f"### Thread: İş {index}\n**Status:** Active"
                    for index in range(1, 5)
                )
                + "\n\n## Always-on Systems\n"
                "### Thread: İş 1\n**Status:** Always\n\n"
                "## Waiting / Parked\n\n## Closed Threads\n",
                encoding="utf-8",
            )

            check = doctor._thread_workload_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("4 active", check.evidence)
        self.assertIn("duplicate", check.evidence)

    def test_doctor_reports_bounded_thread_workload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / "🔮 850-Companion"
            companion.mkdir(parents=True)
            (companion / "Threads.md").write_text(
                "# Threads\n\n"
                "## Active Execution\n### Thread: Aktif\n**Status:** Active\n\n"
                "## Always-on Systems\n### Thread: Sistem\n**Status:** Always\n\n"
                "## Waiting / Parked\n### Thread: Bekleyen\n**Status:** Parked\n\n"
                "## Closed Threads\n",
                encoding="utf-8",
            )

            check = doctor._thread_workload_check(doctor.Context(vault))

        self.assertEqual(check.status, "OK")
        self.assertIn("1 active / 1 always-on / 1 waiting", check.evidence)

    def test_doctor_fails_when_thread_taxonomy_is_missing_or_misordered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / "🔮 850-Companion"
            companion.mkdir(parents=True)
            threads = companion / "Threads.md"
            threads.write_text(
                "# Threads\n\n## Active Threads\n\n## Closed Threads\n",
                encoding="utf-8",
            )
            missing = doctor._thread_workload_check(doctor.Context(vault))
            threads.write_text(
                "# Threads\n\n"
                "## Waiting / Parked\n"
                "## Active Execution\n"
                "## Always-on Systems\n"
                "## Closed Threads\n",
                encoding="utf-8",
            )
            misordered = doctor._thread_workload_check(doctor.Context(vault))

        self.assertEqual(missing.status, "FAIL")
        self.assertIn("taxonomy", missing.evidence)
        self.assertEqual(misordered.status, "FAIL")
        self.assertIn("sırası", misordered.evidence)

    def test_doctor_fails_when_open_thread_has_no_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / "🔮 850-Companion"
            companion.mkdir(parents=True)
            (companion / "Threads.md").write_text(
                "# Threads\n\n"
                "## Active Execution\n### Thread: Eksik Durum\n\n"
                "## Always-on Systems\n\n"
                "## Waiting / Parked\n\n"
                "## Closed Threads\n",
                encoding="utf-8",
            )

            check = doctor._thread_workload_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("status eksik", check.evidence)

    def test_doctor_fails_when_thread_is_both_open_and_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / "🔮 850-Companion"
            companion.mkdir(parents=True)
            (companion / "Threads.md").write_text(
                "# Threads\n\n"
                "## Active Execution\n"
                "### Thread: Aynı İş\n**Status:** Active\n\n"
                "## Always-on Systems\n\n"
                "## Waiting / Parked\n\n"
                "## Closed Threads\n"
                "### Thread: Aynı İş\n**Status:** Closed\n",
                encoding="utf-8",
            )

            check = doctor._thread_workload_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("duplicate: Aynı İş", check.evidence)

    def test_state_privacy_treats_retrieval_token_maps_as_derived_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "vault-retrieval-cache.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "files": {
                            "note.md": {
                                "entry": {
                                    "body_terms": {"prompt": 2, "message": 1},
                                    "safe_lines": ["Prompt güvenliği hakkında not."],
                                }
                            }
                        },
                        "document_frequency": {"content": 1, "transcript": 1},
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._state_privacy_check(doctor.Context(state_dir=state))

        self.assertEqual(check.status, "OK")

    def test_doctor_reports_local_machine_checkpoint_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = vault / "daily"
            knowledge = vault / "knowledge"
            daily.mkdir()
            knowledge.mkdir()
            (daily / "2026-08-28.md").write_text("# Günlük\n", encoding="utf-8")
            (knowledge / "index.md").write_text("# İndeks\n", encoding="utf-8")
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=vault,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "ALF4 Test"],
                cwd=vault,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "alf4-test@example.invalid"],
                cwd=vault,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=vault, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=vault,
                check=True,
                capture_output=True,
            )

            clean = doctor._local_checkpoint_check(doctor.Context(vault))
            (daily / "2026-08-28.md").write_text(
                "# Günlük\nYeni.\n",
                encoding="utf-8",
            )
            dirty = doctor._local_checkpoint_check(doctor.Context(vault))

        self.assertEqual(clean.status, "OK")
        self.assertEqual(dirty.status, "WARN")
        self.assertIn("1 machine-managed fark", dirty.evidence)

    def test_git_hygiene_accepts_clean_non_main_worktree_and_ignored_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / ".gitignore").write_text("tmp/\n", encoding="utf-8")
            ignored = vault / "tmp" / "test output.txt"
            ignored.parent.mkdir()
            ignored.write_text("ignored", encoding="utf-8")
            subprocess.run(
                ["git", "init", "-b", "feature"],
                cwd=vault,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "ALF4 Test"],
                cwd=vault,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "alf4-test@example.invalid"],
                cwd=vault,
                check=True,
            )
            subprocess.run(["git", "add", ".gitignore"], cwd=vault, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=vault,
                check=True,
                capture_output=True,
            )

            check = doctor._git_hygiene_check(doctor.Context(vault))

        self.assertEqual(check.status, "OK")

    def test_git_hygiene_warns_for_untracked_scratch_with_bounded_example(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=vault,
                check=True,
            )
            scratch = vault / ".scratch" / "work"
            scratch.mkdir(parents=True)
            path = scratch / "unicode boş.md"
            path.write_text("SECRET-CONTENT", encoding="utf-8")

            check = doctor._git_hygiene_check(doctor.Context(vault))

        self.assertEqual(check.status, "WARN")
        self.assertIn(".scratch=1", check.evidence)
        self.assertIn("unicode boş.md", check.evidence)
        self.assertNotIn("SECRET-CONTENT", check.evidence)

    def test_git_hygiene_decodes_long_utf8_path_before_truncating(self) -> None:
        raw_path = ("a" * 255 + "é").encode("utf-8")
        with mock.patch.object(
            doctor.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["git"], 0, b"?? " + raw_path + b"\0", b""
            ),
        ):
            check = doctor._git_hygiene_check(doctor.Context(Path(".")))

        self.assertEqual(check.status, "WARN")
        self.assertIn("other=1", check.evidence)
        self.assertIn("…", check.evidence)

    def test_git_hygiene_counts_tracked_modified_deleted_and_renamed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            files = {
                "tracked.txt": "tracked-content",
                "delete me.txt": "delete-content",
                "old Ü spaced name.txt": "rename-content",
            }
            for name, content in files.items():
                (vault / name).write_text(content, encoding="utf-8")
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=vault,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "ALF4 Test"],
                cwd=vault,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "alf4-test@example.invalid"],
                cwd=vault,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=vault, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=vault,
                check=True,
                capture_output=True,
            )
            (vault / "tracked.txt").write_text("changed-content", encoding="utf-8")
            subprocess.run(["git", "rm", "-q", "delete me.txt"], cwd=vault, check=True)
            subprocess.run(
                ["git", "mv", "old Ü spaced name.txt", "new Ü spaced name.txt"],
                cwd=vault,
                check=True,
            )

            check = doctor._git_hygiene_check(doctor.Context(vault))

        self.assertEqual(check.status, "WARN")
        self.assertIn(".scratch=0", check.evidence)
        self.assertIn("other=3", check.evidence)
        self.assertIn("new Ü spaced name.txt", check.evidence)
        self.assertNotIn("changed-content", check.evidence)

    def test_git_hygiene_fails_for_missing_git_non_repo_failed_timeout_and_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            not_repo = doctor._git_hygiene_check(doctor.Context(root))
            with mock.patch.object(
                doctor.subprocess,
                "run",
                side_effect=FileNotFoundError("git"),
            ):
                missing = doctor._git_hygiene_check(doctor.Context(root))
            with mock.patch.object(
                doctor.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git"], 1, b"", b"secret failure output"
                ),
            ):
                failed = doctor._git_hygiene_check(doctor.Context(root))
            with mock.patch.object(
                doctor.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["git"], 5),
            ):
                timed_out = doctor._git_hygiene_check(doctor.Context(root))
            with mock.patch.object(
                doctor.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["git"], 0, b"\0", b""),
            ):
                malformed = doctor._git_hygiene_check(doctor.Context(root))
            with mock.patch.object(
                doctor.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git"], 0, b"  path\0", b""
                ),
            ):
                blank_status = doctor._git_hygiene_check(doctor.Context(root))
            with mock.patch.object(
                doctor.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git"], 0, b"?M path\0", b""
                ),
            ):
                mixed_untracked_status = doctor._git_hygiene_check(doctor.Context(root))

        for check in (
            not_repo,
            missing,
            failed,
            timed_out,
            malformed,
            blank_status,
            mixed_untracked_status,
        ):
            self.assertEqual(check.status, "FAIL")
        self.assertNotIn("secret failure output", failed.evidence)
        self.assertIn("bozuk", malformed.evidence)

    def test_state_privacy_audits_hook_input_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "hookin-active.json").write_text(
                json.dumps(
                    {
                        "session_id": "active-session",
                        "prompt": "active prompt",
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._state_privacy_check(doctor.Context(state_dir=state))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("hookin-active.json", check.evidence)

    def test_doctor_fails_when_machine_knowledge_schema_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            concepts = vault / "knowledge" / "concepts"
            concepts.mkdir(parents=True)
            (vault / "knowledge" / "connections").mkdir()
            (vault / "knowledge" / "index.md").write_text(
                "# Bilgi Tabanı: İndeks\n",
                encoding="utf-8",
            )
            (vault / "knowledge" / "log.md").write_text(
                "# Bilgi Derleme Günlüğü\n",
                encoding="utf-8",
            )
            (concepts / "eksik.md").write_text(
                "---\ntitle: Eksik\n---\n# Eksik\n",
                encoding="utf-8",
            )

            check = doctor._knowledge_schema_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("eksik.md", check.evidence)

    def test_doctor_fails_when_flush_state_contains_raw_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "flush-example.json").write_text(
                json.dumps(
                    {
                        "session_id": "private-session-identity",
                        "ts": 1_777_777_777,
                        "status": "ok",
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._state_privacy_check(doctor.Context(state_dir=state))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("flush-example.json", check.evidence)

    def test_doctor_fails_when_graph_has_an_isolated_markdown_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "Bağlı.md").write_text("[[Hedef]]", encoding="utf-8")
            (vault / "Hedef.md").write_text("# Hedef", encoding="utf-8")
            isolated = vault / "Yalnız.md"
            isolated.write_text("# Yalnız", encoding="utf-8")

            check = doctor._vault_graph_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("Yalnız.md", check.evidence)

    def test_doctor_warns_when_compiler_queue_has_pending_daily_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = vault / "daily"
            state = vault / ".codex" / "scripts" / ".state"
            daily.mkdir(parents=True)
            state.mkdir(parents=True)
            (daily / "2026-08-27.md").write_text("# Yeni günlük", encoding="utf-8")
            (state / "compile-state.json").write_text(
                json.dumps({"ingested": {}, "runs": [], "cursor": ""}),
                encoding="utf-8",
            )

            checks = doctor.run_checks(
                vault,
                project_root=vault,
                state_dir=state,
            )

        queue = next(check for check in checks if check.name == "Derleyici kuyruğu")
        self.assertEqual(queue.status, "WARN")
        self.assertIn("2026-08-27.md", queue.evidence)

    def test_doctor_reports_empty_compiler_queue_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = vault / "daily"
            state = vault / ".codex" / "scripts" / ".state"
            daily.mkdir(parents=True)
            state.mkdir(parents=True)
            daily_file = daily / "2026-08-27.md"
            daily_file.write_text("# İşlenmiş günlük", encoding="utf-8")
            (state / "compile-state.json").write_text(
                json.dumps(
                    {
                        "ingested": {
                            daily_file.name: memory_compile._sha256(daily_file),
                        },
                        "runs": [],
                        "cursor": daily_file.name,
                    }
                ),
                encoding="utf-8",
            )

            checks = doctor.run_checks(
                vault,
                project_root=vault,
                state_dir=state,
            )

        queue = next(check for check in checks if check.name == "Derleyici kuyruğu")
        self.assertEqual(queue.status, "OK")

    def test_doctor_warns_for_unexpected_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "README.md").write_text("# Vault", encoding="utf-8")
            (vault / "Yanlış Yerde.md").write_text("", encoding="utf-8")
            (vault / "⚔️ Kök Artığı.md").write_text("", encoding="utf-8")

            checks = doctor.run_checks(vault, project_root=vault)

        root = next(check for check in checks if check.name == "Kök hijyeni")
        self.assertEqual(root.status, "WARN")
        self.assertIn("Yanlış Yerde.md", root.evidence)
        self.assertIn("⚔️ Kök Artığı.md", root.evidence)

    def test_doctor_fails_when_vault_has_a_broken_wikilink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            knowledge = vault / "🧠 500-Knowledge"
            knowledge.mkdir(parents=True)
            (knowledge / "Kaynak.md").write_text(
                """---
title: Kaynak
created: 2026-08-27
updated: 2026-08-27
tags: [doğrulama]
---
# Kaynak
[[Olmayan Not]]
""",
                encoding="utf-8",
            )

            checks = doctor.run_checks(vault, project_root=vault)

        links = next(check for check in checks if check.name == "Vault bağlantıları")
        self.assertEqual(links.status, "FAIL")
        self.assertIn("Olmayan Not", links.evidence)

    def test_doctor_accepts_obsidian_escaped_alias_wikilink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            concepts = vault / "knowledge" / "concepts"
            concepts.mkdir(parents=True)
            (concepts / "ornek.md").write_text("# Örnek", encoding="utf-8")
            (vault / "knowledge" / "index.md").write_text(
                "[[concepts/ornek\\|Örnek]]",
                encoding="utf-8",
            )

            checks = doctor.run_checks(vault, project_root=vault)

        links = next(check for check in checks if check.name == "Vault bağlantıları")
        self.assertEqual(links.status, "OK")

    def test_doctor_rejects_wikilink_that_escapes_the_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            knowledge = vault / "🧠 500-Knowledge"
            knowledge.mkdir(parents=True)
            (root / "outside.md").write_text("# Dışarıda", encoding="utf-8")
            (knowledge / "Kaynak.md").write_text(
                """---
title: Kaynak
created: 2026-08-27
updated: 2026-08-27
tags: [doğrulama]
---
# Kaynak
[[../outside]]
""",
                encoding="utf-8",
            )

            check = doctor._vault_link_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("../outside", check.evidence)

    def test_doctor_accepts_existing_base_file_wikilink_with_view_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            project = vault / "🏰 300-Projects" / "Tansu X Veri Havuzu"
            note = project / "Dashboard.md"
            note.parent.mkdir(parents=True)
            note.write_text(
                "# Dashboard\n"
                "[[Tansu Kaynakları.base#Sinyal ve Strateji]]\n"
                "[[🏰 300-Projects/Tansu X Veri Havuzu/Tansu Kaynakları.base#Veri ve Kanıt]]\n",
                encoding="utf-8",
            )
            (project / "Tansu Kaynakları.base").write_text(
                "views:\n  - name: Sinyal ve Strateji\n",
                encoding="utf-8",
            )

            check = doctor._vault_link_check(doctor.Context(vault))

        self.assertEqual(check.status, "OK")

    def test_doctor_fails_when_base_file_wikilink_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note = vault / "🧠 500-Knowledge" / "Dashboard.md"
            note.parent.mkdir(parents=True)
            (vault / "other").mkdir()
            (vault / "other" / "Views.base").write_text("views: []\n", encoding="utf-8")
            (note.parent / "Missing.md").write_text("# Missing\n", encoding="utf-8")
            note.write_text(
                "# Dashboard\n[[missing/Views.base]]\n[[Missing.base]]\n",
                encoding="utf-8",
            )

            check = doctor._vault_link_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("2 kırık", check.evidence)
        self.assertIn("missing/Views.base", check.evidence)

    def test_doctor_fails_when_human_note_metadata_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            projects = vault / "🏰 300-Projects"
            projects.mkdir(parents=True)
            (projects / "Eksik.md").write_text(
                "---\ntitle: Eksik\n---\n# Eksik\n",
                encoding="utf-8",
            )

            checks = doctor.run_checks(vault, project_root=vault)

        metadata = next(check for check in checks if check.name == "Metadata şeması")
        self.assertEqual(metadata.status, "FAIL")
        self.assertIn("created", metadata.evidence)

    def test_doctor_fails_when_modified_frontmatter_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "note.md").write_text(
                "---\ncreated: 2026-08-27\nmodified: 2026-08-27\n---\n# Note\n",
                encoding="utf-8",
            )

            check = doctor._metadata_schema_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("modified", check.evidence)

    def test_doctor_ignores_template_placeholder_titles_but_rejects_normal_duplicates(self) -> None:
        template = (
            "---\n"
            'title: "{{title}}"\n'
            "created: 2026-08-27\n"
            "updated: 2026-08-27\n"
            "tags: [template]\n"
            "---\n"
            "# {{title}}\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            templates = vault / "📋 Templates"
            templates.mkdir(parents=True)
            (templates / "One.md").write_text(template, encoding="utf-8")
            (templates / "Two.md").write_text(template, encoding="utf-8")

            self.assertEqual(doctor._metadata_schema_check(doctor.Context(vault)).status, "OK")

            knowledge = vault / "🧠 500-Knowledge"
            knowledge.mkdir()
            normal = template.replace("{{title}}", "Aynı Başlık")
            (knowledge / "One.md").write_text(normal, encoding="utf-8")
            (knowledge / "Two.md").write_text(normal, encoding="utf-8")
            check = doctor._metadata_schema_check(doctor.Context(vault))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("duplicate title", check.evidence)

    def test_doctor_bounds_oversized_session_sources_before_budget_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            memory = vault / "🔮 850-Companion"
            knowledge = vault / "knowledge"
            state = vault / ".codex" / "scripts" / ".state"
            memory.mkdir(parents=True)
            knowledge.mkdir()
            state.mkdir(parents=True)
            (memory / "Core.md").write_text("x" * 7_100, encoding="utf-8")
            (memory / "Last-Session.md").write_text("# Last Session", encoding="utf-8")
            (memory / "Threads.md").write_text("# Threads", encoding="utf-8")
            (memory / "Kurallar.md").write_text("# Kurallar", encoding="utf-8")
            (knowledge / "index.md").write_text("# İndeks", encoding="utf-8")

            check = doctor._session_context_budget_check(doctor.Context(vault, state_dir=state))

        self.assertEqual(check.status, "OK")
        self.assertIn("6000", check.evidence)

    def test_session_context_budget_measures_filtered_context_without_view_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            companion = vault / "🔮 850-Companion"
            knowledge = vault / "knowledge"
            state = vault / ".codex" / "scripts" / ".state"
            for directory in (companion, knowledge, state):
                directory.mkdir(parents=True, exist_ok=True)
            for name, text in {
                "Core.md": "# Cevo\nGizli karar burada.\n",
                "Profile.md": "# Profil\nKısa yanıt ver.\n",
                "Last-Session.md": "# Son Oturum\n\n## Session: dün\nKarar sürdü.\n",
                "Kurallar.md": "# Kurallar\nKaynağı doğrula.\n",
                "Journal.md": "# Journal\n\n## Güncel\n\n### 2026-09-08\nNot.\n",
            }.items():
                (companion / name).write_text(text, encoding="utf-8")
            (knowledge / "index.md").write_text("# Bilgi Tabanı\n", encoding="utf-8")
            memory_ledger.suppress_derived_memory(
                vault / ".codex" / "private-memory", "Gizli karar burada."
            )
            before = {
                path.relative_to(vault).as_posix(): path.read_bytes()
                for path in vault.rglob("*")
                if path.is_file()
            }

            expected = doctor.build_session_context(
                vault, state, consume_reflection=False, write_views=False
            )
            check = doctor._session_context_budget_check(
                doctor.Context(vault, state_dir=state)
            )
            after = {
                path.relative_to(vault).as_posix(): path.read_bytes()
                for path in vault.rglob("*")
                if path.is_file()
            }
            materialized = doctor.build_session_context(
                vault, state, consume_reflection=False
            )

        self.assertNotIn("Gizli karar", expected)
        self.assertIn(str(len(expected)), check.evidence)
        self.assertEqual(before, after)
        self.assertNotIn("private-memory/views", after)
        self.assertEqual(expected, materialized)

    def test_doctor_warns_when_session_context_exceeds_soft_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex" / "scripts" / ".state"
            state.mkdir(parents=True)
            with mock.patch.object(
                doctor,
                "build_session_context",
                return_value="x" * 6_001,
            ):
                check = doctor._session_context_budget_check(doctor.Context(vault, state_dir=state))

        self.assertEqual(check.status, "WARN")
        self.assertIn("6000", check.evidence)

    def test_doctor_reports_required_components(self) -> None:
        checks = doctor.run_checks(CODEX_DIR.parent)
        names = {check.name for check in checks}

        self.assertTrue(
            {
                "Sürüm",
                "AGENTS.md",
                "Codex kancaları",
                "Codex proje kökü",
                "Hook çalışma zamanı",
                "Hook sağlığı",
                "Python",
                "Codex CLI",
                "Hafıza",
                "Makine katmanı",
                "Git",
                "Git hijyeni",
            }.issubset(names)
        )

    def test_doctor_names_a_configured_codex_path_that_does_not_resolve(self) -> None:
        """R11: yanlış binary operatöre görünür; wrapper'a sessiz düşüş yok."""
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "nowhere" / "codex.exe"
            with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": str(missing)}):
                check = doctor._codex_cli_check(doctor.Context())

        self.assertEqual(check.status, "FAIL")
        self.assertIn(str(missing), check.evidence)

    def test_doctor_rejects_a_project_root_outside_the_vault(self) -> None:
        checks = doctor.run_checks(
            CODEX_DIR.parent,
            project_root=CODEX_DIR.parent.parent,
        )

        project_root = next(check for check in checks if check.name == "Codex proje kökü")
        self.assertEqual(project_root.status, "FAIL")
        self.assertIn(str(CODEX_DIR.parent), project_root.evidence)

    def test_doctor_warns_when_hook_runtime_evidence_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checks = doctor.run_checks(
                CODEX_DIR.parent,
                project_root=CODEX_DIR.parent,
                state_dir=Path(temporary),
            )

        runtime = next(check for check in checks if check.name == "Hook çalışma zamanı")
        self.assertEqual(runtime.status, "WARN")

    def test_doctor_timestamp_checks_reject_nonfinite_values_without_crashing(self) -> None:
        bad_values = (float("nan"), float("inf"), float("-inf"), True, "invalid")
        for bad in bad_values:
            with self.subTest(timestamp=repr(bad)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                scoped_state = root / "scoped"
                scoped_state.mkdir()
                (scoped_state / "runtime-session-start-scope.json").write_text(
                    json.dumps({"ts": bad, "cwd": str(root)}),
                    encoding="utf-8",
                )
                scoped = doctor._scoped_hook_runtime(
                    doctor.Context(root, root, scoped_state, 100),
                )
                self.assertIsNotNone(scoped)
                self.assertEqual(scoped.status, "WARN")

                runtime_state = root / "runtime"
                runtime_state.mkdir()
                for name in ("session-start", "user-prompt"):
                    (runtime_state / f"runtime-{name}.json").write_text(
                        json.dumps({"ts": bad, "cwd": str(root)}),
                        encoding="utf-8",
                    )
                runtime = doctor._hook_runtime_check(
                    doctor.Context(root, root, runtime_state, 100),
                )
                self.assertEqual(runtime.status, "WARN")

                health_state = root / "health"
                health_state.mkdir()
                (health_state / "hook-health-bad.json").write_text(
                    json.dumps({
                        "generation": 1,
                        "status": "error",
                        "error": "RuntimeError",
                        "ts": bad,
                    }),
                    encoding="utf-8",
                )
                health = doctor._hook_health_check(
                    doctor.Context(root, root, health_state, 100),
                )
                self.assertEqual(health.status, "FAIL")

                flush_state = root / "flush"
                flush_state.mkdir()
                (flush_state / "flush-bad.json").write_text(
                    json.dumps({"status": "inflight", "ts": bad}),
                    encoding="utf-8",
                )
                inflight = doctor._flush_inflight_check(
                    doctor.Context(root, root, flush_state, 100),
                )
                self.assertEqual(inflight.status, "FAIL")

                queue_state = root / "queue" / "worker-jobs" / "running"
                queue_state.mkdir(parents=True)
                (queue_state / "job-bad.json").write_text(
                    json.dumps({"lease_until": bad, "owner_pid": 999999999}),
                    encoding="utf-8",
                )
                queue = doctor._worker_queue_check(
                    doctor.Context(root, root, root / "queue", 100),
                )
                self.assertEqual(queue.status, "FAIL")

                pending_state = root / "pending" / "worker-jobs" / "pending"
                pending_state.mkdir(parents=True)
                (pending_state / "job-bad.json").write_text(
                    json.dumps({"next_attempt_ts": bad}),
                    encoding="utf-8",
                )
                delayed = doctor._worker_delayed_job_check(
                    doctor.Context(root, root, root / "pending", 100),
                )
                self.assertEqual(delayed.status, "FAIL")

                retrieval_state = root / "retrieval"
                retrieval_state.mkdir()
                (retrieval_state / "runtime-vault-retrieval.json").write_text(
                    json.dumps({"ts": bad, "cwd": str(root)}),
                    encoding="utf-8",
                )
                with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                    retrieval = doctor._vault_retrieval_check(
                        doctor.Context(root, root, retrieval_state, 100),
                    )
                self.assertEqual(retrieval.status, "WARN")

    def test_doctor_fails_when_hook_health_contains_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            now = time.time()
            for event in ("session-start", "user-prompt"):
                (state / f"runtime-{event}.json").write_text(
                    json.dumps({"ts": int(now), "cwd": str(CODEX_DIR.parent)}),
                    encoding="utf-8",
                )
            (state / "hook-health.json").write_text(
                json.dumps({"ts": int(now), "error": "UnicodeEncodeError"}),
                encoding="utf-8",
            )

            checks = doctor.run_checks(
                CODEX_DIR.parent,
                project_root=CODEX_DIR.parent,
                state_dir=state,
                now=now,
            )

        health = next(check for check in checks if check.name == "Hook sağlığı")
        self.assertEqual(health.status, "FAIL")
        self.assertIn("UnicodeEncodeError", health.evidence)

    def test_doctor_fails_when_brain_health_contains_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "health.json").write_text(
                json.dumps(
                    {
                        "component": "flush",
                        "error": "input:transcript-path-missing",
                    }
                ),
                encoding="utf-8",
            )

            checks = doctor.run_checks(
                CODEX_DIR.parent,
                project_root=CODEX_DIR.parent,
                state_dir=state,
            )

        health = next(check for check in checks if check.name == "Beyin sağlığı")
        self.assertEqual(health.status, "FAIL")
        self.assertIn("flush", health.evidence)
        self.assertIn("transcript-path-missing", health.evidence)

    def test_doctor_fails_when_flush_inflight_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            now = time.time()
            (state / "flush-stale.json").write_text(
                json.dumps(
                    {
                        "session_id": "stale-session",
                        "ts": int(now - 301),
                        "status": "inflight",
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._flush_inflight_check(doctor.Context(state_dir=state, now=now))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("yarım", check.evidence)

    def test_doctor_accepts_fresh_or_completed_flush_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            now = time.time()
            for name, status, age in (
                ("flush-fresh.json", "inflight", 10),
                ("flush-complete.json", "ok", 600),
            ):
                (state / name).write_text(
                    json.dumps(
                        {
                            "session_id": name,
                            "ts": int(now - age),
                            "status": status,
                        }
                    ),
                    encoding="utf-8",
                )

            check = doctor._flush_inflight_check(doctor.Context(state_dir=state, now=now))

        self.assertEqual(check.status, "OK")

    def test_doctor_fails_when_vault_has_noncanonical_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            codex = vault / ".codex"
            codex.mkdir()
            (codex / "tag-taxonomy.json").write_text(
                (CODEX_DIR / "tag-taxonomy.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (vault / "note.md").write_text(
                "---\ntags: [search]\n---\n# Note\n",
                encoding="utf-8",
            )

            checks = doctor.run_checks(vault, project_root=vault)

        tags = next(check for check in checks if check.name == "Etiket sözlüğü")
        self.assertEqual(tags.status, "FAIL")
        self.assertIn("search", tags.evidence)

    def test_doctor_reports_vault_retrieval_ok_with_fresh_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex" / "scripts" / ".state"
            knowledge = vault / "🧠 500-Knowledge"
            state.mkdir(parents=True)
            knowledge.mkdir()
            (knowledge / "Karar.md").write_text(
                "---\ntitle: Finansal Karar\n---\n# Finansal Karar\nKanıtlı karar.",
                encoding="utf-8",
            )
            now = time.time()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps(
                    {
                        "ts": int(now),
                        "cwd": str(vault),
                        "outcome": "emitted",
                        "entries": 1,
                        "hits": 1,
                        "emitted": 1,
                        "chars": 2400,
                        "budget": 2600,
                        "paths": ["🧠 500-Knowledge/Karar.md"],
                        "duration_ms": 12,
                    }
                ),
                encoding="utf-8",
            )

            checks = doctor.run_checks(
                vault,
                project_root=vault,
                state_dir=state,
                now=now,
            )

        retrieval = next(check for check in checks if check.name == "Vault retrieval")
        self.assertEqual(retrieval.status, "OK")
        self.assertIn("1 not", retrieval.evidence)
        self.assertIn("emitted", retrieval.evidence)
        self.assertIn("1/1 aday", retrieval.evidence)
        self.assertIn("2400/2600 karakter", retrieval.evidence)

    def test_doctor_fails_when_vault_retrieval_health_contains_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex" / "scripts" / ".state"
            state.mkdir(parents=True)
            (state / "retrieval-health.json").write_text(
                json.dumps({"ts": 1234, "error": "UnicodeError"}),
                encoding="utf-8",
            )

            checks = doctor.run_checks(
                vault,
                project_root=vault,
                state_dir=state,
            )

        retrieval = next(check for check in checks if check.name == "Vault retrieval")
        self.assertEqual(retrieval.status, "FAIL")
        self.assertIn("UnicodeError", retrieval.evidence)

    def test_doctor_warns_when_vault_retrieval_receipt_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex" / "scripts" / ".state"
            knowledge = vault / "🧠 500-Knowledge"
            state.mkdir(parents=True)
            knowledge.mkdir()
            (knowledge / "Karar.md").write_text(
                "# Finansal Karar\nKanıtlı karar.",
                encoding="utf-8",
            )

            checks = doctor.run_checks(
                vault,
                project_root=vault,
                state_dir=state,
            )

        retrieval = next(check for check in checks if check.name == "Vault retrieval")
        self.assertEqual(retrieval.status, "WARN")
        self.assertIn("runtime kanıtı yok", retrieval.evidence)

    def test_doctor_prints_on_windows_cp1254_console(self) -> None:
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1254", errors="strict")
        checks = [
            doctor.Check(
                "Kök hijyeni",
                "WARN",
                "1 beklenmeyen dosya: ⚔️ Kök Artığı.md",
            ),
        ]

        with mock.patch.object(doctor, "run_checks", return_value=checks):
            with mock.patch.object(sys, "stdout", stream):
                exit_code = doctor.main([])
                stream.flush()

        output = buffer.getvalue().decode("cp1254")
        self.assertEqual(exit_code, 0)
        self.assertIn("| Kök hijyeni | WARN |", output)
        self.assertIn("\\u2694", output)
        self.assertEqual(output.count("Özet:"), 1)

    def test_doctor_unstable_snapshot_exits_nonzero(self) -> None:
        checks = [
            doctor.Check(
                "Hook çalışma zamanı",
                "UNSTABLE_SNAPSHOT",
                "mixed generations",
            )
        ]
        with mock.patch.object(doctor, "run_checks", return_value=checks):
            with mock.patch.object(sys, "stdout", io.StringIO()):
                exit_code = doctor.main([])

        self.assertEqual(exit_code, 1)


class DoctorRegistryTests(unittest.TestCase):
    def _defined_checks(self) -> dict[str, object]:
        return {
            name: value
            for name, value in vars(doctor).items()
            if name.endswith("_check")
            and inspect.isfunction(value)
            and value.__module__ == "doctor"
        }

    def test_every_check_function_is_registered(self) -> None:
        registered = {function for _name, function in doctor.CHECKS}
        unregistered = sorted(
            name
            for name, function in self._defined_checks().items()
            if function not in registered
        )
        self.assertEqual(unregistered, [], "T05_CHECK_NOT_REGISTERED")

    def test_registry_has_no_stale_entries(self) -> None:
        defined = set(self._defined_checks().values())
        stale = sorted(
            function.__name__
            for _name, function in doctor.CHECKS
            if function not in defined
        )
        self.assertEqual(stale, [], "T05_REGISTRY_STALE_ENTRY")
        names = [name for name, _function in doctor.CHECKS]
        self.assertEqual(len(names), len(set(names)), "T05_REGISTRY_DUPLICATE_NAME")

    def test_only_runs_a_single_named_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "knowledge" / "concepts").mkdir(parents=True)
            (vault / "knowledge" / "index.md").write_text(
                "# Bilgi indeksi\n\n[[knowledge/concepts/ornek|Örnek]]\n",
                encoding="utf-8",
            )
            (vault / "knowledge" / "concepts" / "ornek.md").write_text(
                "# Örnek\n\n[[knowledge/index|İndeks]]\n",
                encoding="utf-8",
            )
            real_run_checks = doctor.run_checks

            def run_synthetic_checks(_vault, *, project_root, only):
                return real_run_checks(vault, project_root=project_root, only=only)

            stream = io.StringIO()
            with (
                mock.patch.object(doctor, "run_checks", side_effect=run_synthetic_checks),
                mock.patch.object(sys, "stdout", stream),
            ):
                exit_code = doctor.main(["--only", "Vault grafiği"])

        output = stream.getvalue()
        rows = [
            line
            for line in output.splitlines()
            if line.startswith("|") and not line.startswith(("| Parça", "| ---"))
        ]
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(rows), 1, output)
        self.assertTrue(rows[0].startswith("| Vault grafiği | OK |"), rows[0])
        self.assertIn("Özet: OK=1 ", output)

    def test_only_rejects_unknown_check_name(self) -> None:
        with mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                doctor.main(["--only", "olmayan-kontrol"])

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
