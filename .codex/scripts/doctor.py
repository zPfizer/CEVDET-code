from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import cached_property
import json
import math
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Callable, Sequence

import compile_state
from codex_runner import find_codex
from graph_integrity import graph_notes, graph_summary
from knowledge_schema import WIKILINK, validate_knowledge_tree, wikilink_target
from process_control import pid_is_alive
from worker_supervisor import (
    STALE_HOOK_INPUT_SECONDS,
    SUPERVISOR_SCHEMA_VERSION,
    inspect_worker_queue,
    has_unverified_process_tree,
)
from tag_taxonomy import (
    TaxonomyError,
    audit_inline_tags,
    audit_vault,
    load_taxonomy,
)
from tansu_semantic_metadata import audit_manifest as audit_tansu_semantic_manifest
from vault_corpus import (
    ARCHIVE_ROOT,
    DAILY_ROOT,
    HUMAN_NOTE_ROOTS,
    KNOWLEDGE_ROOT,
    NoteIndex,
    by_key,
    by_stem,
    link_key,
    resolve_link,
    vault_notes,
)
from vault_retrieval import MAX_CACHE_BYTES, build_vault_map
from memory_ledger import MemoryPreferenceError, memory_read


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))
from hook import (  # noqa: E402
    SESSION_CONTEXT_SOFT_TARGET_CHARS,
    SESSION_CONTEXT_TARGET_CHARS,
    SESSION_SECTION_TARGET_CHARS,
    build_session_context,
)
MACHINE_CHECKPOINT_SCOPES = ("daily/", "knowledge/")


HOOK_RUNTIME_MAX_AGE_SECONDS = 2 * 60 * 60
SESSION_START_PROMPT_GRACE_SECONDS = 5 * 60
FLUSH_INFLIGHT_MAX_AGE_SECONDS = 5 * 60
SESSION_START_REQUIRED_SECTIONS = {
    *(f"Hafıza: {title}" for title in SESSION_SECTION_TARGET_CHARS),
    "Hafıza Protokolü",
}
REQUIRED_NOTE_FIELDS = ("title", "created", "updated", "tags")
# Roots whose wikilinks are validated; daily joined in tur 2 (spec 3f).
LINK_ROOTS = frozenset({*HUMAN_NOTE_ROOTS, KNOWLEDGE_ROOT, DAILY_ROOT})
ALLOWED_ROOT_FILES = {
    ".beyin-version",
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CONTEXT.md",
    "Hoş geldiniz.md",
    "README.md",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    evidence: str


@dataclass(frozen=True)
class Context:
    """One doctor run's inputs.

    ``run_checks`` fills every field; a test builds only the fields the check it
    exercises reads. Every check takes this and nothing else.
    """

    vault: Path = Path()
    project_root: Path = Path()
    state_dir: Path = Path()
    now: float = 0.0

    @cached_property
    def notes(self) -> tuple[NoteIndex, ...]:
        """The vault read once; every vault check filters this same index."""
        return vault_notes(self.vault)


def _finite_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        timestamp = float(value)
    except (OverflowError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _receipt_error(value: object) -> str:
    return (
        "runtime-error-recorded"
        if isinstance(value, str) and value
        else "error-class-invalid"
    )


_HASH64 = re.compile(r"[0-9a-f]{64}\Z")


def _exists(vault: Path, paths: list[str]) -> tuple[bool, str]:
    missing = [relative for relative in paths if not (vault / relative).exists()]
    return not missing, "tamam" if not missing else "eksik: " + ", ".join(missing)


def _version_check(ctx: Context) -> Check:
    version = ctx.vault / ".beyin-version"
    return Check(
        "Sürüm",
        "OK" if version.is_file() else "FAIL",
        version.read_text(encoding="utf-8").strip() if version.is_file() else "dosya yok",
    )


def _agents_file_check(ctx: Context) -> Check:
    return Check(
        "AGENTS.md",
        "OK" if (ctx.vault / "AGENTS.md").is_file() else "FAIL",
        "yönlendirici",
    )


def _codex_hooks_check(ctx: Context) -> Check:
    try:
        hooks = json.loads(
            (ctx.vault / ".codex" / "hooks.json").read_text(encoding="utf-8")
        )
        events = set(hooks.get("hooks", {}))
    except (OSError, json.JSONDecodeError):
        events = set()
    required_events = {
        "SessionStart",
        "UserPromptSubmit",
        "PreCompact",
        "SessionEnd",
        "Stop",
    }
    return Check(
        "Codex kancaları",
        "OK" if events == required_events else "FAIL",
        ", ".join(sorted(events)) if events else "okunamadı",
    )


def _python_check(ctx: Context) -> Check:
    return Check("Python", "OK", sys.version.split()[0])


def _codex_cli_check(ctx: Context) -> Check:
    try:
        return Check("Codex CLI", "OK", find_codex())
    except FileNotFoundError as exc:
        # The reason travels as evidence: a configured path that does not
        # resolve must not read as "not on PATH".
        return Check("Codex CLI", "FAIL", str(exc) or "codex-cli-missing")


def _companion_memory_check(ctx: Context) -> Check:
    ok, detail = _exists(
        ctx.vault,
        [
            "🔮 850-Companion/Core.md",
            "🔮 850-Companion/Kurallar.md",
        ],
    )
    if ok:
        try:
            with memory_read(ctx.vault) as memory:
                for name in ("Last-Session.md", "Threads.md", "Journal.md"):
                    memory.read_source(ctx.vault / "🔮 850-Companion" / name)
        except (OSError, ValueError) as exc:
            return Check("Hafıza", "FAIL", f"Companion kaynağı okunamadı: {type(exc).__name__}")
    return Check("Hafıza", "OK" if ok else "FAIL", detail)


def _machine_layer_check(ctx: Context) -> Check:
    ok, detail = _exists(
        ctx.vault,
        ["daily", "knowledge/index.md", "knowledge/concepts", "knowledge/connections"],
    )
    return Check("Makine katmanı", "OK" if ok else "FAIL", detail)


def _git_branch_check(ctx: Context) -> Check:
    try:
        repository = subprocess.run(
            ["git", "-C", str(ctx.vault), "rev-parse", "--is-inside-work-tree"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        return Check("Git", "FAIL", "git yok")
    except (OSError, subprocess.SubprocessError):
        return Check("Git", "FAIL", "repo yok")
    if repository.returncode != 0 or repository.stdout.strip().lower() != "true":
        return Check("Git", "FAIL", "repo yok")
    try:
        branch = subprocess.run(
            ["git", "-C", str(ctx.vault), "branch", "--show-current"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return Check("Git", "FAIL", "branch okunamadı")
    branch_name = branch.stdout.strip()
    if branch.returncode != 0:
        return Check("Git", "FAIL", "branch okunamadı")
    if branch_name:
        return Check("Git", "OK", branch_name)
    return Check("Git", "WARN", "detached HEAD")


def _project_root_check(ctx: Context) -> Check:
    expected = ctx.vault.resolve()
    observed = ctx.project_root.resolve()
    if observed == expected:
        return Check("Codex proje kökü", "OK", str(observed))
    return Check(
        "Codex proje kökü",
        "FAIL",
        f"beklenen: {expected}; mevcut: {observed}",
    )


def _scoped_hook_runtime(ctx: Context) -> Check | None:
    start_paths = sorted(ctx.state_dir.glob("runtime-session-start-*.json"))
    if not start_paths:
        return None
    expected_cwd = ctx.project_root.resolve()
    starts: list[dict[str, object]] = []
    prompts: dict[str, list[dict[str, object]]] = {}
    for path in start_paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Check("Hook çalışma zamanı", "FAIL", f"receipt okunamadı: {path.name}")
        if isinstance(value, dict):
            starts.append(value)
    for path in sorted(ctx.state_dir.glob("runtime-user-prompt-*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Check("Hook çalışma zamanı", "FAIL", f"receipt okunamadı: {path.name}")
        if isinstance(value, dict) and isinstance(value.get("session_key"), str):
            prompts.setdefault(value["session_key"], []).append(value)
    fresh_starts: list[dict[str, object]] = []
    for receipt in starts:
        try:
            timestamp = _finite_timestamp(receipt.get("ts"))
            if timestamp is None:
                continue
            observed_cwd = Path(str(receipt["cwd"])).resolve()
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if (
            observed_cwd == expected_cwd
            and ctx.now - timestamp <= HOOK_RUNTIME_MAX_AGE_SECONDS
        ):
            fresh_starts.append(receipt)
    if not fresh_starts:
        return Check("Hook çalışma zamanı", "WARN", "fresh scoped SessionStart receipt yok")
    latest = max(
        fresh_starts,
        key=lambda item: _finite_timestamp(item.get("ts")) or 0,
    )
    key = latest.get("session_key")
    event_generation = latest.get("event_generation")
    context_chars = latest.get("context_chars")
    sections = latest.get("sections")
    if (
        not isinstance(key, str)
        or not key
        or not isinstance(event_generation, int)
        or event_generation < 1
    ):
        return Check("Hook çalışma zamanı", "UNSTABLE_SNAPSHOT", "receipt kimliği eksik")
    if (
        latest.get("outcome") != "emitted"
        or not isinstance(context_chars, int)
        or not isinstance(sections, list)
        or not all(isinstance(section, str) for section in sections)
    ):
        return Check("Hook çalışma zamanı", "FAIL", "SessionStart contract invalid")
    missing_sections = sorted(SESSION_START_REQUIRED_SECTIONS - set(sections))
    if missing_sections:
        return Check(
            "Hook çalışma zamanı",
            "WARN",
            "SessionStart-contract eksik: " + ", ".join(missing_sections),
        )
    coherent_prompts: list[dict[str, object]] = []
    for item in prompts.get(key, []):
        timestamp = _finite_timestamp(item.get("ts"))
        if timestamp is not None and ctx.now - timestamp <= HOOK_RUNTIME_MAX_AGE_SECONDS:
            coherent_prompts.append(item)
    latest_timestamp = _finite_timestamp(latest.get("ts"))
    if latest_timestamp is None:
        return Check("Hook çalışma zamanı", "WARN", "fresh scoped SessionStart receipt yok")
    if not coherent_prompts:
        age = max(0, int(ctx.now - latest_timestamp))
        return Check(
            "Hook çalışma zamanı",
            "WARN",
            (
                "fresh SessionStart aynı-session UserPrompt bekliyor"
                if age <= SESSION_START_PROMPT_GRACE_SECONDS
                else "fresh SessionStart var; aynı session UserPrompt receipt yok"
            ),
        )
    age = max(0, int(ctx.now - latest_timestamp))
    return Check(
        "Hook çalışma zamanı",
        "OK",
        f"aynı oturum hafıza zinciri; context {context_chars} karakter / "
        f"{len(sections)} bölüm; başlangıç kanıtı {age} sn",
    )


def _hook_interpreter_check(ctx: Context) -> Check:
    """Resolve each Windows hook interpreter from its configured command."""
    try:
        hooks = json.loads(
            (ctx.vault / ".codex" / "hooks.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return Check("Hook yorumlayıcısı", "FAIL", "hooks.json okunamadı")
    interpreters: dict[str, Path] = {}
    for entries in hooks.get("hooks", {}).values():
        for entry in entries if isinstance(entries, list) else []:
            for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                command = hook.get("commandWindows", "") if isinstance(hook, dict) else ""
                if not isinstance(command, str) or not command:
                    continue
                try:
                    tokens = shlex.split(command, posix=False)
                except ValueError:
                    return Check("Hook yorumlayıcısı", "FAIL", "commandWindows çözümlenemedi")
                if not tokens:
                    continue
                candidate = tokens[0].strip('"')
                resolved = shutil.which(candidate)
                if resolved is None and Path(candidate).is_file():
                    resolved = candidate
                if resolved is None:
                    return Check("Hook yorumlayıcısı", "FAIL", f"bulunamadı: {candidate[:80]}")
                try:
                    interpreters[candidate] = Path(resolved).resolve()
                except OSError:
                    return Check("Hook yorumlayıcısı", "FAIL", f"çözümlenemedi: {candidate[:80]}")
    if not interpreters:
        return Check("Hook yorumlayıcısı", "FAIL", "commandWindows yorumlayıcısı yok")
    running = Path(sys.executable).resolve()
    mismatched = sorted(
        candidate
        for candidate, path in interpreters.items()
        if path != running
    )
    if mismatched:
        return Check(
            "Hook yorumlayıcısı",
            "WARN",
            f"çalışan yorumlayıcıdan farklı: {mismatched[0][:80]}",
        )
    return Check("Hook yorumlayıcısı", "OK", f"{len(interpreters)} PATH yorumlayıcısı doğrulandı")


def _hook_runtime_check(ctx: Context) -> Check:
    state_dir, project_root, now = ctx.state_dir, ctx.project_root, ctx.now
    scoped = _scoped_hook_runtime(ctx)
    if scoped is not None:
        return scoped
    required = {
        "SessionStart": state_dir / "runtime-session-start.json",
        "UserPromptSubmit": state_dir / "runtime-user-prompt.json",
    }
    missing_or_stale: list[str] = []
    ages: list[int] = []
    session_context_detail = ""
    expected_cwd = project_root.resolve()
    for event, path in required.items():
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise TypeError
            timestamp = _finite_timestamp(receipt.get("ts"))
            if timestamp is None:
                raise ValueError
            observed_cwd = Path(receipt["cwd"]).resolve()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            missing_or_stale.append(event)
            continue
        age = max(0, int(now - timestamp))
        if age > HOOK_RUNTIME_MAX_AGE_SECONDS or observed_cwd != expected_cwd:
            missing_or_stale.append(event)
        else:
            ages.append(age)
            if event == "SessionStart":
                context_chars = receipt.get("context_chars")
                sections = receipt.get("sections")
                if (
                    receipt.get("outcome") == "emitted"
                    and isinstance(context_chars, int)
                    and context_chars >= 0
                    and isinstance(sections, list)
                    and all(isinstance(section, str) for section in sections)
                ):
                    section_values = [
                        section for section in sections if isinstance(section, str)
                    ]
                    missing_sections = sorted(
                        SESSION_START_REQUIRED_SECTIONS - set(section_values)
                    )
                    if missing_sections:
                        missing_or_stale.append("SessionStart-contract")
                        continue
                    session_context_detail = (
                        f"; context {context_chars} karakter / {len(section_values)} bölüm"
                    )
                else:
                    missing_or_stale.append("SessionStart-contract")
    if missing_or_stale:
        return Check(
            "Hook çalışma zamanı",
            "WARN",
            "eksik veya güncel değil: " + ", ".join(missing_or_stale),
        )
    return Check(
        "Hook çalışma zamanı",
        "OK",
        "SessionStart + UserPromptSubmit gözlendi"
        f"{session_context_detail}; en eski kanıt {max(ages)} sn",
    )


def _hook_health_check(ctx: Context) -> Check | list[Check]:
    state_dir = ctx.state_dir
    scoped = sorted(state_dir.glob("hook-health-*.json"))
    if scoped:
        failures: list[str] = []
        historical_failures: list[str] = []
        generations = 0
        for path in scoped:
            try:
                health = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return Check(
                    "Hook sağlığı",
                    "FAIL",
                    f"health kaydı okunamadı: {path.name}",
                )
            if not isinstance(health, dict):
                return Check("Hook sağlığı", "FAIL", f"health object değil: {path.name}")
            expected_session_key = path.stem.removeprefix("hook-health-")
            session_key = health.get("session_key")
            if (
                type(health.get("schema_version")) is not int
                or health["schema_version"] != 2
                or health.get("component") != "hook"
                or not isinstance(session_key, str)
                or (
                    session_key != "global"
                    and _HASH64.fullmatch(session_key) is None
                )
                or session_key != expected_session_key
            ):
                return Check("Hook sağlığı", "FAIL", f"health receipt alanları geçersiz: {path.name}")
            generation = health.get("generation")
            if type(generation) is not int or generation < 1:
                return Check("Hook sağlığı", "FAIL", f"generation eksik: {path.name}")
            if type(health.get("ts")) is not int or _finite_timestamp(health.get("ts")) is None:
                return Check("Hook sağlığı", "FAIL", f"timestamp geçersiz: {path.name}")
            generations += generation
            status = health.get("status")
            if not isinstance(status, str) or status not in {"ok", "error"}:
                return Check("Hook sağlığı", "FAIL", f"status geçersiz: {path.name}")
            if status == "error":
                error = health.get("error")
                if not isinstance(error, str) or not error:
                    return Check("Hook sağlığı", "FAIL", f"hata sınıfı eksik: {path.name}")
                error_text = _receipt_error(error)
                timestamp = _finite_timestamp(health.get("ts"))
                if timestamp is None:
                    failures.append(f"{error_text} (invalid timestamp)")
                elif ctx.now - timestamp > HOOK_RUNTIME_MAX_AGE_SECONDS:
                    historical_failures.append(error_text)
                else:
                    failures.append(error_text)
        current = Check(
            'Hook sağlığı', 'FAIL' if failures else 'OK',
            f'{len(failures)} session failure; ilk: {failures[0]}' if failures else
            f'güncel hata yok; {len(scoped)} session receipt; generation toplamı {generations}',
        )
        if historical_failures:
            return [current, Check(
                "Geçmiş oturum hataları",
                "WARN",
                (
                    f"historical session failure={len(historical_failures)}; "
                    f"ilk: {historical_failures[0]}"
                ),
            )]
        return current
    health_path = state_dir / "hook-health.json"
    if not health_path.exists():
        return Check("Hook sağlığı", "OK", "temiz")
    try:
        health = json.loads(health_path.read_text(encoding="utf-8"))
        if not isinstance(health, dict):
            return Check("Hook sağlığı", "FAIL", "health object değil")
        status = health.get("status")
        error = health.get("error")
    except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError):
        return Check("Hook sağlığı", "FAIL", "health kaydı okunamadı")
    if status is not None and (
        not isinstance(status, str) or status not in {"ok", "error"}
    ):
        return Check("Hook sağlığı", "FAIL", "status geçersiz")
    if status in {"ok", "error"}:
        session_key = health.get("session_key")
        generation = health.get("generation")
        if (
            type(health.get("schema_version")) is not int
            or health["schema_version"] != 2
            or health.get("component") != "hook"
            or not isinstance(session_key, str)
            or (
                session_key != "global"
                and _HASH64.fullmatch(session_key) is None
            )
            or type(generation) is not int
            or generation < 1
            or type(health.get("ts")) is not int
            or _finite_timestamp(health.get("ts")) is None
        ):
            return Check("Hook sağlığı", "FAIL", "health receipt alanları geçersiz")
    if status == "ok":
        return Check("Hook sağlığı", "OK", "temiz")
    if not isinstance(error, str) or not error:
        return Check("Hook sağlığı", "FAIL", "hata sınıfı eksik")
    return Check("Hook sağlığı", "FAIL", _receipt_error(error))


def _brain_health_check(ctx: Context) -> Check:
    health_path = ctx.state_dir / "health.json"
    if not health_path.exists():
        return Check("Beyin sağlığı", "OK", "temiz")
    try:
        health = json.loads(health_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
        return Check("Beyin sağlığı", "FAIL", "health kaydı okunamadı")
    if not isinstance(health, dict):
        return Check("Beyin sağlığı", "FAIL", "health object değil")
    components = health.get("components")
    if health.get("schema_version") == 2:
        if not isinstance(components, dict):
            return Check("Beyin sağlığı", "FAIL", "components object değil")
        if any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("status"), str)
            or entry["status"] not in {"error", "warning"}
            for entry in components.values()
        ):
            return Check("Beyin sağlığı", "FAIL", "component status geçersiz")
        errors = [
            entry
            for entry in components.values()
            if entry.get("status") == "error"
        ]
        warnings = [
            entry
            for entry in components.values()
            if entry.get("status") == "warning"
        ]
        if errors:
            first = errors[0]
            return Check(
                "Beyin sağlığı",
                "FAIL",
                f"{first.get('component', '')}: {first.get('error', '')}",
            )
        if warnings:
            first = warnings[0]
            return Check(
                "Beyin sağlığı",
                "WARN",
                f"{first.get('component', '')}: {first.get('error', '')}",
            )
        return Check(
            "Beyin sağlığı",
            "OK",
            f"{len(components)} scoped generation temiz",
        )
    component = health.get("component")
    error = health.get("error")
    if not isinstance(component, str) or not component:
        return Check("Beyin sağlığı", "FAIL", "bileşen eksik")
    if not isinstance(error, str) or not error:
        return Check("Beyin sağlığı", "FAIL", "hata ayrıntısı eksik")
    status = "WARN" if error.startswith("warn:") else "FAIL"
    return Check("Beyin sağlığı", status, f"{component}: {error}")


def _state_privacy_check(ctx: Context) -> Check:
    forbidden = {
        "content",
        "excerpt",
        "message",
        "messages",
        "prompt",
        "session_id",
        "thread_id",
        "transcript",
    }
    violations: list[str] = []

    def inspect(value: object, path: str) -> None:
        if isinstance(value, dict):
            if path.endswith((".body_terms", ".document_frequency")) and all(
                isinstance(term, str)
                and isinstance(count, int)
                and count >= 0
                for term, count in value.items()
            ):
                return
            for key, item in value.items():
                if key.lower() in forbidden:
                    violations.append(f"{path}:{key}")
                inspect(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                inspect(item, f"{path}[{index}]")

    for path in sorted(ctx.state_dir.glob("*.json")):
        try:
            modified = path.stat().st_mtime
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return Check(
                "State mahremiyeti",
                "FAIL",
                f"okunamadı: {path.name} ({exc.__class__.__name__})",
            )
        if (re.fullmatch(r'hookin-[0-9a-f]{32}\.json', path.name)
            and isinstance(payload, dict)
            and set(payload) == {'session_id', 'transcript_path'}
            and isinstance(payload.get('session_id'), str)
            and isinstance(payload.get('transcript_path'), str)
            and 0 <= ctx.now - modified <= STALE_HOOK_INPUT_SECONDS):
            # Bounded worker transport, removed after processing; no user text.
            continue
        inspect(payload, path.name)
    if violations:
        return Check(
            "State mahremiyeti",
            "FAIL",
            f"{len(violations)} yasak alan: {', '.join(violations)}",
        )
    return Check("State mahremiyeti", "OK", "kalıcı durum metinsiz; geçici taşıma girdileri sınırlı")


def _flush_inflight_check(ctx: Context) -> Check:
    now = ctx.now
    active = 0
    stale: list[tuple[str, int]] = []
    invalid: list[str] = []
    valid_statuses = {"inflight", "prepared", "ok", "fail"}
    for path in sorted(ctx.state_dir.glob("flush-*.json")):
        if path.name.startswith(("flush-coverage-", "flush-batch-", "flush-index-")):
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
            invalid.append(path.name)
            continue
        if not isinstance(receipt, dict):
            invalid.append(path.name)
            continue
        status = receipt.get("status")
        if not isinstance(status, str) or status not in valid_statuses:
            invalid.append(path.name)
            continue
        timestamp = _finite_timestamp(receipt.get("ts"))
        session_key = receipt.get("session_key")
        expected_session_key = path.stem.removeprefix("flush-")
        generation = receipt.get("generation")
        receipts = receipt.get("receipts")
        if (
            type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != 2
            or not isinstance(session_key, str)
            or _HASH64.fullmatch(session_key) is None
            or session_key != expected_session_key
            or type(receipt.get("ts")) is not int
            or timestamp is None
            or type(generation) is not int
            or generation < 1
            or not isinstance(receipts, dict)
        ):
            invalid.append(path.name)
            continue
        entries_invalid = False
        for key, item in receipts.items():
            if (
                not isinstance(key, str)
                or _HASH64.fullmatch(key) is None
                or not isinstance(item, dict)
                or item.get("idempotency_key") != key
                or item.get("status") not in {"prepared", "ok"}
                or type(item.get("ts")) is not int
                or _finite_timestamp(item.get("ts")) is None
                or type(item.get("generation")) is not int
                or item["generation"] < 1
                or any(
                    not isinstance(item.get(field), str)
                    or _HASH64.fullmatch(item[field]) is None
                    for field in ("transcript_digest", "summary_digest")
                )
            ):
                entries_invalid = True
                break
        if entries_invalid:
            invalid.append(path.name)
            continue
        if status != "inflight":
            continue
        age = max(0, int(now - timestamp))
        if age > FLUSH_INFLIGHT_MAX_AGE_SECONDS:
            stale.append((path.name, age))
        else:
            active += 1
    if stale:
        name, age = stale[0]
        return Check(
            "Flush devamlılığı",
            "FAIL",
            f"{len(stale)} yarım flush; ilk: {name} ({age} sn)",
        )
    if invalid:
        return Check(
            "Flush devamlılığı",
            "FAIL",
            f"{len(invalid)} bozuk receipt; ilk: {invalid[0]}",
        )
    return Check(
        "Flush devamlılığı",
        "OK",
        f"yarım flush yok; aktif {active}",
    )


def _derived_state_gitignore_check(ctx: Context) -> Check:
    vault = ctx.vault
    state_root = ".codex/scripts/.state"
    expected_tracked = {f"{state_root}/.gitkeep"}
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--", state_root],
            cwd=vault,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return Check("Derived-state gitignore", "FAIL", "git-ls-files-failed")
    if tracked.returncode != 0:
        return Check("Derived-state gitignore", "FAIL", "git-ls-files-failed")
    tracked_paths = {
        line.strip().replace("\\", "/")
        for line in tracked.stdout.splitlines()
        if line.strip()
    }
    if tracked_paths != expected_tracked:
        return Check(
            "Derived-state gitignore",
            "FAIL",
            f"tracked-drift={len(tracked_paths)}",
        )

    probes = (
        (f"{state_root}/session-probe.json", True),
        (f"{state_root}/worker-jobs/pending/job-probe.json", True),
        (f"{state_root}/.gitkeep", False),
    )
    for relative, should_ignore in probes:
        try:
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", "--", relative],
                cwd=vault,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return Check("Derived-state gitignore", "FAIL", "git-check-ignore-failed")
        if result.returncode not in {0, 1} or (result.returncode == 0) != should_ignore:
            return Check(
                "Derived-state gitignore",
                "FAIL",
                f"ignore-drift:{relative}",
            )
    return Check(
        "Derived-state gitignore",
        "OK",
        "runtime ignored; yalnız .gitkeep tracked",
    )


def _worker_queue_check(ctx: Context) -> Check | list[Check]:
    try:
        for state in (ctx.state_dir, ctx.state_dir / 'maintenance'):
            if has_unverified_process_tree(state):
                return Check('Worker kuyruğu', 'FAIL', f'cleanup-unverified:{state.name}; otomatik işler durdu')
    except OSError:
        return Check('Worker kuyruğu', 'FAIL', 'cleanup fence okunamadı')
    now = ctx.now
    root = ctx.state_dir / "worker-jobs"
    counts = {
        name: len(list((root / name).glob("*.json")))
        for name in (
            "pending",
            "claimed",
            "running",
            "succeeded",
            "failed",
            "dead-letter",
        )
    }
    stale_running = 0
    invalid_timestamps = 0
    unrecoverable_dead = 0
    recovered_dead = 0
    retry_exhausted_dead = 0
    unknown_dead = 0
    for path in (root / "running").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return Check("Worker kuyruğu", "FAIL", f"okunamadı: {path.name}")
        if not isinstance(job, dict):
            return Check("Worker kuyruğu", "FAIL", f"object değil: {path.name}")
        lease_until = _finite_timestamp(job.get("lease_until", 0))
        owner_pid = job.get("owner_pid", 0)
        if lease_until is None:
            invalid_timestamps += 1
        elif (
            lease_until <= now
            and (not isinstance(owner_pid, int) or not pid_is_alive(owner_pid))
        ):
            stale_running += 1
    for path in (root / "dead-letter").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return Check("Worker kuyruğu", "FAIL", f"okunamadı: {path.name}")
        if not isinstance(job, dict):
            return Check("Worker kuyruğu", "FAIL", f"object değil: {path.name}")
        if (
            job.get("terminal_reason") == "unrecoverable-input"
            and job.get("retryable") is False
        ):
            unrecoverable_dead += 1
        elif (
            job.get("terminal_reason") == "retry-exhausted"
            and job.get("retryable") is False
        ):
            retry_exhausted_dead += 1
        elif (
            job.get("terminal_reason") == "recovered-by-successor"
            and job.get("retryable") is False
        ):
            recovery_job_id = job.get("recovery_job_id")
            successor_path = (
                root / "succeeded" / f"job-{recovery_job_id}.json"
                if isinstance(recovery_job_id, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", recovery_job_id)
                else None
            )
            try:
                successor = (
                    json.loads(successor_path.read_text(encoding="utf-8"))
                    if successor_path is not None
                    else None
                )
            except (OSError, json.JSONDecodeError):
                successor = None
            if (
                isinstance(successor, dict)
                and successor.get("status") == "succeeded"
                and successor.get("job_id") == recovery_job_id
                and successor.get("kind") == job.get("kind")
                and successor.get("payload") == job.get("payload")
            ):
                recovered_dead += 1
            else:
                unknown_dead += 1
        else:
            unknown_dead += 1
    try:
        inspection = inspect_worker_queue(ctx.state_dir)
    except (OSError, UnicodeError, ValueError):
        return Check("Worker kuyruğu", "FAIL", "worker kayıtları okunamadı")
    inspection_counts = inspection.get("counts") if isinstance(inspection, dict) else None
    if not isinstance(inspection_counts, dict):
        return Check("Worker kuyruğu", "FAIL", "worker kayıt özeti geçersiz")
    if inspection.get("status") == "error":
        invalid = inspection.get("invalid", 0)
        quarantined = inspection_counts.get("quarantined", 0)
        return Check(
            "Worker kuyruğu",
            "FAIL",
            f"worker kaydı geçersiz; invalid={invalid}; quarantined={quarantined}",
        )
    evidence = ", ".join(f"{name}={count}" for name, count in counts.items() if name != 'dead-letter')
    evidence += f"; stale-running={stale_running}"
    evidence += f"; invalid-timestamp={invalid_timestamps}"
    evidence += f"; retry-exhausted={retry_exhausted_dead}"
    evidence += f"; unclassified-terminal={unknown_dead}"
    if stale_running or counts["failed"] or invalid_timestamps or retry_exhausted_dead or unknown_dead:
        status = 'FAIL'
    elif counts['pending'] or counts['claimed'] or counts['running']:
        status = 'WARN'
    else:
        status = 'OK'
    current = Check('Worker kuyruğu', status, evidence)
    if unrecoverable_dead or recovered_dead:
        return [current, Check(
            'Geçmiş iş sonuçları', 'WARN' if unrecoverable_dead else 'OK',
            f'unrecoverable-input={unrecoverable_dead}; recovered-by-successor={recovered_dead}',
        )]
    return current


def _worker_delayed_job_check(ctx: Context) -> Check:
    state_dir, now = ctx.state_dir, ctx.now
    ready_pending = 0
    for path in (state_dir / "worker-jobs" / "pending").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return Check("Worker gecikmiş iş", "FAIL", "pending okunamadı")
        if not isinstance(job, dict):
            return Check("Worker gecikmiş iş", "FAIL", "pending object değil")
        next_attempt = _finite_timestamp(job.get("next_attempt_ts", 0))
        if next_attempt is None:
            return Check("Worker gecikmiş iş", "FAIL", "next-attempt invalid")
        if next_attempt <= now:
            ready_pending += 1

    receipt_path = state_dir / "worker-supervisor.json"
    if not receipt_path.exists():
        supervisor = "missing"
    else:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return Check("Worker gecikmiş iş", "FAIL", "supervisor receipt okunamadı")
        if not isinstance(receipt, dict):
            return Check("Worker gecikmiş iş", "FAIL", "supervisor object değil")
        status = receipt.get("status")
        if not isinstance(status, str) or status not in {
            "launching",
            "running",
            "idle",
            "failed",
        }:
            return Check("Worker gecikmiş iş", "FAIL", "supervisor status geçersiz")
        if (
            type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != SUPERVISOR_SCHEMA_VERSION
            or isinstance(receipt.get("generation"), bool)
            or not isinstance(receipt.get("generation"), int)
            or receipt["generation"] < 1
            or not isinstance(receipt.get("launch_token"), str)
            or isinstance(receipt.get("owner_pid"), bool)
            or not isinstance(receipt.get("owner_pid"), int)
            or receipt["owner_pid"] < 0
            or _finite_timestamp(receipt.get("lease_until")) is None
            or _finite_timestamp(receipt.get("updated_ts")) is None
        ):
            return Check("Worker gecikmiş iş", "FAIL", "supervisor receipt alanları geçersiz")
        supervisor = status
    evidence = f"ready-pending={ready_pending}; supervisor={supervisor}"
    if ready_pending and supervisor == "idle":
        return Check("Worker gecikmiş iş", "WARN", evidence)
    if ready_pending and supervisor == "missing":
        return Check("Worker gecikmiş iş", "WARN", evidence)
    if ready_pending and supervisor == "failed":
        return Check("Worker gecikmiş iş", "FAIL", evidence)
    return Check("Worker gecikmiş iş", "OK", evidence)


def _state_retention_check(ctx: Context) -> Check:
    state_dir, now = ctx.state_dir, ctx.now
    files = [path for path in state_dir.rglob("*") if path.is_file()]
    total_bytes = 0
    oldest_age = 0
    for path in files:
        try:
            file_stat = path.stat()
        except OSError:
            continue
        total_bytes += file_stat.st_size
        oldest_age = max(oldest_age, max(0, int(now - file_stat.st_mtime)))
    registry = len(list(state_dir.glob("session-*.json")))
    runtime = len(list(state_dir.glob("runtime-*.json")))
    locks = len(list(state_dir.rglob("*.lock")))
    cache_path = state_dir / "vault-retrieval-cache.json"
    try:
        cache_bytes = cache_path.stat().st_size
    except OSError:
        cache_bytes = 0
    evidence = (
        f"files={len(files)}; bytes={total_bytes}; cache={cache_bytes}/{MAX_CACHE_BYTES}; "
        f"registry={registry}; runtime={runtime}; locks={locks}; "
        f"oldest_age={oldest_age}s"
    )
    if cache_bytes > MAX_CACHE_BYTES:
        return Check("State retention", "FAIL", evidence)
    if (
        total_bytes > 32 * 1024 * 1024
        or registry > 256
        or runtime > 512
        or locks > 512
        or oldest_age > 30 * 24 * 60 * 60
    ):
        return Check("State retention", "WARN", evidence)
    return Check("State retention", "OK", evidence)


def _compiler_queue_check(ctx: Context) -> Check:
    try:
        state = compile_state.load(ctx.state_dir)
        changed = compile_state.changed_dailies(ctx.vault, state)
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        compile_state.PolicyError,
    ) as exc:
        return Check(
            "Derleyici kuyruğu",
            "FAIL",
            f"ölçülemedi: {exc.__class__.__name__}",
        )
    if not changed:
        return Check("Derleyici kuyruğu", "OK", "bekleyen günlük yok")
    names = ", ".join(path.name for path, _digest in changed)
    return Check(
        "Derleyici kuyruğu",
        "WARN",
        f"{len(changed)} günlük bekliyor: {names}",
    )


def _local_checkpoint_check(ctx: Context) -> Check:
    vault = ctx.vault
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(vault),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *(scope.rstrip("/") for scope in MACHINE_CHECKPOINT_SCOPES),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return Check("Yerel checkpoint", "FAIL", "Git durumu okunamadı")
    if result.returncode != 0:
        return Check("Yerel checkpoint", "FAIL", "Git durumu okunamadı")
    changes = [line for line in result.stdout.splitlines() if line.strip()]
    if not changes:
        return Check("Yerel checkpoint", "OK", "daily/knowledge committed")
    return Check(
        "Yerel checkpoint",
        "WARN",
        f"{len(changes)} machine-managed fark bekliyor",
    )


def _git_hygiene_check(ctx: Context) -> Check:
    try:
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(ctx.vault),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=False,
            text=False,
            timeout=10,
        )
    except FileNotFoundError:
        return Check("Git hijyeni", "FAIL", "git yok")
    except subprocess.TimeoutExpired:
        return Check("Git hijyeni", "FAIL", "Git durumu zaman aşımına uğradı")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return Check("Git hijyeni", "FAIL", "Git durumu okunamadı")

    try:
        returncode = result.returncode
        output = result.stdout
    except AttributeError:
        return Check("Git hijyeni", "FAIL", "Git durumu çıktısı bozuk")
    if returncode != 0:
        return Check("Git hijyeni", "FAIL", "Git durumu okunamadı")
    if (
        not isinstance(output, bytes)
        or output == b"\0"
        or (output and not output.endswith(b"\0"))
    ):
        return Check("Git hijyeni", "FAIL", "Git durumu çıktısı bozuk")

    fields = output.split(b"\0")[:-1] if output else []
    entries: list[tuple[bytes, ...]] = []
    index = 0
    valid_status = b" MADRCUT?"
    while index < len(fields):
        field = fields[index]
        status = field[:2]
        if (
            len(field) < 4
            or field[2:3] != b" "
            or any(character not in valid_status for character in status)
            or status == b"  "
            or (b"?" in status and status != b"??")
            or not field[3:]
        ):
            return Check("Git hijyeni", "FAIL", "Git durumu çıktısı bozuk")
        paths = [field[3:]]
        if field[:1] in (b"R", b"C") or field[1:2] in (b"R", b"C"):
            index += 1
            if index >= len(fields) or not fields[index]:
                return Check("Git hijyeni", "FAIL", "Git durumu çıktısı bozuk")
            paths.append(fields[index])
        entries.append(tuple(paths))
        index += 1

    scratch = sum(
        any(path == b".scratch" or path.startswith(b".scratch/") for path in paths)
        for paths in entries
    )
    other = len(entries) - scratch
    if not entries:
        return Check("Git hijyeni", "OK", "temiz (clean)")

    examples: list[str] = []
    for paths in entries[:3]:
        decoded = paths[0].decode("utf-8", errors="backslashreplace")
        safe = "".join(
            char if char.isprintable() and char != "|" else f"\\x{ord(char):02x}"
            for char in decoded[:80]
        )
        if len(decoded) > 80:
            safe += "…"
        examples.append(safe)
    return Check(
        "Git hijyeni",
        "WARN",
        f"{len(entries)} dirty; .scratch={scratch}; other={other}; "
        f"örnekler: {', '.join(examples)}",
    )


def _thread_workload_check(ctx: Context) -> Check:
    path = ctx.vault / "🔮 850-Companion" / "Threads.md"
    try:
        with memory_read(ctx.vault) as memory:
            _relative, text = memory.read_source(path)
    except (OSError, ValueError):
        return Check("İş yükü", "FAIL", "Threads.md okunamadı")
    if text is None:
        return Check("İş yükü", "OK", "Threads.md unutma tercihiyle kapsam dışında")
    headings = (
        "## Active Execution",
        "## Waiting / Parked",
        "## Closed Threads",
    )
    if any(text.count(heading) != 1 for heading in headings):
        return Check("İş yükü", "FAIL", "bounded thread taxonomy eksik veya duplicate")
    positions = [text.index(heading) for heading in headings]
    if positions != sorted(positions):
        return Check("İş yükü", "FAIL", "thread taxonomy sırası bozuk")

    def section(heading: str) -> str:
        match = re.search(r'^' + re.escape(heading) + r'\s*\n(.*?)(?=^## |\Z)', text, re.MULTILINE | re.DOTALL)
        return match.group(1) if match else ''

    active_text = section(headings[0])
    always_text = section('## Always-on Systems')
    waiting_text = section(headings[1])
    closed_text = section(headings[2])

    def names(section: str) -> list[str]:
        return re.findall(r"^### Thread:\s*(.+)$", section, re.MULTILINE)

    active = names(active_text)
    always = names(always_text)
    waiting = names(waiting_text)
    closed = names(closed_text)
    open_names = active + always + waiting
    duplicates = sorted(
        name
        for name in set(open_names + closed)
        if (open_names + closed).count(name) > 1
    )
    evidence = (
        f"{len(active)} active / {len(always)} always-on / "
        f"{len(waiting)} waiting"
    )
    failures: list[str] = []
    if len(active) > 3:
        failures.append(f"{len(active)} active > 3")
    if duplicates:
        failures.append("duplicate: " + ", ".join(duplicates))
    open_statuses = re.findall(
        r"^\*\*Status:\*\*",
        active_text + always_text + waiting_text,
        re.MULTILINE,
    )
    if len(open_statuses) != len(open_names):
        failures.append("status eksik")
    if failures:
        return Check("İş yükü", "FAIL", evidence + "; " + "; ".join(failures))
    return Check("İş yükü", "OK", evidence)


def _root_hygiene_check(ctx: Context) -> Check:
    def is_worktree_file(path: Path) -> bool:
        if path.name != ".git" or not path.is_file():
            return False
        try:
            marker = path.read_text(encoding="utf-8").strip()
            repository = subprocess.run(
                ["git", "-C", str(ctx.vault), "rev-parse", "--is-inside-work-tree"],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, UnicodeError, subprocess.SubprocessError):
            return False
        return (
            marker.lower().startswith("gitdir:")
            and repository.returncode == 0
            and repository.stdout.strip().lower() == "true"
        )

    try:
        unexpected = sorted(
            path.name
            for path in ctx.vault.iterdir()
            if path.is_file()
            and path.name not in ALLOWED_ROOT_FILES
            and not is_worktree_file(path)
        )
    except OSError as exc:
        return Check("Kök hijyeni", "FAIL", f"okunamadı: {exc.__class__.__name__}")
    if unexpected:
        return Check(
            "Kök hijyeni",
            "WARN",
            f"{len(unexpected)} beklenmeyen dosya: {', '.join(unexpected)}",
        )
    return Check("Kök hijyeni", "OK", "beklenmeyen dosya yok")


def _vault_retrieval_check(ctx: Context) -> Check:
    vault, state_dir, now = ctx.vault, ctx.state_dir, ctx.now
    health_path = state_dir / "retrieval-health.json"
    if health_path.exists():
        try:
            health = json.loads(health_path.read_text(encoding="utf-8"))
            error = health["error"]
        except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError):
            return Check("Vault retrieval", "FAIL", "health kaydı okunamadı")
        if not isinstance(error, str) or not error:
            return Check("Vault retrieval", "FAIL", "hata sınıfı eksik")
        return Check("Vault retrieval", "FAIL", _receipt_error(error))

    try:
        # Sayım okuyucudur: doctor korpus cache'ini yazmaz.
        entry_count = len(build_vault_map(vault, write_cache=False))
    except (OSError, UnicodeError, ValueError) as exc:
        return Check("Vault retrieval", "FAIL", f"harita kurulamadı: {exc.__class__.__name__}")
    if entry_count == 0:
        return Check("Vault retrieval", "WARN", "0 not; runtime testi bekleniyor")

    receipt_path = state_dir / "runtime-vault-retrieval.json"
    if not receipt_path.exists():
        return Check("Vault retrieval", "WARN", f"{entry_count} not; runtime kanıtı yok")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return Check("Vault retrieval", "FAIL", "runtime receipt object değil")
        timestamp = _finite_timestamp(receipt.get("ts"))
        if timestamp is None:
            return Check(
                "Vault retrieval",
                "WARN",
                f"{entry_count} not; runtime timestamp invalid",
            )
        cwd = receipt.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return Check("Vault retrieval", "FAIL", "runtime cwd geçersiz")
        observed_cwd = Path(cwd).resolve()
    except (
        OSError,
        UnicodeError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return Check("Vault retrieval", "FAIL", "runtime receipt okunamadı")
    outcome = receipt.get("outcome")
    if not isinstance(outcome, str) or outcome not in {
        "skipped",
        "empty",
        "emitted",
        "error",
    }:
        return Check("Vault retrieval", "FAIL", "runtime outcome geçersiz")
    numeric_fields = ("entries", "hits", "emitted", "chars", "budget", "duration_ms")
    if any(field not in receipt for field in numeric_fields) or "paths" not in receipt:
        return Check("Vault retrieval", "FAIL", "runtime receipt metriği eksik")
    metrics = {field: receipt[field] for field in numeric_fields if field in receipt}
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in metrics.values()
    ):
        return Check("Vault retrieval", "FAIL", "runtime receipt metriği geçersiz")
    paths_value = receipt.get("paths")
    if not isinstance(paths_value, list) or not all(
        isinstance(path, str) and path for path in paths_value
    ):
        return Check("Vault retrieval", "FAIL", "runtime receipt yolları geçersiz")
    paths = [path for path in paths_value if isinstance(path, str)]
    if len(set(paths)) != len(paths):
        return Check("Vault retrieval", "FAIL", "runtime receipt yolları geçersiz")
    if len(paths) != metrics["emitted"]:
        return Check("Vault retrieval", "FAIL", "runtime receipt sayımları tutarsız")
    if outcome != "emitted" and paths:
        return Check("Vault retrieval", "FAIL", "runtime receipt sayımları tutarsız")
    if (
        "entries" in metrics
        and "hits" in metrics
        and metrics["hits"] > metrics["entries"]
    ) or (
        "hits" in metrics
        and "emitted" in metrics
        and metrics["emitted"] > metrics["hits"]
    ) or (
        "chars" in metrics
        and "budget" in metrics
        and metrics["chars"] > metrics["budget"]
    ):
        return Check("Vault retrieval", "FAIL", "runtime receipt sayımları tutarsız")
    if outcome == "emitted":
        if metrics["emitted"] < 1 or metrics["chars"] < 1:
            return Check("Vault retrieval", "FAIL", "runtime receipt metriği tutarsız")
    else:
        if any(metrics.get(field, 0) != 0 for field in ("emitted", "chars")):
            return Check("Vault retrieval", "FAIL", "runtime receipt metriği tutarsız")
        if outcome in {"skipped", "error"} and any(
            metrics.get(field, 0) != 0 for field in ("entries", "hits")
        ):
            return Check("Vault retrieval", "FAIL", "runtime receipt metriği tutarsız")
    if outcome == "error":
        return Check("Vault retrieval", "FAIL", f"{entry_count} not; son arama başarısız")
    age = max(0, int(now - timestamp))
    if age > HOOK_RUNTIME_MAX_AGE_SECONDS or observed_cwd != ctx.project_root.resolve():
        return Check(
            "Vault retrieval",
            "WARN",
            f"{entry_count} not; runtime kanıtı güncel değil",
        )
    runtime_detail = ""
    if isinstance(outcome, str) and outcome in {"skipped", "empty", "emitted", "error"}:
        runtime_detail = f"; {outcome}"
        if outcome == "emitted":
            emitted = receipt.get("emitted")
            hits = receipt.get("hits")
            chars = receipt.get("chars")
            budget = receipt.get("budget")
            if all(isinstance(value, int) and value >= 0 for value in (emitted, hits)):
                runtime_detail += f"; {emitted}/{hits} aday"
            if all(isinstance(value, int) and value >= 0 for value in (chars, budget)):
                runtime_detail += f"; {chars}/{budget} karakter"
    return Check(
        "Vault retrieval",
        "OK",
        f"{entry_count} not{runtime_detail}; runtime kanıtı {age} sn",
    )


def _tag_taxonomy_check(ctx: Context) -> Check:
    vault, notes = ctx.vault, ctx.notes
    try:
        taxonomy = load_taxonomy(vault / ".codex" / "tag-taxonomy.json")
        violations = audit_vault(vault, taxonomy, notes)
        inline_violations = audit_inline_tags(vault, notes)
    except (OSError, UnicodeError, TaxonomyError) as exc:
        return Check(
            "Etiket sözlüğü",
            "FAIL",
            f"okunamadı: {exc}",
        )
    if not violations and not inline_violations:
        scoped_count = sum(len(tags) for tags in taxonomy.scoped.values())
        return Check(
            "Etiket sözlüğü",
            "OK",
            (
                f"{len(taxonomy.canonical)} canonical + "
                f"{scoped_count} project-scoped; ihlal yok"
            ),
        )
    if inline_violations:
        first_inline = inline_violations[0]
        return Check(
            "Etiket sözlüğü",
            "FAIL",
            (
                f"{len(inline_violations)} kontrolsüz inline etiket; "
                f"{first_inline.tag} @ {first_inline.path.name}:{first_inline.line}"
            ),
        )
    first = violations[0]
    target = first.canonical or "sözlükte yok"
    return Check(
        "Etiket sözlüğü",
        "FAIL",
        f"{len(violations)} ihlal; {first.tag} -> {target}",
    )


def _tansu_semantic_metadata_check(ctx: Context) -> Check:
    vault = ctx.vault
    semantic_map = (
        vault
        / "🏰 300-Projects"
        / "Tansu X Veri Havuzu"
        / "tansu-semantik-kullanim-haritasi.md"
    )
    if not semantic_map.is_file():
        return Check("Tansu semantik metadata", "OK", "kapsam dışı")
    try:
        audit = audit_tansu_semantic_manifest(vault)
    except (OSError, UnicodeError, ValueError) as exc:
        return Check(
            "Tansu semantik metadata",
            "FAIL",
            f"denetlenemedi: {exc}",
        )
    evidence = f"{audit.matched}/{audit.total} manifest eşleşiyor"
    if audit.mismatches:
        return Check(
            "Tansu semantik metadata",
            "FAIL",
            f"{evidence}; ilk {audit.mismatches[0]}",
        )
    return Check("Tansu semantik metadata", "OK", evidence)


def _profile_maintenance_check(ctx: Context) -> Check:
    try:
        with memory_read(ctx.vault) as memory:
            issues = memory.profile_issues()
    except MemoryPreferenceError as exc:
        return Check("Profil bakımı", "FAIL", str(exc) or "memory-preference-failed")
    return Check('Profil bakımı', 'FAIL' if issues else 'OK',
                 ', '.join(issues) if issues else 'Kaynak, kullanıcı atfı, tarih, tekrar ve bağlam sınırı geçerli.')


def _session_context_budget_check(ctx: Context) -> Check:
    try:
        context = build_session_context(
            ctx.vault,
            ctx.state_dir,
            consume_reflection=False,
            write_views=False,
        )
    except (OSError, UnicodeError) as exc:
        return Check(
            "SessionStart bağlam bütçesi",
            "FAIL",
            f"ölçülemedi: {exc.__class__.__name__}",
        )
    size = len(context)
    if size > SESSION_CONTEXT_TARGET_CHARS:
        status = "FAIL"
    elif size > SESSION_CONTEXT_SOFT_TARGET_CHARS:
        status = "WARN"
    else:
        status = "OK"
    return Check(
        "SessionStart bağlam bütçesi",
        status,
        (
            f"{size}/{SESSION_CONTEXT_TARGET_CHARS} karakter; "
            f"hedef <= {SESSION_CONTEXT_SOFT_TARGET_CHARS}"
        ),
    )


def _linked_notes(notes: Sequence[NoteIndex]) -> list[NoteIndex]:
    return [note for note in notes if note.root in LINK_ROOTS]


def _vault_link_check(ctx: Context) -> Check:
    notes = ctx.notes
    files = _linked_notes(notes)
    stems = by_stem(files)
    duplicates = {name: found for name, found in stems.items() if len(found) > 1}
    if duplicates:
        first = sorted(duplicates)[0]
        return Check("Vault bağlantıları", "FAIL", f"duplicate note adı: {first}")

    keyed = by_key(notes)
    broken: list[str] = []
    for source in files:
        if source.error:
            return Check(
                "Vault bağlantıları",
                "FAIL",
                f"okunamadı: {source.path.name} ({source.error})",
            )
        for match in WIKILINK.finditer(source.text):
            target = wikilink_target(match.group(1))
            if not target:
                continue
            if resolve_link(target, keyed, stems) is None:
                broken.append(f"{source.key} -> {target}")
    if broken:
        return Check(
            "Vault bağlantıları",
            "FAIL",
            f"{len(broken)} kırık; ilk: {broken[0]}",
        )
    return Check("Vault bağlantıları", "OK", f"{len(files)} not; kırık veya duplicate yok")


def _vault_graph_check(ctx: Context) -> Check:
    notes = ctx.notes
    unreadable = next((note for note in graph_notes(notes) if note.error), None)
    if unreadable is not None:
        return Check("Vault grafiği", "FAIL", f"okunamadı: {unreadable.error}")
    total, isolated = graph_summary(notes)
    if isolated:
        return Check(
            "Vault grafiği",
            "FAIL",
            f"{len(isolated)} izole not: {', '.join(isolated)}",
        )
    return Check("Vault grafiği", "OK", f"{total} not; izole yok")


def _archive_index_check(ctx: Context) -> Check:
    if not (ctx.vault / ARCHIVE_ROOT).is_dir():
        return Check("Archive indeks görünürlüğü", "OK", "Archive klasörü yok")
    archive = [note for note in ctx.notes if note.root == ARCHIVE_ROOT]
    index_key = f"{ARCHIVE_ROOT}/Archive.md"
    index_note = next((note for note in archive if note.key == index_key), None)
    if index_note is None:
        return Check(
            "Archive indeks görünürlüğü",
            "WARN",
            "Archive.md yok veya güvenli dosya değil",
        )
    if any(note.error for note in archive):
        return Check("Archive indeks görünürlüğü", "WARN", "Archive okunamadı")
    archived = {note.key for note in archive if note is not index_note}
    indexed: set[str] = set()
    for match in WIKILINK.finditer(index_note.text):
        target = wikilink_target(match.group(1))
        if not target:
            continue
        key = link_key(target)
        if key is not None and key in archived:
            indexed.add(key)
    missing = sorted(archived - indexed)
    if missing:
        return Check(
            "Archive indeks görünürlüğü",
            "WARN",
            f"{len(missing)} indekslenmemiş not; ilk: {missing[0]}",
        )
    return Check(
        "Archive indeks görünürlüğü",
        "OK",
        f"{len(archived)} arşiv notu indeksli",
    )


def _knowledge_schema_check(ctx: Context) -> Check:
    try:
        report = validate_knowledge_tree(ctx.vault)
    except (OSError, UnicodeError) as exc:
        return Check("Bilgi şeması", "FAIL", f"okunamadı: {exc.__class__.__name__}")
    if report.issues:
        return Check(
            "Bilgi şeması",
            "FAIL",
            f"{len(report.issues)} ihlal: {', '.join(report.issues)}",
        )
    return Check(
        "Bilgi şeması",
        "OK",
        f"{report.concepts} kavram; {report.connections} bağlantı; "
        f"{report.index_rows} indeks satırı",
    )


def _metadata_schema_check(ctx: Context) -> Check:
    notes = ctx.notes
    offenders: list[str] = []
    for note in notes:
        if DAILY_ROOT in note.relative.parts or note.error:
            continue
        if "modified" in note.frontmatter:
            offenders.append(f"{note.key}: modified")
    human = [note for note in notes if note.root in HUMAN_NOTE_ROOTS]
    titles: dict[str, list[str]] = {}
    for note in human:
        if note.error:
            offenders.append(f"{note.key}: okunamadı")
            continue
        fields = note.frontmatter
        if not fields:
            offenders.append(f"{note.key}: frontmatter")
            continue
        missing = [field for field in REQUIRED_NOTE_FIELDS if field not in fields]
        if missing:
            offenders.append(f"{note.key}: {','.join(missing)}")
        title = fields.get("title", "")
        if isinstance(title, str) and title:
            titles.setdefault(title, []).append(note.key)
    duplicate_titles = {title: keys for title, keys in titles.items() if len(keys) > 1}
    if duplicate_titles:
        first = sorted(duplicate_titles)[0]
        offenders.append(f"duplicate title: {first}")
    if not offenders:
        return Check(
            "Metadata şeması",
            "OK",
            f"{len(human)} not; required fields complete",
        )
    return Check(
        "Metadata şeması",
        "FAIL",
        f"{len(offenders)} ihlal; ilk: {offenders[0]}",
    )


CHECKS: tuple[tuple[str, Callable[[Context], Check | list[Check]]], ...] = (
    ("Sürüm", _version_check),
    ("AGENTS.md", _agents_file_check),
    ("Codex kancaları", _codex_hooks_check),
    ("Hook yorumlayıcısı", _hook_interpreter_check),
    ("Codex proje kökü", _project_root_check),
    ("Kök hijyeni", _root_hygiene_check),
    ("Hook çalışma zamanı", _hook_runtime_check),
    ("Hook sağlığı", _hook_health_check),
    ("Beyin sağlığı", _brain_health_check),
    ("State mahremiyeti", _state_privacy_check),
    ("Flush devamlılığı", _flush_inflight_check),
    ("Derived-state gitignore", _derived_state_gitignore_check),
    ("Worker kuyruğu", _worker_queue_check),
    ("Worker gecikmiş iş", _worker_delayed_job_check),
    ("State retention", _state_retention_check),
    ("Derleyici kuyruğu", _compiler_queue_check),
    ("Yerel checkpoint", _local_checkpoint_check),
    ("Git hijyeni", _git_hygiene_check),
    ("İş yükü", _thread_workload_check),
    ("Vault retrieval", _vault_retrieval_check),
    ("Etiket sözlüğü", _tag_taxonomy_check),
    ("Tansu semantik metadata", _tansu_semantic_metadata_check),
    ("SessionStart bağlam bütçesi", _session_context_budget_check),
    ("Profil bakımı", _profile_maintenance_check),
    ("Vault bağlantıları", _vault_link_check),
    ("Archive indeks görünürlüğü", _archive_index_check),
    ("Vault grafiği", _vault_graph_check),
    ("Bilgi şeması", _knowledge_schema_check),
    ("Metadata şeması", _metadata_schema_check),
    ("Python", _python_check),
    ("Codex CLI", _codex_cli_check),
    ("Hafıza", _companion_memory_check),
    ("Makine katmanı", _machine_layer_check),
    ("Git", _git_branch_check),
)

CHECK_NAMES = tuple(name for name, _check in CHECKS)


def run_checks(
    vault: Path,
    *,
    project_root: Path | None = None,
    state_dir: Path | None = None,
    now: float | None = None,
    only: str | None = None,
) -> list[Check]:
    context = Context(
        vault=vault,
        project_root=project_root or Path.cwd(),
        state_dir=state_dir or vault / ".codex" / "scripts" / ".state",
        now=time.time() if now is None else now,
    )
    checks: list[Check] = []
    for name, check in CHECKS:
        if only is not None and name != only:
            continue
        try:
            produced = check(context)
        except MemoryPreferenceError as exc:
            checks.append(Check(name, "FAIL", str(exc) or "memory-preference-failed"))
            continue
        except subprocess.SubprocessError:
            checks.append(Check(name, "FAIL", "alt süreç kontrolü tamamlanamadı"))
            continue
        except Exception as exc:
            checks.append(
                Check(
                    name,
                    "FAIL",
                    f"kontrol başarısız: {type(exc).__name__[:64]}",
                )
            )
            continue
        checks.extend(produced if isinstance(produced, list) else [produced])
    return checks


def _console_safe(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--only",
        metavar="NAME",
        choices=CHECK_NAMES,
        help="yalnız adı verilen kontrolü koşar",
    )
    args = parser.parse_args(argv)
    vault = Path(__file__).resolve().parents[2]
    checks = run_checks(vault, project_root=args.project_root, only=args.only)
    print(_console_safe("| Parça | Durum | Kanıt |"))
    print(_console_safe("| --- | --- | --- |"))
    for check in checks:
        print(_console_safe(f"| {check.name} | {check.status} | {check.evidence} |"))
    failing_statuses = {"FAIL", "UNSTABLE_SNAPSHOT"}
    exit_code = 0 if all(check.status not in failing_statuses for check in checks) else 1
    counts = {
        status: sum(check.status == status for check in checks)
        for status in ("OK", "WARN", "FAIL", "UNSTABLE", "UNSTABLE_SNAPSHOT")
    }
    print(
        _console_safe(
            "Özet: "
            f"OK={counts['OK']} WARN={counts['WARN']} FAIL={counts['FAIL']} "
            f"WAITING={counts['UNSTABLE']} "
            f"UNSTABLE={counts['UNSTABLE_SNAPSHOT']} EXIT={exit_code}"
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
