from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import _fixtures  # noqa: F401

import companion_memory
import flush
import memory_ledger
import worker_supervisor as workers


class _DeterministicModel:
    """Replace only the external model while retaining flush's real parser/writes."""

    def __init__(self, claims: tuple[str, ...], on_call=None) -> None:
        self.claims = claims
        self.on_call = on_call
        self.prompts: list[str] = []

    def __call__(self, prompt: str, _vault: Path, **_kwargs: object):
        self.prompts.append(prompt)
        transcript = prompt.split(
            "--- BEGIN UNTRUSTED TRANSCRIPT DATA ---", 1
        )[1].split("--- END UNTRUSTED TRANSCRIPT DATA ---", 1)[0]
        claim = next(
            claim for claim in self.claims if f"**User:** {claim}" in transcript
        )
        if self.on_call is not None:
            self.on_call(prompt, claim)
        citation = json.dumps(
            {"quote": claim, "scope": "session"},
            ensure_ascii=False,
        )
        return flush.SessionSummary(
            {
                "Bağlam": "Kuyruk entegrasyon katkısı.",
                "Önemli Konuşmalar": "",
                "Alınan Kararlar": f"- {claim} <!-- user-source: {citation} -->",
                "Öğrenilenler": "- Entegrasyon kaydı.",
                "Yapılacaklar": "- Yok.",
            }
        ).render(), None


class ConcurrencyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / ".codex" / "scripts" / ".state"
        self.state.mkdir(parents=True)
        (self.root / "daily").mkdir()
        companion = self.root / "🔮 850-Companion"
        companion.mkdir()
        for name in ("Last-Session.md", "Threads.md", "Journal.md"):
            (companion / name).write_text("# Not\n", encoding="utf-8")

    def _transcript(self, name: str, claims: tuple[str, ...]) -> Path:
        path = self.root / f"{name}.jsonl"
        path.write_text(
            "".join(
                json.dumps(
                    {"role": "user", "content": claim}, ensure_ascii=False
                )
                + "\n"
                for claim in claims
            ),
            encoding="utf-8",
        )
        return path

    def _append_turn(self, path: Path, claim: str) -> None:
        with path.open("a", encoding="utf-8") as target:
            target.write(
                json.dumps(
                    {"role": "user", "content": claim}, ensure_ascii=False
                )
                + "\n"
            )

    def _enqueue(
        self,
        session: str,
        transcript: Path,
        *,
        reason: str = "turnend",
        **extra: object,
    ) -> Path:
        return workers.enqueue_flush(
            self.state,
            {"session_id": session, "transcript_path": str(transcript), **extra},
            reason,
            vault_root=self.root,
        )

    def _claim_and_execute(self, now: float) -> tuple[int, dict[str, object]]:
        claimed = workers._claim_next_job(self.state, now=now)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        running, job = claimed
        token = job["claim_token"]
        self.assertIsInstance(token, str)
        result = workers.execute_job_file(
            self.root,
            self.state,
            running,
            now=lambda: now,
            expected_claim_token=token,
        )
        return result, job

    def _reflection_path(self, session: str) -> Path:
        return companion_memory._reflection_path(self.state, session)

    def test_burst_coalesces_active_session_and_drains_other_session_first(self) -> None:
        a_claims = ("A karar 1", "A karar 2", "A karar 3")
        b_claims = ("B karar 1",)
        a_transcript = self._transcript("A", a_claims)
        b_transcript = self._transcript("B", b_claims)
        model = _DeterministicModel(a_claims + b_claims)
        companion_memory.request_reflection(self.state, "A")

        with (
            mock.patch.object(workers, "ensure_supervisor"),
            mock.patch.object(flush, "maybe_trigger_compile", return_value=False),
            mock.patch.object(flush, "run_codex", side_effect=model),
            mock.patch.object(flush, "MAX_TURNS", 1),
        ):
            self._enqueue("A", a_transcript)
            claimed = workers._claim_next_job(self.state, now=1)
            self.assertIsNotNone(claimed)

            # A is active. The newer A event gets its own pending successor;
            # B was admitted first and must remain the next claim.
            self._enqueue("B", b_transcript)
            self._enqueue(
                "A",
                a_transcript,
                continuation=True,
                continuation_reason="newer-turn",
            )
            running, a_job = claimed  # type: ignore[misc]
            a_token = a_job["claim_token"]
            self.assertIsInstance(a_token, str)
            self.assertEqual(
                workers.execute_job_file(
                    self.root,
                    self.state,
                    running,
                    now=lambda: 2,
                    expected_claim_token=a_token,
                ),
                0,
            )
            self.assertTrue(self._reflection_path("A").exists())

            b_claimed = workers._claim_next_job(self.state, now=3)
            self.assertIsNotNone(b_claimed)
            assert b_claimed is not None
            b_running, b_job = b_claimed
            b_payload = workers.load_hook_input(
                Path(b_job["payload"]["hook_input"])
            )
            self.assertEqual(b_payload["session_id"], "B")
            b_token = b_job["claim_token"]
            self.assertEqual(
                workers.execute_job_file(
                    self.root,
                    self.state,
                    b_running,
                    now=lambda: 3,
                    expected_claim_token=b_token,
                ),
                0,
            )
            self.assertTrue(self._reflection_path("A").exists())

            processed_a = 1
            while True:
                pending = workers._claim_next_job(self.state, now=4 + processed_a)
                if pending is None:
                    break
                running, job = pending
                payload = workers.load_hook_input(
                    Path(job["payload"]["hook_input"])
                )
                self.assertEqual(payload["session_id"], "A")
                token = job["claim_token"]
                self.assertEqual(
                    workers.execute_job_file(
                        self.root,
                        self.state,
                        running,
                        now=lambda: 4 + processed_a,
                        expected_claim_token=token,
                    ),
                    0,
                )
                processed_a += 1
                self.assertLessEqual(processed_a, 4)

        self.assertEqual(len(model.prompts), 4)
        coverage = json.loads(
            (self.state / f"flush-coverage-{flush._session_key('A')}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(coverage["count"], len(a_claims))
        self.assertFalse(
            (self.state / f"flush-batch-{flush._session_key('A')}.json").exists()
        )
        self.assertFalse(self._reflection_path("A").exists())
        self.assertEqual(
            list((self.state / "worker-jobs" / "pending").glob("*.json")), []
        )
        self.assertEqual(
            list((self.state / "worker-jobs" / "running").glob("*.json")), []
        )

        daily_files = list((self.root / "daily").glob("*.md"))
        self.assertEqual(len(daily_files), 1)
        daily = daily_files[0].read_text(encoding="utf-8")
        for claim in a_claims + b_claims:
            self.assertEqual(daily.count(f"- {claim} "), 1)
        self.assertEqual(daily.count("<!-- flush:"), len(a_claims) + len(b_claims))
        last_session = (
            self.root / "🔮 850-Companion" / "Last-Session.md"
        ).read_text(encoding="utf-8")
        self.assertIn(a_claims[-1], last_session)
        self.assertIn(b_claims[0], last_session)
        self.assertEqual(len(companion_memory._records(last_session)), 2)

    def test_coverage_crash_reuses_durable_batch_before_processing_appended_turn(self) -> None:
        claims = ("İlk dayanıklı karar", "Sonraki dayanıklı karar")
        transcript = self._transcript("coverage", claims[:1])
        model = _DeterministicModel(claims)
        companion_memory.request_reflection(self.state, "coverage")
        coverage_path = self.state / f"flush-coverage-{flush._session_key('coverage')}.json"
        batch_path = self.state / f"flush-batch-{flush._session_key('coverage')}.json"
        real_atomic_write = flush.atomic_write_json
        crashed = False

        def crash_after_publication(path: Path, value: object, *args: object, **kwargs: object):
            nonlocal crashed
            if path == coverage_path and not crashed:
                crashed = True
                raise OSError("injected-coverage-crash")
            return real_atomic_write(path, value, *args, **kwargs)

        with (
            mock.patch.object(workers, "ensure_supervisor"),
            mock.patch.object(flush, "maybe_trigger_compile", return_value=False),
            mock.patch.object(flush, "run_codex", side_effect=model),
        ):
            self._enqueue("coverage", transcript)
            with mock.patch.object(
                flush, "atomic_write_json", side_effect=crash_after_publication
            ):
                first_result, _first_job = self._claim_and_execute(100)
                self.assertEqual(first_result, 1)

            self.assertTrue(crashed)
            self.assertFalse(coverage_path.exists())
            self.assertTrue(batch_path.exists())
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            self.assertEqual(batch["start"], 0)
            self.assertEqual(batch["end"], 1)
            self.assertIsInstance(batch["digest"], str)
            daily = next((self.root / "daily").glob("*.md")).read_text(encoding="utf-8")
            self.assertIn(claims[0], daily)
            companion = (
                self.root / "🔮 850-Companion" / "Last-Session.md"
            ).read_text(encoding="utf-8")
            self.assertIn(claims[0], companion)
            self.assertTrue(self._reflection_path("coverage").exists())

            self._append_turn(transcript, claims[1])
            retry_result, _retry_job = self._claim_and_execute(106)
            self.assertEqual(retry_result, 0)
            self.assertEqual(len(model.prompts), 1)
            self.assertEqual(json.loads(coverage_path.read_text())["count"], 1)
            self.assertFalse(batch_path.exists())
            self.assertTrue(self._reflection_path("coverage").exists())
            self.assertEqual(
                len(list((self.state / "worker-jobs" / "pending").glob("*.json"))), 1
            )

            final_result, _final_job = self._claim_and_execute(107)
            self.assertEqual(final_result, 0)
            self.assertEqual(len(model.prompts), 2)
            self.assertEqual(json.loads(coverage_path.read_text())["count"], 2)
            self.assertFalse(batch_path.exists())
            self.assertFalse(self._reflection_path("coverage").exists())
            daily = next((self.root / "daily").glob("*.md")).read_text(encoding="utf-8")
            for claim in claims:
                self.assertEqual(daily.count(f"- {claim} "), 1)
            self.assertEqual(daily.count("<!-- flush:"), 2)
            self.assertEqual(list(self.state.glob("hookin-*.json")), [])

    def test_forget_at_model_boundary_retries_without_emitting_forbidden_summary(self) -> None:
        fact = "Model sınırında unutulacak karar"
        transcript = self._transcript("forget", (fact,))
        forget_errors: list[BaseException] = []

        def forget_at_boundary(_prompt: str, _claim: str) -> None:
            def forget() -> None:
                try:
                    memory_ledger.suppress_derived_memory(
                        self.root / ".codex" / "private-memory", fact
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    forget_errors.append(exc)

            thread = threading.Thread(target=forget)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(forget_errors, [])

        model = _DeterministicModel((fact,), on_call=forget_at_boundary)
        companion_memory.request_reflection(self.state, "forget")

        with (
            mock.patch.object(workers, "ensure_supervisor"),
            mock.patch.object(flush, "maybe_trigger_compile", return_value=False),
            mock.patch.object(flush, "run_codex", side_effect=model),
        ):
            self._enqueue("forget", transcript)
            first_result, _first_job = self._claim_and_execute(100)
            self.assertEqual(first_result, 1)
            self.assertEqual(len(model.prompts), 1)
            self.assertIn(f"**User:** {fact}", model.prompts[0])
            retry_result, _retry_job = self._claim_and_execute(106)
            self.assertEqual(retry_result, 0)

        self.assertEqual(len(model.prompts), 1)
        self.assertIn(
            memory_ledger.memory_text_hash(fact),
            memory_ledger.load_suppressed_hashes(
                self.root / ".codex" / "private-memory"
            ),
        )
        coverage = json.loads(
            (self.state / f"flush-coverage-{flush._session_key('forget')}.json").read_text()
        )
        self.assertEqual(coverage["count"], 0)
        self.assertFalse(
            (self.state / f"flush-batch-{flush._session_key('forget')}.json").exists()
        )
        self.assertFalse(self._reflection_path("forget").exists())
        self.assertEqual(list((self.root / "daily").glob("*.md")), [])
        for path in (self.root / "🔮 850-Companion").glob("*.md"):
            self.assertNotIn(fact, path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.state.glob("hookin-*.json")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
