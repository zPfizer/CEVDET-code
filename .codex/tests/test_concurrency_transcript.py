import argparse
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


CODEX_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODEX_DIR / "scripts"))

import flush  # noqa: E402
import jsonl_tail  # noqa: E402


class JsonlTailConcurrencyTests(unittest.TestCase):
    def test_valid_newline_free_record_is_kept(self) -> None:
        records = list(jsonl_tail.iter_records(
            io.BytesIO(b'{"ok":1}'), max_line_bytes=1024,
        ))
        self.assertEqual(records, [(1, {"ok": 1})])

    def test_incomplete_json_tail_is_raised_after_completed_prefix(self) -> None:
        source = io.BytesIO(b'{"ok":1}\n{"message":"unfinished')
        records = []
        with self.assertRaises(jsonl_tail.IncompleteRecord) as context:
            for record in jsonl_tail.iter_records(
                source, max_line_bytes=1024,
                malformed_error="transcript-jsonl-invalid:{line}",
            ):
                records.append(record)
        self.assertEqual(records, [(1, {"ok": 1})])
        self.assertEqual((context.exception.line, context.exception.offset), (2, 9))
        self.assertEqual(context.exception.reason, "json")

    def test_incomplete_utf8_tail_is_deferred(self) -> None:
        source = io.BytesIO(b'{"message":"Turk\xe2\x82')
        with self.assertRaises(jsonl_tail.IncompleteRecord) as context:
            list(jsonl_tail.iter_records(
                source, max_line_bytes=1024,
                malformed_error="transcript-jsonl-invalid:{line}",
            ))
        self.assertEqual(context.exception.reason, "utf8")

    def test_completed_malformed_row_is_still_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "transcript-jsonl-invalid:2") as context:
            list(jsonl_tail.iter_records(
                io.BytesIO(b'{"ok":1}\n{bad}\n'), max_line_bytes=1024,
                malformed_error="transcript-jsonl-invalid:{line}",
            ))
        self.assertNotIsInstance(context.exception, jsonl_tail.IncompleteRecord)

    def test_newline_free_malformed_row_waits_for_writer(self) -> None:
        for data in (b'{bad}', b'{"value":}', b'{"value":true} trailing'):
            with self.subTest(data=data), self.assertRaises(jsonl_tail.IncompleteRecord):
                list(jsonl_tail.iter_records(
                    io.BytesIO(data), max_line_bytes=1024,
                    malformed_error="transcript-jsonl-invalid:{line}",
                ))

    def test_partial_literal_or_number_token_is_deferred(self) -> None:
        for data in (b'{"value":tru', b'{"value":1e', b'{"value":1.'):
            with self.subTest(data=data), self.assertRaises(jsonl_tail.IncompleteRecord):
                list(jsonl_tail.iter_records(
                    io.BytesIO(data), max_line_bytes=1024,
                    malformed_error="transcript-jsonl-invalid:{line}",
                ))

    def test_unterminated_container_and_number_tail_is_deferred(self) -> None:
        for data in (b'{"payload":[]', b'{"payload":{}', b'{"value":1e+}'):
            with self.subTest(data=data), self.assertRaises(jsonl_tail.IncompleteRecord):
                list(jsonl_tail.iter_records(
                    io.BytesIO(data), max_line_bytes=1024,
                    malformed_error="transcript-jsonl-invalid:{line}",
                ))

    def test_flush_reader_propagates_incomplete_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rollout.jsonl"
            path.write_bytes(b'{"role":"user","content":"done"}\n{"role":"user","content":"part')
            self.assertEqual(
                flush.read_transcript_with_coverage(
                    path, max_bytes=None, max_turns=None, include_pending=True,
                ),
                ([('user', 'done')], 1, True),
            )


class FlushTranscriptConcurrencyTests(unittest.TestCase):
    SUMMARY = "\n".join(
        f"## {section}\nKalıcı özet." for section in flush.EXPECTED_SECTIONS
    )

    @staticmethod
    def _write_rows(path: Path, rows: list[tuple[str, str]]) -> None:
        path.write_text(
            "\n".join(
                json.dumps({"role": role, "content": text}, ensure_ascii=False)
                for role, text in rows
            ),
            encoding="utf-8",
        )

    def test_incomplete_tail_keeps_coverage_and_retries_once_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            state.mkdir()
            transcript = root / "rollout.jsonl"
            prefix = json.dumps({"role": "user", "content": "tamam"})
            transcript.write_bytes(
                (prefix + "\n{" + '"role":"user","content":"yarım').encode("utf-8")
            )
            payload = {"session_id": "tail-retry", "transcript_path": str(transcript)}
            args = argparse.Namespace(hook_input=root / "unused", reason="turnend")
            with mock.patch.object(flush, "run_codex", return_value=(self.SUMMARY, None)) as model:
                self.assertEqual(
                    flush.flush_once(args, dt.datetime.now().astimezone(), root, state,
                                     hook_input=payload),
                    1,
                )
                coverage_path = state / f"flush-coverage-{flush._session_key('tail-retry')}.json"
                self.assertEqual(json.loads(coverage_path.read_text())['count'], 1)
                transcript.write_bytes(
                    (prefix + "\n" + json.dumps(
                        {"role": "user", "content": "yarım"}, ensure_ascii=False,
                    )).encode("utf-8")
                )
                with mock.patch.object(flush, "maybe_trigger_compile"):
                    self.assertEqual(
                        flush.flush_once(args, dt.datetime.now().astimezone(), root, state,
                                         hook_input=payload),
                        0,
                    )
            self.assertEqual(model.call_count, 2)
            self.assertEqual(json.loads(coverage_path.read_text())["count"], 2)

    def test_oversize_privacy_row_fails_before_an_earlier_row_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            state.mkdir()
            transcript = root / "rollout.jsonl"
            earlier = json.dumps({"role": "user", "content": "Önceki karar"})
            oversized = json.dumps({
                "role": "user",
                "content": "Bu konuşmada kalsın" + "x" * flush.MAX_TRANSCRIPT_LINE_BYTES,
            }, ensure_ascii=False)
            transcript.write_text(earlier + "\n" + oversized, encoding="utf-8")
            payload = {"session_id": "oversize-privacy", "transcript_path": str(transcript)}
            args = argparse.Namespace(hook_input=root / "unused", reason="turnend")
            with mock.patch.object(flush, "run_codex") as model:
                self.assertEqual(
                    flush.flush_once(
                        args, dt.datetime(2026, 9, 8, 12), root, state,
                        hook_input=payload,
                    ),
                    1,
                )
            self.assertEqual(model.call_count, 0)
            self.assertFalse(list(state.glob("flush-coverage-*")))
            self.assertEqual(
                json.loads((state / "health.json").read_text())['error'],
                "transcript-line-too-large:2",
            )

    def test_changed_same_length_prefix_invalidates_existing_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            state.mkdir()
            transcript = root / "rollout.jsonl"
            self._write_rows(transcript, [("user", "Eski karar"), ("user", "İkinci karar")])
            payload = {"session_id": "prefix-change", "transcript_path": str(transcript)}
            args = argparse.Namespace(hook_input=root / "unused", reason="turnend")
            prompts: list[str] = []

            def summarize(prompt: str, *_args, **_kwargs):
                prompts.append(prompt)
                return self.SUMMARY, None

            with mock.patch.object(flush, "run_codex", side_effect=summarize), \
                 mock.patch.object(flush, "maybe_trigger_compile"), \
                 mock.patch("worker_supervisor.ensure_supervisor"):
                self.assertEqual(
                    flush.flush_once(args, dt.datetime.now().astimezone(), root, state,
                                     hook_input=payload),
                    0,
                )
                self._write_rows(transcript, [("user", "Yeni karar"), ("user", "İkinci karar")])
                self.assertEqual(
                    flush.flush_once(args, dt.datetime.now().astimezone(), root, state,
                                     hook_input=payload),
                    0,
                )
        self.assertEqual(len(prompts), 2)
        self.assertIn("Yeni karar", prompts[1])

    def test_truncated_transcript_does_not_keep_stale_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            state.mkdir()
            transcript = root / "rollout.jsonl"
            self._write_rows(transcript, [("user", f"Karar {index:02}") for index in range(35)])
            payload = {"session_id": "rotation", "transcript_path": str(transcript)}
            args = argparse.Namespace(hook_input=root / "unused", reason="turnend")
            prompts: list[str] = []

            def summarize(prompt: str, *_args, **_kwargs):
                prompts.append(prompt)
                return self.SUMMARY, None

            with mock.patch.object(flush, "run_codex", side_effect=summarize), \
                 mock.patch.object(flush, "maybe_trigger_compile"), \
                 mock.patch("worker_supervisor.ensure_supervisor"):
                self.assertEqual(
                    flush.flush_once(args, dt.datetime.now().astimezone(), root, state,
                                     hook_input=payload),
                    0,
                )
                transcript.write_text(
                    json.dumps({"role": "user", "content": "Yeni nesil"}),
                    encoding="utf-8",
                )
                self.assertEqual(
                    flush.flush_once(args, dt.datetime.now().astimezone(), root, state,
                                     hook_input=payload),
                    0,
                )
        self.assertEqual(len(prompts), 2)
        self.assertIn("Yeni nesil", prompts[1])

    def test_identical_adjacent_batches_are_not_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            state.mkdir()
            transcript = root / "rollout.jsonl"
            self._write_rows(transcript, [("user", "Tekrar") for _ in range(35)])
            payload = {"session_id": "same-batch", "transcript_path": str(transcript)}
            args = argparse.Namespace(hook_input=root / "unused", reason="turnend")
            with mock.patch.object(flush, "run_codex", return_value=(self.SUMMARY, None)) as model, \
                 mock.patch.object(flush, "maybe_trigger_compile"), \
                 mock.patch("worker_supervisor.ensure_supervisor"):
                self.assertEqual(
                    flush.flush_once(args, dt.datetime.now().astimezone(), root, state,
                                     hook_input=payload),
                    0,
                )
                self.assertEqual(
                    flush.flush_once(args, dt.datetime.now().astimezone(), root, state,
                                     hook_input=payload),
                    0,
                )
            coverage_path = state / f"flush-coverage-{flush._session_key('same-batch')}.json"
            coverage = json.loads(coverage_path.read_text())
            daily_files = list((root / "daily").glob("*.md"))
            daily_markers = daily_files[0].read_text(encoding="utf-8").count("<!-- flush:")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(coverage["count"], 35)
        self.assertEqual(len(daily_files), 1)
        self.assertEqual(daily_markers, 2)

    def test_batch_receipt_recovers_when_first_coverage_write_crashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            state.mkdir()
            transcript = root / "rollout.jsonl"
            self._write_rows(transcript, [("user", "İlk karar")])
            payload = {"session_id": "coverage-crash", "transcript_path": str(transcript)}
            args = argparse.Namespace(hook_input=root / "unused", reason="turnend")
            coverage_path = state / f"flush-coverage-{flush._session_key('coverage-crash')}.json"
            original_atomic = flush.atomic_write_json

            def crash_coverage(path, value, *extra, **kwargs):
                if path == coverage_path:
                    raise OSError("coverage crash")
                return original_atomic(path, value, *extra, **kwargs)

            with mock.patch.object(flush, "run_codex", return_value=(self.SUMMARY, None)) as model, \
                 mock.patch.object(flush, "maybe_trigger_compile"), \
                 mock.patch("worker_supervisor.ensure_supervisor"):
                with mock.patch.object(flush, "atomic_write_json", side_effect=crash_coverage):
                    with self.assertRaisesRegex(OSError, "coverage crash"):
                        flush.flush_once(
                            args, dt.datetime(2026, 9, 8, 12), root, state,
                            hook_input=payload,
                        )
                self.assertFalse(coverage_path.exists())
                self._write_rows(transcript, [("user", "İlk karar"), ("user", "İkinci karar")])
                self.assertEqual(
                    flush.flush_once(
                        args, dt.datetime(2026, 9, 8, 12), root, state,
                        hook_input=payload,
                    ),
                    0,
                )
                self.assertEqual(model.call_count, 1)
                self.assertEqual(json.loads(coverage_path.read_text())["count"], 1)
                self.assertEqual(
                    flush.flush_once(
                        args, dt.datetime(2026, 9, 8, 12), root, state,
                        hook_input=payload,
                    ),
                    0,
                )
            daily_files = list((root / "daily").glob("*.md"))
            daily_markers = daily_files[0].read_text(encoding="utf-8").count("<!-- flush:")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(daily_markers, 2)

    def test_active_index_keeps_transcript_text_out_of_persistent_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            state.mkdir()
            path = root / "rollout.jsonl"
            sentinel = "benzersiz-transkript-metni-" + ("x" * 200)
            self._write_rows(path, [("user", sentinel), ("assistant", "yanıt")])
            index = flush.transcript_index.open_or_update(
                state,
                "metadata-only",
                path,
                hashes=frozenset(),
                parser=flush._message_parts_for_index,
                text_from_content=flush._text_from_content,
                max_line_bytes=flush.MAX_TRANSCRIPT_LINE_BYTES,
            )
            persisted = index.path.read_text(encoding="utf-8")
        self.assertEqual(index.rows[0]["filtered_length"], len(sentinel))
        self.assertNotIn(sentinel, persisted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
