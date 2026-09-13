"""worker_supervisor — dilim 2: kuyruk/koruma/dağıtım kollarını kapatır."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
import uuid

from _fixtures import CODEX_DIR  # noqa: F401
import worker_supervisor as workers
from file_lock import LockUnavailable, locked

TOKEN = "f" * 32


def _state(temporary: Path) -> Path:
    state = temporary / "state"
    workers._ensure_job_dirs(state)
    return state


def _job(state_dir: Path, status: str, kind: str = "flush", **overrides) -> Path:
    job_id = uuid.uuid4().hex
    job = {
        "schema_version": 1, "job_id": job_id, "kind": kind, "status": status,
        "generation": 1, "attempt": 0, "enqueue_sequence": 1, "payload": {},
    }
    if status in {"claimed", "running"}:
        job.update(attempt=1, claim_token=TOKEN, owner_pid=os.getpid(),
                   lease_until=10.0 ** 10)
    if status in {"succeeded", "dead-letter"}:
        job["finished_ts"] = 100
    job.update(overrides)
    path = workers._job_root(state_dir) / status / f"job-{job_id}.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    return path


def _tombstone(state_dir: Path, payload: object, *, tamper: bool = False) -> Path:
    job_id = uuid.uuid4().hex
    quarantine = workers._job_root(state_dir) / "quarantined"
    payload_bytes = json.dumps(payload).encode("utf-8")
    payload_path = quarantine / f"job-{job_id}.payload"
    payload_path.write_bytes(payload_bytes)
    import hashlib

    digest = hashlib.sha256(payload_bytes).hexdigest()
    if tamper:
        payload_path.write_bytes(b"bozuk")
    tombstone = quarantine / f"job-{job_id}.json"
    tombstone.write_text(
        json.dumps({
            "schema_version": 1, "job_id": job_id, "status": "quarantined",
            "reason_code": "worker-job-json-invalid",
            "payload_sha256": digest, "payload_file": payload_path.name,
        }),
        encoding="utf-8",
    )
    return tombstone


def _transport(state_dir: Path, name: str, value) -> Path:
    path = state_dir / name
    path.write_text(
        value if isinstance(value, str) else json.dumps(value),
        encoding="utf-8",
    )
    return path


class OwnerIdentityArms(unittest.TestCase):
    def test_identity_reader_shape_and_failure(self) -> None:
        with mock.patch.object(
            workers.process_control, "process_identity", "değil", create=True
        ):
            self.assertIsNone(workers._process_owner_identity(1))
        with mock.patch.object(
            workers.process_control, "process_identity",
            mock.Mock(side_effect=ValueError), create=True,
        ):
            self.assertIsNone(workers._process_owner_identity(1))

    def test_finite_number_overflow(self) -> None:
        self.assertFalse(workers._finite_number(10 ** 400))


class ReferenceArms(unittest.TestCase):
    def test_reference_resolution_failures(self) -> None:
        with mock.patch.object(Path, "resolve", side_effect=OSError):
            self.assertIsNone(
                workers._hook_input_references({"hook_input": "x"})
            )
            self.assertIsNone(
                workers._hook_input_references(
                    {"superseded_hook_inputs": ["x"]}
                )
            )


class FindJobArms(unittest.TestCase):
    def test_quarantined_payload_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            target = str(Path(temporary) / "hedef.json")
            _tombstone(state, {"payload": {"hook_input": target}})
            found = workers._find_hook_input_job_locked(
                state, {"hook_input": target}
            )
        self.assertIsNotNone(found)
        self.assertEqual(found[1], "quarantined")

    def test_pending_corruption_and_invalid_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            target = str(Path(temporary) / "hedef.json")
            pending = workers._job_root(state) / "pending"

            (pending / "job-a.json").write_text("{kirik", encoding="utf-8")
            (pending / "job-b.json").write_text("[]", encoding="utf-8")
            self.assertIsNone(
                workers._find_hook_input_job_locked(
                    state, {"hook_input": target}
                )
            )

            # Durum alanı geçersiz: hedefe referanslıysa invalid döner.
            (pending / "job-c.json").write_text(
                json.dumps({"status": "bilinmez",
                            "payload": {"hook_input": target}}),
                encoding="utf-8",
            )
            found = workers._find_hook_input_job_locked(
                state, {"hook_input": target}
            )
            self.assertEqual(found[1], "invalid")

            (pending / "job-c.json").write_text(
                json.dumps({"status": "bilinmez",
                            "payload": {"hook_input": str(
                                Path(temporary) / "baska.json")}}),
                encoding="utf-8",
            )
            self.assertIsNone(
                workers._find_hook_input_job_locked(
                    state, {"hook_input": target}
                )
            )

    def test_schema_invalid_record_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            target = str(Path(temporary) / "hedef.json")
            pending = workers._job_root(state) / "pending"

            (pending / "job-d.json").write_text(
                json.dumps({"status": "pending",
                            "payload": {"hook_input": target}}),
                encoding="utf-8",
            )
            found = workers._find_hook_input_job_locked(
                state, {"hook_input": target}
            )
            self.assertEqual(found[1], "invalid")

            (pending / "job-d.json").write_text(
                json.dumps({"status": "pending",
                            "payload": {"hook_input": str(
                                Path(temporary) / "baska.json")}}),
                encoding="utf-8",
            )
            self.assertIsNone(
                workers._find_hook_input_job_locked(
                    state, {"hook_input": target}
                )
            )


class ReferenceSetArms(unittest.TestCase):
    def test_referenced_inputs_reject_bad_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            _job(state, "pending",
                 payload={"superseded_hook_inputs": "bozuk"})
            self.assertIsNone(workers._referenced_hook_inputs_locked(state))

    def test_quarantine_reference_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            tombstone = _tombstone(
                state, {"payload": {"superseded_hook_inputs": 5}}
            )
            self.assertIsNone(workers._referenced_hook_inputs_locked(state))
            self.assertEqual(
                workers._referenced_hook_inputs_locked(
                    state,
                    excluded_paths={tombstone.resolve(strict=False)},
                ),
                set(),
            )

    def test_succeeded_inputs_reject_bad_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            _job(state, "succeeded",
                 payload={"superseded_hook_inputs": "bozuk"})
            self.assertIsNone(workers._succeeded_hook_inputs_locked(state))


class SweepArms(unittest.TestCase):
    def test_missing_state_dir_returns(self) -> None:
        workers._sweep_stale_hook_inputs(Path("yok-boyle-dizin"), 0.0)

    def test_unlink_failure_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            transport = _transport(state, "hookin-eski.json", "{kirik")
            os.utime(transport, (1, 1))
            with mock.patch.object(Path, "unlink", side_effect=OSError):
                workers._sweep_stale_hook_inputs(state, 10.0 ** 9)
            self.assertTrue(transport.exists())


class ValidateJobArms(unittest.TestCase):
    def test_shape_and_state_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                workers._validate_job(root / "pending" / "job-a.json", "değil")
            with self.assertRaises(ValueError):
                workers._validate_job(root / "bilinmez" / "job-a.json", {})

            job_id = "a" * 32
            base = {
                "schema_version": 1, "job_id": job_id, "kind": "flush",
                "generation": 1, "attempt": 1, "enqueue_sequence": 1,
                "payload": {},
            }
            claimed = dict(base, status="claimed")
            with self.assertRaises(ValueError):
                workers._validate_job(
                    root / "claimed" / f"job-{job_id}.json", claimed
                )
            succeeded = dict(base, status="succeeded", finished_ts=100.5)
            with self.assertRaises(ValueError):
                workers._validate_job(
                    root / "succeeded" / f"job-{job_id}.json", succeeded
                )


class FlushMergeArms(unittest.TestCase):
    def test_flush_scope_bad_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transport = Path(temporary) / "hookin-a.json"
            transport.write_text("[1]", encoding="utf-8")
            self.assertIsNone(
                workers._flush_scope(
                    {"payload": {"hook_input": str(transport)}}
                )
            )

    def test_naive_event_iso_sort_keys(self) -> None:
        kind, _ts, _name = workers._flush_event_sort_key(
            {"event_iso": "2026-09-11T10:00:00"}
        )
        self.assertEqual(kind, 0)
        kind, _ts, _name = workers._hook_input_recovery_sort_key(
            {"event_iso": "2026-09-11T10:00:00"}, Path("hookin-a.json")
        )
        self.assertEqual(kind, 0)

    def test_merge_superseded_union(self) -> None:
        merged = workers._merge_flush_payload(
            {"superseded_hook_inputs": ["a"]}, {}
        )
        self.assertEqual(merged["superseded_hook_inputs"], ["a"])


class CoalesceArms(unittest.TestCase):
    def _scope_transport(self, temporary: Path, state: Path) -> Path:
        transcript = temporary / "kayit.jsonl"
        transcript.write_text("satır", encoding="utf-8")
        return _transport(
            state, "hookin-kapsam.json",
            {"session_id": "s1", "transcript_path": str(transcript)},
        )

    def test_non_flush_pending_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            transport = self._scope_transport(Path(temporary), state)
            _job(state, "pending", kind="maintenance")
            self.assertIsNone(
                workers._coalesce_pending_flush_locked(
                    state, {"hook_input": str(transport)}, now=100.0
                )
            )

    def test_same_input_drops_superseded_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            transport = self._scope_transport(Path(temporary), state)
            retained = _job(
                state, "pending",
                payload={"hook_input": str(transport),
                         "superseded_hook_inputs": [str(transport)]},
            )
            result = workers._coalesce_pending_flush_locked(
                state, {"hook_input": str(transport)}, now=100.0
            )
            self.assertEqual(result, retained)
            value = json.loads(retained.read_text(encoding="utf-8"))
        self.assertNotIn("superseded_hook_inputs", value["payload"])

    def test_duplicate_unlink_failure_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            transport = self._scope_transport(Path(temporary), state)
            _job(state, "pending", payload={"hook_input": str(transport)})
            _job(state, "pending", enqueue_sequence=2,
                 payload={"hook_input": str(transport)})
            original_unlink = Path.unlink

            def secici(self, *args, **kwargs):
                if self.name.startswith("job-") and self.suffix == ".json":
                    raise OSError("kilitli")
                return original_unlink(self, *args, **kwargs)

            with mock.patch.object(Path, "unlink", secici):
                result = workers._coalesce_pending_flush_locked(
                    state, {"hook_input": str(transport)}, now=100.0
                )
            self.assertIsNotNone(result)


class EnqueueArms(unittest.TestCase):
    def test_terminal_existing_and_missing_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            target = str(Path(temporary) / "hedef.json")
            _job(state, "dead-letter", payload={"hook_input": target})
            with self.assertRaises(workers.WorkerDeliveryTerminal):
                workers._enqueue_job_locked(
                    state, "flush", {"hook_input": target},
                    observed_now=100.0,
                )

        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            missing = str(state / "hookin-yok.json")
            with self.assertRaises(workers.WorkerDeliveryTerminal):
                workers._enqueue_job_locked(
                    state, "flush", {"hook_input": missing},
                    observed_now=100.0,
                )

    def test_enqueue_job_propagates_lock_without_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            with mock.patch.object(
                workers, "locked", side_effect=LockUnavailable("kilit")
            ):
                with self.assertRaises(LockUnavailable):
                    workers.enqueue_job(
                        state, "flush", {}, start_supervisor=False
                    )
            with mock.patch.object(
                workers, "ensure_supervisor",
                side_effect=LockUnavailable("kabul"),
            ):
                with self.assertRaises(LockUnavailable):
                    workers.enqueue_job(state, "flush", {})


class DeliveryPayloadArms(unittest.TestCase):
    def test_unmanaged_and_unreadable_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            outside = Path(temporary) / "hookin-dis.json"
            outside.write_text("{}", encoding="utf-8")
            self.assertIsNone(
                workers._hook_input_delivery_payload(state, outside)
            )
            broken = _transport(state, "hookin-bozuk.json", "{kirik")
            self.assertIsNone(
                workers._hook_input_delivery_payload(state, broken)
            )

    def test_continuation_fields_are_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            transport = _transport(state, "hookin-devam.json", {
                "delivery_schema_version": 1, "session_id": "s1",
                "reason": "turnend",
                "event_iso": "2026-09-11T10:00:00+00:00",
                "coverage": 3,
            })
            payload = workers._hook_input_delivery_payload(state, transport)
        self.assertEqual(payload["coverage"], 3)


class OrphanArms(unittest.TestCase):
    def test_recover_skips_payload_that_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            _transport(state, "hookin-a.json", {})
            payload = {"hook_input": "x", "reason": "turnend",
                       "event_iso": "2026-09-11T10:00:00+00:00"}
            with mock.patch.object(
                workers, "_hook_input_delivery_payload",
                side_effect=[payload, None],
            ):
                recovered = workers._recover_orphan_hook_inputs_locked(
                    state, now=100.0
                )
        self.assertEqual(recovered, 0)

    def test_count_orphans_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            _transport(state, "hookin-bozuk.json", "{kirik")
            self.assertEqual(workers.count_orphan_hook_inputs(state), 0)
            _transport(state, "hookin-tam.json", {
                "delivery_schema_version": 1, "session_id": "s1",
                "reason": "turnend",
                "event_iso": "2026-09-11T10:00:00+00:00",
            })
            self.assertEqual(workers.count_orphan_hook_inputs(state), 1)


class PruneArms(unittest.TestCase):
    def test_pinned_budget_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            dead = workers._job_root(state) / "dead-letter"
            (dead / "job-a.json").write_text("{kirik", encoding="utf-8")
            self.assertIsNone(workers._pinned_successor_ids_locked(state))
            workers._prune_succeeded_jobs_locked(state, 100.0)

            (dead / "job-a.json").write_text(
                json.dumps({
                    "terminal_reason": "recovered-by-successor",
                    "retryable": False, "recovery_job_id": "abc",
                }),
                encoding="utf-8",
            )
            with mock.patch.object(
                workers, "MAX_PINNED_SUCCESSOR_RECEIPTS", 0
            ):
                self.assertIsNone(
                    workers._pinned_successor_ids_locked(state)
                )

    def test_prune_receipt_race_and_unlink_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            path = _job(state, "succeeded")
            valid = json.loads(path.read_text(encoding="utf-8"))
            with mock.patch.object(
                workers, "_load_job",
                side_effect=[valid, ValueError("worker-job-schema-invalid")],
            ):
                workers._prune_succeeded_jobs_locked(state, 100.0)
            self.assertTrue(path.exists())

            with (
                mock.patch.object(workers, "MAX_SUCCEEDED_RECEIPTS", 0),
                mock.patch.object(Path, "unlink", side_effect=OSError),
            ):
                workers._prune_succeeded_jobs_locked(state, 100.0)
            self.assertTrue(path.exists())

    def test_cleanup_skips_unmanaged_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            outside = Path(temporary) / "hookin-dis.json"
            outside.write_text("{}", encoding="utf-8")
            _job(state, "succeeded", payload={"hook_input": str(outside)})
            cleaned = workers._cleanup_succeeded_hook_inputs_locked(state)
            self.assertEqual(cleaned, 1)
            self.assertTrue(outside.exists())


class MigrationAndInspectionArms(unittest.TestCase):
    def test_legacy_records_must_be_wellformed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            failed = workers._job_root(state) / "failed"
            failed.mkdir()
            source = failed / f"job-{'a' * 32}.json"

            source.write_text("{kirik", encoding="utf-8")
            with self.assertRaises(ValueError):
                workers.migrate_legacy_failed_jobs(state, now=100.0)

            source.write_text(
                json.dumps({"job_id": "a" * 32, "status": "pending",
                            "kind": "flush", "payload": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                workers.migrate_legacy_failed_jobs(state, now=100.0)

    def test_has_verified_successor_rejects_bad_id_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(
                workers._has_verified_successor(
                    Path(temporary),
                    {"terminal_reason": "recovered-by-successor",
                     "retryable": False, "recovery_job_id": 5},
                )
            )

    def test_inspect_flags_tampered_quarantine_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            _tombstone(state, {"payload": {}}, tamper=True)
            report = workers.inspect_worker_queue(state)
        self.assertEqual(report["status"], "error")


class RecoveryAndFinishArms(unittest.TestCase):
    def test_terminal_maintenance_health_failure_does_not_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            health = state.parent / "health.json"
            health.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "generation": None,
                        "components": {
                            "flush:global": {
                                "status": "error",
                                "error": "flush-eski",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            before = health.read_bytes()

            workers._report_terminal_maintenance(
                state,
                {
                    "kind": "maintenance",
                    "status": "dead-letter",
                    "last_error": "worker-failed",
                },
            )

            self.assertEqual(health.read_bytes(), before)

    def test_transition_target_occupied_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            kaynak = _job(state, "running")
            moving = workers._job_root(state) / "claimed" / kaynak.name
            kaynak.replace(moving)
            blocker = (
                workers._job_root(state) / "running" / moving.name
            )
            engel = json.loads(moving.read_text(encoding="utf-8"))
            blocker.write_text(json.dumps(engel), encoding="utf-8")
            önce = blocker.read_text(encoding="utf-8")
            workers.recover_stale_jobs(state, now=100.0)
            self.assertEqual(blocker.read_text(encoding="utf-8"), önce)

    def test_retry_delay_base_guard(self) -> None:
        with self.assertRaises(ValueError):
            workers._retry_delay(1, 0)

    def test_finish_claim_drift_and_failures_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            running = _job(state, "running")
            job = json.loads(running.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                workers._finish_job(
                    state, running, dict(job, claim_token="e" * 32),
                    status="failed", now=100.0,
                )

        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            running = _job(state, "running", failures="bozuk")
            job = json.loads(running.read_text(encoding="utf-8"))
            workers._finish_job(state, running, job, status="failed",
                                now=100.0)
            pending = list(
                (workers._job_root(state) / "pending").glob("*.json")
            )
            value = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(len(value["failures"]), 1)


class DispatchArms(unittest.TestCase):
    def test_payload_and_kind_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                workers._dispatch_job(root, root, {"payload": 5})
            with self.assertRaises(ValueError):
                workers._dispatch_job(
                    root, root, {"kind": "bilinmez", "payload": {}}
                )
            with self.assertRaises(ValueError):
                workers._dispatch_job(
                    root, root,
                    {"kind": "flush", "payload": {"hook_input": ""}},
                )
            with self.assertRaises(workers._UnrecoverableWorkerInput):
                workers._dispatch_job(
                    root, root,
                    {"kind": "flush", "payload": {
                        "hook_input": str(root / "yok.json"),
                        "reason": "turnend",
                        "event_iso": "2026-09-11T10:00:00+00:00",
                    }},
                )

    def test_flush_file_not_found_classification(self) -> None:
        import flush

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "kayit.jsonl"
            transcript.write_text("satır", encoding="utf-8")
            transport = root / "hookin-a.json"
            transport.write_text(
                json.dumps({"session_id": "s1",
                            "transcript_path": str(transcript)}),
                encoding="utf-8",
            )
            job = {"kind": "flush", "payload": {
                "hook_input": str(transport), "reason": "turnend",
                "event_iso": "2026-09-11T10:00:00+00:00",
            }}

            with mock.patch.object(
                flush, "flush_once", side_effect=FileNotFoundError("gecici")
            ):
                with self.assertRaises(FileNotFoundError):
                    workers._dispatch_job(root, root, job)

            def kaybol(*_a, **_k):
                transcript.unlink()
                raise FileNotFoundError("kalici")

            with mock.patch.object(flush, "flush_once", side_effect=kaybol):
                with self.assertRaises(workers._UnrecoverableWorkerInput):
                    workers._dispatch_job(root, root, job)


class AdoptAndRunArms(unittest.TestCase):
    def test_adopt_fence_and_non_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            fence = state / workers.TREE_CLEANUP_FENCE_NAME
            fence.write_text("{}", encoding="utf-8")
            pending = _job(state, "pending")
            with self.assertRaises(ValueError):
                workers._adopt_job(state, pending, now=100.0)
            fence.unlink()
            with self.assertRaises(ValueError):
                workers._adopt_job(state, pending, now=100.0)

    def test_run_claimed_job_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            pending = _job(state, "pending")
            with self.assertRaises(ValueError):
                workers._run_claimed_job(Path(temporary), state, pending)

            running = _job(state, "running")
            child = types.SimpleNamespace(returncode=2)
            with mock.patch.object(
                workers, "run_with_tree_timeout", return_value=child
            ):
                with self.assertRaises(RuntimeError):
                    workers._run_claimed_job(Path(temporary), state, running)

    def test_has_pending_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            self.assertFalse(workers._has_pending(state))

    def test_settle_with_unreadable_receipt_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            receipt = state / "worker-supervisor.json"
            receipt.write_text("{kirik", encoding="utf-8")
            self.assertTrue(
                workers._settle_supervisor(
                    state, receipt, owner_pid=os.getpid(), now=100.0
                )
            )


class SupervisorLoopArms(unittest.TestCase):
    def test_lifetime_lock_contention_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            with locked(state / "worker-supervisor"):
                self.assertEqual(
                    workers.run_supervisor(Path(temporary), state), 0
                )

    def test_generic_job_failure_reaches_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            _job(state, "pending")
            clock = {"value": 1000.0}

            def now() -> float:
                clock["value"] += 50.0
                return clock["value"]

            with mock.patch.object(workers.time, "sleep"):
                result = workers.run_supervisor(
                    Path(temporary), state, now=now,
                    job_runner=mock.Mock(side_effect=RuntimeError("patla")),
                )
            dead = list(
                (workers._job_root(state) / "dead-letter").glob("*.json")
            )
            self.assertEqual(result, 0)
            self.assertEqual(len(dead), 1)
            value = json.loads(dead[0].read_text(encoding="utf-8"))
        self.assertEqual(value["last_error"], "RuntimeError")

    def test_main_execute_and_supervisor_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            running = _job(state, "running")
            argv = [
                "worker_supervisor.py", "--vault", str(temporary),
                "--state-dir", str(state),
                "--execute-job", str(running),
                "--claim-token", TOKEN,
            ]
            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(
                    workers, "execute_job_file", mock.Mock(return_value=0)
                ) as execute,
            ):
                self.assertEqual(workers.main(), 0)
            self.assertTrue(execute.called)

            argv = [
                "worker_supervisor.py", "--vault", str(temporary),
                "--state-dir", str(state),
            ]
            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(
                    workers, "run_supervisor", mock.Mock(return_value=0)
                ) as run,
            ):
                self.assertEqual(workers.main(), 0)
            self.assertTrue(run.called)


if __name__ == "__main__":
    unittest.main()
