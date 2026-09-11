import datetime
import json
from pathlib import Path
import tempfile
import unittest

from _fixtures import CODEX_DIR  # noqa: F401
import health_report
from state_store import state_dir_of


def _seed_state(vault: Path) -> Path:
    state = state_dir_of(vault)
    for stage in health_report.WORKER_STAGES:
        (state / "worker-jobs" / stage).mkdir(parents=True, exist_ok=True)
    return state


class HealthReportTests(unittest.TestCase):
    def test_report_reflects_queue_markers_and_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = _seed_state(vault)
            for index in range(2):
                (state / "worker-jobs" / "pending" / f"job-{index}.json").write_text(
                    json.dumps({"job_id": f"{index}" * 32}), encoding="utf-8"
                )
            (state / "worker-jobs" / "dead-letter" / "job-dead.json").write_text(
                json.dumps(
                    {
                        "job_id": "abcdef0123456789abcdef0123456789",
                        "kind": "flush",
                        "terminal_reason": "retry-exhausted",
                        "finished_ts": 1757500000,
                    }
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

    def test_rewrite_preserves_created_date(self) -> None:
        # `created` alanı notun kimliğidir; panel her üretimde bugüne
        # kaymamalı, ilk üretim tarihinde sabit kalmalı.
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_state(vault)
            first = datetime.datetime(2026, 1, 1, 9, 0)
            health_report.write_report(vault, now=first)
            second = datetime.datetime(2026, 9, 11, 9, 0)
            text = health_report.write_report(vault, now=second).read_text(
                encoding="utf-8"
            )

        self.assertIn("created: 2026-01-01", text)
        self.assertIn("updated: 2026-09-11", text)


if __name__ == "__main__":
    unittest.main()
