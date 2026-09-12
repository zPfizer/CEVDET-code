"""state_store paylaşılan yazma/özet yüzeyinin doğrudan testleri.

T01: atomik yazıcı kopyaları state_store'a indi; burası o yüzeyin tek sahibi.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (sys.path seam)

import compile_state  # noqa: E402
import graph_integrity  # noqa: E402
import state_store  # noqa: E402


CODEX_DIR = Path(__file__).resolve().parents[1]
FROZEN_SHA_OWNERS = {"daily_store.py"}
FROZEN_WRITE_OWNERS: set[str] = set()
HELPER_DEF = re.compile(r"^def (_atomic_write\w*|_sha256|sha256_file)\(", re.MULTILINE)


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_json_is_compact_with_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"
            state_store.atomic_write_json(path, {"b": 1, "a": "ç"})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"b":1,"a":"ç"}\n')

    def test_atomic_write_json_forwards_indent_separators_sort_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            state_store.atomic_write_json(
                path,
                {"b": 1, "a": 2},
                indent=2,
                separators=None,
                sort_keys=True,
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                '{\n  "a": 2,\n  "b": 1\n}\n',
            )

    def test_atomic_write_json_default_separators_when_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            state_store.atomic_write_json(path, {"a": 1, "b": 2}, separators=None)
            self.assertEqual(path.read_text(encoding="utf-8"), '{"a": 1, "b": 2}\n')

    def test_atomic_write_text_with_lf_newline_keeps_crlf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "daily.md"
            text = "# Başlık\r\n\r\ngövde\r\n"
            state_store.atomic_write_text(path, text, newline="\n")
            self.assertEqual(path.read_bytes(), text.encode("utf-8"))

    def test_atomic_write_text_leaves_no_temporary_behind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_store.atomic_write_text(root / "note.md", "gövde\n")
            self.assertEqual(sorted(item.name for item in root.iterdir()), ["note.md"])

    def test_atomic_write_text_no_clobber_is_atomic_and_preserves_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "note.md"
            original = b"kullanici metni\r\nek not\r\n"
            target.write_bytes(original)
            with mock.patch.object(
                state_store.os,
                "replace",
                side_effect=AssertionError("no-clobber must not replace"),
            ):
                with self.assertRaises(FileExistsError):
                    state_store.atomic_write_text(
                        target,
                        "generated\n",
                        overwrite=False,
                        newline="\n",
                    )
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(root.glob(".note.md.*.tmp")), [])

            created = root / "created.md"
            state_store.atomic_write_text(created, "generated\n", overwrite=False)
            self.assertEqual(created.read_text(encoding="utf-8"), "generated\n")

    def test_stale_temporary_does_not_corrupt_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            state_store.atomic_write_json(target, {"generation": 1})
            crashed = root / ".state.json.deadbeef.tmp"
            crashed.write_text('{"generation": 9', encoding="utf-8")
            state_store.atomic_write_json(target, {"generation": 2})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"generation": 2},
            )
            self.assertTrue(crashed.is_file())

    def test_failed_replace_keeps_previous_target_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "state.json"
            state_store.atomic_write_json(target, {"generation": 1})
            with mock.patch("os.replace", side_effect=OSError("replace-failed")):
                with self.assertRaises(OSError):
                    state_store.atomic_write_json(target, {"generation": 2})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"generation": 1},
            )
            self.assertEqual(list(root.glob(".state.json.*.tmp")), [])
            self.assertTrue((root / "state.json").is_file())

    def test_fsync_is_default_and_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            with mock.patch("os.fsync") as synced:
                state_store.atomic_write_json(path, {"a": 1})
            self.assertEqual(synced.call_count, 1)
            with mock.patch("os.fsync") as not_synced:
                state_store.atomic_write_json(path, {"a": 2}, fsync=False)
            self.assertEqual(not_synced.call_count, 0)

    def test_keep_mode_requires_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "note.md"
            with self.assertRaises(FileNotFoundError):
                state_store.atomic_write_text(path, "gövde\n", keep_mode=True)
            path.write_text("eski\n", encoding="utf-8")
            mode = os.stat(path).st_mode
            state_store.atomic_write_text(path, "yeni\n", keep_mode=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "yeni\n")
            self.assertEqual(os.stat(path).st_mode, mode)


class DigestAndRootTests(unittest.TestCase):
    def test_sha256_file_matches_hashlib_across_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "big.bin"
            blob = (b"cevdet" * 400_000)[: 2 * 1024 * 1024 + 7]
            path.write_bytes(blob)
            self.assertEqual(
                state_store.sha256_file(path),
                hashlib.sha256(blob).hexdigest(),
            )

    def test_state_dir_and_vault_root_are_inverses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            state_dir = state_store.state_dir_of(vault)
            self.assertEqual(state_dir, vault / ".codex" / "scripts" / ".state")
            state_dir.mkdir(parents=True)
            self.assertEqual(state_store.vault_root_of(state_dir), vault)

    def test_vault_root_of_shallow_path_raises_index_error(self) -> None:
        with self.assertRaises(IndexError):
            state_store.vault_root_of(Path(Path(os.sep).anchor or os.sep))


class HealthPayloadTests(unittest.TestCase):
    def test_load_health_returns_empty_payload_for_missing_or_broken_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty = {
                "schema_version": state_store.HEALTH_SCHEMA_VERSION,
                "generation": 0,
                "components": {},
            }
            self.assertEqual(state_store._load_health(root / "yok.json"), empty)
            broken = root / "broken.json"
            broken.write_text("{", encoding="utf-8")
            self.assertEqual(state_store._load_health(broken), empty)
            listed = root / "listed.json"
            listed.write_text("[]", encoding="utf-8")
            self.assertEqual(state_store._load_health(listed), empty)

    def test_load_health_migrates_v1_error_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "health.json"
            path.write_text(
                json.dumps(
                    {
                        "component": "flush",
                        "error": "flush-failed",
                        "ts": 1_700_000_000,
                        "warnings": ["warn:clock"],
                    }
                ),
                encoding="utf-8",
            )
            migrated = state_store._load_health(path)
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["generation"], 1)
            self.assertEqual(
                migrated["components"]["flush:global"],
                {
                    "component": "flush",
                    "scope_key": "global",
                    "generation": 1,
                    "ts": 1_700_000_000,
                    "status": "error",
                    "error": "flush-failed",
                    "warnings": ["warn:clock"],
                },
            )

    def test_load_health_migrates_v1_warning_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "health.json"
            path.write_text(
                json.dumps({"component": "compile", "error": "warn:clock-seam", "ts": 5}),
                encoding="utf-8",
            )
            migrated = state_store._load_health(path)
            entry = migrated["components"]["compile:global"]
            self.assertEqual(entry["status"], "warning")
            self.assertEqual(entry["warnings"], [])

    def test_load_health_v1_without_identity_yields_zero_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "health.json"
            path.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            migrated = state_store._load_health(path)
            self.assertEqual(migrated["generation"], 0)
            self.assertEqual(migrated["components"], {})

    def test_summarize_health_prefers_error_over_warning(self) -> None:
        payload = {
            "components": {
                "a:global": {
                    "component": "a",
                    "status": "warning",
                    "error": "warn:a",
                    "warnings": ["warn:a"],
                },
                "b:global": {"component": "b", "status": "error", "error": "b-failed"},
            }
        }
        state_store._summarize_health(payload)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["component"], "b")
        self.assertEqual(payload["error"], "b-failed")
        self.assertNotIn("warnings", payload)

    def test_summarize_health_clears_identity_when_all_ok(self) -> None:
        payload = {
            "components": {"a:global": {"component": "a", "status": "ok"}},
            "component": "a",
            "error": "eski",
            "warnings": ["warn:a"],
        }
        state_store._summarize_health(payload)
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("component", payload)
        self.assertNotIn("error", payload)
        self.assertNotIn("warnings", payload)


class SingleOwnerTests(unittest.TestCase):
    def test_only_frozen_region_still_defines_private_write_helpers(self) -> None:
        owners: dict[str, list[str]] = {}
        for directory in ("scripts", "hooks"):
            for path in sorted((CODEX_DIR / directory).glob("*.py")):
                if path.name == "state_store.py":
                    continue
                found = HELPER_DEF.findall(path.read_text(encoding="utf-8"))
                if found:
                    owners[path.name] = found
        self.assertEqual(set(owners), FROZEN_WRITE_OWNERS | FROZEN_SHA_OWNERS, owners)

    def test_compile_cursor_writer_fsyncs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with mock.patch("os.fsync") as synced:
                compile_state.save(state_dir, compile_state.CompileState())
            path = state_dir / "compile-state.json"
            self.assertGreaterEqual(synced.call_count, 1)
            self.assertIn("cursor", json.loads(path.read_text(encoding="utf-8")))

    def test_graph_integrity_daily_link_writer_fsyncs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "2026-01-01.md"
            path.write_bytes("# Günlük Log: 2026-01-01\r\n\r\ngövde\r\n".encode("utf-8"))
            with mock.patch("os.fsync") as synced:
                self.assertTrue(graph_integrity.ensure_daily_graph_link(path))
            self.assertGreaterEqual(synced.call_count, 1)
            raw = path.read_bytes()
            self.assertIn(graph_integrity.DAILY_GRAPH_LINK.encode("utf-8"), raw)
            self.assertNotIn(b"\r\r\n", raw)


if __name__ == "__main__":
    unittest.main()
