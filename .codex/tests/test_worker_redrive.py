import json
from pathlib import Path
import tempfile
import unittest

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


class RedriveDeadLetterTests(unittest.TestCase):
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
            original = _dead_letter_job(
                state,
                job_id,
                payload={},
                terminal_reason="recovered-by-successor",
                recovery_job_id="c" * 32,
            )

            redriven, skipped = workers.redrive_dead_letter(state)
            still_there = original.is_file()

        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [(job_id, "zaten kurtarılmış")])
        self.assertTrue(still_there)

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
