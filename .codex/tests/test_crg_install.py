import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("crg_install", Path(__file__).parents[1] / "scripts" / "crg_install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FakeWindows:
    def __init__(self):
        xml = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Command>old.exe</Command><Arguments>watch</Arguments></Exec></Actions></Task>'
        self.tasks = {name: {"xml": xml, "enabled": True, "running": True} for name in ("CEVDET Graph - Code", "CEVDET Graph - Vault")}
        self.user_path = "keep;old;also-keep"
        self.stopped = []

    def path(self):
        return self.user_path

    def worktree_roots(self, code):
        return [code]

    def set_path(self, value, expected):
        if self.user_path != expected:
            raise ValueError("Concurrent user PATH change inside transaction")
        self.user_path = value

    def task(self, name):
        return copy.deepcopy(self.tasks[name])

    def process_snapshot(self, name):
        return [{"pid": 123, "creation_date": "2026-09-21T00:00:00Z"}]

    def stop(self, name, snapshot):
        self.stopped.append(name)
        self.tasks[name]["running"] = False
        return {"stopped": True, "terminated_ids": [123]}

    def processes_absent(self, snapshot):
        return True

    def set_task(self, name, value):
        self.tasks[name] = copy.deepcopy(value)

    def disable_task(self, name):
        value = installer.disabled_task(self.tasks[name])
        value["running"] = self.tasks[name]["running"]
        self.tasks[name] = value

    def pid_running(self, pid):
        return False


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = FakeWindows()
        self.plan = {key: str(self.root / name) for key, name in (
            ("code_root", "code"), ("vault_root", "vault"), ("runtime_source", "source.py"),
            ("canonical_root", "integrations/crg"), ("old_venv_scripts", "old"))}
        self.plan["tasks"] = {"code": "CEVDET Graph - Code", "vault": "CEVDET Graph - Vault"}
        for role in ("code_root", "vault_root"):
            repo = Path(self.plan[role])
            (repo / ".git" / "hooks").mkdir(parents=True)
            (repo / ".codex").mkdir()
            (repo / ".git" / "hooks" / "post-commit").write_bytes(b"#!/bin/sh\r\necho original\r\n")
            (repo / ".codex" / "config.toml").write_bytes(b"[features]\r\nhooks = false\r\n\r\n[mcp_servers.code-review-graph]\r\nenabled = true\r\ncommand = 'old.exe'\r\nargs = ['serve']\r\ncwd = 'old'\r\n\r\n[other]\r\nkeep = true\r\n")
        canonical = Path(self.plan["canonical_root"])
        scripts = canonical / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_bytes(b"python")
        (scripts / "pythonw.exe").write_bytes(b"pythonw")
        (canonical / "cevdet_runtime.py").write_bytes(b"old runtime")
        Path(self.plan["runtime_source"]).write_bytes(b"new runtime")
        self.host.user_path = "keep;" + self.plan["old_venv_scripts"] + ";also-keep"
        self.original_path = self.host.path()
        self.original_tasks = copy.deepcopy(self.host.tasks)
        self.state_path = self.root / "state.json"
        self.state = installer.prepare(self.plan, self.state_path, self.host)
        self.receipt = {"verdict": "PASS", "source_hashes": self.state["source_hashes"]}

    def apply(self):
        return installer.transition(self.state_path, self.host, "after", self.receipt)

    def test_apply_rollback_apply_preserves_fixed_original(self):
        self.assertEqual(len(self.state["files"]), 14)
        self.assertEqual(self.apply()["status"], "applied")
        self.assertEqual(self.host.path(), "keep;" + str(Path(self.plan["canonical_root"]) / ".venv" / "Scripts") + ";also-keep")
        self.assertFalse(self.host.tasks["CEVDET Graph - Vault"]["running"])
        self.assertIn("protected-status", self.host.tasks["CEVDET Graph - Vault"]["xml"])
        self.assertNotIn(self.plan["vault_root"], self.host.tasks["CEVDET Graph - Vault"]["xml"])
        config = Path(self.plan["vault_root"]) / ".codex" / "config.toml"
        self.assertIn(b"[other]\r\nkeep = true\r\n", config.read_bytes())
        self.assertIn(b"hooks = false", config.read_bytes())
        hook = Path(self.plan["code_root"]) / ".git" / "hooks" / "post-checkout"
        self.assertIn(b'root=$(git rev-parse --show-toplevel)', hook.read_bytes())
        self.assertIn(b'--repo "$root"', hook.read_bytes())
        self.assertNotIn(self.plan["code_root"].encode(), hook.read_bytes())
        self.assertEqual(self.apply()["status"], "applied")
        restored = installer.transition(self.state_path, self.host, "before")
        self.assertEqual(restored["status"], "rolled-back")
        for path, values in self.state["files"].items():
            self.assertEqual(installer.read_bytes(path), installer.unpacked(values["before"]))
        self.assertEqual(self.host.path(), self.original_path)
        expected_tasks = copy.deepcopy(self.original_tasks)
        expected_tasks["CEVDET Graph - Code"] = installer.disabled_task(expected_tasks["CEVDET Graph - Code"])
        expected_tasks["CEVDET Graph - Vault"] = installer.disabled_task(expected_tasks["CEVDET Graph - Vault"])
        self.assertEqual(self.host.tasks, expected_tasks)
        self.assertEqual(len(restored["runtime_differences"]), 1)
        self.assertEqual(self.apply()["files"], self.state["files"])

    def test_stale_file_path_and_task_refused_before_mutation(self):
        config = Path(self.plan["code_root"]) / ".codex" / "config.toml"
        original = config.read_bytes()
        config.write_bytes(b"concurrent edit")
        with self.assertRaisesRegex(ValueError, "Concurrent file"):
            self.apply()
        self.assertEqual(self.host.stopped, [])
        config.write_bytes(original)
        self.host.user_path += ";concurrent"
        with self.assertRaisesRegex(ValueError, "Concurrent user PATH"):
            self.apply()
        self.host.user_path = self.original_path
        self.host.tasks["CEVDET Graph - Code"]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "Concurrent task"):
            self.apply()
        self.assertEqual(self.host.stopped, [])

    def test_interruption_journal_recovers_original_bytes(self):
        unknown = Path(self.plan["canonical_root"]) / "unrelated-user-file.txt"
        unknown.write_bytes(b"preserve me")
        original_replace = installer.preserved_replace
        count = 0
        def interrupted(path, data, expected, backup):
            nonlocal count
            if str(path) in self.state["files"]:
                count += 1
                if count == 4:
                    raise OSError("injected interruption")
            original_replace(path, data, expected, backup)
        with patch.object(installer, "preserved_replace", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "injected interruption"):
                self.apply()
        interrupted_state = json.loads(self.state_path.read_text())
        self.assertEqual(interrupted_state["status"], "applying")
        self.assertFalse(interrupted_state["journal"][-1]["done"])
        installer.transition(self.state_path, self.host, "before")
        for path, values in self.state["files"].items():
            self.assertEqual(installer.read_bytes(path), installer.unpacked(values["before"]))
        expected_tasks = copy.deepcopy(self.original_tasks)
        expected_tasks["CEVDET Graph - Code"] = installer.disabled_task(expected_tasks["CEVDET Graph - Code"])
        expected_tasks["CEVDET Graph - Vault"] = installer.disabled_task(expected_tasks["CEVDET Graph - Vault"])
        self.assertEqual(self.host.tasks, expected_tasks)
        self.assertEqual(self.host.path(), self.original_path)
        self.assertEqual(unknown.read_bytes(), b"preserve me")

    def test_receipt_source_change_and_baseline_overwrite_refused(self):
        with self.assertRaisesRegex(ValueError, "review receipt"):
            installer.transition(self.state_path, self.host, "after", {"verdict": "PASS"})
        Path(self.plan["runtime_source"]).write_bytes(b"unreviewed")
        with self.assertRaisesRegex(ValueError, "Reviewed source changed"):
            self.apply()
        with self.assertRaisesRegex(ValueError, "Baseline already exists"):
            installer.prepare(self.plan, self.state_path, self.host)
        self.assertEqual(self.host.stopped, [])

    def test_rollback_refuses_external_changes(self):
        self.apply()
        config = Path(self.plan["vault_root"]) / ".codex" / "config.toml"
        config.write_bytes(b"new user work")
        with self.assertRaisesRegex(ValueError, "Concurrent file"):
            installer.transition(self.state_path, self.host, "before")
        self.assertEqual(config.read_bytes(), b"new user work")

    def test_owner_marker_requires_verified_terminated_watcher(self):
        marker = Path(self.state["owner_marker"])
        marker.parent.mkdir()
        marker.write_text(json.dumps({"owner": "unknown", "pid": 123}))
        with self.assertRaisesRegex(ValueError, "no verified terminated watcher"):
            self.apply()
        self.assertTrue(marker.exists())
        marker.write_text(json.dumps({"owner": "cevdet-native-watch", "pid": 123}))
        with patch.object(self.host, "pid_running", return_value=True):
            with self.assertRaisesRegex(ValueError, "no verified terminated watcher"):
                self.apply()
        self.assertTrue(marker.exists())
        self.apply()
        self.assertFalse(marker.exists())

    def test_optional_install_state_preserves_unrelated_fields_and_rollback_bytes(self):
        target = self.root / ".cevdet-codex-install-state.json"
        original = b'{"sdk":{"installed_version":"original"},"integrations":{"other":42}}\r\n'
        target.write_bytes(original)
        self.plan["install_state"] = str(target)
        self.state_path.unlink()
        self.state = installer.prepare(self.plan, self.state_path, self.host)
        self.receipt = {"verdict": "PASS", "source_hashes": self.state["source_hashes"]}
        self.apply()
        record = json.loads(target.read_bytes())
        self.assertEqual(record["sdk"], {"installed_version": "original"})
        self.assertEqual(record["integrations"]["other"], 42)
        self.assertEqual(record["integrations"]["crg_runtime_separation"]["source_classification"]["vault_root"], "PROTECTED")
        identity_path = Path(self.plan["canonical_root"]) / "migration-identity.json"
        identity = json.loads(identity_path.read_bytes())
        self.assertEqual(record["integrations"]["crg_runtime_separation"]["identity_sha256"], installer.digest(identity_path.read_bytes()))
        self.assertEqual(record["integrations"]["crg_runtime_separation"]["physical_roots"]["vault"], self.plan["vault_root"])
        self.assertNotIn(str(identity_path), identity["file_sha256"])
        self.assertNotIn(str(target), identity["file_sha256"])
        for path, expected in identity["file_sha256"].items():
            self.assertEqual(installer.digest(Path(path).read_bytes()), expected)
        for name, expected in identity["task_xml_sha256"].items():
            self.assertEqual(installer.digest(self.host.task(name)["xml"].encode("utf-8")), expected)
        self.assertEqual(identity["user_path_sha256"], installer.digest(json.dumps(self.host.path(), ensure_ascii=False).encode("utf-8")))
        installer.transition(self.state_path, self.host, "before")
        self.assertEqual(target.read_bytes(), original)

    def test_plan_rejects_runtime_inside_protected_vault(self):
        self.state_path.unlink()
        self.plan["canonical_root"] = str(Path(self.plan["vault_root"]) / "runtime")
        with self.assertRaisesRegex(ValueError, "overlapping roots"):
            installer.prepare(self.plan, self.state_path, self.host)

    def test_parent_reparse_refused_before_read_or_write(self):
        parent = Path(self.plan["canonical_root"])
        original_lstat = Path.lstat
        def reparse(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path == parent:
                class Reparse:
                    st_mode = value.st_mode
                    st_file_attributes = 0x400
                return Reparse()
            return value
        with patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(ValueError, "linked path"):
                installer.read_bytes(parent / "cevdet_runtime.py")
            with self.assertRaisesRegex(ValueError, "linked path"):
                installer.atomic(parent / "cevdet_runtime.py", b"unsafe")
        self.assertEqual((parent / "cevdet_runtime.py").read_bytes(), b"old runtime")

    def test_late_edit_preserved_at_native_replace_boundary(self):
        original_replace = installer.replace_with_backup
        injected = False
        def race(path, replacement, backup):
            nonlocal injected
            if not injected:
                injected = True
                Path(path).write_bytes(b"late user edit")
            return original_replace(path, replacement, backup)
        with patch.object(installer, "replace_with_backup", side_effect=race):
            with self.assertRaisesRegex(ValueError, "conflict backup"):
                self.apply()
        state = json.loads(self.state_path.read_bytes())
        pending = state["journal"][-1]
        self.assertFalse(pending["done"])
        self.assertEqual(Path(pending["backup"]).read_bytes(), b"late user edit")
        with self.assertRaisesRegex(ValueError, "Unresolved concurrent file preserved"):
            installer.transition(self.state_path, self.host, "before")
        self.assertEqual(Path(pending["backup"]).read_bytes(), b"late user edit")

    def test_no_clobber_creation_preserves_late_new_file(self):
        original_link = installer.os.link
        def race(source, target, **kwargs):
            Path(target).write_bytes(b"late independent creation")
            return original_link(source, target, **kwargs)
        with patch.object(installer.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.apply()
        state = json.loads(self.state_path.read_bytes())
        self.assertEqual(Path(state["journal"][-1]["name"]).read_bytes(), b"late independent creation")

    def test_kill_before_result_checkpoint_recovers_marker(self):
        marker = Path(self.state["owner_marker"])
        marker.parent.mkdir()
        marker.write_text(json.dumps({"owner": "cevdet-native-watch", "pid": 123}))
        original_stop = self.host.stop
        def interrupted(name, snapshot):
            original_stop(name, snapshot)
            raise OSError("killed before result checkpoint")
        with patch.object(self.host, "stop", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "result checkpoint"):
                self.apply()
        state = json.loads(self.state_path.read_bytes())
        self.assertFalse(state["journal"][-1]["done"])
        self.assertEqual(state["journal"][-1]["process_snapshot"][0]["pid"], 123)
        with patch.object(self.host, "process_snapshot", return_value=[]):
            restored = installer.transition(self.state_path, self.host, "before")
        self.assertFalse(marker.exists())
        self.assertEqual(restored["status"], "rolled-back")

    def test_path_late_observed_edit_is_saved_and_refused(self):
        original_save = installer.save
        def race(path, state):
            original_save(path, state)
            if state["journal"] and state["journal"][-1]["kind"] == "path" and "conflicting_observed" not in state["journal"][-1]:
                self.host.user_path = "concurrent editor path"
        with patch.object(installer, "save", side_effect=race):
            with self.assertRaisesRegex(ValueError, "Concurrent user PATH"):
                self.apply()
        state = json.loads(self.state_path.read_bytes())
        self.assertEqual(state["journal"][-1]["actual_before"], self.original_path)
        self.assertEqual(state["journal"][-1]["conflicting_observed"], "concurrent editor path")
        self.assertEqual(self.host.path(), "concurrent editor path")

    def test_worktree_marker_checkpoint_survives_stop_interruption(self):
        code = Path(self.plan["code_root"])
        worktree = code.with_name(code.name + "-task")
        marker = worktree / ".code-review-graph" / "crg-freshness.lock"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"owner": "cevdet-native-watch", "pid": 123}))
        original_stop = self.host.stop
        def interrupted(name, snapshot):
            original_stop(name, snapshot)
            raise OSError("stop result lost")
        with patch.object(self.host, "worktree_roots", return_value=[code, worktree]):
            with patch.object(self.host, "stop", side_effect=interrupted):
                with self.assertRaisesRegex(OSError, "stop result lost"):
                    self.apply()
            with patch.object(self.host, "process_snapshot", return_value=[]):
                installer.transition(self.state_path, self.host, "before")
        self.assertFalse(marker.exists())

    def test_task_observed_race_preserves_conflicting_definition(self):
        original_save = installer.save
        def race(path, state):
            original_save(path, state)
            if state["journal"] and state["journal"][-1]["kind"] == "task" and "conflicting_observed" not in state["journal"][-1]:
                self.host.tasks[state["journal"][-1]["name"]]["xml"] = self.original_tasks["CEVDET Graph - Code"]["xml"].replace("old.exe", "user-edit.exe")
        with patch.object(installer, "save", side_effect=race):
            with self.assertRaisesRegex(ValueError, "Concurrent task change"):
                self.apply()
        state = json.loads(self.state_path.read_bytes())
        entry = state["journal"][-1]
        self.assertIn("old.exe", entry["actual_before"]["xml"])
        self.assertIn("user-edit.exe", entry["conflicting_observed"]["xml"])
        self.assertIn("user-edit.exe", self.host.task(entry["name"])["xml"])

    def test_prepare_uses_same_config_snapshot_for_derivation_and_baseline(self):
        self.state_path.unlink()
        config = Path(self.plan["code_root"]) / ".codex" / "config.toml"
        first = config.read_bytes()
        original = installer.config_bytes
        def race(data, python, runtime, repo):
            result = original(data, python, runtime, repo)
            config.write_bytes(b"concurrent prepare edit")
            return result
        with patch.object(installer, "config_bytes", side_effect=race):
            self.state = installer.prepare(self.plan, self.state_path, self.host)
        self.receipt = {"verdict": "PASS", "source_hashes": self.state["source_hashes"]}
        self.assertEqual(installer.unpacked(self.state["files"][str(config)]["before"]), first)
        with self.assertRaisesRegex(ValueError, "Concurrent file change"):
            self.apply()
        self.assertEqual(config.read_bytes(), b"concurrent prepare edit")

    def test_prepare_runtime_bytes_and_hash_share_one_read(self):
        self.state_path.unlink()
        source = Path(self.plan["runtime_source"])
        first = source.read_bytes()
        original = installer.read_bytes
        reads = 0
        def race(path):
            nonlocal reads
            data = original(path)
            if Path(path) == source:
                reads += 1
                source.write_bytes(b"later unreviewed runtime")
            return data
        with patch.object(installer, "read_bytes", side_effect=race):
            self.state = installer.prepare(self.plan, self.state_path, self.host)
        self.receipt = {"verdict": "PASS", "source_hashes": self.state["source_hashes"]}
        runtime = Path(self.plan["canonical_root"]) / "cevdet_runtime.py"
        self.assertEqual(reads, 1)
        self.assertEqual(installer.unpacked(self.state["files"][str(runtime)]["after"]), first)
        self.assertEqual(self.state["source_hashes"][str(source)], installer.digest(first))
        with self.assertRaisesRegex(ValueError, "Reviewed source changed"):
            self.apply()

    def test_vault_rollback_registration_xml_disables_triggers_before_register(self):
        self.apply()
        original = self.host.set_task
        def checked(name, value):
            if name in ("CEVDET Graph - Vault", "CEVDET Graph - Code"):
                self.assertFalse(value["enabled"])
                self.assertFalse(value["running"])
                root = installer.ET.fromstring(value["xml"])
                ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                self.assertEqual(root.find("t:Settings/t:Enabled", ns).text, "false")
                self.assertIn("old.exe", value["xml"])
            original(name, value)
        with patch.object(self.host, "set_task", side_effect=checked):
            installer.transition(self.state_path, self.host, "before")
        self.assertEqual(self.state["tasks"]["CEVDET Graph - Vault"]["before"], self.original_tasks["CEVDET Graph - Vault"])
        self.assertEqual(self.apply()["status"], "applied")

    def test_quiesce_disables_all_triggers_before_stop_and_recovers_interruption(self):
        original_disable = self.host.disable_task
        def interrupted(name):
            original_disable(name)
            raise OSError("disabled before checkpoint")
        with patch.object(self.host, "disable_task", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "disabled before checkpoint"):
                self.apply()
        state = json.loads(self.state_path.read_bytes())
        entry = state["journal"][-1]
        self.assertEqual(entry["kind"], "quiesce")
        self.assertFalse(entry["done"])
        self.assertTrue(entry["actual_before"]["enabled"])
        self.assertFalse(self.host.task(entry["name"])["enabled"])
        self.assertEqual(self.host.stopped, [])
        for path, values in self.state["files"].items():
            self.assertEqual(installer.read_bytes(path), installer.unpacked(values["before"]))
        original_stop = self.host.stop
        attempted_trigger_launches = []
        def stop_with_trigger(name, snapshot):
            for task_name, value in self.host.tasks.items():
                if value["enabled"]:
                    attempted_trigger_launches.append(task_name)
            return original_stop(name, snapshot)
        with patch.object(self.host, "stop", side_effect=stop_with_trigger):
            self.apply()
        self.assertEqual(attempted_trigger_launches, [])
        self.assertTrue(self.host.task("CEVDET Graph - Code")["enabled"])
        self.assertTrue(self.host.task("CEVDET Graph - Code")["running"])
        self.assertTrue(self.host.task("CEVDET Graph - Vault")["enabled"])
        self.assertFalse(self.host.task("CEVDET Graph - Vault")["running"])


@unittest.skipUnless(os.name == "nt", "native Windows transactional registry test")
class TransactionalPathTests(unittest.TestCase):
    def setUp(self):
        import winreg
        self.registry = winreg
        self.subkey = "Software\\CRGInstallerTest-" + uuid.uuid4().hex
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.subkey) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, r"%TEST_ROOT%\old;keep")
        self.addCleanup(winreg.DeleteKey, winreg.HKEY_CURRENT_USER, self.subkey)

    def read(self):
        with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, self.subkey) as key:
            return self.registry.QueryValueEx(key, "Path")

    def test_native_transaction_preserves_raw_value_type_and_expected_check(self):
        expected, registry_type = self.read()
        installer.transactional_path(r"%TEST_ROOT%\new;keep", expected, self.subkey)
        self.assertEqual(self.read(), (r"%TEST_ROOT%\new;keep", registry_type))
        with self.assertRaisesRegex(ValueError, "inside transaction"):
            installer.transactional_path("wrong", expected, self.subkey)
        self.assertEqual(self.read(), (r"%TEST_ROOT%\new;keep", registry_type))

    def test_nontransactional_interference_preserves_external_write(self):
        winreg = self.registry
        expected, registry_type = self.read()
        original_set = winreg.SetValueEx
        def race(key, name, reserved, kind, value):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.subkey, 0, winreg.KEY_SET_VALUE) as outside:
                original_set(outside, "Path", 0, registry_type, "external writer wins")
            return original_set(key, name, reserved, kind, value)
        with patch.object(winreg, "SetValueEx", side_effect=race):
            with self.assertRaises(OSError):
                installer.transactional_path("migration must not win", expected, self.subkey)
        self.assertEqual(self.read(), ("external writer wins", registry_type))


if __name__ == "__main__":
    unittest.main()
