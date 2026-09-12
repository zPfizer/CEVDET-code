"""Sentetik hafıza yolculuğu, gerçek süreç sınırlarıyla.

Diğer testler fonksiyon seviyesinde mock kullanır; burada zincir üretimdeki
gibi ayrı süreçlere ayrılır: hook.py stdin JSON ile çalışır; kuyruk, worker
CLI'si de çağrılarak boşaltılır. Flush model olarak CODEX_CLI_PATH'in
gösterdiği sahte codex ikilisini çağırır; daily dosyası yazılır ve başka bir
session-start o içeriği geri çağırır. Bu test bağımsız arka plan handoff'unu,
gerçek modeli veya App oturumunu doğrulamaz.
"""

from __future__ import annotations

from contextlib import ExitStack
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
import process_control
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
    shutil.copy2(CODEX_DIR / "tag-taxonomy.json", codex / "tag-taxonomy.json")
    for source in (CODEX_DIR / "scripts").glob("*.py"):
        shutil.copy2(source, codex / "scripts" / source.name)
    shutil.copy2(CODEX_DIR / "hooks" / "hook.py", codex / "hooks" / "hook.py")
    (codex / "hooks" / "journey_hook_runner.py").write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "scripts = Path(__file__).resolve().parent.parent / 'scripts'\n"
        "if str(scripts) not in sys.path:\n"
        "    sys.path.insert(0, str(scripts))\n"
        "import hook\n"
        "import worker_supervisor\n"
        "\n"
        "def managed_popen(command, **kwargs):\n"
        "    options = dict(kwargs)\n"
        "    if os.name == 'nt':\n"
        "        breakaway = getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0x01000000)\n"
        "        options['creationflags'] = int(options.get('creationflags', 0)) & ~breakaway\n"
        "    state_dir = None\n"
        "    pending = []\n"
        "    command_values = [str(value) for value in command]\n"
        "    if '--state-dir' in command_values:\n"
        "        state_dir = Path(command_values[command_values.index('--state-dir') + 1])\n"
        "        for job_path in sorted((state_dir / 'worker-jobs' / 'pending').glob('*.json')):\n"
        "            try:\n"
        "                job = json.loads(job_path.read_text(encoding='utf-8'))\n"
        "                payload = job.get('payload', {})\n"
        "                hook_input = payload.get('hook_input') if isinstance(payload, dict) else None\n"
        "                transport = None\n"
        "                if isinstance(hook_input, str) and hook_input:\n"
        "                    transport = json.loads(Path(hook_input).read_text(encoding='utf-8'))\n"
        "                pending.append({'job_id': job.get('job_id'), 'kind': job.get('kind'),\n"
        "                                'reason': payload.get('reason') if isinstance(payload, dict) else None,\n"
        "                                'hook_input': hook_input, 'transport': transport})\n"
        "            except (OSError, UnicodeError, json.JSONDecodeError):\n"
        "                continue\n"
        "    process = subprocess.Popen(list(command), **options)\n"
        "    registry = os.environ.get('CEVO_JOURNEY_WORKER_REGISTRY')\n"
        "    if registry:\n"
        "        identity = worker_supervisor.process_control.process_identity(process.pid)\n"
        "        with Path(registry).open('a', encoding='utf-8') as stream:\n"
        "            stream.write(json.dumps({'pid': process.pid, 'identity': identity,\n"
        "                                      'state_dir': str(state_dir) if state_dir else '',\n"
        "                                      'pending': pending}) + '\\n')\n"
        "    return process\n"
        "\n"
        "def enqueue_flush(payload, reason, *, popen_factory=subprocess.Popen, deadline=None):\n"
        "    del popen_factory\n"
        "    return worker_supervisor.enqueue_flush(\n"
        "        hook.STATE_DIR, payload, reason, vault_root=hook.VAULT_ROOT,\n"
        "        launcher=managed_popen, deadline=deadline,\n"
        "    )\n"
        "\n"
        "def ensure_supervisor(state_dir, *, vault_root, launcher=subprocess.Popen, now=None, deadline=None):\n"
        "    del launcher\n"
        "    return worker_supervisor.ensure_supervisor(\n"
        "        state_dir, vault_root=vault_root, launcher=managed_popen,\n"
        "        now=now, deadline=deadline,\n"
        "    )\n"
        "\n"
        "hook.enqueue_flush = enqueue_flush\n"
        "hook.ensure_supervisor = ensure_supervisor\n"
        "raise SystemExit(hook.main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
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
        [
            sys.executable,
            str(vault / ".codex" / "hooks" / "journey_hook_runner.py"),
            event,
        ],
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
    interesting = tuple(
        path
        for path in (
            *state.glob("*.json"),
            *(state / "worker-jobs").glob("*/*.json"),
        )
        if path.name != "worker-sequence.json"
    )
    for path in sorted(interesting):
        try:
            lines.append(f"{path.name}: {path.read_text(encoding='utf-8')[:400]}")
        except OSError:
            continue
    return "\n".join(lines)


class _ManagedWorkerHandle:
    """Popen-shaped owner handle for identity-verified test cleanup."""

    def __init__(self, pid: int, identity: str) -> None:
        self.pid = pid
        self._beyin_process_identity = identity
        self._beyin_command = ["journey-managed-worker", str(pid)]
        self._beyin_process_group = os.name != "nt"

    def poll(self) -> int | None:
        if _posix_process_is_zombie(self.pid):
            return 0
        return None if process_control.process_is_same(
            self.pid, self._beyin_process_identity
        ) else 0

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self._beyin_command, timeout)
            time.sleep(0.05)
        return 0

    def terminate(self) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                creationflags=FLAGS,
            )
        else:
            os.kill(self.pid, 15)

    def kill(self) -> None:
        if os.name == "nt":
            self.terminate()
        else:
            os.kill(self.pid, 9)


def _posix_process_is_zombie(pid: int) -> bool:
    if os.name == "nt":
        return False
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return False
    _prefix, separator, fields = raw.rpartition(") ")
    return bool(separator and fields and fields.split()[0] == "Z")


def _hook_result_diagnostics(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return ""
    return (
        f"\nhook-returncode: {result.returncode}"
        f"\nhook-stdout: {result.stdout[:1000]}"
        f"\nhook-stderr: {result.stderr[:1000]}"
    )


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
        environment["CEVO_JOURNEY_WORKER_REGISTRY"] = str(
            Path(tempfile.gettempdir())
            / f"cevo-journey-workers-{uuid.uuid4().hex}.jsonl"
        )
        return environment

    @staticmethod
    def _managed_worker_records(
        environment: dict[str, str],
    ) -> list[dict[str, object]]:
        path = Path(environment["CEVO_JOURNEY_WORKER_REGISTRY"])
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records: list[dict[str, object]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def _cleanup_managed_workers(self, registry: Path, state: Path) -> None:
        records = self._managed_worker_records(
            {"CEVO_JOURNEY_WORKER_REGISTRY": str(registry)},
        )
        # Flush children may launch maintenance themselves. Its real receipt
        # belongs to this temporary fixture and preserves native birth identity.
        for lane in (state, state / "maintenance"):
            try:
                receipt = json.loads(
                    (lane / "worker-supervisor.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if isinstance(receipt, dict):
                records.append({
                    "pid": receipt.get("owner_pid"),
                    "identity": receipt.get("owner_identity"),
                })
        for record in records:
            pid = record.get("pid")
            identity = record.get("identity")
            if (
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 0
                and isinstance(identity, str)
                and identity
                and self._managed_worker_is_alive(record)
            ):
                process_control.terminate_process_tree(
                    _ManagedWorkerHandle(pid, identity)
                )
        try:
            registry.unlink()
        except FileNotFoundError:
            pass

    def _assert_managed_workers_stopped(
        self, environment: dict[str, str],
    ) -> None:
        def all_stopped() -> bool:
            records = self._managed_worker_records(environment)
            if not records:
                return False
            for record in records:
                if self._managed_worker_is_alive(record):
                    return False
            return True

        self.assertTrue(
            _wait_until(all_stopped, DAILY_TIMEOUT_SECONDS),
            "managed worker süreci kapanmadı",
        )

    @staticmethod
    def _managed_worker_is_alive(record: dict[str, object]) -> bool:
        pid = record.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        identity = record.get("identity")
        if isinstance(identity, str) and identity:
            return (
                process_control.process_is_same(pid, identity)
                and not _posix_process_is_zombie(pid)
            )
        return process_control.pid_is_alive(pid) and not _posix_process_is_zombie(pid)

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

    def _assert_session_runtime_succeeded(
        self, state: Path, event: str, session_id: str,
        expected_generation: int = 1,
        result: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        session_key = hashlib.sha256(
            session_id.encode("utf-8")
        ).hexdigest()
        session_receipt = state / f"runtime-{event}-{session_key}.json"
        self.assertTrue(
            _wait_until(
                lambda: self._session_receipt_has_generation(
                    session_receipt, expected_generation,
                ),
                DAILY_TIMEOUT_SECONDS,
            ),
            f"{event} runtime makbuzu yok;\n"
            + _debug_state(state)
            + _hook_result_diagnostics(result),
        )
        session_runtime = json.loads(
            session_receipt.read_text(encoding="utf-8")
        )
        self.assertEqual(session_runtime["event"], event)
        self.assertEqual(session_runtime["session_key"], session_key)
        self.assertEqual(
            session_runtime["event_generation"], expected_generation
        )

    @staticmethod
    def _session_receipt_has_generation(
        path: Path, expected_generation: int,
    ) -> bool:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        return (
            isinstance(value, dict)
            and value.get("event_generation") == expected_generation
        )

    def _assert_session_end_succeeded(
        self, state: Path, session_id: str, expected_generation: int = 1,
        result: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        self._assert_session_runtime_succeeded(
            state, "session-end", session_id, expected_generation, result,
        )

    def _assert_pending_session_end_transport(
        self, state: Path, session_id: str, transcript: Path,
        environment: dict[str, str],
        excluded_job_ids: set[str] | None = None,
    ) -> tuple[str, str]:
        excluded = excluded_job_ids or set()
        matches: list[tuple[str, str]] = []
        for record in self._managed_worker_records(environment):
            if record.get("state_dir") != str(state):
                continue
            snapshots = record.get("pending")
            if not isinstance(snapshots, list):
                continue
            for snapshot in snapshots:
                if not isinstance(snapshot, dict):
                    continue
                job_id = snapshot.get("job_id")
                hook_input = snapshot.get("hook_input")
                transport = snapshot.get("transport")
                if (
                    not isinstance(job_id, str)
                    or job_id in excluded
                    or snapshot.get("kind") != "flush"
                    or snapshot.get("reason") != "sessionend"
                    or not isinstance(hook_input, str)
                    or not isinstance(transport, dict)
                    or transport.get("session_id") != session_id
                    or transport.get("transcript_path") != str(transcript)
                ):
                    continue
                matches.append((job_id, hook_input))
        self.assertEqual(len(matches), 1, _debug_state(state))
        return matches[0]

    def _assert_session_end_source_was_consumed(
        self, state: Path, job_id: str, hook_input: str,
    ) -> None:
        receipt = state / "worker-jobs" / "succeeded" / f"job-{job_id}.json"
        self.assertTrue(
            _wait_until(receipt.is_file, DAILY_TIMEOUT_SECONDS),
            _debug_state(state),
        )
        job = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(job["kind"], "flush")
        self.assertEqual(job["payload"]["reason"], "sessionend")
        self.assertEqual(job["payload"]["hook_input"], hook_input)

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
        with (
            tempfile.TemporaryDirectory(prefix="cevo-journey-") as temporary,
            ExitStack() as workers_cleanup,
        ):
            root = Path(temporary)
            vault = root / "vault"
            codex = _copy_runtime(vault)
            _init_git(vault)
            state = codex / "scripts" / ".state"
            stub = _write_stub_codex(root)
            environment = self._environment(stub, root / "codex-home")
            workers_cleanup.callback(
                self._cleanup_managed_workers,
                Path(environment["CEVO_JOURNEY_WORKER_REGISTRY"]),
                state,
            )
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
                 "prompt": TRANSCRIPT_MESSAGES[0]},
                environment,
            )
            self._assert_prompt_succeeded(prompt, vault, state, "[Vault Arama Sonucu]")
            # Prompt yalnız oturumu açar; kayıt session-end'den gelmeli.
            self.assertEqual(list((state / "worker-jobs").glob("*/*.json")), [])

            event_date = datetime.date.today().isoformat()
            ended = _run_hook(vault, "session-end", payload, environment)
            self.assertEqual(ended.returncode, 0, ended.stderr)
            self._assert_session_end_succeeded(state, session_id, result=ended)
            first_job_id, first_hook_input = self._assert_pending_session_end_transport(
                state, session_id, transcript, environment,
            )
            _drain_worker(vault, environment)
            self._assert_session_end_source_was_consumed(
                state, first_job_id, first_hook_input,
            )
            self._assert_managed_workers_stopped(environment)

            daily = vault / "daily" / f"{event_date}.md"

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
                f"[[daily/{event_date}#user-"
                f"{evidence_record['id']}|Kullanıcı dayanağı; kapsam: project]]"
            )
            self.assertIn(evidence_link, daily_text)

            # Aynı kapanışın tekrarı ikinci bir kayıt üretmemeli (idempotency).
            repeated = _run_hook(vault, "session-end", payload, environment)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self._assert_session_end_succeeded(
                state, session_id, expected_generation=2, result=repeated,
            )
            replay_job_id, replay_hook_input = self._assert_pending_session_end_transport(
                state, session_id, transcript, environment,
                excluded_job_ids={first_job_id},
            )
            _drain_worker(vault, environment)
            self._assert_session_end_source_was_consumed(
                state, replay_job_id, replay_hook_input,
            )
            self._assert_managed_workers_stopped(environment)
            self.assertTrue(
                _wait_until(
                    lambda: _queue_idle(state)
                    and replay_job_id in _successful_session_ends(state)
                    and bool(_successful_session_ends(state) - first_receipts),
                    DAILY_TIMEOUT_SECONDS,
                ),
                "tekrar kapanış başarılı bir makbuz üretmedi;\n" + _debug_state(state),
            )
            self.assertEqual(
                daily.read_text(encoding="utf-8"), daily_text
            )
            self.assertEqual(
                daily.read_text(encoding="utf-8").count(CANNED_TODO), 1
            )

            # Bir SONRAKİ oturum: session-start bağlamı flush'ın yazdığını
            # gerçekten geri çağırıyor mu?
            next_session_id = str(uuid.uuid4())
            started = _run_hook(
                vault, "session-start",
                {"session_id": next_session_id, "cwd": str(vault)},
                environment,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            self._assert_session_runtime_succeeded(
                state, "session-start", next_session_id, result=started,
            )
            emitted = json.loads(started.stdout)
            context = emitted["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(
                emitted["hookSpecificOutput"]["hookEventName"], "SessionStart"
            )
            daily_match = re.search(
                r"\[Hafıza: Bugünün Logu\]\n(?P<body>.*?)(?=\n\n\[|\Z)",
                context,
                re.DOTALL,
            )
            self.assertIsNotNone(daily_match, context)
            assert daily_match is not None
            daily_context = daily_match.group("body")
            self.assertIn(CANNED_TODO, daily_context)
            self.assertIn(CANNED_CLAIM, daily_context)
            self.assertIn(evidence_link, daily_context)
            self._assert_managed_workers_stopped(environment)

    def test_read_only_turn_blocks_the_whole_write_chain(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="cevo-journey-ro-") as temporary,
            ExitStack() as workers_cleanup,
        ):
            root = Path(temporary)
            vault = root / "vault"
            codex = _copy_runtime(vault)
            _init_git(vault)
            state = codex / "scripts" / ".state"
            stub = _write_stub_codex(root)
            environment = self._environment(stub, root / "codex-home")
            workers_cleanup.callback(
                self._cleanup_managed_workers,
                Path(environment["CEVO_JOURNEY_WORKER_REGISTRY"]),
                state,
            )
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
            self._assert_session_end_succeeded(state, session_id, result=ended)

            # Salt okunur kapanış hiçbir iş kuyruklamaz: bekleme gerekmez,
            # dönüş anında ne hookin taşıyıcısı ne pending iş ne daily olmalı.
            self.assertEqual(list(state.glob("hookin-*.json")), [])
            self.assertEqual(list((state / "worker-jobs").glob("*/*.json")), [])
            self.assertFalse((state / "worker-sequence.json").exists())
            self.assertFalse((vault / "daily" / f"{datetime.date.today().isoformat()}.md").exists())


if __name__ == "__main__":
    unittest.main()
