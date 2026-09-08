from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR

import compile as memory_compile  # noqa: E402
import daily_store  # noqa: E402
import flush  # noqa: E402
import hook  # noqa: E402
import knowledge_schema  # noqa: E402
import memory_ledger  # noqa: E402
import vault_retrieval  # noqa: E402
import user_evidence  # noqa: E402


def _decision_proof(day: str, claim: str) -> tuple[str, str]:
    sections = {name: '' for name in flush.EXPECTED_SECTIONS}
    sections['Alınan Kararlar'] = '- ' + claim + ' <!-- user-source: ' + json.dumps({'quote': claim, 'scope': 'project'}, ensure_ascii=False) + ' -->'
    bound = user_evidence.bind_evidence(sections, [('user', claim)], day + 'T00:00:00+00:00')
    proof = bound['Önemli Konuşmalar']
    return proof, json.loads(user_evidence.EVIDENCE.search(proof)[1])['id']


def _seed_vault(vault: Path) -> None:
    companion = vault / "🔮 850-Companion"
    projects = vault / "🏰 300-Projects"
    human = vault / "🧠 500-Knowledge"
    knowledge = vault / "knowledge"
    daily = vault / "daily"
    state = vault / ".codex" / "scripts" / ".state"
    for path in (
        companion,
        projects,
        human,
        knowledge / "concepts",
        knowledge / "connections",
        daily,
        state,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (companion / "Core.md").write_text(
        "# Cevo\nLevent'in düşünme ortağı.\n", encoding="utf-8"
    )
    (companion / "Profile.md").write_text(
        "# Profil\nDoğrudan ve kısa konuş.\n", encoding="utf-8"
    )
    (companion / "Last-Session.md").write_text(
        "# Son Oturum\n\n## Session: dün\n"
        "Atlas için yerel önbellek seçildi; gerekçe çevrimdışı devamlılık.\n",
        encoding="utf-8",
    )
    (companion / "Kurallar.md").write_text(
        "# Kurallar\nLevent doğal konuşur; şablon isteme.\n", encoding="utf-8"
    )
    (companion / "Journal.md").write_text(
        "# Journal\n\n## Güncel\n\n### 2026-09-04\nAtlas kararı geçerli.\n",
        encoding="utf-8",
    )
    (projects / "Atlas.md").write_text(
        "---\ntitle: Atlas\ntags: [hafıza, doğrulama]\n---\n"
        "# Atlas\n\nKarar: yerel önbellek.\n"
        "Gerekçe: çevrimdışı devamlılık.\nMevcut durum: doğrulandı.\n"
        "Açık soru: kapasite sınırı.\n",
        encoding="utf-8",
    )
    (human / "Kullanıcı Notu.md").write_text(
        "# Kullanıcı Notu\n\nBu içerik kullanıcıya aittir ve korunur.\n",
        encoding="utf-8",
    )
    (knowledge / "index.md").write_text(
        "# Bilgi Tabanı: İndeks\n\n"
        "| Makale | Özet | Kaynak | Güncellendi |\n"
        "| --- | --- | --- | --- |\n",
        encoding="utf-8",
    )
    (knowledge / "log.md").write_text("# Derleme Günlüğü\n", encoding="utf-8")
    (daily / "2026-09-03.md").write_text(
        "# Günlük Log: 2026-09-03\n\nKaynak: https://example.com/makale\n"
        "Karar: yerel hafıza A düzenini kullansın. Gerekçe: basitlik.\n",
        encoding="utf-8",
    )
    (daily / "2026-09-04.md").write_text(
        "# Günlük Log: 2026-09-04\n\nDüzeltme: yerel hafıza B düzenini kullansın. "
        "Gerekçe: geri çağırma doğruluğu.\n",
        encoding="utf-8",
    )
    for day, claim in (
        ('2026-09-03', 'Yerel hafıza A düzenini kullanır; gerekçe basitlik.'),
        ('2026-09-04', 'Yerel hafıza B düzenini kullanır; gerekçe geri çağırma doğruluğu.'),
    ):
        proof, _ = _decision_proof(day, claim)
        with (daily / (day + '.md')).open('a', encoding='utf-8') as target:
            target.write('\n' + proof + '\n')
    shutil.copy2(
        CODEX_DIR / "tag-taxonomy.json",
        vault / ".codex" / "tag-taxonomy.json",
    )


def _concept(
    title: str,
    sources: tuple[str, ...],
    claims: tuple[str, ...],
    related_slug: str,
) -> str:
    created = sources[0].removesuffix(".md")
    updated = sources[-1].removesuffix(".md")
    source_links = "\n".join(
        f"- [[daily/{source.removesuffix('.md')}|Kaynak]]" for source in sources
    )
    return f"""---
schema: knowledge-v2
title: {title}
aliases: []
tags: [hafıza]
sources: [{', '.join(sources)}]
created: {created}
updated: {updated}
---
# {title}

Kaynaklı ve yeniden üretilebilir türev bilgi. Kullanıcı notları ayrı kalır.

## Önemli Noktalar

- Kaynak korunur.
- Tarih korunur.
- Bilgi niteliği korunur.

## Detaylar

Derleme yalnız günlük kaynaklarını kullanır.

## Kayıtlar

{chr(10).join(claims)}

## İlgili Kavramlar

- [[knowledge/concepts/{related_slug}|İlgili kavram]] bu kayıtla bağlantılıdır.
- [[knowledge/index|Bilgi indeksi]] kaydı bulunabilir yapar.

## Kaynaklar

{source_links}
"""


def _write_derived_tree(stage: Path, latest: bool) -> None:
    knowledge = stage / "knowledge"
    concepts = knowledge / "concepts"
    connections = knowledge / "connections"
    concepts.mkdir(parents=True, exist_ok=True)
    connections.mkdir(parents=True, exist_ok=True)
    local_sources = (
        ("2026-09-03.md", "2026-09-04.md")
        if latest
        else ("2026-09-03.md",)
    )
    old_state = "gecmis" if latest else "gecerli"
    old_freshness = "eski" if latest else "guncel"
    _, old_proof = _decision_proof('2026-09-03', 'Yerel hafıza A düzenini kullanır; gerekçe basitlik.')
    _, new_proof = _decision_proof('2026-09-04', 'Yerel hafıza B düzenini kullanır; gerekçe geri çağırma doğruluğu.')
    local_claims = [
        f"- `{old_state}` `kullanici-dusuncesi` `{old_freshness}` 2026-09-03 "
        f"[[daily/2026-09-03#user-{old_proof}|Kaynak]] — Yerel hafıza A düzenini kullanır; gerekçe basitlik."
    ]
    if latest:
        local_claims.append(
            "- `gecerli` `kullanici-dusuncesi` `guncel` 2026-09-04 "
            f"[[daily/2026-09-04#user-{new_proof}|Kaynak]] — Yerel hafıza B düzenini kullanır; "
            "gerekçe geri çağırma doğruluğu."
        )
    (concepts / "yerel-hafiza.md").write_text(
        _concept(
            "Yerel Hafıza",
            local_sources,
            tuple(local_claims),
            "kaynakli-sentez",
        ),
        encoding="utf-8",
    )
    (concepts / "kaynakli-sentez.md").write_text(
        _concept(
            "Kaynaklı Sentez",
            ("2026-09-03.md",),
            (
                "- `gecerli` `dis-gorus` `belirsiz` 2026-09-03 "
                "[[daily/2026-09-03|Kaynak]] — https://example.com/makale "
                "yerel hafıza yöntemini öneriyor.",
            ),
            "yerel-hafiza",
        ),
        encoding="utf-8",
    )
    connection_sources = local_sources
    connection_links = "\n".join(
        f"- [[daily/{source.removesuffix('.md')}|Kaynak]]"
        for source in connection_sources
    )
    (connections / "kaynakli-sentez--yerel-hafiza.md").write_text(
        f"""---
schema: knowledge-v2
connects: [kaynakli-sentez, yerel-hafiza]
sources: [{', '.join(connection_sources)}]
updated: {connection_sources[-1].removesuffix('.md')}
---
# Kaynaklı Sentez ve Yerel Hafıza

## Bağlantı

[[knowledge/concepts/kaynakli-sentez|Kaynaklı Sentez]] ↔ [[knowledge/concepts/yerel-hafiza|Yerel Hafıza]]

## Ana Fikir

Dış görüş ile kullanıcının tarihli kararı ayrı niteliklerle bağlanır.

## Kaynaklar

{connection_links}
""",
        encoding="utf-8",
    )
    (knowledge / "index.md").write_text(
        "# Bilgi Tabanı: İndeks\n\n"
        "| Makale | Özet | Kaynak | Güncellendi |\n"
        "| --- | --- | --- | --- |\n"
        "| [[concepts/kaynakli-sentez\\|Kaynaklı Sentez]] | Kaynaklı dış görüş. | 2026-09-03.md | 2026-09-03 |\n"
        f"| [[concepts/yerel-hafiza\\|Yerel Hafıza]] | Tarihli karar. | {connection_sources[-1]} | {connection_sources[-1].removesuffix('.md')} |\n",
        encoding="utf-8",
    )
    log_blocks = [
        "## [2026-09-03T00:00:00+00:00] compile | 2026-09-03.md\n"
        "Oluşturulan: kaynakli-sentez, yerel-hafiza.\n"
        "Güncellenen: yok.\n"
    ]
    if latest:
        log_blocks.append(
            "## [2026-09-04T00:00:00+00:00] compile | 2026-09-04.md\n"
            "Oluşturulan: yok.\nGüncellenen: yerel-hafiza.\n"
        )
    (knowledge / "log.md").write_text(
        "# Derleme Günlüğü\n\n" + "\n".join(log_blocks), encoding="utf-8"
    )


def _deterministic_compiler(_prompt: str, stage: Path) -> str | None:
    daily_name = next((stage / "daily").glob("*.md")).name
    _write_derived_tree(stage, latest=daily_name == "2026-09-04.md")
    return None


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path) -> str:
    rows = [
        f"{path.relative_to(root).as_posix()}|{_digest(path)}"
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    ]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


class SecondBrainAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self._scope_guard = mock.patch.object(hook, '_validate_hook_scope')
        self._scope_guard.start()
        self.addCleanup(self._scope_guard.stop)

    def test_examples_01_03_08_09_use_automatic_bounded_vault_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state = vault / ".codex" / "scripts" / ".state"
            project = vault / "🏰 300-Projects" / "Atlas.md"
            project_before = _digest(project)

            startup = hook.build_session_context(vault, state)
            missing = hook.handle_user_prompt(
                {"session_id": "missing", "prompt": "Mars serası oksijen kararı nedir?"},
                state,
                vault_root=vault,
                now=1,
            )
            project_context = hook.handle_user_prompt(
                {
                    "session_id": "project",
                    "prompt": "Atlas yerel önbellek kararını, gerekçesini ve açık soruyu özetle",
                },
                state,
                vault_root=vault,
                now=2,
            )
            project_after = _digest(project)

        self.assertIn("yerel önbellek seçildi", startup)
        self.assertIn("çevrimdışı devamlılık", startup)
        self.assertIn("Bilgi yoksa veya eskiyse boşluğu söyle", missing)
        self.assertIn("BELİRSİZ bırak", missing)
        self.assertIn("Atlas.md", project_context)
        self.assertIn("yerel önbellek", project_context)
        self.assertIn("Yanıt biçimini kullanıcının isteğine göre seç", project_context)
        self.assertNotIn("Kararlar / Gerekçeler / Mevcut durum / Açık sorular", project_context)
        self.assertEqual(project_before, project_after)
        self.assertIn("Levent'ten şablon isteme", project_context)
        for removed in ("work packet", "permit", "forensic", "mem0", "session access"):
            self.assertNotIn(removed, f"{startup}\n{missing}\n{project_context}".casefold())

    def test_examples_04_06_merge_sessions_and_queue_early_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            state = vault / ".codex" / "scripts" / ".state"
            now = dt.datetime.fromisoformat("2026-09-04T12:00:00+03:00")
            first_key = "a" * 64
            second_key = "b" * 64
            daily_store.publish(
                vault,
                state,
                "## Bağlam\nBirinci oturum kararı.",
                "sessionend",
                now,
                idempotency_key=first_key,
            )
            daily_store.publish(
                vault,
                state,
                "## Bağlam\nBirinci oturum kararı.",
                "sessionend",
                now,
                idempotency_key=first_key,
            )
            daily_store.publish(
                vault,
                state,
                "## Bağlam\nİkinci oturum gerekçesi.",
                "sessionend",
                now,
                idempotency_key=second_key,
            )
            merged = (vault / "daily" / "2026-09-04.md").read_text(encoding="utf-8")

            transcript = state / "rollout.jsonl"
            transcript.write_text(
                json.dumps(
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "Kalıcı karar"}}
                ),
                encoding="utf-8",
            )
            payload = {
                "session_id": "early-capture",
                "cwd": str(vault),
                "prompt": "Kararım: haftalık plan pazartesi yapılacak.",
                "transcript_path": str(transcript),
            }
            real_handle = hook.handle_user_prompt
            real_enqueue = hook.enqueue_flush
            launcher = mock.Mock()

            def handle(input_payload: dict[str, object], input_state: Path, **kwargs: object) -> str:
                kwargs.setdefault("vault_root", vault)
                kwargs.setdefault("now", 3)
                return real_handle(input_payload, input_state, **kwargs)

            def enqueue(input_payload: dict[str, object], reason: str, **kwargs: object) -> Path:
                kwargs.setdefault("popen_factory", launcher)
                return real_enqueue(input_payload, reason, **kwargs)

            with (
                mock.patch.object(hook, "VAULT_ROOT", vault),
                mock.patch.object(hook, "STATE_DIR", state),
                mock.patch.object(hook, "handle_user_prompt", side_effect=handle),
                mock.patch.object(hook, "enqueue_flush", side_effect=enqueue),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                exit_code = hook.main(["user-prompt"])
            pending = list((state / "worker-jobs" / "pending").glob("*.json"))

        self.assertEqual(merged.count("Birinci oturum kararı"), 1)
        self.assertEqual(merged.count("İkinci oturum gerekçesi"), 1)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(pending), 1)
        launcher.assert_called_once()

    def test_examples_02_07_rebuild_sources_links_and_conflict_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            output = root / "knowledge"
            _seed_vault(vault)

            created = memory_compile.rebuild_knowledge(
                vault,
                output,
                runner=_deterministic_compiler,
            )
            first_digest = _tree_digest(output)
            repeated = memory_compile.rebuild_knowledge(
                vault,
                output,
                runner=_deterministic_compiler,
            )
            repeated_digest = _tree_digest(output)
            report = knowledge_schema.validate_knowledge_tree(root)
            local = (output / "concepts" / "yerel-hafiza.md").read_text(encoding="utf-8")
            sourced = (output / "concepts" / "kaynakli-sentez.md").read_text(encoding="utf-8")
            connection = output / "connections" / "kaynakli-sentez--yerel-hafiza.md"
            connection_exists = connection.is_file()

        self.assertEqual(created, "created")
        self.assertEqual(repeated, "unchanged")
        self.assertEqual(first_digest, repeated_digest)
        self.assertEqual(report.issues, ())
        self.assertIn("https://example.com/makale", sourced)
        self.assertIn("[[daily/2026-09-03|Kaynak]]", sourced)
        self.assertTrue(connection_exists)
        self.assertIn("`gecmis`", local)
        self.assertIn("gerekçe basitlik", local)
        self.assertIn("`gecerli`", local)
        self.assertIn("gerekçe geri çağırma doğruluğu", local)

    def test_example_05_applies_forget_and_correction_without_touching_human_note(self) -> None:
        forgotten = "Levent Ankara'da yaşıyor"
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            _seed_vault(vault)
            derived = vault / "knowledge" / "concepts" / "profil.md"
            human = vault / "🧠 500-Knowledge" / "Profil Notum.md"
            derived.write_text(f"# Profil\n\n- {forgotten}\n", encoding="utf-8")
            human.write_text(f"# Profil Notum\n\n{forgotten}\n", encoding="utf-8")
            human_before = _digest(human)
            memory_ledger.suppress_derived_memory(
                vault / ".codex" / "private-memory",
                forgotten,
                now=1,
            )
            result = vault_retrieval.retrieve_vault_context_detailed(
                vault,
                "Levent Ankara yaşıyor",
                write_cache=False,
            )
            correction = hook.handle_user_prompt(
                {
                    "session_id": "correction",
                    "prompt": "Tercihimi B olarak düzelt.",
                },
                vault / ".codex" / "scripts" / ".state",
                vault_root=vault,
                now=2,
            )
            human_after = _digest(human)

        self.assertEqual(result.paths, ())
        self.assertEqual(human_before, human_after)
        self.assertIn("geçmiş olarak koru", correction)
        self.assertIn("yeni geçerli kayıt", correction)

    def test_example_10_rebuild_does_not_mutate_user_content_or_live_derived_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            output = root / "knowledge"
            _seed_vault(vault)
            human = vault / "🧠 500-Knowledge" / "Kullanıcı Notu.md"
            live_knowledge = vault / "knowledge"
            before = (_digest(human), _tree_digest(live_knowledge))

            memory_compile.rebuild_knowledge(
                vault,
                output,
                runner=_deterministic_compiler,
            )

            after = (_digest(human), _tree_digest(live_knowledge))
            output_exists = (output / "index.md").is_file()

        self.assertEqual(before, after)
        self.assertTrue(output_exists)

    def test_rebuild_preserves_drift_and_rejects_a_vault_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            output = root / "knowledge"
            _seed_vault(vault)
            with self.assertRaisesRegex(
                memory_compile.PolicyError,
                "rebuild-output-inside-vault",
            ):
                memory_compile.rebuild_knowledge(
                    vault,
                    vault / "rebuilt",
                    runner=_deterministic_compiler,
                )
            memory_compile.rebuild_knowledge(
                vault,
                output,
                runner=_deterministic_compiler,
            )
            index = output / "index.md"
            index.write_text(index.read_text(encoding="utf-8") + "\nlocal drift\n", encoding="utf-8")
            drifted = _tree_digest(output)

            with self.assertRaisesRegex(
                memory_compile.PolicyError,
                "rebuild-output-drift",
            ):
                memory_compile.rebuild_knowledge(
                    vault,
                    output,
                    runner=_deterministic_compiler,
                )

            after = _tree_digest(output)

        self.assertEqual(drifted, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
