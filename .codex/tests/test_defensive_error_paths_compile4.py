"""compile.py — dilim 4: checkpoint uçları, terfi/kurtarma matrisi, koşu kuyruğu."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import compile as memory_compile
import compile_state
from compile_state import CompileState

DIGEST = "a" * 64
TAXONOMY = Path(__file__).resolve().parents[1] / "tag-taxonomy.json"


def _r(code=0, out=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr="")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compile_vault(temporary: Path) -> tuple[Path, Path, Path]:
    vault = temporary / "vault"
    (vault / "daily").mkdir(parents=True)
    knowledge = vault / "knowledge"
    (knowledge / "concepts").mkdir(parents=True)
    (knowledge / "connections").mkdir()
    (knowledge / "index.md").write_text(
        "# Bilgi Tabanı: İndeks\n\n"
        "| Makale | Özet | Kaynak | Güncellendi |\n"
        "| --- | --- | --- | --- |\n",
        encoding="utf-8",
    )
    (knowledge / "log.md").write_text("# Derleme Günlüğü\n", encoding="utf-8")
    (vault / ".codex").mkdir()
    (vault / ".codex" / "tag-taxonomy.json").write_text(
        TAXONOMY.read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = vault / ".codex/scripts/.state"
    state.mkdir(parents=True)
    daily = vault / "daily" / "2026-09-11.md"
    daily.write_text("Kalıcı karar alındı.\n", encoding="utf-8")
    return vault, state, daily


def _journal(**overrides) -> dict:
    journal = {
        "schema_version": compile_state.PUBLICATION_SCHEMA_VERSION,
        "status": "pending",
        "operation_id": "f" * 32,
        "timestamp": "2026-09-11T10:00:00",
        "source_relative": "daily/2026-09-11.md",
        "source_digest": DIGEST,
        "source_size": 5,
        "suppression_digest": DIGEST,
        "stage": "compile-stage-" + "0" * 32,
        "targets": [
            {"relative": "knowledge/index.md", "before_sha256": None,
             "after_sha256": DIGEST, "completed": False}
        ],
    }
    journal.update(overrides)
    return journal


class SnapshotTailArms(unittest.TestCase):
    def _runner(self, index_path, *, update_ref, verify_limit=None):
        state = {"verify": 0}

        def runner(_root, *args, **_k):
            joined = " ".join(str(a) for a in args)
            if "commit.gpgsign" in joined:
                return _r(1, "")
            if "--git-path" in joined and "hooks" in joined:
                return _r(0, "hooks")
            if "--git-path" in joined and "index" in joined:
                return _r(0, str(index_path))
            if "symbolic-ref" in joined:
                return _r(0, "refs/heads/main")
            if "rev-parse" in joined and "--verify" in joined:
                state["verify"] += 1
                if verify_limit is not None and state["verify"] > verify_limit:
                    raise OSError("ref okunamadı")
                return _r(0, "b" * 40)
            if "diff" in joined and "--cached" in joined and "-z" in args:
                return _r(0, "daily/x.md\0")
            if "diff" in joined and "--cached" in joined:
                return _r(0, "")
            if "ls-files" in joined:
                return _r(0, "daily/x.md\0")
            if "write-tree" in joined:
                return _r(0, "c" * 40)
            if "commit-tree" in joined:
                return _r(0, "d" * 40)
            if "update-ref" in joined:
                return update_ref()
            return _r(0, "")

        return runner

    def test_ref_recovery_probe_failure_defers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            index = vault / "index"
            index.write_bytes(b"indeks")

            def update_ref():
                raise subprocess.SubprocessError("kilit")

            runner = self._runner(index, update_ref=update_ref, verify_limit=1)
            with mock.patch.object(memory_compile, "_git", side_effect=runner):
                status, reason = memory_compile._commit_machine_snapshot(
                    vault, "etiket"
                )
        self.assertEqual((status, reason),
                         ("deferred", "machine-commit-uncertain"))

    def test_index_drift_during_restore_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            index = vault / "index"
            index.write_bytes(b"indeks")

            def update_ref():
                index.write_bytes(b"baska-yazar")
                return _r(1, "")

            runner = self._runner(index, update_ref=update_ref)
            with mock.patch.object(memory_compile, "_git", side_effect=runner):
                with self.assertRaises(OSError):
                    memory_compile._commit_machine_snapshot(vault, "etiket")

    def test_posix_lock_cleanup_arm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            index = vault / "index"
            index.write_bytes(b"indeks")

            def runner(_root, *args, **_k):
                joined = " ".join(str(a) for a in args)
                if "commit.gpgsign" in joined:
                    return _r(1, "")
                if "--git-path" in joined and "hooks" in joined:
                    return _r(0, "hooks")
                if "--git-path" in joined and "index" in joined:
                    return _r(0, str(index))
                if "diff" in joined and "--cached" in joined:
                    return _r(1, "")
                return _r(0, "")

            with (
                mock.patch.object(memory_compile, "_git", side_effect=runner),
                mock.patch.object(memory_compile.os, "name", "posix"),
            ):
                status, reason = memory_compile._commit_machine_snapshot(
                    vault, "etiket"
                )
        self.assertEqual((status, reason),
                         ("deferred", "staged-index-unreadable"))


class StageAndManifestArms(unittest.TestCase):
    def test_stage_name_collision_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            original = Path.mkdir

            def collide(self, *args, **kwargs):
                if self.name.startswith("compile-stage-"):
                    raise FileExistsError(self)
                return original(self, *args, **kwargs)

            with mock.patch.object(Path, "mkdir", collide):
                with self.assertRaises(FileExistsError) as scope:
                    memory_compile._create_stage_directory(state)
        self.assertIn("collision", str(scope.exception))

    def test_manifest_directory_link_and_shape_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "stage"
            root.mkdir()
            hedef = Path(temporary) / "hedef"
            hedef.mkdir()
            try:
                os.symlink(hedef, root / "linkdir", target_is_directory=True)
            except OSError:
                self.skipTest("symlink izni yok")
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._manifest(root)
            self.assertIn("staging-symlink", str(scope.exception))
            (root / "linkdir").rmdir()

            alt = root / "altdir"
            alt.mkdir()
            dosya = Path(temporary) / "dosya"
            dosya.write_text("x", encoding="utf-8")
            sahte = os.lstat(dosya)
            original = Path.lstat

            def fake(self, *args, **kwargs):
                if self.name == "altdir":
                    return sahte
                return original(self, *args, **kwargs)

            with mock.patch.object(Path, "lstat", fake):
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    memory_compile._manifest(root)
            self.assertIn("staging-special", str(scope.exception))
            alt.rmdir()

            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(root / "kacak"),
                 str(hedef)],
                capture_output=True,
            )
            if result.returncode != 0:
                self.skipTest("junction oluşturulamadı")
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._manifest(root)
            self.assertIn("staging-escape", str(scope.exception))
            subprocess.run(
                ["cmd", "/c", "rmdir", str(root / "kacak")],
                capture_output=True,
            )

            kacak_dosya = root / "kacak.md"
            kacak_dosya.write_text("x", encoding="utf-8")
            original_resolve = Path.resolve

            def dis_resolve(self, *args, **kwargs):
                if self.name == "kacak.md":
                    return hedef / "kacak.md"
                return original_resolve(self, *args, **kwargs)

            with mock.patch.object(Path, "resolve", dis_resolve):
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    memory_compile._manifest(root)
            self.assertIn("staging-escape", str(scope.exception))


class LiveDestinationArms(unittest.TestCase):
    def test_parent_and_target_shape_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            knowledge = vault / "knowledge"
            knowledge.mkdir()

            (knowledge / "concepts").write_text("x", encoding="utf-8")
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._validate_live_destination(
                    vault, "knowledge/concepts/alt.md", None
                )
            self.assertIn("unsafe-live-parent", str(scope.exception))
            (knowledge / "concepts").unlink()

            hedef = Path(temporary) / "disari"
            hedef.mkdir()
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(knowledge / "connections"),
                 str(hedef)],
                capture_output=True,
            )
            if result.returncode == 0:
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    memory_compile._validate_live_destination(
                        vault, "knowledge/connections/x.md", None
                    )
                self.assertIn("live-parent-escape", str(scope.exception))

            (knowledge / "index.md").mkdir()
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._validate_live_destination(
                    vault, "knowledge/index.md", None
                )
            self.assertIn("unsafe-live-target", str(scope.exception))

            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._validate_live_destination(
                    vault, "knowledge/log.md", DIGEST
                )
            self.assertIn("live-target-missing", str(scope.exception))


class SnapshotMatcherArms(unittest.TestCase):
    def test_read_window_race_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "2026-09-11.md"
            source.write_bytes(b"kayit")
            digest = _sha(b"kayit")

            original = Path.resolve

            def kacak(self, *args, **kwargs):
                strict = kwargs.get("strict", args[0] if args else False)
                if self.name == source.name and strict:
                    return Path(temporary) / "baska" / source.name
                return original(self, *args, **kwargs)

            with mock.patch.object(Path, "resolve", kacak):
                self.assertFalse(
                    memory_compile._source_snapshot_matches(source, digest, 5)
                )

            with mock.patch.object(
                memory_compile.os, "fstat",
                return_value=types.SimpleNamespace(st_size=0),
            ):
                self.assertFalse(
                    memory_compile._source_snapshot_matches(source, digest, 5)
                )

            class _Bos:
                def fileno(self):
                    return 0

                def read(self, _n):
                    return b""

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            original_open = Path.open

            def fake_open(self, *args, **kwargs):
                if self.name == source.name and args[:1] == ("rb",):
                    return _Bos()
                return original_open(self, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", fake_open),
                mock.patch.object(
                    memory_compile.os, "fstat",
                    return_value=types.SimpleNamespace(st_size=5),
                ),
            ):
                self.assertFalse(
                    memory_compile._source_snapshot_matches(source, digest, 5)
                )

            with mock.patch.object(
                memory_compile.os, "fstat", side_effect=OverflowError
            ):
                self.assertFalse(
                    memory_compile._source_snapshot_matches(source, digest, 5)
                )


class PublicationPathArms(unittest.TestCase):
    def test_source_symlink_outside_daily(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "daily").mkdir()
            hedef = Path(temporary) / "gercek.md"
            hedef.write_text("x", encoding="utf-8")
            try:
                os.symlink(hedef, vault / "daily" / "2026-09-11.md")
            except OSError:
                self.skipTest("symlink izni yok")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile._publication_source_path(
                    vault, "daily/2026-09-11.md"
                )

    def test_stage_junction_and_resolve_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            hedef = Path(temporary) / "disari"
            hedef.mkdir()
            name = "compile-stage-" + "0" * 32
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(state / name), str(hedef)],
                capture_output=True,
            )
            if result.returncode == 0:
                with self.assertRaises(memory_compile.PolicyError):
                    memory_compile._publication_stage_path(state, name)
                subprocess.run(
                    ["cmd", "/c", "rmdir", str(state / name)],
                    capture_output=True,
                )

            (state / name).mkdir()
            original = Path.resolve

            def yariss(self, *args, **kwargs):
                strict = kwargs.get("strict", args[0] if args else False)
                if self.name == name and strict:
                    return Path(temporary) / "baska" / name
                return original(self, *args, **kwargs)

            with mock.patch.object(Path, "resolve", yariss):
                with self.assertRaises(memory_compile.PolicyError):
                    memory_compile._publication_stage_path(state, name)


class PromoteArms(unittest.TestCase):
    def test_metadata_and_pending_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily = _compile_vault(Path(temporary))
            stage = state / "s"
            stage.mkdir()
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._promote_changes(
                    stage, vault, [], {}, state_dir=state
                )
            self.assertIn("metadata-invalid", str(scope.exception))

            compile_state.save_publication(state, _journal())
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._promote_changes(
                    stage, vault, [], {},
                    state_dir=state, source_relative="daily/2026-09-11.md",
                    source_digest=DIGEST, source_size=5,
                    timestamp="2026-09-11T10:00:00",
                    suppression_digest=DIGEST,
                )
            self.assertIn("publication-pending", str(scope.exception))

    def test_output_and_stage_presence_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily = _compile_vault(Path(temporary))
            stage = state / "s"
            stage.mkdir()
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._promote_changes(
                    stage, vault, ["baska/x.md"], {}
                )
            self.assertIn("forbidden-promotion", str(scope.exception))

            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._promote_changes(
                    stage, vault, ["knowledge/concepts/yeni.md"], {}
                )
            self.assertIn("stage-output-missing", str(scope.exception))

    def test_non_journal_copy_and_journal_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily = _compile_vault(Path(temporary))
            stage = state / "s"
            (stage / "knowledge" / "concepts").mkdir(parents=True)
            (stage / "knowledge" / "concepts" / "yeni.md").write_text(
                "yeni içerik", encoding="utf-8"
            )
            memory_compile._promote_changes(
                stage, vault, ["knowledge/concepts/yeni.md"], {}
            )
            self.assertTrue((vault / "knowledge" / "concepts" / "yeni.md").is_file())

    def _journal_promote(self, vault, state, stage, **kwargs):
        return memory_compile._promote_changes(
            stage, vault, ["knowledge/concepts/yeni.md"],
            kwargs.pop("baseline", {"knowledge/concepts/yeni.md": None}),
            state_dir=state, source_relative="daily/2026-09-11.md",
            source_digest=_sha(b"kayit"), source_size=5,
            timestamp="2026-09-11T10:00:00",
            suppression_digest=memory_compile._suppression_digest(frozenset()),
            **kwargs,
        )

    def test_journal_promotion_digest_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily = _compile_vault(Path(temporary))
            stage = state / ("compile-stage-" + "1" * 32)
            (stage / "knowledge" / "concepts").mkdir(parents=True)
            staged = stage / "knowledge" / "concepts" / "yeni.md"
            staged.write_text("yeni içerik", encoding="utf-8")
            live = vault / "knowledge" / "concepts" / "yeni.md"

            # Canlı hedef zaten aynı içerikte: kopya atlanır.
            live.write_text("yeni içerik", encoding="utf-8")
            digest = memory_compile._sha256(staged)
            self._journal_promote(
                vault, state, stage,
                baseline={"knowledge/concepts/yeni.md": digest},
            )
            compile_state.clear_publication(state)
            live.unlink()

            def bozuk_kopya(source, destination, **_k):
                destination.write_text("bambaşka", encoding="utf-8")

            with mock.patch.object(
                memory_compile, "_atomic_copy", side_effect=bozuk_kopya
            ):
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    self._journal_promote(vault, state, stage)
            self.assertIn("target-drift", str(scope.exception))
            compile_state.clear_publication(state)

            live.write_text("eski içerik", encoding="utf-8")
            eski_digest = memory_compile._sha256(live)
            with mock.patch.object(
                memory_compile, "_live_digest", return_value="0" * 64
            ):
                self._journal_promote(
                    vault, state, stage,
                    baseline={"knowledge/concepts/yeni.md": eski_digest},
                )


class CompileOneArms(unittest.TestCase):
    def _run_one(self, vault, state, daily, *, digest=None, runner=None,
                 extra=()):
        managers = list(extra)
        entered = []
        try:
            for manager in managers:
                entered.append(manager.__enter__())
            return memory_compile._compile_one(
                vault, state, daily,
                digest or memory_compile._sha256(daily),
                "2026-09-11T00:00:00+00:00",
                runner=runner or (lambda _p, _s: None),
            )
        finally:
            for manager in reversed(managers):
                manager.__exit__(None, None, None)

    def test_pending_publication_and_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))
            compile_state.save_publication(state, _journal())
            reason, detail = self._run_one(vault, state, daily)
            self.assertEqual(reason, "policy")
            self.assertIn("publication-pending", detail)

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))
            reason, detail = self._run_one(
                vault, state, daily, digest="0" * 64
            )
            self.assertEqual(
                (reason, detail),
                ("source-changed", "source-changed-before-call"),
            )

    def test_directive_warning_and_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))
            daily.write_text("TALİMAT: bir şey\nKalıcı karar.\n",
                             encoding="utf-8")
            reason, _detail = self._run_one(vault, state, daily)
        self.assertEqual(reason, "no-changes")

    def test_taxonomy_failure_maps_to_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))
            (vault / ".codex" / "tag-taxonomy.json").write_text(
                "{bozuk", encoding="utf-8"
            )
            reason, detail = self._run_one(vault, state, daily)
        self.assertEqual(reason, "policy")
        self.assertIn("tag-taxonomy", detail)

    def test_source_mutation_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))

            def bozan(*_a, **_k):
                daily.write_bytes(b"BAMBASKA")
                return None

            reason, detail = self._run_one(
                vault, state, daily,
                extra=[mock.patch.object(
                    memory_compile,
                    "_normalize_validate_with_single_repair", bozan,
                )],
            )
            self.assertEqual(
                (reason, detail),
                ("source-changed", "source-changed-after-repair"),
            )

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))

            def bozan_diff(_before, _after):
                daily.write_bytes(b"BAMBASKA")
                return ["knowledge/index.md"]

            reason, detail = self._run_one(
                vault, state, daily,
                extra=[
                    mock.patch.object(
                        memory_compile,
                        "_normalize_validate_with_single_repair",
                        return_value=None,
                    ),
                    mock.patch.object(
                        memory_compile, "_validate_manifest_diff", bozan_diff,
                    ),
                ],
            )
            self.assertEqual(
                (reason, detail),
                ("source-changed", "source-changed-before-promotion"),
            )

    def test_value_error_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))

            def tercih(_p, _s):
                raise ValueError("memory-preferences-changed")

            reason, _detail = self._run_one(
                vault, state, daily, runner=tercih
            )
            self.assertEqual(reason, "memory-preferences-changed")

            def patlayan(_p, _s):
                raise ValueError("bilinmez")

            with self.assertRaises(ValueError):
                self._run_one(vault, state, daily, runner=patlayan)

    def test_stage_cleanup_failure_writes_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))
            reason, _detail = self._run_one(
                vault, state, daily, digest="0" * 64,
                extra=[mock.patch.object(
                    memory_compile.shutil, "rmtree",
                    side_effect=OSError("silinemedi"),
                )],
            )
        self.assertEqual(reason, "source-changed")


class PublicationStageValidatorArms(unittest.TestCase):
    def test_journal_shape_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            (stage / "daily").mkdir(parents=True)
            kaynak = stage / "daily" / "2026-09-11.md"
            kaynak.write_text("kayit", encoding="utf-8")
            kaynak_digest = memory_compile._sha256(kaynak)
            cases = [
                (_journal(source_relative=5), "journal-invalid"),
                (_journal(source_relative="daily/yok.md"),
                 "source-stage-invalid"),
                (_journal(targets="liste-degil"), "journal-invalid"),
                (_journal(targets=[5]), "journal-invalid"),
                (_journal(targets=[{"relative": 5, "after_sha256": 6}]),
                 "journal-invalid"),
                (_journal(targets=[{"relative": "knowledge/index.md",
                                    "after_sha256": DIGEST}]),
                 "stage-drift"),
            ]
            for journal, phrase in cases:
                with self.subTest(phrase=phrase):
                    with self.assertRaises(
                        memory_compile.PolicyError
                    ) as scope:
                        memory_compile._validate_publication_stage(
                            stage, journal
                        )
                    self.assertIn(phrase, str(scope.exception))
            self.assertTrue(kaynak_digest)


class RecoveryArms(unittest.TestCase):
    def _seed(self, temporary: Path, **journal_overrides):
        vault, state, daily = _compile_vault(temporary)
        daily.write_bytes(b"kayit")
        journal = _journal(
            source_digest=_sha(b"kayit"),
            source_size=5,
            suppression_digest=memory_compile._suppression_digest(frozenset()),
            **journal_overrides,
        )
        compile_state.save_publication(state, journal)
        return vault, state, daily, journal

    def test_source_missing_and_stage_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily, _journal_value = self._seed(Path(temporary))
            daily.unlink()
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._recover_pending_publication(vault, state)
            self.assertIn("source-missing", str(scope.exception))

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily, _journal_value = self._seed(Path(temporary))
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._recover_pending_publication(vault, state)
            self.assertIn("stage-missing", str(scope.exception))

    def test_locked_source_recheck_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily, journal = self._seed(Path(temporary))
            (state / journal["stage"]).mkdir()
            with (
                mock.patch.object(
                    memory_compile, "_validate_publication_stage"
                ),
                mock.patch.object(
                    memory_compile, "_source_snapshot_matches",
                    side_effect=[True, False],
                ),
            ):
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    memory_compile._recover_pending_publication(vault, state)
        self.assertIn("source-changed", str(scope.exception))

    def test_target_shape_defenses_after_validation(self) -> None:
        cases = [
            {"targets": "liste-degil"},
            {"targets": [5]},
            {"targets": [{"relative": 5, "before_sha256": None,
                          "after_sha256": 6}]},
        ]
        for overrides in cases:
            with self.subTest(overrides=str(overrides)[:30]):
                with tempfile.TemporaryDirectory() as temporary:
                    vault, state, _daily, journal = self._seed(
                        Path(temporary), **overrides
                    )
                    (state / journal["stage"]).mkdir()
                    with (
                        mock.patch.object(
                            memory_compile, "_validate_publication_journal",
                            side_effect=lambda _s, j: j,
                        ),
                        mock.patch.object(
                            memory_compile, "_validate_publication_stage"
                        ),
                    ):
                        with self.assertRaises(
                            memory_compile.PolicyError
                        ) as scope:
                            memory_compile._recover_pending_publication(
                                vault, state
                            )
                self.assertIn("journal-invalid", str(scope.exception))

    def test_complete_journal_target_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily, _journal_value = self._seed(
                Path(temporary),
                status="complete",
                targets=[{
                    "relative": "knowledge/index.md",
                    "before_sha256": None,
                    "after_sha256": "f" * 64,
                    "completed": True,
                }],
            )
            (vault / "knowledge" / "index.md").unlink()
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._recover_pending_publication(vault, state)
        self.assertIn("target-drift", str(scope.exception))

    def test_pending_stage_disappears_mid_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily, journal = self._seed(
                Path(temporary),
                targets=[{
                    "relative": "knowledge/concepts/yeni.md",
                    "before_sha256": None,
                    "after_sha256": "f" * 64,
                    "completed": False,
                }],
            )
            stage = state / journal["stage"]
            stage.mkdir()
            calls = {"n": 0}

            def validate(_stage, _journal):
                calls["n"] += 1
                if calls["n"] >= 2:
                    stage.rmdir()

            with mock.patch.object(
                memory_compile, "_validate_publication_stage", validate
            ):
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    memory_compile._recover_pending_publication(vault, state)
        self.assertIn("stage-missing", str(scope.exception))

    def test_pending_copy_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily, journal = self._seed(
                Path(temporary),
                targets=[{
                    "relative": "knowledge/concepts/yeni.md",
                    "before_sha256": None,
                    "after_sha256": "f" * 64,
                    "completed": False,
                }],
            )
            stage = state / journal["stage"]
            (stage / "knowledge" / "concepts").mkdir(parents=True)
            (stage / "knowledge" / "concepts" / "yeni.md").write_text(
                "içerik", encoding="utf-8"
            )
            with mock.patch.object(
                memory_compile, "_validate_publication_stage"
            ):
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    memory_compile._recover_pending_publication(vault, state)
        self.assertIn("target-drift", str(scope.exception))


class FinalizeAndApplyArms(unittest.TestCase):
    def test_finalize_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            compile_state.save_publication(state, _journal())
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile._finalize_publication(state)
            self.assertIn("incomplete", str(scope.exception))

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            journal = _journal(status="complete", operation_id=5)
            compile_state.save_publication(state, _journal(status="complete"))
            with mock.patch.object(
                memory_compile, "_validate_publication_journal",
                return_value=journal,
            ):
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    memory_compile._finalize_publication(state)
            self.assertIn("journal-invalid", str(scope.exception))

    def test_apply_recovered_guards(self) -> None:
        with self.assertRaises(memory_compile.PolicyError):
            memory_compile._apply_recovered_publication(
                CompileState(), _journal(source_relative=5)
            )
        state = CompileState(
            ingested={"2026-09-11.md": DIGEST}
        )
        self.assertFalse(
            memory_compile._apply_recovered_publication(state, _journal())
        )


class RebuildArms(unittest.TestCase):
    def test_output_shape_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, _state, _daily = _compile_vault(Path(temporary))
            hedef = Path(temporary) / "hedef"
            hedef.mkdir()
            link = Path(temporary) / "link"
            try:
                os.symlink(hedef, link, target_is_directory=True)
            except OSError:
                link = None
            if link is not None:
                with self.assertRaises(memory_compile.PolicyError):
                    memory_compile.rebuild_knowledge(vault, link)

            dosya = Path(temporary) / "dosya"
            dosya.write_text("x", encoding="utf-8")
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile.rebuild_knowledge(vault, dosya)
            with self.assertRaises(memory_compile.PolicyError):
                memory_compile.rebuild_knowledge(vault, dosya / "alt")

    def test_daily_failure_and_schema_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, _state, _daily = _compile_vault(Path(temporary))
            out = Path(temporary) / "cikti"
            with self.assertRaises(memory_compile.PolicyError) as scope:
                memory_compile.rebuild_knowledge(
                    vault, out, runner=lambda _p, _s: "codex-err"
                )
            self.assertIn("rebuild-codex-err", str(scope.exception))

        with tempfile.TemporaryDirectory() as temporary:
            vault, _state, daily = _compile_vault(Path(temporary))
            daily.unlink()
            out = Path(temporary) / "cikti"
            report = types.SimpleNamespace(issues=["ihlal"])
            with mock.patch.object(
                memory_compile, "validate_knowledge_tree",
                return_value=report,
            ):
                with self.assertRaises(memory_compile.PolicyError) as scope:
                    memory_compile.rebuild_knowledge(vault, out)
            self.assertIn("knowledge-schema", str(scope.exception))


class RunAndFailureArms(unittest.TestCase):
    def test_release_claim_swallows_directory_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            claim = state / "compile-trigger-2026-09-11"
            claim.mkdir()
            (claim / "içerik").write_text("x", encoding="utf-8")
            memory_compile._release_trigger_claim(state, claim)
            self.assertTrue(claim.exists())

    def test_record_failure_persistence_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            bozuk = CompileState(ingested={"daily/api_key=abc12345.md": "x"})
            memory_compile._record_failure(state, bozuk, "x.md", "policy")

            temiz = CompileState()
            with mock.patch.object(
                memory_compile.compile_state, "save", side_effect=OSError
            ):
                memory_compile._record_failure(state, temiz, "x.md", "policy")

    def test_run_locked_success_tail_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault, state, daily = _compile_vault(Path(temporary))
            daily.unlink()
            with mock.patch.object(
                memory_compile.compile_state, "save", side_effect=OSError
            ):
                self.assertTrue(
                    memory_compile._run_locked(vault, state, False, 1, None)
                )
            with mock.patch.object(
                memory_compile, "_finalize_publication",
                side_effect=ValueError("bozuk"),
            ):
                self.assertTrue(
                    memory_compile._run_locked(vault, state, False, 1, None)
                )

        with tempfile.TemporaryDirectory() as temporary:
            vault, state, _daily = _compile_vault(Path(temporary))
            with (
                mock.patch.object(
                    memory_compile, "_compile_one", return_value=(None, "")
                ),
                mock.patch.object(
                    memory_compile.compile_state, "save", side_effect=OSError
                ),
            ):
                self.assertTrue(
                    memory_compile._run_locked(vault, state, False, 1, None)
                )
            with (
                mock.patch.object(
                    memory_compile, "_compile_one", return_value=(None, "")
                ),
                mock.patch.object(
                    memory_compile, "_finalize_publication",
                    side_effect=ValueError("bozuk"),
                ),
            ):
                self.assertTrue(
                    memory_compile._run_locked(vault, state, False, 1, None)
                )

    def test_main_state_load_failure_arm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            environment = {k: v for k, v in os.environ.items()
                           if k != "BEYIN_INVOKED_BY"}
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(memory_compile, "STATE_DIR", state),
                mock.patch.object(
                    memory_compile, "VAULT_ROOT", Path(temporary)
                ),
                mock.patch.object(
                    memory_compile, "_run_locked",
                    side_effect=RuntimeError("çök"),
                ),
                mock.patch.object(
                    memory_compile.compile_state, "load",
                    side_effect=OSError("okunamadı"),
                ),
            ):
                self.assertEqual(memory_compile.main(["--strict"]), 1)


if __name__ == "__main__":
    unittest.main()
