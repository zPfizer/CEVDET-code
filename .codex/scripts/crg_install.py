"""Scoped, reversible CRG relocation. No package installation or Vault content reads."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET


HOOKS = ("post-commit", "post-merge", "post-checkout", "post-rewrite", "pre-commit")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def physical(path):
    """Reject every reparse component before resolving or touching its target."""
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Refusing linked path: {component}")
    return path


def read_bytes(path):
    path = physical(path)
    return path.read_bytes() if path.exists() else None


def packed(data):
    return None if data is None else base64.b64encode(data).decode("ascii")


def unpacked(data):
    return None if data is None else base64.b64decode(data)


def atomic(path, data):
    path = physical(path)
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        physical(path)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save(path, state):
    atomic(path, json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8"))


def replace_with_backup(path, replacement, backup):
    """ReplaceFileW preserves the file actually displaced at the OS boundary."""
    if os.name != "nt":
        raise OSError("Windows ReplaceFileW required for existing-file replacement")
    import ctypes
    api = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    api.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    api.restype = ctypes.c_int
    if not api(str(path), str(replacement), str(backup), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def preserved_replace(path, desired, expected, backup):
    """Keep late edits intact in a journaled sibling; never silently accept them."""
    path, backup = physical(path), physical(backup)
    if backup.parent != path.parent or backup.exists():
        raise ValueError("Invalid displacement backup")
    if expected == desired:
        if read_bytes(path) != expected:
            raise ValueError(f"Concurrent file change: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if desired is None:
        # Rename captures the actual removed file; it is retained, never unlinked.
        os.rename(path, backup)
    else:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(desired)
                stream.flush()
                os.fsync(stream.fileno())
            physical(path)
            if expected is None:
                # Atomic no-clobber publication: an independently created file wins.
                os.link(temporary, path)
            else:
                replace_with_backup(path, temporary, backup)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if expected is not None and read_bytes(backup) != expected:
        raise ValueError(f"Concurrent file preserved; conflict backup: {backup}")


def config_bytes(original, python, runtime, repo):
    text = (original or b"").decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    header = "[mcp_servers.code-review-graph]"
    values = {"command": json.dumps(str(python)), "args": json.dumps(
        ["-B", str(runtime), "serve", "--repo", str(repo)]), "cwd": json.dumps(str(repo)),
        "enabled_tools": json.dumps(["query_graph_tool", "get_impact_radius_tool"])}
    match = re.search(r"(?m)^\[mcp_servers\.code-review-graph\][ \t]*\r?$", text)
    if match:
        end_match = re.search(r"(?m)^\[", text[match.end():])
        end = match.end() + end_match.start() if end_match else len(text)
        section = text[match.start():end]
        for key, value in values.items():
            pattern = rf"(?m)^{key}[ \t]*=.*(?:\r?\n|$)"
            if len(re.findall(pattern, section)) > 1:
                raise ValueError(f"Duplicate CRG config field: {key}")
            replacement = key + " = " + value + newline
            section = re.sub(pattern, lambda _: replacement, section) if re.search(pattern, section) else section.rstrip("\r\n") + newline + replacement
        text = text[:match.start()] + section + text[end:]
    else:
        text = text.rstrip("\r\n") + newline * 2 + header + newline
        text += "".join(key + " = " + value + newline for key, value in values.items())
    # Reject multiline/unsupported replacements instead of corrupting a valid config.
    import tomllib
    parsed = tomllib.loads(text)["mcp_servers"]["code-review-graph"]
    if parsed["args"] != json.loads(values["args"]):
        raise ValueError("CRG config replacement failed")
    return text.encode("utf-8")


def hooks_dir(repo):
    git = Path(repo) / ".git"
    if git.is_dir():
        return git / "hooks"
    raise ValueError("Live target must have a physical .git directory")


def transactional_path(value, expected, subkey="Environment"):
    """Query/compare/write share one transacted HKCU key; preserve REG_SZ/EXPAND_SZ.

    Production calls only Environment/Path. The subkey parameter permits isolated
    native tests without accessing the real environment value.
    """
    import ctypes
    from ctypes import wintypes
    import winreg
    ktm = ctypes.WinDLL("KtmW32", use_last_error=True)
    registry = ctypes.WinDLL("Advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ktm.CreateTransaction.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPWSTR]
    ktm.CreateTransaction.restype = wintypes.HANDLE
    ktm.CommitTransaction.argtypes = [wintypes.HANDLE]
    ktm.CommitTransaction.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    registry.RegOpenKeyTransactedW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.HANDLE, ctypes.c_void_p]
    registry.RegOpenKeyTransactedW.restype = wintypes.LONG
    transaction = ktm.CreateTransaction(None, None, 0, 0, 0, 10000, "CRG scoped PATH migration")
    if transaction == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    key = wintypes.HANDLE()
    try:
        # HKEY_CURRENT_USER is a sign-extended predefined Windows handle.
        status = registry.RegOpenKeyTransactedW(wintypes.HANDLE(-2147483647), subkey, 0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE, ctypes.byref(key), transaction, None)
        if status:
            raise ctypes.WinError(status)
        try:
            current, registry_type = winreg.QueryValueEx(key.value, "Path")
        except FileNotFoundError:
            current, registry_type = None, winreg.REG_EXPAND_SZ
        if current != expected:
            raise ValueError("Concurrent user PATH change inside transaction")
        if registry_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            raise ValueError("Unsupported user PATH registry type")
        if value is None:
            if current is not None:
                winreg.DeleteValue(key.value, "Path")
        else:
            winreg.SetValueEx(key.value, "Path", 0, registry_type, value)
        if not ktm.CommitTransaction(transaction):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if key.value:
            winreg.CloseKey(key.value)
        # Closing an uncommitted transaction rolls it back, including exceptions.
        kernel.CloseHandle(transaction)


class Windows:
    """Only named task operations and the current user's PATH."""

    @staticmethod
    def ps(script, data=None):
        env = os.environ.copy()
        env["CRG_MIGRATION_INPUT"] = json.dumps(data or {})
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; $d=$env:CRG_MIGRATION_INPUT|ConvertFrom-Json; " + script],
            env=env, check=True, capture_output=True, text=True)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def path(self):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try:
                return winreg.QueryValueEx(key, "Path")[0]
            except FileNotFoundError:
                return None

    def worktree_roots(self, code):
        output = subprocess.check_output(["git", "-C", str(code), "worktree", "list", "--porcelain", "-z"])
        roots = []
        for field in output.decode("utf-8").split("\0"):
            if not field.startswith("worktree "):
                continue
            root = physical(field[len("worktree "):])
            if root != code and not (root.parent == code.parent and root.name.startswith(code.name + "-")):
                continue
            common = subprocess.check_output(["git", "-C", str(root), "rev-parse",
                "--path-format=absolute", "--git-common-dir"], text=True).strip()
            if physical(common) != code / ".git":
                raise ValueError("Unregistered worktree marker scope")
            roots.append(root)
        return roots

    def set_path(self, value, expected):
        transactional_path(value, expected)

    def task(self, name):
        return self.ps("$t=Get-ScheduledTask -TaskName $d.name -TaskPath '\\'; "
            "@{xml=(Export-ScheduledTask -TaskName $d.name -TaskPath '\\'); "
            "enabled=[bool]$t.Settings.Enabled; running=($t.State -eq 'Running')}|ConvertTo-Json -Compress",
            {"name": name})

    def process_snapshot(self, name):
        # Capture before stopping, so the journal can survive loss of the stop result.
        return self.ps("$t=Get-ScheduledTask -TaskName $d.name -TaskPath '\\'; "
            "$all=@(Get-CimInstance Win32_Process); "
            "$p=@($all|Where-Object { $p=$_; @($t.Actions|Where-Object { "
            "$_.Execute -and "
            "($p.CommandLine -eq ('\"'+$_.Execute+'\" '+$_.Arguments) -or "
            "$p.CommandLine -eq ($_.Execute+' '+$_.Arguments)) }).Count -gt 0 }); "
            "if($t.State -eq 'Running' -and !$p.Count){throw 'Running task process not identified'}; "
            "do { $prior=$p.Count; $ids=@($p|ForEach-Object {$_.ProcessId}); "
            "$p+=@($all|Where-Object {$_.ParentProcessId -in $ids -and $_.ProcessId -notin $ids}) "
            "} while($p.Count -gt $prior); "
            "ConvertTo-Json -InputObject @($p|ForEach-Object {@{pid=$_.ProcessId; "
            "creation_date=$_.CreationDate.ToUniversalTime().ToString('o')}}) -Depth 4 -Compress",
            {"name": name})

    def stop(self, name, snapshot):
        return self.ps("$p=@($d.snapshot); "
            "Stop-ScheduledTask -TaskName $d.name -TaskPath '\\'; "
            "foreach($p0 in $p){ if(Get-CimInstance Win32_Process -Filter ('ProcessId='+$p0.pid) | "
            "Where-Object {$_.CreationDate.ToUniversalTime().ToString('o') -eq $p0.creation_date}){ "
            "& taskkill.exe /PID $p0.pid /T /F | Out-Null; if($LASTEXITCODE -ne 0){throw 'taskkill failed'} } }; "
            "$remaining=@(Get-CimInstance Win32_Process|Where-Object {$q=$_; @($p|Where-Object { "
            "$_.pid -eq $q.ProcessId -and $_.creation_date -eq $q.CreationDate.ToUniversalTime().ToString('o') }).Count -gt 0}); "
            "if($remaining.Count){throw 'Prior task process still alive'}; "
            "for($i=0;$i -lt 30;$i++){if((Get-ScheduledTask -TaskName $d.name -TaskPath '\\').State -ne 'Running'){break}; Start-Sleep -Milliseconds 100}; "
            "if((Get-ScheduledTask -TaskName $d.name -TaskPath '\\').State -eq 'Running'){throw 'Task remains running'}; "
            "@{terminated_ids=@($p|ForEach-Object {$_.pid}); processes=$p; "
            "stopped=$true}|ConvertTo-Json -Depth 4 -Compress",
            {"name": name, "snapshot": snapshot})

    def processes_absent(self, snapshot):
        return self.ps("$all=@(Get-CimInstance Win32_Process); $found=@($all|Where-Object {$q=$_; "
            "@($d.snapshot|Where-Object {$_.pid -eq $q.ProcessId -and "
            "$_.creation_date -eq $q.CreationDate.ToUniversalTime().ToString('o')}).Count -gt 0}); "
            "($found.Count -eq 0)|ConvertTo-Json", {"snapshot": snapshot})

    def set_task(self, name, value):
        self.ps("Register-ScheduledTask -TaskName $d.name -TaskPath '\\' -Xml $d.value.xml -Force|Out-Null; "
            "if($d.value.enabled){Enable-ScheduledTask -TaskName $d.name -TaskPath '\\'|Out-Null}" 
            "else{Disable-ScheduledTask -TaskName $d.name -TaskPath '\\'|Out-Null}; "
            "if($d.value.running -and $d.value.enabled){Start-ScheduledTask -TaskName $d.name -TaskPath '\\'}",
            {"name": name, "value": value})

    def disable_task(self, name):
        self.ps("Disable-ScheduledTask -TaskName $d.name -TaskPath '\\'|Out-Null", {"name": name})

    def pid_running(self, pid):
        return self.ps("[bool](Get-Process -Id $d.pid -ErrorAction SilentlyContinue)|ConvertTo-Json", {"pid": pid})


def task_target(original, python, runtime, repo, role):
    root = ET.fromstring(original["xml"])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    ET.register_namespace("", ns["t"])
    actions = root.find("t:Actions", ns)
    if actions is None:
        raise ValueError("Task has no actions")
    for child in list(actions):
        actions.remove(child)
    action = ET.SubElement(actions, "{" + ns["t"] + "}Exec")
    for key, text in (("Command", str(python)), ("Arguments", subprocess.list2cmdline(
            ["-B", str(runtime), role, "--repo", str(repo)])), ("WorkingDirectory", str(repo))):
        ET.SubElement(action, "{" + ns["t"] + "}" + key).text = text
    return {"xml": ET.tostring(root, encoding="unicode"), "enabled": original["enabled"],
            "running": original["running"] if role == "watch" else False}


def disabled_task(original):
    """Disable in the XML itself, before registration can activate a trigger."""
    root = ET.fromstring(original["xml"])
    namespace = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    settings = root.find(namespace + "Settings")
    if settings is None:
        settings = ET.SubElement(root, namespace + "Settings")
    enabled = settings.find(namespace + "Enabled")
    if enabled is None:
        enabled = ET.SubElement(settings, namespace + "Enabled")
    enabled.text = "false"
    return {"xml": ET.tostring(root, encoding="unicode"), "enabled": False, "running": False}


def same_task(a, b):
    # Scheduler formatting differs on export. Compare parsed structure, ignore current running state.
    def shape(xml):
        node = ET.fromstring(xml)
        def visit(n):
            return (n.tag, sorted(n.attrib.items()), (n.text or "").strip(), [visit(c) for c in n])
        return visit(node)
    return a["enabled"] == b["enabled"] and shape(a["xml"]) == shape(b["xml"])


def prepare(plan, state_path, host):
    state_path = physical(state_path)
    if state_path.exists():
        raise ValueError("Baseline already exists; reuse it for apply/rollback")
    required = ("code_root", "vault_root", "runtime_source", "canonical_root", "old_venv_scripts")
    paths = {key: physical(plan[key]) for key in required}
    code, vault, source, canonical, old = (paths[key] for key in required)
    if (code == vault or code.is_relative_to(vault) or vault.is_relative_to(code)
            or canonical.is_relative_to(vault) or canonical.is_relative_to(code)
            or vault.is_relative_to(canonical) or code.is_relative_to(canonical)
            or canonical == old or old in canonical.parents or source.is_relative_to(vault)
            or state_path.is_relative_to(vault)):
        raise ValueError("Invalid overlapping roots")
    runtime = canonical / "cevdet_runtime.py"
    python = canonical / ".venv" / "Scripts" / "python.exe"
    pythonw = python.with_name("pythonw.exe")
    if not python.is_file() or not pythonw.is_file():
        raise ValueError("Existing canonical Python installation is required")
    source_bytes = read_bytes(source)
    installer_bytes = read_bytes(__file__)
    desired = {runtime: source_bytes}
    originals = {runtime: read_bytes(runtime)}
    if desired[runtime] is None:
        raise ValueError("Runtime source missing")
    identity = physical(plan.get("identity_path", canonical / "migration-identity.json"))
    if identity != canonical / "migration-identity.json":
        raise ValueError("Identity must be directly under canonical runtime root")
    originals[identity] = read_bytes(identity)
    desired[identity] = json.dumps({"runtime": str(runtime), "python": str(python),
        "runtime_sha256": digest(desired[runtime])}, indent=2).encode()
    install_state = None
    if plan.get("install_state"):
        install_state = physical(plan["install_state"])
        if install_state != canonical.parents[1] / ".cevdet-codex-install-state.json":
            raise ValueError("Unexpected canonical install-state location")
        originals[install_state] = read_bytes(install_state)
        record = json.loads(originals[install_state])
        record.setdefault("integrations", {})["crg_runtime_separation"] = {
            "runtime": str(runtime), "python": str(python), "runtime_sha256": digest(desired[runtime]),
            "identity_path": str(identity), "source_classification": {
                "code_root": "CODE", "vault_root": "PROTECTED"},
            "ownership": "native scheduled watcher; hooks requests; scoped MCP readonly",
            "policy_unchanged": True}
        desired[install_state] = json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8")
    for repo in (code, vault):
        for hook in HOOKS:
            originals[hooks_dir(repo) / hook] = read_bytes(hooks_dir(repo) / hook)
            command = [str(python), "-B", str(runtime), "hook", "--event", hook]
            desired[hooks_dir(repo) / hook] = ("#!/bin/sh\n# CRG notice only; indexing is owned by the watcher.\n"
                + 'root=$(git rev-parse --show-toplevel) || exit 1\n'
                + shlex.join([part.replace("\\", "/") for part in command]) + ' --repo "$root"\n').encode()
        config = repo / ".codex" / "config.toml"
        originals[config] = read_bytes(config)
        desired[config] = config_bytes(originals[config], python, runtime, code)
    task_names = plan["tasks"]
    if set(task_names) != {"code", "vault"} or len(set(task_names.values())) != 2:
        raise ValueError("Exactly two distinct named tasks required")
    for name in task_names.values():
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9 _-]*", name) or name != name.strip():
            raise ValueError("Only explicit root task names supported")
    tasks = {}
    for role, name in task_names.items():
        before = host.task(name)
        tasks[name] = {"before": before, "after": task_target(before, pythonw, runtime, code,
            "watch" if role == "code" else "protected-status")}
        tasks[name]["rollback"] = disabled_task(before)
    old_path = host.path()
    new_path = None if old_path is None else ";".join(str(python.parent)
        if os.path.normcase(part.strip().strip('"').rstrip("\\/")) == os.path.normcase(str(old).rstrip("\\/"))
        else part for part in old_path.split(";"))
    identity_record = json.loads(desired[identity])
    identity_record.update(
        file_sha256={str(path): digest(data) for path, data in desired.items() if path not in (identity, install_state)},
        task_xml_sha256={name: digest(value["after"]["xml"].encode("utf-8")) for name, value in tasks.items()},
        user_path_sha256=digest(json.dumps(new_path, ensure_ascii=False).encode("utf-8")))
    desired[identity] = json.dumps(identity_record, indent=2, ensure_ascii=False).encode("utf-8")
    if install_state is not None:
        record["integrations"]["crg_runtime_separation"].update(
            identity_sha256=digest(desired[identity]), physical_roots={"code": str(code), "vault": str(vault)})
        desired[install_state] = json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8")
    state = {"version": 1, "status": "prepared", "files": {str(path): {
        "before": packed(originals[path]), "after": packed(data)} for path, data in desired.items()},
        "tasks": tasks, "vault_task": task_names["vault"],
        "install_state": str(install_state) if install_state else None, "roots": {
            "code": str(code), "vault": str(vault), "canonical": str(canonical)},
        "owner_marker": str(code / ".code-review-graph" / "crg-freshness.lock"),
        "path": {"before": old_path, "after": new_path}, "journal": [],
        "source_hashes": {str(Path(__file__).resolve()): digest(installer_bytes),
                          str(source): digest(source_bytes)}}
    save(state_path, state)
    return state


def transition(state_path, host, direction, receipt=None):
    state_path = physical(state_path)
    # OS-released lock also survives interrupted runs without stale lock recovery machinery.
    lock_path = physical(state_path.with_suffix(state_path.suffix + ".lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _transition(state_path, host, direction, receipt)


def _transition(state_path, host, direction, receipt):
    if direction not in ("before", "after"):
        raise ValueError("Invalid transition direction")
    state = json.loads(read_bytes(state_path))
    # A crash after ReplaceFileW still leaves the actual displaced version here.
    for item in state["journal"]:
        if item.get("backup") and Path(item["backup"]).exists():
            if packed(read_bytes(item["backup"])) != item["expected"]:
                raise ValueError(f"Unresolved concurrent file preserved: {item['backup']}")
    code, vault, canonical = (physical(state["roots"][key]) for key in ("code", "vault", "canonical"))
    expected_files = {str(canonical / name) for name in ("cevdet_runtime.py", "migration-identity.json")}
    for repo in (code, vault):
        expected_files.update(str(hooks_dir(repo) / hook) for hook in HOOKS)
        expected_files.add(str(repo / ".codex" / "config.toml"))
    if state.get("install_state"):
        install_state = canonical.parents[1] / ".cevdet-codex-install-state.json"
        if state["install_state"] != str(install_state):
            raise ValueError("Unexpected canonical install-state location")
        expected_files.add(str(install_state))
    if set(state["files"]) != expected_files or state["owner_marker"] != str(code / ".code-review-graph" / "crg-freshness.lock"):
        raise ValueError("Unexpected migration write targets")
    if direction == "after":
        if not receipt or receipt.get("verdict") != "PASS" or receipt.get("source_hashes") != state["source_hashes"]:
            raise ValueError("Matching independent review receipt required")
        for path, expected in state["source_hashes"].items():
            if digest(read_bytes(path)) != expected:
                raise ValueError("Reviewed source changed")
    # Validate every surface before any mutation. Recovery accepts only the two recorded states.
    mixed = state["status"] in ("applying", "rolling-back")
    current_side = "after" if state["status"] == "applied" else "before"
    allowed = ("before", "after") if mixed else (current_side,)
    for path, values in state["files"].items():
        if packed(read_bytes(path)) not in [values[key] for key in allowed]:
            raise ValueError(f"Concurrent file change: {path}")
    if host.path() not in [state["path"][key] for key in allowed]:
        raise ValueError("Concurrent user PATH change")
    for name, values in state["tasks"].items():
        task_allowed = list(allowed)
        if "rollback" in values and (mixed or state["status"] == "rolled-back"):
            task_allowed = list(values) if mixed else ["rollback"]
        candidates = [values[key] for key in task_allowed]
        if mixed:
            candidates.extend(disabled_task(value) for value in values.values())
        if not any(same_task(host.task(name), value) for value in candidates):
            raise ValueError(f"Concurrent task change: {name}")
    state["status"] = "applying" if direction == "after" else "rolling-back"
    save(state_path, state)
    def operation(kind, name, action):
        state["journal"].append({"kind": kind, "name": name, "direction": direction, "done": False})
        entry = state["journal"][-1]
        if kind == "stop":
            entry["markers"] = {}
            for root in host.worktree_roots(code):
                root = physical(root)
                if root != code and not (root.parent == code.parent and root.name.startswith(code.name + "-")):
                    raise ValueError("Unexpected worktree marker scope")
                marker_path = root / ".code-review-graph" / "crg-freshness.lock"
                marker_before = read_bytes(marker_path)
                if marker_before is not None:
                    entry["markers"][str(marker_path)] = digest(marker_before)
            entry["process_snapshot"] = host.process_snapshot(name)
        elif kind in ("file", "owner-marker"):
            entry["expected"] = packed(read_bytes(name))
            entry["backup"] = str(Path(name).with_name(Path(name).name + ".crg-" + uuid.uuid4().hex + ".bak"))
        elif kind == "path":
            entry["actual_before"] = host.path()
        elif kind in ("task", "quiesce"):
            entry["actual_before"] = host.task(name)
        save(state_path, state)
        result = action()
        state["journal"][-1].update(done=True, result=result)
        save(state_path, state)
    # Disable all triggers before stopping any process or changing runtime files.
    # Stop alone is insufficient: a still-enabled old task could restart mid-copy.
    for name in state["tasks"]:
        def quiesce(name=name):
            entry = state["journal"][-1]
            observed = host.task(name)
            values = state["tasks"][name]
            candidates = list(values.values()) + [disabled_task(value) for value in values.values()]
            if not same_task(observed, entry["actual_before"]) or not any(same_task(observed, candidate) for candidate in candidates):
                entry["conflicting_observed"] = observed
                save(state_path, state)
                raise ValueError(f"Concurrent task change before quiesce: {name}")
            host.disable_task(name)
            observed = host.task(name)
            if not same_task(observed, disabled_task(entry["actual_before"])):
                entry["conflicting_observed"] = observed
                save(state_path, state)
                raise ValueError(f"Task quiesce verification failed: {name}")
        operation("quiesce", name, quiesce)
    for name in state["tasks"]:
        operation("stop", name, lambda name=name: host.stop(name, state["journal"][-1]["process_snapshot"]))
    markers = {path for item in state["journal"] if item["kind"] == "stop" for path in item.get("markers", {})}
    for marker_name in sorted(markers):
        marker = physical(marker_name)
        root = marker.parent.parent
        if marker.name != "crg-freshness.lock" or marker.parent.name != ".code-review-graph" or (root != code and not (root.parent == code.parent and root.name.startswith(code.name + "-"))):
            raise ValueError("Unexpected saved worktree marker scope")
        if not marker.exists():
            continue
        marker_data = read_bytes(marker)
        owner = json.loads(marker_data)
        terminated = {process["pid"] for item in state["journal"] if item["kind"] == "stop"
                      and item.get("markers", {}).get(str(marker)) == digest(marker_data)
                      and host.processes_absent(item.get("process_snapshot", []))
                      for process in item.get("process_snapshot", [])}
        if (owner.get("owner") != "cevdet-native-watch" or owner.get("pid") not in terminated
                or host.pid_running(owner["pid"])):
            raise ValueError("Owner marker has no verified terminated watcher")
        def clear_marker():
            if read_bytes(marker) != marker_data:
                raise ValueError("Concurrent owner marker change")
            entry = state["journal"][-1]
            preserved_replace(marker, None, marker_data, entry["backup"])
        operation("owner-marker", str(marker), clear_marker)
    for path, values in state["files"].items():
        def replace(path=path, values=values):
            entry = state["journal"][-1]
            if entry["expected"] not in (values["before"], values["after"]):
                raise ValueError(f"Concurrent file change: {path}")
            preserved_replace(path, unpacked(values[direction]), unpacked(entry["expected"]), entry["backup"])
        operation("file", path, replace)
    def replace_path():
        entry = state["journal"][-1]
        observed = host.path()
        if observed != entry["actual_before"] or observed not in (state["path"]["before"], state["path"]["after"]):
            entry["conflicting_observed"] = observed
            save(state_path, state)
            raise ValueError("Concurrent user PATH change")
        host.set_path(state["path"][direction], expected=entry["actual_before"])
        observed = host.path()
        if observed != state["path"][direction]:
            entry["conflicting_observed"] = observed
            save(state_path, state)
            raise ValueError("Concurrent user PATH change after setter")
    operation("path", "User", replace_path)
    for name, values in state["tasks"].items():
        def replace_task(name=name, values=values):
            entry = state["journal"][-1]
            observed = host.task(name)
            candidates = list(values.values()) + [disabled_task(value) for value in values.values()]
            if not same_task(observed, entry["actual_before"]) or not any(same_task(observed, candidate) for candidate in candidates):
                entry["conflicting_observed"] = observed
                save(state_path, state)
                raise ValueError(f"Concurrent task change: {name}")
            value = values["rollback"] if direction == "before" else values[direction]
            host.set_task(name, value)
            observed = host.task(name)
            if not same_task(observed, value):
                entry["conflicting_observed"] = observed
                save(state_path, state)
                raise ValueError(f"Concurrent task change after setter: {name}")
        operation("task", name, replace_task)
    for path, values in state["files"].items():
        if read_bytes(path) != unpacked(values[direction]):
            raise ValueError(f"File verification failed: {path}")
    if host.path() != state["path"][direction]:
        raise ValueError("PATH verification failed")
    for name, values in state["tasks"].items():
        expected_task = values["rollback"] if direction == "before" else values[direction]
        if not same_task(host.task(name), expected_task):
            raise ValueError(f"Task verification failed: {name}")
    state["status"] = "applied" if direction == "after" else "rolled-back"
    state["runtime_differences"] = (["Both original task actions restored with Enabled=false in registration XML; triggers and restart paused to protect Vault and keep the old environment untouched"]
        if direction == "before" else [])
    state["platform_limitations"] = ["Task Scheduler exposes no compare-and-swap: journaled immediate pre/post observations do not exclude an unobserved external write during the native setter."]
    save(state_path, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "rollback"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        if not args.plan:
            parser.error("prepare requires --plan")
        state = prepare(json.loads(args.plan.read_text(encoding="utf-8")), args.state, Windows())
    else:
        receipt = json.loads(args.review.read_text(encoding="utf-8")) if args.review else None
        state = transition(args.state, Windows(), "after" if args.action == "apply" else "before", receipt)
    print(json.dumps({"status": state["status"], "files": len(state["files"]), "tasks": len(state["tasks"])}))


if __name__ == "__main__":
    main()
