from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


CODEX_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = CODEX_DIR / "scripts"
HOOKS_DIR = CODEX_DIR / "hooks"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(HOOKS_DIR))

import flush  # noqa: E402
import worker_supervisor  # noqa: E402


class FlushResultContractTests(unittest.TestCase):
    """flush_once sıfır dışı dönerse iş başarı sayılmamalı ve girdi silinmemeli."""

    def test_archived_codex_transcript_is_saved_through_the_real_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / 'vault'
            state = vault / '.state'
            state.mkdir(parents=True)
            codex_home = Path(temporary) / 'codex'
            archived = codex_home / 'archived_sessions'
            archived.mkdir(parents=True)
            session_id = '01a06eb1-6102-74f0-afc3-77414554f236'
            name = f'rollout-2026-09-05T02-11-53-{session_id}.jsonl'
            original = codex_home / 'sessions/2026/09/05' / name
            moved = archived / name
            moved.write_text(json.dumps({'role': 'user', 'content': 'Kapanıştaki son karar.'}), encoding='utf-8')
            hook_input = state / 'hookin-archive.json'
            source_path = ('\\\\?\\' + str(original)) if os.name == 'nt' else str(original)
            hook_input.write_text(json.dumps({'session_id': session_id, 'transcript_path': source_path}), encoding='utf-8')
            job = {'kind': 'flush', 'payload': {'hook_input': str(hook_input),
                'reason': 'sessionend', 'event_iso': '2026-09-05T12:00:00+03:00'}}
            summary = '\n'.join(f'## {s}\nKapanıştaki son karar.' for s in flush.EXPECTED_SECTIONS)
            with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), mock.patch.object(
                flush, 'run_codex', return_value=(summary, None)
            ), mock.patch.object(flush, 'maybe_trigger_compile'):
                worker_supervisor._dispatch_job(vault, state, job)
            self.assertIn('Kapanıştaki son karar.', (vault / 'daily/2026-09-05.md').read_text('utf-8'))
            self.assertTrue(moved.exists())
            self.assertTrue(hook_input.exists(), "Input is retained until the success receipt is durable")

    def test_archive_fallback_does_not_replace_an_unrelated_or_existing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archived = root / 'archived_sessions'
            archived.mkdir()
            name = 'rollout-2026-09-05T02-11-53-01a06eb1-6102-74f0-afc3-77414554f236.jsonl'
            (archived / name).write_text('archive', encoding='utf-8')
            existing = root / 'sessions' / name
            existing.parent.mkdir()
            existing.write_text('original', encoding='utf-8')
            for source, session_id in ((existing, '01a06eb1-6102-74f0-afc3-77414554f236'),
                (root / 'unrelated' / name, '01a06eb1-6102-74f0-afc3-77414554f236'),
                (root / 'sessions/missing' / name, 'different-session')):
                hook_input = root / 'input.json'
                hook_input.write_text(json.dumps({'session_id': session_id, 'transcript_path': str(source)}), encoding='utf-8')
                with mock.patch.dict(os.environ, {'CODEX_HOME': str(root)}):
                    self.assertEqual(flush.load_hook_input(hook_input)['transcript_path'], str(source))

    def test_non_zero_flush_result_raises_and_keeps_hook_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            state = root / "state"
            vault.mkdir()
            state.mkdir()
            hook_input = state / "hookin-contract.json"
            hook_input.write_text(json.dumps({"session_id": "s"}), encoding="utf-8")
            job = {
                "kind": "flush",
                "payload": {
                    "hook_input": str(hook_input),
                    "reason": "sessionend",
                    "event_iso": "2026-08-31T13:00:00+03:00",
                },
            }

            with mock.patch.object(flush, "flush_once", return_value=2):
                with self.assertRaises(RuntimeError) as caught:
                    worker_supervisor._dispatch_job(vault, state, job)

            self.assertIn("worker-flush-failed", str(caught.exception))
            self.assertTrue(hook_input.exists(), "başarısız flush girdisi silinmemeli")

    def test_real_synthesis_failure_keeps_worker_input_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / '.state'
            state.mkdir()
            transcript = vault / 'source.jsonl'
            transcript.write_text(json.dumps({
                'role': 'user', 'content': 'Karar: çalışma saati 08:40.'
            }), encoding='utf-8')
            hook_input = state / 'hookin-retry.json'
            hook_input.write_text(json.dumps({
                'session_id': 'retry', 'transcript_path': str(transcript)
            }), encoding='utf-8')
            job = {'kind': 'flush', 'payload': {
                'hook_input': str(hook_input), 'reason': 'sessionend',
                'event_iso': '2026-09-05T12:00:00+03:00',
            }}
            with mock.patch.object(flush, 'run_codex', return_value=(None, 'codex-timeout')):
                with self.assertRaisesRegex(RuntimeError, 'worker-flush-failed'):
                    worker_supervisor._dispatch_job(vault, state, job)
            self.assertTrue(hook_input.exists())
            self.assertFalse((vault / 'daily').exists())

    def test_malformed_kind_is_quarantined_without_blocking_valid_sibling(self) -> None:
        for malformed_kind in ([], {}, None, "unknown"):
            with self.subTest(kind=malformed_kind), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                valid_path = worker_supervisor.enqueue_job(
                    state, "flush", {"source": "valid"},
                    start_supervisor=False, now=100,
                )
                malformed = json.loads(valid_path.read_text(encoding="utf-8"))
                malformed.update(job_id="0" * 32, kind=malformed_kind)
                malformed_path = state / "worker-jobs" / "pending" / ("job-" + "0" * 32 + ".json")
                worker_supervisor.atomic_write_json(malformed_path, malformed)

                claimed = worker_supervisor._claim_next_job(state, now=100)
                self.assertIsNotNone(claimed)
                running, job = claimed
                self.assertEqual(job["payload"], {"source": "valid"})
                worker_supervisor._finish_job(state, running, job, status="succeeded", now=101)

                report = worker_supervisor.inspect_worker_queue(state)
                self.assertEqual(report["invalid"], 0)
                self.assertEqual(report["counts"]["quarantined"], 1)
                self.assertEqual(report["counts"]["succeeded"], 1)
                tombstone = next((state / "worker-jobs" / "quarantined").glob("*.json"))
                self.assertEqual(
                    json.loads(tombstone.read_text(encoding="utf-8"))["reason_code"],
                    "worker-job-schema-invalid",
                )

    def test_unhashable_quarantine_reason_is_reported_as_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            quarantine = state / "worker-jobs" / "quarantined"
            quarantine.mkdir(parents=True)
            payload = b"original malformed job"
            payload_path = quarantine / ("job-" + "a" * 32 + ".payload")
            payload_path.write_bytes(payload)
            tombstone = quarantine / ("job-" + "a" * 32 + ".json")
            tombstone.write_text(json.dumps({
                "schema_version": worker_supervisor.JOB_SCHEMA_VERSION,
                "job_id": "a" * 32,
                "status": "quarantined",
                "reason_code": [],
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "payload_file": payload_path.name,
            }), encoding="utf-8")

            report = worker_supervisor.inspect_worker_queue(state)

        self.assertEqual(report["invalid"], 1)


class WorkerTimestampContractTests(unittest.TestCase):
    def test_invalid_pending_timestamp_is_quarantined_and_supervisor_drains_valid_job(self) -> None:
        invalid_values = ("not-a-timestamp", True, float("nan"), float("inf"), float("-inf"))
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=repr(invalid_value)), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                invalid_path = worker_supervisor.enqueue_job(
                    state,
                    "flush",
                    {"source": "invalid"},
                    start_supervisor=False,
                    now=100,
                )
                worker_supervisor.enqueue_job(
                    state,
                    "flush",
                    {"source": "valid"},
                    start_supervisor=False,
                    now=100,
                )
                malformed = json.loads(invalid_path.read_text(encoding="utf-8"))
                malformed["next_attempt_ts"] = invalid_value
                worker_supervisor.atomic_write_json(invalid_path, malformed)
                invalid_bytes = invalid_path.read_bytes()
                processed: list[dict[str, object]] = []

                def fake_runner(_vault_root: Path, state_dir: Path, running: Path) -> None:
                    job = worker_supervisor._load_job(running)
                    processed.append(job)
                    worker_supervisor._finish_job(
                        state_dir,
                        running,
                        job,
                        status="succeeded",
                        now=101,
                    )

                self.assertEqual(
                    worker_supervisor.run_supervisor(
                        state,
                        state,
                        now=lambda: 100,
                        job_runner=fake_runner,
                    ),
                    0,
                )

                report = worker_supervisor.inspect_worker_queue(state)
                self.assertEqual([job["payload"] for job in processed], [{"source": "valid"}])
                self.assertEqual(report["invalid"], 0)
                self.assertEqual(report["counts"]["pending"], 0)
                self.assertEqual(report["counts"]["succeeded"], 1)
                self.assertEqual(report["counts"]["quarantined"], 1)
                tombstone = state / "worker-jobs" / "quarantined" / invalid_path.name
                self.assertEqual(
                    tombstone.with_suffix(".payload").read_bytes(),
                    invalid_bytes,
                )

if __name__ == "__main__":
    unittest.main()
