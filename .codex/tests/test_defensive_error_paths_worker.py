"""worker_supervisor korkuluklarının savunma dallarını sürer."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import worker_supervisor as workers
from file_lock import LockUnavailable


def _state(temporary: Path) -> Path:
    state = temporary / "state"
    workers._ensure_job_dirs(state)
    return state


class HookInputGuards(unittest.TestCase):
    def test_fence_corruption_falls_back_to_job_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            (state / workers.TREE_CLEANUP_FENCE_NAME).write_text("{bozuk", encoding="utf-8")
            error = workers._fenced_cleanup_error(state, {"owner_pid": True, "job_id": "x"}, 5.0)
        self.assertEqual(error.pid, 0)

    def test_invalid_escape_repair_and_non_dict_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hookin.json"
            path.write_text('{"a": "C:\\Users\\x", "b": "\\uZZZZ"}', encoding="utf-8")
            value = workers.load_hook_input(path)
            self.assertIn("a", value)
            path.write_text("[1]", encoding="utf-8")
            with self.assertRaises(ValueError):
                workers.load_hook_input(path)

    def test_job_timeout_kind_guards(self) -> None:
        with self.assertRaises(ValueError):
            workers._job_timeout(123)
        with self.assertRaises(ValueError):
            workers._job_timeout("bilinmez")

    def test_reference_helpers_reject_broken_payloads(self) -> None:
        self.assertIsNone(workers._hook_input_reference(None))
        self.assertIsNone(workers._hook_input_reference({"hook_input": 5}))
        self.assertEqual(workers._hook_input_references(None), set())
        self.assertIsNone(
            workers._hook_input_references({"hook_input": "x", "superseded_hook_inputs": [1]})
        )
        self.assertIsNone(
            workers._hook_input_references({"superseded_hook_inputs": "liste-degil"})
        )
        with mock.patch.object(Path, "resolve", side_effect=OSError):
            self.assertIsNone(workers._hook_input_reference({"hook_input": "x"}))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(Path, "resolve", side_effect=OSError):
                self.assertFalse(workers._managed_hook_input(state / "hookin-a.json", state))

    def test_flush_scope_and_sort_keys(self) -> None:
        self.assertIsNone(workers._flush_scope({"session_id": "", "transcript_path": "x"}))
        self.assertIsNone(workers._flush_scope({"session_id": "s", "transcript_path": ""}))
        self.assertEqual(
            workers._flush_event_sort_key({"event_iso": "bozuk"})[1], float("-inf")
        )
        key = workers._hook_input_recovery_sort_key({"event_iso": "bozuk"}, Path("a"))
        self.assertEqual(key[0], 1)

    def test_delivery_payload_schema_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            path = state / "hookin-abc.json"
            for payload in (
                {"session_id": "", "reason": "sessionend", "event_iso": "x"},
                {"session_id": "s", "reason": "bilinmez", "event_iso": "x"},
                {"session_id": "s", "reason": "sessionend", "event_iso": ""},
            ):
                path.write_text(json.dumps({"delivery_schema_version": 1, **payload}), encoding="utf-8")
                with self.subTest(payload=payload):
                    self.assertIsNone(workers._hook_input_delivery_payload(state, path))


class QueueRecordGuards(unittest.TestCase):
    def test_validate_job_type_edges(self) -> None:
        base = {
            "schema_version": 1,
            "job_id": "a" * 32,
            "kind": "maintenance",
            "status": "pending",
            "generation": 1,
            "attempt": 0,
            "enqueue_sequence": 1,
        }
        cases = [
            {**base, "generation": True},
            {**base, "attempt": 4},
            {**base, "enqueued_ts": float("inf")},
            {**base, "status": "claimed"},
        ]
        for job in cases:
            with self.subTest(job=str(job)[:60]):
                with self.assertRaises(ValueError):
                    workers._validate_job(
                        Path("pending") / f"job-{'a' * 32}.json", job
                    )

    def test_enqueue_job_argument_guards_and_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            with self.assertRaises(ValueError):
                workers.enqueue_job(state, "bilinmez", {}, start_supervisor=False)
            with self.assertRaises(ValueError):
                workers.enqueue_job(state, "maintenance", None, start_supervisor=False)
            with mock.patch.object(workers, "locked", side_effect=LockUnavailable("dolu")):
                with self.assertRaises(workers.WorkerDeliveryTimeout):
                    workers.enqueue_job(
                        state, "maintenance", {}, start_supervisor=False, deadline=0.0
                    )
                with self.assertRaises(LockUnavailable):
                    workers.enqueue_job(state, "maintenance", {}, start_supervisor=False)

    def test_quarantine_lookup_skips_corrupt_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            quarantine = state / "worker-jobs" / "quarantined"
            (quarantine / "job-bozuk.json").write_text("{kirik", encoding="utf-8")
            tombstone = {
                "schema_version": 1,
                "job_id": "b" * 32,
                "status": "quarantined",
                "reason_code": "worker-job-json-invalid",
                "payload_sha256": "c" * 64,
                "payload_file": "job-" + "b" * 32 + ".payload",
            }
            (quarantine / f"job-{'b' * 32}.json").write_text(
                json.dumps(tombstone), encoding="utf-8"
            )
            result = workers._find_hook_input_job_locked(
                state, {"hook_input": str(state / "hookin-x.json")}
            )
        self.assertIsNone(result)

    def test_pinned_successor_budget_and_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            dead = state / "worker-jobs" / "dead-letter"
            (dead / "job-bozuk.json").write_text("[1]", encoding="utf-8")
            self.assertIsNone(workers._pinned_successor_ids_locked(state))
            (dead / "job-bozuk.json").unlink()
            record = {
                "terminal_reason": "recovered-by-successor",
                "retryable": False,
                "recovery_job_id": "GEÇERSİZ!!",
            }
            (dead / "job-a.json").write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(workers._pinned_successor_ids_locked(state), set())

    def test_has_verified_successor_rejects_bad_recovery_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            job = {
                "terminal_reason": "recovered-by-successor",
                "recovery_job_id": "GEÇERSİZ!!",
            }
            self.assertFalse(
                workers._has_verified_successor(state / "worker-jobs", job)
            )

    def test_default_vault_root_falls_back_on_shallow_state(self) -> None:
        with mock.patch.object(workers, "vault_root_of", side_effect=IndexError):
            with self.assertRaises(ValueError):
                workers._default_vault_root(Path("C:/"))


class SupervisorLifecycleGuards(unittest.TestCase):
    def test_ensure_supervisor_marks_failed_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            with mock.patch.object(
                workers, "spawn_detached", side_effect=OSError("başlatılamadı")
            ):
                with self.assertRaises(OSError):
                    workers.ensure_supervisor(
                        state, vault_root=Path(temporary), launcher=mock.Mock()
                    )
            receipt = json.loads(
                workers._supervisor_receipt_path(state).read_text(encoding="utf-8")
            )
        self.assertEqual(receipt["status"], "failed")

    def test_recover_stale_jobs_skips_occupied_transition_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            job_id = "d" * 32
            record = {
                "schema_version": 1,
                "job_id": job_id,
                "kind": "maintenance",
                "status": "pending",
                "generation": 2,
                "attempt": 1,
                "enqueue_sequence": 1,
            }
            claimed = state / "worker-jobs" / "claimed" / f"job-{job_id}.json"
            claimed.write_text(json.dumps(record), encoding="utf-8")
            blocker = state / "worker-jobs" / "pending" / f"job-{job_id}.json"
            blocker.write_text(json.dumps({**record, "enqueue_sequence": 2}), encoding="utf-8")
            blocker_before = blocker.read_bytes()
            recovered = workers.recover_stale_jobs(state, now=1.0)
            blocker_after = blocker.read_bytes()
        # Hedef doluyken geçiş tamamlanmaz; bekleyen kayıt asla ezilmez.
        self.assertEqual(recovered, 0)
        self.assertEqual(blocker_before, blocker_after)

    def test_claim_skips_future_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            job_id = "e" * 32
            record = {
                "schema_version": 1,
                "job_id": job_id,
                "kind": "maintenance",
                "status": "pending",
                "generation": 1,
                "attempt": 1,
                "enqueue_sequence": 1,
                "next_attempt_ts": 10_000.0,
            }
            (state / "worker-jobs" / "pending" / f"job-{job_id}.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            claimed = workers._claim_next_job(state, now=5.0)
        self.assertIsNone(claimed)

    def test_cli_requires_claim_token_and_valid_execute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _state(Path(temporary))
            vault = Path(temporary)
            with mock.patch.object(
                workers.sys, "argv",
                ["worker", "--vault", str(vault), "--state-dir", str(state),
                 "--execute-job", str(state)],
            ):
                with self.assertRaises(ValueError):
                    workers.main()
            running = state / "worker-jobs" / "running"
            outside = Path(temporary) / "dis.json"
            outside.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                workers.sys, "argv",
                ["worker", "--vault", str(vault), "--state-dir", str(state),
                 "--claim-token", "f" * 32, "--execute-job", str(outside)],
            ):
                with self.assertRaises(ValueError):
                    workers.main()
            del running


if __name__ == "__main__":
    unittest.main()
