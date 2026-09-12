"""compile.py — dilim 3: main/_run_locked/tetik/kurtarma kolları."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import compile as memory_compile
import compile_state
from compile_state import CompileState
from file_lock import LockUnavailable, locked
from process_control import ProcessTreeCleanupError


def _patched(state: Path, vault: Path):
    return (
        mock.patch.object(memory_compile, "STATE_DIR", state),
        mock.patch.object(memory_compile, "VAULT_ROOT", vault),
    )


class TriggerClaimGuards(unittest.TestCase):
    def test_release_and_validation_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            memory_compile._release_trigger_claim(state, None)
            missing = state / "compile-trigger-2026-09-11"
            memory_compile._release_trigger_claim(state, missing)

            with self.assertRaises(ValueError):
                memory_compile._validated_trigger_claim(
                    state, Path(temporary) / "baska" / "compile-trigger-2026-09-11"
                )
            with self.assertRaises(ValueError):
                memory_compile._validated_trigger_claim(state, state / "kotu-ad")
            klasor = state / "compile-trigger-2026-09-12"
            klasor.mkdir()
            with self.assertRaises(ValueError):
                memory_compile._validated_trigger_claim(state, klasor)
            self.assertIsNone(memory_compile._validated_trigger_claim(state, None))


class RunLockedArms(unittest.TestCase):
    def _dirs(self, temporary: Path) -> tuple[Path, Path]:
        vault = temporary / "vault"
        (vault / "daily").mkdir(parents=True)
        state = vault / ".codex/scripts/.state"
        state.mkdir(parents=True)
        return vault, state

    def test_state_read_and_path_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._dirs(Path(temporary))
            (state / compile_state.STATE_NAME).write_text("{bozuk", encoding="utf-8")
            self.assertTrue(
                memory_compile._run_locked(vault, state, True, 1, None)
            )
            self.assertTrue(
                memory_compile._run_locked(vault, state, False, 1, None)
            )

        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._dirs(Path(temporary))
            bad = CompileState(ingested={"daily/api_key=abc12345.md": "x"})
            compile_state.save(state, bad)
            self.assertTrue(
                memory_compile._run_locked(vault, state, True, 1, None)
            )
            self.assertTrue(
                memory_compile._run_locked(vault, state, False, 1, None)
            )

    def test_recovery_and_daily_read_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._dirs(Path(temporary))
            with mock.patch.object(
                memory_compile, "_recover_pending_publication",
                side_effect=OSError("kurtarma"),
            ):
                self.assertTrue(
                    memory_compile._run_locked(vault, state, False, 1, None)
                )
            with mock.patch.object(
                memory_compile, "_recover_pending_publication", return_value=None
            ):
                with mock.patch.object(
                    memory_compile, "changed_dailies",
                    side_effect=ValueError("okunamadı"),
                ):
                    self.assertTrue(
                        memory_compile._run_locked(vault, state, True, 1, None)
                    )
                    self.assertTrue(
                        memory_compile._run_locked(vault, state, False, 1, None)
                    )

    def test_recovered_publication_finalize_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._dirs(Path(temporary))
            with (
                mock.patch.object(
                    memory_compile, "_recover_pending_publication",
                    return_value={"sahte": True},
                ),
                mock.patch.object(
                    memory_compile, "_apply_recovered_publication",
                    return_value=False,
                ),
                mock.patch.object(
                    memory_compile, "_finalize_publication",
                    side_effect=ValueError("günlük"),
                ),
            ):
                self.assertTrue(
                    memory_compile._run_locked(vault, state, False, 1, None)
                )

    def test_dry_run_lists_and_clean_run_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._dirs(Path(temporary))
            (vault / "daily" / "2026-09-11.md").write_text("kayıt", encoding="utf-8")
            self.assertFalse(
                memory_compile._run_locked(vault, state, True, 1, None)
            )
            with mock.patch.object(
                memory_compile, "_checkpoint_machine_outputs",
                return_value=("clean", "no-machine-changes"),
            ):
                with mock.patch.object(
                    memory_compile, "changed_dailies", return_value=[]
                ):
                    self.assertFalse(
                        memory_compile._run_locked(vault, state, False, 1, None)
                    )

    def test_compile_one_failure_maps_to_recorded_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._dirs(Path(temporary))
            daily = vault / "daily" / "2026-09-11.md"
            daily.write_text("kayıt", encoding="utf-8")
            with (
                mock.patch.object(
                    memory_compile, "_compile_one",
                    return_value=("policy", "forbidden-write:x"),
                ),
                mock.patch.object(
                    memory_compile, "_checkpoint_machine_outputs",
                    return_value=("clean", ""),
                ),
            ):
                self.assertTrue(
                    memory_compile._run_locked(vault, state, False, 1, None)
                )

    def test_budgeted_runner_stops_after_max_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._dirs(Path(temporary))
            for day in ("2026-09-10", "2026-09-11"):
                (vault / "daily" / f"{day}.md").write_text(day, encoding="utf-8")
            seen = []

            def fake_compile_one(_v, _s, daily_path, _d, _t, runner):
                seen.append(daily_path.name)
                # Bütçeyi gerçekten tüket: runner bir model çağrısı sayar.
                runner("istem", Path("."))
                self.assertEqual(runner("istem", Path(".")),
                                 "model-call-budget-exhausted")
                return None, ""

            with (
                mock.patch.object(
                    memory_compile, "_compile_one",
                    side_effect=fake_compile_one,
                ),
                mock.patch.object(
                    memory_compile, "_run_codex", return_value=None
                ),
                mock.patch.object(
                    memory_compile, "_checkpoint_machine_outputs",
                    return_value=("clean", ""),
                ),
                mock.patch.object(
                    memory_compile, "_finalize_publication", return_value=None
                ),
            ):
                self.assertFalse(
                    memory_compile._run_locked(vault, state, False, 1, None)
                )
        self.assertEqual(len(seen), 1)


class MainArms(unittest.TestCase):
    def test_invoked_by_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.dict(os.environ, {"BEYIN_INVOKED_BY": "beyin-scripts"}):
                with mock.patch.object(memory_compile, "STATE_DIR", state):
                    self.assertEqual(memory_compile.main(["--strict"]), 3)
                    self.assertEqual(memory_compile.main([]), 0)

    def test_argument_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            environment = {k: v for k, v in os.environ.items()
                           if k != "BEYIN_INVOKED_BY"}
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch.object(memory_compile, "STATE_DIR", state):
                    self.assertEqual(memory_compile.main(["--bilinmez"]), 0)
                    self.assertEqual(
                        memory_compile.main(["--strict", "--bilinmez"]), 1
                    )
                    self.assertEqual(memory_compile.main(["--max-calls", "0"]), 0)
                    self.assertEqual(
                        memory_compile.main(
                            ["--trigger-claim", str(state / "kotu-ad")]
                        ),
                        0,
                    )

    def test_dry_run_and_lock_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            (vault / "daily").mkdir(parents=True)
            state = vault / ".codex/scripts/.state"
            state.mkdir(parents=True)
            environment = {k: v for k, v in os.environ.items()
                           if k != "BEYIN_INVOKED_BY"}
            with mock.patch.dict(os.environ, environment, clear=True):
                patches = _patched(state, vault)
                with patches[0], patches[1]:
                    with mock.patch.object(
                        memory_compile, "_run_locked", side_effect=RuntimeError
                    ):
                        self.assertEqual(memory_compile.main(["--dry-run"]), 0)

                    with locked(state / "compile"):
                        self.assertEqual(memory_compile.main([]), 0)

                    with mock.patch.object(
                        memory_compile, "locked", side_effect=OSError("kilit")
                    ):
                        self.assertEqual(memory_compile.main([]), 0)

                    with mock.patch.object(
                        memory_compile, "_run_locked",
                        side_effect=ProcessTreeCleanupError(
                            ["cmd"], 1.0, 123, OSError("ağaç")
                        ),
                    ):
                        self.assertEqual(memory_compile.main([]), 0)

                    with mock.patch.object(
                        memory_compile, "_run_locked", side_effect=RuntimeError("çök")
                    ):
                        self.assertEqual(memory_compile.main(["--strict"]), 1)


if __name__ == "__main__":
    unittest.main()
