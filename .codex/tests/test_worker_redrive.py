import io
import json
from contextlib import redirect_stdout
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


def _legacy_failed_job(state: Path, job_id: str, **overrides) -> Path:
    record = {
        "schema_version": 1,
        "job_id": job_id,
        "kind": "maintenance",
        "status": "failed",
        "generation": 2,
        "attempt": 2,
        "enqueue_sequence": 1,
        "enqueued_ts": 1757400000,
        "payload": {"reason": "legacy"},
    }
    record.update(overrides)
    path = state / "worker-jobs" / "failed" / f"job-{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _active_job(state: Path, job_id: str, status: str, **overrides) -> Path:
    record = {
        "schema_version": 1,
        "job_id": job_id,
        "kind": "maintenance",
        "status": status,
        "generation": 4,
        "attempt": 1,
        "enqueue_sequence": 2,
        "enqueued_ts": 1757400000,
        "claimed_ts": 1757400001,
        "running_ts": 1757400002,
        "claim_token": "d" * 32,
        "owner_pid": 1,
        "lease_until": 1757500000,
        "payload": {"reason": "retry"},
        "redriven_ts": 1757500000,
    }
    record.update(overrides)
    path = state / "worker-jobs" / status / f"job-{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _pending_job(state: Path, job_id: str, **overrides) -> Path:
    record = {
        "schema_version": 1,
        "job_id": job_id,
        "kind": "maintenance",
        "status": "pending",
        "generation": 2,
        "attempt": 0,
        "enqueue_sequence": 2,
        "enqueued_ts": 1757400000,
        "payload": {"reason": "active"},
    }
    record.update(overrides)
    path = state / "worker-jobs" / "pending" / f"job-{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class _Cp1252Stdout(io.StringIO):
    encoding = "cp1252"


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

    def test_unmarked_pending_status_stays_in_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "c" * 32
            dead_letter = _dead_letter_job(
                state,
                job_id,
                status="pending",
                payload={"reason": "unmarked"},
            )

            recovered = workers.recover_stale_jobs(state, now=1757500001)

            pending = state / "worker-jobs" / "pending" / dead_letter.name
            stayed = dead_letter.is_file()
            moved = pending.is_file()

        self.assertEqual(recovered, 0)
        self.assertTrue(stayed)
        self.assertFalse(moved)

    def test_recovery_fences_marked_duplicate_before_moving_dead_letter(self) -> None:
        for same_identity in (True, False):
            with self.subTest(same_identity=same_identity), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                job_id = "c" * 32
                _succeeded_job(state, job_id, payload={"reason": "same"})
                dead_letter = _dead_letter_job(
                    state,
                    job_id,
                    status="pending",
                    payload={
                        "reason": "same" if same_identity else "different"
                    },
                    redriven_ts=1757500000,
                )

                recovered = workers.recover_stale_jobs(state, now=1757500001)
                conflict = workers._load_job(dead_letter)
                pending = state / "worker-jobs" / "pending" / dead_letter.name

                (state / "worker-jobs" / "succeeded" / dead_letter.name).unlink()
                workers.recover_stale_jobs(state, now=1757500002)
                still_conflict = workers._load_job(dead_letter)

            self.assertEqual(recovered, 0)
            self.assertEqual(conflict["status"], "dead-letter")
            self.assertEqual(
                conflict["terminal_reason"],
                "redrive-conflict"
                if same_identity
                else "redrive-identity-conflict",
            )
            self.assertFalse(pending.exists())
            self.assertEqual(
                still_conflict["terminal_reason"], conflict["terminal_reason"]
            )

    def test_redrive_fences_interrupted_legacy_migration_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "c" * 32
            source = _legacy_failed_job(
                state, job_id, payload={"reason": "legacy"}
            )
            dead_letter = _dead_letter_job(
                state, job_id, payload={"reason": "legacy"}
            )

            redriven, skipped = workers.redrive_dead_letter(
                state, job_id=job_id
            )
            conflict = workers._load_job(dead_letter)
            migrated = workers.migrate_legacy_failed_jobs(state, now=1757500001)
            source_exists = source.exists()
            dead_letter_exists = dead_letter.exists()
            conflict_reason = workers._load_job(dead_letter)["terminal_reason"]

        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [(job_id, "redrive çakışması")])
        self.assertEqual(conflict["terminal_reason"], "redrive-conflict")
        self.assertEqual(migrated, 0)
        self.assertTrue(source_exists)
        self.assertTrue(dead_letter_exists)
        self.assertEqual(conflict_reason, "redrive-conflict")

    def test_redrive_reconciles_interrupted_legacy_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "c" * 32
            source = _legacy_failed_job(
                state, job_id, payload={"reason": "interrupted"}
            )
            with self.assertRaisesRegex(RuntimeError, "worker-migration-injected"):
                workers.migrate_legacy_failed_jobs(
                    state,
                    now=1757500000,
                    _fail_after="destination",
                )

            redriven, skipped = workers.redrive_dead_letter(
                state, job_id=job_id, now=1757500001
            )
            pending = state / "worker-jobs" / "pending" / source.name
            source_exists = source.exists()
            pending_record = workers._load_job(pending)
            dead_letter = state / "worker-jobs" / "dead-letter" / source.name
            receipt = state / f"worker-migration-{job_id}.json"
            dead_letter_exists = dead_letter.exists()
            receipt_exists = receipt.exists()

        self.assertEqual(redriven, [job_id])
        self.assertEqual(skipped, [])
        self.assertFalse(source_exists)
        self.assertEqual(pending_record["status"], "pending")
        self.assertFalse(dead_letter_exists)
        self.assertTrue(receipt_exists)

    def test_recovery_collision_does_not_overwrite_redrive_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "c" * 32
            running = _active_job(
                state,
                job_id,
                "running",
                attempt=workers.MAX_ATTEMPTS,
                payload={"reason": "active"},
            )
            dead_letter = _dead_letter_job(
                state,
                job_id,
                status="pending",
                payload={"reason": "stale"},
                redriven_ts=1757500000,
            )
            running_before = running.read_bytes()

            with mock.patch.object(
                workers, "_process_owner_is_active", return_value=False
            ):
                workers.recover_stale_jobs(state, now=1757500001)
                first_conflict = workers._load_job(dead_letter)
                workers.recover_stale_jobs(state, now=1757500002)
                second_conflict = workers._load_job(dead_letter)

            running_after = running.read_bytes()
            running_exists = running.exists()

        self.assertEqual(first_conflict["terminal_reason"], "redrive-identity-conflict")
        self.assertEqual(second_conflict["terminal_reason"], "redrive-identity-conflict")
        self.assertEqual(running_after, running_before)
        self.assertTrue(running_exists)

    def test_legacy_migration_keeps_active_redrive_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "c" * 32
            source = _legacy_failed_job(
                state, job_id, payload={"reason": "legacy"}
            )
            pending = _pending_job(
                state,
                job_id,
                payload={"reason": "legacy"},
                redriven_ts=1757500000,
            )

            migrated = workers.migrate_legacy_failed_jobs(state, now=1757500001)
            source_exists = source.exists()
            pending_exists = pending.exists()
            dead_letter_exists = (
                state / "worker-jobs" / "dead-letter" / pending.name
            ).exists()

        self.assertEqual(migrated, 1)
        self.assertFalse(source_exists)
        self.assertTrue(pending_exists)
        self.assertFalse(dead_letter_exists)

    def test_legacy_migration_preserves_ambiguous_identity_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "c" * 32
            source = _legacy_failed_job(
                state,
                job_id,
                payload={"reason": "ambiguous"},
                last_error="legacy-failure",
                finished_ts=1757400200,
            )
            active = _pending_job(
                state,
                job_id,
                payload={"reason": "ambiguous"},
            )

            migrated = workers.migrate_legacy_failed_jobs(state, now=1757500001)
            conflict = state / "worker-jobs" / "dead-letter" / source.name
            conflict_record = workers._load_job(conflict)
            source_exists = source.exists()
            active_exists = active.exists()

            redriven, skipped = workers.redrive_dead_letter(
                state, job_id=job_id
            )
            pending_exists = active.exists()
            conflict_after_redrive = workers._load_job(conflict)

            active.unlink()
            retried, retry_skipped = workers.redrive_dead_letter(
                state, job_id=job_id
            )
            conflict_after_prune = workers._load_job(conflict)
            duplicate_pending = (
                state / "worker-jobs" / "pending" / source.name
            ).exists()

        self.assertEqual(migrated, 1)
        self.assertFalse(source_exists)
        self.assertTrue(active_exists)
        self.assertEqual(conflict_record["status"], "dead-letter")
        self.assertEqual(conflict_record["terminal_reason"], "redrive-conflict")
        self.assertEqual(conflict_record["last_error"], "legacy-failure")
        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [(job_id, "redrive çakışması")])
        self.assertTrue(pending_exists)
        self.assertEqual(
            conflict_after_redrive["terminal_reason"], "redrive-conflict"
        )
        self.assertEqual(retried, [])
        self.assertEqual(retry_skipped, [(job_id, "redrive çakışması")])
        self.assertEqual(
            conflict_after_prune["terminal_reason"], "redrive-conflict"
        )
        self.assertFalse(duplicate_pending)

    def test_legacy_migration_preserves_independent_dead_letter_collision(self) -> None:
        for with_active in (False, True):
            with self.subTest(with_active=with_active), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                job_id = "c" * 32
                source = _legacy_failed_job(
                    state,
                    job_id,
                    payload={"reason": "same"},
                    last_error="legacy-failure",
                    finished_ts=1757400200,
                )
                if with_active:
                    _pending_job(
                        state,
                        job_id,
                        payload={"reason": "same"},
                    )
                destination = _dead_letter_job(
                    state,
                    job_id,
                    payload={"reason": "same"},
                    last_error="destination-failure",
                    terminal_reason="retry-exhausted",
                )

                migrated = workers.migrate_legacy_failed_jobs(
                    state, now=1757500001
                )
                source_exists = source.exists()
                saved = workers._load_job(destination)
                receipt = state / f"worker-migration-{job_id}.json"
                receipt_exists = receipt.exists()
                migrated_again = workers.migrate_legacy_failed_jobs(
                    state, now=1757500002
                )
                source_still_exists = source.exists()
                source_last_error = json.loads(
                    source.read_text(encoding="utf-8")
                )["last_error"]

            self.assertEqual(migrated, 0)
            self.assertTrue(source_exists)
            self.assertEqual(source_last_error, "legacy-failure")
            self.assertEqual(saved["status"], "dead-letter")
            self.assertEqual(saved["terminal_reason"], "redrive-conflict")
            self.assertEqual(saved["last_error"], "destination-failure")
            self.assertFalse(receipt_exists)
            self.assertEqual(migrated_again, 0)
            self.assertTrue(source_still_exists)

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

    def test_cli_redrive_all_wakes_recovered_stale_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            stale = _active_job(
                state,
                job_id,
                "running",
                payload={"reason": "stale"},
                redriven_ts=None,
            )
            argv = [
                "worker_supervisor.py",
                "--vault",
                str(vault),
                "--state-dir",
                str(state),
                "--redrive",
            ]
            output = io.StringIO()
            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(workers, "ensure_supervisor") as wake,
                mock.patch.object(
                    workers, "_process_owner_is_active", return_value=False
                ),
                redirect_stdout(output),
            ):
                result = workers.main()

            pending = state / "worker-jobs" / "pending" / stale.name
            recovered = workers._load_job(pending)

        self.assertEqual(result, 0)
        self.assertEqual(recovered["status"], "pending")
        self.assertFalse(recovered.get("redriven_ts"))
        wake.assert_called_once_with(state, vault_root=vault)
        self.assertIn("stale işler toparlandı: 1", output.getvalue())

    def test_cli_targeted_recovery_ignores_unreadable_unrelated_pending_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            unrelated_id = "b" * 32
            stale = _active_job(
                state,
                job_id,
                "running",
                payload={"reason": "stale-target"},
                redriven_ts=None,
            )
            unrelated = _pending_job(
                state,
                unrelated_id,
                payload={"reason": "unrelated"},
            )
            real_read_text = Path.read_text

            def read_text(path: Path, *args: object, **kwargs: object) -> str:
                if path == unrelated:
                    raise OSError("pending record temporarily locked")
                return real_read_text(path, *args, **kwargs)

            argv = [
                "worker_supervisor.py",
                "--vault",
                str(vault),
                "--state-dir",
                str(state),
                "--redrive",
                job_id,
            ]
            with (
                mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text),
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(
                    workers, "_process_owner_is_active", return_value=False
                ),
                mock.patch.object(workers, "ensure_supervisor") as wake,
            ):
                result = workers.main()

            pending = state / "worker-jobs" / "pending" / stale.name
            recovered = workers._load_job(pending)
            unrelated_exists = unrelated.is_file()

        self.assertEqual(result, 1)
        self.assertEqual(recovered["status"], "pending")
        self.assertTrue(unrelated_exists)
        wake.assert_called_once_with(state, vault_root=vault)

    def test_cli_redrive_retry_recovers_transition_and_wakes_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            dead_letter = _dead_letter_job(state, job_id, payload={"reason": "retry"})
            real_replace = workers.os.replace

            def fail_transition(source: Path, destination: Path) -> None:
                if (
                    source.parent.name == "dead-letter"
                    and destination.parent.name == "pending"
                ):
                    raise OSError("simulated interruption")
                real_replace(source, destination)

            with mock.patch.object(workers.os, "replace", side_effect=fail_transition):
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
                        job_id,
                    ],
                ):
                    with self.assertRaisesRegex(OSError, "simulated interruption"):
                        workers.main()

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
                        job_id,
                    ],
                ),
                mock.patch.object(workers, "ensure_supervisor") as wake,
            ):
                result = workers.main()

            pending = state / "worker-jobs" / "pending" / dead_letter.name
            recovered = workers._load_job(pending)

        self.assertEqual(result, 0)
        self.assertEqual(recovered["job_id"], job_id)
        wake.assert_called_once_with(state, vault_root=vault)

    def test_cli_redrive_retry_wakes_pending_after_launcher_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            dead_letter = _dead_letter_job(state, job_id, payload={"reason": "launcher"})
            argv = [
                "worker_supervisor.py",
                "--vault",
                str(vault),
                "--state-dir",
                str(state),
                "--redrive",
                job_id,
            ]
            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(
                    workers,
                    "ensure_supervisor",
                    side_effect=OSError("launcher failed"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "launcher failed"):
                    workers.main()
            pending = state / "worker-jobs" / "pending" / dead_letter.name
            self.assertIsInstance(workers._load_job(pending)["redriven_ts"], int)

            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(workers, "ensure_supervisor") as wake,
            ):
                result = workers.main()

        self.assertEqual(result, 0)
        wake.assert_called_once_with(state, vault_root=vault)

    def test_cli_targeted_redrive_ignores_unrelated_invalid_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            unrelated_id = "b" * 32
            _dead_letter_job(state, job_id, payload={"reason": "target"})
            unrelated = (
                state / "worker-jobs" / "dead-letter" / f"job-{unrelated_id}.json"
            )
            unrelated.write_text("{bozuk", encoding="utf-8")
            argv = [
                "worker_supervisor.py",
                "--vault",
                str(vault),
                "--state-dir",
                str(state),
                "--redrive",
                job_id,
            ]
            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(workers, "ensure_supervisor") as wake,
            ):
                result = workers.main()

            pending = state / "worker-jobs" / "pending" / f"job-{job_id}.json"
            moved = pending.is_file()
            unrelated_stayed = unrelated.is_file()

        self.assertEqual(result, 0)
        self.assertTrue(moved)
        self.assertTrue(unrelated_stayed)
        wake.assert_called_once_with(state, vault_root=vault)

    def test_cli_targeted_redrive_ignores_unreadable_unrelated_legacy_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            unrelated_id = "b" * 32
            _dead_letter_job(state, job_id, payload={"reason": "target"})
            unrelated = _legacy_failed_job(state, unrelated_id)
            real_read_text = Path.read_text

            def read_text(path: Path, *args: object, **kwargs: object) -> str:
                if path == unrelated:
                    raise OSError("legacy record temporarily locked")
                return real_read_text(path, *args, **kwargs)

            argv = [
                "worker_supervisor.py",
                "--vault",
                str(vault),
                "--state-dir",
                str(state),
                "--redrive",
                job_id,
            ]
            with (
                mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text),
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(workers, "ensure_supervisor") as wake,
            ):
                result = workers.main()

            pending = state / "worker-jobs" / "pending" / f"job-{job_id}.json"
            moved = pending.is_file()
            unrelated_stayed = unrelated.is_file()

        self.assertEqual(result, 0)
        self.assertTrue(moved)
        self.assertTrue(unrelated_stayed)
        wake.assert_called_once_with(state, vault_root=vault)

    def test_cli_redrive_defers_ownerless_launch_without_duplicate_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            _dead_letter_job(state, job_id, payload={"reason": "deferred"})
            (state / "worker-supervisor.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "launching",
                        "generation": 1,
                        "launch_token": "c" * 32,
                        "owner_pid": 0,
                        "lease_until": int(workers.time.time()) + workers.LAUNCH_LEASE_SECONDS,
                        "updated_ts": int(workers.time.time()),
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "worker_supervisor.py",
                "--vault",
                str(vault),
                "--state-dir",
                str(state),
                "--redrive",
                job_id,
            ]
            real_ensure = workers.ensure_supervisor
            launch = mock.Mock()

            def ensure_without_duplicate(*args: object, **kwargs: object) -> bool:
                kwargs["launcher"] = launch
                return real_ensure(*args, **kwargs)

            output = io.StringIO()
            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(
                    workers,
                    "ensure_supervisor",
                    side_effect=ensure_without_duplicate,
                ) as wake,
                redirect_stdout(output),
            ):
                first_result = workers.main()
                second_result = workers.main()

            pending = state / "worker-jobs" / "pending" / f"job-{job_id}.json"
            pending_stayed = pending.is_file()

        self.assertEqual(first_result, 1)
        self.assertEqual(second_result, 1)
        self.assertTrue(pending_stayed)
        self.assertEqual(wake.call_count, 2)
        launch.assert_not_called()
        self.assertEqual(output.getvalue().count("redrive beklemede"), 2)

    def test_cli_redrive_accepts_false_wake_when_supervisor_owner_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            _dead_letter_job(state, job_id, payload={"reason": "active-owner"})
            (state / "worker-supervisor.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "running",
                        "generation": 1,
                        "launch_token": "c" * 32,
                        "owner_pid": 123,
                        "lease_until": int(workers.time.time()) + workers.SUPERVISOR_LEASE_SECONDS,
                        "updated_ts": int(workers.time.time()),
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "worker_supervisor.py",
                "--vault",
                str(vault),
                "--state-dir",
                str(state),
                "--redrive",
                job_id,
            ]
            output = io.StringIO()
            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(workers, "ensure_supervisor", return_value=False) as wake,
                mock.patch.object(workers, "_process_owner_is_active", return_value=True),
                redirect_stdout(output),
            ):
                result = workers.main()

        self.assertEqual(result, 0)
        wake.assert_called_once_with(state, vault_root=vault)
        self.assertIn("pending'e döndü", output.getvalue())
        self.assertNotIn("belirsiz", output.getvalue())

    def test_cli_redrive_reports_uncertain_owner_identity(self) -> None:
        for classification in ("invalid", "unreadable"):
            with self.subTest(classification=classification), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / "state"
                vault = root / "vault"
                vault.mkdir()
                job_id = "a" * 32
                _dead_letter_job(state, job_id, payload={"reason": "uncertain"})
                (state / "worker-supervisor.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "running",
                            "generation": 1,
                            "launch_token": "c" * 32,
                            "owner_pid": 123,
                            "owner_identity": "recorded-owner",
                            "lease_until": int(workers.time.time()) + workers.SUPERVISOR_LEASE_SECONDS,
                            "updated_ts": int(workers.time.time()),
                        }
                    ),
                    encoding="utf-8",
                )
                argv = [
                    "worker_supervisor.py",
                    "--vault",
                    str(vault),
                    "--state-dir",
                    str(state),
                    "--redrive",
                    job_id,
                ]
                output = io.StringIO()
                with (
                    mock.patch.object(workers.sys, "argv", argv),
                    mock.patch.object(workers, "ensure_supervisor", return_value=False),
                    mock.patch.object(workers, "_process_owner_is_active", return_value=True),
                    mock.patch.object(
                        workers,
                        "_process_owner_classification",
                        return_value=classification,
                    ),
                    redirect_stdout(output),
                ):
                    result = workers.main()

            self.assertEqual(result, 1)
            self.assertIn("supervisor doğrulanamadı", output.getvalue())

    def test_cli_redrive_retry_recognizes_jobs_after_pending(self) -> None:
        for status in ("claimed", "running", "succeeded"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / "state"
                vault = root / "vault"
                vault.mkdir()
                job_id = "a" * 32
                if status == "succeeded":
                    _succeeded_job(
                        state,
                        job_id,
                        payload={"reason": "retry"},
                        redriven_ts=1757500000,
                    )
                else:
                    _active_job(state, job_id, status)
                argv = [
                    "worker_supervisor.py",
                    "--vault",
                    str(vault),
                    "--state-dir",
                    str(state),
                    "--redrive",
                    job_id,
                ]
                output = io.StringIO()
                with (
                    mock.patch.object(workers.sys, "argv", argv),
                    mock.patch.object(workers, "ensure_supervisor") as wake,
                    mock.patch.object(
                        workers, "_process_owner_is_active", return_value=True
                    ),
                    redirect_stdout(output),
                ):
                    result = workers.main()

            self.assertEqual(result, 0)
            wake.assert_not_called()
            message = output.getvalue()
            if status == "succeeded":
                self.assertIn("redrive zaten tamamlandı", message)
            else:
                self.assertIn("redrive zaten sürüyor", message)

    def test_redrive_does_not_duplicate_marked_active_job(self) -> None:
        for status in ("running", "succeeded"):
            for same_identity in (True, False):
                with self.subTest(
                    status=status, same_identity=same_identity
                ), tempfile.TemporaryDirectory() as temporary:
                    state = Path(temporary)
                    job_id = "a" * 32
                    active_payload = {"reason": "active"}
                    dead_payload = (
                        active_payload
                        if same_identity
                        else {"reason": "stale-copy"}
                    )
                    if status == "running":
                        _active_job(
                            state, job_id, status, payload=active_payload
                        )
                    else:
                        _succeeded_job(
                            state,
                            job_id,
                            payload=active_payload,
                            redriven_ts=1757500000,
                        )
                    dead_letter = _dead_letter_job(
                        state, job_id, payload=dead_payload
                    )

                    redriven, skipped = workers.redrive_dead_letter(
                        state, job_id=job_id
                    )

                    pending = state / "worker-jobs" / "pending" / dead_letter.name
                    saved = workers._load_job(dead_letter)
                    duplicate_pending = pending.exists()
                    dead_letter_stayed = dead_letter.is_file()

                    if status == "succeeded":
                        (
                            state / "worker-jobs" / "succeeded" / dead_letter.name
                        ).unlink()
                        retried, retry_skipped = workers.redrive_dead_letter(
                            state, job_id=job_id
                        )
                        conflict_still_there = dead_letter.is_file()

                self.assertEqual(redriven, [job_id])
                self.assertEqual(skipped, [(job_id, "redrive çakışması")])
                expected_reason = (
                    "redrive-conflict"
                    if same_identity
                    else "redrive-identity-conflict"
                )
                self.assertEqual(saved["terminal_reason"], expected_reason)
                self.assertFalse(duplicate_pending)
                self.assertTrue(dead_letter_stayed)
                if status == "succeeded":
                    self.assertEqual(retried, [])
                    self.assertEqual(
                        retry_skipped, [(job_id, "redrive çakışması")]
                    )
                    self.assertTrue(conflict_still_there)

    def test_redrive_rejects_unmarked_existing_job_id(self) -> None:
        for status in ("pending", "claimed", "running", "succeeded", "quarantined"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                job_id = "a" * 32
                active_payload = {"reason": "active"}
                if status == "pending":
                    _pending_job(state, job_id, payload=active_payload)
                elif status == "quarantined":
                    source = _succeeded_job(
                        state, job_id, payload=active_payload
                    )
                    workers._quarantine_job(
                        state, source, "worker-job-schema-invalid"
                    )
                elif status == "succeeded":
                    _succeeded_job(state, job_id, payload=active_payload)
                else:
                    _active_job(
                        state,
                        job_id,
                        status,
                        payload=active_payload,
                        redriven_ts=None,
                    )
                dead_letter = _dead_letter_job(
                    state, job_id, payload=active_payload
                )

                redriven, skipped = workers.redrive_dead_letter(
                    state, job_id=job_id
                )
                saved = workers._load_job(dead_letter)
                pending = state / "worker-jobs" / "pending" / dead_letter.name
                conflict_still_there = dead_letter.is_file()

            self.assertEqual(redriven, [])
            self.assertEqual(skipped, [(job_id, "redrive çakışması")])
            self.assertEqual(saved["terminal_reason"], "redrive-conflict")
            self.assertFalse(pending.exists())
            self.assertTrue(conflict_still_there)

    def test_redrive_rejects_unreadable_same_id_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "a" * 32
            receipt = state / "worker-jobs" / "succeeded" / f"job-{job_id}.json"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text("{bozuk", encoding="utf-8")
            dead_letter = _dead_letter_job(
                state, job_id, payload={"reason": "stale-copy"}
            )

            redriven, skipped = workers.redrive_dead_letter(
                state, job_id=job_id
            )
            conflict = workers._load_job(dead_letter)
            pending = state / "worker-jobs" / "pending" / dead_letter.name

        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [(job_id, "redrive çakışması")])
        self.assertEqual(conflict["terminal_reason"], "redrive-identity-conflict")
        self.assertFalse(pending.exists())

    def test_redrive_does_not_requeue_failed_redrive_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            job_id = "a" * 32
            dead_letter = _dead_letter_job(
                state,
                job_id,
                payload={"reason": "failed"},
                redriven_ts=1757500000,
                terminal_reason="retry-exhausted",
            )

            redriven, skipped = workers.redrive_dead_letter(
                state, job_id=job_id
            )
            pending = state / "worker-jobs" / "pending" / dead_letter.name
            stayed = dead_letter.is_file()

        self.assertEqual(redriven, [])
        self.assertEqual(
            skipped,
            [(job_id, "önceki redrive dead-letter'da kaldı")],
        )
        self.assertFalse(pending.exists())
        self.assertTrue(stayed)

    def test_cli_redrive_output_is_safe_for_legacy_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            job_id = "a" * 32
            _succeeded_job(
                state,
                job_id,
                payload={"reason": "legacy"},
                redriven_ts=1757500000,
            )
            argv = [
                "worker_supervisor.py",
                "--vault",
                str(vault),
                "--state-dir",
                str(state),
                "--redrive",
                job_id,
            ]
            output = _Cp1252Stdout()
            with (
                mock.patch.object(workers.sys, "argv", argv),
                mock.patch.object(workers, "ensure_supervisor") as wake,
                mock.patch.object(workers.sys, "stdout", output),
            ):
                result = workers.main()

        self.assertEqual(result, 0)
        wake.assert_not_called()
        self.assertIn(r"redrive zaten tamamland\u0131", output.getvalue())

    def test_cli_help_is_safe_for_legacy_encoding(self) -> None:
        output = _Cp1252Stdout()
        with (
            mock.patch.object(
                workers.sys,
                "argv",
                ["worker_supervisor.py", "--help"],
            ),
            mock.patch.object(workers.sys, "stdout", output),
        ):
            with self.assertRaises(SystemExit) as raised:
                workers.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("move dead-letter jobs to pending", output.getvalue())

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

    def test_cli_targeted_normal_pending_job_is_not_treated_as_redriven(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            vault = root / "vault"
            vault.mkdir()
            pending = workers.enqueue_job(
                state,
                "maintenance",
                {"reason": "ordinary"},
                start_supervisor=False,
            )
            job_id = pending.stem.removeprefix("job-")
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
                        job_id,
                    ],
                ),
                mock.patch.object(workers, "ensure_supervisor") as wake,
            ):
                result = workers.main()

        self.assertEqual(result, 1)
        wake.assert_not_called()

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
            conflict = workers._load_job(dead_letter)
            still_there = dead_letter.is_file()

        self.assertEqual(redriven, [])
        self.assertEqual(skipped, [(job_id, "redrive çakışması")])
        self.assertEqual(saved["payload"], {"reason": "existing"})
        self.assertEqual(conflict["terminal_reason"], "redrive-identity-conflict")
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
