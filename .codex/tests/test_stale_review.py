import datetime
import os
from pathlib import Path
import tempfile
import unittest

from _fixtures import CODEX_DIR  # noqa: F401
import compile_state
import memory_ledger as ledger
import stale_review
from state_store import state_dir_of


def _note(vault: Path, name: str, *, updated: str, sources: list[str]) -> Path:
    lines = ["---", f"title: {name}", "created: 2026-01-01", f"updated: {updated}"]
    if sources:
        lines.append("sources:")
        lines.extend(f"  - {source}" for source in sources)
    lines += ["---", "içerik"]
    path = vault / "knowledge" / "concepts" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _daily(vault: Path, name: str, *, mtime: datetime.date) -> Path:
    path = vault / "daily" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("günlük", encoding="utf-8")
    stamp = datetime.datetime.combine(mtime, datetime.time(12)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


class StaleReviewTests(unittest.TestCase):
    def test_old_note_and_drifted_source_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            today = datetime.date(2026, 9, 11)
            # Eski VE kaynağı sonradan değişmiş: iki neden birden.
            _daily(vault, "2026-03-01.md", mtime=datetime.date(2026, 8, 1))
            _note(vault, "eski-not", updated="2026-03-02", sources=["2026-03-01.md"])
            # Taze not, kaynağı da eski: hiç görünmemeli.
            _daily(vault, "2026-09-01.md", mtime=datetime.date(2026, 9, 1))
            _note(vault, "taze-not", updated="2026-09-10", sources=["2026-09-01.md"])

            findings = stale_review.review(vault, days=90, now=today)

        self.assertEqual([finding.note for finding in findings], ["knowledge/concepts/eski-not.md"])
        reasons = findings[0].reasons
        self.assertEqual(findings[0].age_days, 193)
        self.assertIn("193 gündür güncellenmemiş", reasons)
        self.assertTrue(any("kaynağı sonradan değişmiş: 2026-03-01.md" in reason for reason in reasons))

    def test_fresh_note_with_drifted_source_is_still_flagged(self) -> None:
        # Sürüklenme yaştan bağımsız bir sinyal: not dün güncellenmiş olsa
        # bile dayandığı daily ondan sonra değiştiyse incelenmeli.
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            today = datetime.date(2026, 9, 11)
            _daily(vault, "2026-09-09.md", mtime=datetime.date(2026, 9, 10))
            _note(vault, "taze-ama-kaymis", updated="2026-09-09", sources=["2026-09-09.md"])

            findings = stale_review.review(vault, days=90, now=today)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].age_days, 2)
        self.assertTrue(any("kaynağı sonradan değişmiş" in reason for reason in findings[0].reasons))

    def test_missing_source_and_broken_date_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            today = datetime.date(2026, 9, 11)
            _note(vault, "kaynaksiz", updated="2026-09-10", sources=["yok-boyle-gun.md"])
            broken = vault / "knowledge" / "concepts" / "tarihsiz.md"
            broken.write_text("---\ntitle: tarihsiz\n---\niçerik", encoding="utf-8")

            findings = {
                finding.note: finding
                for finding in stale_review.review(vault, days=90, now=today)
            }

        self.assertIn(
            "kaynağı yok: yok-boyle-gun.md",
            findings["knowledge/concepts/kaynaksiz.md"].reasons,
        )
        self.assertEqual(
            findings["knowledge/concepts/tarihsiz.md"].reasons,
            ("tarih alanı yok ya da bozuk",),
        )

    def test_report_written_to_command_center_with_findings_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            today = datetime.date(2026, 9, 11)
            _note(vault, "eski-not", updated="2026-01-02", sources=[])

            target, count = stale_review.write_report(vault, days=90, now=today)
            text = target.read_text(encoding="utf-8")

        self.assertEqual(count, 1)
        self.assertEqual(target.name, "Cevo Bayat İnceleme.md")
        self.assertIn("| [[knowledge/concepts/eski-not.md]] | 252 |", text)
        self.assertIn("type: dashboard", text)

    def test_clean_vault_renders_empty_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "knowledge" / "concepts").mkdir(parents=True)
            target, count = stale_review.write_report(
                vault, days=90, now=datetime.date(2026, 9, 11)
            )
            text = target.read_text(encoding="utf-8")

        self.assertEqual(count, 0)
        self.assertIn("Bayat aday yok", text)

    def test_suppressed_note_is_excluded_before_report_wikilink_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "gizli-not", updated="2026-01-02", sources=[])
            _note(vault, "acik-not", updated="2026-01-03", sources=[])
            ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "gizli-not"
            )

            findings = stale_review.review(
                vault, days=90, now=datetime.date(2026, 9, 11)
            )
            target, count = stale_review.write_report(
                vault, days=90, now=datetime.date(2026, 9, 11)
            )
            text = target.read_text(encoding="utf-8")

        self.assertEqual(count, 1)
        self.assertEqual([finding.note for finding in findings], [
            "knowledge/concepts/acik-not.md"
        ])
        self.assertNotIn("[[knowledge/concepts/gizli-not.md]]", text)

    def test_malformed_suppression_controls_fail_closed_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            controls = vault / ".codex/private-memory/controls"
            controls.mkdir(parents=True)
            (controls / "suppressions.jsonl").write_text("{\n", encoding="utf-8")
            target = vault / "report.md"

            with self.assertRaisesRegex(ledger.MemoryPreferenceError, "memory-suppression-invalid"):
                stale_review.write_report(vault, output=target)

        self.assertFalse(target.exists())

    def test_pending_publication_fails_closed_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "pending", updated="2026-01-02", sources=[])
            compile_state.save_publication(
                state_dir_of(vault), {"schema_version": 1, "status": "pending"}
            )
            target = vault / "report.md"

            with self.assertRaisesRegex(ledger.MemoryPreferenceError, "publication-pending"):
                stale_review.write_report(vault, output=target)

        self.assertFalse(target.exists())

    def test_source_path_escape_is_not_statted_or_echoed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            outside = vault.parent / "outside-secret.md"
            outside.write_text("özel içerik", encoding="utf-8")
            try:
                _note(
                    vault,
                    "kaynak-yolu",
                    updated="2026-01-02",
                    sources=[
                        "/tmp/outside-secret.md",
                        "../outside-secret.md",
                        r"C:\outside-secret.md",
                    ],
                )
                findings = stale_review.review(
                    vault, days=90, now=datetime.date(2026, 9, 11)
                )
                target, _count = stale_review.write_report(
                    vault, now=datetime.date(2026, 9, 11)
                )
                text = target.read_text(encoding="utf-8")
            finally:
                outside.unlink()

        reasons = findings[0].reasons
        self.assertEqual(reasons[0], "252 gündür güncellenmemiş")
        self.assertEqual(reasons[1:], (
            "kaynak yolu geçersiz",
            "kaynak yolu geçersiz",
            "kaynak yolu geçersiz",
        ))
        self.assertNotIn("outside-secret", text)


if __name__ == "__main__":
    unittest.main()
