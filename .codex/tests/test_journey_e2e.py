"""Gerçek kullanıcı yolculuğu, gerçek süreç sınırlarıyla.

Diğer testler fonksiyon seviyesinde mock kullanır; burada zincir üretimdeki
gibi koşar: hook.py stdin JSON ile subprocess olarak çalışır, worker'ı
kendisi başlatır, flush model olarak CODEX_CLI_PATH'in gösterdiği sahte
codex ikilisini (süreç sınırında stub) çağırır, daily dosyası gerçekten
yazılır ve bir SONRAKİ oturumun session-start'ı o içeriği geri çağırır.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from _fixtures import CODEX_DIR

CANNED_TODO = "Atlas planını yaz."
CANNED_SUMMARY = f"""## Bağlam
Kullanıcı Atlas hedefini konuştu.

## Önemli Konuşmalar
Yok.

## Alınan Kararlar
Yok.

## Öğrenilenler
- Kullanıcı hızlı ve net yanıt istiyor.

## Yapılacaklar
- {CANNED_TODO}
"""
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DAILY_TIMEOUT_SECONDS = 120


def _copy_runtime(vault: Path) -> Path:
    codex = vault / ".codex"
    (codex / "hooks").mkdir(parents=True)
    (codex / "scripts").mkdir()
    for source in (CODEX_DIR / "scripts").glob("*.py"):
        shutil.copy2(source, codex / "scripts" / source.name)
    shutil.copy2(CODEX_DIR / "hooks" / "hook.py", codex / "hooks" / "hook.py")
    return codex


def _init_git(root: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True,
            creationflags=FLAGS,
        )

    run("init", "-b", "main")
    run("config", "user.name", "Cevo Journey")
    run("config", "user.email", "journey@example.invalid")
    run("commit", "--allow-empty", "-m", "initial")


def _write_stub_codex(root: Path) -> Path:
    """CODEX_CLI_PATH için süreç sınırında deterministik model."""
    script = root / "fake_codex.py"
    script.write_text(
        "import sys\n"
        "raw = sys.stdin.buffer.read()\n"
        "args = sys.argv[1:]\n"
        "target = None\n"
        "for index, value in enumerate(args):\n"
        "    if value == '--output-last-message' and index + 1 < len(args):\n"
        "        target = args[index + 1]\n"
        "if target:\n"
        "    with open(target, 'w', encoding='utf-8') as handle:\n"
        f"        handle.write({CANNED_SUMMARY!r})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        stub = root / "fake_codex.cmd"
        stub.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        stub = root / "fake_codex"
        stub.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return stub


def _write_transcript(root: Path, session_id: str) -> Path:
    lines = [
        {"type": "event_msg", "payload": {"type": "user_message",
         "message": "Atlas projesine başlayalım; hızlı ve net ilerleyelim."}},
        {"type": "event_msg", "payload": {"type": "agent_message",
         "message": "Atlas için ilk adım planı çıkarıyorum."}},
        {"type": "event_msg", "payload": {"type": "user_message",
         "message": "Tamam, planı yarın yazalım."}},
    ]
    transcript = root / "sessions" / f"rollout-2026-09-11-{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return transcript


def _run_hook(
    vault: Path,
    event: str,
    payload: dict[str, object],
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(vault / ".codex" / "hooks" / "hook.py"), event, "--strict"],
        cwd=vault,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=120,
        env=environment,
        creationflags=FLAGS,
    )


def _wait_until(condition, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def _queue_idle(state: Path) -> bool:
    jobs = state / "worker-jobs"
    return all(
        not any((jobs / stage).glob("*.json"))
        for stage in ("pending", "claimed", "running")
    )


class JourneyE2ETests(unittest.TestCase):
    """Yolculuklar süreç sınırlarını gerçek geçer; model stub'ı bile ikili."""

    def _environment(self, stub: Path, home: Path) -> dict[str, str]:
        environment = dict(os.environ)
        environment["CODEX_CLI_PATH"] = str(stub)
        environment["CODEX_HOME"] = str(home)
        return environment

    def test_full_memory_journey_survives_process_boundaries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo-journey-", ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            vault = root / "vault"
            codex = _copy_runtime(vault)
            _init_git(vault)
            state = codex / "scripts" / ".state"
            stub = _write_stub_codex(root)
            environment = self._environment(stub, root / "codex-home")
            session_id = str(uuid.uuid4())
            transcript = _write_transcript(root, session_id)
            payload = {
                "session_id": session_id,
                "cwd": str(vault),
                "transcript_path": str(transcript),
            }

            prompt = _run_hook(
                vault, "user-prompt",
                {**payload, "prompt": "Atlas projesine başlayalım; hızlı ilerleyelim."},
                environment,
            )
            self.assertEqual(prompt.returncode, 0, prompt.stderr)

            ended = _run_hook(vault, "session-end", payload, environment)
            self.assertEqual(ended.returncode, 0, ended.stderr)

            daily = vault / "daily" / f"{datetime.date.today().isoformat()}.md"

            def daily_has_summary() -> bool:
                try:
                    return CANNED_TODO in daily.read_text(encoding="utf-8")
                except OSError:
                    return False

            self.assertTrue(
                _wait_until(daily_has_summary, DAILY_TIMEOUT_SECONDS),
                f"daily yazılmadı; state: {sorted(p.name for p in state.glob('*'))}",
            )

            # Aynı kapanışın tekrarı ikinci bir kayıt üretmemeli (idempotency).
            repeated = _run_hook(vault, "session-end", payload, environment)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertTrue(
                _wait_until(lambda: _queue_idle(state), DAILY_TIMEOUT_SECONDS),
                "kuyruk boşalmadı",
            )
            self.assertEqual(
                daily.read_text(encoding="utf-8").count(CANNED_TODO), 1
            )

            # Bir SONRAKİ oturum: session-start bağlamı flush'ın yazdığını
            # gerçekten geri çağırıyor mu?
            started = _run_hook(
                vault, "session-start",
                {"session_id": str(uuid.uuid4()), "cwd": str(vault)},
                environment,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            emitted = json.loads(started.stdout)
            context = emitted["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(
                emitted["hookSpecificOutput"]["hookEventName"], "SessionStart"
            )
            self.assertIn(CANNED_TODO, context)

    def test_read_only_turn_blocks_the_whole_write_chain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo-journey-ro-", ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            vault = root / "vault"
            codex = _copy_runtime(vault)
            _init_git(vault)
            state = codex / "scripts" / ".state"
            stub = _write_stub_codex(root)
            environment = self._environment(stub, root / "codex-home")
            session_id = str(uuid.uuid4())
            transcript = _write_transcript(root, session_id)
            payload = {
                "session_id": session_id,
                "cwd": str(vault),
                "transcript_path": str(transcript),
            }

            prompt = _run_hook(
                vault, "user-prompt",
                {**payload, "prompt": "Salt okunur modda sadece incele, dosya değiştirme."},
                environment,
            )
            self.assertEqual(prompt.returncode, 0, prompt.stderr)

            ended = _run_hook(vault, "session-end", payload, environment)
            self.assertEqual(ended.returncode, 0, ended.stderr)

            # Salt okunur kapanış hiçbir iş kuyruklamaz: bekleme gerekmez,
            # dönüş anında ne hookin taşıyıcısı ne pending iş ne daily olmalı.
            self.assertEqual(list(state.glob("hookin-*.json")), [])
            pending = state / "worker-jobs" / "pending"
            self.assertEqual(
                list(pending.glob("*.json")) if pending.is_dir() else [], []
            )
            self.assertFalse((vault / "daily" / f"{datetime.date.today().isoformat()}.md").exists())


if __name__ == "__main__":
    unittest.main()
