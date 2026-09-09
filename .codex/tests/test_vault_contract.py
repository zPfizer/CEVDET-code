"""Dışa aktarılan checkout için sınırlı doküman ve kanca sözleşmeleri."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import sys
import tempfile
import time
import unittest

from _fixtures import CODEX_DIR  # sys.path seam

import hook  # noqa: E402


class VaultContractTests(unittest.TestCase):
    def test_session_start_loads_complete_untruncated_rules_contract(self) -> None:
        from test_second_brain_acceptance import _seed_vault

        headings = "\n".join((
            "Doğal konuşma", "Kendiliğinden hafıza", "Kullanıcı sahipliği",
            "Bilgi niteliği", "Doğal kontroller", "İlgili geçmiş",
            "Eksik bilgi", "Proje sınırı",
        ))
        end_marker = "SYNTHETIC_RULES_END"
        padding = "x" * (2_490 - len(headings) - len(end_marker) - 2)
        expected_rules = f"{headings}\n{padding}\n{end_marker}"
        self.assertGreater(len(expected_rules), 2_400)
        self.assertLessEqual(len(expected_rules), 2_500)

        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            rules_file = vault / "🔮 850-Companion" / "Kurallar.md"
            rules_file.write_text(expected_rules, encoding="utf-8")
            context = hook.build_session_context(
                vault,
                vault / ".codex" / "scripts" / ".state",
                consume_reflection=False,
            )
        rules = context.split("[Hafıza: Kurallar]\n", 1)[1].split(
            "\n\n[Hafıza:", 1
        )[0]
        self.assertEqual(expected_rules, rules)

    def test_journal_latest_pointer_matches_first_current_entry_and_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / "Journal.md"
            journal.write_text(
                "<!-- journal-latest: 2026-09-08 -->\n"
                "## Güncel\n\n### 2026-09-08\nSentetik günlük.\n",
                encoding="utf-8",
            )
            content = journal.read_text(encoding="utf-8")
            latest = re.search(r"^<!-- journal-latest: (.+) -->$", content, re.MULTILINE)
            self.assertIsNotNone(latest)
            current = content.split("## Güncel", 1)[1]
            first_heading = re.search(r"^### (.+)$", current, re.MULTILINE)
            self.assertIsNotNone(first_heading)
            self.assertEqual(first_heading.group(1), latest.group(1))
            self.assertTrue(hook._last_journal(journal).startswith(f"### {latest.group(1)}"))

    def test_frontmatter_uses_updated_instead_of_modified(self) -> None:
        vault = CODEX_DIR.parent
        offenders = []
        for path in vault.rglob("*.md"):
            if any(part in {".codex", ".git", "daily"} for part in path.parts):
                continue
            if any(
                line.startswith("modified:")
                for line in path.read_text(encoding="utf-8").splitlines()
            ):
                offenders.append(str(path.relative_to(vault)))

        self.assertEqual(offenders, [])

    def test_codex_hooks_wire_all_memory_events(self) -> None:
        config = (CODEX_DIR / "config.toml").read_text(encoding="utf-8")
        hooks = json.loads((CODEX_DIR / "hooks.json").read_text(encoding="utf-8"))

        self.assertIn("hooks = false", config)
        self.assertEqual(
            set(hooks["hooks"]),
            {
                "SessionStart",
                "UserPromptSubmit",
                "PreCompact",
                "SessionEnd",
                "Stop",
            },
        )
        for groups in hooks["hooks"].values():
            for group in groups:
                for handler in group["hooks"]:
                    self.assertIn("commandWindows", handler)
                    self.assertIn(".codex", handler["commandWindows"])
                    self.assertNotIn(".claude", handler["commandWindows"])
                    self.assertNotIn(str(CODEX_DIR.parent), handler["commandWindows"])

    @unittest.skipUnless(os.name == "nt", "Windows command contract")
    def test_session_start_command_runs_from_relocated_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo checkout ") as temporary:
            vault = Path(temporary) / "checkout ü space"
            vault.mkdir()
            codex = vault / ".codex"
            (codex / "hooks").mkdir(parents=True)
            (codex / "scripts").mkdir()
            for source in (CODEX_DIR / "scripts").glob("*.py"):
                shutil.copy2(source, codex / "scripts" / source.name)
            shutil.copy2(CODEX_DIR / "hooks" / "hook.py", codex / "hooks" / "hook.py")
            shutil.copy2(CODEX_DIR / "hooks.json", codex / "hooks.json")
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=vault,
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            nested = vault / "nested" / "work"
            nested.mkdir(parents=True)
            command = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
            self.assertTrue(command.startswith("python -c "), command)
            self.assertNotIn(str(CODEX_DIR.parent), command)
            self.assertNotIn('"', command)
            session_id = "windows-shell-contract"
            key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            live_state = CODEX_DIR / "scripts" / ".state"
            before = {
                path.relative_to(live_state).as_posix(): path.read_bytes()
                for path in live_state.rglob("*")
                if path.is_file()
            } if live_state.is_dir() else None
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/C", command],
                input=json.dumps({
                    "session_id": session_id,
                    "cwd": str(nested),
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                }).encode("utf-8"),
                text=False,
                capture_output=True,
                check=False,
                timeout=10,
                cwd=nested,
            )
            isolated_receipt = codex / "scripts" / ".state" / f"runtime-session-start-{key}.json"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(isolated_receipt.is_file(), result.stderr)
        after = {
            path.relative_to(live_state).as_posix(): path.read_bytes()
            for path in live_state.rglob("*")
            if path.is_file()
        } if live_state.is_dir() else None
        self.assertEqual(before, after, "Shell test changed the live Vault receipt")
        self.assertEqual(
            json.loads(result.stdout.decode("utf-8"))["hookSpecificOutput"]["hookEventName"],
            "SessionStart",
        )

    @unittest.skipUnless(os.name == "nt", "Windows command contract")
    def test_all_windows_hooks_resolve_nested_linked_worktree_without_rewriting(self) -> None:
        hooks = json.loads((CODEX_DIR / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        with tempfile.TemporaryDirectory(prefix="cevo linked checkout ") as temporary:
            parent = Path(temporary)
            source = parent / "source"
            worktree = parent / "worktree with spaces"
            subprocess.run(["git", "init", "--quiet", str(source)], check=True, capture_output=True)
            subprocess.run(["git", "-c", "core.hooksPath=NUL", "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "--quiet",
                            "--allow-empty", "-m", "fixture"], cwd=source, check=True, capture_output=True)
            subprocess.run(["git", "-c", "core.hooksPath=NUL", "worktree", "add", "--detach",
                            str(worktree)], cwd=source, check=True, capture_output=True)
            stub = worktree / ".codex" / "hooks" / "hook.py"
            stub.parent.mkdir(parents=True)
            stub.write_text(
                "import json, sys\nfrom pathlib import Path\n"
                "print(json.dumps({'root': str(Path(__file__).resolve().parents[2]), "
                "'event': sys.argv[1], 'payload': json.load(sys.stdin)}))\n", encoding="utf-8")
            nested = worktree / "nested folder"
            nested.mkdir()
            self.assertTrue((worktree / ".git").is_file())
            for event, groups in hooks.items():
                command = groups[0]["hooks"][0]["commandWindows"]
                # A RED test must never execute the original main-Vault hook.
                self.assertIn("--show-toplevel", command)
                with self.subTest(event=event):
                    payload = {"cwd": str(nested), "hook_event_name": event}
                    result = subprocess.run(command, input=json.dumps(payload), text=True,
                                            capture_output=True, shell=True, cwd=nested, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    observed = json.loads(result.stdout)
                    self.assertEqual(Path(observed["root"]), worktree.resolve())
                    expected_event = {
                        "SessionStart": "session-start",
                        "UserPromptSubmit": "user-prompt",
                        "PreCompact": "pre-compact",
                        "Stop": "turn-end",
                        "SessionEnd": "session-end",
                    }[event]
                    self.assertEqual(observed["event"], expected_event)
                    self.assertEqual(observed["payload"], payload)
            outside = parent / "outside git"
            outside.mkdir()
            result = subprocess.run(command, input="{}", text=True, capture_output=True,
                                    shell=True, cwd=outside, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "", "No fallback may run a different Vault's hook")

    @unittest.skipUnless(os.name == "nt", "Windows command contract")
    def test_precompact_command_returns_before_host_deadline_with_slow_git_and_queue_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo hook deadline ") as temporary:
            vault = Path(temporary) / "checkout ü space"
            vault.mkdir()
            codex = vault / ".codex"
            (codex / "hooks").mkdir(parents=True)
            (codex / "scripts").mkdir()
            for source in (CODEX_DIR / "scripts").glob("*.py"):
                shutil.copy2(source, codex / "scripts" / source.name)
            shutil.copy2(CODEX_DIR / "hooks" / "hook.py", codex / "hooks" / "hook.py")
            shutil.copy2(CODEX_DIR / "hooks.json", codex / "hooks.json")
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=vault,
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for key, value in (("user.name", "Cevo Test"), ("user.email", "cevo@example.invalid")):
                subprocess.run(
                    ["git", "config", key, value],
                    cwd=vault,
                    check=True,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            state = codex / "scripts" / ".state"
            state.mkdir(parents=True)
            transcript = vault / "transcript.jsonl"
            transcript.write_text("{}", encoding="utf-8")
            ready = state / "queue-holder-ready"
            holder_script = (
                "import sys,time;"
                "from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);"
                "from file_lock import locked;"
                "resource=Path(sys.argv[2]);ready=Path(sys.argv[3]);"
                "guard=locked(resource);guard.__enter__();ready.write_text('ready');"
                "time.sleep(float(sys.argv[4]));guard.__exit__(None,None,None)"
            )
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    holder_script,
                    str(CODEX_DIR / "scripts"),
                    str(state / "worker-queue"),
                    str(ready),
                    "4",
                ],
                cwd=vault,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                holder_ready_deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < holder_ready_deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "queue lock holder did not start")
                command = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreCompact"][0]["hooks"][0]["commandWindows"]
                (vault / "slow_git.py").write_text(
                    "import subprocess,time\noriginal_run=subprocess.run\n"
                    "def slow_run(*args, **kwargs):\n"
                    "    if args and args[0][0] == 'git': time.sleep(0.6)\n"
                    "    return original_run(*args, **kwargs)\n"
                    "subprocess.run=slow_run\n", encoding="utf-8",
                )
                command = command.replace(
                    "python -c ", "python -c __import__('runpy').run_path('slow_git.py');", 1,
                )
                payload = {
                    "session_id": "host-deadline",
                    "cwd": str(vault),
                    "transcript_path": str(transcript),
                    "hook_event_name": "PreCompact",
                }
                started = time.monotonic()
                result = subprocess.run(
                    [os.environ.get("COMSPEC", "cmd.exe"), "/C", command],
                    input=json.dumps(payload).encode("utf-8"),
                    cwd=vault,
                    capture_output=True,
                    check=False,
                    timeout=3,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                elapsed = time.monotonic() - started
            finally:
                holder.wait(timeout=8)

            self.assertLess(elapsed, 3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list(state.glob("hookin-*.json"))), 1)
            health = json.loads((state / "hook-health.json").read_text(encoding="utf-8"))
            self.assertEqual(health["status"], "error")

    def test_written_files_have_no_template_placeholders(self) -> None:
        vault = CODEX_DIR.parent
        offenders = []
        for current, directories, files in os.walk(vault):
            directories[:] = [
                name for name in directories
                if name.casefold() not in {
                    ".git", ".state", "__pycache__", ".scratch", ".code-review-graph"
                }
                and not (Path(current) == vault and name.casefold() == "tmp")
            ]
            for name in files:
                path = Path(current) / name
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                if re.search(r"\{\{[A-Z_]+\}\}", text):
                    offenders.append(str(path.relative_to(vault)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
