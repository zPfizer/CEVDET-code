from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (sys.path seam; before project modules)

import doctor  # noqa: E402
import flush  # noqa: E402
import hook  # noqa: E402
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
            transcript_digest = "d" * 64
            summary_digest = "e" * 64
            flush._write_flush_state(
                state,
                "valid-session",
                100,
                "ok",
                reason="turnend",
                transcript_digest=transcript_digest,
                summary_digest=summary_digest,
                idempotency_key=flush._flush_idempotency_key(
                    "valid-session",
                    "turnend",
                    transcript_digest,
                    summary_digest,
                ),
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

    def test_flush_valid_writer_receipt_requires_each_top_level_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            transcript_digest = "b" * 64
            summary_digest = "c" * 64
            idempotency_key = flush._flush_idempotency_key(
                "valid-session",
                "turnend",
                transcript_digest,
                summary_digest,
            )
            flush._write_flush_state(
                state,
                "valid-session",
                100,
                "ok",
                reason="turnend",
                transcript_digest=transcript_digest,
                summary_digest=summary_digest,
                idempotency_key=idempotency_key,
            )
            path = flush._session_state_path(state, "valid-session")
            valid = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                doctor._flush_inflight_check(doctor.Context(state_dir=state, now=100)).status,
                "OK",
            )
            prepared_state = state / "prepared"
            prepared_state.mkdir()
            flush._write_flush_state(
                prepared_state,
                "prepared-session",
                100,
                "prepared",
                "ready-to-append",
                reason="turnend",
                transcript_digest=transcript_digest,
                summary_digest=summary_digest,
                idempotency_key=flush._flush_idempotency_key(
                    "prepared-session",
                    "turnend",
                    transcript_digest,
                    summary_digest,
                ),
                daily_file="2026-09-09.md",
                event_iso="2026-09-09T12:00:00+03:00",
                batch_start=0,
                batch_end=1,
            )
            self.assertEqual(
                doctor._flush_inflight_check(
                    doctor.Context(state_dir=prepared_state, now=100)
                ).status,
                "OK",
            )

            for field in (
                "session_key",
                "ts",
                "status",
                "generation",
                "schema_version",
                "receipts",
                "idempotency_key",
                "transcript_digest",
                "summary_digest",
                "reason",
            ):
                with self.subTest(field=field):
                    malformed = dict(valid)
                    malformed.pop(field)
                    path.write_text(json.dumps(malformed), encoding="utf-8")
                    self.assertEqual(
                        doctor._flush_inflight_check(
                            doctor.Context(state_dir=state, now=100)
                        ).status,
                        "FAIL",
                    )

            for field, value in (
                ("session_key", "0" * 64),
                ("ts", "bad"),
                ("generation", True),
                ("schema_version", 1),
                ("receipts", {"a" * 64: []}),
                ("idempotency_key", "0" * 64),
                ("transcript_digest", "bad"),
                ("summary_digest", "bad"),
                ("reason", "bad"),
            ):
                with self.subTest(field=field, value=value):
                    malformed = dict(valid)
                    malformed[field] = value
                    path.write_text(json.dumps(malformed), encoding="utf-8")
                    self.assertEqual(
                        doctor._flush_inflight_check(
                            doctor.Context(state_dir=state, now=100)
                        ).status,
                        "FAIL",
                    )

            noop_state = state / "noop"
            noop_state.mkdir()
            flush._write_flush_state(
                noop_state,
                "noop-session",
                100,
                "ok",
                "below-minimum-turns",
                reason="turnend",
                transcript_digest=transcript_digest,
                summary_digest=summary_digest,
                idempotency_key=flush._flush_idempotency_key(
                    "noop-session",
                    "turnend",
                    transcript_digest,
                    summary_digest,
                ),
            )
            noop_path = flush._session_state_path(noop_state, "noop-session")
            noop = json.loads(noop_path.read_text(encoding="utf-8"))
            for field in ("idempotency_key", "transcript_digest", "summary_digest"):
                noop.pop(field)
            noop_path.write_text(json.dumps(noop), encoding="utf-8")
            self.assertEqual(
                doctor._flush_inflight_check(
                    doctor.Context(state_dir=noop_state, now=100)
                ).status,
                "OK",
            )

            receipt_key, receipt_item = next(iter(valid["receipts"].items()))
            for field in ("idempotency_key", "transcript_digest", "summary_digest"):
                with self.subTest(receipt_field=field):
                    malformed_item = dict(receipt_item)
                    malformed_item.pop(field)
                    malformed = dict(valid)
                    malformed["receipts"] = {receipt_key: malformed_item}
                    path.write_text(json.dumps(malformed), encoding="utf-8")
                    self.assertEqual(
                        doctor._flush_inflight_check(
                            doctor.Context(state_dir=state, now=100)
                        ).status,
                        "FAIL",
                    )

            for receipts in (
                {"0" * 64: receipt_item},
                {receipt_key: {**receipt_item, "idempotency_key": "0" * 64}},
                {receipt_key: {**receipt_item, "transcript_digest": "bad"}},
                {receipt_key: {**receipt_item, "summary_digest": "bad"}},
                {receipt_key: {**receipt_item, "status": "inflight"}},
            ):
                with self.subTest(receipts=receipts):
                    malformed = dict(valid)
                    malformed["receipts"] = receipts
                    path.write_text(json.dumps(malformed), encoding="utf-8")
                    self.assertEqual(
                        doctor._flush_inflight_check(
                            doctor.Context(state_dir=state, now=100)
                        ).status,
                        "FAIL",
                    )

    def test_hook_valid_writer_receipt_requires_each_scoped_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            hook.write_hook_health(
                state,
                {"session_id": "valid-session"},
                status="ok",
            )
            path = next(state.glob("hook-health-*.json"))
            valid = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                doctor._hook_health_check(
                    doctor.Context(state_dir=state, now=valid["ts"])
                ).status,
                "OK",
            )

            for field in (
                "schema_version",
                "ts",
                "component",
                "session_key",
                "generation",
                "status",
            ):
                with self.subTest(field=field):
                    malformed = dict(valid)
                    malformed.pop(field)
                    path.write_text(json.dumps(malformed), encoding="utf-8")
                    self.assertEqual(
                        doctor._hook_health_check(
                            doctor.Context(state_dir=state, now=valid["ts"])
                        ).status,
                        "FAIL",
                    )

            for field, value in (
                ("schema_version", 1),
                ("ts", "bad"),
                ("component", "other"),
                ("session_key", "0" * 64),
                ("generation", True),
            ):
                with self.subTest(field=field, value=value):
                    malformed = dict(valid)
                    malformed[field] = value
                    path.write_text(json.dumps(malformed), encoding="utf-8")
                    self.assertEqual(
                        doctor._hook_health_check(
                            doctor.Context(state_dir=state, now=valid["ts"])
                        ).status,
                        "FAIL",
                    )

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
            hook.write_hook_health(
                state,
                {"session_id": "redaction-session"},
                status="error",
                error="profile-secret-password-abc",
            )
            receipt = json.loads(next(state.glob("hook-health-*.json")).read_text())

            check = doctor._hook_health_check(
                doctor.Context(state_dir=state, now=receipt["ts"])
            )

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("profile-secret-password-abc", check.evidence)
            self.assertIn("runtime-error-recorded", check.evidence)

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
                json.dumps({"ts": 100, "error": "profile-secret-password-abc"}),
                encoding="utf-8",
            )
            with mock.patch.object(doctor, "build_vault_map", return_value=[object()]):
                check = doctor._vault_retrieval_check(
                    doctor.Context(root, root, state, 100)
                )

            self.assertEqual(check.status, "FAIL")
            self.assertNotIn("profile-secret-password-abc", check.evidence)
            self.assertEqual(check.evidence, "runtime-error-recorded")

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
