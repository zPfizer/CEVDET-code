"""Kanca kapsamı ve checkout çözümlemesinin güvenlik regresyonları."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import doctor


def _copy_runtime(target: Path) -> Path:
    codex = target / ".codex"
    (codex / "hooks").mkdir(parents=True)
    (codex / "scripts").mkdir()
    for source in (CODEX_DIR / "scripts").glob("*.py"):
        shutil.copy2(source, codex / "scripts" / source.name)
    shutil.copy2(CODEX_DIR / "hooks" / "hook.py", codex / "hooks" / "hook.py")
    return codex


def _init_git(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for key, value in (("user.name", "Cevo Test"), ("user.email", "cevo@example.invalid")):
        subprocess.run(
            ["git", "config", key, value],
            cwd=root,
            check=True,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "initial"],
        cwd=root,
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _run_hook(
    script: Path,
    cwd: Path,
    payload: dict[str, object] | None = None,
    *,
    event: str = "session-start",
    raw: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    input_data = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    return subprocess.run(
        [sys.executable, str(script), event, "--strict"],
        cwd=cwd,
        input=input_data,
        text=False,
        capture_output=True,
        check=False,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class HookScopeTests(unittest.TestCase):
    def test_missing_or_mismatched_cwd_fails_before_state_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo hook scope ") as temporary:
            root = Path(temporary) / "checkout ü space"
            root.mkdir()
            codex = _copy_runtime(root)
            _init_git(root)
            other = Path(temporary) / "other"
            other.mkdir()
            state = codex / "scripts" / ".state"
            state.mkdir()
            sentinel = state / "sentinel.json"
            sentinel.write_bytes(b"before")
            before = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}

            for payload in (
                {"session_id": "missing-cwd"},
                {"session_id": "mismatched-cwd", "cwd": str(other)},
            ):
                with self.subTest(payload=payload):
                    result = _run_hook(codex / "hooks" / "hook.py", root, payload)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(b"kapsam", result.stderr.lower())
                    after = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
                    self.assertEqual(before, after, result.stderr)

    def test_git_root_mismatch_fails_before_script_root_state_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo hook roots ") as temporary:
            parent = Path(temporary)
            script_root = parent / "script ü space"
            script_root.mkdir()
            codex = _copy_runtime(script_root)
            repo = parent / "repo"
            repo.mkdir()
            _init_git(repo)
            state = codex / "scripts" / ".state"
            state.mkdir()
            sentinel = state / "sentinel.json"
            sentinel.write_bytes(b"before")
            before = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}

            result = _run_hook(
                codex / "hooks" / "hook.py",
                repo,
                {"session_id": "wrong-root", "cwd": str(repo)},
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(b"git ", result.stderr.lower())
            after = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
            self.assertEqual(before, after, result.stderr)

    def test_malformed_input_fails_before_script_root_state_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo hook input ") as temporary:
            parent = Path(temporary)
            script_root = parent / "script ü space"
            script_root.mkdir()
            codex = _copy_runtime(script_root)
            repo = parent / "repo"
            repo.mkdir()
            _init_git(repo)
            state = codex / "scripts" / ".state"

            for raw in (b"{broken", b"[]", b"\xff"):
                with self.subTest(raw=raw):
                    malformed = _run_hook(
                        codex / "hooks" / "hook.py",
                        repo,
                        raw=raw,
                    )
                    self.assertNotEqual(malformed.returncode, 0, malformed.stdout)
                    self.assertIn(b"girdi", malformed.stderr.lower())
                    self.assertFalse(state.exists(), malformed.stderr)

    def test_valid_scope_handler_error_still_records_health(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo hook handler ") as temporary:
            root = Path(temporary) / "checkout ü space"
            root.mkdir()
            codex = _copy_runtime(root)
            _init_git(root)

            result = _run_hook(
                codex / "hooks" / "hook.py",
                root,
                {"session_id": "handler-error", "cwd": str(root)},
                event="turn-end",
            )

            state = codex / "scripts" / ".state"
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertTrue((state / "hook-health.json").is_file(), result.stderr)


class DoctorCompatibilityTests(unittest.TestCase):
    def test_existing_non_main_branch_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo doctor branch ") as temporary:
            root = Path(temporary)
            _init_git(root)
            subprocess.run(
                ["git", "switch", "-c", "worker"],
                cwd=root,
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            check = doctor._git_branch_check(doctor.Context(root))

        self.assertEqual(check.status, "OK")
        self.assertIn("worker", check.evidence)

    def test_missing_repository_stays_failed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo doctor missing ") as temporary:
            check = doctor._git_branch_check(doctor.Context(Path(temporary)))

        self.assertEqual(check.status, "FAIL")
        self.assertIn("repo yok", check.evidence)

    def test_detached_head_is_reported_as_detached(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo doctor detached ") as temporary:
            root = Path(temporary)
            _init_git(root)
            subprocess.run(
                ["git", "checkout", "--detach", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            check = doctor._git_branch_check(doctor.Context(root))

        self.assertEqual(check.status, "WARN")
        self.assertIn("detached", check.evidence.lower())

    def test_hook_interpreter_resolves_path_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo doctor interpreter ") as temporary:
            root = Path(temporary)
            hooks = root / ".codex"
            hooks.mkdir()
            (hooks / "hooks.json").write_text(
                json.dumps({
                    "hooks": {
                        "SessionStart": [{
                            "hooks": [{"commandWindows": "python -c print(1)"}],
                        }],
                    },
                }),
                encoding="utf-8",
            )
            with mock.patch.object(doctor.shutil, "which", return_value=sys.executable) as which:
                check = doctor._hook_interpreter_check(doctor.Context(root))

        which.assert_called_once_with("python")
        self.assertEqual(check.status, "OK")
        self.assertIn("PATH", check.evidence)

    def test_valid_worktree_git_file_is_not_an_unexpected_root_file(self) -> None:
        check = doctor._root_hygiene_check(doctor.Context(CODEX_DIR.parent))

        self.assertNotIn(".git", check.evidence)


if __name__ == "__main__":
    unittest.main()
