"""Sentetik hafıza yolculuğu, gerçek süreç sınırlarıyla.

Diğer testler fonksiyon seviyesinde mock kullanır; burada zincir üretimdeki
gibi ayrı süreçlere ayrılır: hook.py stdin JSON ile çalışır; kuyruk, worker
CLI'si de çağrılarak boşaltılır. Flush model olarak CODEX_CLI_PATH'in
gösterdiği sahte codex ikilisini çağırır; daily dosyası yazılır ve başka bir
session-start o içeriği geri çağırır. Bu test bağımsız arka plan handoff'unu,
gerçek modeli veya App oturumunu doğrulamaz.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from _fixtures import CODEX_DIR
import user_evidence

CANNED_TODO = "Atlas planını yaz."
TRANSCRIPT_MESSAGES = (
    "Atlas projesine başlayalım; hızlı ve net ilerleyelim.",
    "Atlas için ilk adım planı çıkarıyorum.",
    "Tamam, planı yarın yazalım.",
)
TRANSCRIPT_RENDERED = "\n".join(
    f"**{role}:** {message}"
    for role, message in zip(
        ("User", "Assistant", "User"), TRANSCRIPT_MESSAGES
    )
)
CANNED_CLAIM = "Atlas projesinde hızlı ve net ilerleme tercih ediliyor."
CANNED_SUMMARY = f"""## Bağlam
Kullanıcı Atlas hedefini konuştu.

## Önemli Konuşmalar
Yok.

## Alınan Kararlar
Yok.

## Öğrenilenler
- {CANNED_CLAIM} <!-- user-source: {{"quote":{json.dumps(TRANSCRIPT_MESSAGES[0], ensure_ascii=False)},"scope":"project"}} -->

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
        "raw = sys.stdin.buffer.read().decode('utf-8')\n"
        f"expected = {TRANSCRIPT_RENDERED!r}\n"
        "begin = '--- BEGIN UNTRUSTED TRANSCRIPT DATA ---'\n"
        "end = '--- END UNTRUSTED TRANSCRIPT DATA ---'\n"
        "if begin not in raw or end not in raw:\n"
        "    raise SystemExit('journey-transcript-block-missing')\n"
        "block = raw.split(begin, 1)[1].split(end, 1)[0].strip()\n"
        "if block != expected:\n"
        "    raise SystemExit('journey-transcript-mismatch')\n"
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
         "message": TRANSCRIPT_MESSAGES[0]}},
        {"type": "event_msg", "payload": {"type": "agent_message",
         "message": TRANSCRIPT_MESSAGES[1]}},
        {"type": "event_msg", "payload": {"type": "user_message",
         "message": TRANSCRIPT_MESSAGES[2]}},
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
        # Üretim paritesi: Codex host kancayı --strict OLMADAN, fail-open
        # çalıştırır. Sonuç iddiaları dönüş koduna değil çıktılara dayanır.
        [sys.executable, str(vault / ".codex" / "hooks" / "hook.py"), event],
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


def _debug_state(state: Path) -> str:
    """Başarısızlık mesajı için: state listesi, kuyruk içi ve kayıt gövdeleri."""
    lines = [f"state: {sorted(path.name for path in state.glob('*'))}"]
    for stage_dir in sorted((state / "worker-jobs").glob("*")):
        names = sorted(path.name for path in stage_dir.glob("*"))
        if names:
            lines.append(f"worker-jobs/{stage_dir.name}: {names}")
    interesting = (
        *state.glob("*health*.json"),
        *(state / "worker-jobs" / "dead-letter").glob("*.json"),
        *(state / "worker-jobs" / "failed").glob("*.json"),
        *state.glob("flush-*.json"),
    )
    for path in sorted(interesting):
        try:
            lines.append(f"{path.name}: {path.read_text(encoding='utf-8')[:400]}")
        except OSError:
            continue
    return "\n".join(lines)


def _drain_worker(vault: Path, environment: dict[str, str]) -> None:
    """Worker'ı üretim CLI girişiyle deterministik koştur.

    Kancanın başlattığı ayrık supervisor CI runner'ında hook süreciyle
    birlikte ölebilir (job object torunları toplar); yerelde ise yaşar ve
    yaşarken lifetime kilidini tuttuğu için bu çağrı 0 ile hemen döner.
    İki ortamda da sonuç aynı: kuyruk boşalana kadar biri çalışır.
    """
    subprocess.run(
        [
            sys.executable,
            str(vault / ".codex" / "scripts" / "worker_supervisor.py"),
            "--vault", str(vault),
            "--state-dir", str(vault / ".codex" / "scripts" / ".state"),
        ],
        cwd=vault,
        capture_output=True,
        check=False,
        timeout=110,
        env=environment,
        creationflags=FLAGS,
    )


def _queue_idle(state: Path) -> bool:
    jobs = state / "worker-jobs"
    return all(
        not any((jobs / stage).glob("*.json"))
        for stage in ("pending", "claimed", "running")
    )


def _successful_session_ends(state: Path) -> set[str]:
    receipts = (state / "worker-jobs" / "succeeded").glob("*.json")
    return {
        job["job_id"]
        for path in receipts
        if (job := json.loads(path.read_text(encoding="utf-8")))["kind"] == "flush"
        and job["status"] == "succeeded"
        and job["payload"]["reason"] == "sessionend"
    }


class JourneyE2ETests(unittest.TestCase):
    """Yolculuklar süreç sınırlarını gerçek geçer; model stub'ı bile ikili."""

    def _environment(self, stub: Path, home: Path) -> dict[str, str]:
        environment = dict(os.environ)
        environment["CODEX_CLI_PATH"] = str(stub)
        environment["CODEX_HOME"] = str(home)
        return environment

    def _assert_prompt_succeeded(self, result, vault: Path, state: Path, marker: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        emitted = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(emitted["hookEventName"], "UserPromptSubmit")
        context = emitted["additionalContext"]
        receipt_path = state / "runtime-user-prompt.json"
        self.assertTrue(receipt_path.exists(), "user-prompt başarı makbuzu yok")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["event"], "user-prompt")
        self.assertEqual(receipt["event_generation"], 1)
        self.assertEqual(receipt["cwd"], str(vault))
        self.assertEqual(receipt["outcome"], "emitted")
        self.assertEqual(
            receipt["context_sha256"], hashlib.sha256(context.encode("utf-8")).hexdigest()
        )
        self.assertIn(marker, context)

    def test_model_stub_rejects_missing_or_misordered_transcript(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cevo-journey-stub-") as temporary:
            root = Path(temporary)
            _write_stub_codex(root)
            output = root / "answer.md"
            swapped_roles = "\n".join(
                f"**{role}:** {message}"
                for role, message in zip(
                    ("Assistant", "User", "User"), TRANSCRIPT_MESSAGES
                )
            )
            reordered_turns = "\n".join(
                f"**{role}:** {message}"
                for role, message in (
                    ("User", TRANSCRIPT_MESSAGES[2]),
                    ("Assistant", TRANSCRIPT_MESSAGES[1]),
                    ("User", TRANSCRIPT_MESSAGES[0]),
                )
            )
            prompts = (
                ("unrelated input", "journey-transcript-block-missing"),
                ("\n".join(TRANSCRIPT_MESSAGES[:-1]), "journey-transcript-block-missing"),
                (
                    "--- BEGIN UNTRUSTED TRANSCRIPT DATA ---\n"
                    + swapped_roles
                    + "\n--- END UNTRUSTED TRANSCRIPT DATA ---",
                    "journey-transcript-mismatch",
                ),
                (
                    "--- BEGIN UNTRUSTED TRANSCRIPT DATA ---\n"
                    + reordered_turns
                    + "\n--- END UNTRUSTED TRANSCRIPT DATA ---",
                    "journey-transcript-mismatch",
                ),
            )
            for prompt, error in prompts:
                with self.subTest(prompt=prompt, error=error):
                    result = subprocess.run(
                        [sys.executable, str(root / "fake_codex.py"),
                         "--output-last-message", str(output)],
                        input=prompt, text=True, encoding="utf-8",
                        capture_output=True, timeout=10, creationflags=FLAGS,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)
                    self.assertFalse(output.exists())

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
                {"session_id": session_id, "cwd": str(vault),
                 "prompt": "Atlas projesine başlayalım; hızlı ilerleyelim."},
                environment,
            )
            self._assert_prompt_succeeded(prompt, vault, state, "[Vault Arama Sonucu]")
            # Prompt yalnız oturumu açar; kayıt session-end'den gelmeli.
            self.assertEqual(list((state / "worker-jobs").glob("*/*.json")), [])

            ended = _run_hook(vault, "session-end", payload, environment)
            self.assertEqual(ended.returncode, 0, ended.stderr)
            session_end_key = hashlib.sha256(
                session_id.encode("utf-8")
            ).hexdigest()
            session_end_receipt = (
                state / f"runtime-session-end-{session_end_key}.json"
            )
            self.assertTrue(
                session_end_receipt.is_file(),
                "session-end runtime makbuzu yok",
            )
            session_end_runtime = json.loads(
                session_end_receipt.read_text(encoding="utf-8")
            )
            self.assertEqual(session_end_runtime["event"], "session-end")
            self.assertEqual(session_end_runtime["session_key"], session_end_key)
            _drain_worker(vault, environment)

            daily = vault / "daily" / f"{datetime.date.today().isoformat()}.md"

            def daily_has_summary() -> bool:
                try:
                    return CANNED_TODO in daily.read_text(encoding="utf-8")
                except OSError:
                    return False

            self.assertTrue(
                _wait_until(
                    lambda: daily_has_summary() and bool(_successful_session_ends(state)),
                    DAILY_TIMEOUT_SECONDS,
                ),
                "daily yazılmadı;\n"
                + _debug_state(state)
                + f"\nprompt-stdout: {prompt.stdout[:500]}"
                + f"\nend-stdout: {ended.stdout[:500]}\nend-stderr: {ended.stderr[:500]}",
            )
            first_receipts = _successful_session_ends(state)
            self.assertEqual(len(first_receipts), 1)
            daily_text = daily.read_text(encoding="utf-8")
            evidence_match = re.search(
                r"<!-- user-evidence: (\{[^\n]+\}) -->", daily_text
            )
            self.assertIsNotNone(evidence_match)
            assert evidence_match is not None
            evidence_record = json.loads(evidence_match.group(1))
            self.assertEqual(evidence_record["claim"], CANNED_CLAIM)
            self.assertEqual(
                evidence_record["quote"], TRANSCRIPT_MESSAGES[0]
            )
            self.assertEqual(evidence_record["scope"], "project")
            self.assertEqual(evidence_record["previous_assistant"], "")
            self.assertEqual(
                evidence_record["message_hash"],
                hashlib.sha256(
                    TRANSCRIPT_MESSAGES[0].encode("utf-8")
                ).hexdigest(),
            )
            self.assertEqual(
                user_evidence.evidence_for(
                    daily_text,
                    evidence_record["id"],
                    CANNED_CLAIM,
                ),
                evidence_record,
            )
            evidence_link = (
                f"[[daily/{datetime.date.today().isoformat()}#user-"
                f"{evidence_record['id']}|Kullanıcı dayanağı; kapsam: project]]"
            )
            self.assertIn(evidence_link, daily_text)

            # Aynı kapanışın tekrarı ikinci bir kayıt üretmemeli (idempotency).
            repeated = _run_hook(vault, "session-end", payload, environment)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            _drain_worker(vault, environment)
            self.assertTrue(
                _wait_until(
                    lambda: _queue_idle(state)
                    and bool(_successful_session_ends(state) - first_receipts),
                    DAILY_TIMEOUT_SECONDS,
                ),
                "tekrar kapanış başarılı bir makbuz üretmedi;\n" + _debug_state(state),
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
            self._assert_prompt_succeeded(prompt, vault, state, "Salt okunur kapsam açık")

            ended = _run_hook(vault, "session-end", payload, environment)
            self.assertEqual(ended.returncode, 0, ended.stderr)

            # Salt okunur kapanış hiçbir iş kuyruklamaz: bekleme gerekmez,
            # dönüş anında ne hookin taşıyıcısı ne pending iş ne daily olmalı.
            self.assertEqual(list(state.glob("hookin-*.json")), [])
            self.assertEqual(list((state / "worker-jobs").glob("*/*.json")), [])
            self.assertFalse((state / "worker-sequence.json").exists())
            self.assertFalse((vault / "daily" / f"{datetime.date.today().isoformat()}.md").exists())


if __name__ == "__main__":
    unittest.main()
