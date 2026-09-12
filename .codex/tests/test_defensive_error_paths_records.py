"""Daily kayıt zinciri ve intake sözleşmesinin savunma dallarını sürer.

%100 kapsam sözleşmesinin ikinci dilimi: bozuk makbuzlar, kaçan yollar,
sürüklenmiş imzalar ve intake ihlalleri gerçek dosyalarla üretilir.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from _fixtures import CODEX_DIR  # noqa: F401
import daily_store
import file_lock
import intake_contract
from memory_ledger import memory_text_hash


KEY = "a" * 64
NOW = dt.datetime(2026, 9, 11, 10, 0)


def _junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DailyReceiptGuards(unittest.TestCase):
    def _receipt(self, tmp: Path, payload) -> Path:
        path = tmp / "r.json"
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )
        return path

    def test_read_receipt_rejects_malformed_payloads(self) -> None:
        cases = [
            "{bozuk",
            {"schema_version": 9},
            {"schema_version": 1, "after_generation": 0},
            {"schema_version": 1, "after_generation": 2, "previous_after_generation": 2},
            {"schema_version": 1, "previous_after_sha256": "KISA"},
            {"schema_version": 2, "status": "prepared"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    path = self._receipt(Path(temporary), payload)
                    with self.assertRaises(ValueError):
                        daily_store._read_receipt(path)

    def test_daily_path_rejects_bad_fields_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            with self.assertRaises(ValueError):
                daily_store._daily_path(vault, {"date": 5, "daily_relative": "x"})
            with self.assertRaises(ValueError):
                daily_store._daily_path(
                    vault,
                    {"date": "2026-09-11", "daily_relative": "daily/../kacak.md"},
                )
            # daily bir junction'sa çözümlenen yol beyan edilen yoldan ayrılır.
            outside = vault / "dis"
            outside.mkdir()
            _junction(vault / "daily", outside)
            with self.assertRaises(ValueError):
                daily_store._daily_path(
                    vault,
                    {"date": "2026-09-11", "daily_relative": "daily/2026-09-11.md"},
                )

    def test_after_path_rejects_bad_id_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            operation_dir = Path(temporary)
            with self.assertRaises(ValueError):
                daily_store._after_path(operation_dir, "KISA", {})
            with self.assertRaises(ValueError):
                daily_store._after_path(operation_dir, KEY, {"after_generation": 0})

    def test_previous_after_path_derives_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            operation_dir = Path(temporary)
            committed = daily_store._previous_after_path(
                operation_dir, KEY, {"after_generation": 1}
            )
            self.assertTrue(committed.name.endswith(".after.md"))
            numbered = daily_store._previous_after_path(
                operation_dir, KEY, {"after_generation": 3}
            )
            self.assertTrue(numbered.name.endswith(".after.2.md"))

    def test_after_path_rejects_symlink_to_external_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operation_dir = root / "operations"
            operation_dir.mkdir()
            external = root / "external.md"
            external.write_bytes(b"user content")
            image = operation_dir / f"{KEY}.after.md"
            image.symlink_to(external)

            with self.assertRaisesRegex(ValueError, "daily-operation-path-invalid"):
                daily_store._after_path(operation_dir, KEY, {})
            self.assertEqual(external.read_bytes(), b"user content")

    def test_checked_compact_values_rejects_drifted_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            after_path = Path(temporary) / "after.md"
            with self.assertRaises(ValueError):
                daily_store._checked_compact_values(
                    {"schema_version": 2, "status": "committed", "after_size": 0,
                     "after_sha256": KEY},
                    after_path,
                    b"icerik",
                )
            current = b"govde"
            after_path.write_bytes(b"farkli")
            with self.assertRaises(ValueError):
                daily_store._checked_compact_values(
                    {"schema_version": 2, "status": "committed",
                     "after_size": len(current), "after_sha256": _sha(current)},
                    after_path,
                    current,
                )


class DailyCompactGuards(unittest.TestCase):
    def test_operations_junction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            outside = root / "dis"
            outside.mkdir()
            _junction(state / "daily-operations", outside)
            with self.assertRaises(ValueError):
                daily_store.compact_completed(root / "vault", state, apply=True)

    def test_invalid_entries_are_counted_as_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            operations = state / "daily-operations"
            operations.mkdir(parents=True)
            (operations / "kisa-ad.json").write_text("{}", encoding="utf-8")
            mismatched = "b" * 64
            (operations / f"{mismatched}.json").write_text(
                json.dumps({"schema_version": 1, "operation_id": "c" * 64,
                            "status": "committed"}),
                encoding="utf-8",
            )
            zombie = "d" * 64
            (operations / f"{zombie}.json").write_text(
                json.dumps({"schema_version": 1, "operation_id": zombie,
                            "status": "zombi"}),
                encoding="utf-8",
            )
            report = daily_store.compact_completed(root / "vault", state, apply=True)
        self.assertEqual(len(report["blocked"]), 3)

    def test_held_lock_counts_as_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            operations = state / "daily-operations"
            operations.mkdir(parents=True)
            busy_id = "e" * 64
            (operations / f"{busy_id}.json").write_text(
                json.dumps({"schema_version": 1, "operation_id": busy_id,
                            "status": "committed"}),
                encoding="utf-8",
            )
            with file_lock.locked(state / f"daily-operation-{busy_id}"):
                report = daily_store.compact_completed(
                    root / "vault", state, apply=True
                )
        self.assertEqual(report["busy"], 1)


class DailyPublishGuards(unittest.TestCase):
    def _vault(self, temporary: Path) -> tuple[Path, Path]:
        vault = temporary / "vault"
        state = temporary / "state"
        (vault / "daily").mkdir(parents=True)
        state.mkdir()
        return vault, state

    def test_argument_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            cases = [
                ("Özet", "bozuk-neden", "flush", KEY),
                ("Özet", "turnend", "BÜYÜK", KEY),
                ("Özet", "turnend", "flush", "kisa"),
                ("", "turnend", "flush", KEY),
            ]
            for summary, reason, namespace, key in cases:
                with self.subTest(reason=reason, namespace=namespace):
                    with self.assertRaises(ValueError):
                        daily_store.publish(
                            vault, state, summary, reason, NOW,
                            idempotency_key=key, marker_namespace=namespace,
                        )

    def test_fully_suppressed_summary_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            summary = "Gizli karar."
            # suppression_guard diske bakar: bastırmayı gerçekten kaydet.
            from memory_ledger import suppress_derived_memory
            suppress_derived_memory(vault / ".codex/private-memory", summary)
            published = daily_store.publish(
                vault, state, summary, "turnend", NOW, idempotency_key=KEY,
            )
            self.assertFalse(published)
            self.assertFalse((vault / "daily" / "2026-09-11.md").exists())

    def test_operations_junction_blocks_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            outside = Path(temporary) / "dis"
            outside.mkdir()
            _junction(state / "daily-operations", outside)
            with self.assertRaises(ValueError):
                daily_store.publish(
                    vault, state, "Özet", "turnend", NOW, idempotency_key=KEY
                )

    def test_daily_junction_is_rejected_before_external_content_is_staged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault, state = self._vault(root)
            outside = root / "dis"
            outside.mkdir()
            external = outside / "2026-09-11.md"
            external.write_text(
                "# Günlük Log: 2026-09-11\n\nexternal daily\n",
                encoding="utf-8",
            )
            (vault / "daily").rmdir()
            _junction(vault / "daily", outside)

            with self.assertRaisesRegex(ValueError, "daily-operation-path-invalid"):
                daily_store.publish(
                    vault, state, "Özet", "turnend", NOW, idempotency_key=KEY
                )

            self.assertEqual(
                external.read_text(encoding="utf-8"),
                "# Günlük Log: 2026-09-11\n\nexternal daily\n",
            )
            staged = [
                path.read_bytes()
                for path in state.rglob("*")
                if path.is_file() and path.suffix in {".json", ".md"}
            ]
            self.assertEqual(staged, [])

    def test_receipt_with_foreign_operation_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            operation_id = daily_store._operation_id("flush", KEY)
            operations = state / "daily-operations"
            operations.mkdir()
            (operations / f"{operation_id}.json").write_text(
                json.dumps({"schema_version": 1, "operation_id": "f" * 64,
                            "status": "prepared"}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                daily_store.publish(
                    vault, state, "Özet", "turnend", NOW, idempotency_key=KEY
                )

    def test_marker_prefix_in_base_without_receipt_is_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            (vault / "daily" / "2026-09-11.md").write_text(
                f"# Günlük Log: 2026-09-11\n\n<!-- flush:{KEY}:{'9'*64} -->\neski\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                daily_store.publish(
                    vault, state, "Yeni özet", "turnend", NOW, idempotency_key=KEY
                )

    def _prepared(self, vault: Path, state: Path) -> tuple[Path, Path, dict]:
        with self.assertRaises(RuntimeError):
            daily_store.publish(
                vault, state, "Özet gövdesi.", "turnend", NOW,
                idempotency_key=KEY, _fail_after="prepared",
            )
        operation_id = daily_store._operation_id("flush", KEY)
        operations = state / "daily-operations"
        receipt_path = operations / f"{operation_id}.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return receipt_path, operations / f"{operation_id}.after.md", receipt

    def test_unknown_receipt_status_blocks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            receipt_path, _after, receipt = self._prepared(vault, state)
            receipt["status"] = "zombi"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaises(ValueError):
                daily_store.publish(
                    vault, state, "Özet gövdesi.", "turnend", NOW, idempotency_key=KEY
                )

    def test_tampered_after_image_blocks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            _receipt_path, after_path, _receipt = self._prepared(vault, state)
            after_path.write_text("kurcalandı", encoding="utf-8")
            with self.assertRaises(ValueError):
                daily_store.publish(
                    vault, state, "Özet gövdesi.", "turnend", NOW, idempotency_key=KEY
                )

    def test_marker_prefix_drift_in_current_daily_blocks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            self._prepared(vault, state)
            (vault / "daily" / "2026-09-11.md").write_text(
                f"# Günlük Log: 2026-09-11\n\n<!-- flush:{KEY}:{'9'*64} -->\nbaşka\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                daily_store.publish(
                    vault, state, "Özet gövdesi.", "turnend", NOW, idempotency_key=KEY
                )

    def test_after_image_without_marker_blocks_rebase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            receipt_path, after_path, receipt = self._prepared(vault, state)
            after_path.write_text("işaretsiz görüntü", encoding="utf-8")
            tampered = after_path.read_bytes()
            receipt["after_image_sha256"] = _sha(tampered)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            (vault / "daily" / "2026-09-11.md").write_text(
                "# Günlük Log: 2026-09-11\n\nbambaşka içerik\n", encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                daily_store.publish(
                    vault, state, "Özet gövdesi.", "turnend", NOW, idempotency_key=KEY
                )


class IntakeContractEdges(unittest.TestCase):
    def _vault(self, temporary: Path) -> Path:
        vault = temporary / "vault"
        (vault / "🧠 500-Knowledge").mkdir(parents=True)
        return vault

    def test_non_markdown_paths_are_not_validated(self) -> None:
        self.assertFalse(intake_contract.should_validate_path(Path("🧠 500-Knowledge/a.txt")))

    def test_tag_values_coerces_scalars(self) -> None:
        self.assertEqual(intake_contract._tag_values(["a"]), ("a",))
        self.assertEqual(intake_contract._tag_values("tek"), ("tek",))
        self.assertEqual(intake_contract._tag_values(""), ())

    def test_validate_note_reports_structural_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            note = vault / "🧠 500-Knowledge" / "not.md"

            self.assertEqual(
                intake_contract.validate_note(vault, note, "frontmattersız"),
                ("frontmatter-missing",),
            )

            text = (
                "---\ntitle: Not\ncreated: 2026-09-11\ntype: uzaylı\n"
                "status: active\ntags: tek-etiket\n---\niçerik"
            )
            issues = intake_contract.validate_note(vault, note, text)
            self.assertIn("tags-empty-or-invalid", issues)
            self.assertIn("intake-type-unknown:uzaylı", issues)
            self.assertTrue(any(issue.startswith("taxonomy-unreadable:") for issue in issues))

            codex_dir = vault / ".codex"
            codex_dir.mkdir(exist_ok=True)
            (codex_dir / "tag-taxonomy.json").write_bytes(bytes([255, 254, 250]))
            unicode_issues = intake_contract.validate_note(vault, note, text)
            self.assertIn("taxonomy-unreadable:UnicodeDecodeError", unicode_issues)

            missing_field = "---\ntitle: Not\n---\niçerik"
            self.assertIn(
                "frontmatter-field-missing:created",
                intake_contract.validate_note(vault, note, missing_field),
            )

    def test_validate_note_reports_type_specific_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            note = vault / "🧠 500-Knowledge" / "kaynak.md"
            source = (
                "---\ntitle: K\ncreated: 2026-09-11\ntype: source-note\n"
                "status: active\ntags:\n  - x\ndedupe_key: k\n---\n"
                "Üst kayıt: [[🧠 500-Knowledge/İndeks]]\n"
            )
            self.assertIn(
                "source-type-missing",
                intake_contract.validate_note(vault, note, source),
            )

            project = vault / "🏰 300-Projects" / "proje.md"
            marker = (
                "---\ntitle: P\ncreated: 2026-09-11\ntype: project-marker\n"
                "status: active\ntags:\n  - x\ncanonical_link: '[[a]]'\n---\n"
                "Üst kayıt: [[🏰 300-Projects/Projeler]]\n"
            )
            issues = intake_contract.validate_note(vault, project, marker)
            self.assertIn("project-last-decision-summary-missing", issues)
            self.assertIn("project-mirroring-decision-missing", issues)

    def test_validate_note_reports_image_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            note = vault / "🧠 500-Knowledge" / "resimli.md"
            text = (
                "---\ntitle: R\ncreated: 2026-09-11\ntype: knowledge-note\n"
                "status: active\ntags:\n  - x\n---\n"
                "Üst kayıt: [[🧠 500-Knowledge/İndeks]]\n"
                "![bir](Assets/klasor-a/im1.png)\n"
                "![iki](Assets/klasor-b/im2.png)\n"
                "## Görselde verilmeyenler\n"
            )
            issues = intake_contract.validate_note(vault, note, text)
            self.assertIn("image-record-type-invalid", issues)
            self.assertIn("image-assets-folder-split", issues)
            self.assertIn("image-observed-section-missing", issues)

    def test_git_new_paths_requires_repo_and_lists_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plain = Path(temporary) / "duz"
            plain.mkdir()
            with self.assertRaises(ValueError):
                intake_contract._git_new_paths(plain)

            repo = Path(temporary) / "repo"
            repo.mkdir()
            for command in (
                ["init", "-b", "main"],
                ["config", "user.name", "t"],
                ["config", "user.email", "t@example.invalid"],
                ["commit", "--allow-empty", "-m", "ilk"],
            ):
                subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
            (repo / "yeni.md").write_text("x", encoding="utf-8")
            paths = intake_contract._git_new_paths(repo)
            self.assertEqual([path.as_posix() for path in paths], ["yeni.md"])

    def test_main_reports_violations_and_clean_skips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self._vault(Path(temporary))
            bad = vault / "🧠 500-Knowledge" / "bozuk.md"
            bad.write_text("frontmattersız", encoding="utf-8")

            self.assertEqual(
                intake_contract.main(
                    ["🧠 500-Knowledge/bozuk.md", "🧠 500-Knowledge/yok.md", "daily/atla.md"],
                    vault_root=vault,
                ),
                1,
            )
            self.assertEqual(
                intake_contract.main(["daily/atla.md"], vault_root=vault), 0
            )


if __name__ == "__main__":
    unittest.main()
