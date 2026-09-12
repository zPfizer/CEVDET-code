from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401

import compile as compiler
import compile_state
import memory_ledger
import state_store


class PublicationFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / ".codex" / "scripts" / ".state"
        self.knowledge = root / "knowledge"
        self.source = root / "daily" / "2026-09-08.md"
        self.state.mkdir(parents=True)
        (self.knowledge / "concepts").mkdir(parents=True)
        (self.knowledge / "connections").mkdir()
        self.source.parent.mkdir()
        self.source.write_text("Kaynak kararı.\n", encoding="utf-8")
        (self.knowledge / "index.md").write_text("eski indeks\n", encoding="utf-8")
        (self.knowledge / "log.md").write_text("eski günlük\n", encoding="utf-8")
        self.stage = self.state / "compile-stage-publication"
        (self.stage / "daily").mkdir(parents=True)
        (self.stage / "knowledge").mkdir()
        (self.stage / "daily" / self.source.name).write_bytes(self.source.read_bytes())
        (self.stage / "knowledge" / "index.md").write_text("yeni indeks\n", encoding="utf-8")
        (self.stage / "knowledge" / "log.md").write_text("yeni günlük\n", encoding="utf-8")
        self.changed = ["knowledge/index.md", "knowledge/log.md"]
        self.baseline = {
            relative: compiler._sha256(self.root / relative)
            for relative in self.changed
        }

    def promote(self) -> None:
        compiler._promote_changes(
            self.stage,
            self.root,
            self.changed,
            dict(self.baseline),
            state_dir=self.state,
            source_relative=f"daily/{self.source.name}",
            source_digest=hashlib.sha256(self.source.read_bytes()).hexdigest(),
            source_size=self.source.stat().st_size,
            timestamp="2026-09-08T12:00:00+03:00",
            suppression_digest=compiler._suppression_digest(frozenset()),
        )


class PublicationRecoveryTests(unittest.TestCase):
    def test_source_snapshot_match_rejects_invalid_or_huge_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "daily.md"
            source.write_bytes(b"snapshot")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            for size in (-1, True, 2**100, source.stat().st_size + 1):
                with self.subTest(size=size):
                    self.assertFalse(compiler._source_snapshot_matches(source, digest, size))
            self.assertFalse(
                compiler._source_snapshot_matches(source, "0" * 64, source.stat().st_size)
            )

    def test_publication_journal_json_is_bounded_and_schema_types_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = compile_state.publication_file(state)
            path.parent.mkdir(parents=True, exist_ok=True)
            for status in ([], {}):
                path.write_text(
                    json.dumps({"schema_version": 1, "status": status}),
                    encoding="utf-8",
                )
                with self.subTest(status=status):
                    with self.assertRaisesRegex(
                        compile_state.PolicyError,
                        "publication-journal-invalid",
                    ):
                        compile_state.load_publication(state)
            oversized = {"schema_version": 1, "status": "pending", "payload": "x" * (4 * 1024 * 1024)}
            path.write_text(json.dumps(oversized), encoding="utf-8")
            with self.assertRaisesRegex(compile_state.PolicyError, "publication-journal-too-large"):
                compile_state.load_publication(state)
            path.unlink()
            with self.assertRaisesRegex(compile_state.PolicyError, "publication-journal-too-large"):
                compile_state.save_publication(state, oversized)
            self.assertFalse(path.exists())

    def test_second_target_failure_keeps_journal_stage_and_cursor_old(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            real_copy = compiler._atomic_copy

            def fail_second(*args: object, **kwargs: object) -> None:
                fail_second.calls += 1
                if fail_second.calls == 2:
                    raise OSError("injected-second-target-failure")
                real_copy(*args, **kwargs)

            fail_second.calls = 0
            with mock.patch.object(compiler, "_atomic_copy", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "second-target"):
                    fixture.promote()

            journal = compile_state.load_publication(fixture.state)
            self.assertIsNotNone(journal)
            self.assertEqual(journal["status"], "pending")
            self.assertEqual(journal["source_size"], len(fixture.source.read_bytes()))
            self.assertEqual(
                (fixture.knowledge / "index.md").read_text(encoding="utf-8"),
                "yeni indeks\n",
            )
            self.assertEqual(
                (fixture.knowledge / "log.md").read_text(encoding="utf-8"),
                "eski günlük\n",
            )
            self.assertTrue(fixture.stage.is_dir())
            self.assertEqual(compile_state.load(fixture.state).ingested, {})
            with self.assertRaisesRegex(compile_state.PolicyError, "publication-pending"):
                compile_state.assert_publication_ready(fixture.root)

    def test_append_before_partial_failure_replays_snapshot_and_leaves_append_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            original = fixture.source.read_bytes()
            real_copy = compiler._atomic_copy

            def fail_second(*args: object, **kwargs: object) -> None:
                fail_second.calls += 1
                if fail_second.calls == 2:
                    fixture.source.write_bytes(original + "ek katkı.\n".encode("utf-8"))
                    raise OSError("injected-second-target-failure")
                real_copy(*args, **kwargs)

            fail_second.calls = 0
            with mock.patch.object(compiler, "_atomic_copy", side_effect=fail_second):
                with self.assertRaises(OSError):
                    fixture.promote()

            journal = compiler._recover_pending_publication(fixture.root, fixture.state)
            state = compile_state.CompileState()
            compiler._apply_recovered_publication(state, journal)
            self.assertEqual(state.ingested[fixture.source.name], journal["source_digest"])
            self.assertEqual(
                fixture.source.read_bytes(),
                original + "ek katkı.\n".encode("utf-8"),
            )
            self.assertEqual(
                (fixture.knowledge / "log.md").read_text(encoding="utf-8"),
                "yeni günlük\n",
            )

    def test_append_after_complete_journal_is_accepted_before_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            fixture.promote()
            journal = compile_state.load_publication(fixture.state)
            snapshot_digest = journal["source_digest"]
            fixture.source.write_bytes(
                fixture.source.read_bytes() + "son katkı.\n".encode("utf-8")
            )

            recovered = compiler._recover_pending_publication(fixture.root, fixture.state)
            state = compile_state.CompileState()
            compiler._apply_recovered_publication(state, recovered)
            self.assertEqual(state.ingested[fixture.source.name], snapshot_digest)
            compiler._finalize_publication(fixture.state)
            self.assertIsNone(compile_state.load_publication(fixture.state))

    def test_source_overwrite_or_truncation_blocks_stale_replay(self) -> None:
        for mutation in ("overwrite", "truncate"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                fixture = PublicationFixture(Path(temporary))
                original = fixture.source.read_bytes()
                real_copy = compiler._atomic_copy

                def fail_second(*args: object, **kwargs: object) -> None:
                    fail_second.calls += 1
                    if fail_second.calls == 2:
                        raise OSError("injected-second-target-failure")
                    real_copy(*args, **kwargs)

                fail_second.calls = 0
                with mock.patch.object(compiler, "_atomic_copy", side_effect=fail_second):
                    with self.assertRaises(OSError):
                        fixture.promote()
                fixture.source.write_bytes(
                    (b"X" + original[1:]) if mutation == "overwrite" else original[:2]
                )
                with self.assertRaisesRegex(
                    compile_state.PolicyError,
                    "publication-source-changed",
                ):
                    compiler._recover_pending_publication(fixture.root, fixture.state)
                self.assertTrue(compile_state.publication_file(fixture.state).is_file())

    def test_recovery_is_idempotent_and_run_advances_cursor_after_full_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            real_copy = compiler._atomic_copy

            def crash_after_first(*args: object, **kwargs: object) -> None:
                real_copy(*args, **kwargs)
                if crash_after_first.calls == 0:
                    crash_after_first.calls += 1
                    raise RuntimeError("injected-publication-crash")

            crash_after_first.calls = 0
            with mock.patch.object(compiler, "_atomic_copy", side_effect=crash_after_first):
                with self.assertRaisesRegex(RuntimeError, "publication-crash"):
                    fixture.promote()

            first = compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertEqual(first["status"], "complete")
            second = compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertEqual(second["status"], "complete")
            with mock.patch.object(compiler, "_checkpoint_machine_outputs"):
                self.assertFalse(
                    compiler._run_locked(
                        fixture.root,
                        fixture.state,
                        dry_run=False,
                        max_calls=1,
                        trigger_claim=None,
                    )
                )

            state = compile_state.load(fixture.state)
            self.assertEqual(state.cursor, fixture.source.name)
            self.assertEqual(state.ingested[fixture.source.name], compiler._sha256(fixture.source))
            self.assertIsNone(compile_state.load_publication(fixture.state))
            self.assertFalse(fixture.stage.exists())
            self.assertEqual(
                (fixture.knowledge / "index.md").read_text(encoding="utf-8"),
                "yeni indeks\n",
            )
            self.assertEqual(
                (fixture.knowledge / "log.md").read_text(encoding="utf-8"),
                "yeni günlük\n",
            )

    def test_posix_recovery_keeps_legacy_after_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            real_copy = compiler._atomic_copy

            def posix_replace(source: Path, destination: Path, **_: object) -> None:
                os.replace(source, destination)

            def crash_after_copy(*args: object, **kwargs: object) -> None:
                real_copy(*args, **kwargs)
                raise RuntimeError("injected-posix-publication-crash")

            with mock.patch.object(compiler, "replace_with_retry", side_effect=posix_replace):
                with mock.patch.object(compiler, "_atomic_copy", side_effect=crash_after_copy):
                    with self.assertRaisesRegex(RuntimeError, "posix-publication-crash"):
                        fixture.promote()

            journal = compile_state.load_publication(fixture.state)
            self.assertFalse(journal["targets"][0]["completed"])
            self.assertFalse((fixture.root / journal["targets"][0]["backup_relative"]).exists())
            class PosixOsView:
                name = "posix"

                def __getattr__(self, name: str) -> object:
                    return getattr(os, name)

            with mock.patch.object(compiler, "os", PosixOsView()):
                recovered = compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertEqual(recovered["status"], "complete")
            self.assertTrue(recovered["targets"][0]["completed"])

    def test_recovery_accepts_crash_after_new_file_no_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            relative = "knowledge/concepts/new.md"
            staged = fixture.stage / relative
            staged.parent.mkdir()
            staged.write_text("yeni dosya\n", encoding="utf-8")
            destination = fixture.root / relative
            real_copy = compiler._atomic_copy

            def crash_after_copy(*args: object, **kwargs: object) -> None:
                real_copy(*args, **kwargs)
                raise RuntimeError("injected-new-file-publication-crash")

            with mock.patch.object(compiler, "_atomic_copy", side_effect=crash_after_copy):
                with self.assertRaisesRegex(RuntimeError, "new-file-publication-crash"):
                    compiler._promote_changes(
                        fixture.stage,
                        fixture.root,
                        [relative],
                        {relative: None},
                        state_dir=fixture.state,
                        source_relative=f"daily/{fixture.source.name}",
                        source_digest=hashlib.sha256(fixture.source.read_bytes()).hexdigest(),
                        source_size=fixture.source.stat().st_size,
                        timestamp="2026-09-08T12:00:00+03:00",
                        suppression_digest=compiler._suppression_digest(frozenset()),
                    )

            journal = compile_state.load_publication(fixture.state)
            self.assertIsNotNone(journal)
            target = journal["targets"][0]
            backup = fixture.root / target["backup_relative"]
            self.assertTrue(destination.is_file())
            self.assertTrue(os.path.samefile(backup, destination))
            self.assertFalse(os.path.samefile(staged, destination))

            recovered = compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertEqual(recovered["status"], "complete")
            self.assertTrue(recovered["targets"][0]["completed"])
            self.assertFalse(backup.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "yeni dosya\n")

    def test_recovery_rejects_same_hash_external_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            relative = "knowledge/concepts/new.md"
            staged = fixture.stage / relative
            staged.parent.mkdir()
            staged.write_text("yeni dosya\n", encoding="utf-8")
            destination = fixture.root / relative
            real_copy = compiler._atomic_copy

            def crash_after_copy(*args: object, **kwargs: object) -> None:
                real_copy(*args, **kwargs)
                raise RuntimeError("injected-new-file-publication-crash")

            with mock.patch.object(compiler, "_atomic_copy", side_effect=crash_after_copy):
                with self.assertRaises(RuntimeError):
                    compiler._promote_changes(
                        fixture.stage,
                        fixture.root,
                        [relative],
                        {relative: None},
                        state_dir=fixture.state,
                        source_relative=f"daily/{fixture.source.name}",
                        source_digest=hashlib.sha256(fixture.source.read_bytes()).hexdigest(),
                        source_size=fixture.source.stat().st_size,
                        timestamp="2026-09-08T12:00:00+03:00",
                        suppression_digest=compiler._suppression_digest(frozenset()),
                    )

            journal = compile_state.load_publication(fixture.state)
            backup = fixture.root / journal["targets"][0]["backup_relative"]
            replacement = fixture.root / "external-same.md"
            replacement.write_bytes(destination.read_bytes())
            os.replace(replacement, destination)

            with self.assertRaisesRegex(compile_state.PolicyError, "publication-target-changed"):
                compiler._recover_pending_publication(fixture.root, fixture.state)
            journal = compile_state.load_publication(fixture.state)
            self.assertTrue(journal["targets"][0]["conflict"])
            self.assertTrue(backup.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "yeni dosya\n")

    def test_recovery_rejects_delete_after_new_file_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            relative = "knowledge/concepts/new.md"
            staged = fixture.stage / relative
            staged.parent.mkdir()
            staged.write_text("yeni dosya\n", encoding="utf-8")
            destination = fixture.root / relative
            real_copy = compiler._atomic_copy

            def crash_after_copy(*args: object, **kwargs: object) -> None:
                real_copy(*args, **kwargs)
                raise RuntimeError("injected-new-file-publication-crash")

            with mock.patch.object(compiler, "_atomic_copy", side_effect=crash_after_copy):
                with self.assertRaises(RuntimeError):
                    compiler._promote_changes(
                        fixture.stage,
                        fixture.root,
                        [relative],
                        {relative: None},
                        state_dir=fixture.state,
                        source_relative=f"daily/{fixture.source.name}",
                        source_digest=hashlib.sha256(fixture.source.read_bytes()).hexdigest(),
                        source_size=fixture.source.stat().st_size,
                        timestamp="2026-09-08T12:00:00+03:00",
                        suppression_digest=compiler._suppression_digest(frozenset()),
                    )

            journal = compile_state.load_publication(fixture.state)
            target = journal["targets"][0]
            backup = fixture.root / target["backup_relative"]
            destination.unlink()
            self.assertTrue(backup.exists())

            with self.assertRaisesRegex(
                compile_state.PolicyError,
                "publication-conflict-backup-exists",
            ):
                compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertFalse(destination.exists())
            self.assertTrue(backup.exists())

    def test_recovery_keeps_same_hash_replaced_temporary_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            relative = "knowledge/concepts/new.md"
            staged = fixture.stage / relative
            staged.parent.mkdir()
            staged.write_text("yeni dosya\n", encoding="utf-8")
            destination = fixture.root / relative
            real_publish = state_store.os.rename if os.name == "nt" else state_store.os.link
            calls = 0

            def fail_before_publish(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if (
                    os.name == "nt" and calls == 1
                ) or (
                    os.name != "nt" and Path(target) == destination
                ):
                    raise OSError("injected-pre-rename-failure")
                real_publish(source, target)

            publish_patch = mock.patch.object(
                state_store.os,
                "rename" if os.name == "nt" else "link",
                side_effect=fail_before_publish,
            )
            with publish_patch:
                with self.assertRaisesRegex(OSError, "pre-rename-failure"):
                    compiler._promote_changes(
                        fixture.stage,
                        fixture.root,
                        [relative],
                        {relative: None},
                        state_dir=fixture.state,
                        source_relative=f"daily/{fixture.source.name}",
                        source_digest=hashlib.sha256(fixture.source.read_bytes()).hexdigest(),
                        source_size=fixture.source.stat().st_size,
                        timestamp="2026-09-08T12:00:00+03:00",
                        suppression_digest=compiler._suppression_digest(frozenset()),
                    )

            journal = compile_state.load_publication(fixture.state)
            target = journal["targets"][0]
            marker = fixture.root / target["backup_relative"]
            temporary_path = fixture.root / target["temporary_relative"]
            marker.unlink()
            replacement = temporary_path.with_name(temporary_path.name + ".external")
            replacement.write_bytes(temporary_path.read_bytes())
            os.replace(replacement, temporary_path)

            with self.assertRaisesRegex(
                compile_state.PolicyError,
                "publication-target-ambiguous",
            ):
                compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertTrue(temporary_path.exists())
            self.assertFalse(destination.exists())

    def test_legacy_missing_target_journal_with_backup_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            relative = "knowledge/concepts/new.md"
            staged = fixture.stage / relative
            staged.parent.mkdir()
            staged.write_text("yeni dosya\n", encoding="utf-8")

            def fail_publication(*args: object, **kwargs: object) -> None:
                raise OSError("injected-publication-failure")

            with mock.patch.object(compiler, "_atomic_copy", side_effect=fail_publication):
                with self.assertRaises(OSError):
                    compiler._promote_changes(
                        fixture.stage,
                        fixture.root,
                        [relative],
                        {relative: None},
                        state_dir=fixture.state,
                        source_relative=f"daily/{fixture.source.name}",
                        source_digest=hashlib.sha256(fixture.source.read_bytes()).hexdigest(),
                        source_size=fixture.source.stat().st_size,
                        timestamp="2026-09-08T12:00:00+03:00",
                        suppression_digest=compiler._suppression_digest(frozenset()),
                    )

            journal = compile_state.load_publication(fixture.state)
            target = journal["targets"][0]
            target.pop("temporary_relative", None)
            target.pop("temporary_file_id", None)
            target.pop("marker_created", None)
            compile_state.save_publication(fixture.state, journal)
            backup = fixture.root / target["backup_relative"]
            backup.write_bytes(b"unattributed backup\n")

            with self.assertRaisesRegex(
                compile_state.PolicyError,
                "publication-target-ambiguous",
            ):
                compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertTrue(backup.exists())

    def test_unknown_target_drift_blocks_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            real_copy = compiler._atomic_copy

            def fail_second(*args: object, **kwargs: object) -> None:
                fail_second.calls += 1
                if fail_second.calls == 2:
                    raise OSError("injected-second-target-failure")
                real_copy(*args, **kwargs)

            fail_second.calls = 0
            with mock.patch.object(compiler, "_atomic_copy", side_effect=fail_second):
                with self.assertRaises(OSError):
                    fixture.promote()
            drifted = fixture.knowledge / "log.md"
            drifted.write_text("kullanıcı değişikliği\n", encoding="utf-8")

            with self.assertRaisesRegex(compile_state.PolicyError, "live-target-changed"):
                compiler._recover_pending_publication(fixture.root, fixture.state)
            self.assertEqual(drifted.read_text(encoding="utf-8"), "kullanıcı değişikliği\n")
            self.assertTrue(compile_state.publication_file(fixture.state).is_file())
            self.assertTrue(fixture.stage.is_dir())

    def test_publication_snapshot_token_changes_across_pending_aba(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PublicationFixture(Path(temporary))
            before = compile_state.publication_snapshot(fixture.root)
            self.assertEqual(before, compile_state.PublicationSnapshot(False, None))
            fixture.promote()
            pending = compile_state.publication_snapshot(fixture.root)
            self.assertTrue(pending.pending)
            self.assertIsNone(pending.publication_id)
            journal = compile_state.load_publication(fixture.state)
            operation_id = journal["operation_id"]
            self.assertRegex(
                journal["targets"][0]["backup_relative"],
                rf"^\.codex/scripts/\.state/\.cevo-publication-{operation_id}-[0-9a-f]{{64}}\.bak$",
            )
            compiler._finalize_publication(fixture.state)
            after = compile_state.publication_snapshot(fixture.root)
            self.assertFalse(after.pending)
            self.assertEqual(after.publication_id, operation_id)
            self.assertNotEqual(before, after)

    def test_source_and_preference_drift_block_stale_replay(self) -> None:
        for change in ("source", "preference"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                fixture = PublicationFixture(Path(temporary))
                real_copy = compiler._atomic_copy

                def fail_second(*args: object, **kwargs: object) -> None:
                    fail_second.calls += 1
                    if fail_second.calls == 2:
                        raise OSError("injected-second-target-failure")
                    real_copy(*args, **kwargs)

                fail_second.calls = 0
                with mock.patch.object(compiler, "_atomic_copy", side_effect=fail_second):
                    with self.assertRaises(OSError):
                        fixture.promote()
                if change == "source":
                    fixture.source.write_text("kaynak değişti\n", encoding="utf-8")
                else:
                    memory_ledger.suppress_derived_memory(
                        fixture.root / ".codex" / "private-memory",
                        "kaynak kararı",
                    )
                with self.assertRaisesRegex(
                    compile_state.PolicyError,
                    "publication-(source|preferences)-changed",
                ):
                    compiler._recover_pending_publication(fixture.root, fixture.state)
                self.assertTrue(compile_state.publication_file(fixture.state).is_file())
                self.assertTrue(fixture.stage.is_dir())


if __name__ == "__main__":
    unittest.main()
