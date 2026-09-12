import datetime
import contextlib
import io
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
    def test_cli_distinguishes_empty_complete_and_incomplete_ledgers(self) -> None:
        valid = json.dumps({"schema": 1, "purpose": "probe", "prompt_chars": 10,
                            "duration_ms": 5, "outcome": "ok"}).encode() + b"\n"
        for name, content, code, calls in (
            ("no-file", None, 0, 0),
            ("empty-file", b"", 0, 0),
            ("valid", valid, 0, 1),
            ("valid-crlf", valid[:-1] + b"\r\n", 0, 1),
            ("missing-newline", valid[:-1], 1, 1),
            ("missing-lf", valid[:-1] + b"\r", 1, 1),
            ("corrupt", b"{private-invalid-record\n", 1, 0),
            ("invalid-encoding", b"\xff\n", 1, 0),
            ("wrong-schema", b'{"schema": 2}\n', 1, 0),
            ("partial", valid + b"{private-invalid-record\n", 1, 1),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                ledger = state / f"model-usage-{datetime.datetime.now():%Y%m%d}.jsonl"
                if content is not None:
                    ledger.write_bytes(content)
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    result = model_usage.main(["--state-dir", str(state), "--days", "1"])
                self.assertEqual(result, code)
                if code:
                    self.assertIn("raporu eksik", errors.getvalue())
                    self.assertNotIn("kayıtlı model çağrısı yok", output.getvalue())
                    if calls:
                        self.assertIn("okunabilen kayıtlar (eksik)", output.getvalue())
                    else:
                        self.assertIn("doğrulanamadı", output.getvalue())
                else:
                    self.assertEqual(errors.getvalue(), "")
                    if not calls:
                        self.assertIn("kayıtlı model çağrısı yok", output.getvalue())
                if calls:
                    self.assertIn("| probe | 1 | 1 | 0 |", output.getvalue())
                self.assertNotIn("private-invalid", output.getvalue() + errors.getvalue())
                if content is not None:
                    self.assertEqual(ledger.read_bytes(), content)

    def test_cli_reports_file_and_directory_read_failures(self) -> None:
        for operation in ("open", "iterdir"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                ledger = state / f"model-usage-{datetime.datetime.now():%Y%m%d}.jsonl"
                ledger.write_bytes(b"{}\n")
                original = getattr(Path, operation)
                def fail_selected(path, *args, **kwargs):
                    if path == (ledger if operation == "open" else state):
                        raise PermissionError("private access failure")
                    return original(path, *args, **kwargs)
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(Path, operation, fail_selected), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = model_usage.main(["--state-dir", str(state), "--days", "1"])
                self.assertEqual(code, 1)
                self.assertIn("doğrulanamadı", output.getvalue())
                self.assertIn("raporu eksik", errors.getvalue())
                self.assertNotIn("private access", output.getvalue() + errors.getvalue())

    def test_oversized_line_is_skipped_as_one_record_before_valid_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            valid = json.dumps({"schema": 1, "purpose": "probe", "prompt_chars": 10,
                                "duration_ms": 5, "outcome": "ok"}).encode() + b"\n"
            (state / "model-usage-20260911.jsonl").write_bytes(
                b"x" * (model_usage.MAX_RECORD_BYTES + 2) + valid + valid
            )
            summary, incomplete = model_usage.usage_summary(
                state, days=1, now=datetime.datetime(2026, 9, 11, 12, 0),
            )
        self.assertTrue(incomplete)
        self.assertEqual(summary["probe"]["calls"], 1)

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

            summary, incomplete = model_usage.usage_summary(state, days=7, now=now)

        self.assertEqual(summary["flush"]["calls"], 2)
        self.assertEqual(summary["flush"]["ok"], 1)
        self.assertEqual(summary["flush"]["failed"], 1)
        self.assertEqual(summary["flush"]["prompt_chars"], 400)
        self.assertEqual(summary["compile"]["calls"], 1)
        self.assertFalse(incomplete)

    def test_summary_excludes_future_dated_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            now = datetime.datetime(2026, 9, 11, 12, 0)
            model_usage.record(
                state,
                purpose="compile",
                prompt_chars=10,
                duration_ms=5,
                outcome="ok",
                now=now + datetime.timedelta(days=10),
            )

            summary, incomplete = model_usage.usage_summary(state, days=7, now=now)

        self.assertEqual(summary, {})
        self.assertFalse(incomplete)

    def test_summary_rejects_window_beyond_retention(self) -> None:
        with self.assertRaisesRegex(ValueError, "summary-window-out-of-retention"):
            model_usage.usage_summary(
                Path("unused"), days=model_usage.KEEP_DAYS + 1,
                now=datetime.datetime(2026, 9, 11, 12, 0),
            )

    def test_record_separates_an_incomplete_jsonl_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            ledger = state / "model-usage-20260911.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text('{"schema": 1, "outcome":', encoding="utf-8")
            model_usage.record(
                state,
                purpose="compile",
                prompt_chars=10,
                duration_ms=5,
                outcome="ok",
                now=datetime.datetime(2026, 9, 11, 12, 0),
            )
            lines = ledger.read_bytes().splitlines()
            summary, incomplete = model_usage.usage_summary(
                state, days=7, now=datetime.datetime(2026, 9, 11, 12, 0)
            )

        self.assertEqual(len(lines), 2)
        self.assertEqual(summary["compile"]["calls"], 1)
        self.assertTrue(incomplete)

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
            outside.write_text(json.dumps({
                "schema": 1,
                "purpose": "outside",
                "prompt_chars": 999,
                "duration_ms": 1,
                "outcome": "ok",
            }) + "\n", encoding="utf-8")
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
            summary, incomplete = model_usage.usage_summary(
                state, days=7, now=datetime.datetime(2026, 9, 11, 12, 0)
            )
            self.assertEqual(summary, {})
            self.assertTrue(incomplete)
            self.assertEqual(
                outside.read_text(encoding="utf-8"),
                json.dumps({
                    "schema": 1,
                    "purpose": "outside",
                    "prompt_chars": 999,
                    "duration_ms": 1,
                    "outcome": "ok",
                }) + "\n",
            )
            self.assertTrue(ledger.is_symlink())

    def test_record_and_summary_drop_hardlinked_daily_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            outside = root / "outside.jsonl"
            outside.write_text("sentinel\n", encoding="utf-8")
            ledger = state / "model-usage-20260911.jsonl"
            try:
                os.link(outside, ledger)
            except OSError as exc:
                self.skipTest(f"hard link unavailable: {exc}")

            model_usage.record(
                state,
                purpose="compile",
                prompt_chars=10,
                duration_ms=5,
                outcome="ok",
                now=datetime.datetime(2026, 9, 11, 12, 0),
            )
            summary, incomplete = model_usage.usage_summary(
                state, days=7, now=datetime.datetime(2026, 9, 11, 12, 0)
            )
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")
            self.assertEqual(summary, {})
            self.assertTrue(incomplete)

    def test_record_drops_daily_ledger_swapped_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            ledger = state / "model-usage-20260911.jsonl"
            ledger.write_text("original\n", encoding="utf-8")
            canary = root / "canary.jsonl"
            canary.write_text("canary\n", encoding="utf-8")
            original_open = Path.open

            def swap_before_open(path: Path, *args: object, **kwargs: object):
                if path == ledger and args and args[0] == "a+b":
                    os.replace(canary, ledger)
                return original_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=swap_before_open):
                model_usage.record(
                    state,
                    purpose="compile",
                    prompt_chars=10,
                    duration_ms=5,
                    outcome="ok",
                    now=datetime.datetime(2026, 9, 11, 12, 0),
                )

            self.assertEqual(ledger.read_text(encoding="utf-8"), "canary\n")

    def test_record_drops_symlinked_lock_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            outside = root / "outside.lock"
            outside.write_text("sentinel", encoding="utf-8")
            lock = state / "model-usage.lock"
            try:
                lock.symlink_to(outside)
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

            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")
            self.assertEqual(list(state.glob("model-usage-*.jsonl")), [])

    def test_record_drops_linked_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            outside_ledger = outside / "model-usage-20260911.jsonl"
            outside_ledger.write_text(json.dumps({
                "schema": 1,
                "purpose": "outside",
                "prompt_chars": 999,
                "duration_ms": 1,
                "outcome": "ok",
            }) + "\n", encoding="utf-8")
            before = outside_ledger.read_text(encoding="utf-8")
            state = root / "state"
            try:
                state.symlink_to(outside, target_is_directory=True)
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

            summary, incomplete = model_usage.usage_summary(
                state, days=7, now=datetime.datetime(2026, 9, 11, 12, 0)
            )
            self.assertEqual(summary, {})
            self.assertTrue(incomplete)
            self.assertEqual(outside_ledger.read_text(encoding="utf-8"), before)

    def test_summary_skips_records_with_invalid_numeric_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            ledger = state / "model-usage-20260911.jsonl"
            valid = {
                "schema": 1,
                "purpose": "compile",
                "prompt_chars": 10,
                "duration_ms": 5,
                "outcome": "ok",
            }
            invalid_prompt = {**valid, "prompt_chars": "not-a-number"}
            invalid_duration = {**valid, "duration_ms": "not-a-number"}
            huge_prompt = {**valid, "prompt_chars": 10 ** 3000}
            negative_duration = {**valid, "duration_ms": -1}
            ledger.write_text(
                "\n".join(json.dumps(item) for item in (
                    valid, invalid_prompt, invalid_duration,
                    huge_prompt, negative_duration,
                )) + "\n",
                encoding="utf-8",
            )

            summary, incomplete = model_usage.usage_summary(
                state, days=7, now=datetime.datetime(2026, 9, 11, 12, 0)
            )

        self.assertEqual(summary["compile"]["calls"], 1)
        self.assertEqual(summary["compile"]["prompt_chars"], 10)
        self.assertEqual(summary["compile"]["duration_ms"], 5)
        self.assertTrue(incomplete)

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
