"""hook/companion/ledger/attachment — kalan savunma kolları (dilim 2)."""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import attachment_memory
import companion_memory
import flush
import hook
import memory_ledger
from memory_ledger import MemoryPreferenceError
from test_defensive_error_paths_attachments import (
    _capture,
    _envelope,
    _seed,
)

NOW = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.timezone.utc)


def _junction(source: Path, target: Path) -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(source), str(target)],
        capture_output=True,
    )
    return result.returncode == 0


class HookHelperArms(unittest.TestCase):
    def test_compact_index_head_and_tail_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "index.md"
            rows = [
                "| [[knowledge/k0]] | " + "ç" * 400 + " |",
                "| [[knowledge/k1]] | kısa özet bir |",
                "| [[knowledge/k2]] | kısa özet iki |",
            ]
            index.write_text(
                "# Bilgi Tabanı\n\n| Makale | Özet |\n| --- | --- |\n"
                + "\n".join(rows) + "\n",
                encoding="utf-8",
            )
            compact = hook._compact_knowledge_index(index, 280)
            self.assertIn("gösterilmedi", compact)
            self.assertIn("k0", compact)
            self.assertIn("kısa özet iki", compact)

            rows = [
                "| [[knowledge/k0]] | kısa özet sıfır |",
                "| [[knowledge/k1]] | " + "ç" * 400 + " |",
                "| [[knowledge/k2]] | kısa özet iki |",
            ]
            index.write_text(
                "# Bilgi Tabanı\n\n| Makale | Özet |\n| --- | --- |\n"
                + "\n".join(rows) + "\n",
                encoding="utf-8",
            )
            compact = hook._compact_knowledge_index(index, 300)
            self.assertIn("kısa özet sıfır", compact)

    def test_recent_daily_tail_text_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            today = NOW.date().isoformat()
            tail = hook._recent_daily_tail(
                vault, now=NOW,
                texts={f"daily/{today}.md": "### Oturum (10:00)\niçerik"},
            )
            self.assertIn("Oturum", tail)
            self.assertEqual(
                hook._recent_daily_tail(
                    vault, now=NOW, texts={f"daily/{today}.md": None}
                ),
                "",
            )

    def test_prompt_count_failure_and_context_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            state = vault / "state"
            state.mkdir()
            retrieval = types.SimpleNamespace(
                outcome="ok", entries=1, hits=1, paths=("daily/x.md",),
                text="bulgu", budget=1000,
            )
            with (
                mock.patch.object(
                    hook, "retrieve_vault_context_detailed",
                    return_value=retrieval,
                ),
                mock.patch.object(
                    hook, "_increment_prompt_count", side_effect=OSError
                ),
            ):
                context = hook.handle_user_prompt(
                    {"session_id": "s", "prompt": "Atlas planı nedir?",
                     "cwd": str(vault)},
                    state, vault_root=vault, now=1.0,
                )
            self.assertIsInstance(context, str)

            with (
                mock.patch.object(
                    hook, "retrieve_vault_context_detailed",
                    return_value=retrieval,
                ),
                mock.patch.object(
                    hook, "USER_PROMPT_CONTEXT_TARGET_CHARS", 5
                ),
            ):
                context = hook.handle_user_prompt(
                    {"session_id": "s", "prompt": "Atlas planı nedir?",
                     "cwd": str(vault)},
                    state, vault_root=vault, now=1.0,
                )
            self.assertEqual(context, "")

    def test_reflection_state_guards(self) -> None:
        hook._mark_reflection_if_needed({"session_id": ""})
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with mock.patch.object(hook, "STATE_DIR", state):
                record = state / (
                    f"conversation-{hook.session_key('s')}.json"
                )
                record.write_text("{bozuk", encoding="utf-8")
                with self.assertRaises(ValueError):
                    hook._mark_reflection_if_needed({"session_id": "s"})
                record.write_text("[]", encoding="utf-8")
                with self.assertRaises(ValueError):
                    hook._mark_reflection_if_needed({"session_id": "s"})

    def test_queue_count_shape_guards(self) -> None:
        self.assertEqual(
            hook._unresolved_terminal_count({"counts": {"dead-letter": "x"}}),
            0,
        )
        self.assertEqual(
            hook._quarantined_count({"counts": {"quarantined": True}}), 0
        )

    def test_clear_health_swallows_write_error(self) -> None:
        with mock.patch.object(
            hook, "write_hook_health", side_effect=ValueError
        ):
            hook.clear_hook_health(Path("."), {})

    def test_stop_message_emits_block_decision(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout):
            hook._emit_user_prompt_result("Ne oldu: kesinti gerekli")
        value = json.loads(stdout.getvalue())
        self.assertEqual(value["decision"], "block")


class HookMainArms(unittest.TestCase):
    def _main(self, event, payload, patches):
        stdout = io.StringIO()
        stderr = io.StringIO()
        managers = [
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
            mock.patch.object(hook, "_validate_hook_scope"),
            *patches,
        ]
        entered = []
        try:
            for manager in managers:
                entered.append(manager.__enter__())
            code = hook.main([event])
        finally:
            for manager in reversed(managers):
                manager.__exit__(None, None, None)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_session_start_generic_preference_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            code, _out, _err = self._main(
                "session-start", {"session_id": "s", "cwd": str(temporary)},
                [
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(
                        hook, "VAULT_ROOT", Path(temporary)
                    ),
                    mock.patch.object(
                        hook, "build_session_context",
                        side_effect=MemoryPreferenceError("memory-x"),
                    ),
                ],
            )
        self.assertEqual(code, 0)

    def test_session_start_wakes_maintenance_supervisor(self) -> None:
        supervisor = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            code, _out, _err = self._main(
                "session-start", {"session_id": "s", "cwd": str(temporary)},
                [
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "VAULT_ROOT", Path(temporary)),
                    mock.patch.object(
                        hook, "build_session_context", return_value="bağlam"
                    ),
                    mock.patch.object(
                        flush, "maybe_trigger_compile", return_value=False
                    ),
                    mock.patch.object(
                        hook, "inspect_worker_queue",
                        return_value={"counts": {"pending": 1}},
                    ),
                    mock.patch.object(hook, "ensure_supervisor", supervisor),
                    mock.patch.object(
                        hook, "count_orphan_hook_inputs", return_value=0,
                        create=True,
                    ),
                ],
            )
        self.assertEqual(code, 0)
        supervisor.assert_called()

    def test_user_prompt_marks_session_only(self) -> None:
        marker = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            code, _out, _err = self._main(
                "user-prompt",
                {"session_id": "s", "prompt": "Bu konuşmada kalsın.",
                 "cwd": str(temporary)},
                [
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "VAULT_ROOT", Path(temporary)),
                    mock.patch.object(
                        hook, "handle_user_prompt", return_value=""
                    ),
                    mock.patch.object(hook, "mark_session_only", marker),
                ],
            )
        self.assertEqual(code, 0)
        marker.assert_called_once()

    def test_pre_compact_enqueues_flush(self) -> None:
        enqueue = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            code, _out, _err = self._main(
                "pre-compact", {"session_id": "s", "cwd": str(temporary)},
                [
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "VAULT_ROOT", Path(temporary)),
                    mock.patch.object(hook, "enqueue_flush", enqueue),
                ],
            )
        self.assertEqual(code, 0)
        enqueue.assert_called_once()

    def test_turn_end_requires_transcript_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            code, _out, _err = self._main(
                "turn-end", {"session_id": "s", "cwd": str(temporary)},
                [
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "VAULT_ROOT", Path(temporary)),
                ],
            )
        self.assertEqual(code, 0)


class CompanionArms(unittest.TestCase):
    def test_safe_path_rejects_junction_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gercek = root / "gercek"
            gercek.mkdir()
            if not _junction(root / "link", gercek):
                self.skipTest("junction oluşturulamadı")
            with self.assertRaises(ValueError):
                companion_memory._safe_path(root, ("link", "x.md"), "hata")

    def test_reflection_directory_junction_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            gercek = state / "gercek"
            gercek.mkdir()
            if not _junction(state / "reflection-requests", gercek):
                self.skipTest("junction oluşturulamadı")
            with self.assertRaises(ValueError):
                companion_memory._reflection_path(state, "s")

    def test_block_matches_bad_timestamp_falls_through(self) -> None:
        payload = (
            b"<!-- cevo-auto-session 1.2.3 " + b"a" * 64 + b" -->\n"
            b"govde\n<!-- /cevo-auto-session -->"
        )
        expected = {"x": (NOW, "a" * 64, "deger")}
        self.assertEqual(
            companion_memory._block_matches(payload, expected), []
        )

    def test_records_block_shape_arms(self) -> None:
        blok = (
            companion_memory.BEGIN + "1.0 " + "a" * 64 + " -->\nx\n"
            + companion_memory.END
        )
        with self.assertRaises(ValueError):
            companion_memory._records(blok + "\n" + blok)
        self.assertEqual(companion_memory._records(blok), {})

    def test_visible_skips_unparseable_summary(self) -> None:
        self.assertEqual(
            companion_memory._visible(
                {"id": (NOW, "a" * 64, "bozuk özet")}, frozenset()
            ),
            {},
        )

    def test_raw_views_suppression_and_manual_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / "🔮 850-Companion" / "Sources"
            sources.mkdir(parents=True)
            (sources / "Threads.md").write_bytes(
                b"onsoz "
                + companion_memory.source_marker("Threads.md")
                + b" sonsoz"
            )
            son = set(memory_ledger.memory_syntactic_units(
                "🔮 850-Companion/Last-Session.md"
            ))
            digerleri = set(memory_ledger.memory_syntactic_units(
                "🔮 850-Companion/Threads.md"
            )) | set(memory_ledger.memory_syntactic_units(
                "🔮 850-Companion/Journal.md"
            ))
            hashes = frozenset(
                memory_ledger.memory_text_hash(unit)
                for unit in son - digerleri
            )
            self.assertTrue(hashes)
            views = companion_memory._raw_views(root, hashes)
        self.assertNotIn("Last-Session.md", views)
        self.assertIn("Threads.md", views)
        self.assertIn("onsoz", views["Threads.md"])

    def test_reconcile_skips_suppressed_last_session(self) -> None:
        hashes = frozenset(
            memory_ledger.memory_text_hash(unit)
            for unit in memory_ledger.memory_syntactic_units(
                "🔮 850-Companion/Last-Session.md"
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            companion_memory._reconcile_current(
                Path(temporary), {}, hashes
            )

    def test_migrate_requires_manual_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "🔮 850-Companion").mkdir()
            canonical = root / "daily" / companion_memory.CANONICAL_RELATIVE.name
            canonical.parent.mkdir()
            canonical.write_text("", encoding="utf-8")
            with mock.patch.object(
                companion_memory, "_load_catalog",
                return_value=({}, {}, True),
            ):
                with self.assertRaises(ValueError) as scope:
                    companion_memory.migrate(root)
            self.assertEqual(
                str(scope.exception), "companion-manual-source-missing"
            )

    def test_publish_normalizes_naive_event(self) -> None:
        summary = "\n".join(
            f"## {section}\nKalıcı özet." for section in flush.EXPECTED_SECTIONS
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "🔮 850-Companion").mkdir()
            state = root / ".codex/scripts/.state"
            state.mkdir(parents=True)
            companion_memory.publish(
                root, state, summary,
                dt.datetime(2026, 9, 11, 10, 0),
                "a" * 64, "s", frozenset(),
            )
            catalog = json.loads(
                (root / companion_memory.CANONICAL_RELATIVE).read_text(encoding="utf-8")
            )
            record = catalog["records"][companion_memory.session_scope("s")]
            self.assertEqual(record["event"], NOW.isoformat())
            self.assertEqual(record["key"], "a" * 64)


class LedgerArms(unittest.TestCase):
    def test_balanced_value_mismatch(self) -> None:
        self.assertIsNone(memory_ledger._balanced_value_end("(]", 0))

    def test_json_region_recursion_error_classified(self) -> None:
        fake = mock.Mock()
        fake.raw_decode.side_effect = RecursionError
        with mock.patch.object(
            memory_ledger.json, "JSONDecoder", return_value=fake
        ):
            with self.assertRaises(MemoryPreferenceError):
                list(memory_ledger._json_regions("[1]"))


class AttachmentArms(unittest.TestCase):
    def test_note_missing_source_marker_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note = vault / "not.md"
            digest = "b" * 64
            text = (
                f"source_sha256: {digest}\n"
                "source_attachment: ek.md\n"
                + attachment_memory.SUMMARY_BODY
                + "özet\n"
                + attachment_memory.SOURCE_BODY
                + "kaynak\n```"
            )
            note.write_text(text, encoding="utf-8", newline="\n")
            with self.assertRaises(ValueError):
                attachment_memory._read_note(
                    note, vault, expected_hash=None,
                    source_digest=digest, source_attachment="ek.md",
                )

    def test_committed_mapping_status_refresh_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _seed(root)
            text = _envelope(source)
            first = _capture(root, text)
            self.assertTrue(first)
            mappings = list(root.rglob("attachment-memory-*.json"))
            self.assertTrue(mappings)
            value = json.loads(mappings[0].read_text(encoding="utf-8"))
            value["status"] = "prepared"
            mappings[0].write_text(json.dumps(value), encoding="utf-8")
            second = _capture(root, text)
            self.assertTrue(second)
            refreshed = json.loads(mappings[0].read_text(encoding="utf-8"))
        self.assertEqual(refreshed["status"], "committed")


if __name__ == "__main__":
    unittest.main()
