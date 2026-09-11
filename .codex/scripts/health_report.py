"""Cevo altyapı durumunu Command-Center'da tek bakışlık panele yazar.

Bütün okumalar kilitsiz ve sınırlıdır: panel yaklaşık bir anlık görüntüdür,
yetkili durum state dizinindeki dosyaların kendisidir. Not `dashboard` tipiyle
üretilir ki intake sözleşmesinin üst-bağlantı zorunluluğuna takılmasın.
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import re
from typing import Any, Sequence

import compile_state
from state_store import atomic_write_text, state_dir_of

PANEL_RELATIVE = Path("🎯 100-Command-Center") / "Cevo Sağlık.md"
WORKER_STAGES = (
    "pending",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "dead-letter",
    "quarantined",
)
DEAD_LETTER_LIMIT = 10
MAX_RECORD_BYTES = 131072
_CREATED = re.compile(r"(?m)^created: (?P<value>.+)$")


def _bounded_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def worker_counts(state_dir: Path) -> dict[str, int]:
    jobs = state_dir / "worker-jobs"
    return {stage: len(list((jobs / stage).glob("*.json"))) for stage in WORKER_STAGES}


def dead_letter_rows(state_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in (state_dir / "worker-jobs" / "dead-letter").glob("*.json"):
        record = _bounded_json(path) or {}
        rows.append(
            {
                "job_id": str(record.get("job_id", path.stem))[:8],
                "kind": str(record.get("kind", "?")),
                "terminal_reason": str(record.get("terminal_reason", "?")),
                "finished_ts": record.get("finished_ts", 0),
            }
        )
    rows.sort(key=lambda row: row["finished_ts"], reverse=True)
    return rows[:DEAD_LETTER_LIMIT]


def marker_counts(state_dir: Path) -> dict[str, int]:
    def count(prefix: str) -> int:
        return sum(
            1
            for path in state_dir.glob(f"{prefix}*")
            if path.is_file() and path.suffix != ".lock"
        )

    return {
        "read_only": count("memory-read-only-"),
        "session_only": count("memory-session-only-"),
    }


def compile_summary(state_dir: Path) -> tuple[str, str]:
    try:
        state = compile_state.load(state_dir)
    except compile_state.PolicyError:
        return "?", "bozuk kayıt"
    return state.last_run or "hiç", state.last_status


def health_summary(state_dir: Path) -> str:
    path = state_dir / "health.json"
    if not path.exists():
        return "kayıt yok (temiz)"
    loaded = _bounded_json(path)
    if loaded is None:
        return "okunamadı"
    components = loaded.get("components")
    if not isinstance(components, dict):
        return "okunamadı"
    errors = sum(1 for entry in components.values() if isinstance(entry, dict) and entry.get("status") == "error")
    warnings = sum(1 for entry in components.values() if isinstance(entry, dict) and entry.get("status") == "warning")
    if not errors and not warnings:
        return "temiz"
    return f"hata={errors} uyarı={warnings}"


def flush_state_count(state_dir: Path) -> int:
    return sum(
        1
        for path in state_dir.glob("flush-*.json")
        if path.is_file()
    )


def _format_ts(value: Any) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return "?"
    if stamp <= 0:
        return "?"
    return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def _previous_created(output: Path, fallback: str) -> str:
    try:
        head = output.read_text(encoding="utf-8")[:2048]
    except (OSError, UnicodeError):
        return fallback
    match = _CREATED.search(head)
    return match.group("value").strip() if match else fallback


def render(vault: Path, *, now: datetime.datetime | None = None) -> str:
    state_dir = state_dir_of(vault)
    moment = now or datetime.datetime.now()
    today = moment.strftime("%Y-%m-%d")
    created = _previous_created(vault / PANEL_RELATIVE, today)

    counts = worker_counts(state_dir)
    markers = marker_counts(state_dir)
    last_run, last_status = compile_summary(state_dir)
    dead_rows = dead_letter_rows(state_dir)

    lines = [
        "---",
        "title: Cevo Sağlık",
        f"created: {created}",
        f"updated: {today}",
        "type: dashboard",
        "status: active",
        "tags:",
        "  - sistem",
        "  - sağlık",
        "---",
        "# Cevo Sağlık",
        "",
        f"Üretilme: {moment.strftime('%Y-%m-%d %H:%M')} — bu notu `health_report.py` üretir; elle düzenleme bir sonraki çalıştırmada silinir.",
        "",
        "## İş kuyruğu",
        "",
        "| Aşama | Adet |",
        "| --- | --- |",
    ]
    lines.extend(f"| {stage} | {counts[stage]} |" for stage in WORKER_STAGES)
    lines += ["", "## Dead-letter", ""]
    if dead_rows:
        lines += [
            f"Takılı iş var: {counts['dead-letter']} kayıt. En yeniler:",
            "",
            "| İş | Tür | Neden | Bitiş |",
            "| --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {row['job_id']} | {row['kind']} | {row['terminal_reason']} | {_format_ts(row['finished_ts'])} |"
            for row in dead_rows
        )
    else:
        lines.append("Boş — takılı iş yok.")
    lines += [
        "",
        "## İşaretler ve durum",
        "",
        f"- Aktif read-only işareti: {markers['read_only']}",
        f"- Aktif session-only işareti: {markers['session_only']}",
        f"- Flush durum dosyası: {flush_state_count(state_dir)}",
        f"- Derleyici son çalışma: {last_run} (durum: {last_status})",
        f"- Sağlık kaydı (health.json): {health_summary(state_dir)}",
        "",
    ]
    return "\n".join(lines)


def write_report(
    vault: Path,
    output: Path | None = None,
    *,
    now: datetime.datetime | None = None,
) -> Path:
    vault = Path(vault).resolve()
    target = output if output is not None else vault / PANEL_RELATIVE
    atomic_write_text(target, render(vault, now=now))
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    target = write_report(args.vault, args.output)
    print(f"Panel yazıldı: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
