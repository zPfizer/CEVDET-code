"""hook.py'nin kapsam korkulukları, direktif dalları ve fail-open yolları."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import hook
from memory_ledger import mark_read_only_turn, is_read_only_turn


def _retrieval(text: str = "", outcome: str = "ok"):
    return types.SimpleNamespace(
        outcome=outcome, entries=1, hits=1, paths=("daily/x.md",),
        text=text, budget=1000,
    )


def _run_prompt(vault: Path, state: Path, prompt: str, session: str = "oturum",
                retrieval=None, **payload_extra) -> str:
    payload = {"session_id": session, "prompt": prompt, "cwd": str(vault)}
    payload.update(payload_extra)
    target = retrieval if retrieval is not None else _retrieval()
    side = target if callable(target) else (lambda *a, **k: target)
    with mock.patch.object(hook, "retrieve_vault_context_detailed", side_effect=side):
        return hook.handle_user_prompt(payload, state, vault_root=vault, now=1.0)


class DeadlineAndScopeGuards(unittest.TestCase):
    def test_hook_deadline_env_variants(self) -> None:
        with mock.patch.dict("os.environ", {"CEVO_HOOK_DEADLINE": "bozuk"}):
            self.assertGreater(hook._hook_deadline("session-start"), time.monotonic())
        with mock.patch.dict("os.environ", {"CEVO_HOOK_DEADLINE": "-1"}):
            self.assertLessEqual(hook._hook_deadline("session-start"), time.monotonic())
        near = time.monotonic() + 0.5
        with mock.patch.dict("os.environ", {"CEVO_HOOK_DEADLINE": str(near)}):
            self.assertAlmostEqual(hook._hook_deadline("session-start"), near, delta=0.2)

    def test_validate_hook_scope_failure_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            other = Path(temporary) / "baska"
            other.mkdir()
            cases = [
                {"cwd": 123},
                {"cwd": "göreli/yol"},
                {"cwd": str(Path(temporary) / "yok")},
                {"cwd": str(other)},
            ]
            for payload in cases:
                with self.subTest(payload=payload):
                    with self.assertRaises(hook.HookScopeError):
                        hook._validate_hook_scope(payload)

            current = {"cwd": str(Path.cwd())}
            with mock.patch.object(
                hook.subprocess, "run",
                side_effect=hook.subprocess.SubprocessError("git yok"),
            ):
                with self.assertRaises(hook.HookScopeError):
                    hook._validate_hook_scope(current)
            fail = types.SimpleNamespace(returncode=128, stdout="")
            with mock.patch.object(hook.subprocess, "run", return_value=fail):
                with self.assertRaises(hook.HookScopeError):
                    hook._validate_hook_scope(current)
            ghost = types.SimpleNamespace(
                returncode=0, stdout=str(Path(temporary) / "hayalet")
            )
            with mock.patch.object(hook.subprocess, "run", return_value=ghost):
                with self.assertRaises(hook.HookScopeError):
                    hook._validate_hook_scope(current)
            foreign = types.SimpleNamespace(returncode=0, stdout=str(other))
            with mock.patch.object(hook.subprocess, "run", return_value=foreign):
                with self.assertRaises(hook.HookScopeError):
                    hook._validate_hook_scope(current)


class ReaderHelpers(unittest.TestCase):
    def test_source_text_and_limited_edges(self) -> None:
        self.assertIsNone(hook._read_source_text(None))
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "not.md"
            target.write_text("içerik", encoding="utf-8")
            with mock.patch.object(Path, "read_text", side_effect=OSError):
                self.assertIsNone(hook._read_source_text(target))
            fake_memory = types.SimpleNamespace(
                read_source=lambda _p: (None, None), active=False
            )
            self.assertIsNone(hook._read_source_text(target, fake_memory))
            open_front = Path(temporary) / "acik.md"
            open_front.write_text("---\ntitle: X\ngövde yok", encoding="utf-8")
            self.assertIn("title", hook._read_limited(open_front, None))

    def test_profile_card_warns_when_source_unavailable(self) -> None:
        fake_memory = types.SimpleNamespace(
            excludes=lambda _v: False,
            profile_issues=lambda: (),
            read_source=lambda _p: (None, None),
            active=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            warning = hook._profile_card(
                Path(temporary) / "Profil.md", Path(temporary), fake_memory
            )
        self.assertIn("Profil kontrolü başarısız", warning)

    def test_compact_knowledge_index_budgets(self) -> None:
        rows = "\n".join(
            f"| [[concepts/k{i}\\|Kavram {i}]] | Uzun özet metni {i} | k | 2026-09-0{i%9+1} |"
            for i in range(12)
        )
        text = "# Bilgi İndeksi\n| Makale |\n| bozuk |\n" + rows + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.md"
            path.write_text(text, encoding="utf-8")
            compact = hook._compact_knowledge_index(path, 180)
            wide = hook._compact_knowledge_index(path, 5000)
        self.assertIn("12 kayıt", compact)
        self.assertIn("Kavram 0", wide)

    def test_last_session_and_journal_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plain = Path(temporary) / "duz.md"
            plain.write_text("işaretsiz metin", encoding="utf-8")
            self.assertEqual(hook._last_session(plain), "")
            journal = hook._last_journal(plain)
        self.assertEqual(journal, "")


class PromptHandlingBranches(unittest.TestCase):
    def _vault(self, temporary: Path) -> tuple[Path, Path]:
        vault = temporary / "vault"
        (vault / "daily").mkdir(parents=True)
        state = temporary / "state"
        state.mkdir()
        return vault, state

    def test_prompt_count_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "conversation.json"
            record.write_text("{bozuk", encoding="utf-8")
            with self.assertRaises(ValueError):
                hook._increment_prompt_count(record)
            record.write_text("[1]", encoding="utf-8")
            with self.assertRaises(ValueError):
                hook._increment_prompt_count(record)

    def test_missing_session_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            self.assertEqual(
                hook.handle_user_prompt({"prompt": "x"}, state, vault_root=vault), ""
            )

    def test_directive_messages(self) -> None:
        cases = [
            ("Bu bilgiyi düzelt: toplantı salı günü.", "[Hafıza Düzeltmesi]"),
            ("Kırmızı Rota kararını unut.", "[Hafıza Unutma] "),
            ("Bunu unut.", "hedefi mevcut konuşmadan çöz"),
            ("Bunu kaydetme.", "kalıcı hafızaya alınmayacak"),
            ("şifrem: gizli-deger-123", "Gizli değer kalıcı hafızaya alınmadı"),
            ("Benim hakkımda ne biliyorsun?", "[Hafıza Görünümü]"),
        ]
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                with tempfile.TemporaryDirectory() as temporary:
                    vault, state = self._vault(Path(temporary))
                    output = _run_prompt(vault, state, prompt)
                self.assertIn(expected, output)

    def test_read_only_scope_variants(self) -> None:
        scoped = [
            ("Bu bilgiyi düzelt: toplantı salı.", "düzeltme kaydı bu turda"),
            ("Kırmızı Rota kararını unut.", "Salt okunur kapsam sürüyor"),
        ]
        for prompt, expected in scoped:
            with self.subTest(prompt=prompt):
                with tempfile.TemporaryDirectory() as temporary:
                    vault, state = self._vault(Path(temporary))
                    mark_read_only_turn(state, "oturum")
                    output = _run_prompt(vault, state, prompt)
                self.assertIn(expected, output)

        requested = [
            ("Salt okunur: Kırmızı Rota kararını unut.", "unutma kaydı bu turda"),
            ("Unut bunu; salt okunur kal.", "netleştirme ve"),
            ("Salt okunur: Bunu kaydetme.", "mevcut kaynak ve türetilmiş notlara dokunma"),
        ]
        for prompt, expected in requested:
            with self.subTest(prompt=prompt):
                with tempfile.TemporaryDirectory() as temporary:
                    vault, state = self._vault(Path(temporary))
                    output = _run_prompt(vault, state, prompt)
                self.assertIn(expected, output)

    def test_retrieval_failure_classes(self) -> None:
        from memory_ledger import MemoryPreferenceError

        cases = [
            (MemoryPreferenceError("memory-publication-pending"), "yayın", True),
            (MemoryPreferenceError("memory-suppression-invalid"), "Unutma tercihleri", True),
            (OSError("vault-retrieval-incomplete"), "kaynak tutarlılığı", False),
            (OSError("disk arızası"), "Vault araması tamamlanamadı", False),
        ]
        for error, expected, _returns in cases:
            with self.subTest(error=str(error)):
                with tempfile.TemporaryDirectory() as temporary:
                    vault, state = self._vault(Path(temporary))

                    def patlayan(*_a, **_k):
                        raise error

                    output = _run_prompt(
                        vault, state, "Atlas kararları neydi?", retrieval=patlayan
                    )
                self.assertIn(expected, output)

    def test_retrieval_telemetry_failure_is_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state = self._vault(Path(temporary))
            with mock.patch.object(hook, "atomic_write", side_effect=OSError):
                output = _run_prompt(
                    vault, state, "Atlas kararları neydi?",
                    retrieval=_retrieval(text="[Hafıza: Bulgular]\nkayıt"),
                )
        self.assertIn("Bulgular", output)

    def test_queue_and_payload_helpers(self) -> None:
        self.assertEqual(hook._unresolved_terminal_count(None), 0)
        self.assertEqual(
            hook._unresolved_terminal_count({"counts": {"dead-letter": 5}}), 5
        )
        self.assertEqual(hook._quarantined_count(None), 0)
        self.assertFalse(hook._has_current_flush_error("metin"))
        self.assertTrue(
            hook._has_current_flush_error(
                {"component": "flush", "status": "error", "error": "x"}
            )
        )
        fake_stdin = io.StringIO(json.dumps([1]))
        with mock.patch.object(sys, "stdin", fake_stdin):
            with self.assertRaises(ValueError):
                hook._load_payload()


class MainFailOpenPaths(unittest.TestCase):
    def _main(self, event: str, payload, *, patches=(), argv_extra=()):
        stdout = io.StringIO()
        stderr = io.StringIO()
        stack = [
            mock.patch.object(sys, "stdin", io.StringIO(
                payload if isinstance(payload, str) else json.dumps(payload)
            )),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
        ]
        stack.extend(patches)
        with mock.ExitStack() if False else _enter_all(stack):
            code = hook.main([event, *argv_extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_scope_error_and_invalid_input(self) -> None:
        code, _out, err = self._main(
            "session-start", {"cwd": "x"},
            patches=[mock.patch.object(
                hook, "_validate_hook_scope",
                side_effect=hook.HookScopeError("kapsam"),
            )],
        )
        self.assertEqual(code, 1)
        self.assertIn("kapsam", err)

        code, _out, err = self._main("session-start", "{bozuk")
        self.assertEqual(code, 1)
        self.assertIn("doğrulanamadı", err)

    def test_user_prompt_late_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            code, out, _err = self._main(
                "user-prompt", {"session_id": "s", "prompt": "x", "cwd": "."},
                patches=[
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(
                        hook, "handle_user_prompt", side_effect=RuntimeError("çök")
                    ),
                    mock.patch.object(
                        hook, "write_hook_health", side_effect=OSError
                    ),
                ],
            )
        self.assertEqual(code, 0)
        self.assertIn("Haf\\u0131za", out)

    def test_turn_end_without_session_reports_system_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            code, out, _err = self._main(
                "turn-end", {"cwd": "."},
                patches=[
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "STATE_DIR", state),
                ],
            )
        self.assertEqual(code, 0)
        self.assertIn("systemMessage", out)

    def test_session_end_partial_cleanup_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            code, _out, _err = self._main(
                "session-end", {"session_id": "s", "cwd": "."},
                patches=[
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(
                        hook, "enqueue_flush", side_effect=OSError("kuyruk")
                    ),
                ],
            )
        self.assertEqual(code, 0)

    def test_explicit_write_intent_clears_read_only_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            mark_read_only_turn(state, "s")
            code, _out, _err = self._main(
                "user-prompt",
                {"session_id": "s", "cwd": ".",
                 "prompt": "notlar.md dosyasını güncelle."},
                patches=[
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "handle_user_prompt", return_value=""),
                ],
            )
            cleared = not is_read_only_turn(state, "s")
        self.assertEqual(code, 0)
        self.assertTrue(cleared)

    def test_forget_prompt_triggers_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            suppressor = mock.Mock()
            code, _out, _err = self._main(
                "user-prompt",
                {"session_id": "s", "cwd": ".",
                 "prompt": "Kırmızı Rota kararını unut."},
                patches=[
                    mock.patch.object(hook, "_validate_hook_scope"),
                    mock.patch.object(hook, "STATE_DIR", state),
                    mock.patch.object(hook, "handle_user_prompt", return_value=""),
                    mock.patch.object(hook, "suppress_derived_memory", suppressor),
                ],
            )
        self.assertEqual(code, 0)
        suppressor.assert_called_once()


class _enter_all:
    def __init__(self, managers):
        self._managers = managers
        self._entered = []

    def __enter__(self):
        for manager in self._managers:
            self._entered.append(manager.__enter__())
        return self

    def __exit__(self, *exc):
        result = False
        for manager in reversed(self._managers):
            result = manager.__exit__(*exc) or result
        return result


if __name__ == "__main__":
    unittest.main()
