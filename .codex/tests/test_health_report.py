import datetime
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import health_report
import worker_supervisor
from state_store import state_dir_of


def _seed_state(vault: Path) -> Path:
    state = state_dir_of(vault)
    for stage in health_report.WORKER_STAGES:
        (state / "worker-jobs" / stage).mkdir(parents=True, exist_ok=True)
    return state


def _dead_letter_record(
    job_id: str,
    *,
    finished_ts: object,
    kind: str = "flush",
    terminal_reason: str = "retry-exhausted",
    recovery_job_id: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": worker_supervisor.JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "kind": kind,
        "status": "dead-letter",
        "generation": 1,
        "attempt": 3,
        "enqueue_sequence": 1,
        "enqueued_ts": 1,
        "payload": {},
        "finished_ts": finished_ts,
        "terminal_reason": terminal_reason,
        "retryable": False,
        "lease_until": 0,
    }
    if recovery_job_id is not None:
        record["recovery_job_id"] = recovery_job_id
    return record


def _pending_record(job_id: str) -> dict[str, object]:
    record = _dead_letter_record(job_id, finished_ts=1)
    record["status"] = "pending"
    record.pop("terminal_reason")
    record.pop("retryable")
    record.pop("lease_until")
    return record


class HealthReportTests(unittest.TestCase):
    def test_report_reflects_queue_markers_and_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            for index in range(2):
                job_id = f"{index}" * 32
                (state / "worker-jobs" / "pending" / f"job-{job_id}.json").write_text(
                    json.dumps(_pending_record(job_id)), encoding="utf-8"
                )
            job_id = "abcdef0123456789abcdef0123456789"
            (state / "worker-jobs" / "dead-letter" / f"job-{job_id}.json").write_text(
                json.dumps(
                    _dead_letter_record(job_id, finished_ts=1757500000)
                ),
                encoding="utf-8",
            )
            # İşaretin kendisi sayılmalı, kilit sidecar'ı sayılmamalı.
            (state / ("memory-read-only-" + "a" * 64)).write_text("", encoding="utf-8")
            (state / ("memory-read-only-" + "a" * 64 + ".lock")).write_text("", encoding="utf-8")
            (state / "compile-state.json").write_text(
                json.dumps(
                    {
                        "ingested": {},
                        "cursor": "",
                        "last_run": "2026-09-10T12:00:00",
                        "last_status": "ok",
                        "runs": [],
                    }
                ),
                encoding="utf-8",
            )

            target = health_report.write_report(vault)
            text = target.read_text(encoding="utf-8")

        self.assertEqual(target.name, "Cevo Sağlık.md")
        self.assertIn("| pending | 2 |", text)
        self.assertIn("| dead-letter | 1 |", text)
        self.assertIn("| abcdef01 | flush | retry-exhausted |", text)
        self.assertIn("Aktif read-only işareti: 1", text)
        self.assertIn("Aktif session-only işareti: 0", text)
        self.assertIn("2026-09-10T12:00:00 (durum: ok)", text)
        self.assertIn("type: dashboard", text)

    def test_empty_state_renders_clean_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            text = health_report.write_report(vault).read_text(encoding="utf-8")

        self.assertIn("Boş — takılı iş yok.", text)
        self.assertIn("kayıt yok (temiz)", text)
        self.assertIn("Derleyici son çalışma: hiç", text)

    def test_unreadable_worker_directory_aborts_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            target = vault / "report.md"
            with mock.patch.object(Path, "iterdir", side_effect=PermissionError):
                with self.assertRaisesRegex(OSError, "worker-directory-unreadable"):
                    health_report.write_report(vault, target)
            self.assertFalse(target.exists())

    def test_linked_worker_jobs_root_aborts_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-worker"
            vault.mkdir()
            outside.mkdir()
            state = state_dir_of(vault)
            state.mkdir(parents=True)
            jobs = state / "worker-jobs"
            try:
                jobs.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = vault / "report.md"
            with self.assertRaisesRegex(OSError, "worker-directory-invalid"):
                health_report.write_report(vault, target)
            self.assertFalse(target.exists())

    def test_unreadable_marker_enumeration_aborts_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            target = vault / "report.md"
            real_entries = health_report._directory_entries

            def fail_markers(
                directory: Path, *, error_prefix: str
            ) -> tuple[Path, ...]:
                if error_prefix == "state":
                    raise OSError("state-directory-unreadable")
                return real_entries(directory, error_prefix=error_prefix)

            with mock.patch.object(
                health_report,
                "_directory_entries",
                side_effect=fail_markers,
            ):
                with self.assertRaisesRegex(OSError, "state-directory-unreadable"):
                    health_report.write_report(vault, target)
            self.assertFalse(target.exists())

    def test_linked_compile_state_is_reported_as_unreadable(self) -> None:
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    vault = root / "vault"
                    outside = root / "outside-compile-state.json"
                    vault.mkdir()
                    state = _seed_state(vault)
                    outside.write_text(
                        json.dumps(
                            {
                                "ingested": {},
                                "cursor": "",
                                "last_run": "2026-01-01T00:00:00",
                                "last_status": "ok",
                                "runs": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    path = state / "compile-state.json"
                    try:
                        path.symlink_to(
                            state / "missing-compile-state.json"
                            if dangling
                            else outside
                        )
                    except (OSError, NotImplementedError) as exc:
                        self.skipTest(f"symlink unavailable: {exc}")

                    self.assertEqual(
                        health_report.compile_summary(state),
                        ("?", "okunamadı"),
                    )

    def test_invalid_compile_metadata_is_not_published(self) -> None:
        cases = (
            {"last_run": "bozuk\nprivate-run", "last_status": "ok"},
            {"last_run": "2026-09-10\n12:00:00", "last_status": "ok"},
            {"last_run": "2026-09-10T12:00:00", "last_status": "bad\nprivate-status"},
            {"last_run": "2026-09-10T12:00:00", "last_status": ["ok"]},
        )
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            for metadata in cases:
                with self.subTest(metadata=metadata):
                    (state / "compile-state.json").write_text(
                        json.dumps(
                            {
                                "ingested": {},
                                "cursor": "",
                                "last_run": metadata["last_run"],
                                "last_status": metadata["last_status"],
                                "runs": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    text = health_report.render(
                        vault, now=datetime.datetime(2026, 9, 11)
                    )

                    self.assertIn(
                        "Derleyici son çalışma: ? (durum: bozuk kayıt)", text
                    )
                    self.assertNotIn("private-", text)

    def test_maintenance_lane_and_cleanup_fence_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            pending = state / "maintenance" / "worker-jobs" / "pending"
            pending.mkdir(parents=True)
            job_id = "a" * 32
            (pending / f"job-{job_id}.json").write_text(
                json.dumps(_pending_record(job_id)), encoding="utf-8"
            )
            fence = state / "maintenance" / "worker-tree-cleanup-unverified.json"
            fence.parent.mkdir(parents=True, exist_ok=True)
            fence.write_text("{}", encoding="utf-8")

            text = health_report.render(
                vault, now=datetime.datetime(2026, 9, 11)
            )

        self.assertIn("| pending | 1 |", text)
        self.assertIn("Worker temizleme fence'i etkin", text)
        self.assertIn("maintenance", text)
        self.assertNotIn("Boş — takılı iş yok.", text)

    def test_invalid_queue_record_aborts_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            job_id = "b" * 32
            (state / "worker-jobs" / "succeeded" / f"job-{job_id}.json").write_text(
                "{}", encoding="utf-8"
            )
            target = vault / "report.md"

            with self.assertRaisesRegex(OSError, "worker-record-invalid"):
                health_report.write_report(vault, target)
            self.assertFalse(target.exists())

    def test_quarantined_queue_record_is_not_reported_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            quarantine = state / "worker-jobs" / "quarantined"
            job_id = "c" * 32
            payload = quarantine / f"job-{job_id}.payload"
            payload.write_bytes(b"unreadable worker payload")
            (quarantine / f"job-{job_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": worker_supervisor.JOB_SCHEMA_VERSION,
                        "job_id": job_id,
                        "status": "quarantined",
                        "reason_code": "worker-job-json-invalid",
                        "payload_sha256": hashlib.sha256(
                            payload.read_bytes()
                        ).hexdigest(),
                        "payload_file": payload.name,
                    }
                ),
                encoding="utf-8",
            )

            text = health_report.render(
                vault, now=datetime.datetime(2026, 9, 11)
            )

        self.assertIn("Quarantine'da çözülemeyen iş var", text)
        self.assertNotIn("Boş — takılı iş yok.", text)

    def test_nonexistent_vault_is_rejected_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "missing-vault"
            target = root / "report.md"

            with self.assertRaisesRegex(ValueError, "vault-invalid"):
                health_report.write_report(vault, target)

            self.assertFalse(vault.exists())
            self.assertFalse(target.exists())

    def test_missing_runtime_state_is_rejected_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "unrelated-directory"
            vault.mkdir()
            target = vault / "report.md"

            with self.assertRaisesRegex(ValueError, "vault-runtime-invalid"):
                health_report.write_report(vault, target)

            self.assertFalse(target.exists())

    def test_linked_runtime_state_ancestor_is_rejected_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-runtime"
            vault.mkdir()
            outside.mkdir()
            link = vault / ".codex"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = vault / "report.md"
            try:
                with self.assertRaisesRegex(ValueError, "vault-runtime-invalid"):
                    health_report.write_report(vault, target)
                self.assertFalse(target.exists())
            finally:
                link.unlink(missing_ok=True)

    def test_orphan_hook_input_prevents_clean_queue_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            (state / "hookin-orphan.json").write_text(
                json.dumps(
                    {
                        "delivery_schema_version": 1,
                        "session_id": "orphan",
                        "transcript_path": str(state / "source.jsonl"),
                        "reason": "turnend",
                        "event_iso": "2026-09-09T12:00:00+03:00",
                    }
                ),
                encoding="utf-8",
            )

            text = health_report.render(
                vault, now=datetime.datetime(2026, 9, 11)
            )

        self.assertIn("Kurtarılmayı bekleyen hook girdisi: 1", text)
        self.assertIn("Worker kurtarma bekliyor", text)
        self.assertNotIn("Boş — takılı iş yok.", text)

    def test_flush_support_artifacts_are_not_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            for prefix in ("flush-coverage-", "flush-batch-", "flush-index-"):
                (state / f"{prefix}{'c' * 64}.json").write_text(
                    "{}", encoding="utf-8"
                )
            (state / "flush-session.json").write_text("{}", encoding="utf-8")

            self.assertEqual(health_report.flush_state_count(state), 1)

    def test_invalid_health_component_is_reported_as_unreadable(self) -> None:
        cases = (
            {"compile:global": ["malformed"]},
            {"compile:global": {"status": "unknown"}},
        )
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            for components in cases:
                with self.subTest(components=components):
                    (state / "health.json").write_text(
                        json.dumps(
                            {
                                "schema_version": health_report.HEALTH_SCHEMA_VERSION,
                                "components": components,
                            }
                        ),
                        encoding="utf-8",
                    )
                    self.assertEqual(health_report.health_summary(state), "okunamadı")

            (state / "health.json").write_text(
                json.dumps(
                    {
                        "schema_version": health_report.HEALTH_SCHEMA_VERSION,
                        "components": {
                            "compile:global": {"status": "error"},
                            "flush:global": {"status": "warning"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(health_report.health_summary(state), "hata=1 uyarı=1")

    def test_malformed_dead_letter_timestamps_sort_and_render_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            dead_letter = state / "worker-jobs" / "dead-letter"
            records = (
                ("old-job", "0" * 32, 1),
                ("new-job", "1" * 32, "1757500000"),
                ("null-job", "2" * 32, None),
                ("nan-job", "3" * 32, float("nan")),
                ("inf-job", "4" * 32, float("inf")),
                ("huge-job", "5" * 32, 10**1000),
            )
            for _label, job_id, finished_ts in records:
                (dead_letter / f"job-{job_id}.json").write_text(
                    json.dumps(_dead_letter_record(job_id, finished_ts=finished_ts)),
                    encoding="utf-8",
                )

            rows = health_report.dead_letter_rows(state)
            text = health_report.render(vault, now=datetime.datetime(2026, 9, 11))

        self.assertEqual([row["job_id"] for row in rows[:2]], ["11111111", "00000000"])
        self.assertEqual(health_report._format_ts(None), "?")
        self.assertEqual(health_report._format_ts(float("nan")), "?")
        self.assertEqual(health_report._format_ts(10**1000), "?")
        for prefix in ("2", "3", "4", "5"):
            self.assertIn(
                f"| {prefix * 8} | flush | retry-exhausted | ? |",
                text,
            )

    def test_invalid_dead_letter_fields_are_not_published(self) -> None:
        cases = (
            {"kind": "flush\nprivate-kind"},
            {"terminal_reason": "retry-exhausted\nprivate-reason"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            dead_letter = state / "worker-jobs" / "dead-letter"
            for index, changes in enumerate(cases):
                job_id = f"{index + 6:x}" * 32
                record = _dead_letter_record(job_id, finished_ts=1757500000)
                record.update(changes)
                (dead_letter / f"job-{job_id}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )

            text = health_report.render(
                vault, now=datetime.datetime(2026, 9, 11)
            )

        self.assertNotIn("private-kind", text)
        self.assertNotIn("private-reason", text)
        self.assertEqual(text.count("| ? | ? | ? | ? |"), 2)

    def test_current_redrive_conflict_reasons_are_published(self) -> None:
        reasons = ("redrive-conflict", "redrive-identity-conflict")
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            dead_letter = state / "worker-jobs" / "dead-letter"
            for index, reason in enumerate(reasons):
                job_id = f"{index + 8:x}" * 32
                (dead_letter / f"job-{job_id}.json").write_text(
                    json.dumps(
                        _dead_letter_record(
                            job_id,
                            finished_ts=1757500000 + index,
                            terminal_reason=reason,
                        )
                    ),
                    encoding="utf-8",
                )

            rows = health_report.dead_letter_rows(state)

        self.assertEqual(
            {row["terminal_reason"] for row in rows}, set(reasons)
        )

    def test_verified_recovered_dead_letter_is_not_stuck(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            dead_letter = state / "worker-jobs" / "dead-letter"
            succeeded = state / "worker-jobs" / "succeeded"
            old_id = "6" * 32
            successor_id = "7" * 32
            (dead_letter / f"job-{old_id}.json").write_text(
                json.dumps(
                    _dead_letter_record(
                        old_id,
                        finished_ts=1757500000,
                        terminal_reason="recovered-by-successor",
                        recovery_job_id=successor_id,
                    )
                ),
                encoding="utf-8",
            )
            successor = _dead_letter_record(
                successor_id,
                finished_ts=1757500001,
            )
            successor["status"] = "succeeded"
            (succeeded / f"job-{successor_id}.json").write_text(
                json.dumps(successor),
                encoding="utf-8",
            )

            text = health_report.render(
                vault, now=datetime.datetime(2026, 9, 11)
            )

        self.assertIn("| dead-letter | 0 |", text)
        self.assertIn("Boş — takılı iş yok.", text)
        self.assertNotIn("Takılı iş var", text)

    def test_malformed_recovered_dead_letter_stays_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            dead_letter = state / "worker-jobs" / "dead-letter"
            succeeded = state / "worker-jobs" / "succeeded"
            old_id = "8" * 32
            successor_id = "9" * 32
            (dead_letter / f"job-{old_id}.json").write_text(
                json.dumps(
                    _dead_letter_record(
                        old_id,
                        finished_ts=None,
                        terminal_reason="recovered-by-successor",
                        recovery_job_id=successor_id,
                    )
                ),
                encoding="utf-8",
            )
            successor = _dead_letter_record(
                successor_id,
                finished_ts=1757500001,
            )
            successor["status"] = "succeeded"
            (succeeded / f"job-{successor_id}.json").write_text(
                json.dumps(successor), encoding="utf-8"
            )

            text = health_report.render(
                vault, now=datetime.datetime(2026, 9, 11)
            )

        self.assertIn("| dead-letter | 1 |", text)
        self.assertIn("Takılı iş var: 1 kayıt.", text)
        self.assertIn("| ? | ? | ? | ? |", text)

    def test_default_report_write_does_not_clobber_existing_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            target = vault / health_report.PANEL_RELATIVE
            original = b"# Kullanici paneli\r\nEk not\r\n"
            target.parent.mkdir(parents=True)
            target.write_bytes(original)

            with self.assertRaises(FileExistsError):
                health_report.write_report(vault)
            preserved = target.read_bytes()

        self.assertEqual(preserved, original)

    def test_overwrite_rejects_symlink_report_target_before_metadata_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-secret.md"
            vault.mkdir()
            _seed_state(vault)
            outside.write_text("created: 2020-01-01\nprivate\n", encoding="utf-8")
            target = vault / "report.md"
            try:
                target.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "report-target-invalid"):
                health_report.write_report(vault, target, overwrite=True)
            self.assertTrue(target.is_symlink())
            preserved = outside.read_text(encoding="utf-8")

        self.assertEqual(preserved, "created: 2020-01-01\nprivate\n")

    def test_overwrite_ignores_invalid_created_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            target = vault / "report.md"
            target.write_text(
                "---\ncreated: private-token: [broken\n---\n", encoding="utf-8"
            )
            text = health_report.write_report(
                vault,
                target,
                now=datetime.datetime(2026, 9, 11),
                overwrite=True,
            ).read_text(encoding="utf-8")

        self.assertIn("created: 2026-09-11", text)
        self.assertNotIn("private-token", text)

    def test_overwrite_aborts_when_existing_report_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            target = vault / "report.md"
            original = b"\xff\xfe"
            target.write_bytes(original)

            with self.assertRaisesRegex(ValueError, "report-target-unreadable"):
                health_report.write_report(vault, target, overwrite=True)
            self.assertEqual(target.read_bytes(), original)

    def test_default_report_rejects_linked_command_center_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside"
            vault.mkdir()
            outside.mkdir()
            parent = vault / health_report.PANEL_RELATIVE.parent
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = outside / health_report.PANEL_RELATIVE.name
            _seed_state(vault)
            try:
                with self.assertRaisesRegex(ValueError, "report-target-invalid"):
                    health_report.write_report(vault)
                self.assertFalse(target.exists())
            finally:
                parent.unlink(missing_ok=True)

    def test_dangling_health_record_is_reported_as_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _seed_state(Path(temporary))
            path = state / "health.json"
            try:
                path.symlink_to(state / "missing-health.json")
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            self.assertEqual(health_report.health_summary(state), "okunamadı")

    def test_health_record_metadata_failure_is_reported_as_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = _seed_state(Path(temporary))
            with mock.patch.object(Path, "lstat", side_effect=PermissionError):
                self.assertEqual(health_report.health_summary(state), "okunamadı")

    def test_cli_overwrite_replaces_existing_report_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            target = vault / "report.md"
            target.write_text("kullanici notu\n", encoding="utf-8")

            with mock.patch("builtins.print"):
                result = health_report.main(
                    ["--vault", str(vault), "--output", str(target), "--overwrite"]
                )
            text = target.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertIn("Cevo Sağlık", text)
        self.assertNotIn("kullanici notu", text)

    def test_rewrite_preserves_created_date(self) -> None:
        # `created` alanı notun kimliğidir; panel her üretimde bugüne
        # kaymamalı, ilk üretim tarihinde sabit kalmalı.
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            first = datetime.datetime(2026, 1, 1, 9, 0)
            health_report.write_report(vault, now=first)
            target = vault / health_report.PANEL_RELATIVE
            target.write_bytes(target.read_bytes() + b"\nKullanici eki\n")
            second = datetime.datetime(2026, 9, 11, 9, 0)
            text = health_report.write_report(vault, now=second, overwrite=True).read_text(
                encoding="utf-8"
            )

        self.assertIn("created: 2026-01-01", text)
        self.assertIn("updated: 2026-09-11", text)
        self.assertNotIn("Kullanici eki", text)

    def test_rewrite_preserves_created_date_for_custom_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            target = vault / "report.md"
            first = datetime.datetime(2026, 1, 1, 9, 0)
            health_report.write_report(vault, target, now=first)
            second = datetime.datetime(2026, 9, 11, 9, 0)
            text = health_report.write_report(
                vault, target, now=second, overwrite=True
            ).read_text(encoding="utf-8")

        self.assertIn("created: 2026-01-01", text)
        self.assertIn("updated: 2026-09-11", text)

    def test_previous_created_reads_only_bounded_prefix(self) -> None:
        reader = mock.mock_open(read_data="---\ncreated: 2026-01-01\n---\n")
        with (
            mock.patch.object(Path, "lstat", return_value=mock.Mock(st_mode=stat.S_IFREG)),
            mock.patch.object(Path, "open", reader),
        ):
            created = health_report._previous_created(Path("panel.md"), "fallback")

        self.assertEqual(created, "2026-01-01")
        reader.return_value.__enter__.return_value.read.assert_called_once_with(2048)


if __name__ == "__main__":
    unittest.main()
