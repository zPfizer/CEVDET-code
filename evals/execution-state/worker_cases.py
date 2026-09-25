"""Real worker failure, successor and killed-process observations on synthetic roots."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from evidence import (LoaderObservation, file_digest, implementation_manifest, write_new,
                      new_fixture, fixture_bytes, capture_and_restore, digest)


def _child(scripts, root, state, running, marker):
    """Stable observed child entry point; parameters are locations, never results."""
    scripts = Path(scripts).resolve()
    sys.path.insert(0, str(scripts))
    import worker_supervisor as workers
    def observed(frame, event, arg):
        if event == 'call' and frame.f_code.co_name == 'flush_once':
            loaded = []
            for module in tuple(sys.modules.values()):
                name = getattr(module, '__file__', None)
                if name:
                    path = Path(name).resolve()
                    if path.suffix == '.py' and (path.is_relative_to(scripts) or path.parent == Path(__file__).parent):
                        loaded.append({'path': str(path), 'sha256': file_digest(path)})
            write_new(marker, {'entry': 'real-flush_once', 'loaded': loaded})
    sys.setprofile(observed)
    return workers.execute_job_file(Path(root), Path(state), Path(running))


def _session_start(hook, root, state, *, read_only=False):
    """Configure an isolated runtime root, then execute the actual main handler."""
    previous = hook.VAULT_ROOT, hook.STATE_DIR, sys.stdin, Path.cwd()
    output = io.StringIO()
    try:
        hook.VAULT_ROOT, hook.STATE_DIR = root, state
        os.chdir(root)
        if read_only:
            sys.stdin = io.StringIO(json.dumps({'cwd': str(root), 'session_id': 'reader',
                                               'prompt': 'Bu çalışma salt okunur. Hiçbir dosyayı değiştirme.'}))
            with redirect_stdout(io.StringIO()):
                hook.main(['user-prompt', '--strict'])
            if not hook.is_read_only_turn(state, 'reader'):
                raise ValueError('worker-fixture-read-only-source-not-recognized')
        sys.stdin = io.StringIO(json.dumps({'cwd': str(root), 'session_id': 'reader'}))
        with redirect_stdout(output):
            code = hook.main(['session-start', '--strict'])
        response = json.loads(output.getvalue())
        return response['hookSpecificOutput']['additionalContext'], code
    finally:
        hook.VAULT_ROOT, hook.STATE_DIR, sys.stdin, cwd = previous
        os.chdir(cwd)


def run_worker_case(case_id: str, source: dict, repo: Path, folder: Path) -> dict:
    scripts, hooks = repo / '.codex/scripts', repo / '.codex/hooks'
    sys.path[:0] = [str(scripts), str(hooks)]
    observer = LoaderObservation([scripts, hooks, Path(__file__).parent])
    observer.start()
    import companion_memory
    import flush
    import hook
    import worker_supervisor as workers
    from file_lock import locked
    from process_control import terminate_process_tree

    started = time.monotonic_ns()
    root = new_fixture()
    (root / 'unrelated-user-source.keep').write_text('Preserve this unrelated synthetic source.', encoding='utf-8')
    state = root / '.codex/scripts/.state'
    state.mkdir(parents=True)
    subprocess.run(['git', 'init', '--quiet', str(root)], check=True, capture_output=True)
    source_path = folder / 'source.json'
    write_new(source_path, source)
    source_before = file_digest(source_path)
    baseline = fixture_bytes(root)
    seed_snapshot = {'source_sha256': source_before,
                     'files': {name: digest(data.hex()) for name, data in baseline.items()},
                     'scope': 'NEW_DISPOSABLE_EVAL_FIXTURE_ONLY'}
    write_new(folder / 'seed-snapshot.json', seed_snapshot)
    before = companion_memory.execution_snapshot(root)
    events = []
    interrupted = None
    child_execution = None

    if case_id in {'EVAL-075', 'EVAL-076'}:
        payload = {'reason': 'synthetic-compile-recovery'}
        job_path = workers.enqueue_job(state, 'maintenance', payload, start_supervisor=False, now=100)
        # A held real compile lock forces the actual dispatcher to fail. No result is mocked.
        with locked(state / 'compile'):
            for attempt in range(workers.MAX_ATTEMPTS):
                stamp = 100 + attempt * 1000
                running, claimed = workers._claim_next_job(state, now=stamp)
                code = workers.execute_job_file(root, state, running, now=lambda: stamp + 1)
                events.append({'event': 'worker_execute', 'exit_code': code, 'job_id': claimed['job_id'],
                               'attempt': claimed['attempt'], 'fault': 'held-real-compile-lock'})
        failed = state / 'worker-jobs/dead-letter' / job_path.name
        if not failed.is_file():
            raise ValueError('worker-fixture-did-not-reach-dead-letter')
        if case_id == 'EVAL-076':
            successor = workers.enqueue_job(state, 'maintenance', payload, start_supervisor=False, now=4000)
            running, claimed = workers._claim_next_job(state, now=4000)
            code = workers.execute_job_file(root, state, running, now=lambda: 4001)
            events.append({'event': 'actual-successor-dispatch', 'exit_code': code, 'job_id': claimed['job_id'],
                           'receipt_exists': (state / 'worker-jobs/succeeded' / successor.name).is_file()})
        queue = workers.inspect_worker_queue(state)
        context, hook_code = _session_start(hook, root, state)
    elif case_id == 'EVAL-077':
        transcript = folder / 'transcript.jsonl'
        transcript.write_text(json.dumps({'role': 'user', 'content': 'Atlas raporu açık; sonucu doğrulamadan tamamlandı sayma.'}), encoding='utf-8')
        transcript_before = file_digest(transcript)
        transport = state / 'hookin-interruption.json'
        transport.write_text(json.dumps({'session_id': 'interrupted', 'transcript_path': str(transcript),
                                        'cwd': str(root)}), encoding='utf-8')
        workers.enqueue_job(state, 'flush', {'hook_input': str(transport), 'reason': 'sessionend',
                            'event_iso': '2026-09-03T12:00:00+00:00'}, start_supervisor=False)
        running, claimed = workers._claim_next_job(state, now=time.time())
        marker = folder / 'entered-flush.json'
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--child',
                   str(scripts), str(root), str(state), str(running), str(marker)]
        with locked(flush._session_lock_target(state, 'interrupted')):
            process = subprocess.Popen(command, cwd=root,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while not marker.is_file() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                if not marker.is_file():
                    raise ValueError('actual-flush-entry-not-observed')
                child_execution = {'command': command, 'entrypoint_sha256': file_digest(Path(__file__)),
                                   'observed': json.loads(marker.read_text(encoding='utf-8'))}
                terminate_process_tree(process)
                interrupted = process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    terminate_process_tree(process)
                process.communicate(timeout=10)
        recovered = workers.recover_stale_jobs(state, now=time.time() + 10000)
        companion_memory.request_reflection(state, 'interrupted')
        queue = workers.inspect_worker_queue(state)
        # Explicit read-only new-session source prevents starting another background worker.
        context, hook_code = _session_start(hook, root, state, read_only=True)
        events.append({'event': 'real-flush-process-interrupted', 'entry_observed': marker.is_file(),
                       'exit_code': interrupted, 'requeued': recovered,
                       'transcript_unchanged': file_digest(transcript) == transcript_before})
    else:
        raise ValueError('unsupported-worker-case')

    events.append({'event': 'actual-SessionStart-main', 'exit_code': hook_code, 'queue': queue})
    actual = companion_memory.execution_snapshot(root)
    persisted = companion_memory.execution_snapshot(root)
    files = observer.finish()
    if child_execution is not None:
        by_path = {item['path']: item for item in files}
        for item in child_execution['observed']['loaded']:
            if file_digest(item['path']) != item['sha256']:
                raise ValueError('EVIDENCE_BINDING_INVALID:child-loaded-file-drift')
            by_path.setdefault(item['path'], {**item, 'roles': ['adapter']})
        files = [by_path[name] for name in sorted(by_path)]
    write_new(folder / 'loader.json', {'observed_paths': [item['path'] for item in files], 'native_events': []})
    closure = {'observed_paths': [item['path'] for item in files]}
    common = folder.parent / ('worker-loader-closure-' + case_id + '.json')
    if common.exists():
        if json.loads(common.read_text(encoding='utf-8')) != closure:
            raise ValueError('EVIDENCE_BINDING_INVALID:worker-closure-changed')
    else:
        write_new(common, closure)
    config = {'surface': 'real-worker-dispatch-and-SessionStart-main', 'model_calls': 0,
              'fault': 'real-lock-or-process-interruption', 'expected-state-injected': False}
    manifest = implementation_manifest(files, config, 'working-tree', str(common), [])
    write_new(folder / 'implementation.json', manifest)
    warning = 'sonucu doğrulanamadı' in context
    observation = {'catalog': actual, 'persisted_catalog': persisted, 'catalog_before': before,
        'context': context, 'source_unchanged': file_digest(source_path) == source_before,
        'route': 'production', 'surface': config['surface'], 'events': events, 'native_events': [],
        'child_execution': child_execution,
        'unresolved': hook._unresolved_terminal_count(queue), 'recovered': queue['terminal']['recovered'],
        'warning_visible': warning, 'retry_visible': 'henüz doğrulanmadı' in context and queue['counts']['pending'] > 0,
        'interrupted_exit_code': interrupted, 'wall_ms': (time.monotonic_ns() - started) // 1_000_000,
        'fixture_seed_or_snapshot_ref': str(folder / 'seed-snapshot.json'), 'fixture_state_digest': digest(seed_snapshot),
        'implementation_manifest_ref': str(folder / 'implementation.json'),
        'implementation_digest': manifest['implementation_digest'], 'effective_config_digest': manifest['effective_config_digest']}
    observation['cleanup_verified'] = capture_and_restore(
        root, baseline, folder, allowed_roots=('.codex/scripts/.state',),
        allowed_files=('.codex/scripts/health.json', '.codex/scripts/health.lock'))
    write_new(folder / 'observation.json', observation)
    return observation


if __name__ == '__main__':
    if len(sys.argv) != 7 or sys.argv[1] != '--child':
        raise SystemExit('worker-cases-child-arguments-invalid')
    raise SystemExit(_child(*sys.argv[2:]))
