"""Companion görünüm hattının savunma dallarını sürer."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import companion_memory as companion
import flush
from memory_ledger import load_suppressed_hashes, memory_read, suppress_derived_memory


IDENTITY = "1" * 64
KEY = "2" * 64
STAMP = "2026-09-11T10:00:00+00:00"
SUMMARY = "\n\n".join("## " + h + "\nOturum dersi." for h in flush.EXPECTED_SECTIONS)


def _junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


def _session_block(identity: str = IDENTITY, stamp: str = STAMP, key: str = KEY,
                   value: str = SUMMARY) -> str:
    return (
        f"<!-- cevo-session {identity} {stamp} {key} -->\n{value}\n<!-- /cevo-session -->"
    )


def _view_payload(*blocks: str, ts: float = 1789000000.0, key: str = KEY) -> bytes:
    body = "\n\n".join(blocks)
    return (
        f"{companion.BEGIN}{ts} {key} -->\n{body}\n{companion.END}"
    ).encode("utf-8")


def _companion_root(temporary: Path) -> Path:
    root = temporary / "vault"
    (root / "🔮 850-Companion").mkdir(parents=True)
    (root / "daily").mkdir()
    return root


class CompanionPathGuards(unittest.TestCase):
    def test_name_and_path_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            with self.assertRaises(ValueError):
                companion.source_marker("Bilinmez.md")
            with self.assertRaises(ValueError):
                companion._source_path(root, "Bilinmez.md")
            with self.assertRaises(ValueError):
                companion._view_path(root, "Bilinmez.md")
            (root / "🔮 850-Companion" / "Last-Session.md").mkdir()
            with self.assertRaises(ValueError):
                companion._view_path(root, "Last-Session.md")

    def test_daily_junction_blocks_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            root.mkdir()
            outside = Path(temporary) / "dis"
            outside.mkdir()
            _junction(root / "daily", outside)
            with self.assertRaises(ValueError):
                companion._canonical_path(root)


class ReflectionGuards(unittest.TestCase):
    def test_reflection_request_corruption_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            directory = state / "reflection-requests"
            directory.mkdir()
            target = companion._reflection_path(state, "oturum")

            self.assertIsNone(companion._reflection_bytes(target))

            gercek = state / "gercek.json"
            gercek.write_text("{}", encoding="utf-8")
            os.symlink(gercek, target)
            with self.assertRaises(ValueError):
                companion._reflection_bytes(target)
            target.unlink()

            target.write_bytes(b"x" * 5000)
            with self.assertRaises(ValueError):
                companion.capture_reflection(state, "oturum")

            target.write_bytes(bytes([255, 254, 250]))
            with self.assertRaises(ValueError):
                companion.capture_reflection(state, "oturum")

            target.write_text(json.dumps({"schema": 2}), encoding="utf-8")
            with self.assertRaises(ValueError):
                companion.capture_reflection(state, "oturum")

    def test_request_reflection_honors_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            companion.request_reflection(
                state, "oturum", deadline=time.monotonic() + 2.0
            )
            token = companion.capture_reflection(state, "oturum")
        self.assertIsNotNone(token.request_id)

    def test_request_reflection_passes_deadline_to_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            deadline = time.monotonic() + 2.0
            with mock.patch.object(
                companion,
                "atomic_write_json",
                wraps=companion.atomic_write_json,
            ) as write:
                companion.request_reflection(state, "oturum", deadline=deadline)

        write.assert_called_once()
        self.assertEqual(write.call_args.kwargs["deadline"], deadline)


class SessionParsingGuards(unittest.TestCase):
    def test_parse_sessions_rejects_duplicates_naive_time_and_stray_markers(self) -> None:
        with self.assertRaises(ValueError):
            companion._parse_sessions(_session_block() + "\n" + _session_block())
        with self.assertRaises(ValueError):
            companion._parse_sessions(
                _session_block(stamp="2026-09-11T10:00:00")
            )
        with self.assertRaises(ValueError):
            companion._parse_sessions(_session_block() + "\n<!-- cevo-session bozuk")

    def test_block_matches_skips_undecodable_and_rejects_duplicates(self) -> None:
        good = _view_payload(_session_block())
        bad = (
            companion.BEGIN.encode() + b"1.0 " + b"3" * 64 + b" -->\n"
            + bytes([255, 254, 250]) + b"\n" + companion.END.encode()
        )
        self.assertEqual(len(companion._block_matches(bad + b"\n" + good)), 1)
        with self.assertRaises(ValueError):
            companion._block_matches(good + b"\n" + good)

    def test_records_legacy_and_error_paths(self) -> None:
        self.assertEqual(companion._records("blok yok"), {})
        with self.assertRaises(ValueError):
            companion._records(companion.BEGIN + "eksik kapanış")

        two = (
            _view_payload(_session_block()).decode() + "\n"
            + _view_payload(_session_block(identity="4" * 64)).decode()
        )
        with self.assertRaises(ValueError):
            companion._records(two)

        legacy_sections = "\n\n".join(
            "### " + h + "\nEski ders." for h in flush.EXPECTED_SECTIONS
        )
        legacy = (
            f"{companion.BEGIN}1789000000.0 {KEY} -->\n{legacy_sections}\n{companion.END}"
        )
        records = companion._records(legacy)
        self.assertEqual(list(records), [KEY])
        self.assertEqual(records[KEY][0].tzinfo, dt.timezone.utc)

    def test_manual_split_guards(self) -> None:
        with self.assertRaises(ValueError):
            companion._manual_parts("Journal.md", b"marker yok")
        marker = companion.source_marker("Journal.md")
        with self.assertRaises(ValueError):
            companion._join_manual("Journal.md", marker + b"onces", b"sonra")


class CatalogGuards(unittest.TestCase):
    def _write_catalog(self, root: Path, payload) -> None:
        path = root / "daily" / "companion-sessions.json"
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_load_catalog_rejects_each_corruption(self) -> None:
        valid_row = {"event": STAMP, "key": KEY, "summary": SUMMARY}
        cases = [
            "{bozuk",
            {"schema": "v2"},
            {"schema": companion.CANONICAL_SCHEMA, "records": {"KISA": valid_row}},
            {"schema": companion.CANONICAL_SCHEMA,
             "records": {IDENTITY: {"event": STAMP, "key": KEY}}},
            {"schema": companion.CANONICAL_SCHEMA,
             "records": {IDENTITY: {**valid_row, "event": "2026-09-11T10:00:00"}}},
            {"schema": companion.CANONICAL_SCHEMA, "records": []},
            {"schema": companion.CANONICAL_SCHEMA, "records": {}, "manual": "x"},
            {"schema": companion.CANONICAL_SCHEMA, "records": {},
             "manual": {"Bilinmez.md": {"source_sha256": "a" * 64,
                                          "outside_sha256": "b" * 64}}},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:60]):
                with tempfile.TemporaryDirectory() as temporary:
                    root = _companion_root(Path(temporary))
                    self._write_catalog(root, payload)
                    with self.assertRaises(ValueError):
                        companion._load_catalog(root)


class ManualViewGuards(unittest.TestCase):
    def test_manual_for_view_conflict_and_adoption(self) -> None:
        name = "Journal.md"
        marker = companion.source_marker(name)
        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            sources = root / "🔮 850-Companion" / "Sources"
            sources.mkdir()
            (sources / name).write_bytes(b"kaynak-onu" + marker + b"kaynak-sonu")
            view = root / "🔮 850-Companion" / name
            view.write_bytes(b"gorunum-onu-gorunum-sonu")

            with self.assertRaises(ValueError):
                companion._manual_for_view(
                    root, name, {"source_sha256": "f" * 64}, {}, write_source=False,
                    canonical=True,
                )

            # Benimseme: önceki kayıt kaynak dosyanın özetine eşitse görünümdeki
            # elle düzenleme kaynağa devralınır.
            source_bytes = (sources / name).read_bytes()
            prefix, _suffix, meta = companion._manual_for_view(
                root, name, {"source_sha256": companion._sha(source_bytes)}, {},
                write_source=False, canonical=True,
            )
            self.assertEqual(prefix, b"gorunum-onu-gorunum-sonu")
            view_identity = companion._join_manual(name, b"gorunum-onu-gorunum-sonu", b"")
            self.assertEqual(meta["source_sha256"], companion._sha(view_identity))

        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            with self.assertRaises(ValueError):
                companion._manual_for_view(
                    root, name, None, {}, write_source=False, canonical=True
                )

    def test_bodies_render_and_projection_fallback(self) -> None:
        self.assertEqual(
            companion._bodies({}), {name: "" for name in companion.VIEW_NAMES}
        )
        marker = companion.source_marker("Threads.md")
        manuals = {"Threads.md": (b"on-", b"-son")}
        event = dt.datetime.fromisoformat(STAMP)
        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            suppress_derived_memory(
                root / ".codex/private-memory", f"daily/{event.date().isoformat()}.md"
            )
            hashes = load_suppressed_hashes(root / ".codex/private-memory")
        rendered = companion._render(
            {IDENTITY: (event, KEY, SUMMARY)}, manuals, hashes
        )
        self.assertEqual(rendered["Threads.md"], b"on--son")

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "cikti.md"
            companion._write_projection(target, bytes([255, 254, 250]))
            self.assertEqual(target.read_bytes(), bytes([255, 254, 250]))
        del marker

    def test_render_and_ensure_reject_stale_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            self.assertEqual(companion.render_views(root), {})
            with memory_read(root) as memory:
                with self.assertRaises(ValueError):
                    companion.render_views(
                        root, hashes=frozenset({"a" * 64}), memory=memory
                    )
                with self.assertRaises(ValueError):
                    companion.ensure_views(
                        root, hashes=frozenset({"a" * 64}), memory=memory
                    )


class ReconcileAndPublishGuards(unittest.TestCase):
    def _seeded(self, temporary: Path) -> Path:
        root = _companion_root(temporary)
        state = root / ".codex/scripts/.state"
        companion.publish(
            root, state, SUMMARY, dt.datetime.fromisoformat(STAMP), KEY,
            "oturum", frozenset(),
        )
        return root

    def test_reconcile_skips_suppressed_daily_and_flags_conflicts(self) -> None:
        event = dt.datetime.fromisoformat(STAMP)
        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            view = root / "🔮 850-Companion" / "Last-Session.md"
            view.write_bytes(_view_payload(_session_block()))

            suppress_derived_memory(
                root / ".codex/private-memory", f"daily/{event.date().isoformat()}.md"
            )
            hashes = load_suppressed_hashes(root / ".codex/private-memory")
            records: dict = {}
            companion._reconcile_current(root, records, hashes)
            self.assertEqual(records, {})

        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            view = root / "🔮 850-Companion" / "Last-Session.md"
            other = SUMMARY.replace("Oturum dersi.", "Çelişen ders.")
            view.write_bytes(_view_payload(_session_block(value=other)))
            records = {IDENTITY: (event, "5" * 64, SUMMARY)}
            with self.assertRaises(ValueError):
                companion._reconcile_current(root, records, frozenset())

    def test_publish_guards(self) -> None:
        event = dt.datetime.fromisoformat(STAMP)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            (root / "daily").mkdir(parents=True)
            companion.publish(
                root, root / "state", SUMMARY, event, KEY, "s", frozenset()
            )
            self.assertFalse((root / "daily" / "companion-sessions.json").exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            (root / "daily").mkdir(parents=True)
            outside = Path(temporary) / "dis-companion"
            outside.mkdir()
            _junction(root / "🔮 850-Companion", outside)
            with self.assertRaises(ValueError):
                companion.publish(
                    root, root / "state", SUMMARY, event, KEY, "s", frozenset()
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            suppress_derived_memory(
                root / ".codex/private-memory", f"daily/{event.date().isoformat()}.md"
            )
            hashes = load_suppressed_hashes(root / ".codex/private-memory")
            companion.publish(
                root, root / "state", SUMMARY, event, KEY, "s", hashes
            )
            self.assertFalse((root / "daily" / "companion-sessions.json").exists())

    def test_ensure_views_skips_suppressed_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._seeded(Path(temporary))
            suppress_derived_memory(
                root / ".codex/private-memory", "🔮 850-Companion/Threads.md"
            )
            result = companion.ensure_views(root)
        self.assertNotIn("Threads.md", result)

    def test_previous_summary_respects_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._seeded(Path(temporary))
            suppress_derived_memory(
                root / ".codex/private-memory", "🔮 850-Companion/Last-Session.md"
            )
            self.assertEqual(companion.previous_summary(root, "oturum"), "")


class MigrationGuards(unittest.TestCase):
    def test_migrate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "vault"
            (root / "daily").mkdir(parents=True)
            with self.assertRaises(ValueError):
                companion.migrate(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            (root / "🔮 850-Companion" / "Last-Session.md").write_bytes(
                _view_payload(_session_block())
            )
            (root / "🔮 850-Companion" / "Journal.md").write_bytes(
                _view_payload(
                    _session_block(value=SUMMARY.replace("Oturum", "Başka")),
                    ts=1789000001.0,
                )
            )
            with self.assertRaises(ValueError):
                companion.migrate(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            sources = root / "🔮 850-Companion" / "Sources"
            sources.mkdir()
            (sources / "Threads.md").write_bytes(b"farkli kaynak")
            with self.assertRaises(ValueError):
                companion.migrate(root)

    def test_migrate_detects_view_changed_during_run(self) -> None:
        real = companion._block_matches
        state = {"armed": True}

        def racing(payload, expected=None):
            if state["armed"]:
                state["armed"] = False
                view = state["view"]
                view.write_bytes(view.read_bytes() + b"\nyarista eklendi")
            return real(payload, expected)

        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            view = root / "🔮 850-Companion" / "Last-Session.md"
            view.write_bytes(_view_payload(_session_block()))
            state["view"] = view
            with mock.patch.object(companion, "_block_matches", racing):
                with self.assertRaises(ValueError):
                    companion.migrate(root)

    def test_migrate_success_and_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _companion_root(Path(temporary))
            (root / "🔮 850-Companion" / "Last-Session.md").write_bytes(
                _view_payload(_session_block())
            )
            self.assertEqual(companion._main(["migrate", "--root", str(root)]), 0)
            again = companion.migrate(root)
        self.assertEqual(again["status"], "already-migrated")


if __name__ == "__main__":
    unittest.main()
