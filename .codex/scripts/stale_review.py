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
from typing import Sequence

from state_store import atomic_write_text
from vault_corpus import DAILY_ROOT, KNOWLEDGE_ROOT, NoteIndex, vault_notes

REPORT_RELATIVE = Path("🎯 100-Command-Center") / "Cevo Bayat İnceleme.md"
DEFAULT_DAYS = 90
DERIVED_SUBDIRS = frozenset({"concepts", "connections"})


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
) -> list[str]:
    sources = note.frontmatter.get("sources")
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list):
        return []
    reasons = []
    for source in sources:
        if not isinstance(source, str) or not source.strip():
            continue
        daily = vault / DAILY_ROOT / source.strip()
        try:
            modified = datetime.date.fromtimestamp(daily.stat().st_mtime)
        except OSError:
            reasons.append(f"kaynağı yok: {source.strip()}")
            continue
        if modified > note_date:
            reasons.append(f"kaynağı sonradan değişmiş: {source.strip()} ({modified})")
    return reasons


def review(
    vault: Path,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime.date | None = None,
) -> list[StaleFinding]:
    vault = Path(vault).resolve()
    today = now or datetime.date.today()
    findings = []
    for note in vault_notes(vault):
        parts = note.relative.parts
        if note.root != KNOWLEDGE_ROOT or len(parts) < 3 or parts[1] not in DERIVED_SUBDIRS:
            continue
        note_date = _note_date(note)
        if note_date is None:
            findings.append(StaleFinding(note.key, -1, ("tarih alanı yok ya da bozuk",)))
            continue
        age = (today - note_date).days
        reasons = _source_reasons(vault, note, note_date)
        if age >= days:
            reasons.insert(0, f"{age} gündür güncellenmemiş")
        if reasons:
            findings.append(StaleFinding(note.key, age, tuple(reasons)))
    findings.sort(key=lambda finding: (-finding.age_days, finding.note))
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
    findings = review(vault, days=days, now=today)
    target = output if output is not None else vault / REPORT_RELATIVE
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
