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
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
from typing import Sequence

from knowledge_schema import DAILY_SOURCE, parse_frontmatter
from memory_ledger import (
    MemoryRead,
    MemorySourceError,
    load_suppressed_hashes,
    memory_read,
    suppression_guard,
)
from state_store import atomic_write_text
from vault_corpus import DAILY_ROOT, KNOWLEDGE_ROOT, NoteIndex, markdown_paths

REPORT_RELATIVE = Path("🎯 100-Command-Center") / "Cevo Bayat İnceleme.md"
DEFAULT_DAYS = 90
DERIVED_SUBDIRS = frozenset({"concepts", "connections"})
MAX_SOURCE_FIELDS = 64
MAX_SOURCE_CHARS = 256
_SAFE_SOURCE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")


@dataclasses.dataclass(frozen=True)
class StaleFinding:
    note: str
    age_days: int
    reasons: tuple[str, ...]


def _note_date(note: NoteIndex) -> datetime.date | None:
    for field in ("updated", "created"):
        value = note.frontmatter.get(field)
        if isinstance(value, str):
            try:
                return datetime.date.fromisoformat(value.strip())
            except ValueError:
                return None
    return None


def _source_reasons(
    vault: Path,
    note: NoteIndex,
    note_date: datetime.date,
    *,
    memory: MemoryRead | None = None,
) -> list[str]:
    sources = note.frontmatter.get("sources")
    if isinstance(sources, str):
        source_values: Sequence[object] = (sources,)
    elif isinstance(sources, list):
        if len(sources) > MAX_SOURCE_FIELDS:
            return ["kaynak listesi sınırı aşıldı"]
        source_values = sources
    else:
        return []
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
        if (
            not source
            or len(source) > MAX_SOURCE_CHARS
            or not DAILY_SOURCE.fullmatch(source)
        ):
            if _SAFE_SOURCE_LABEL.fullmatch(source):
                reasons.append(f"kaynağı yok: {source}")
            else:
                reasons.append("kaynak yolu geçersiz")
            continue
        if memory is not None and (
            memory.excludes(source)
            or memory.excludes(f"{DAILY_ROOT}/{source}")
        ):
            continue
        if daily_status == "missing":
            reasons.append(f"kaynağı yok: {source}")
            continue
        if daily_status != "safe":
            reasons.append("kaynak yolu güvensiz")
            continue
        daily = daily_root / source
        try:
            source_stat = daily.lstat()
            resolved = daily.resolve(strict=False)
            if (
                stat.S_ISLNK(source_stat.st_mode)
                or not stat.S_ISREG(source_stat.st_mode)
                or not resolved.is_relative_to(vault)
                or not resolved.is_relative_to(daily_root.resolve(strict=False))
            ):
                reasons.append("kaynak yolu güvensiz")
                continue
            modified = datetime.date.fromtimestamp(source_stat.st_mtime)
        except FileNotFoundError:
            reasons.append(f"kaynağı yok: {source}")
            continue
        except (OSError, OverflowError, ValueError):
            reasons.append(f"kaynak zamanı okunamadı: {source}")
            continue
        if modified > note_date:
            reasons.append(f"kaynağı sonradan değişmiş: {source} ({modified})")
    return reasons


def _eligible_note_paths(vault: Path) -> tuple[Path, ...]:
    root = vault.resolve()
    paths = []
    for path in markdown_paths(root / KNOWLEDGE_ROOT):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) >= 3 and parts[0] == KNOWLEDGE_ROOT and parts[1] in DERIVED_SUBDIRS:
            paths.append(path)
    return tuple(sorted(paths))


def _snapshot_notes(vault: Path, memory: MemoryRead) -> tuple[NoteIndex, ...]:
    notes = []
    for path in _eligible_note_paths(vault):
        try:
            relative, text = memory.read_source(path)
        except (MemorySourceError, OSError, UnicodeError):
            continue
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
) -> list[StaleFinding]:
    findings = []
    for note in notes:
        note_date = _note_date(note)
        if note_date is None:
            findings.append(StaleFinding(note.key, -1, ("tarih alanı yok ya da bozuk",)))
            continue
        age = (today - note_date).days
        reasons = _source_reasons(vault, note, note_date, memory=memory)
        if age >= days:
            reasons.insert(0, f"{age} gündür güncellenmemiş")
        if reasons:
            findings.append(StaleFinding(note.key, age, tuple(reasons)))
    findings.sort(key=lambda finding: (-finding.age_days, finding.note))
    return findings


def review(
    vault: Path,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime.date | None = None,
) -> list[StaleFinding]:
    vault = Path(vault).resolve()
    today = now or datetime.date.today()
    with memory_read(vault) as memory:
        findings = _review_notes(
            vault,
            _snapshot_notes(vault, memory),
            days=days,
            today=today,
            memory=memory,
        )
        memory.check_knowledge_snapshot()
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
    if not findings:
        lines += ["Bayat aday yok — türetilmiş bilgi güncel görünüyor.", ""]
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
        lines.append(f"| [[{finding.note}]] | {age} | {reasons} |")
    lines.append("")
    return "\n".join(lines)


def write_report(
    vault: Path,
    output: Path | None = None,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime.date | None = None,
) -> tuple[Path, int]:
    vault = Path(vault).resolve()
    today = now or datetime.date.today()
    target = output if output is not None else vault / REPORT_RELATIVE
    private_root = vault / ".codex/private-memory"
    hashes = load_suppressed_hashes(private_root)
    with suppression_guard(private_root, hashes):
        with memory_read(vault) as memory:
            findings = _review_notes(
                vault,
                _snapshot_notes(vault, memory),
                days=days,
                today=today,
                memory=memory,
            )
            memory.check_knowledge_snapshot()
            atomic_write_text(target, render(findings, days=days, today=today))
            return target, len(findings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    target, count = write_report(args.vault, args.output, days=args.days)
    print(f"Rapor yazıldı: {target} ({count} aday)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
