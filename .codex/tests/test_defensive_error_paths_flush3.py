"""flush.py — dilim 3: batch göçü, kanıt bağlamı, ek bütçe ve salt-okunur kollar."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import flush
import transcript_index
from memory_ledger import MemoryReadOnlyError
from process_control import ProcessTreeCleanupError
from test_defensive_error_paths_flush2 import (
    NOW,
    SUMMARY,
    TURN,
    _payload,
    _run,
    _vault,
)


def _legacy_digest(rows) -> str:
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class StateWriterArms(unittest.TestCase):
    def test_receipts_field_reset_when_not_dict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _vault_root, state = _vault(Path(temporary))
            key = flush._session_key("s")
            (state / f"flush-{key}.json").write_text(
                json.dumps({"receipts": "liste-degil"}), encoding="utf-8"
            )
            flush._write_flush_state(
                state, "s", 100.0, "fail", "hata",
            )
            value = json.loads(
                (state / f"flush-{key}.json").read_text(encoding="utf-8")
            )
        self.assertEqual(value["status"], "fail")

    def test_failure_record_swallows_state_write_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _vault_root, state = _vault(Path(temporary))
            with mock.patch.object(
                flush, "_write_flush_state", side_effect=OSError("disk")
            ):
                flush._record_flush_failure(state, "s", 100.0, "hata")


class CoveragePolicyArms(unittest.TestCase):
    def _first_run(self, temporary: Path):
        vault, state = _vault(temporary)
        payload = _payload(state, [{"role": "user", "content": TURN}])
        self.assertEqual(_run(vault, state, payload), 0)
        key = flush._session_key("oturum")
        return vault, state, payload, state / f"flush-coverage-{key}.json"

    def test_legacy_zero_count_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, coverage_path = self._first_run(
                Path(temporary)
            )
            coverage_path.write_text(
                json.dumps({"schema_version": 1, "count": 0,
                            "digest": _legacy_digest([])}),
                encoding="utf-8",
            )
            self.assertEqual(_run(vault, state, payload), 0)

    def test_legacy_fingerprint_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, coverage_path = self._first_run(
                Path(temporary)
            )
            coverage_path.write_text(
                json.dumps({"schema_version": 1, "count": 1,
                            "digest": "0" * 64}),
                encoding="utf-8",
            )
            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        transcript_index, "read_selected_rows",
                        side_effect=ValueError("okunamadi"),
                    )],
                ),
                1,
            )

    def test_policy_digest_value_error_forces_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, coverage_path = self._first_run(
                Path(temporary)
            )
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            coverage["policy_version"] = flush.LEGACY_POLICY_VERSION
            coverage_path.write_text(json.dumps(coverage), encoding="utf-8")

            original = transcript_index.TranscriptIndex.coverage_digest

            def fake(self, count, *, policy_version=None):
                if policy_version is not None:
                    raise ValueError("transcript-index-coverage-invalid")
                return original(self, count)

            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        transcript_index.TranscriptIndex,
                        "coverage_digest", fake,
                    )],
                ),
                1,
            )

    def test_prepared_receipt_without_coverage_or_batch_requires_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            with mock.patch.object(
                flush, "append_daily", side_effect=OSError("disk")
            ):
                self.assertEqual(_run(vault, state, payload), 1)

            key = flush._session_key("oturum")
            coverage_path = state / f"flush-coverage-{key}.json"
            batch_path = state / f"flush-batch-{key}.json"
            session_path = state / f"flush-{key}.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            receipts = session.get("receipts")
            self.assertTrue(
                isinstance(receipts, dict)
                and any(
                    isinstance(receipt, dict)
                    and receipt.get("status") == "prepared"
                    and receipt.get("policy_version") == transcript_index.POLICY_VERSION
                    for receipt in receipts.values()
                )
            )
            batch_path.unlink(missing_ok=True)
            coverage_path.unlink(missing_ok=True)
            Path(payload["transcript_path"]).write_text("", encoding="utf-8")

            self.assertEqual(_run(vault, state, payload), 1)
            failure = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "fail")
            self.assertEqual(failure["detail"], flush.POLICY_MIGRATION_REQUIRED)
            self.assertFalse(coverage_path.exists())
            self.assertFalse(batch_path.exists())


class BatchMigrationArms(unittest.TestCase):
    def _prepared(self, temporary: Path):
        vault, state = _vault(temporary)
        payload = _payload(state, [{"role": "user", "content": TURN}])
        with mock.patch.object(
            flush, "append_daily", side_effect=OSError("disk")
        ):
            self.assertEqual(_run(vault, state, payload), 1)
        key = flush._session_key("oturum")
        batch_path = state / f"flush-batch-{key}.json"
        self.assertTrue(batch_path.exists())
        return vault, state, payload, batch_path

    def _rewrite(self, batch_path: Path, **overrides) -> None:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch.update(overrides)
        batch_path.write_text(json.dumps(batch), encoding="utf-8")

    def test_unknown_batch_policy_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, batch_path = self._prepared(Path(temporary))
            self._rewrite(batch_path, policy_version="bilinmez")
            self.assertEqual(_run(vault, state, payload), 1)

    def test_current_digest_only_updates_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, batch_path = self._prepared(Path(temporary))
            self._rewrite(batch_path, policy_version=None)
            self.assertEqual(_run(vault, state, payload), 0)

    def test_current_digest_policy_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, batch_path = self._prepared(Path(temporary))
            self._rewrite(batch_path, policy_version=None)
            real = flush.atomic_write_json

            def failing(path, *args, **kwargs):
                if "flush-batch-" in Path(path).name:
                    raise OSError("disk")
                return real(path, *args, **kwargs)

            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        flush, "atomic_write_json", failing,
                    )],
                ),
                1,
            )

    def test_unrecognized_batch_digest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, batch_path = self._prepared(Path(temporary))
            self._rewrite(batch_path, policy_version=None, digest="0" * 64)
            self.assertEqual(_run(vault, state, payload), 1)

    def test_legacy_batch_digest_check_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, batch_path = self._prepared(Path(temporary))
            self._rewrite(batch_path, digest="0" * 64)
            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        transcript_index, "read_selected_rows",
                        side_effect=ValueError("okunamadi"),
                    )],
                ),
                1,
            )

    def test_legacy_batch_digest_migrates_and_write_failure(self) -> None:
        legacy = _legacy_digest([["user", TURN]])
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, batch_path = self._prepared(Path(temporary))
            self._rewrite(batch_path, digest=legacy)
            self.assertEqual(_run(vault, state, payload), 0)

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, batch_path = self._prepared(Path(temporary))
            self._rewrite(batch_path, digest=legacy)
            real = flush.atomic_write_json

            def failing(path, *args, **kwargs):
                if "flush-batch-" in Path(path).name:
                    raise OSError("disk")
                return real(path, *args, **kwargs)

            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        flush, "atomic_write_json", failing,
                    )],
                ),
                1,
            )


class EvidenceContextArms(unittest.TestCase):
    def test_previous_assistant_turn_is_prepended(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [
                {"role": "user", "content": TURN},
                {"role": "assistant", "content": "Kalıcı öneri sunuldu."},
            ])
            self.assertEqual(_run(vault, state, payload), 0)
            transcript = Path(payload["transcript_path"])
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"role": "user", "content": "evet"}, ensure_ascii=False
                ) + "\n")
            self.assertEqual(_run(vault, state, payload), 0)


class AttachmentDeadlineArms(unittest.TestCase):
    def _capture_with(self, call):
        def capture(source_turns, _vault, _event, _hashes, summarize, **_k):
            return call(summarize)

        return mock.patch.object(
            flush.attachment_memory, "capture_sources", capture
        )

    def test_summarize_deadline_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            clock = {"value": 0.0}

            def monotonic() -> float:
                clock["value"] += 100_000.0
                return clock["value"]

            def call(summarize):
                summarize("kaynak")
                return []

            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[
                        self._capture_with(call),
                        mock.patch.object(flush.time, "monotonic", monotonic),
                    ],
                ),
                1,
            )

    def test_summarize_session_exclusion_and_preference_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])

            def session_only(summarize):
                with mock.patch.object(
                    flush, "is_session_only", return_value=True
                ):
                    summarize("kaynak")
                return []

            self.assertEqual(
                _run(vault, state, payload,
                     extra=[self._capture_with(session_only)]),
                1,
            )

            def preference_change(summarize):
                with mock.patch.object(
                    flush, "load_suppressed_hashes",
                    return_value=frozenset({"x"}),
                ):
                    summarize("kaynak")
                return []

            self.assertEqual(
                _run(vault, state, payload,
                     extra=[self._capture_with(preference_change)]),
                1,
            )

    def test_capture_readonly_and_tree_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        flush.attachment_memory, "capture_sources",
                        side_effect=MemoryReadOnlyError("ro"),
                    )],
                ),
                0,
            )
            with self.assertRaises(ProcessTreeCleanupError):
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        flush.attachment_memory, "capture_sources",
                        side_effect=ProcessTreeCleanupError(
                            ["cmd"], 1.0, 123, OSError("ağaç")
                        ),
                    )],
                )

    def test_final_deadline_after_source_model_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            clock = {"value": 0.0}

            def monotonic() -> float:
                clock["value"] += 200.0
                return clock["value"]

            def call(summarize):
                summarize("kaynak")
                return []

            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[
                        self._capture_with(call),
                        mock.patch.object(flush.time, "monotonic", monotonic),
                    ],
                ),
                1,
            )


class ReadOnlyAndDriftArms(unittest.TestCase):
    def test_duplicate_completion_trigger_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            self.assertEqual(_run(vault, state, payload), 0)
            key = flush._session_key("oturum")
            (state / f"flush-coverage-{key}.json").unlink()
            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        flush, "memory_write_guard",
                        side_effect=MemoryReadOnlyError("ro"),
                    )],
                ),
                0,
            )

    def test_late_drift_with_preference_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            durum = {"verify": 0}

            def verify(_self) -> None:
                durum["verify"] += 1
                if durum["verify"] >= 2:
                    raise ValueError("transcript-index-source-drift")

            def load(_root):
                if durum["verify"] >= 2:
                    return frozenset({"x"})
                return frozenset()

            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[
                        mock.patch.object(
                            transcript_index.TranscriptIndex,
                            "verify_source_current", verify,
                        ),
                        mock.patch.object(
                            flush, "load_suppressed_hashes", load,
                        ),
                    ],
                ),
                1,
            )

    def test_publish_readonly_returns_clean(self) -> None:
        import companion_memory

        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        companion_memory, "publish",
                        side_effect=MemoryReadOnlyError("ro"),
                    )],
                ),
                0,
            )

    def test_compile_trigger_readonly_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(
                        flush, "maybe_trigger_compile",
                        side_effect=MemoryReadOnlyError("ro"),
                    )],
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
