"""doctor.py — dilim 2: kalan kontrol kollarını makbuz/dosya matrisleriyle sürer."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import doctor
from vault_corpus import NoteIndex

KEY = "a" * 64
DIGEST_T = "b" * 64
DIGEST_S = "c" * 64


def _ctx(root: Path, now: float = 1000.0) -> doctor.Context:
    state = root / ".codex/scripts/.state"
    state.mkdir(parents=True, exist_ok=True)
    return doctor.Context(vault=root, project_root=root, state_dir=state, now=now)


def _note(relative: str, text: str = "", error: str = "") -> NoteIndex:
    return NoteIndex(
        Path(relative), PurePosixPath(relative), text, {}, error
    )


def _memory_stub(read_source):
    memory = types.SimpleNamespace(read_source=read_source)
    manager = mock.MagicMock()
    manager.__enter__.return_value = memory
    manager.__exit__.return_value = False
    return mock.Mock(return_value=manager)


class SmallHelperArms(unittest.TestCase):
    def test_source_relative_base_key_rejects_empty_tails(self) -> None:
        self.assertIsNone(
            doctor._source_relative_base_key(PurePosixPath("a"), "../")
        )
        self.assertIsNone(
            doctor._source_relative_base_key(PurePosixPath("a"), "./")
        )

    def test_finite_timestamp_overflow(self) -> None:
        self.assertIsNone(doctor._finite_timestamp(10 ** 400))

    def test_companion_memory_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = root / "🔮 850-Companion"
            companion.mkdir(parents=True)
            for name in ("Core.md", "Kurallar.md"):
                (companion / name).write_text("içerik", encoding="utf-8")
            ctx = _ctx(root)
            with mock.patch.object(
                doctor, "memory_read",
                _memory_stub(mock.Mock(side_effect=ValueError("tercih"))),
            ):
                check = doctor._companion_memory_check(ctx)
        self.assertEqual(check.status, "FAIL")
        self.assertIn("ValueError", check.evidence)


class GitBranchArms(unittest.TestCase):
    def test_missing_git_and_branch_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor.subprocess, "run", side_effect=FileNotFoundError
            ):
                self.assertEqual(
                    doctor._git_branch_check(ctx).evidence, "git yok"
                )

            ok = types.SimpleNamespace(returncode=0, stdout="true\n", stderr="")
            with mock.patch.object(
                doctor.subprocess, "run",
                side_effect=[ok, subprocess.SubprocessError()],
            ):
                self.assertEqual(
                    doctor._git_branch_check(ctx).evidence, "branch okunamadı"
                )

            bad = types.SimpleNamespace(returncode=1, stdout="", stderr="")
            with mock.patch.object(
                doctor.subprocess, "run", side_effect=[ok, bad]
            ):
                self.assertEqual(
                    doctor._git_branch_check(ctx).evidence, "branch okunamadı"
                )


class ScopedRuntimeArms(unittest.TestCase):
    def test_receipt_without_cwd_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            (ctx.state_dir / "runtime-session-start-a.json").write_text(
                json.dumps({"session_key": KEY, "ts": ctx.now - 1}),
                encoding="utf-8",
            )
            check = doctor._scoped_hook_runtime(ctx)
        self.assertEqual(check.status, "WARN")
        self.assertIn("fresh", check.evidence)

    def test_hook_runtime_check_prefers_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            (ctx.state_dir / "runtime-session-start-a.json").write_text(
                "{kirik", encoding="utf-8"
            )
            check = doctor._hook_runtime_check(ctx)
        self.assertEqual(check.status, "FAIL")


class InterpreterArms(unittest.TestCase):
    def _run(self, command, root: Path):
        hooks = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"commandWindows": command}]}
                ]
            }
        }
        (root / ".codex").mkdir(exist_ok=True)
        (root / ".codex" / "hooks.json").write_text(
            json.dumps(hooks), encoding="utf-8"
        )
        return doctor._hook_interpreter_check(_ctx(root))

    def test_command_shape_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            check = self._run(5, root)
            self.assertIn("yorumlayıcısı yok", check.evidence)

            check = self._run('"acik kalan', root)
            self.assertIn("çözümlenemedi", check.evidence)

            check = self._run("   ", root)
            self.assertIn("yorumlayıcısı yok", check.evidence)

    def test_resolution_failures_and_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(doctor.shutil, "which", return_value=None):
                check = self._run(str(root / "yok.exe"), root)
                self.assertIn("bulunamadı", check.evidence)

                script = root / "sahte.py"
                script.write_text("print()", encoding="utf-8")
                check = self._run(f'"{script}"', root)
                self.assertEqual(check.status, "WARN")
                self.assertIn("farklı", check.evidence)

            with (
                mock.patch.object(
                    doctor.shutil, "which", return_value=str(root / "x.exe")
                ),
                mock.patch.object(Path, "resolve", side_effect=OSError),
            ):
                check = self._run("x.exe", root)
            self.assertIn("çözümlenemedi:", check.evidence)


class LegacyRuntimeArms(unittest.TestCase):
    def _write(self, ctx, name: str, payload) -> None:
        (ctx.state_dir / name).write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )

    def _receipt(self, ctx, **overrides) -> dict:
        receipt = {
            "session_key": KEY,
            "ts": ctx.now - 1,
            "cwd": str(ctx.project_root),
            "outcome": "emitted",
            "context_chars": 100,
            "sections": sorted(doctor.SESSION_START_REQUIRED_SECTIONS),
        }
        receipt.update(overrides)
        return receipt

    def test_non_dict_and_stale_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            self._write(ctx, "runtime-session-start.json", "[]")
            check = doctor._hook_runtime_check(ctx)
            self.assertIn("SessionStart", check.evidence)

            self._write(
                ctx, "runtime-session-start.json",
                self._receipt(ctx, cwd=str(ctx.project_root / "baska")),
            )
            check = doctor._hook_runtime_check(ctx)
            self.assertIn("SessionStart", check.evidence)

    def test_contract_sections_and_clean_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            self._write(
                ctx, "runtime-user-prompt.json",
                {"session_key": KEY, "ts": ctx.now - 1,
                 "cwd": str(ctx.project_root)},
            )
            self._write(
                ctx, "runtime-session-start.json",
                self._receipt(ctx, sections=["Hafıza Protokolü"]),
            )
            check = doctor._hook_runtime_check(ctx)
            self.assertIn("SessionStart-contract", check.evidence)

            self._write(
                ctx, "runtime-session-start.json", self._receipt(ctx)
            )
            check = doctor._hook_runtime_check(ctx)
            self.assertEqual(check.status, "OK")
            self.assertIn("context 100 karakter", check.evidence)


class HookHealthArms(unittest.TestCase):
    def test_scoped_receipt_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            scoped = ctx.state_dir / "hook-health-global.json"

            scoped.write_text("{kirik", encoding="utf-8")
            self.assertIn(
                "okunamadı", doctor._hook_health_check(ctx).evidence
            )

            scoped.write_text("[]", encoding="utf-8")
            self.assertIn(
                "object değil", doctor._hook_health_check(ctx).evidence
            )

            scoped.write_text(
                json.dumps({
                    "schema_version": 2, "component": "hook",
                    "session_key": "global", "generation": 1,
                    "ts": 100, "status": "error",
                }),
                encoding="utf-8",
            )
            self.assertIn(
                "hata sınıfı eksik", doctor._hook_health_check(ctx).evidence
            )

    def test_legacy_receipt_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            legacy = ctx.state_dir / "hook-health.json"

            legacy.write_text("[]", encoding="utf-8")
            self.assertIn(
                "object değil", doctor._hook_health_check(ctx).evidence
            )

            legacy.write_text(
                json.dumps({
                    "schema_version": 2, "component": "hook",
                    "session_key": "global", "generation": 1,
                    "ts": 100, "status": "ok",
                }),
                encoding="utf-8",
            )
            check = doctor._hook_health_check(ctx)
            self.assertEqual((check.status, check.evidence), ("OK", "temiz"))

            legacy.write_text(json.dumps({"detay": 1}), encoding="utf-8")
            self.assertIn(
                "hata sınıfı eksik", doctor._hook_health_check(ctx).evidence
            )


class BrainHealthArms(unittest.TestCase):
    def test_invalid_current_health_generation_is_not_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            health = ctx.state_dir / "health.json"
            for generation in (-1, True, 1.5):
                with self.subTest(generation=generation):
                    health.write_text(
                        json.dumps(
                            {
                                "schema_version": 2,
                                "generation": generation,
                                "components": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                    self.assertNotEqual(
                        doctor._brain_health_check(ctx).status, "OK"
                    )

            health.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "generation": 0,
                        "components": {},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(doctor._brain_health_check(ctx).status, "OK")

    def test_read_and_shape_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            health = ctx.state_dir / "health.json"

            health.write_text("{kirik", encoding="utf-8")
            self.assertIn(
                "okunamadı", doctor._brain_health_check(ctx).evidence
            )

            health.write_text("[]", encoding="utf-8")
            self.assertIn(
                "object değil", doctor._brain_health_check(ctx).evidence
            )

            health.write_text(
                json.dumps({
                    "schema_version": 2,
                    "components": {
                        "flush": {"status": "warning", "component": "flush",
                                  "error": "warn:x"},
                    },
                }),
                encoding="utf-8",
            )
            check = doctor._brain_health_check(ctx)
            self.assertEqual(check.status, "WARN")

            health.write_text(
                json.dumps({"component": "flush"}), encoding="utf-8"
            )
            self.assertIn(
                "hata ayrıntısı eksik", doctor._brain_health_check(ctx).evidence
            )


def _flush_receipt(status: str = "prepared", **overrides) -> dict:
    nested = {
        "idempotency_key": KEY, "status": "ok", "ts": 100, "generation": 1,
        "transcript_digest": DIGEST_T, "summary_digest": DIGEST_S,
        "reason": "turnend",
    }
    nested.update(overrides.pop("nested", {}))
    receipt = {
        "schema_version": 2, "session_key": KEY, "ts": 100, "generation": 1,
        "status": status, "reason": "turnend",
        "receipts": {KEY: nested},
        "idempotency_key": KEY,
        "transcript_digest": DIGEST_T, "summary_digest": DIGEST_S,
    }
    receipt.update(overrides)
    return receipt


class FlushInflightArms(unittest.TestCase):
    def _check(self, receipt) -> doctor.Check:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            (ctx.state_dir / f"flush-{KEY}.json").write_text(
                receipt if isinstance(receipt, str) else json.dumps(receipt),
                encoding="utf-8",
            )
            return doctor._flush_inflight_check(ctx)

    def test_non_dict_receipt(self) -> None:
        self.assertIn("bozuk receipt", self._check("[]").evidence)

    def test_nested_identity_mismatch_matrix(self) -> None:
        cases = [
            {"nested": {"transcript_digest": "d" * 64}},
            {"status": "ok", "nested": {"status": "prepared"}},
            {"nested": {"status": "prepared", "ts": 101}},
            {"nested": {"status": "prepared", "generation": 2}},
            {"nested": {"status": "prepared", "reason": "precompact"}},
        ]
        for overrides in cases:
            with self.subTest(overrides=str(overrides)[:50]):
                status = overrides.pop("status", "prepared")
                receipt = _flush_receipt(status, **overrides)
                check = self._check(receipt)
                self.assertIn("bozuk receipt", check.evidence)

    def test_inflight_fail_and_ok_shape_guards(self) -> None:
        inflight = {
            "schema_version": 2, "session_key": KEY, "ts": 100,
            "generation": 1, "status": "inflight", "reason": "turnend",
            "receipts": {}, "transcript_digest": DIGEST_T,
            "idempotency_key": KEY,
        }
        self.assertIn("bozuk receipt", self._check(inflight).evidence)

        fail_eksik = {
            "schema_version": 2, "session_key": KEY, "ts": 100,
            "generation": 1, "status": "fail", "receipts": {},
        }
        self.assertIn("bozuk receipt", self._check(fail_eksik).evidence)

        fail_reason = {
            "schema_version": 2, "session_key": KEY, "ts": 100,
            "generation": 1, "status": "fail", "detail": "hata",
            "reason": "turnend", "receipts": {},
        }
        self.assertIn("bozuk receipt", self._check(fail_reason).evidence)

        ok_detay = {
            "schema_version": 2, "session_key": KEY, "ts": 100,
            "generation": 1, "status": "ok", "detail": "bilinmez",
            "receipts": {},
        }
        self.assertIn("bozuk receipt", self._check(ok_detay).evidence)


class WorkerQueueArms(unittest.TestCase):
    def test_fence_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor, "has_unverified_process_tree", side_effect=OSError
            ):
                check = doctor._worker_queue_check(ctx)
        self.assertIn("cleanup fence okunamadı", check.evidence)

    def test_job_file_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            running = ctx.state_dir / "worker-jobs" / "running"
            running.mkdir(parents=True)
            job = running / "job-x.json"

            job.write_text("{kirik", encoding="utf-8")
            self.assertIn("okunamadı", doctor._worker_queue_check(ctx).evidence)

            job.write_text("[]", encoding="utf-8")
            self.assertIn(
                "object değil", doctor._worker_queue_check(ctx).evidence
            )
            job.unlink()

            dead = ctx.state_dir / "worker-jobs" / "dead-letter"
            dead.mkdir(parents=True)
            (dead / "job-y.json").write_text("[]", encoding="utf-8")
            self.assertIn(
                "object değil", doctor._worker_queue_check(ctx).evidence
            )

    def test_unknown_terminal_and_inspection_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            dead = ctx.state_dir / "worker-jobs" / "dead-letter"
            dead.mkdir(parents=True)
            (dead / "job-y.json").write_text(
                json.dumps({"terminal_reason": "bilinmez"}), encoding="utf-8"
            )
            with mock.patch.object(
                doctor, "inspect_worker_queue",
                return_value={"status": "ok", "counts": {}},
            ):
                check = doctor._worker_queue_check(ctx)
            self.assertEqual(check.status, "FAIL")
            self.assertIn("unclassified-terminal=1", check.evidence)

            with mock.patch.object(
                doctor, "inspect_worker_queue", side_effect=ValueError
            ):
                self.assertIn(
                    "kayıtları okunamadı",
                    doctor._worker_queue_check(ctx).evidence,
                )
            with mock.patch.object(
                doctor, "inspect_worker_queue", return_value={"counts": 5}
            ):
                self.assertIn(
                    "özeti geçersiz", doctor._worker_queue_check(ctx).evidence
                )

    def test_pending_only_queue_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            pending = ctx.state_dir / "worker-jobs" / "pending"
            pending.mkdir(parents=True)
            (pending / "job-z.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(
                doctor, "inspect_worker_queue",
                return_value={"status": "ok", "counts": {}},
            ):
                check = doctor._worker_queue_check(ctx)
        self.assertEqual(check.status, "WARN")


class WorkerDelayedArms(unittest.TestCase):
    def _supervisor(self, status: str) -> dict:
        return {
            "schema_version": doctor.SUPERVISOR_SCHEMA_VERSION,
            "status": status, "generation": 1, "launch_token": "t",
            "owner_pid": 0, "lease_until": 1.0, "updated_ts": 1.0,
        }

    def test_pending_and_supervisor_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            pending = ctx.state_dir / "worker-jobs" / "pending"
            pending.mkdir(parents=True)
            job = pending / "job-x.json"

            job.write_text("{kirik", encoding="utf-8")
            self.assertIn(
                "pending okunamadı",
                doctor._worker_delayed_job_check(ctx).evidence,
            )
            job.write_text("[]", encoding="utf-8")
            self.assertIn(
                "pending object değil",
                doctor._worker_delayed_job_check(ctx).evidence,
            )

            job.write_text(
                json.dumps({"next_attempt_ts": 1}), encoding="utf-8"
            )
            check = doctor._worker_delayed_job_check(ctx)
            self.assertEqual(check.status, "WARN")
            self.assertIn("supervisor=missing", check.evidence)

            receipt = ctx.state_dir / "worker-supervisor.json"
            receipt.write_text("[]", encoding="utf-8")
            self.assertIn(
                "supervisor object değil",
                doctor._worker_delayed_job_check(ctx).evidence,
            )

            receipt.write_text(
                json.dumps(self._supervisor("failed")), encoding="utf-8"
            )
            check = doctor._worker_delayed_job_check(ctx)
            self.assertEqual(check.status, "FAIL")
            self.assertIn("supervisor=failed", check.evidence)


class RetentionArms(unittest.TestCase):
    def test_stat_failures_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            (ctx.state_dir / "kayit.json").write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(Path, "is_file", return_value=True),
                mock.patch.object(Path, "stat", side_effect=OSError),
            ):
                check = doctor._state_retention_check(ctx)
        self.assertEqual(check.status, "OK")
        self.assertIn("bytes=0", check.evidence)

    def test_cache_and_age_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            (ctx.state_dir / "kayit.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(doctor, "MAX_CACHE_BYTES", -1):
                self.assertEqual(
                    doctor._state_retention_check(ctx).status, "FAIL"
                )
            eski = doctor.Context(
                vault=ctx.vault, project_root=ctx.vault,
                state_dir=ctx.state_dir, now=10.0 ** 10,
            )
            self.assertEqual(
                doctor._state_retention_check(eski).status, "WARN"
            )


class GitBackedArms(unittest.TestCase):
    def test_local_checkpoint_subprocess_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor.subprocess, "run", side_effect=OSError
            ):
                check = doctor._local_checkpoint_check(ctx)
        self.assertIn("Git durumu okunamadı", check.evidence)

    def test_git_hygiene_result_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor.subprocess, "run", side_effect=PermissionError
            ):
                self.assertIn(
                    "okunamadı", doctor._git_hygiene_check(ctx).evidence
                )
            with mock.patch.object(
                doctor.subprocess, "run", return_value=object()
            ):
                self.assertIn(
                    "çıktısı bozuk", doctor._git_hygiene_check(ctx).evidence
                )
            rename = types.SimpleNamespace(
                returncode=0, stdout=b"R  x\x00", stderr=b""
            )
            with mock.patch.object(
                doctor.subprocess, "run", return_value=rename
            ):
                self.assertIn(
                    "çıktısı bozuk", doctor._git_hygiene_check(ctx).evidence
                )

    def test_thread_workload_forget_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor, "memory_read",
                _memory_stub(mock.Mock(return_value=("rel", None))),
            ):
                check = doctor._thread_workload_check(ctx)
        self.assertEqual(check.status, "OK")
        self.assertIn("kapsam dışında", check.evidence)

    def test_root_hygiene_failure_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = _ctx(root)
            (root / ".git").write_text("gitdir: baska", encoding="utf-8")
            with mock.patch.object(
                doctor.subprocess, "run", side_effect=OSError
            ):
                check = doctor._root_hygiene_check(ctx)
            self.assertEqual(check.status, "WARN")
            self.assertIn(".git", check.evidence)

            with mock.patch.object(Path, "iterdir", side_effect=OSError):
                check = doctor._root_hygiene_check(ctx)
            self.assertIn("okunamadı", check.evidence)


class RetrievalArms(unittest.TestCase):
    def _check(self, receipt, now: float = 1000.0) -> doctor.Check:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary), now=now)
            if receipt is not None:
                (ctx.state_dir / "runtime-vault-retrieval.json").write_text(
                    receipt if isinstance(receipt, str)
                    else json.dumps(receipt),
                    encoding="utf-8",
                )
            with mock.patch.object(
                doctor, "build_vault_map", return_value=[1]
            ):
                return doctor._vault_retrieval_check(ctx)

    def _receipt(self, ctx_root: str = "", **overrides) -> dict:
        receipt = {
            "ts": 999, "cwd": ctx_root or str(Path.cwd()),
            "outcome": "emitted", "entries": 2, "hits": 2, "emitted": 1,
            "chars": 5, "budget": 10, "duration_ms": 1, "paths": ["x"],
        }
        receipt.update(overrides)
        return receipt

    def test_health_error_class_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            (ctx.state_dir / "retrieval-health.json").write_text(
                json.dumps({"error": ""}), encoding="utf-8"
            )
            self.assertIn(
                "hata sınıfı eksik", doctor._vault_retrieval_check(ctx).evidence
            )

    def test_map_build_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor, "build_vault_map", side_effect=ValueError
            ):
                check = doctor._vault_retrieval_check(ctx)
        self.assertIn("harita kurulamadı", check.evidence)

    def test_receipt_shape_matrix(self) -> None:
        cases = [
            ("[]", "object değil"),
            (self._receipt(cwd=""), "cwd geçersiz"),
            ("{kirik", "receipt okunamadı"),
            (self._receipt(chars=-1), "metriği geçersiz"),
            (self._receipt(paths="x"), "yolları geçersiz"),
            (self._receipt(paths=["x", "x"], emitted=2), "yolları geçersiz"),
            (self._receipt(paths=["x"], emitted=0), "sayımları tutarsız"),
            (
                self._receipt(outcome="empty", entries=0, hits=0, emitted=1,
                              chars=0),
                "sayımları tutarsız",
            ),
            (
                self._receipt(outcome="emitted", emitted=0, chars=0,
                              paths=[], hits=0, entries=0),
                "metriği tutarsız",
            ),
            (
                self._receipt(outcome="empty", emitted=0, chars=5, paths=[],
                              hits=0, entries=0),
                "metriği tutarsız",
            ),
            (
                self._receipt(outcome="skipped", emitted=0, chars=0, paths=[],
                              hits=0, entries=1),
                "metriği tutarsız",
            ),
            (
                self._receipt(outcome="error", emitted=0, chars=0, paths=[],
                              hits=0, entries=0),
                "son arama başarısız",
            ),
        ]
        for receipt, phrase in cases:
            with self.subTest(phrase=phrase):
                check = self._check(receipt)
                self.assertEqual(check.status, "FAIL")
                self.assertIn(phrase, check.evidence)

    def test_stale_receipt_warns(self) -> None:
        receipt = self._receipt(ts=10)
        check = self._check(receipt, now=10.0 ** 6)
        self.assertEqual(check.status, "WARN")
        self.assertIn("güncel değil", check.evidence)


class VaultNoteArms(unittest.TestCase):
    def test_taxonomy_clean_vault_reports_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".codex").mkdir()
            kaynak = (
                Path(__file__).resolve().parents[1] / "tag-taxonomy.json"
            )
            (root / ".codex" / "tag-taxonomy.json").write_text(
                kaynak.read_text(encoding="utf-8"), encoding="utf-8"
            )
            check = doctor._tag_taxonomy_check(_ctx(root))
        self.assertEqual(check.status, "OK")
        self.assertIn("ihlal yok", check.evidence)

    def test_link_check_duplicate_and_error_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            ctx.__dict__["notes"] = (
                _note("🧠 500-Knowledge/x.md", "a"),
                _note("🧠 500-Knowledge/alt/x.md", "b"),
            )
            check = doctor._vault_link_check(ctx)
            self.assertIn("duplicate note adı", check.evidence)

            ctx2 = _ctx(Path(temporary))
            ctx2.__dict__["notes"] = (
                _note("🧠 500-Knowledge/x.md", error="UnicodeDecodeError"),
            )
            check = doctor._vault_link_check(ctx2)
            self.assertIn("okunamadı", check.evidence)

            ctx3 = _ctx(Path(temporary))
            ctx3.__dict__["notes"] = (
                _note("🧠 500-Knowledge/x.md", "önce [[|takma]] sonra"),
            )
            check = doctor._vault_link_check(ctx3)
            self.assertEqual(check.status, "OK")

    def test_graph_check_unreadable_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            ctx.__dict__["notes"] = (
                _note("🧠 500-Knowledge/x.md", error="OSError"),
            )
            check = doctor._vault_graph_check(ctx)
        self.assertIn("okunamadı", check.evidence)

    def test_archive_index_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "📦 900-Archive").mkdir()
            ctx = _ctx(root)
            check = doctor._archive_index_check(ctx)
            self.assertEqual(check.status, "WARN")
            self.assertIn("Archive.md yok", check.evidence)

            ctx2 = _ctx(root)
            ctx2.__dict__["notes"] = (
                _note("📦 900-Archive/Archive.md", "[[#hedefsiz]]"),
                _note("📦 900-Archive/eski.md", error="OSError"),
            )
            check = doctor._archive_index_check(ctx2)
            self.assertEqual(check.status, "WARN")
            self.assertIn("Archive okunamadı", check.evidence)

            ctx3 = _ctx(root)
            ctx3.__dict__["notes"] = (
                _note("📦 900-Archive/Archive.md", "[[#hedefsiz]]"),
                _note("📦 900-Archive/eski.md", "içerik"),
            )
            check = doctor._archive_index_check(ctx3)
            self.assertEqual(check.status, "WARN")
            self.assertIn("indekslenmemiş", check.evidence)

    def test_knowledge_schema_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor, "validate_knowledge_tree", side_effect=OSError
            ):
                self.assertIn(
                    "okunamadı", doctor._knowledge_schema_check(ctx).evidence
                )
            report = types.SimpleNamespace(
                issues=[], concepts=1, connections=2, index_rows=3
            )
            with mock.patch.object(
                doctor, "validate_knowledge_tree", return_value=report
            ):
                check = doctor._knowledge_schema_check(ctx)
        self.assertEqual(check.status, "OK")
        self.assertIn("1 kavram", check.evidence)

    def test_metadata_check_flags_unreadable_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            ctx.__dict__["notes"] = (
                _note("🧠 500-Knowledge/x.md", error="OSError"),
            )
            check = doctor._metadata_schema_check(ctx)
        self.assertEqual(check.status, "FAIL")
        self.assertIn("okunamadı", check.evidence)


class RunChecksArms(unittest.TestCase):
    def test_subprocess_error_is_classified(self) -> None:
        def patlayan(_ctx):
            raise subprocess.SubprocessError("alt süreç")

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(doctor, "CHECKS", (("X", patlayan),)):
                checks = doctor.run_checks(Path(temporary))
        self.assertEqual(len(checks), 1)
        self.assertIn("alt süreç kontrolü", checks[0].evidence)


if __name__ == "__main__":
    unittest.main()
