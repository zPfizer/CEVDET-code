from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import transcript_index
import doctor
import memory_ledger


def parse_record(record):
    role = record.get("role")
    return role if role in {"user", "assistant"} else None, record.get("content"), role in {"user", "assistant"}


def text_from_content(content):
    return content if isinstance(content, str) else ""


class TranscriptIndexTests(unittest.TestCase):
    def _index(self, state, session, source, hashes=frozenset(), max_line_bytes=1024):
        return transcript_index.open_or_update(
            state,
            session,
            source,
            hashes=hashes,
            parser=parse_record,
            text_from_content=text_from_content,
            max_line_bytes=max_line_bytes,
        )

    def test_state_has_only_hashed_source_metadata_and_reuses_append_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(
                "\n".join(json.dumps({"role": "user", "content": f"turn-{n}"}) for n in range(2)) + "\n",
                encoding="utf-8",
            )
            first = self._index(state, "session", source)
            source.write_text(
                source.read_text(encoding="utf-8")
                + json.dumps({"role": "assistant", "content": "tail"})
                + "\n",
                encoding="utf-8",
            )
            second = self._index(state, "session", source)
            payload = json.loads(first.path.read_text(encoding="utf-8"))
            encoded = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(len(first.chunks), 2)
        self.assertEqual(len(second.chunks), 3)
        self.assertEqual(second.counters["json_decodes_last"], 1)
        self.assertNotIn("turn-0", encoded)
        self.assertNotIn("rollout.jsonl", encoded)
        self.assertNotIn(str(source), encoded)
        self.assertEqual(second.coverage_digest(2), first.coverage_digest(2))

    def test_same_stat_rewrite_reindexes_and_selected_row_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(json.dumps({"role": "user", "content": "old"}) + "\n", encoding="utf-8")
            first = self._index(state, "session", source)
            stat = source.stat()
            source.write_text(json.dumps({"role": "user", "content": "new"}) + "\n", encoding="utf-8")
            source.touch()
            if source.stat().st_size != stat.st_size:
                self.skipTest("fixture rewrite changed line size")
            second = self._index(state, "session", source)
            selected = transcript_index.read_selected_rows(
                second,
                source,
                [0],
                hashes=frozenset(),
                parser=parse_record,
                text_from_content=text_from_content,
                max_line_bytes=1024,
            )
            source.write_text(json.dumps({"role": "user", "content": "drift"}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "transcript-index-source-drift"):
                transcript_index.read_selected_rows(
                    second,
                    source,
                    [0],
                    hashes=frozenset(),
                    parser=parse_record,
                    text_from_content=text_from_content,
                    max_line_bytes=1024,
                )

        self.assertNotEqual(first.state["source_sha256"], second.state["source_sha256"])
        self.assertEqual(selected, {0: ("user", "new")})

    def test_late_session_only_clears_already_indexed_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(
                "\n".join(
                    json.dumps({"role": role, "content": text})
                    for role, text in (("user", "keep"), ("assistant", "reply"))
                )
                + "\n",
                encoding="utf-8",
            )
            first = self._index(state, "session", source)
            with source.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"role": "user", "content": "Bu konuşmada kalsın."}) + "\n")
            second = self._index(state, "session", source)

        self.assertEqual(len(first.chunks), 2)
        self.assertEqual(second.chunks, ())
        self.assertTrue(second.session_only)
        self.assertTrue(all(not row["retained"] for row in second.rows))
        self.assertEqual(second.counters["json_decodes_last"], 1)

    def test_standalone_do_not_save_removes_previous_exchange_without_old_decode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(
                "\n".join(
                    json.dumps({"role": role, "content": text})
                    for role, text in (("user", "old"), ("assistant", "old reply"))
                )
                + "\n",
                encoding="utf-8",
            )
            self._index(state, "session", source)
            with source.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"role": "user", "content": "Bunu kaydetme, lütfen."}) + "\n")
                handle.write(json.dumps({"role": "user", "content": "new"}) + "\n")
            index = self._index(state, "session", source)

        self.assertEqual([row["retained"] for row in index.rows], [False, False, False, True])
        self.assertEqual(index.rows[2]["privacy_classification"], "do-not-save")
        self.assertEqual(index.counters["json_decodes_last"], 2)

    def test_policy_bump_rebuilds_legacy_do_not_save_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            courtesy = "Bunu kaydetme, lütfen."
            source.write_text(
                "\n".join(
                    json.dumps({"role": role, "content": text})
                    for role, text in (
                        ("user", "old"),
                        ("assistant", "old reply"),
                        ("user", courtesy),
                        ("user", "new"),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            actual_directive = memory_ledger.memory_directive

            def legacy_directive(text):
                if text == courtesy:
                    return memory_ledger.MemoryDirective("do-not-save", text)
                return actual_directive(text)

            with (
                mock.patch.object(transcript_index, "POLICY_VERSION", "persistent-turns-v1"),
                mock.patch.object(memory_ledger, "memory_directive", side_effect=legacy_directive),
            ):
                legacy = self._index(state, "session", source)
            self.assertEqual(legacy.state["policy_version"], "persistent-turns-v1")
            self.assertEqual([row["retained"] for row in legacy.rows], [True, True, False, True])

            rebuilt = self._index(state, "session", source)

        self.assertEqual(rebuilt.state["policy_version"], transcript_index.POLICY_VERSION)
        self.assertEqual([row["retained"] for row in rebuilt.rows], [False, False, False, True])
        self.assertEqual(rebuilt.counters["json_decodes_last"], 4)

    def test_partial_last_line_is_replayed_when_the_writer_finishes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_bytes(
                (json.dumps({"role": "user", "content": "first"}) + "\n").encode()
                + b'{"role":"user","content":"partial'
            )
            first = self._index(state, "session", source)
            with source.open("ab") as handle:
                handle.write(b'"}\n')
            second = self._index(state, "session", source)

        self.assertIsNotNone(first.partial_tail)
        self.assertFalse(first.partial_tail["complete"])
        self.assertEqual(len(first.chunks), 1)
        self.assertIsNone(second.partial_tail)
        self.assertEqual([chunk.length for chunk in second.chunks], [5, 7])
        self.assertEqual(second.counters["json_decodes_last"], 1)

    def test_index_distinguishes_incomplete_utf8_from_invalid_eof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / '.state'
            source = root / 'rollout.jsonl'
            source.write_bytes(b'{"role":"user","content":"\xe2\x82')
            pending = self._index(state, 'session', source)
            self.assertEqual(pending.counters['partial_lines'], 1)
            self.assertEqual(pending.chunks, ())

            source.write_bytes(b'{"role":"user","content":"\xff')
            with self.assertRaisesRegex(ValueError, 'transcript-jsonl-invalid:1'):
                self._index(state, 'session', source)

    def test_valid_newline_free_tail_is_reparsed_without_decoding_the_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(
                "\n".join(
                    json.dumps({"role": "user", "content": value})
                    for value in ("old-1", "old-2")
                ),
                encoding="utf-8",
            )
            self._index(state, "session", source)
            with source.open("a", encoding="utf-8") as handle:
                handle.write("\n" + json.dumps({"role": "user", "content": "tail"}))
            index = self._index(state, "session", source)

        self.assertEqual([chunk.length for chunk in index.chunks], [5, 5, 4])
        self.assertEqual(index.counters["json_decodes_last"], 3)
        self.assertEqual(index.counters["json_decodes_total"], 3)

    def test_growing_newline_free_directive_rebuilds_before_reapplying_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(
                "\n".join(
                    json.dumps({"role": role, "content": text})
                    for role, text in (
                        ("user", "first"),
                        ("assistant", "first reply"),
                        ("user", "second"),
                        ("assistant", "second reply"),
                    )
                )
                + "\n"
                + json.dumps({"role": "user", "content": "Bunu kaydetme."}),
                encoding="utf-8",
            )
            self._index(state, "session", source)
            with source.open("a", encoding="utf-8") as handle:
                handle.write("\n" + json.dumps({"role": "user", "content": "third"}))
            index = self._index(state, "session", source)
            rows = transcript_index.read_selected_rows(
                index,
                source,
                [row_id for row_id, row in enumerate(index.rows) if row["retained"]],
                hashes=frozenset(),
                parser=parse_record,
                text_from_content=text_from_content,
                max_line_bytes=1024,
            )

        self.assertEqual(rows, {0: ("user", "first"), 1: ("assistant", "first reply"), 5: ("user", "third")})

    def test_appended_oversized_line_reports_its_source_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(json.dumps({"role": "user", "content": "ok"}) + "\n", encoding="utf-8")
            self._index(state, "session", source)
            with source.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"role": "user", "content": "x" * 100}) + "\n")
            with self.assertRaisesRegex(ValueError, "transcript-line-too-large:2"):
                self._index(state, "session", source, max_line_bytes=80)

    def test_index_write_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "rollout.jsonl"
            source.write_text(json.dumps({"role": "user", "content": "safe"}) + "\n", encoding="utf-8")
            with mock.patch.object(transcript_index, "atomic_write_json", side_effect=OSError("read-only")):
                with self.assertRaisesRegex(ValueError, "transcript-index-write-failed"):
                    self._index(root / ".state", "session", source)

    def test_prefix_hash_reuses_existing_full_digest_without_read_bytes_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "rollout.jsonl"
            source.write_bytes(b"prefix")
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected copy")):
                size, full, prefix = transcript_index._hash_source(source, 100)

        self.assertEqual(size, 6)
        self.assertEqual(prefix, full)

    def test_invalid_metadata_is_rebuilt_instead_of_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(json.dumps({"role": "user", "content": "safe"}) + "\n", encoding="utf-8")
            first = self._index(state, "session", source)
            payload = json.loads(first.path.read_text(encoding="utf-8"))
            payload["rows"][0]["filtered_chunks"][0]["sha256"] = "0" * 64
            first.path.write_text(json.dumps(payload), encoding="utf-8")
            rebuilt = self._index(state, "session", source)

        self.assertEqual(rebuilt.counters["json_decodes_last"], 1)
        self.assertEqual(rebuilt.rows[0]["filtered_chunks"][0]["length"], 4)

    def test_suppression_revision_reindexes_old_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(json.dumps({"role": "user", "content": "sensitive"}) + "\n", encoding="utf-8")
            first = self._index(state, "session", source)
            second = self._index(state, "session", source, hashes=frozenset({"a" * 64}))

        self.assertNotEqual(first.state["suppression_revision"], second.state["suppression_revision"])
        self.assertEqual(second.counters["json_decodes_last"], 1)

    def test_coverage_digest_includes_metadata_beyond_the_chunk_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(json.dumps({"role": "user", "content": "same"}) + "\n", encoding="utf-8")
            first = self._index(state, "session", source)
            source.write_text(json.dumps({"role": "user", "content": "same"}) + "\n", encoding="utf-8")
            second = self._index(state, "session", source)

        self.assertEqual(len(first.chunks), len(second.chunks))
        self.assertEqual(first.coverage_digest(1), second.coverage_digest(1))

    def test_index_state_passes_existing_state_privacy_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(json.dumps({"role": "user", "content": "private"}) + "\n", encoding="utf-8")
            self._index(state, "session", source)
            check = doctor._state_privacy_check(doctor.Context(state_dir=state))

        self.assertEqual(check.status, "OK")

    def test_derived_rows_and_chunks_are_cached_for_repeat_lookups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "rollout.jsonl"
            source.write_text(
                "\n".join(json.dumps({"role": "user", "content": str(n)}) for n in range(4)) + "\n",
                encoding="utf-8",
            )
            index = self._index(root / ".state", "session", source)

        self.assertIs(index.rows, index.rows)
        self.assertIs(index.chunks, index.chunks)
        self.assertEqual(index.coverage_digest(4), index.coverage_digest(4))

    def test_three_fresh_processes_reuse_indexed_prefix(self):
        child = """
import json, sys
from pathlib import Path
import transcript_index

def parse(record):
    role = record.get('role')
    return role if role in {'user', 'assistant'} else None, record.get('content'), role in {'user', 'assistant'}

index = transcript_index.open_or_update(
    Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]),
    hashes=frozenset(), parser=parse,
    text_from_content=lambda value: value if isinstance(value, str) else '',
    max_line_bytes=1024,
)
print(json.dumps(index.counters))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            source = root / "rollout.jsonl"
            source.write_text(
                "\n".join(json.dumps({"role": "user", "content": f"turn-{n}"}) for n in range(65)) + "\n",
                encoding="utf-8",
            )
            environment = dict(os.environ, PYTHONPATH=str(CODEX_DIR / "scripts"))
            counters = []
            for _ in range(3):
                result = subprocess.run(
                    [sys.executable, "-c", child, str(state), "session", str(source)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                counters.append(json.loads(result.stdout))

        self.assertEqual(counters[0]["json_decodes_last"], 65)
        self.assertEqual(counters[1]["json_decodes_last"], 0)
        self.assertEqual(counters[2]["json_decodes_last"], 0)
        self.assertEqual(counters[-1]["json_decodes_total"], 65)

    def test_three_fresh_flush_workers_drain_65_messages_without_redecoding_prefix(self):
        child = """
import argparse, datetime as dt, json, sys
from pathlib import Path
import flush, worker_supervisor

summary = '\\n'.join(f'## {section}\\nSaved.' for section in flush.EXPECTED_SECTIONS)
flush.run_codex = lambda *_args, **_kwargs: (summary, None)
flush.maybe_trigger_compile = lambda *_args, **_kwargs: False
worker_supervisor.ensure_supervisor = lambda *_args, **_kwargs: None
root = Path(sys.argv[1])
state = root / '.state'
source = root / 'rollout.jsonl'
payload = {'session_id': 'worker-session', 'transcript_path': str(source)}
result = flush.flush_once(
    argparse.Namespace(reason='turnend', hook_input=source),
    dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc),
    root, state, hook_input=payload,
)
index = json.loads(next(state.glob('flush-index-*.json')).read_text())
coverage = json.loads(next(state.glob('flush-coverage-*.json')).read_text())
print(json.dumps({'result': result, 'index': index['counters'], 'coverage': coverage['count']}))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "rollout.jsonl"
            source.write_text(
                "\n".join(json.dumps({"role": "user", "content": f"turn-{n}"}) for n in range(65)) + "\n",
                encoding="utf-8",
            )
            environment = dict(os.environ, PYTHONPATH=str(CODEX_DIR / "scripts"))
            observations = []
            for _ in range(3):
                result = subprocess.run(
                    [sys.executable, "-c", child, str(root)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                observations.append(json.loads(result.stdout))

        self.assertEqual([item["result"] for item in observations], [0, 0, 0])
        self.assertEqual([item["coverage"] for item in observations], [30, 60, 65])
        self.assertEqual(observations[0]["index"]["json_decodes_total"], 65)
        self.assertEqual(observations[1]["index"]["json_decodes_total"], 65)
        self.assertEqual(observations[2]["index"]["json_decodes_total"], 65)
        self.assertEqual(observations[2]["index"]["json_decodes_total"], 65)


if __name__ == "__main__":
    unittest.main()
