"""Observed Python loader and real native-process evidence, without credentials."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import uuid


def digest(value, domain=''):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return 'sha256:' + hashlib.sha256((domain.encode() + b'\0' if domain else b'') + body).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def new_fixture():
    base = Path(tempfile.mkdtemp(prefix='cevdet-execution-eval-')).resolve()
    root = base / 'vault'
    root.mkdir()
    write_new(base / 'fixture-owner.json', {'root': str(root), 'owner': uuid.uuid4().hex,
                                          'scope': 'NEW_DISPOSABLE_EVAL_FIXTURE_ONLY'})
    return root


def fixture_bytes(root):
    root = Path(root).resolve(strict=True)
    files, pending = {}, [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root):
                raise ValueError('FIXTURE_SCOPE_INVALID:linked-path')
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
            else:
                raise ValueError('FIXTURE_SCOPE_INVALID:nonregular-file')
    return files


def capture_and_restore(root, baseline, folder, *, allowed_roots=(), allowed_files=()):
    """Copy/hash all post-state evidence, then reset only this new temp fixture.

    The temp root and all baseline files (including unrelated sentinel) remain.
    Historical evidence trees and user files can never be cleanup targets.
    """
    root = Path(root)
    if root.is_symlink() or root.is_junction():
        raise ValueError('FIXTURE_SCOPE_INVALID:linked-root')
    root = root.resolve(strict=True)
    temporary = Path(tempfile.gettempdir()).resolve()
    owner_path = root.parent / 'fixture-owner.json'
    if owner_path.is_symlink() or owner_path.is_junction():
        raise ValueError('FIXTURE_SCOPE_INVALID:linked-owner')
    owner = json.loads(owner_path.read_text(encoding='utf-8'))
    if (not root.is_relative_to(temporary) or not root.parent.name.startswith('cevdet-execution-eval-')
            or owner.get('root') != str(root) or owner.get('scope') != 'NEW_DISPOSABLE_EVAL_FIXTURE_ONLY'
            or not isinstance(owner.get('owner'), str) or root == temporary):
        raise ValueError('FIXTURE_SCOPE_INVALID:owner')
    for name in (*baseline, *allowed_roots, *allowed_files):
        relative = Path(name)
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                or not (root / relative).resolve().is_relative_to(root)):
            raise ValueError('FIXTURE_SCOPE_INVALID:baseline-target')
    def mutable(name):
        return name in allowed_files or any(name.startswith(prefix.rstrip('/') + '/') for prefix in allowed_roots)

    def refuse(reason, **details):
        write_new(Path(folder) / 'cleanup.json', {'verified': False, 'reason': reason,
                  'owner': owner, 'allowed_mutable_roots': list(allowed_roots),
                  'allowed_mutable_files': list(allowed_files),
                  'post_snapshot_ref': str(Path(folder) / 'post-fixture'),
                  'seed_snapshot_ref': str(Path(folder) / 'seed-fixture'), **details})
        raise ValueError('FIXTURE_SCOPE_INVALID:' + reason)

    def protect(current):
        changed = sorted(name for name, data in baseline.items() if not mutable(name) and current.get(name) != data)
        unexpected = sorted(name for name in set(current) - set(baseline) if not mutable(name))
        if changed or unexpected:
            refuse('unrelated-state-changed', changed_immutable_files=changed, new_unrelated_files=unexpected)

    before = fixture_bytes(root)
    for name, data in before.items():
        target = Path(folder) / 'post-fixture' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        if target.read_bytes() != data:
            raise ValueError('FIXTURE_CAPTURE_INVALID:copy-hash')
    for name, data in baseline.items():
        target = Path(folder) / 'seed-fixture' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        if target.read_bytes() != data:
            raise ValueError('FIXTURE_CAPTURE_INVALID:seed-copy')
    # Re-enumerate before mutation: concurrent/new files cannot escape capture.
    if fixture_bytes(root) != before:
        refuse('concurrent-change')
    protect(before)

    def check_target(name):
        # All immutable files and new unowned paths remain protected throughout
        # cleanup, not only at the initial snapshot.
        protect(fixture_bytes(root))
        target = root / name
        if target.is_symlink() or target.is_junction() or not target.resolve().is_relative_to(root):
            refuse('cleanup-target')
        current = target.read_bytes() if target.is_file() else None
        if current != before.get(name):
            refuse('cleanup-target-changed', target=name)
        return target

    removed = []
    for name in sorted(set(before) - set(baseline)):
        target = check_target(name)
        target.unlink()
        removed.append(name)
    for name, data in baseline.items():
        if not mutable(name) or before.get(name) == data:
            continue
        target = check_target(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = check_target(name)
        target.write_bytes(data)
    restored = fixture_bytes(root)
    verified = restored == baseline
    write_new(Path(folder) / 'cleanup.json', {'verified': verified, 'owner': owner,
              'baseline_digest': digest({name: hashlib.sha256(data).hexdigest() for name, data in baseline.items()}),
              'restored_digest': digest({name: hashlib.sha256(data).hexdigest() for name, data in restored.items()}),
              'post_snapshot_ref': str(Path(folder) / 'post-fixture'),
              'seed_snapshot_ref': str(Path(folder) / 'seed-fixture'),
              'allowed_mutable_roots': list(allowed_roots), 'allowed_mutable_files': list(allowed_files),
              'removed_new_disposable_files': removed, 'protected_evidence_deleted': 0})
    return verified


class LoaderObservation:
    """Audit reads and imports independently of the later manifest file list."""

    def __init__(self, roots):
        self.roots = [Path(root).resolve() for root in roots]
        self.observed = set()
        self.preimage = {}
        self.active = False

    def _owned(self, path):
        return any(path.is_relative_to(root) for root in self.roots)

    def _audit(self, event, args):
        if self.active and event == 'open' and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).absolute()
            if self._owned(path) and path.suffix in {'.py', '.pyc', '.toml', '.json'}:
                self.observed.add(str(path))

    def start(self):
        # Snapshot potential code/config once; actual observation selects closure.
        for root in self.roots:
            for path in root.rglob('*'):
                if path.is_file() and path.suffix in {'.py', '.pyc', '.toml', '.json'}:
                    self.preimage[str(path)] = file_digest(path)
        sys.addaudithook(self._audit)
        self.active = True

    def finish(self):
        self.active = False
        for module in tuple(sys.modules.values()):
            source = getattr(module, '__file__', None)
            if source:
                path = Path(source).resolve()
                if self._owned(path) and path.suffix == '.py':
                    self.observed.add(str(path))
                    cached = getattr(module, '__cached__', None)
                    if cached and Path(cached).is_file() and self._owned(Path(cached).resolve()):
                        self.observed.add(str(Path(cached).resolve()))
        files = []
        for name in sorted(self.observed):
            path = Path(name)
            if not path.is_file() or path.is_symlink() or str(path.resolve()) != str(path):
                raise ValueError('EVIDENCE_BINDING_INVALID:closure-path')
            current = file_digest(path)
            if self.preimage.get(name) != current:
                raise ValueError('EVIDENCE_BINDING_INVALID:loaded-file-drift:' + name)
            roles = ['runner', 'grader', 'adapter'] if path.name == 'adapter.py' else ['adapter'] if path.suffix in {'.py', '.pyc'} else ['config']
            files.append({'path': name, 'sha256': current, 'roles': roles})
        return files


@contextmanager
def observe_native(codex_runner, events):
    """Delegate to the production process runner; observe only safe metadata.

    stdout/stderr were discarded by production. Capture them in memory solely
    to extract the native banner. Neither raw output nor environment is saved.
    """
    original = codex_runner.run_with_tree_timeout

    def observed(command, **kwargs):
        executable = Path(command[0]).resolve()
        before = file_digest(executable)
        forwarded = dict(kwargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = original(command, **forwarded)
        text = b'\n'.join(value for value in (getattr(result, 'stdout', b''), getattr(result, 'stderr', b'')) if isinstance(value, bytes)).decode('utf-8', errors='replace')
        metadata = {}
        for key in ('model', 'provider', 'approval', 'sandbox', 'reasoning effort'):
            match = re.search(r'^' + re.escape(key) + r':\s*([^\r\n]+)', text, re.MULTILINE)
            if match:
                metadata[key] = match.group(1).strip()
        version = re.search(r'OpenAI Codex v([^\s]+)', text)
        events.append({'executable': str(executable), 'artifact_digest': before,
                       'artifact_unchanged': before == file_digest(executable),
                       'version': version.group(1) if version else None,
                       'command': command, 'exit_code': result.returncode,
                       'metadata': metadata, 'source': 'REAL_PROCESS_OUTPUT'})
        return result

    codex_runner.run_with_tree_timeout = observed
    try:
        yield
    finally:
        codex_runner.run_with_tree_timeout = original


def implementation_manifest(files, config, revision, loader_ref, native_events):
    root = Path(__file__).resolve().parents[2]
    relative_files = []
    for item in files:
        path = Path(item['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('EVIDENCE_BINDING_INVALID:manifest-root-escape')
        relative_files.append(dict(item, path=path.relative_to(root).as_posix()))
    relative_files.sort(key=lambda item: item['path'])
    effective = digest(config, 'CEVDET-EFFECTIVE-CONFIG-v1')
    inventory = []
    for name in ('harness_metrics', 'contract_tool'):
        module = sys.modules.get(name)
        path = getattr(module, '__file__', None)
        if path:
            inventory.append({'name': name, 'version': 'installed-source', 'artifact_digest': file_digest(path)})
    for event in native_events:
        if not event['artifact_unchanged'] or not event['version'] or not event['metadata'].get('model'):
            raise ValueError('EVIDENCE_BINDING_INVALID:native-identity-incomplete')
        item = {'name': 'codex', 'version': event['version'], 'artifact_digest': event['artifact_digest']}
        if item not in inventory:
            inventory.append(item)
    manifest = {'schema_version': '1.0', 'root': str(root), 'entrypoint': 'evals/execution-state/adapter.py',
                'implementation_revision': revision, 'files': relative_files,
                'closure_evidence_ref': loader_ref, 'effective_config_digest': effective,
                'runtime_identity': {'interpreter': sys.version, 'platform': platform.platform(),
                                     'dependency_inventory': inventory}}
    manifest['implementation_digest'] = digest(manifest, 'CEVDET-IMPLEMENTATION-v1')
    return manifest
