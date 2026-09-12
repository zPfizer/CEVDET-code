"""flush.py — dilim 2: göç, hazırlanmış makbuz, tercih yarışı ve ek bütçesi."""

from __future__ import annotations

import argparse
import datetime as dt
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


SUMMARY = "\n".join(f"## {s}\nKalıcı özet." for s in flush.EXPECTED_SECTIONS)
NOW = dt.datetime.fromisoformat("2026-09-11T10:00:00+03:00")
TURN = "Kalıcı karar alındı."


def _vault(temporary: Path) -> tuple[Path, Path]:
    state = temporary / ".codex/scripts/.state"
    state.mkdir(parents=True)
    return temporary, state


def _payload(state: Path, rows, session: str = "oturum") -> dict:
    transcript = state / "rollout.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return {"session_id": session, "transcript_path": str(transcript)}


def _args() -> argparse.Namespace:
    return argparse.Namespace(hook_input=Path("x"), reason="turnend")


def _run(vault, state, payload, *, codex=None, extra=()):
    side = codex if codex is not None else (lambda *a, **k: (SUMMARY, None))
    managers = [
        mock.patch.object(flush, "run_codex", side_effect=side),
        mock.patch.object(flush, "maybe_trigger_compile", return_value=False),
        *extra,
    ]
    entered = []
    try:
        for manager in managers:
            entered.append(manager.__enter__())
        return flush.flush_once(_args(), NOW, vault, state, hook_input=payload)
    finally:
        for manager in reversed(managers):
            manager.__exit__(None, None, None)


class SmallHelperArms(unittest.TestCase):
    def test_fit_and_chunk_arms(self) -> None:
        turns = [("user", "a" * 30), ("user", "b" * 30)]
        selected = flush.fit_turns(turns, 5, 45)
        self.assertEqual(len(selected), 1)
        with self.assertRaises(ValueError):
            flush.chunk_turns(turns, 0)
        chunks = flush.chunk_turns(turns, 45)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(
            flush._text_from_content({"type": "text", "text": "metin"}), "metin"
        )

    def test_recent_duplicate_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            key = flush._session_key("s")
            path = state / f"flush-{key}.json"

            def probe(payload, now=100.0, **kwargs):
                path.write_text(json.dumps(payload), encoding="utf-8")
                return flush._is_recent_duplicate(state, "s", now, **kwargs)

            receipt_state = {
                "receipts": {
                    "makbuz": {
                        "status": "ok",
                        "transcript_digest": "d" * 64,
                        "batch_start": 0,
                        "batch_end": 1,
                    }
                }
            }
            self.assertTrue(
                probe(receipt_state, reason="turnend", transcript_digest="d" * 64)
            )
            self.assertFalse(
                probe(
                    receipt_state, reason="turnend", transcript_digest="d" * 64,
                    batch_start=5, batch_end=9,
                )
            )
            self.assertFalse(
                probe({}, reason="turnend", transcript_digest="d" * 64)
            )

            legacy = {"session_key": key, "status": "ok", "ts": 100.0}
            self.assertTrue(probe(legacy))
            self.assertFalse(probe({**legacy, "session_key": "y" * 64}))
            self.assertFalse(probe({**legacy, "status": "inflight"}))
            self.assertFalse(probe({**legacy, "ts": "bozuk"}))
            self.assertFalse(probe(legacy, now=999.0))
            self.assertTrue(probe({"session_id": "s", "status": "ok", "ts": 100.0}))
            with self.assertRaises(ValueError):
                flush._flush_idempotency_key("s", "bozuk", "d" * 64, "e" * 64)

    def test_session_state_receipt_guards(self) -> None:
        self.assertIsNone(
            flush._find_flush_receipt(
                {"receipts": "liste-degil"}, transcript_digest="d" * 64,
                statuses={"prepared"}, batch_start=0, batch_end=1,
            )
        )

    def test_failure_record_swallows_health_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _vault_root, state = _vault(Path(temporary))
            with mock.patch.object(
                flush, "clear_health_error", side_effect=OSError
            ):
                flush._record_flush_failure(state, "s", 1.0, "hata")

    def test_run_codex_passes_reason_through(self) -> None:
        with mock.patch.object(
            flush.codex_runner, "run_exec", return_value=(None, "codex-timeout")
        ):
            self.assertEqual(
                flush.run_codex("p", Path(".")), (None, "codex-timeout")
            )

    def test_flush_once_argument_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            with self.assertRaises(ValueError):
                flush.flush_once(
                    argparse.Namespace(hook_input=Path("x"), reason="bozuk"),
                    NOW, vault, state, hook_input={"session_id": "s"},
                )
            with self.assertRaises(ValueError):
                flush.flush_once(
                    _args(), NOW, vault, state,
                    hook_input={"session_id": "", "transcript_path": "t"},
                )


class PreferenceRaceArms(unittest.TestCase):
    def _race(self, sequence_builder) -> int:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            real = flush.load_suppressed_hashes
            calls = {"n": 0}

            def racing(private_root):
                calls["n"] += 1
                return sequence_builder(calls["n"], real(private_root))

            with mock.patch.object(flush, "load_suppressed_hashes", side_effect=racing):
                return _run(vault, state, payload)

    def test_preference_change_windows(self) -> None:
        # 2. çağrıda değişim: indeks sonrası kontrol (819-822).
        self.assertEqual(
            self._race(lambda n, real: frozenset({"x" * 64}) if n == 2 else real), 1
        )
        # 3. çağrıda değişim: kaynak okuma sonrası kontrol.
        self.assertEqual(
            self._race(lambda n, real: frozenset({"x" * 64}) if n == 3 else real), 1
        )
        # 4. çağrıda değişim: yakalama sonrası kontrol.
        self.assertEqual(
            self._race(lambda n, real: frozenset({"x" * 64}) if n == 4 else real), 1
        )


class CoverageMigrationArms(unittest.TestCase):
    def _first_run(self, temporary: Path):
        vault, state = _vault(temporary)
        payload = _payload(state, [{"role": "user", "content": TURN}])
        self.assertEqual(_run(vault, state, payload), 0)
        key = flush._session_key("oturum")
        coverage_path = state / f"flush-coverage-{key}.json"
        return vault, state, payload, coverage_path

    def test_legacy_schema_migration_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, coverage_path = self._first_run(Path(temporary))
            legacy_digest = hashlib.sha256(
                json.dumps([["user", TURN]], ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            coverage_path.write_text(
                json.dumps({"schema_version": 1, "count": 1,
                            "digest": legacy_digest}),
                encoding="utf-8",
            )
            self.assertEqual(_run(vault, state, payload), 0)

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, coverage_path = self._first_run(Path(temporary))
            coverage_path.write_text(
                json.dumps({"schema_version": 1, "count": 1, "digest": "0" * 64}),
                encoding="utf-8",
            )
            self.assertEqual(_run(vault, state, payload), 1)

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, coverage_path = self._first_run(Path(temporary))
            coverage_path.write_text(
                json.dumps({"schema_version": 1, "count": 99, "digest": "0" * 64}),
                encoding="utf-8",
            )
            self.assertEqual(_run(vault, state, payload), 1)

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, coverage_path = self._first_run(Path(temporary))
            coverage_path.write_text(
                json.dumps({"schema_version": 7, "count": 1, "digest": "0" * 64}),
                encoding="utf-8",
            )
            self.assertEqual(_run(vault, state, payload), 1)

    def test_migration_write_failure_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, coverage_path = self._first_run(Path(temporary))
            legacy_digest = hashlib.sha256(
                json.dumps([["user", TURN]], ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            coverage_path.write_text(
                json.dumps({"schema_version": 1, "count": 1,
                            "digest": legacy_digest}),
                encoding="utf-8",
            )
            transcript = state / "rollout.jsonl"
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"role": "user", "content": "İkinci kalıcı karar."},
                    ensure_ascii=False,
                ) + "\n")
            real = flush.atomic_write_json
            fails = {"n": 0}

            def failing(path, *a, **k):
                if "flush-coverage-" in Path(path).name and fails["n"] == 0:
                    fails["n"] += 1
                    raise OSError("disk")
                return real(path, *a, **k)

            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(flush, "atomic_write_json", failing)],
                ),
                1,
            )

    def test_read_only_guard_during_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, _coverage = self._first_run(Path(temporary))
            # Yeni içerik yok; tamamlama ve tetik yolunda yazma korunmalı.
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

    def test_partial_only_transcript_fails_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            transcript = state / "rollout.jsonl"
            transcript.write_bytes(b'{"role":"user","content":"yarim')
            payload = {"session_id": "oturum", "transcript_path": str(transcript)}
            self.assertEqual(_run(vault, state, payload), 1)


class PreparedReceiptArms(unittest.TestCase):
    def _prepared_state(self, temporary: Path):
        vault, state = _vault(temporary)
        payload = _payload(state, [{"role": "user", "content": TURN}])
        with mock.patch.object(flush, "append_daily", side_effect=OSError("disk")):
            self.assertEqual(_run(vault, state, payload), 1)
        key = flush._session_key("oturum")
        session_path = state / f"flush-{key}.json"
        self.assertTrue(session_path.exists())
        return vault, state, payload, session_path

    def test_prepared_receipt_field_and_summary_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, session_path = self._prepared_state(Path(temporary))
            record = json.loads(session_path.read_text(encoding="utf-8"))
            for receipt in record.get("receipts", {}).values():
                if receipt.get("status") == "prepared":
                    receipt["idempotency_key"] = ""
            session_path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(_run(vault, state, payload), 1)

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, payload, _session = self._prepared_state(Path(temporary))
            for prepared in state.glob("flush-prepared-*.md"):
                prepared.unlink()
            self.assertEqual(_run(vault, state, payload), 1)

    def test_prepared_state_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            real = flush.atomic_write_text

            def failing(path, *a, **k):
                if "flush-prepared-" in Path(path).name:
                    raise OSError("disk")
                return real(path, *a, **k)

            self.assertEqual(
                _run(
                    vault, state, payload,
                    extra=[mock.patch.object(flush, "atomic_write_text", failing)],
                ),
                1,
            )

    def test_late_source_verify_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            with mock.patch.object(
                transcript_index.TranscriptIndex, "verify_source_current",
                side_effect=[None, ValueError("bozuk-kaynak")],
            ):
                self.assertEqual(_run(vault, state, payload), 1)
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])
            with mock.patch.object(
                transcript_index.TranscriptIndex, "verify_source_current",
                side_effect=ValueError("bozuk-kaynak"),
            ):
                self.assertEqual(_run(vault, state, payload), 1)


class AttachmentBudgetArms(unittest.TestCase):
    def _capture_with(self, summarize_call):
        def capture(source_turns, _vault, _event, _hashes, summarize, **_k):
            return summarize_call(summarize)

        return capture

    def test_summarize_source_error_classes(self) -> None:
        cases = [
            (lambda *a, **k: (None, "model-hata"), "attachment-summary-failed"),
            (lambda *a, **k: ("## Bozuk\nx", None), "attachment-summary-failed"),
        ]
        for codex, _expected in cases:
            with self.subTest(expected=_expected):
                with tempfile.TemporaryDirectory() as temporary:
                    vault, state = _vault(Path(temporary))
                    payload = _payload(state, [{"role": "user", "content": TURN}])

                    def call(summarize):
                        summarize("kaynak metni")
                        return []

                    self.assertEqual(
                        _run(
                            vault, state, payload, codex=codex,
                            extra=[mock.patch.object(
                                flush.attachment_memory, "capture_sources",
                                side_effect=self._capture_with(call),
                            )],
                        ),
                        1,
                    )

    def test_flush_bos_with_sources_merges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = _vault(Path(temporary))
            payload = _payload(state, [{"role": "user", "content": TURN}])

            def call(_summarize):
                return [("📥 000-Inbox/Kaynak", SUMMARY)]

            self.assertEqual(
                _run(
                    vault, state, payload,
                    codex=lambda *a, **k: ("FLUSH_BOS", None),
                    extra=[mock.patch.object(
                        flush.attachment_memory, "capture_sources",
                        side_effect=self._capture_with(call),
                    )],
                ),
                0,
            )
            daily = (vault / "daily" / "2026-09-11.md").read_text(encoding="utf-8")
        self.assertIn("Kalıcı özet.", daily)


if __name__ == "__main__":
    unittest.main()
