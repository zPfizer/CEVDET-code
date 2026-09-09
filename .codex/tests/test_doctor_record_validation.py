from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (sys.path seam; before project modules)

import doctor  # noqa: E402
import worker_supervisor  # noqa: E402


class DoctorRecordValidationTests(unittest.TestCase):
    def test_flush_invalid_json_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "flush-bad.json").write_text("{broken", encoding="utf-8")

            check = doctor._flush_inflight_check(doctor.Context(state_dir=state))

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("OK", check.evidence)

    def test_flush_ignores_producer_coverage_and_batch_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            digest = "a" * 64
            (state / f"flush-coverage-{'b' * 64}.json").write_text(
                json.dumps({"count": 1, "digest": digest}), encoding="utf-8"
            )
            (state / f"flush-batch-{'c' * 64}.json").write_text(
                json.dumps({"start": 0, "end": 1, "digest": digest}),
                encoding="utf-8",
            )
            (state / f"flush-{'d' * 64}.json").write_text(
                json.dumps({"status": "ok", "ts": 100}), encoding="utf-8"
            )

            check = doctor._flush_inflight_check(doctor.Context(state_dir=state, now=100))

            self.assertEqual(check.status, "OK")
            self.assertIn("aktif 0", check.evidence)

    def test_flush_completed_receipt_requires_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "flush-x.json").write_text(
                json.dumps({"status": "ok"}), encoding="utf-8"
            )

            check = doctor._flush_inflight_check(doctor.Context(state_dir=state, now=100))

            self.assertEqual(check.status, "FAIL")

    def test_flush_index_receipt_is_auxiliary_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "flush-index-x.json").write_text(
                json.dumps({"schema_version": 1, "rows": []}), encoding="utf-8"
            )

            check = doctor._flush_inflight_check(doctor.Context(state_dir=state, now=100))

            self.assertEqual(check.status, "OK")

    def test_hook_health_unknown_status_fails_without_echoing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "hook-health-scope.json").write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "status": "unknown",
                        "error": "secret-error-value",
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._hook_health_check(doctor.Context(state_dir=state))

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("secret-error-value", check.evidence)

    def test_hook_health_ok_requires_finite_generation_and_timestamp(self) -> None:
        for payload in (
            {"generation": True, "status": "ok", "ts": 100},
            {"generation": 1, "status": "ok", "ts": "bad"},
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                (state / "hook-health-scope.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

                check = doctor._hook_health_check(doctor.Context(state_dir=state, now=100))

                self.assertEqual(check.status, "FAIL")
                self.assertNotIn("bad", check.evidence)

    def test_hook_health_error_payload_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "hook-health-scope.json").write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "status": "error",
                        "ts": 100,
                        "error": "sk-proj-example",
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._hook_health_check(doctor.Context(state_dir=state, now=100))

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("sk-proj-example", check.evidence)

    def test_brain_health_schema_v2_rejects_non_object_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "health.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "components": {"compile:global": ["secret-error-value"]},
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._brain_health_check(doctor.Context(state_dir=state))

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("secret-error-value", check.evidence)

    def test_brain_health_schema_v2_rejects_unknown_component_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "health.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "components": {
                            "compile:global": {
                                "status": "unknown",
                                "error": "secret-error-value",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            check = doctor._brain_health_check(doctor.Context(state_dir=state))

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("secret-error-value", check.evidence)

    def test_retrieval_missing_fresh_receipt_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "WARN")
            self.assertIn("runtime kanıtı yok", check.evidence)

    def test_retrieval_health_error_payload_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "retrieval-health.json").write_text(
                json.dumps({"ts": 100, "error": "password:abc"}),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("password:abc", check.evidence)

    def test_retrieval_fresh_receipt_requires_known_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps({"ts": 100, "cwd": str(root)}), encoding="utf-8"
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")

    def test_retrieval_unknown_outcome_fails_without_echoing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps(
                    {
                        "ts": 100,
                        "cwd": str(root),
                        "outcome": "secret-outcome-value",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("secret-outcome-value", check.evidence)

    def test_retrieval_emitted_receipt_requires_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps({"ts": 100, "cwd": str(root), "outcome": "emitted"}),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")

    def test_retrieval_emitted_receipt_rejects_invalid_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps(
                    {
                        "ts": 100,
                        "cwd": str(root),
                        "outcome": "emitted",
                        "entries": 1,
                        "hits": False,
                        "emitted": -5,
                        "chars": 99999,
                        "budget": 1,
                        "paths": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("99999/1 karakter", check.evidence)

    def test_retrieval_emitted_receipt_rejects_incoherent_counts_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps(
                    {
                        "ts": 100,
                        "cwd": str(root),
                        "outcome": "emitted",
                        "entries": 1,
                        "hits": 2,
                        "emitted": 1,
                        "chars": 10,
                        "budget": 20,
                        "paths": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")

    def test_retrieval_non_emitted_receipt_rejects_impossible_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps(
                    {
                        "ts": 100,
                        "cwd": str(root),
                        "outcome": "empty",
                        "entries": 0,
                        "hits": 1,
                        "emitted": 0,
                        "chars": 0,
                        "budget": 10,
                        "paths": [],
                        "duration_ms": 1,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")

    def test_retrieval_empty_receipt_allows_hits_without_emitted_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps(
                    {
                        "ts": 100,
                        "cwd": str(root),
                        "outcome": "empty",
                        "entries": 2,
                        "hits": 1,
                        "emitted": 0,
                        "chars": 0,
                        "budget": 10,
                        "paths": [],
                        "duration_ms": 1,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "OK")
            self.assertIn("empty", check.evidence)

    def test_retrieval_empty_receipt_requires_producer_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "runtime-vault-retrieval.json").write_text(
                json.dumps({"ts": 100, "cwd": str(root), "outcome": "empty"}),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")

    def test_succeeded_worker_json_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            succeeded = state / "worker-jobs" / "succeeded"
            succeeded.mkdir(parents=True)
            (succeeded / "job-bad.json").write_text("{broken", encoding="utf-8")

            check = doctor._worker_queue_check(doctor.Context(state_dir=state, now=100))

            self.assertEqual(check.status, "FAIL")

    def test_ready_pending_with_malformed_supervisor_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            worker_supervisor.enqueue_job(
                state,
                "flush",
                {},
                start_supervisor=False,
                now=0,
            )
            (state / "worker-supervisor.json").write_text(
                "{broken", encoding="utf-8"
            )

            check = doctor._worker_delayed_job_check(
                doctor.Context(state_dir=state, now=100)
            )

            self.assertEqual(check.status, "FAIL")

    def test_supervisor_idle_receipt_requires_producer_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "worker-supervisor.json").write_text(
                json.dumps({"status": "idle", "error": "secret-error-value"}),
                encoding="utf-8",
            )

            check = doctor._worker_delayed_job_check(
                doctor.Context(state_dir=state, now=100)
            )

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("secret-error-value", check.evidence)


if __name__ == "__main__":
    unittest.main()
