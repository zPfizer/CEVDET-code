import datetime
import json
from pathlib import Path
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
) -> dict[str, object]:
    return {
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


class HealthReportTests(unittest.TestCase):
    def test_report_reflects_queue_markers_and_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            for index in range(2):
                (state / "worker-jobs" / "pending" / f"job-{index}.json").write_text(
                    json.dumps({"job_id": f"{index}" * 32}), encoding="utf-8"
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

    def test_invalid_compile_metadata_is_not_published(self) -> None:
        cases = (
            {"last_run": "bozuk\nprivate-run", "last_status": "ok"},
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
        reader = mock.mock_open(read_data="created: 2026-01-01\n")
        with mock.patch.object(Path, "open", reader):
            created = health_report._previous_created(Path("panel.md"), "fallback")

        self.assertEqual(created, "2026-01-01")
        reader.return_value.__enter__.return_value.read.assert_called_once_with(2048)


if __name__ == "__main__":
    unittest.main()
