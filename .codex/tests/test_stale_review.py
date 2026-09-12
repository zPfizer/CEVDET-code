import datetime
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import compile_state
from file_lock import LockUnavailable, locked as file_locked
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

    def test_same_day_source_change_is_flagged_as_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            today = datetime.date(2026, 9, 11)
            _daily(vault, "2026-09-11.md", mtime=today)
            _note(
                vault,
                "ayni-gun-not",
                updated=today.isoformat(),
                sources=["2026-09-11.md"],
            )

            findings = stale_review.review(vault, days=90, now=today)

        self.assertEqual(len(findings), 1)
        self.assertTrue(
            any(
                "kaynak değişim zamanı belirsiz" in reason
                for reason in findings[0].reasons
            )
        )

    def test_noncanonical_note_dates_are_reported_as_broken(self) -> None:
        for value in ("20260911", "2026-W37-5"):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as temporary:
                    vault = Path(temporary)
                    note = _note(
                        vault,
                        "bozuk-tarih",
                        updated="2026-09-11",
                        sources=[],
                    )
                    note.write_text(
                        note.read_text(encoding="utf-8").replace(
                            "updated: 2026-09-11", f"updated: {value}"
                        ),
                        encoding="utf-8",
                    )

                    findings = stale_review.review(
                        vault, days=90, now=datetime.date(2026, 9, 11)
                    )

                self.assertEqual(findings[0].age_days, -1)
                self.assertEqual(
                    findings[0].reasons, ("tarih alanı yok ya da bozuk",)
                )

    def test_missing_source_field_is_reported_as_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "kaynaksiz", updated="2026-09-11", sources=[])
            findings = stale_review.review(
                vault, days=90, now=datetime.date(2026, 9, 11)
            )

        self.assertEqual(
            findings[0].reasons, ("kaynak alanı yok ya da bozuk",)
        )

    def test_scalar_source_field_is_reported_as_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = _daily(vault, "2026-01-01.md", mtime=datetime.date(2026, 1, 1))
            note = _note(
                vault,
                "skaler-kaynak",
                updated="2026-01-02",
                sources=["2026-01-01.md"],
            )
            note.write_text(
                note.read_text(encoding="utf-8").replace(
                    "sources:\n  - 2026-01-01.md",
                    "sources: 2026-01-01.md",
                ),
                encoding="utf-8",
            )

            findings = stale_review.review(
                vault, days=90, now=datetime.date(2026, 9, 11)
            )

        self.assertEqual(findings[0].note, "knowledge/concepts/skaler-kaynak.md")
        self.assertIn("kaynak alanı yok ya da bozuk", findings[0].reasons)

    def test_unreadable_daily_source_is_reported_as_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = _daily(vault, "2026-01-01.md", mtime=datetime.date(2026, 1, 1))
            _note(
                vault,
                "okunamayan-kaynak",
                updated="2026-01-02",
                sources=["2026-01-01.md"],
            )
            real_read = ledger.MemoryRead.read_source

            def deny_daily(memory, path, **kwargs):
                if path == daily:
                    raise PermissionError("daily source unreadable")
                return real_read(memory, path, **kwargs)

            with mock.patch.object(
                ledger.MemoryRead, "read_source", new=deny_daily
            ):
                target, count = stale_review.write_report(
                    vault, output=vault / "report.md", now=datetime.date(2026, 9, 11)
                )
                text = target.read_text(encoding="utf-8")

        self.assertEqual(count, 1)
        self.assertIn("kaynak okunamadı: 2026-01-01.md", text)
        self.assertNotIn("Bayat aday yok", text)

    def test_daily_source_drift_before_publish_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            daily = _daily(vault, "2026-09-01.md", mtime=datetime.date(2026, 9, 1))
            _note(
                vault,
                "kaynakli-not",
                updated="2026-09-01",
                sources=["2026-09-01.md"],
            )
            target = vault / "report.md"
            real_review = stale_review._review_notes

            def review_then_mutate(*args: object, **kwargs: object) -> list[stale_review.StaleFinding]:
                findings = real_review(*args, **kwargs)
                daily.write_text("günlük değişti", encoding="utf-8")
                stamp = datetime.datetime(2026, 9, 10, 12).timestamp()
                os.utime(daily, (stamp, stamp))
                return findings

            with mock.patch.object(
                stale_review,
                "_review_notes",
                new=review_then_mutate,
            ):
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError, "stale-review-source-changed"
                ):
                    stale_review.write_report(
                        vault, output=target, now=datetime.date(2026, 9, 11)
                    )
            self.assertFalse(target.exists())

    def test_missing_daily_source_created_before_publish_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(
                vault,
                "sonradan-gelen-kaynak",
                updated="2026-01-02",
                sources=["2026-01-01.md"],
            )
            target = vault / "report.md"
            real_review = stale_review._review_notes

            def review_then_create(*args: object, **kwargs: object) -> list[stale_review.StaleFinding]:
                findings = real_review(*args, **kwargs)
                _daily(vault, "2026-01-01.md", mtime=datetime.date(2026, 1, 1))
                return findings

            with mock.patch.object(
                stale_review,
                "_review_notes",
                new=review_then_create,
            ):
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError, "stale-review-source-changed"
                ):
                    stale_review.write_report(
                        vault, output=target, now=datetime.date(2026, 9, 11)
                    )
            self.assertFalse(target.exists())

    def test_review_revalidates_derived_note_before_return(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note = _note(vault, "degisen-not", updated="2026-01-02", sources=[])
            real_review = stale_review._review_notes

            def review_then_mutate(*args: object, **kwargs: object) -> list[stale_review.StaleFinding]:
                findings = real_review(*args, **kwargs)
                note.write_text(
                    note.read_text(encoding="utf-8") + "\nsonradan düzenlendi\n",
                    encoding="utf-8",
                )
                return findings

            with mock.patch.object(
                stale_review,
                "_review_notes",
                new=review_then_mutate,
            ):
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError, "stale-review-note-changed"
                ):
                    stale_review.review(vault, now=datetime.date(2026, 9, 11))

    def test_derived_note_drift_before_publish_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note = _note(vault, "degisen-not", updated="2026-01-02", sources=[])
            target = vault / "report.md"
            real_review = stale_review._review_notes

            def review_then_mutate(*args: object, **kwargs: object) -> list[stale_review.StaleFinding]:
                findings = real_review(*args, **kwargs)
                note.write_text(
                    note.read_text(encoding="utf-8") + "\nsonradan düzenlendi\n",
                    encoding="utf-8",
                )
                return findings

            with mock.patch.object(
                stale_review,
                "_review_notes",
                new=review_then_mutate,
            ):
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError, "stale-review-note-changed"
                ):
                    stale_review.write_report(
                        vault, output=target, now=datetime.date(2026, 9, 11)
                    )
            self.assertFalse(target.exists())

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

    def test_impossible_daily_source_date_is_reported_as_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _daily(vault, "2026-99-99.md", mtime=datetime.date(2026, 1, 1))
            _note(
                vault,
                "imkansiz-kaynak",
                updated="2026-01-02",
                sources=["2026-99-99.md"],
            )

            findings = stale_review.review(
                vault, days=90, now=datetime.date(2026, 9, 11)
            )

        self.assertEqual(
            findings[0].reasons,
            ("252 gündür güncellenmemiş", "kaynak yolu geçersiz"),
        )

    def test_source_date_after_note_is_reported_as_invalid_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _daily(vault, "2026-09-11.md", mtime=datetime.date(2026, 9, 9))
            _note(
                vault,
                "gelecek-kaynak",
                updated="2026-09-10",
                sources=["2026-09-11.md"],
            )

            findings = stale_review.review(
                vault, days=90, now=datetime.date(2026, 9, 11)
            )

        self.assertEqual(
            findings[0].reasons,
            ("kaynak tarihi nottan sonra: 2026-09-11.md (2026-09-11)",),
        )

    def test_missing_vault_fails_closed_before_creating_state_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "missing-vault"
            target = root / "report.md"
            with self.assertRaisesRegex(
                ledger.MemoryPreferenceError, "stale-review-vault-invalid"
            ):
                stale_review.write_report(vault, output=target)
            self.assertFalse(target.exists())
            self.assertFalse(vault.exists())

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

    def test_note_path_delimiters_are_not_rendered_as_wikilink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "x]] ![[Private.md", updated="2026-01-02", sources=[])
            target, _count = stale_review.write_report(
                vault, output=vault / "report.md", now=datetime.date(2026, 9, 11)
            )
            text = target.read_text(encoding="utf-8")

        self.assertNotIn("[[Private.md]]", text)
        self.assertIn(r"`knowledge/concepts/x\]\] !\[\[Private.md.md`", text)

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

    def test_default_report_write_does_not_clobber_existing_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "eski-not", updated="2026-01-02", sources=[])
            target = vault / stale_review.REPORT_RELATIVE
            original = b"# Kullanici paneli\r\nEk not\r\n"
            target.parent.mkdir(parents=True)
            target.write_bytes(original)

            with self.assertRaises(FileExistsError):
                stale_review.write_report(vault, now=datetime.date(2026, 9, 11))
            preserved = target.read_bytes()

        self.assertEqual(preserved, original)

    def test_default_report_rejects_linked_command_center_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside"
            vault.mkdir()
            outside.mkdir()
            parent = vault / stale_review.REPORT_RELATIVE.parent
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = outside / stale_review.REPORT_RELATIVE.name
            _note(vault, "eski-not", updated="2026-01-02", sources=[])
            try:
                with self.assertRaisesRegex(ValueError, "report-target-invalid"):
                    stale_review.write_report(vault, now=datetime.date(2026, 9, 11))
                self.assertFalse(target.exists())
            finally:
                parent.unlink(missing_ok=True)

    def test_linked_knowledge_entries_fail_closed_before_output(self) -> None:
        for relative in (
            Path("knowledge"),
            Path("knowledge") / "concepts",
            Path("knowledge") / "concepts" / "linked.md",
            Path("knowledge") / "connections" / "nested",
        ):
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    vault = root / "vault"
                    outside = root / "outside"
                    vault.mkdir()
                    if relative.suffix == ".md":
                        outside.write_text("dışarı", encoding="utf-8")
                    else:
                        outside.mkdir()
                    link = vault / relative
                    link.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        link.symlink_to(
                            outside, target_is_directory=relative.suffix != ".md"
                        )
                    except (OSError, NotImplementedError) as exc:
                        self.skipTest(f"symlink unavailable: {exc}")

                    target = vault / "report.md"
                    try:
                        with self.assertRaisesRegex(
                            ledger.MemoryPreferenceError,
                            "stale-review-knowledge-root-invalid",
                        ):
                            stale_review.write_report(vault, output=target)
                        self.assertFalse(target.exists())
                    finally:
                        link.unlink(missing_ok=True)

    def test_linked_suppression_controls_fail_closed_before_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-controls"
            vault.mkdir()
            outside.mkdir()
            (vault / "knowledge" / "concepts").mkdir(parents=True)
            controls = vault / ".codex" / "private-memory" / "controls"
            controls.parent.mkdir(parents=True)
            try:
                controls.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = vault / "report.md"
            try:
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError,
                    "stale-review-runtime-path-invalid",
                ):
                    stale_review.write_report(vault, output=target)
                self.assertFalse(target.exists())
            finally:
                controls.unlink(missing_ok=True)

    def test_linked_suppression_file_fails_closed_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-suppressions.jsonl"
            vault.mkdir()
            outside.write_text("external\n", encoding="utf-8")
            (vault / "knowledge" / "concepts").mkdir(parents=True)
            controls = vault / ".codex" / "private-memory" / "controls"
            controls.mkdir(parents=True)
            link = controls / "suppressions.jsonl"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = vault / "report.md"
            try:
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError,
                    "stale-review-runtime-path-invalid",
                ):
                    stale_review.write_report(vault, output=target)
                self.assertFalse(target.exists())
                self.assertEqual(outside.read_text(encoding="utf-8"), "external\n")
            finally:
                link.unlink(missing_ok=True)

    def test_linked_suppression_lock_fails_closed_before_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-suppressions.lock"
            vault.mkdir()
            outside.write_bytes(b"sentinel")
            (vault / "knowledge" / "concepts").mkdir(parents=True)
            controls = vault / ".codex" / "private-memory" / "controls"
            controls.mkdir(parents=True)
            link = controls / "suppressions.lock"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = vault / "report.md"
            try:
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError,
                    "stale-review-runtime-path-invalid",
                ):
                    stale_review.write_report(vault, output=target)
                self.assertFalse(target.exists())
                self.assertEqual(outside.read_bytes(), b"sentinel")
            finally:
                link.unlink(missing_ok=True)

    def test_linked_compile_lock_fails_closed_before_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-compile.lock"
            vault.mkdir()
            outside.write_bytes(b"sentinel")
            (vault / "knowledge" / "concepts").mkdir(parents=True)
            state_dir = vault / ".codex" / "scripts" / ".state"
            state_dir.mkdir(parents=True)
            link = state_dir / "compile.lock"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = vault / "report.md"
            try:
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError,
                    "stale-review-runtime-path-invalid",
                ):
                    stale_review.write_report(vault, output=target)
                self.assertFalse(target.exists())
                self.assertEqual(outside.read_bytes(), b"sentinel")
            finally:
                link.unlink(missing_ok=True)

    def test_custom_report_target_rejects_linked_parent_inside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-output"
            vault.mkdir()
            outside.mkdir()
            (vault / "knowledge" / "concepts").mkdir(parents=True)
            parent = vault / "custom-output"
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            target = parent / "report.md"
            try:
                with self.assertRaisesRegex(ValueError, "report-target-invalid"):
                    stale_review.write_report(vault, output=target)
                self.assertFalse((outside / "report.md").exists())
            finally:
                parent.unlink(missing_ok=True)

    def test_report_target_rejects_protected_roots_without_overwrite(self) -> None:
        for root_name in ("daily", "knowledge", ".codex"):
            with self.subTest(root_name=root_name):
                with tempfile.TemporaryDirectory() as temporary:
                    vault = Path(temporary)
                    _note(vault, "eski-not", updated="2026-01-02", sources=[])
                    parent = vault / root_name
                    parent.mkdir(parents=True, exist_ok=True)
                    target = parent / "report.md"
                    target.write_text("kullanici metni\n", encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, "report-target-invalid"):
                        stale_review.write_report(
                            vault, output=target, overwrite=True
                        )
                    self.assertEqual(
                        target.read_text(encoding="utf-8"), "kullanici metni\n"
                    )

    def test_external_alias_to_protected_root_is_rejected(self) -> None:
        for root_name in ("daily", "knowledge", ".codex"):
            with self.subTest(root_name=root_name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    vault = root / "vault"
                    vault.mkdir()
                    _note(vault, "eski-not", updated="2026-01-02", sources=[])
                    protected = vault / root_name
                    protected.mkdir(parents=True, exist_ok=True)
                    alias = root / "external-output"
                    try:
                        alias.symlink_to(protected, target_is_directory=True)
                    except (OSError, NotImplementedError) as exc:
                        self.skipTest(f"symlink unavailable: {exc}")

                    target = alias / "report.md"
                    target.write_text("kullanici metni\n", encoding="utf-8")
                    try:
                        with self.assertRaisesRegex(
                            ValueError, "report-target-invalid"
                        ):
                            stale_review.write_report(
                                vault, output=target, overwrite=True
                            )
                        self.assertEqual(
                            target.read_text(encoding="utf-8"),
                            "kullanici metni\n",
                        )
                    finally:
                        alias.unlink(missing_ok=True)

    def test_report_target_rejects_case_alias_to_protected_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "eski-not", updated="2026-01-02", sources=[])
            target = vault / "DAILY" / "report.md"

            with self.assertRaisesRegex(ValueError, "report-target-invalid"):
                stale_review.write_report(vault, output=target)
            self.assertFalse(target.exists())

    def test_report_parent_is_revalidated_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            outside = root / "outside-output"
            vault.mkdir()
            outside.mkdir()
            _note(vault, "eski-not", updated="2026-01-02", sources=[])
            parent = vault / "custom-output"
            parent.mkdir()
            target = parent / "report.md"
            real_render = stale_review.render

            def render_then_link(*args: object, **kwargs: object) -> str:
                parent.rmdir()
                try:
                    parent.symlink_to(outside, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    raise unittest.SkipTest(f"symlink unavailable: {exc}") from exc
                return real_render(*args, **kwargs)

            with mock.patch.object(
                stale_review, "render", side_effect=render_then_link
            ):
                with self.assertRaisesRegex(ValueError, "report-target-invalid"):
                    stale_review.write_report(vault, output=target)
            self.assertFalse((outside / "report.md").exists())
            parent.unlink(missing_ok=True)

    def test_report_stays_unwritten_when_compile_lock_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "eski-not", updated="2026-01-02", sources=[])
            target = vault / "report.md"
            with mock.patch.object(
                stale_review, "locked", side_effect=LockUnavailable("lock-busy")
            ):
                with self.assertRaises(LockUnavailable):
                    stale_review.write_report(vault, output=target)
            self.assertFalse(target.exists())

    def test_compile_lock_covers_atomic_write_and_is_released_after_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "eski-not", updated="2026-01-02", sources=[])
            target = vault / "report.md"
            state_dir = state_dir_of(vault)
            probe_result: list[str] = []

            def probe_compile_lock() -> None:
                try:
                    with file_locked(state_dir / "compile", timeout=0):
                        probe_result.append("acquired")
                except LockUnavailable:
                    probe_result.append("busy")

            def observe_writer(_path: Path, _text: str, **_kwargs: object) -> None:
                probe = threading.Thread(target=probe_compile_lock)
                probe.start()
                probe.join(timeout=2)
                self.assertFalse(probe.is_alive())
                self.assertEqual(probe_result, ["busy"])

            with mock.patch.object(
                stale_review, "atomic_write_text", side_effect=observe_writer
            ):
                result = stale_review.write_report(
                    vault, output=target, now=datetime.date(2026, 9, 11)
                )
            self.assertEqual(result, (target, 1))
            self.assertFalse(target.exists())
            with file_locked(state_dir / "compile", timeout=0):
                pass

    def test_report_text_is_rendered_before_final_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "eski-not", updated="2026-01-02", sources=[])
            order: list[str] = []
            real_render = stale_review.render
            real_validate = stale_review._validate_note_observations

            def observe_render(*args: object, **kwargs: object) -> str:
                order.append("render")
                return real_render(*args, **kwargs)

            def observe_validate(*args: object, **kwargs: object) -> None:
                order.append("validate")
                real_validate(*args, **kwargs)

            with (
                mock.patch.object(stale_review, "render", side_effect=observe_render),
                mock.patch.object(
                    stale_review,
                    "_validate_note_observations",
                    side_effect=observe_validate,
                ),
            ):
                stale_review.write_report(
                    vault, output=vault / "report.md", now=datetime.date(2026, 9, 11)
                )

        self.assertEqual(order[0], "render")
        self.assertIn("validate", order)
        self.assertLess(order.index("render"), order.index("validate"))

    def test_cli_overwrite_replaces_existing_report_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _note(vault, "eski-not", updated="2026-01-02", sources=[])
            target = vault / "report.md"
            target.write_text("kullanici notu\n", encoding="utf-8")

            with mock.patch("builtins.print"):
                result = stale_review.main(
                    ["--vault", str(vault), "--output", str(target), "--overwrite"]
                )
            text = target.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertIn("Cevo Bayat İnceleme", text)
        self.assertNotIn("kullanici notu", text)

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

    def test_suppressed_daily_source_is_not_reemitted_in_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _daily(vault, "2026-01-02.md", mtime=datetime.date(2026, 8, 1))
            _note(
                vault,
                "kaynakli-not",
                updated="2026-01-03",
                sources=["2026-01-02.md"],
            )
            ledger.suppress_derived_memory(
                vault / ".codex/private-memory", "daily/2026-01-02.md"
            )

            target, _count = stale_review.write_report(
                vault, now=datetime.date(2026, 9, 11)
            )
            text = target.read_text(encoding="utf-8")

        self.assertNotIn("2026-01-02.md", text)
        self.assertIn("kaynak güveni doğrulanamadı", text)

    def test_malformed_suppression_controls_fail_closed_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "knowledge" / "concepts").mkdir(parents=True)
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

    def test_unreadable_eligible_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            path = _note(vault, "bozuk-not", updated="2026-01-02", sources=[])
            path.write_bytes(b"\xff")
            target = vault / "report.md"

            with self.assertRaises(UnicodeDecodeError):
                stale_review.write_report(vault, output=target)
            self.assertFalse(target.exists())

    def test_inventory_identity_mismatch_fails_closed_after_symlink_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            target = _note(vault, "ilk-not", updated="2026-01-02", sources=[])
            other = _note(vault, "baska-not", updated="2026-01-03", sources=[])
            real_read = ledger.MemoryRead.read_source

            def replace_with_link(memory, path, **kwargs):
                if path == target:
                    target.unlink()
                    target.symlink_to(other)
                return real_read(memory, path, **kwargs)

            with mock.patch.object(
                ledger.MemoryRead, "read_source", new=replace_with_link
            ):
                with self.assertRaisesRegex(
                    ledger.MemoryPreferenceError, "stale-review-source-identity"
                ):
                    stale_review.review(vault, now=datetime.date(2026, 9, 11))

    def test_source_path_escape_is_not_statted_or_echoed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            outside = root / "outside-secret.md"
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
