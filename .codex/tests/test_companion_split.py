import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import companion_memory
import compile as compiler
import flush
import hook
import vault_retrieval
import memory_ledger
import doctor
import state_store


EVENT = dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc)


def _summary(context="Bağlam", pending="Açık iş"):
    parts = {name: "Yok." for name in flush.EXPECTED_SECTIONS}
    parts["Bağlam"] = context
    parts["Yapılacaklar"] = pending
    return flush.SessionSummary(parts).render()


def _canonical_block(session_id="old", event=EVENT, key="a" * 64):
    identity = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    value = _summary("Eski bağlam", "Eski iş")
    return (
        f"{companion_memory.BEGIN}{event.timestamp()} {key} -->\n"
        f"## Session: {event.isoformat()}\n"
        f"<!-- cevo-session {identity} {event.isoformat()} {key} -->\n"
        f"{value}\n<!-- /cevo-session -->\n"
        f"## Previous Sessions\n{companion_memory.END}"
    )


def _seed(root, *, block=None, prefix=b"# Last\r\n\r\n", suffix=b"\r\n# Tail\r\n"):
    companion = root / "🔮 850-Companion"
    companion.mkdir(parents=True)
    for name in companion_memory.VIEW_NAMES:
        body = prefix + (block.encode("utf-8") if block else b"") + suffix
        (companion / name).write_bytes(body)
    return companion


class CompanionSplitTests(unittest.TestCase):
    def test_previous_summary_reads_late_same_session_view_update_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            late = _canonical_block(event=EVENT + dt.timedelta(minutes=1), key="b" * 64)
            (companion / "Last-Session.md").write_text(
                "# Last\n\n" + late.replace("Eski bağlam", "Geç gelen bağlam"), encoding="utf-8",
            )
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertIn("Geç gelen bağlam", companion_memory.previous_summary(root, "old"))
            self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_deleted_manual_source_is_not_silently_recreated_from_a_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            companion_memory.ensure_views(root)
            source = companion / "Sources/Last-Session.md"
            source.unlink()
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(ValueError, "companion-manual-source-missing"):
                companion_memory.ensure_views(root)
            self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_doctor_reads_missing_generated_views_without_writing_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, prefix=b"# Notes\n\n## Active Execution\n\n## Waiting / Parked\n\n## Closed Threads\n", suffix=b"")
            for name in ("Core.md", "Kurallar.md"):
                (companion / name).write_text("# Source\n", encoding="utf-8")
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary(), EVENT, "a" * 64, "one", frozenset())
            for name in companion_memory.VIEW_NAMES:
                (companion / name).unlink()
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(doctor._companion_memory_check(doctor.Context(root)).status, "OK")
            self.assertEqual(doctor._thread_workload_check(doctor.Context(root)).status, "OK")
            self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_source_read_and_search_use_canonical_views_without_creating_missing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary("Kehribar kararı korundu."),
                                     EVENT, "a" * 64, "one", frozenset())
            for name in companion_memory.VIEW_NAMES:
                (companion / name).unlink()
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

            text = memory_ledger.read_memory_source(root, companion / "Last-Session.md")
            entries = vault_retrieval.build_vault_map(root, write_cache=False)
            hits = vault_retrieval.search_vault(entries, "Kehribar kararı")

            self.assertIn("Kehribar kararı", text)
            self.assertTrue(hits)
            self.assertEqual({entry.path for entry in entries}, {
                f"🔮 850-Companion/{name}" for name in companion_memory.VIEW_NAMES
            })
            self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_retrieval_reads_canonical_only_views_without_creating_missing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary("Kehribar kararı korundu."),
                                     EVENT, "a" * 64, "one", frozenset())
            for name in companion_memory.VIEW_NAMES:
                (companion / name).unlink()
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

            result = vault_retrieval.retrieve_vault_context_detailed(
                root, "Kehribar kararı", write_cache=False,
            )

            self.assertGreater(result.hits, 0)
            self.assertIn("Kehribar", result.text)
            self.assertEqual(before, {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*") if path.is_file()
            })

    def test_opaque_retrieval_reads_missing_canonical_view_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            companion_memory.migrate(root)
            companion_memory.publish(
                root,
                root / ".state",
                _summary("H08 canonical görünüm.\nH08 gizli canonical."),
                EVENT,
                "a" * 64,
                "one",
                frozenset(),
            )
            memory_ledger.suppress_derived_memory(
                root / ".codex/private-memory", "H08 gizli canonical.", now=1
            )
            for name in companion_memory.VIEW_NAMES:
                (companion / name).unlink()
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*") if path.is_file()
            }
            relative = f"🔮 850-Companion/{companion_memory.VIEW_NAMES[0]}"
            opaque = memory_ledger.memory_view_relative_path(relative)

            result = vault_retrieval.retrieve_vault_context_detailed(
                root,
                "H08 canonical görünüm",
                write_cache=False,
                write_views=False,
            )
            text = memory_ledger.read_memory_source(root, root / opaque)
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*") if path.is_file()
            }
            view_exists = (companion / companion_memory.VIEW_NAMES[0]).exists()

        self.assertGreater(result.hits, 0)
        self.assertIn(opaque, result.text)
        self.assertIn("H08 canonical görünüm.", text)
        self.assertNotIn("H08 gizli canonical.", text)
        self.assertEqual(before, after)
        self.assertFalse(view_exists)

    def test_source_read_and_search_prefer_new_manual_source_to_existing_stale_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary(), EVENT, "a" * 64, "one", frozenset())
            source = companion / "Sources/Last-Session.md"
            source.write_bytes("Morötesi karar.\n".encode("utf-8") + source.read_bytes())
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

            text = memory_ledger.read_memory_source(root, companion / "Last-Session.md")
            entries = vault_retrieval.build_vault_map(root, write_cache=False)
            self.assertIn("Morötesi karar", text)
            self.assertTrue(vault_retrieval.search_vault(entries, "Morötesi karar"))
            self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_repeated_forgetting_does_not_turn_filtered_views_into_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            companion_memory.migrate(root)
            original = _summary("Birinci unutulacak cümle.\nİkinci unutulacak cümle.\nKalıcı bilgi <!-- not -->")
            companion_memory.publish(root, root / ".state", original, EVENT, "a" * 64, "one", frozenset())
            for sentence in ("Birinci unutulacak cümle.", "İkinci unutulacak cümle."):
                private = root / ".codex/private-memory"
                memory_ledger.suppress_derived_memory(private, sentence)
                hashes = memory_ledger.load_suppressed_hashes(private)
                for _ in range(2):
                    views = companion_memory.ensure_views(root, root / ".state", hashes=hashes)
                    self.assertNotIn(sentence, views["Last-Session.md"])
                    self.assertIn("Kalıcı bilgi", views["Last-Session.md"])
            catalog = json.loads((root / companion_memory.CANONICAL_RELATIVE).read_text(encoding="utf-8"))
            self.assertEqual(catalog["records"][hashlib.sha256(b"one").hexdigest()]["summary"], original)

    def test_migration_preserves_manual_bytes_and_canonical_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = b"# Last\r\n\r\nManual prefix\xC4\x9F\r\n"
            suffix = b"\r\nManual suffix\xC4\x9F\r\n"
            _seed(root, block=_canonical_block(), prefix=prefix, suffix=suffix)

            report = companion_memory.migrate(root)

            self.assertEqual(report["records"], 1)
            source = root / "🔮 850-Companion/Sources/Last-Session.md"
            marker = companion_memory.source_marker("Last-Session.md")
            self.assertEqual(source.read_bytes(), prefix + marker + suffix)
            catalog = json.loads((root / "daily/companion-sessions.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["schema"], companion_memory.CANONICAL_SCHEMA)
            self.assertIn(hashlib.sha256(b"old").hexdigest(), catalog["records"])

    def test_migration_accepts_crlf_generated_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root, block=_canonical_block().replace("\n", "\r\n"))

            report = companion_memory.migrate(root)

            self.assertEqual(report["records"], 1)

    def test_publish_recreates_missing_views_from_canonical_without_source_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            sources = {
                name: (companion / "Sources" / name).read_bytes()
                for name in companion_memory.VIEW_NAMES
            }
            for name in companion_memory.VIEW_NAMES:
                (companion / name).unlink()

            companion_memory.publish(root, root / ".state", _summary("Yeni bağlam", "Yeni iş"),
                                     EVENT + dt.timedelta(minutes=1), "b" * 64, "new", frozenset())

            self.assertTrue(all((companion / name).is_file() for name in companion_memory.VIEW_NAMES))
            self.assertIn("Yeni bağlam", (companion / "Last-Session.md").read_text(encoding="utf-8"))
            self.assertEqual(
                sources,
                {name: (companion / "Sources" / name).read_bytes() for name in companion_memory.VIEW_NAMES},
            )
            self.assertIn("Eski bağlam", companion_memory.previous_summary(root, "old"))

    def test_generated_view_manual_edit_is_adopted_without_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary(), EVENT, "a" * 64, "one", frozenset())
            path = companion / "Last-Session.md"
            # Keep the generated block intact while changing only its prefix.
            raw = path.read_bytes()
            start = raw.find(companion_memory.BEGIN.encode())
            path.write_bytes(b"# User edited prefix\r\n" + raw[start:])

            companion_memory.publish(root, root / ".state", _summary("İkinci bağlam"),
                                     EVENT + dt.timedelta(minutes=1), "b" * 64, "one", frozenset())

            source = (companion / "Sources/Last-Session.md").read_bytes()
            self.assertIn(b"# User edited prefix\r\n", source)
            self.assertIn(b"# User edited prefix\r\n", (companion / "Last-Session.md").read_bytes())
            self.assertIn("İkinci bağlam", path.read_text(encoding="utf-8"))

    def test_source_and_view_edits_conflict_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary(), EVENT, "a" * 64, "one", frozenset())
            source = companion / "Sources/Last-Session.md"
            source.write_bytes(source.read_bytes().replace(b"# Last", b"# Source edit", 1))
            view = companion / "Last-Session.md"
            raw = view.read_bytes()
            start = raw.find(companion_memory.BEGIN.encode())
            view.write_bytes(b"# View edit\r\n" + raw[start:])

            with self.assertRaisesRegex(ValueError, "companion-manual-view-conflict"):
                companion_memory.publish(root, root / ".state", _summary(),
                                         EVENT + dt.timedelta(minutes=1), "b" * 64, "one", frozenset())

    def test_source_edit_before_guarded_manual_write_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary(), EVENT, "a" * 64, "one", frozenset())
            view = companion / "Last-Session.md"
            raw = view.read_bytes()
            start = raw.find(companion_memory.BEGIN.encode())
            view.write_bytes(b"# User edited prefix\r\n" + raw[start:])
            source = companion / "Sources/Last-Session.md"
            user_bytes = b"# Concurrent source edit\r\n" + source.read_bytes()
            real_write = companion_memory._write_manual
            injected = False

            def inject(path, payload, **kwargs):
                nonlocal injected
                if path == source and not injected:
                    injected = True
                    path.write_bytes(user_bytes)
                return real_write(path, payload, **kwargs)

            with mock.patch.object(companion_memory, "_write_manual", side_effect=inject):
                with self.assertRaisesRegex(ValueError, "companion-manual-view-conflict"):
                    companion_memory.publish(
                        root, root / ".state", _summary("İkinci bağlam"),
                        EVENT + dt.timedelta(minutes=1), "b" * 64, "one", frozenset(),
                    )
            self.assertTrue(injected)
            self.assertEqual(source.read_bytes(), user_bytes)

    def test_view_edit_before_guarded_projection_is_preserved(self):
        for operation in ("ensure", "publish"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                companion = _seed(root)
                companion_memory.migrate(root)
                companion_memory.publish(root, root / ".state", _summary(), EVENT, "a" * 64, "one", frozenset())
                source = companion / "Sources/Last-Session.md"
                source.write_bytes(source.read_bytes().replace(b"# Last", b"# Source edit", 1))
                target = companion / "Last-Session.md"
                user_bytes = b"# Concurrent view edit\r\n" + target.read_bytes()
                real_write = companion_memory._write_projection
                injected = False

                def inject(path, payload, **kwargs):
                    nonlocal injected
                    if path == target and not injected:
                        injected = True
                        path.write_bytes(user_bytes)
                    return real_write(path, payload, **kwargs)

                with mock.patch.object(companion_memory, "_write_projection", side_effect=inject):
                    with self.assertRaisesRegex(ValueError, "companion-manual-view-conflict"):
                        if operation == "ensure":
                            companion_memory.ensure_views(root, root / ".state", write=True)
                        else:
                            companion_memory.publish(
                                root, root / ".state", _summary("İkinci bağlam"),
                                EVENT + dt.timedelta(minutes=1), "b" * 64, "one", frozenset(),
                            )
                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), user_bytes)

    @unittest.skipUnless(os.name == "nt", "requires a Windows guarded replacement")
    def test_guarded_projection_keeps_backup_after_backup_digest_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "Last-Session.md"
            state = root / ".state"
            original = b"user content\r\n"
            target.write_bytes(original)

            with mock.patch.object(
                state_store,
                "_locked_windows_digest",
                side_effect=OSError("backup digest unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "backup digest unavailable"):
                    companion_memory._write_projection(
                        target,
                        b"generated\n",
                        expected_digest=companion_memory._sha(original),
                        state=state,
                    )

            backups = list(state.glob(".Last-Session.md.companion-*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(target.read_bytes(), b"generated\n")
            target.write_bytes(original)
            with self.assertRaisesRegex(ValueError, "companion-manual-view-conflict"):
                companion_memory._write_projection(
                    target,
                    b"retry\n",
                    expected_digest=companion_memory._sha(original),
                    state=state,
                )
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(backups[0].read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "requires a Windows guarded replacement")
    def test_guarded_projection_keeps_backup_after_output_digest_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "Last-Session.md"
            state = root / ".state"
            original = b"user content\r\n"
            target.write_bytes(original)
            real_sha = state_store.sha256_file
            calls = 0

            def fail_output(path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("output digest unavailable")
                return real_sha(path)

            with mock.patch.object(state_store, "sha256_file", side_effect=fail_output):
                with self.assertRaisesRegex(OSError, "output digest unavailable"):
                    companion_memory._write_projection(
                        target,
                        b"generated\n",
                        expected_digest=companion_memory._sha(original),
                        state=state,
                    )

            backups = list(state.glob(".Last-Session.md.companion-*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(target.read_bytes(), b"generated\n")

    @unittest.skipUnless(os.name == "nt", "requires a Windows guarded replacement")
    def test_publish_rejects_retry_with_unresolved_guarded_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            state = root / ".state"
            companion_memory.migrate(root)
            companion_memory.publish(root, state, _summary(), EVENT, "a" * 64, "one", frozenset())
            source = companion / "Sources/Last-Session.md"
            source.write_bytes(source.read_bytes().replace(b"# Last", b"# Source edit", 1))

            with mock.patch.object(
                state_store,
                "_locked_windows_digest",
                side_effect=OSError("backup digest unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "backup digest unavailable"):
                    companion_memory.publish(
                        root,
                        state,
                        _summary("Yeni bağlam"),
                        EVENT + dt.timedelta(minutes=1),
                        "b" * 64,
                        "one",
                        frozenset(),
                    )

            backups = list(state.glob(".Last-Session.md.companion-*.bak"))
            self.assertEqual(len(backups), 1)
            with self.assertRaisesRegex(ValueError, "companion-manual-view-conflict"):
                companion_memory.publish(
                    root,
                    state,
                    _summary("Yeni bağlam"),
                    EVENT + dt.timedelta(minutes=1),
                    "b" * 64,
                    "one",
                    frozenset(),
                )
            self.assertTrue(backups[0].is_file())

    def test_anonymous_legacy_block_remains_tracked_manual_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = (
                f"{companion_memory.BEGIN}{EVENT.timestamp()} {'a' * 64} -->\n"
                "## Session: legacy\n\nAnonim eski kayıt.\n"
                f"{companion_memory.END}"
            )
            companion = _seed(root, block=legacy)
            companion_memory.migrate(root)
            source = companion / "Sources/Last-Session.md"
            self.assertIn(legacy.encode("utf-8"), source.read_bytes())
            for name in companion_memory.VIEW_NAMES:
                (companion / name).unlink()
            companion_memory.publish(root, root / ".state", _summary("Yeni"), EVENT, "b" * 64, "new", frozenset())
            self.assertIn("Anonim eski kayıt.", (companion / "Last-Session.md").read_text(encoding="utf-8"))

    def test_duplicate_canonical_blocks_are_rejected_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            duplicate = (companion / "Last-Session.md").read_bytes()
            path = companion / "Last-Session.md"
            path.write_bytes(duplicate + duplicate)

            with self.assertRaisesRegex(ValueError, "companion-block-duplicate"):
                companion_memory.migrate(root)
            self.assertFalse((root / "daily/companion-sessions.json").exists())

    def test_unparseable_legacy_block_is_kept_in_manual_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = f"{companion_memory.BEGIN}{'a' * 64} eski ve eksik"
            companion = _seed(root, block=legacy)

            report = companion_memory.migrate(root)

            self.assertEqual(report["records"], 0)
            self.assertIn(legacy.encode("utf-8"), (companion / "Sources/Last-Session.md").read_bytes())

    def test_virtual_startup_fallback_does_not_write_in_read_only_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            for name in companion_memory.VIEW_NAMES:
                (companion / name).unlink()
            for name, text in {
                "Core.md": "# Cevo\n",
                "Profile.md": "# Profil\n",
                "Kurallar.md": "# Kurallar\n",
            }.items():
                (companion / name).write_text(text, encoding="utf-8")
            state = root / ".codex/scripts/.state"
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()
            }

            context = hook.build_session_context(root, state, write_views=False, now=EVENT)

            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()
            }
            self.assertIn("Eski bağlam", context)
            self.assertEqual(before, after)
            self.assertFalse((root / "🔮 850-Companion/Last-Session.md").exists())

    def test_manual_source_directory_is_not_indexed_as_a_second_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = root / "🔮 850-Companion/Sources"
            companion.mkdir(parents=True)
            (companion / "Last-Session.md").write_text(
                "---\ntitle: Duplicate\n---\nunique-source-term\n", encoding="utf-8"
            )

            entries = vault_retrieval.build_vault_map(root, write_cache=False)

            self.assertNotIn("🔮 850-Companion/Sources/Last-Session.md", {entry.path for entry in entries})

    def test_partial_migration_retries_exact_sources_without_overwriting_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root, block=_canonical_block())
            real_write = companion_memory._write_manual
            writes = 0

            def interrupt(path, payload):
                nonlocal writes
                real_write(path, payload)
                writes += 1
                if writes == 1:
                    raise OSError("interrupted")

            with mock.patch.object(companion_memory, "_write_manual", side_effect=interrupt):
                with self.assertRaises(OSError):
                    companion_memory.migrate(root)
            first = (root / "🔮 850-Companion/Sources/Last-Session.md").read_bytes()

            report = companion_memory.migrate(root)

            self.assertEqual(report["status"], "migrated")
            self.assertEqual(first, (root / "🔮 850-Companion/Sources/Last-Session.md").read_bytes())
            self.assertTrue((root / "daily/companion-sessions.json").is_file())

    def test_whole_last_session_suppression_hides_canonical_previous_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            memory_ledger.suppress_derived_memory(
                root / ".codex/private-memory",
                "🔮 850-Companion/Last-Session.md",
            )

            self.assertEqual(companion_memory.previous_summary(root, "old"), "")

    def test_per_view_suppression_does_not_stop_other_projection_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            (companion / "Journal.md").unlink()
            memory_ledger.suppress_derived_memory(
                root / ".codex/private-memory",
                "🔮 850-Companion/Journal.md",
            )

            companion_memory.publish(root, root / ".state", _summary("Yeni"),
                                     EVENT + dt.timedelta(minutes=1), "b" * 64, "new",
                                     memory_ledger.load_suppressed_hashes(root / ".codex/private-memory"))

            self.assertFalse((companion / "Journal.md").exists())
            self.assertIn("Yeni", (companion / "Last-Session.md").read_text(encoding="utf-8"))

    def test_git_checkpoint_keeps_generated_views_ignored_but_manual_source_dirty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            (root / "daily").mkdir()
            (root / ".gitignore").write_text(
                "🔮 850-Companion/Last-Session.md\n"
                "🔮 850-Companion/Journal.md\n"
                "🔮 850-Companion/Threads.md\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary("İlk"), EVENT, "a" * 64, "one", frozenset())
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "seed"], cwd=root, check=True)
            compiler._checkpoint_machine_outputs(root, root / ".state", "2026-09-08")
            self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True,
                                            capture_output=True, check=True).stdout, "")

            view = companion / "Last-Session.md"
            raw = view.read_bytes()
            view.write_bytes(b"# Manual view edit\r\n" + raw[raw.find(companion_memory.BEGIN.encode()):])
            companion_memory.publish(root, root / ".state", _summary("İkinci"),
                                     EVENT + dt.timedelta(minutes=1), "b" * 64, "one", frozenset())
            compiler._checkpoint_machine_outputs(root, root / ".state", "2026-09-08")
            status = subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True,
                                    capture_output=True, check=True).stdout
            self.assertIn("Sources/Last-Session.md", status)
            self.assertNotIn("Last-Session.md\n", status)

    def test_fresh_clone_without_state_or_views_regenerates_from_tracked_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            companion = _seed(root, block=_canonical_block(), prefix=b"# Last\n\n", suffix=b"\n# Tail\n")
            (root / "daily").mkdir()
            (root / ".gitattributes").write_bytes((_fixtures.CODEX_DIR.parent / ".gitattributes").read_bytes())
            (root / ".gitignore").write_text("\n".join(
                f"🔮 850-Companion/{name}" for name in companion_memory.VIEW_NAMES
            ) + "\n.codex/scripts/.state/*\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary("Kalıcı"), EVENT, "a" * 64, "one", frozenset())
            manual_before = {name: (companion / "Sources" / name).read_bytes() for name in companion_memory.VIEW_NAMES}
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "seed"], cwd=root, check=True)
            clone = Path(temporary) / "clone"
            subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(root), str(clone)], check=True)
            subprocess.run(["git", "-C", str(clone), "config", "core.autocrlf", "true"], check=True)
            subprocess.run(["git", "-C", str(clone), "checkout", "--quiet", "main"], check=True)
            for name in companion_memory.VIEW_NAMES:
                self.assertFalse((clone / "🔮 850-Companion" / name).exists())
            self.assertFalse((clone / ".codex/scripts/.state").exists())
            self.assertEqual(manual_before, {
                name: (clone / "🔮 850-Companion/Sources" / name).read_bytes()
                for name in companion_memory.VIEW_NAMES
            })

            companion_memory.ensure_views(clone, clone / ".codex/scripts/.state", write=True)

            self.assertTrue((clone / "🔮 850-Companion/Last-Session.md").is_file())
            self.assertIn("Kalıcı", (clone / "🔮 850-Companion/Last-Session.md").read_text(encoding="utf-8"))

    def test_projection_failure_does_not_roll_manual_source_back_on_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            source = companion / "Sources/Last-Session.md"
            source.write_bytes(source.read_bytes().replace(b"# Last", b"# Source edit", 1))
            with mock.patch.object(companion_memory, "_write_projection", side_effect=OSError("view interrupted")):
                with self.assertRaises(OSError):
                    companion_memory.ensure_views(root, root / ".state", write=True)
            self.assertIn(b"# Source edit", source.read_bytes())

            companion_memory.ensure_views(root, root / ".state", write=True)
            self.assertIn(b"# Source edit", source.read_bytes())

    def test_manual_view_block_move_updates_the_full_tracked_split_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            path = companion / "Last-Session.md"
            raw = path.read_bytes()
            match = companion_memory._block_matches(raw, companion_memory._discover_current(root))[0][0]
            before, machine, after = raw[:match.start()], raw[match.start():match.end()], raw[match.end():]
            path.write_bytes(before + after[:1] + machine + after[1:])

            companion_memory.ensure_views(root, root / ".state", write=True)

            moved = (companion / "Sources/Last-Session.md").read_bytes()
            marker = companion_memory.source_marker("Last-Session.md")
            self.assertEqual(moved, before + after[:1] + marker + after[1:])

    def test_publish_projection_failure_keeps_manual_source_baseline_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            source = companion / "Sources/Last-Session.md"
            source.write_bytes(source.read_bytes().replace(b"# Last", b"# Publish source edit", 1))

            with mock.patch.object(companion_memory, "_write_projection", side_effect=OSError("view interrupted")):
                with self.assertRaises(OSError):
                    companion_memory.publish(root, root / ".state", _summary("Yeni"),
                                             EVENT + dt.timedelta(minutes=1), "b" * 64, "new", frozenset())
            self.assertIn(b"# Publish source edit", source.read_bytes())

            companion_memory.publish(root, root / ".state", _summary("Yeni"),
                                     EVENT + dt.timedelta(minutes=1), "b" * 64, "new", frozenset())
            self.assertIn(b"# Publish source edit", source.read_bytes())

    def test_startup_reconciles_a_late_view_record_before_rendering_canonical_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            old_records = companion_memory._discover_current(root)
            late_id = hashlib.sha256(b"late-worker").hexdigest()
            late_event = EVENT + dt.timedelta(minutes=2)
            late_value = _summary("Geç gelen iş", "Geç gelen kayıt")
            records = dict(old_records)
            records[late_id] = late_event, "c" * 64, late_value
            body = companion_memory._bodies(records)["Last-Session.md"]
            path = companion / "Last-Session.md"
            raw = path.read_text(encoding="utf-8")
            match = companion_memory.BLOCK.search(raw)
            self.assertIsNotNone(match)
            replacement = (
                f"{companion_memory.BEGIN}{late_event.timestamp()} {'c' * 64} -->\n"
                f"{body}{companion_memory.END}"
            )
            path.write_text(raw[:match.start()] + replacement + raw[match.end():], encoding="utf-8")

            companion_memory.ensure_views(root, root / ".state", write=True)

            catalog = json.loads((root / "daily/companion-sessions.json").read_text(encoding="utf-8"))
            self.assertIn(late_id, catalog["records"])
            self.assertIn("Geç gelen iş", (companion / "Last-Session.md").read_text(encoding="utf-8"))

    def test_equal_timestamp_conflict_is_preserved_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            path = companion / "Last-Session.md"
            path.write_bytes(path.read_bytes().replace("Eski bağlam".encode("utf-8"), "Çatışan değer".encode("utf-8"), 1))
            before_catalog = (root / "daily/companion-sessions.json").read_bytes()
            before_view = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "companion-session-conflict"):
                companion_memory.ensure_views(root, root / ".state", write=True)

            self.assertEqual(before_catalog, (root / "daily/companion-sessions.json").read_bytes())
            self.assertEqual(before_view, path.read_bytes())

    def test_read_only_startup_prefers_virtual_companion_over_stale_physical_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            path = companion / "Last-Session.md"
            stale = path.read_bytes().replace("Eski bağlam".encode("utf-8"), "STALE physical".encode("utf-8"), 1)
            path.write_bytes(stale)
            for name, text in {"Core.md": "# Cevo\n", "Profile.md": "# Profil\n", "Kurallar.md": "# Kurallar\n"}.items():
                (companion / name).write_text(text, encoding="utf-8")
            before = path.read_bytes()

            context = hook.build_session_context(root, root / ".state", write_views=False, now=EVENT)

            self.assertNotIn("STALE physical", context)
            self.assertIn("Eski bağlam", context)
            self.assertEqual(before, path.read_bytes())

    def test_read_source_keeps_lexical_alias_for_suppression_but_resolved_path_for_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "🔮 850-Companion/Profile.md"
            profile.parent.mkdir(parents=True)
            profile.write_text("# Profil\nGeçersiz tercih kaydı.\n", encoding="utf-8")
            human = root / "human.md"
            fake_user = "a" * 64
            human.write_text(f"Sahte [[daily/2026-09-08#user-{fake_user}|dayanak]].\n", encoding="utf-8")
            (root / "daily").mkdir()

            with memory_ledger.memory_read(root) as memory:
                _relative, profile_text = memory.read_source(profile, relative="alias/Profile.md")
                _relative, text = memory.read_source(human, relative="daily/alias.md")

            self.assertIsNone(profile_text)
            self.assertNotIn("Sahte", text)

    def test_old_view_suppression_hides_the_new_tracked_manual_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root, block=_canonical_block())
            companion_memory.migrate(root)
            memory_ledger.suppress_derived_memory(
                root / ".codex/private-memory",
                "🔮 850-Companion/Last-Session.md",
            )

            self.assertEqual(
                memory_ledger.read_memory_source(root, companion / "Sources/Last-Session.md"),
                "",
            )

    def test_canonical_session_json_is_not_a_readable_vault_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            companion = _seed(root)
            companion_memory.migrate(root)
            companion_memory.publish(root, root / ".state", _summary("JSON"), EVENT, "a" * 64, "one", frozenset())

            with self.assertRaisesRegex(ValueError, "memory-source-internal"):
                memory_ledger.read_memory_source(root, root / "daily/companion-sessions.json")


if __name__ == "__main__":
    unittest.main()
