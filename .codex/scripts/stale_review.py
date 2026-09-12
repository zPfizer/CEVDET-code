"""Türetilmiş bilgi notlarının bayatlama adaylarını raporlar.

Yazma tarafında suppress var ama zaman tarafı yoktu: eski bir bilgi notu,
dayandığı daily kaynağı sonradan değişmiş olsa bile aynı ağırlıkla geri
çağrılıyordu. Bu betik yalnız aday listeler; hiçbir notu değiştirmez veya
bastırmaz — karar kullanıcınındır (yanlışsa notu düzeltir ya da bastırır).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
from typing import Iterator, Sequence

from file_lock import locked
from knowledge_schema import DAILY_SOURCE, DATE, parse_frontmatter
from memory_ledger import (
    MemoryPreferenceError,
    MemoryRead,
    load_suppressed_hashes,
    memory_read,
    sanitize_text,
    suppression_guard,
)
from state_store import atomic_write_text, state_dir_of
from vault_corpus import (
    DAILY_ROOT,
    EXCLUDED_DIRS,
    KNOWLEDGE_ROOT,
    NoteIndex,
)

REPORT_RELATIVE = Path("🎯 100-Command-Center") / "Cevo Bayat İnceleme.md"
DEFAULT_DAYS = 90
DERIVED_SUBDIRS = frozenset({"concepts", "connections"})
MAX_SOURCE_FIELDS = 64
MAX_SOURCE_CHARS = 256
_SAFE_SOURCE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_NOTE_LINK_UNSAFE = frozenset("[]|#^\\`\r\n")


def _validate_days(days: int) -> int:
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("days-invalid")
    return days


def _path_component_key(value: str) -> str:
    normalized = os.path.normcase(value).casefold()
    return normalized.rstrip(" .") if os.name == "nt" else normalized


def _default_report_target(vault: Path) -> Path:
    target = vault / REPORT_RELATIVE
    parent = target.parent
    try:
        parent_stat = parent.lstat()
        if (
            stat.S_ISLNK(parent_stat.st_mode)
            or parent.is_junction()
            or not stat.S_ISDIR(parent_stat.st_mode)
        ):
            raise ValueError("report-target-invalid")
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError) as exc:
        raise ValueError("report-target-invalid") from exc
    try:
        resolved_parent = parent.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("report-target-invalid") from exc
    if not resolved_parent.is_relative_to(vault):
        raise ValueError("report-target-invalid")
    return target


def _validate_report_target(vault: Path, target: Path) -> None:
    lexical = target if target.is_absolute() else Path.cwd() / target
    lexical = lexical.absolute()
    protected_roots = {
        _path_component_key(root)
        for root in (DAILY_ROOT, KNOWLEDGE_ROOT, ".codex")
    }

    def protected(relative: Path) -> bool:
        return (
            bool(relative.parts)
            and _path_component_key(relative.parts[0]) in protected_roots
        )

    try:
        resolved_target = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("report-target-invalid") from exc
    try:
        relative_target = lexical.relative_to(vault)
    except ValueError:
        try:
            resolved_relative = resolved_target.relative_to(vault)
        except ValueError:
            return
        if protected(resolved_relative):
            raise ValueError("report-target-invalid")
        return
    if protected(relative_target):
        raise ValueError("report-target-invalid")
    try:
        resolved_relative = resolved_target.relative_to(vault)
    except ValueError:
        resolved_relative = None
    if resolved_relative is not None and protected(resolved_relative):
        raise ValueError("report-target-invalid")
    relative_parent = lexical.parent.relative_to(vault)
    current = vault
    for part in relative_parent.parts:
        current /= part
        try:
            current_stat = current.lstat()
            resolved = current.resolve(strict=False)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            raise ValueError("report-target-invalid") from exc
        if (
            stat.S_ISLNK(current_stat.st_mode)
            or not stat.S_ISDIR(current_stat.st_mode)
            or current.is_junction()
            or not resolved.is_relative_to(vault)
        ):
            raise ValueError("report-target-invalid")


@dataclasses.dataclass(frozen=True)
class StaleFinding:
    note: str
    age_days: int
    reasons: tuple[str, ...]


def _note_date(note: NoteIndex) -> datetime.date | None:
    value = note.frontmatter.get("updated")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if DATE.fullmatch(value) is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _daily_source_date(source: str) -> datetime.date | None:
    if DAILY_SOURCE.fullmatch(source) is None:
        return None
    try:
        return datetime.date.fromisoformat(source[:-3])
    except ValueError:
        return None


def _source_reasons(
    vault: Path,
    note: NoteIndex,
    note_date: datetime.date,
    *,
    memory: MemoryRead | None = None,
    observations: dict[Path, tuple[int, int, int, int, int] | None] | None = None,
) -> list[str]:
    sources = note.frontmatter.get("sources")
    if isinstance(sources, list):
        if not sources:
            return ["kaynak alanı yok ya da bozuk"]
        if len(sources) > MAX_SOURCE_FIELDS:
            return ["kaynak listesi sınırı aşıldı"]
        source_values = sources
    else:
        return ["kaynak alanı yok ya da bozuk"]
    reasons = []
    daily_root = vault / DAILY_ROOT
    try:
        daily_stat = daily_root.lstat()
        daily_status = "safe" if (
            stat.S_ISDIR(daily_stat.st_mode)
            and not stat.S_ISLNK(daily_stat.st_mode)
            and not daily_root.is_junction()
            and daily_root.resolve(strict=False).is_relative_to(vault)
        ) else "unsafe"
    except FileNotFoundError:
        daily_status = "missing"
    except (OSError, RuntimeError):
        daily_status = "unsafe"
    for raw_source in source_values:
        if not isinstance(raw_source, str):
            reasons.append("kaynak yolu geçersiz")
            continue
        source = raw_source.strip()
        if len(source) <= MAX_SOURCE_CHARS and memory is not None and (
            memory.excludes(source)
            or memory.excludes(f"{DAILY_ROOT}/{source}")
        ):
            reasons.append("kaynak güveni doğrulanamadı")
            continue
        source_date = _daily_source_date(source)
        if (
            not source
            or len(source) > MAX_SOURCE_CHARS
            or source_date is None
        ):
            if (
                _SAFE_SOURCE_LABEL.fullmatch(source)
                and DAILY_SOURCE.fullmatch(source) is None
            ):
                reasons.append(f"kaynağı yok: {source}")
            else:
                reasons.append("kaynak yolu geçersiz")
            continue
        daily = daily_root / source
        if daily_status == "missing":
            if observations is not None:
                observations.setdefault(daily, None)
            reasons.append(f"kaynağı yok: {source}")
            continue
        if daily_status != "safe":
            reasons.append("kaynak yolu güvensiz")
            continue
        try:
            source_stat = daily.lstat()
            resolved = daily.resolve(strict=False)
            if (
                stat.S_ISLNK(source_stat.st_mode)
                or not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_nlink != 1
                or not resolved.is_relative_to(vault)
                or not resolved.is_relative_to(daily_root.resolve(strict=False))
            ):
                reasons.append("kaynak yolu güvensiz")
                continue
            modified = datetime.date.fromtimestamp(source_stat.st_mtime)
        except FileNotFoundError:
            if observations is not None:
                observations.setdefault(daily, None)
            reasons.append(f"kaynağı yok: {source}")
            continue
        except (OSError, OverflowError, ValueError):
            reasons.append(f"kaynak zamanı okunamadı: {source}")
            continue
        if source_date > note_date:
            reasons.append(f"kaynak tarihi nottan sonra: {source} ({source_date})")
        try:
            relative, text = memory.read_source(
                daily,
                relative=f"{DAILY_ROOT}/{source}",
            ) if memory is not None else (None, "")
        except (OSError, UnicodeError):
            reasons.append(f"kaynak okunamadı: {source}")
            continue
        expected_relative = PurePosixPath(f"{DAILY_ROOT}/{source}")
        if memory is not None and (
            PurePosixPath(relative.as_posix()) != expected_relative
            or not isinstance(text, str)
            or not text.strip()
        ):
            reasons.append("kaynak güveni doğrulanamadı")
            continue
        if observations is not None:
            observations.setdefault(
                daily,
                (
                    source_stat.st_dev,
                    source_stat.st_ino,
                    source_stat.st_size,
                    source_stat.st_mtime_ns,
                    source_stat.st_ctime_ns,
                ),
            )
        if modified > note_date:
            reasons.append(f"kaynağı sonradan değişmiş: {source} ({modified})")
        elif modified == note_date:
            reasons.append(f"kaynak değişim zamanı belirsiz: {source} ({modified})")
    return reasons


def _validate_source_observations(
    vault: Path,
    observations: dict[Path, tuple[int, int, int, int, int] | None],
) -> None:
    if not observations:
        return
    daily_root = vault / DAILY_ROOT
    try:
        daily_stat = daily_root.lstat()
        daily_resolved = daily_root.resolve(strict=False)
        if (
            stat.S_ISLNK(daily_stat.st_mode)
            or not stat.S_ISDIR(daily_stat.st_mode)
            or daily_root.is_junction()
            or not daily_resolved.is_relative_to(vault)
        ):
            raise ValueError("daily-root-invalid")
        for path, expected in observations.items():
            try:
                source_stat = path.lstat()
            except FileNotFoundError:
                if expected is None:
                    continue
                raise
            resolved = path.resolve(strict=False)
            if (
                stat.S_ISLNK(source_stat.st_mode)
                or not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_nlink != 1
                or not resolved.is_relative_to(vault)
                or not resolved.is_relative_to(daily_resolved)
            ):
                raise ValueError("daily-source-invalid")
            if expected is None:
                raise ValueError("daily-source-created")
            current = (
                source_stat.st_dev,
                source_stat.st_ino,
                source_stat.st_size,
                source_stat.st_mtime_ns,
                source_stat.st_ctime_ns,
            )
            if current != expected:
                raise ValueError("daily-source-changed")
    except FileNotFoundError:
        if all(expected is None for expected in observations.values()):
            return
        raise MemoryPreferenceError("stale-review-source-changed")
    except (OSError, RuntimeError, ValueError) as exc:
        raise MemoryPreferenceError("stale-review-source-changed") from exc


def _validate_vault(vault: Path) -> None:
    try:
        vault_stat = vault.lstat()
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise MemoryPreferenceError("stale-review-vault-invalid") from exc
    if not stat.S_ISDIR(vault_stat.st_mode) or vault.is_junction():
        raise MemoryPreferenceError("stale-review-vault-invalid")
    knowledge = vault / KNOWLEDGE_ROOT
    try:
        knowledge_stat = knowledge.lstat()
        knowledge_resolved = knowledge.resolve(strict=False)
        if (
            stat.S_ISLNK(knowledge_stat.st_mode)
            or not stat.S_ISDIR(knowledge_stat.st_mode)
            or knowledge.is_junction()
            or not knowledge_resolved.is_relative_to(vault)
        ):
            raise ValueError("knowledge-root-invalid")
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise MemoryPreferenceError("stale-review-knowledge-root-invalid") from exc


def _validate_runtime_paths(vault: Path) -> None:
    for parts in (
        (".codex", "scripts", ".state"),
        (".codex", "private-memory", "controls"),
    ):
        current = vault
        for part in parts:
            current /= part
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError) as exc:
                raise MemoryPreferenceError("stale-review-runtime-path-invalid") from exc
            try:
                resolved = current.resolve(strict=False)
                if (
                    stat.S_ISLNK(current_stat.st_mode)
                    or not stat.S_ISDIR(current_stat.st_mode)
                    or current.is_junction()
                    or not resolved.is_relative_to(vault)
                ):
                    raise ValueError("runtime-path-invalid")
            except (OSError, RuntimeError, ValueError) as exc:
                raise MemoryPreferenceError("stale-review-runtime-path-invalid") from exc
    for parts in (
        (".codex", "scripts", ".state", "compile.lock"),
        (".codex", "private-memory", "controls", "suppressions.jsonl"),
        (".codex", "private-memory", "controls", "suppressions.lock"),
    ):
        current = vault.joinpath(*parts)
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            raise MemoryPreferenceError("stale-review-runtime-path-invalid") from exc
        try:
            resolved = current.resolve(strict=False)
            if (
                stat.S_ISLNK(current_stat.st_mode)
                or not stat.S_ISREG(current_stat.st_mode)
                or current_stat.st_nlink != 1
                or not resolved.is_relative_to(vault)
            ):
                raise ValueError("runtime-file-invalid")
        except (OSError, RuntimeError, ValueError) as exc:
            raise MemoryPreferenceError("stale-review-runtime-path-invalid") from exc


def _validate_note_observations(
    vault: Path,
    observations: dict[Path, tuple[int, int, int, int, int]],
) -> None:
    for path, expected in observations.items():
        try:
            path_stat = path.lstat()
            resolved = path.resolve(strict=False)
            if (
                stat.S_ISLNK(path_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_nlink != 1
                or not resolved.is_relative_to(vault / KNOWLEDGE_ROOT)
                or not resolved.is_relative_to(
                    vault / KNOWLEDGE_ROOT / path.relative_to(vault).parts[1]
                )
            ):
                raise ValueError("knowledge-note-invalid")
            current = (
                path_stat.st_dev,
                path_stat.st_ino,
                path_stat.st_size,
                path_stat.st_mtime_ns,
                path_stat.st_ctime_ns,
            )
            if current != expected:
                raise ValueError("knowledge-note-changed")
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise MemoryPreferenceError("stale-review-note-changed") from exc


def _eligible_note_paths(vault: Path) -> tuple[Path, ...]:
    root = vault.resolve()
    paths = []
    knowledge = root / KNOWLEDGE_ROOT
    try:
        knowledge_stat = knowledge.lstat()
    except FileNotFoundError:
        raise MemoryPreferenceError("stale-review-knowledge-root-invalid")
    except (OSError, RuntimeError) as exc:
        raise MemoryPreferenceError("stale-review-knowledge-root-unreadable") from exc
    try:
        knowledge_resolved = knowledge.resolve(strict=False)
        if (
            stat.S_ISLNK(knowledge_stat.st_mode)
            or not stat.S_ISDIR(knowledge_stat.st_mode)
            or knowledge.is_junction()
            or not knowledge_resolved.is_relative_to(root)
        ):
            raise ValueError("knowledge-root-invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        raise MemoryPreferenceError("stale-review-knowledge-root-invalid") from exc
    for name in DERIVED_SUBDIRS:
        derived = knowledge / name
        try:
            derived_stat = derived.lstat()
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            raise MemoryPreferenceError("stale-review-knowledge-root-unreadable") from exc
        try:
            derived_resolved = derived.resolve(strict=False)
            if (
                stat.S_ISLNK(derived_stat.st_mode)
                or not stat.S_ISDIR(derived_stat.st_mode)
                or derived.is_junction()
                or not derived_resolved.is_relative_to(knowledge_resolved)
            ):
                raise ValueError("derived-root-invalid")
        except (OSError, RuntimeError, ValueError) as exc:
            raise MemoryPreferenceError("stale-review-knowledge-root-invalid") from exc
        for path in _checked_markdown_paths(derived):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            parts = relative.parts
            if len(parts) >= 3 and parts[0] == KNOWLEDGE_ROOT and parts[1] in DERIVED_SUBDIRS:
                paths.append(path)
    return tuple(sorted(paths))


def _checked_markdown_paths(root: Path) -> Iterator[Path]:
    def read_error(error: OSError) -> None:
        raise error

    try:
        walker = os.walk(
            root,
            topdown=True,
            followlinks=False,
            onerror=read_error,
        )
        for current, directories, files in walker:
            current_path = Path(current)
            kept_directories = []
            for name in directories:
                if name.casefold() in EXCLUDED_DIRS:
                    continue
                path = current_path / name
                try:
                    path_stat = path.lstat()
                    if (
                        stat.S_ISLNK(path_stat.st_mode)
                        or path.is_junction()
                        or not stat.S_ISDIR(path_stat.st_mode)
                    ):
                        raise ValueError("linked-directory")
                except (OSError, RuntimeError, ValueError) as exc:
                    raise MemoryPreferenceError(
                        "stale-review-knowledge-root-invalid"
                    ) from exc
                kept_directories.append(name)
            directories[:] = kept_directories
            for name in files:
                if Path(name).suffix.casefold() != ".md":
                    continue
                path = current_path / name
                try:
                    path_stat = path.lstat()
                    if (
                        stat.S_ISLNK(path_stat.st_mode)
                        or path.is_junction()
                        or not stat.S_ISREG(path_stat.st_mode)
                        or path_stat.st_nlink != 1
                    ):
                        raise ValueError("linked-file")
                except (OSError, RuntimeError, ValueError) as exc:
                    raise MemoryPreferenceError(
                        "stale-review-knowledge-root-invalid"
                    ) from exc
                yield path
    except MemoryPreferenceError:
        raise
    except OSError as exc:
        raise MemoryPreferenceError(
            "stale-review-knowledge-root-unreadable"
        ) from exc


def _snapshot_notes(
    vault: Path,
    memory: MemoryRead,
    observations: dict[Path, tuple[int, int, int, int, int] | None] | None = None,
) -> tuple[NoteIndex, ...]:
    notes = []
    for path in _eligible_note_paths(vault):
        try:
            path_stat = path.lstat()
            if (
                stat.S_ISLNK(path_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_nlink != 1
            ):
                raise ValueError("knowledge-note-invalid")
            if observations is not None:
                observations.setdefault(
                    path,
                    (
                        path_stat.st_dev,
                        path_stat.st_ino,
                        path_stat.st_size,
                        path_stat.st_mtime_ns,
                        path_stat.st_ctime_ns,
                    ),
                )
        except (OSError, RuntimeError, ValueError) as exc:
            raise MemoryPreferenceError("stale-review-source-identity") from exc
        relative, text = memory.read_source(path)
        expected_relative = PurePosixPath(path.relative_to(vault).as_posix())
        if PurePosixPath(relative.as_posix()) != expected_relative:
            raise MemoryPreferenceError("stale-review-source-identity")
        if text is None:
            continue
        notes.append(
            NoteIndex(
                path,
                PurePosixPath(relative.as_posix()),
                text,
                parse_frontmatter(text),
            )
        )
    return tuple(notes)


def _review_notes(
    vault: Path,
    notes: Sequence[NoteIndex],
    *,
    days: int,
    today: datetime.date,
    memory: MemoryRead,
    observations: dict[Path, tuple[int, int, int, int, int]] | None = None,
) -> list[StaleFinding]:
    findings = []
    for note in notes:
        try:
            safe_note = sanitize_text(note.key, max_chars=None)[0]
        except (MemoryPreferenceError, ValueError) as exc:
            raise MemoryPreferenceError("stale-review-note-unverifiable") from exc
        note_date = _note_date(note)
        if note_date is None:
            findings.append(
                StaleFinding(safe_note, -1, ("tarih alanı yok ya da bozuk",))
            )
            continue
        age = (today - note_date).days
        if age < 0:
            reasons = ["tarih gelecekte"]
        else:
            reasons = []
        reasons.extend(
            _source_reasons(
                vault,
                note,
                note_date,
                memory=memory,
                observations=observations,
            )
        )
        if age >= days:
            reasons.insert(0, f"{age} gündür güncellenmemiş")
        if reasons:
            findings.append(StaleFinding(safe_note, age, tuple(reasons)))
    findings.sort(key=lambda finding: (-finding.age_days, finding.note))
    return findings


def review(
    vault: Path,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime.date | None = None,
) -> list[StaleFinding]:
    days = _validate_days(days)
    vault = Path(vault).resolve()
    _validate_vault(vault)
    _validate_runtime_paths(vault)
    today = now or datetime.date.today()
    with memory_read(vault) as memory:
        observations: dict[Path, tuple[int, int, int, int, int] | None] = {}
        note_observations: dict[Path, tuple[int, int, int, int, int]] = {}
        notes = _snapshot_notes(vault, memory, note_observations)
        findings = _review_notes(
            vault,
            notes,
            days=days,
            today=today,
            memory=memory,
            observations=observations,
        )
        memory.check_knowledge_snapshot()
        _validate_note_observations(vault, note_observations)
        _validate_source_observations(vault, observations)
        return findings


def render(findings: Sequence[StaleFinding], *, days: int, today: datetime.date) -> str:
    lines = [
        "---",
        "title: Cevo Bayat İnceleme",
        f"created: {today.isoformat()}",
        f"updated: {today.isoformat()}",
        "type: dashboard",
        "status: active",
        "tags:",
        "  - sistem",
        "  - hafıza",
        "---",
        "# Cevo Bayat İnceleme",
        "",
        f"Üretilme: {today.isoformat()} — eşik {days} gün. Bu notu `stale_review.py` üretir.",
        "",
        "Aday hâlâ doğruysa dokunma; yanlışsa bilgi notunu düzelt ya da Cevo'ya",
        "bastırmasını söyle. Bu rapor hiçbir şeyi kendiliğinden silmez.",
        "",
    ]

    def finding_link(note: str) -> str:
        if any(character in _NOTE_LINK_UNSAFE for character in note):
            longest_backticks = max(
                (len(run) for run in re.findall(r"`+", note)),
                default=0,
            )
            delimiter = "`" * (longest_backticks + 1)
            escaped = (
                note.replace("\\", "\\\\")
                .replace("\r", "\\r")
                .replace("\n", "\\n")
                .replace("|", "\\|")
                .replace("[", "\\[")
                .replace("]", "\\]")
            )
            return f"{delimiter}{escaped}{delimiter}"
        return f"[[{note}]]"

    if not findings:
        lines += [
            "Taranan tarih ve kaynak ölçütleriyle bayat aday saptanmadı.",
            "",
        ]
        return "\n".join(lines)
    lines += [
        f"{len(findings)} aday:",
        "",
        "| Not | Yaş (gün) | Neden |",
        "| --- | --- | --- |",
    ]
    for finding in findings:
        age = "?" if finding.age_days < 0 else str(finding.age_days)
        reasons = "; ".join(finding.reasons)
        lines.append(f"| {finding_link(finding.note)} | {age} | {reasons} |")
    lines.append("")
    return "\n".join(lines)


def write_report(
    vault: Path,
    output: Path | None = None,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime.date | None = None,
    overwrite: bool = False,
) -> tuple[Path, int]:
    days = _validate_days(days)
    vault = Path(vault).resolve()
    _validate_vault(vault)
    _validate_runtime_paths(vault)
    today = now or datetime.date.today()
    state_dir = state_dir_of(vault)
    private_root = vault / ".codex/private-memory"
    with locked(state_dir / "compile", timeout=0):
        target = output if output is not None else _default_report_target(vault)
        _validate_report_target(vault, target)
        hashes = load_suppressed_hashes(private_root)
        with suppression_guard(private_root, hashes):
            with memory_read(vault) as memory:
                observations: dict[
                    Path, tuple[int, int, int, int, int] | None
                ] = {}
                note_observations: dict[Path, tuple[int, int, int, int, int]] = {}
                notes = _snapshot_notes(vault, memory, note_observations)
                findings = _review_notes(
                    vault,
                    notes,
                    days=days,
                    today=today,
                    memory=memory,
                    observations=observations,
                )
                rendered = render(findings, days=days, today=today)
                memory.check_knowledge_snapshot()
                _validate_note_observations(vault, note_observations)
                _validate_source_observations(vault, observations)
                _validate_report_target(vault, target)
                atomic_write_text(
                    target,
                    rendered,
                    overwrite=overwrite,
                )
                return target, len(findings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing report target",
    )
    args = parser.parse_args(argv)
    target, count = write_report(
        args.vault,
        args.output,
        days=args.days,
        overwrite=args.overwrite,
    )
    print(f"Rapor yazıldı: {target} ({count} aday)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
