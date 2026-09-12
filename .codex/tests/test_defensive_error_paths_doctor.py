"""doctor.py kontrol dallarını makbuz/dosya bozulmalarıyla sürer."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import doctor
import flush
import hook
import worker_supervisor


def _ctx(root: Path, now: float = 1000.0) -> doctor.Context:
    state = root / ".codex/scripts/.state"
    state.mkdir(parents=True, exist_ok=True)
    return doctor.Context(vault=root, project_root=root, state_dir=state, now=now)


def _start_receipt(ctx: doctor.Context, name: str = "a", **overrides) -> Path:
    receipt = {
        "session_key": "k" * 64,
        "ts": ctx.now - 1,
        "cwd": str(ctx.project_root),
        "event_generation": 1,
        "outcome": "emitted",
        "context_chars": 100,
        "sections": sorted(doctor.SESSION_START_REQUIRED_SECTIONS),
    }
    receipt.update(overrides)
    path = ctx.state_dir / f"runtime-session-start-{name}.json"
    path.write_text(
        receipt if isinstance(receipt, str) else json.dumps(receipt),
        encoding="utf-8",
    )
    return path


class ScopedHookRuntimeMatrix(unittest.TestCase):
    def test_finite_timestamp_rejects_bool_and_overflow(self) -> None:
        self.assertIsNone(doctor._finite_timestamp(True))
        self.assertIsNone(doctor._finite_timestamp(float("inf")))
        self.assertIsNone(doctor._finite_timestamp("x"))

    def test_receipt_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = _ctx(root)

            self.assertIsNone(doctor._scoped_hook_runtime(ctx))

            bozuk = ctx.state_dir / "runtime-session-start-x.json"
            bozuk.write_text("{kirik", encoding="utf-8")
            self.assertEqual(doctor._scoped_hook_runtime(ctx).status, "FAIL")
            bozuk.unlink()

            path = _start_receipt(ctx, cwd=str(root / "baska"))
            self.assertIn("fresh", doctor._scoped_hook_runtime(ctx).evidence)
            path.unlink()

            path = _start_receipt(ctx, event_generation=0)
            self.assertEqual(
                doctor._scoped_hook_runtime(ctx).status, "UNSTABLE_SNAPSHOT"
            )
            path.unlink()

            path = _start_receipt(ctx, outcome="skipped")
            self.assertEqual(doctor._scoped_hook_runtime(ctx).status, "FAIL")
            path.unlink()

            path = _start_receipt(ctx, sections=["Hafıza Protokolü"])
            check = doctor._scoped_hook_runtime(ctx)
            self.assertEqual(check.status, "WARN")
            self.assertIn("eksik", check.evidence)
            path.unlink()

            path = _start_receipt(ctx)
            check = doctor._scoped_hook_runtime(ctx)
            self.assertEqual(check.status, "WARN")
            self.assertIn("UserPrompt", check.evidence)

            prompt_bozuk = ctx.state_dir / "runtime-user-prompt-x.json"
            prompt_bozuk.write_text("{kirik", encoding="utf-8")
            self.assertEqual(doctor._scoped_hook_runtime(ctx).status, "FAIL")
            prompt_bozuk.write_text(
                json.dumps({"session_key": "k" * 64, "ts": ctx.now - 1}),
                encoding="utf-8",
            )
            self.assertEqual(doctor._scoped_hook_runtime(ctx).status, "OK")


class SubprocessBackedChecks(unittest.TestCase):
    def test_git_branch_check_handles_subprocess_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor.subprocess, "run",
                side_effect=subprocess.SubprocessError("git yok"),
            ):
                self.assertIn(
                    doctor._git_branch_check(ctx).status, {"FAIL", "WARN"}
                )
            with mock.patch.object(
                doctor.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("git", 1),
            ):
                self.assertIn(
                    doctor._git_branch_check(ctx).status, {"FAIL", "WARN"}
                )

    def test_companion_memory_check_maps_read_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(doctor, "memory_read", side_effect=OSError("disk")):
                check = doctor._companion_memory_check(ctx)
        self.assertEqual(check.status, "FAIL")

    def test_hook_interpreter_check_failure_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = _ctx(root)
            hooks = root / ".codex" / "hooks.json"
            hooks.parent.mkdir(parents=True, exist_ok=True)

            hooks.write_text(json.dumps({"hooks": {"SessionStart": [
                {"command": '"kapanmamis tirnak'}
            ]}}), encoding="utf-8")
            self.assertIn(
                doctor._hook_interpreter_check(ctx).status, {"FAIL", "WARN"}
            )

            hooks.write_text(json.dumps({"hooks": {"SessionStart": [
                {"command": "yok-boyle-yorumlayici arg"}
            ]}}), encoding="utf-8")
            self.assertIn(
                doctor._hook_interpreter_check(ctx).status, {"FAIL", "WARN"}
            )


class HealthReceiptChecks(unittest.TestCase):
    def test_hook_health_rejects_malformed_receipts(self) -> None:
        cases = [
            "{kirik",
            {"schema_version": 99, "status": "ok", "ts": 1},
            {"schema_version": 2, "component": "hook", "session_key": "k" * 64,
             "generation": 1, "status": "tuhaf", "ts": 1},
            {"schema_version": 2, "component": "hook", "session_key": "k" * 64,
             "generation": 1, "status": "error", "ts": float("inf")},
            {"schema_version": 2, "component": "hook", "session_key": "k" * 64,
             "generation": 1, "status": "error", "ts": 999},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:50]):
                with tempfile.TemporaryDirectory() as temporary:
                    ctx = _ctx(Path(temporary))
                    target = ctx.state_dir / "hook-health.json"
                    target.write_text(
                        payload if isinstance(payload, str) else json.dumps(payload),
                        encoding="utf-8",
                    )
                    check = doctor._hook_health_check(ctx)
                self.assertIn(check.status, {"FAIL", "WARN", "UNSTABLE_SNAPSHOT"})

    def test_brain_health_rejects_bad_schema(self) -> None:
        cases = [
            {"schema_version": 1},
            {"schema_version": 2, "generation": 1, "components": []},
            {"schema_version": 2, "generation": 1,
             "components": {"a": {"status": "error"}}},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:40]):
                with tempfile.TemporaryDirectory() as temporary:
                    ctx = _ctx(Path(temporary))
                    (ctx.state_dir / "health.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    check = doctor._brain_health_check(ctx)
                self.assertIn(check.status, {"FAIL", "WARN"})

    def test_state_privacy_flags_unreadable_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            (ctx.state_dir / "conversation-bozuk.json").write_text(
                "{kirik", encoding="utf-8"
            )
            check = doctor._state_privacy_check(ctx)
        self.assertIn(check.status, {"FAIL", "WARN"})

    def test_flush_inflight_rejects_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            flush._write_flush_state(
                ctx.state_dir,
                "oturum",
                ctx.now,
                "inflight",
                reason="turnend",
                transcript_digest="a" * 64,
            )
            path = ctx.state_dir / f"flush-{flush._session_key('oturum')}.json"
            receipt = json.loads(path.read_text(encoding="utf-8"))
            receipt["session_key"] = "b" * 64
            path.write_text(json.dumps(receipt), encoding="utf-8")
            check = doctor._flush_inflight_check(ctx)
        self.assertEqual(
            check,
            doctor.Check(
                "Flush devamlılığı",
                "FAIL",
                f"1 bozuk receipt; ilk: {path.name}",
            ),
        )


class WorkerAndRetentionChecks(unittest.TestCase):
    def test_worker_queue_check_flags_corrupt_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            dead = ctx.state_dir / "worker-jobs" / "dead-letter"
            dead.mkdir(parents=True)
            (dead / "job-bozuk.json").write_text("{kirik", encoding="utf-8")
            check = doctor._worker_queue_check(ctx)
        self.assertIn(check.status, {"FAIL", "WARN"})

    def test_worker_delayed_check_flags_bad_supervisor_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            jobs = ctx.state_dir / "worker-jobs" / "pending"
            jobs.mkdir(parents=True)
            (jobs / "job-ready.json").write_text(
                json.dumps({"next_attempt_ts": ctx.now - 1}),
                encoding="utf-8",
            )
            worker_supervisor.ensure_supervisor(
                ctx.state_dir,
                vault_root=ctx.vault,
                launcher=mock.Mock(),
                now=ctx.now,
            )
            receipt_path = ctx.state_dir / "worker-supervisor.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["schema_version"] = 99
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            check = doctor._worker_delayed_job_check(ctx)
            self.assertEqual(
                check,
                doctor.Check(
                    "Worker gecikmiş iş",
                    "FAIL",
                    "supervisor receipt alanları geçersiz",
                ),
            )

            receipt["schema_version"] = doctor.SUPERVISOR_SCHEMA_VERSION
            receipt["status"] = "bozuk"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            check = doctor._worker_delayed_job_check(ctx)
        self.assertEqual(
            check,
            doctor.Check("Worker gecikmiş iş", "FAIL", "supervisor status geçersiz"),
        )

    def test_compiler_queue_check_maps_policy_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            import compile_state
            with mock.patch.object(
                doctor.compile_state, "load",
                side_effect=compile_state.PolicyError("bozuk"),
            ):
                check = doctor._compiler_queue_check(ctx)
        self.assertEqual(check.status, "FAIL")

    def test_retrieval_check_receipt_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            health = ctx.state_dir / "retrieval-health.json"
            health.write_text("{kirik", encoding="utf-8")
            self.assertIn(
                doctor._vault_retrieval_check(ctx).status, {"FAIL", "WARN"}
            )
            health.unlink()

            receipt = ctx.state_dir / "runtime-vault-retrieval.json"
            receipt.write_text(json.dumps({"outcome": "error"}), encoding="utf-8")
            self.assertIn(
                doctor._vault_retrieval_check(ctx).status, {"FAIL", "WARN"}
            )
            receipt.write_text(
                json.dumps({
                    "ts": ctx.now - 1, "cwd": str(ctx.project_root),
                    "outcome": "ok", "entries": 1, "hits": 5, "emitted": 9,
                    "chars": 10, "budget": 5, "paths": ["a"], "duration_ms": 1,
                }),
                encoding="utf-8",
            )
            self.assertIn(
                doctor._vault_retrieval_check(ctx).status, {"FAIL", "WARN"}
            )

    def test_session_context_budget_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _ctx(Path(temporary))
            with mock.patch.object(
                doctor, "build_session_context",
                return_value="u" * (doctor.SESSION_CONTEXT_TARGET_CHARS + 1),
            ):
                self.assertEqual(
                    doctor._session_context_budget_check(ctx).status, "FAIL"
                )
            with mock.patch.object(
                doctor, "build_session_context",
                return_value="u" * (doctor.SESSION_CONTEXT_SOFT_TARGET_CHARS + 1),
            ):
                self.assertEqual(
                    doctor._session_context_budget_check(ctx).status, "WARN"
                )
            with mock.patch.object(
                doctor, "build_session_context", side_effect=OSError("kaynak")
            ):
                self.assertIn(
                    doctor._session_context_budget_check(ctx).status,
                    {"FAIL", "WARN"},
                )

    def test_run_checks_isolates_crashing_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = doctor.CHECKS

            def patlayan(_ctx):
                raise RuntimeError("çökme")

            with mock.patch.object(
                doctor, "CHECKS", ((original[0][0], patlayan),)
                if isinstance(original[0], tuple) else (patlayan,)
            ):
                checks = doctor.run_checks(root, project_root=root)
        self.assertTrue(checks)


if __name__ == "__main__":
    unittest.main()
