"""SessionStart deadline and read-only scope integration boundaries."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _fixtures import SCRIPTS_DIR
import flush
import hook
import memory_ledger
import state_store
import worker_supervisor as workers


def _queue_report(*, pending: int = 0) -> dict[str, object]:
    return {
        "counts": {
            "pending": pending,
            "claimed": 0,
            "running": 0,
            "succeeded": 0,
            "dead-letter": 0,
            "quarantined": 0,
        },
        "invalid": 0,
        "orphan_hook_inputs": 0,
        "terminal": {"recovered": 0, "unresolved": 0},
    }


class HookIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(state_store.os.name == "nt", "Windows sharing retry")
    def test_reflection_conversation_write_bounds_sharing_retry_and_preserves_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            session_id = "reflection-deadline"
            conversation = state / f"conversation-{hook.session_key(session_id)}.json"
            conversation.write_text(
                json.dumps({"prompt_count": 5, "meaningful_prompt_seen": True}),
                encoding="utf-8",
            )
            original = conversation.read_bytes()
            attempts = 0
            deadline = time.monotonic() + 0.2
            real_replace = state_store.os.replace

            def sharing_conflict(source: Path, destination: Path) -> None:
                nonlocal attempts
                if Path(destination) != conversation:
                    real_replace(source, destination)
                    return
                attempts += 1
                error = PermissionError("synthetic sharing conflict")
                error.winerror = 32
                raise error

            started = time.monotonic()
            with (
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(state_store.os, "replace", side_effect=sharing_conflict),
            ):
                with self.assertRaisesRegex(PermissionError, "synthetic sharing conflict"):
                    hook._mark_reflection_if_needed(
                        {"session_id": session_id}, deadline=deadline
                    )
            elapsed = time.monotonic() - started

            record = json.loads(conversation.read_text(encoding="utf-8"))
            self.assertEqual(conversation.read_bytes(), original)
            self.assertTrue(record["meaningful_prompt_seen"])
            self.assertNotIn("reflection_checked", record)
            self.assertTrue(any((state / "reflection-requests").glob("*.json")))
            self.assertEqual(list(state.glob("*.tmp")), [])

        self.assertGreaterEqual(attempts, 2)
        self.assertLess(elapsed, 0.75)

    def test_session_start_propagates_one_deadline_to_context_queue_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            deadline = time.monotonic() + 30
            reports = [_queue_report(), _queue_report(pending=1)]
            context = "[Hafıza: Kimlik]\nCevo"
            payload = {"session_id": "deadline-session", "cwd": str(vault)}
            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                mock.patch.object(hook, "build_session_context", return_value=context) as build,
                mock.patch.object(flush, "maybe_trigger_compile", return_value=False) as trigger,
                mock.patch.object(hook, "inspect_worker_queue", side_effect=reports) as inspect,
                mock.patch.object(hook, "ensure_supervisor") as ensure,
                mock.patch.object(hook, "_emit_context"),
                mock.patch.object(hook, "record_hook_runtime") as runtime,
                mock.patch.object(hook, "clear_hook_health") as health,
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                result = hook.main(["session-start", "--strict"])

        self.assertEqual(result, 0)
        build.assert_called_once_with(
            vault,
            state,
            write_views=True,
            deadline=deadline,
        )
        trigger.assert_called_once_with(vault, deadline=deadline)
        self.assertEqual(inspect.call_args_list, [
            mock.call(state / "maintenance", deadline=deadline),
            mock.call(state, deadline=deadline),
        ])
        ensure.assert_called_once_with(state, vault_root=vault, deadline=deadline)
        self.assertEqual(runtime.call_args.kwargs["deadline"], deadline)
        self.assertEqual(health.call_args.kwargs["deadline"], deadline)

    def test_session_start_read_only_snapshot_skips_all_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            memory_ledger.mark_read_only_turn(state, "audit")
            deadline = time.monotonic() + 30
            payload = {"session_id": "audit", "cwd": str(vault)}
            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                mock.patch.object(hook, "build_session_context", return_value="ctx") as build,
                mock.patch.object(flush, "maybe_trigger_compile") as trigger,
                mock.patch.object(hook, "inspect_worker_queue") as inspect,
                mock.patch.object(hook, "ensure_supervisor") as ensure,
                mock.patch.object(hook, "_emit_context"),
                mock.patch.object(hook, "record_hook_runtime"),
                mock.patch.object(hook, "clear_hook_health"),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                result = hook.main(["session-start", "--strict"])

        self.assertEqual(result, 0)
        build.assert_called_once_with(
            vault,
            state,
            write_views=False,
            deadline=deadline,
        )
        trigger.assert_not_called()
        inspect.assert_not_called()
        ensure.assert_not_called()

    def test_session_start_scope_lock_contention_emits_safe_context_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            session_id = "scope-contention"
            memory_ledger.mark_read_only_turn(state, session_id)
            marker = memory_ledger._read_only_path(state, session_id)
            ready = state / "scope-lock-ready"
            holder_script = (
                "import sys,time;"
                "from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);"
                "from file_lock import locked;"
                "resource=Path(sys.argv[2]);ready=Path(sys.argv[3]);"
                "guard=locked(resource);guard.__enter__();"
                "ready.write_text('ready', encoding='ascii');"
                "time.sleep(float(sys.argv[4]));"
                "guard.__exit__(None,None,None)"
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_script,
                    str(SCRIPTS_DIR),
                    str(marker),
                    str(ready),
                    "1.0",
                ],
                cwd=vault,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                limit = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < limit:
                    time.sleep(0.01)
                if not ready.exists():
                    raise AssertionError("scope lock holder did not start")
                output = io.StringIO()
                payload = {"session_id": session_id, "cwd": str(vault)}
                with (
                    mock.patch.object(hook, "VAULT_ROOT", vault),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "_hook_deadline", return_value=time.monotonic() + 0.2),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                ):
                    result = hook.main(["session-start", "--strict"])
            finally:
                child.wait(timeout=5)

            emitted = json.loads(output.getvalue())

        self.assertEqual(result, 1)
        context = emitted["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bağlamı güvenli biçimde doğrulanamadı", context)
        self.assertIn("Ham notlara veya eski önbelleğe geçme", context)

    def test_session_start_checks_deadline_after_late_scope_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            session_id = "late-scope"
            marker = memory_ledger._read_only_path(state, session_id)
            ready = state / "late-scope-lock-ready"
            holder_script = (
                "import sys,time;"
                "from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);"
                "from file_lock import locked;"
                "resource=Path(sys.argv[2]);ready=Path(sys.argv[3]);"
                "guard=locked(resource);guard.__enter__();"
                "ready.write_text('ready', encoding='ascii');"
                "time.sleep(float(sys.argv[4]));"
                "guard.__exit__(None,None,None)"
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_script,
                    str(SCRIPTS_DIR),
                    str(marker),
                    str(ready),
                    "0.35",
                ],
                cwd=vault,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                limit = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < limit:
                    time.sleep(0.01)
                if not ready.exists():
                    raise AssertionError("late scope lock holder did not start")
                deadline = time.monotonic() + 0.1
                payload = {"session_id": session_id, "cwd": str(vault)}
                output = io.StringIO()
                with (
                    mock.patch.object(hook, "VAULT_ROOT", vault),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                    mock.patch.object(hook, "timeout_for_deadline", return_value=1.0),
                    mock.patch.object(hook, "build_session_context") as build,
                    mock.patch.object(hook, "write_hook_health"),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                ):
                    result = hook.main(["session-start", "--strict"])
            finally:
                child.wait(timeout=5)

        lines = output.getvalue().splitlines()
        self.assertEqual(result, 1)
        self.assertEqual(len(lines), 1)
        context = json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bağlamı güvenli biçimde doğrulanamadı", context)
        build.assert_not_called()

    def test_session_start_checks_deadline_after_context_before_heavy_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            deadline = time.monotonic() + 0.03
            payload = {"session_id": "late-context", "cwd": str(vault)}
            output = io.StringIO()

            def slow_context(*_args: object, **_kwargs: object) -> str:
                time.sleep(0.12)
                return "ctx"

            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                mock.patch.object(hook, "build_session_context", side_effect=slow_context) as build,
                mock.patch.object(flush, "maybe_trigger_compile") as trigger,
                mock.patch.object(hook, "write_hook_health"),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", output),
            ):
                result = hook.main(["session-start", "--strict"])

        lines = output.getvalue().splitlines()
        self.assertEqual(result, 1)
        self.assertEqual(len(lines), 1)
        context = json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bağlamı güvenli biçimde doğrulanamadı", context)
        build.assert_called_once()
        trigger.assert_not_called()

    def test_privacy_boundary_failure_blocks_prompt_without_claiming_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            session_id = "privacy-contention"
            marker = memory_ledger._read_only_path(state, session_id)
            ready = state / "privacy-lock-ready"
            holder_script = (
                "import sys,time;"
                "from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);"
                "from file_lock import locked;"
                "resource=Path(sys.argv[2]);ready=Path(sys.argv[3]);"
                "guard=locked(resource);guard.__enter__();"
                "ready.write_text('ready', encoding='ascii');"
                "time.sleep(float(sys.argv[4]));"
                "guard.__exit__(None,None,None)"
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_script,
                    str(SCRIPTS_DIR),
                    str(marker),
                    str(ready),
                    "0.8",
                ],
                cwd=vault,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                limit = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < limit:
                    time.sleep(0.01)
                if not ready.exists():
                    raise AssertionError("privacy lock holder did not start")
                payload = {
                    "session_id": session_id,
                    "prompt": "Do not modify files or settings.",
                    "cwd": str(vault),
                }
                output = io.StringIO()
                with (
                    mock.patch.object(hook, "VAULT_ROOT", vault),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "_hook_deadline", return_value=time.monotonic() + 0.15),
                    mock.patch.object(hook, "handle_user_prompt") as handle,
                    mock.patch.object(hook, "write_hook_health"),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                ):
                    result = hook.main(["user-prompt", "--strict"])
            finally:
                child.wait(timeout=5)

        lines = output.getvalue().splitlines()
        self.assertEqual(result, 1)
        self.assertEqual(len(lines), 1)
        emitted = json.loads(lines[0])
        self.assertEqual(emitted["decision"], "block")
        self.assertIn("Gizlilik kapsamı", emitted["reason"])
        self.assertFalse(marker.exists())
        handle.assert_not_called()

    def test_session_only_and_forget_boundary_failures_block_prompt(self) -> None:
        cases = (
            ("Bu konuşmada kalsın.", "mark_session_only"),
            ("Şunu unut: Ankara.", "suppress_derived_memory"),
        )
        for prompt, boundary_name in cases:
            with self.subTest(boundary=boundary_name):
                with tempfile.TemporaryDirectory() as temporary:
                    vault = Path(temporary)
                    state = vault / ".codex/scripts/.state"
                    deadline = time.monotonic() + 30
                    payload = {
                        "session_id": "privacy-boundary-failure",
                        "prompt": prompt,
                        "cwd": str(vault),
                    }
                    output = io.StringIO()
                    with (
                        mock.patch.object(hook, "VAULT_ROOT", vault),
                        mock.patch.object(hook, "STATE_DIR", state),
                        mock.patch.object(hook, "_validate_hook_scope"),
                        mock.patch.object(
                            hook,
                            "_hook_deadline",
                            return_value=deadline,
                        ),
                        mock.patch.object(
                            hook,
                            boundary_name,
                            side_effect=OSError("privacy-boundary-failure"),
                        ) as boundary,
                        mock.patch.object(hook, "handle_user_prompt") as handle,
                        mock.patch.object(hook, "write_hook_health"),
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                        mock.patch.object(sys, "stdout", output),
                    ):
                        result = hook.main(["user-prompt", "--strict"])

                lines = output.getvalue().splitlines()
                self.assertEqual(result, 1)
                self.assertEqual(len(lines), 1)
                emitted = json.loads(lines[0])
                self.assertEqual(emitted["decision"], "block")
                self.assertIn("Gizlilik kapsamı", emitted["reason"])
                boundary.assert_called_once()
                self.assertEqual(boundary.call_args.kwargs["deadline"], deadline)
                handle.assert_not_called()

    def test_session_only_and_forget_boundary_timeouts_block_without_persistence(self) -> None:
        cases = (
            (
                "Bu konuşmada kalsın.",
                "session-only",
                lambda state, _private: memory_ledger.session_only_path(
                    state, "privacy-timeout"
                ),
            ),
            (
                "Şunu unut: Ankara.",
                "forget",
                lambda _state, private: private / "controls" / "suppressions.jsonl",
            ),
        )
        holder_script = (
            "import sys,time;"
            "from pathlib import Path;"
            "sys.path.insert(0,sys.argv[1]);"
            "from file_lock import locked;"
            "resource=Path(sys.argv[2]);ready=Path(sys.argv[3]);"
            "guard=locked(resource);guard.__enter__();"
            "ready.write_text('ready', encoding='ascii');"
            "time.sleep(float(sys.argv[4]));"
            "guard.__exit__(None,None,None)"
        )
        for prompt, boundary_name, resource_for in cases:
            with self.subTest(boundary=boundary_name):
                with tempfile.TemporaryDirectory() as temporary:
                    vault = Path(temporary)
                    state = vault / ".codex/scripts/.state"
                    state.mkdir(parents=True)
                    private = vault / "private-memory"
                    resource = resource_for(state, private)
                    ready = state / f"{boundary_name}-lock-ready"
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            holder_script,
                            str(SCRIPTS_DIR),
                            str(resource),
                            str(ready),
                            "0.8",
                        ],
                        cwd=vault,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    try:
                        limit = time.monotonic() + 5
                        while not ready.exists() and time.monotonic() < limit:
                            time.sleep(0.01)
                        if not ready.exists():
                            raise AssertionError(
                                f"{boundary_name} lock holder did not start"
                            )
                        payload = {
                            "session_id": "privacy-timeout",
                            "prompt": prompt,
                            "cwd": str(vault),
                        }
                        output = io.StringIO()
                        with (
                            mock.patch.object(hook, "VAULT_ROOT", vault),
                            mock.patch.object(hook, "STATE_DIR", state),
                            mock.patch.object(hook, "LOCAL_MEMORY_ROOT", private),
                            mock.patch.object(hook, "_validate_hook_scope"),
                            mock.patch.object(
                                hook,
                                "_hook_deadline",
                                return_value=time.monotonic() + 0.15,
                            ),
                            mock.patch.object(hook, "handle_user_prompt") as handle,
                            mock.patch.object(hook, "write_hook_health"),
                            mock.patch.object(
                                sys, "stdin", io.StringIO(json.dumps(payload))
                            ),
                            mock.patch.object(sys, "stdout", output),
                        ):
                            result = hook.main(["user-prompt", "--strict"])
                    finally:
                        child.wait(timeout=5)

                lines = output.getvalue().splitlines()
                self.assertEqual(result, 1)
                self.assertEqual(len(lines), 1)
                emitted = json.loads(lines[0])
                self.assertEqual(emitted["decision"], "block")
                self.assertIn("Gizlilik kapsamı", emitted["reason"])
                self.assertFalse(resource.exists())
                handle.assert_not_called()

    def test_session_start_receipt_failure_does_not_emit_second_context_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            deadline = time.monotonic() + 30
            payload = {"session_id": "receipt-failure", "cwd": str(vault)}
            output = io.StringIO()
            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                mock.patch.object(hook, "build_session_context", return_value="ctx"),
                mock.patch.object(flush, "maybe_trigger_compile", return_value=False),
                mock.patch.object(hook, "inspect_worker_queue", side_effect=[_queue_report(), _queue_report()]),
                mock.patch.object(
                    hook,
                    "record_hook_runtime",
                    side_effect=hook.LockUnavailable("receipt-busy"),
                ),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", output),
            ):
                result = hook.main(["session-start", "--strict"])

        lines = output.getvalue().splitlines()
        self.assertEqual(result, 1)
        self.assertEqual(len(lines), 1)
        emitted = json.loads(lines[0])
        self.assertEqual(
            emitted["hookSpecificOutput"]["hookEventName"],
            "SessionStart",
        )
        self.assertEqual(
            emitted["hookSpecificOutput"]["additionalContext"],
            "ctx",
        )

    def test_user_prompt_receipt_failure_does_not_emit_second_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            deadline = time.monotonic() + 30
            payload = {"session_id": "prompt-receipt-failure", "prompt": "merhaba", "cwd": str(vault)}
            output = io.StringIO()
            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "_validate_hook_scope"),
                mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                mock.patch.object(hook, "handle_user_prompt", return_value="ctx"),
                mock.patch.object(
                    hook,
                    "record_hook_runtime",
                    side_effect=hook.LockUnavailable("receipt-busy"),
                ),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", output),
            ):
                result = hook.main(["user-prompt", "--strict"])

        lines = output.getvalue().splitlines()
        self.assertEqual(result, 1)
        self.assertEqual(len(lines), 1)
        emitted = json.loads(lines[0])
        self.assertEqual(
            emitted["hookSpecificOutput"]["hookEventName"],
            "UserPromptSubmit",
        )
        self.assertEqual(
            emitted["hookSpecificOutput"]["additionalContext"],
            "ctx",
        )

    def test_session_start_pre_emit_failures_emit_one_safe_context_warning(self) -> None:
        failures = (
            workers.LockUnavailable("scope-busy"),
            OSError("context-unavailable"),
            memory_ledger.MemoryPreferenceError("memory-preferences-changed"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with tempfile.TemporaryDirectory() as temporary:
                    vault = Path(temporary)
                    state = vault / ".codex/scripts/.state"
                    deadline = time.monotonic() + 30
                    payload = {"session_id": "pre-emit-failure", "cwd": str(vault)}
                    output = io.StringIO()
                    with (
                        mock.patch.object(hook, "VAULT_ROOT", vault),
                        mock.patch.object(hook, "STATE_DIR", state),
                        mock.patch.object(hook, "_validate_hook_scope"),
                        mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                        mock.patch.object(hook, "build_session_context", side_effect=failure),
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                        mock.patch.object(sys, "stdout", output),
                    ):
                        result = hook.main(["session-start", "--strict"])

                lines = output.getvalue().splitlines()
                self.assertEqual(result, 1)
                self.assertEqual(len(lines), 1)
                context = json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]
                self.assertIn("bağlamı güvenli biçimde doğrulanamadı", context)
                self.assertIn("Ham notlara veya eski önbelleğe geçme", context)

    def test_session_start_holds_scope_lock_until_maintenance_admission(self) -> None:
        """A marker completing after the snapshot must wait for SessionStart writes."""
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / ".codex/scripts/.state"
            attempt = state / "marker-attempted"
            done = state / "marker-completed"
            snapshot = state / "snapshot-taken"
            session_id = "racing-session"
            child = None
            observed: list[bool] = []
            marker_script = (
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from memory_ledger import mark_read_only_turn\n"
                "state = Path(sys.argv[2])\n"
                "Path(sys.argv[3]).write_text('1', encoding='ascii')\n"
                "mark_read_only_turn(state, sys.argv[5])\n"
                "Path(sys.argv[4]).write_text('1', encoding='ascii')\n"
            )

            def build(_vault: Path, _state: Path, **_kwargs: object) -> str:
                nonlocal child
                _state.mkdir(parents=True, exist_ok=True)
                snapshot.write_text("1", encoding="ascii")
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        marker_script,
                        str(SCRIPTS_DIR),
                        str(state),
                        str(attempt),
                        str(done),
                        session_id,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                limit = time.monotonic() + 5
                while not attempt.exists() and time.monotonic() < limit:
                    time.sleep(0.01)
                if not attempt.exists():
                    raise AssertionError("marker writer did not reach the snapshot boundary")
                time.sleep(0.15)
                return "ctx"

            deadline = time.monotonic() + 30
            payload = {"session_id": session_id, "cwd": str(vault)}
            snapshot_exists = False
            done_exists = False
            try:
                with (
                    mock.patch.object(hook, "VAULT_ROOT", vault),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                    mock.patch.object(hook, "build_session_context", side_effect=build),
                    mock.patch.object(
                        flush,
                        "maybe_trigger_compile",
                        side_effect=lambda *_args, **_kwargs: observed.append(done.exists()) or False,
                    ),
                    mock.patch.object(hook, "inspect_worker_queue", return_value=_queue_report()),
                    mock.patch.object(hook, "_emit_context"),
                    mock.patch.object(hook, "record_hook_runtime"),
                    mock.patch.object(hook, "clear_hook_health"),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", io.StringIO()),
                ):
                    result = hook.main(["session-start", "--strict"])
            finally:
                if child is not None:
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                snapshot_exists = snapshot.is_file()
                done_exists = done.is_file()

        self.assertEqual(result, 0)
        self.assertEqual(observed, [False])
        self.assertTrue(snapshot_exists)
        self.assertTrue(done_exists)

    def test_final_receipts_bound_locks_and_persist_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            deadline = time.monotonic() + 30
            seen: list[tuple[Path, float | None]] = []
            real_locked = hook.locked

            @contextmanager
            def capture(path: Path, **kwargs: object):
                seen.append((path, kwargs.get("timeout")))
                with real_locked(path, **kwargs):
                    yield

            payload = {"session_id": "receipt-session", "cwd": "synthetic"}
            context = "[Hafıza: Kimlik]\nCevo"
            with mock.patch.object(hook, "locked", side_effect=capture):
                hook.record_hook_runtime(
                    "session-start",
                    payload,
                    state,
                    now=1234,
                    context=context,
                    deadline=deadline,
                )
                hook.write_hook_health(
                    state,
                    payload,
                    status="error",
                    error="synthetic-failure",
                    deadline=deadline,
                )

            runtime = json.loads((state / "runtime-session-start.json").read_text(encoding="utf-8"))
            health = json.loads((state / "hook-health.json").read_text(encoding="utf-8"))

        self.assertEqual(runtime["outcome"], "emitted")
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["error"], "synthetic-failure")
        self.assertGreaterEqual(len(seen), 4)
        self.assertTrue(all(timeout is not None for _path, timeout in seen))
        self.assertTrue(all(0 <= timeout <= 30 for _path, timeout in seen if timeout is not None))

    def test_maintenance_and_queue_admission_use_the_shared_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            deadline = time.monotonic() + 30
            seen: list[tuple[Path, float | None]] = []

            @contextmanager
            def capture(path: Path, **kwargs: object):
                seen.append((path, kwargs.get("timeout")))
                yield

            with (
                mock.patch.object(workers, "locked", side_effect=capture),
                mock.patch.object(workers, "enqueue_job", return_value=state / "job.json") as enqueue,
                mock.patch.object(workers, "ensure_supervisor") as ensure,
            ):
                result = workers.enqueue_maintenance(
                    state,
                    vault_root=root,
                    deadline=deadline,
                )

        lane = state / "maintenance"
        self.assertEqual(result, state / "job.json")
        self.assertEqual(enqueue.call_args.args[:3], (lane, "maintenance", {}))
        self.assertEqual(enqueue.call_args.kwargs["deadline"], deadline)
        ensure.assert_called_once_with(
            lane,
            vault_root=root,
            launcher=mock.ANY,
            deadline=deadline,
        )
        self.assertEqual([path for path, _timeout in seen], [
            lane / "maintenance-admission",
            lane / "worker-queue",
        ])
        self.assertTrue(all(timeout is not None for _path, timeout in seen))

    def test_enqueue_transport_sequence_and_job_writes_share_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            transcript = root / "transcript.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            deadline = time.monotonic() + 30
            seen: list[tuple[Path, float | None]] = []
            real_atomic_write_json = workers.atomic_write_json

            def capture(path: Path, payload: object, **kwargs: object) -> None:
                seen.append((path, kwargs.get("deadline")))
                real_atomic_write_json(path, payload, **kwargs)

            with (
                mock.patch.object(workers, "atomic_write_json", side_effect=capture),
                mock.patch.object(workers, "ensure_supervisor"),
            ):
                result = workers.enqueue_flush(
                    state,
                    {"session_id": "enqueue-deadline", "transcript_path": str(transcript)},
                    "turnend",
                    vault_root=root,
                    deadline=deadline,
                )

        self.assertEqual(result.parent.name, "pending")
        self.assertGreaterEqual(len(seen), 3)
        self.assertTrue(all(value == deadline for _path, value in seen))

    def test_queue_admission_lookup_helpers_share_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            transcript = root / "transcript.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            workers._ensure_job_dirs(state)
            event_iso = "2026-09-12T00:00:00+00:00"
            payloads: list[dict[str, object]] = []
            for name in ("hookin-first.json", "hookin-second.json"):
                hook_input = state / name
                hook_input.write_text(
                    json.dumps(
                        {
                            "delivery_schema_version": workers.HOOK_INPUT_SCHEMA_VERSION,
                            "session_id": "queue-admission-deadline",
                            "transcript_path": str(transcript),
                            "reason": "turnend",
                            "event_iso": event_iso,
                        }
                    ),
                    encoding="utf-8",
                )
                payloads.append(
                    {
                        "hook_input": str(hook_input),
                        "reason": "turnend",
                        "event_iso": event_iso,
                    }
                )
            deadline = time.monotonic() + 30
            with (
                mock.patch.object(
                    workers,
                    "_find_hook_input_job_locked",
                    wraps=workers._find_hook_input_job_locked,
                ) as find_job,
                mock.patch.object(
                    workers,
                    "_referenced_hook_inputs_locked",
                    wraps=workers._referenced_hook_inputs_locked,
                ) as references,
            ):
                first = workers._enqueue_job_locked(
                    state,
                    "flush",
                    payloads[0],
                    observed_now=100,
                    deadline=deadline,
                )
                retained = workers._enqueue_job_locked(
                    state,
                    "flush",
                    payloads[1],
                    observed_now=101,
                    deadline=deadline,
                )

        self.assertEqual(find_job.call_args_list, [
            mock.call(state, payloads[0], deadline=deadline),
            mock.call(state, payloads[1], deadline=deadline),
        ])
        references.assert_called_once()
        self.assertEqual(references.call_args.kwargs["deadline"], deadline)
        self.assertIn(
            first.resolve(strict=False),
            references.call_args.kwargs["excluded_paths"],
        )
        self.assertEqual(retained, first)

    def test_maintenance_quarantine_writes_share_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            lane = state / "maintenance"
            workers._ensure_job_dirs(lane)
            (lane / "worker-jobs" / "pending" / "job-corrupt.json").write_text(
                "{bozuk",
                encoding="utf-8",
            )
            deadline = time.monotonic() + 30
            seen_bytes: list[tuple[Path, float | None]] = []
            seen_json: list[tuple[Path, float | None]] = []
            real_atomic_write_bytes = workers.atomic_write_bytes
            real_atomic_write_json = workers.atomic_write_json

            def capture_bytes(path: Path, payload: bytes, **kwargs: object) -> None:
                seen_bytes.append((path, kwargs.get("deadline")))
                real_atomic_write_bytes(path, payload, **kwargs)

            def capture_json(path: Path, payload: object, **kwargs: object) -> None:
                seen_json.append((path, kwargs.get("deadline")))
                real_atomic_write_json(path, payload, **kwargs)

            with (
                mock.patch.object(workers, "atomic_write_bytes", side_effect=capture_bytes),
                mock.patch.object(workers, "atomic_write_json", side_effect=capture_json),
            ):
                result = workers.enqueue_maintenance(
                    state,
                    vault_root=root,
                    start_supervisor=False,
                    deadline=deadline,
                )

        self.assertEqual(result.parent.name, "pending")
        self.assertEqual(len(seen_bytes), 1)
        self.assertTrue(any(path.parent.name == "quarantined" for path, _value in seen_json))
        self.assertGreaterEqual(len(seen_json), 3)
        self.assertTrue(all(value == deadline for _path, value in seen_bytes + seen_json))

    def test_supervisor_launch_failure_receipt_writes_share_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state"
            deadline = time.monotonic() + 30
            seen: list[tuple[str | None, float | None]] = []
            real_atomic_write_json = workers.atomic_write_json

            def capture(path: Path, payload: object, **kwargs: object) -> None:
                if path == state / "worker-supervisor.json":
                    seen.append((payload.get("status") if isinstance(payload, dict) else None,
                                 kwargs.get("deadline")))
                real_atomic_write_json(path, payload, **kwargs)

            with (
                mock.patch.object(workers, "atomic_write_json", side_effect=capture),
                mock.patch.object(
                    workers,
                    "spawn_detached",
                    side_effect=OSError("synthetic-launch-failure"),
                ),
            ):
                with self.assertRaises(OSError):
                    workers.ensure_supervisor(
                        state,
                        vault_root=root,
                        deadline=deadline,
                    )

        self.assertEqual([status for status, _value in seen], ["launching", "failed"])
        self.assertTrue(all(value == deadline for _status, value in seen))

    def test_maybe_trigger_compile_passes_deadline_to_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deadline = time.monotonic() + 30
            launcher = mock.Mock()
            with (
                mock.patch.object(flush.compile_state, "load_publication", return_value=None),
                mock.patch.object(flush.compile_state, "has_changes", return_value=True),
                mock.patch.object(workers, "enqueue_maintenance") as enqueue,
            ):
                result = flush.maybe_trigger_compile(
                    root,
                    popen_factory=launcher,
                    deadline=deadline,
                )

        self.assertTrue(result)
        enqueue.assert_called_once_with(
            root / ".codex/scripts/.state",
            vault_root=root,
            launcher=launcher,
            deadline=deadline,
        )

    def test_queue_inspection_bounds_its_read_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            deadline = time.monotonic() + 30
            seen: list[tuple[Path, float | None]] = []

            @contextmanager
            def capture(path: Path, **kwargs: object):
                seen.append((path, kwargs.get("timeout")))
                yield

            with mock.patch.object(workers, "locked", side_effect=capture):
                report = workers.inspect_worker_queue(state, deadline=deadline)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(seen[0][0], state / "worker-queue")
        self.assertIsNotNone(seen[0][1])

    def test_queue_scan_stops_when_deadline_expires_during_receipt_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            for index in range(4):
                workers.enqueue_job(
                    state,
                    "flush",
                    {"source": f"scan-{index}"},
                    start_supervisor=False,
                    now=100 + index,
                )
            real_load_job = workers._load_job
            loaded: list[Path] = []

            def slow_load(path: Path) -> dict[str, object]:
                loaded.append(path)
                time.sleep(0.04)
                return real_load_job(path)

            deadline = time.monotonic() + 0.07
            with mock.patch.object(workers, "_load_job", side_effect=slow_load):
                with self.assertRaises(workers.WorkerDeliveryTimeout):
                    workers.inspect_worker_queue(state, deadline=deadline)

        self.assertGreaterEqual(len(loaded), 1)
        self.assertLess(len(loaded), 4)

    def test_queue_quarantine_hash_scan_receives_deadline_and_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            workers._ensure_job_dirs(state)
            job_id = "a" * 32
            quarantine = workers._job_root(state) / "quarantined"
            payload_path = quarantine / f"job-{job_id}.payload"
            payload = b"queue-payload\n" * 200_000
            payload_path.write_bytes(payload)
            tombstone = quarantine / f"job-{job_id}.json"
            tombstone.write_text(
                json.dumps(
                    {
                        "schema_version": workers.JOB_SCHEMA_VERSION,
                        "job_id": job_id,
                        "status": "quarantined",
                        "reason_code": "worker-job-json-invalid",
                        "payload_sha256": hashlib.sha256(payload).hexdigest(),
                        "payload_file": payload_path.name,
                    }
                ),
                encoding="utf-8",
            )
            deadline = time.monotonic() + 30
            with mock.patch.object(
                workers,
                "_sha256_file_with_deadline",
                wraps=workers._sha256_file_with_deadline,
            ) as digest:
                report = workers.inspect_worker_queue(state, deadline=deadline)

        self.assertEqual(report["counts"]["quarantined"], 1)
        digest.assert_called_once_with(payload_path, deadline=deadline)

    def test_stop_profile_block_survives_telemetry_timeout(self) -> None:
        for failed_telemetry in ("write_hook_health", "record_hook_runtime"):
            with self.subTest(failed_telemetry=failed_telemetry):
                with tempfile.TemporaryDirectory() as temporary:
                    vault = Path(temporary)
                    state = vault / ".codex/scripts/.state"
                    deadline = time.monotonic() + 30
                    payload = {
                        "session_id": "stop-profile-timeout",
                        "cwd": str(vault),
                        "transcript_path": str(vault / "transcript.jsonl"),
                        "stop_hook_active": False,
                    }
                    profile = mock.Mock()
                    profile.profile_issues.return_value = ("profile-invalid",)
                    memory_context = mock.MagicMock()
                    memory_context.__enter__.return_value = profile
                    memory_context.__exit__.return_value = False
                    output = io.StringIO()
                    failure = hook.LockUnavailable("telemetry-busy")
                    health_side_effect = failure if failed_telemetry == "write_hook_health" else None
                    runtime_side_effect = failure if failed_telemetry == "record_hook_runtime" else None
                    with (
                        mock.patch.object(hook, "VAULT_ROOT", vault),
                        mock.patch.object(hook, "STATE_DIR", state),
                        mock.patch.object(hook, "_validate_hook_scope"),
                        mock.patch.object(hook, "_hook_deadline", return_value=deadline),
                        mock.patch.object(hook, "enqueue_flush"),
                        mock.patch.object(hook, "memory_read", return_value=memory_context),
                        mock.patch.object(
                            hook,
                            "write_hook_health",
                            side_effect=health_side_effect,
                        ),
                        mock.patch.object(
                            hook,
                            "record_hook_runtime",
                            side_effect=runtime_side_effect,
                        ),
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                        mock.patch.object(sys, "stdout", output),
                    ):
                        result = hook.main(["turn-end", "--strict"])

                lines = output.getvalue().splitlines()
                self.assertEqual(result, 1)
                self.assertEqual(len(lines), 1)
                emitted = json.loads(lines[0])
                self.assertEqual(emitted["decision"], "block")
                self.assertIn("Profil kontrolü başarısız", emitted["reason"])


if __name__ == "__main__":
    unittest.main()
