import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import codex_runner
import file_lock
import model_usage
import process_control


class ModelUsageTests(unittest.TestCase):
    def test_record_appends_daily_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            moment = datetime.datetime(2026, 9, 11, 10, 0)
            for outcome in ("ok", "codex-timeout"):
                model_usage.record(
                    state,
                    purpose="flush",
                    prompt_chars=1200,
                    duration_ms=850,
                    outcome=outcome,
                    result_chars=300,
                    now=moment,
                )
            lines = (state / "model-usage-20260911.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()

        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["purpose"], "flush")
        self.assertEqual(first["prompt_chars"], 1200)
        self.assertEqual(first["outcome"], "ok")

    def test_record_prunes_files_older_than_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            stale = state / "model-usage-20260101.jsonl"
            stale.write_text("{}\n", encoding="utf-8")
            foreign = state / "model-usage-notlar.txt"
            foreign.write_text("dokunma", encoding="utf-8")

            model_usage.record(
                state,
                purpose="compile",
                prompt_chars=10,
                duration_ms=5,
                outcome="ok",
                now=datetime.datetime(2026, 9, 11, 12, 0),
            )
            stale_removed = not stale.exists()
            foreign_kept = foreign.exists()

        self.assertTrue(stale_removed)
        self.assertTrue(foreign_kept)

    def test_summary_groups_by_purpose_within_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            now = datetime.datetime(2026, 9, 11, 12, 0)
            model_usage.record(state, purpose="flush", prompt_chars=100,
                               duration_ms=1000, outcome="ok", now=now)
            model_usage.record(state, purpose="flush", prompt_chars=300,
                               duration_ms=3000, outcome="codex-timeout", now=now)
            model_usage.record(state, purpose="compile", prompt_chars=50,
                               duration_ms=500, outcome="ok", now=now)
            # Pencere dışı: 7 günlük özet 10 gün önceyi saymamalı.
            model_usage.record(state, purpose="flush", prompt_chars=999,
                               duration_ms=9, outcome="ok",
                               now=now - datetime.timedelta(days=10))

            summary = model_usage.usage_summary(state, days=7, now=now)

        self.assertEqual(summary["flush"]["calls"], 2)
        self.assertEqual(summary["flush"]["ok"], 1)
        self.assertEqual(summary["flush"]["failed"], 1)
        self.assertEqual(summary["flush"]["prompt_chars"], 400)
        self.assertEqual(summary["compile"]["calls"], 1)

    def test_run_exec_records_failure_outcome_with_purpose(self) -> None:
        # Muhasebe boru hattı uçtan uca: geçersiz CLI yolu bile amaçla kayda düşer.
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.dict(
                os.environ, {"CODEX_CLI_PATH": str(Path(temporary) / "yok.exe")}
            ):
                text, reason = codex_runner.run_exec(
                    "deneme",
                    sandbox="read-only",
                    timeout=5,
                    usage_state_dir=state,
                    purpose="flush",
                )
            entries = [
                json.loads(line)
                for path in state.glob("model-usage-*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertIsNone(text)
        self.assertEqual(reason, "codex-cli-path-invalid")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["purpose"], "flush")
        self.assertEqual(entries[0]["outcome"], "codex-cli-path-invalid")
        self.assertEqual(entries[0]["prompt_chars"], len("deneme"))

    def test_run_exec_without_usage_dir_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(
                os.environ, {"CODEX_CLI_PATH": str(Path(temporary) / "yok.exe")}
            ):
                text, reason = codex_runner.run_exec(
                    "deneme", sandbox="read-only", timeout=5,
                )
            leftovers = list(Path(temporary).glob("model-usage-*"))

        self.assertIsNone(text)
        self.assertEqual(reason, "codex-cli-path-invalid")
        self.assertEqual(leftovers, [])

    def test_run_exec_counts_missing_output_as_ok_for_stage_only_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(codex_runner, "_bounded_exec", return_value=(None, None)):
                result = codex_runner.run_exec(
                    "stage prompt",
                    sandbox="workspace-write",
                    timeout=5,
                    usage_state_dir=state,
                    purpose="compile",
                    usage_output_optional=True,
                )
            entries = [
                json.loads(line)
                for path in state.glob("model-usage-*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result, (None, None))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "ok")

    def test_record_drops_symlinked_daily_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            outside = root / "outside.jsonl"
            outside.write_text("sentinel\n", encoding="utf-8")
            ledger = state / "model-usage-20260911.jsonl"
            try:
                ledger.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            model_usage.record(
                state,
                purpose="compile",
                prompt_chars=10,
                duration_ms=5,
                outcome="ok",
                now=datetime.datetime(2026, 9, 11, 12, 0),
            )
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")
            self.assertTrue(ledger.is_symlink())

    def test_run_exec_keeps_result_when_usage_lock_is_contended(self) -> None:
        cases = (
            ("success", ("answer", None), None),
            ("timeout", (None, "codex-timeout"), None),
            (
                "cleanup-error",
                None,
                process_control.ProcessTreeCleanupError(
                    ["codex"], 1, 4242, OSError("tree still running")
                ),
            ),
        )
        for name, bounded_result, bounded_error in cases:
            with self.subTest(outcome=name), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                captured: dict[str, object] = {}

                def invoke() -> None:
                    try:
                        captured["result"] = codex_runner.run_exec(
                            "prompt",
                            sandbox="read-only",
                            timeout=1,
                            usage_state_dir=state,
                            purpose="test",
                        )
                    except BaseException as exc:  # Thread boundary for cleanup errors.
                        captured["error"] = exc

                patch_kwargs = (
                    {"side_effect": bounded_error}
                    if bounded_error is not None
                    else {"return_value": bounded_result}
                )
                with mock.patch.object(codex_runner, "_bounded_exec", **patch_kwargs):
                    holder = file_lock.locked(state / "model-usage")
                    holder.__enter__()
                    try:
                        thread = threading.Thread(target=invoke, daemon=True)
                        thread.start()
                        thread.join(1)
                        completed_while_locked = not thread.is_alive()
                    finally:
                        holder.__exit__(None, None, None)
                    thread.join(1)

                self.assertTrue(completed_while_locked)
                self.assertFalse(thread.is_alive())
                if bounded_error is None:
                    self.assertEqual(captured.get("result"), bounded_result)
                else:
                    self.assertIs(captured.get("error"), bounded_error)
                self.assertEqual(list(state.glob("model-usage-*.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
