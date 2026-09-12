import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import worker_supervisor as workers


def _dead_letter_job(state: Path, job_id: str, **overrides) -> Path:
    record = {
        "schema_version": 1,
        "job_id": job_id,
        "kind": "maintenance",
        "status": "dead-letter",
        "generation": 2,
        "attempt": 3,
        "enqueue_sequence": 1,
        "enqueued_ts": 1757400000,
        "finished_ts": 1757400100,
        "last_error": "worker-lease-expired",
        "terminal_reason": "retry-exhausted",
        "retryable": False,
    }
    record.update(overrides)
    path = state / "worker-jobs" / "dead-letter" / f"job-{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _succeeded_job(state: Path, job_id: str, **overrides) -> Path:
    record = {
        "schema_version": 1,
        "job_id": job_id,
        "kind": "maintenance",
        "status": "succeeded",
        "generation": 4,
        "attempt": 1,
        "enqueue_sequence": 2,
        "enqueued_ts": 1757400000,
        "finished_ts": 1757400200,
        "payload": {},
    }
    record.update(overrides)
    path = state / "worker-jobs" / "succeeded" / f"job-{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class RedriveDeadLetterTests(unittest.TestCase):
    def test_interrupted_redrive_is_completed_by_queue_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "c" * 32
            dead_letter = _dead_letter_job(state, job_id, payload={"reason": "kesinti"})
            real_replace = workers.os.replace

            def fail_transition(source: Path, destination: Path) -> None:
                if (
                    source.parent.name == "dead-letter"
                    and destination.parent.name == "pending"
                ):
                    raise OSError("simulated interruption")
                real_replace(source, destination)

            with mock.patch.object(workers.os, "replace", side_effect=fail_transition):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    workers.redrive_dead_letter(state, now=1757500000)

            self.assertTrue(dead_letter.is_file())
            interrupted = json.loads(dead_letter.read_text(encoding="utf-8"))
            self.assertEqual(interrupted["status"], "pending")

            workers.recover_stale_jobs(state, now=1757500001)
            pending = state / "worker-jobs" / "pending" / dead_letter.name
            recovered = workers._load_job(pending)
            self.assertFalse(dead_letter.is_file())

        self.assertEqual(recovered["job_id"], job_id)
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(recovered["generation"], 3)
        self.assertEqual(recovered["attempt"], 0)

    def test_redrive_revives_job_as_valid_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "a" * 32
            _dead_letter_job(state, job_id, payload={"reason": "bakım"})

            redriven, skipped = workers.redrive_dead_letter(state, now=1757500000)

            pending = state / "worker-jobs" / "pending" / f"job-{job_id}.json"
            revived = workers._load_job(pending)

        self.assertEqual(redriven, [job_id])
        self.assertEqual(skipped, [])
        self.assertEqual(revived["status"], "pending")
        self.assertEqual(revived["attempt"], 0)
        self.assertEqual(revived["generation"], 3)
        self.assertEqual(revived["redriven_ts"], 1757500000)
        for field in ("terminal_reason", "retryable", "last_error", "finished_ts"):
            self.assertNotIn(field, revived)

    def test_recovered_by_successor_is_not_redriven(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "b" * 32
            successor_id = "c" * 32
            _succeeded_job(state, successor_id, payload={"reason": "same"})
            original = _dead_letter_job(
                state,
                job_id,
                payload={"reason": "same"},
                terminal_reason="recovered-by-successor",
                recovery_job_id=successor_id,
            )

            redriven, skipped = workers.redrive_dead_letter(state)
            still_there = original.is_file()

        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [(job_id, "zaten kurtarılmış")])
        self.assertTrue(still_there)

    def test_unverified_successor_marker_is_redriven(self) -> None:
        for variant in ("missing", "corrupt", "pending", "different_payload"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                job_id = "b" * 32
                successor_id = "c" * 32
                _dead_letter_job(
                    state,
                    job_id,
                    payload={"reason": "original"},
                    terminal_reason="recovered-by-successor",
                    recovery_job_id=successor_id,
                )
                if variant == "corrupt":
                    successor = state / "worker-jobs" / "succeeded" / f"job-{successor_id}.json"
                    successor.parent.mkdir(parents=True, exist_ok=True)
                    successor.write_text("{bozuk", encoding="utf-8")
                elif variant == "pending":
                    successor = state / "worker-jobs" / "pending" / f"job-{successor_id}.json"
                    successor.parent.mkdir(parents=True, exist_ok=True)
                    successor.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "job_id": successor_id,
                                "kind": "maintenance",
                                "status": "pending",
                                "generation": 4,
                                "attempt": 0,
                                "enqueue_sequence": 2,
                                "enqueued_ts": 1757400000,
                                "payload": {"reason": "original"},
                            }
                        ),
                        encoding="utf-8",
                    )
                elif variant == "different_payload":
                    _succeeded_job(state, successor_id, payload={"reason": "different"})

                redriven, skipped = workers.redrive_dead_letter(state, now=1757500000)
                pending = state / "worker-jobs" / "pending" / f"job-{job_id}.json"
                revived = workers._load_job(pending)

            self.assertEqual(redriven, [job_id])
            self.assertEqual(skipped, [])
            self.assertEqual(revived["status"], "pending")
            self.assertNotIn("terminal_reason", revived)

    def test_cli_redrive_wakes_supervisor_for_redriven_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            _dead_letter_job(state, job_id, payload={"reason": "wake"})
            with (
                mock.patch.object(
                    workers.sys,
                    "argv",
                    [
                        "worker_supervisor.py",
                        "--vault",
                        str(vault),
                        "--state-dir",
                        str(state),
                        "--redrive",
                    ],
                ),
                mock.patch.object(workers, "ensure_supervisor") as wake,
            ):
                result = workers.main()

        self.assertEqual(result, 0)
        wake.assert_called_once_with(state, vault_root=vault)

    def test_cli_targeted_missing_job_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            missing_id = "a" * 32
            with mock.patch.object(
                workers.sys,
                "argv",
                [
                    "worker_supervisor.py",
                    "--vault",
                    str(vault),
                    "--state-dir",
                    str(state),
                    "--redrive",
                    missing_id,
                ],
            ):
                result = workers.main()

        self.assertEqual(result, 1)

    def test_flush_job_with_deleted_managed_input_is_skipped(self) -> None:
        # Yönetilen hookin girdisi silinmişse iş koşamaz; sessiz düşmek
        # yerine kayıt dead-letter'da kalıp nedeni raporlanmalı.
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "d" * 32
            missing_input = state / "hookin-silinmis.json"
            original = _dead_letter_job(
                state,
                job_id,
                kind="flush",
                payload={"hook_input": str(missing_input)},
            )

            redriven, skipped = workers.redrive_dead_letter(state)
            still_there = original.is_file()

        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [(job_id, "girdi dosyası silinmiş")])
        self.assertTrue(still_there)

    def test_targeted_redrive_leaves_other_jobs_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            wanted, other = "e" * 32, "f" * 32
            _dead_letter_job(state, wanted, payload={})
            untouched = _dead_letter_job(state, other, payload={})

            redriven, skipped = workers.redrive_dead_letter(state, job_id=wanted)

            pending = state / "worker-jobs" / "pending" / f"job-{wanted}.json"
            moved = pending.is_file()
            other_stayed = untouched.is_file()

        self.assertEqual(redriven, [wanted])
        self.assertEqual(skipped, [])
        self.assertTrue(moved)
        self.assertTrue(other_stayed)

    def test_existing_pending_record_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "f" * 32
            dead_letter = _dead_letter_job(state, job_id, payload={"reason": "dead"})
            pending = state / "worker-jobs" / "pending" / dead_letter.name
            existing = {
                "schema_version": 1,
                "job_id": job_id,
                "kind": "maintenance",
                "status": "pending",
                "generation": 9,
                "attempt": 0,
                "enqueue_sequence": 2,
                "enqueued_ts": 1757500000,
                "payload": {"reason": "existing"},
            }
            workers.atomic_write_json(pending, existing)

            redriven, skipped = workers.redrive_dead_letter(state, now=1757500001)
            saved = workers._load_job(pending)
            still_there = dead_letter.is_file()

        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [(job_id, "hedef kayıt mevcut")])
        self.assertEqual(saved["payload"], {"reason": "existing"})
        self.assertTrue(still_there)

    def test_invalid_record_is_reported_and_left_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            broken = state / "worker-jobs" / "dead-letter" / "job-bozuk.json"
            broken.parent.mkdir(parents=True, exist_ok=True)
            broken.write_text("{bozuk", encoding="utf-8")

            redriven, skipped = workers.redrive_dead_letter(state)
            still_there = broken.is_file()

        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [("job-bozuk.json", "worker-job-json-invalid")])
        self.assertTrue(still_there)


if __name__ == "__main__":
    unittest.main()
