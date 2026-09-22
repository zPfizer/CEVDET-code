"""Canonical execution-state adapter: source inputs and hidden grader are separate.

Only ``run_sut`` receives source conversations; canonical record, expectations,
component predicates and thresholds are owned by the parent-side grader.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from evidence import (digest, write_new, file_digest, LoaderObservation, observe_native,
                      implementation_manifest, new_fixture, fixture_bytes, capture_and_restore)
from fixtures import CASE_IDS, agent_input


def segments(observation, *, historical=False):
    scopes = observation['catalog']['scopes'].values()
    return [segment for scope in scopes for task in scope['tasks'].values()
            for segment in (task['history'] if historical else task['segments'].values())]


def state_assertions(case_id, observed):
    """Deterministic, source-pinned predicates; never passed to a task model."""
    current = segments(observed)
    historical = segments(observed, historical=True)
    tasks = [task for scope in observed['catalog']['scopes'].values() for task in scope['tasks'].values()]
    active = [task for task in tasks if task['status'] not in {'completed', 'failed', 'cancelled'}]
    def has(kind, marker, rows=current):
        return any(row['kind'] == kind and marker in row['quote'] for row in rows)
    def text(marker, rows=current):
        return any(marker in row['quote'] for row in rows)
    context = observed.get('context', '')
    base = {'active_goal': has('goal', 'ATLAS-AKTIF'), 'open_work': has('open', 'ATLAS-ACIK'),
            'next_step': has('next', 'ATLAS-SONRA'), 'fresh_session_visible': 'ATLAS-AKTIF' in context}
    if case_id == 'EVAL-029':
        return {'current_B': has('decision', 'KARAR-B'), 'old_A_historical': has('decision', 'KARAR-A', historical),
                'old_A_not_current': not has('decision', 'KARAR-A')}
    if case_id == 'EVAL-031':
        return {'corrected_segment': any(row['kind'] == 'decision' and 'ATLAS-AYAR' in row['quote'] and '48' in row['quote'] for row in current),
                'old_value_preserved': any('32' in row['quote'] for row in historical),
                'newer_user_state_preserved': text('YENI-ORION'), 'valid_progress_preserved': text('ATLAS-TAMAM'),
                'reason_preserved': text('yeni kapasite ölçümü'),
                'actual_verification_passed': observed.get('verified_progress_receipt', {}).get('exit_code') == 0,
                'verification_input_preserved': observed.get('verified_progress_receipt', {}).get('input_unchanged') is True,
                'verification_source_preserved': any(event['source'] == observed.get('progress_source_digest')
                    for scope in observed['catalog']['scopes'].values() for event in scope['events'])}
    if case_id == 'EVAL-045':
        return {'wrong_meaning_rejected': observed.get('source_validation_rejected') is True,
                'no_wrong_commit': observed.get('catalog_before') == observed['catalog']}
    if case_id in {'EVAL-046', 'EVAL-047', 'EVAL-048'}:
        result = dict(base, current_decision=has('decision', 'Yerel önbelleği'))
        if case_id == 'EVAL-047':
            result['tangent_not_goal'] = not any(row['kind'] == 'goal' and 'Güneş' in row['quote'] for row in current)
        return result
    if case_id == 'EVAL-049':
        return {'terminal_retained': any(task['status'] == 'completed' for task in tasks), 'not_resurrected': not active}
    if case_id == 'EVAL-050':
        return dict(base, failed_branch_isolated=any(task['status'] in {'failed', 'cancelled'} and any('ATLAS-ESKI' in row['quote'] for row in task['segments'].values()) for task in tasks),
                    old_branch_not_active=not any(row['kind'] in {'goal', 'decision', 'next'} and 'ATLAS-ESKI' in row['quote'] for task in active for row in task['segments'].values()))
    if case_id == 'EVAL-051':
        return {'selected_current': has('decision', 'SECILEN-YEREL'),
                'alternative_not_decision': not has('decision', 'SECILMEYEN-BULUT')}
    if case_id == 'EVAL-052':
        return {'conditional_preserved': has('conditional', 'BELKI-SONRA'),
                'not_committed': not any(row['kind'] in {'decision', 'open', 'next'} and 'BELKI-SONRA' in row['quote'] for row in current)}
    if case_id == 'EVAL-053':
        return {'reply_bound': any(row['kind'] == 'reply' and 'ORION-KANIT' in row['quote'] and row['target'] and any(old['id'] == row['target'] and 'ATLAS-BEKLEYEN' in old['quote'] for old in historical) for row in current),
                'unrelated_open_preserved': has('open', 'ATLAS-ACIK')}
    if case_id == 'EVAL-075':
        return {'unresolved_blocker': observed.get('unresolved', 0) > 0, 'warning_visible': observed.get('warning_visible') is True}
    if case_id == 'EVAL-076':
        return {'no_active_old_error': observed.get('unresolved') == 0, 'recovered_history': observed.get('recovered', 0) > 0,
                'no_stale_warning': observed.get('warning_visible') is False}
    if case_id == 'EVAL-077':
        return {'last_valid_retained': observed.get('catalog_before') == observed['catalog'],
                'retry_visible': observed.get('retry_visible') is True, 'not_success': observed.get('interrupted_exit_code', 0) != 0}
    raise ValueError('unknown-case')


def grade(record, observation, evidence_ref):
    state = state_assertions(record['acceptance_id'], observation)
    predicates = {'STATE': state,
                  'FS': {'source_bytes_preserved': observation.get('source_unchanged') is True,
                         'durable_roundtrip': observation.get('persisted_catalog') == observation['catalog']},
                  'TRACE': {'production_route': observation.get('route') == 'production',
                            'real_events': bool(observation.get('events'))}}
    if record['acceptance_id'] == 'EVAL-077':
        predicates['TRACE'].update(state)
    components = {}
    for name in record['grader'].split('+'):
        checks = predicates[name]
        components[name] = {'verdict': 'PASS' if checks and all(checks.values()) else 'FAIL',
                            'evidence_ref': evidence_ref, 'assertions': checks}
    return {'execution': 'EXECUTED', 'verdict': 'PASS' if all(value['verdict'] == 'PASS' for value in components.values()) else 'FAIL',
            'components': components}


def negative_control(record, observation, folder=None):
    """Corrupt observed artifacts; run the unchanged grader, never rewrite gold."""
    before = grade(record, observation, str(folder / 'observation.json') if folder else 'unit-test-positive-observation')
    results = {}
    for component in record['grader'].split('+'):
        altered = deepcopy(observation)
        if component == 'FS':
            altered['source_unchanged'] = False
        elif component == 'TRACE':
            altered['events'] = []
        elif component == 'STATE':
            altered['catalog'] = {'schema': 1, 'scopes': {}}
            altered['unresolved'] = -1
            altered['recovered'] = 0
            altered['source_validation_rejected'] = False
        ref = str(folder / ('injected-' + component + '.json')) if folder else 'unit-test-injected-' + component
        if folder:
            write_new(Path(ref), altered)
        negative = grade(record, altered, ref)
        results[component] = {'positive': before, 'negative': negative,
                              'sensitive': before['verdict'] == 'PASS' and negative['components'][component]['verdict'] == 'FAIL'}
    return results


def smoke_controls(record, observation, folder):
    controls = negative_control(record, observation, folder)
    # Composite canonical graders already wrote their TRACE injection. Only
    # add the harness-only probe when TRACE is absent from that canonical set.
    if 'TRACE' not in controls:
        trace_observation = deepcopy(observation)
        trace_observation['events'] = []
        write_new(folder / 'injected-TRACE.json', trace_observation)
        controls['TRACE'] = {'scope': 'HARNESS_ONLY',
                             'positive': bool(observation['events']) and observation['route'] == 'production',
                             'negative': bool(trace_observation['events']),
                             'sensitive': bool(observation['events']) and not trace_observation['events'],
                             'evidence_ref': str(folder / 'injected-TRACE.json')}
    return controls


def catalog(path):
    mapping = json.loads(Path(path).read_text(encoding='utf-8'))
    records = {record['acceptance_id']: record for record in mapping['acceptance_catalog'] if record['acceptance_id'] in CASE_IDS}
    if set(records) != set(CASE_IDS) or sum(record['run_count'] for record in records.values()) != 36:
        raise ValueError('canonical-selection-invalid')
    return mapping, records


@contextmanager
def codex_home(path):
    previous = os.environ.get('CODEX_HOME')
    os.environ['CODEX_HOME'] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('CODEX_HOME', None)
        else:
            os.environ['CODEX_HOME'] = previous


def seed(root, fixture_home):
    """Create only synthetic source and locator, never copy credentials or gold."""
    root.mkdir(parents=True, exist_ok=True)
    companion = root / '🔮 850-Companion'
    companion.mkdir()
    for name in ('Core', 'Profile', 'Last-Session', 'Journal', 'Threads', 'Kurallar'):
        (companion / (name + '.md')).write_text('# Sentetik ' + name + '\n', encoding='utf-8')
    state = root / '.codex/scripts/.state'
    state.mkdir(parents=True)
    manifest = fixture_home / 'manifest.json'
    write_new(manifest, {'manifest_id': 'synthetic-execution-eval', 'files': []})
    stat = root.stat()
    write_new(fixture_home / 'cevdet-codex/.cevdet-codex-install-state.json', {
        'PRE_CEVDET': {'note_universe': {'path': str(manifest), 'manifest_id': 'synthetic-execution-eval',
            'manifest_file_sha256': file_digest(manifest), 'role': 'FROZEN_INVENTORY_ONLY',
            'retrieval_promotion_egress_authority': False,
            'source_vault_identity': {'canonical_root': root.resolve().as_posix(), 'device': stat.st_dev, 'directory_file_id': stat.st_ino}}}})
    return state


def prepare_source(case_id, source, folder):
    """Actual verifier output is source material, never a hidden expected label."""
    if case_id != 'EVAL-031':
        return source
    dataset = folder / 'verified-progress-input.json'
    write_new(dataset, [{'id': 'one', 'capacity': 16}, {'id': 'two', 'capacity': 32}, {'id': 'three', 'capacity': 48}])
    code = ('import json,sys; rows=json.load(open(sys.argv[1],encoding="utf-8")); '
            'assert len(rows)==3 and len({r["id"] for r in rows})==3; '
            'assert all(type(r["capacity"]) is int and r["capacity"]>0 for r in rows); '
            'print("ATLAS-DOGRULAMA-RECEIPT: veri modeli doğrulandı")')
    command = [sys.executable, '-I', '-B', '-X', 'utf8', '-c', code, str(dataset)]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=10)
    receipt = {'command': command, 'exit_code': result.returncode, 'stdout': result.stdout,
               'stderr': result.stderr, 'input_sha256': file_digest(dataset)}
    write_new(folder / 'verified-progress-receipt.json', receipt)
    if result.returncode != 0:
        raise ValueError('EVAL_ENV_INVALID:progress-verification-failed')
    enriched = deepcopy(source)
    at = next(index for index, message in enumerate(enriched['messages']) if 'Yalnız ATLAS-TAMAM' in message['text'])
    enriched['messages'].insert(at, {'role': 'tool', 'text': json.dumps(receipt, ensure_ascii=False)})
    return enriched


def synthetic_typed_metadata(prompt, answer):
    """Record only final schema fields for this synthetic fixture runner."""
    metadata = {'prompt_digest': digest(prompt), 'source_digest': None,
                'output_digest': digest(answer), 'typed_output_available': False}
    try:
        envelope = json.loads(prompt.rsplit('\n', 1)[-1])
        if isinstance(envelope, dict) and isinstance(envelope.get('turns'), list):
            metadata['source_digest'] = digest(envelope['turns'])
    except (ValueError, TypeError):
        pass
    try:
        value = json.loads(answer) if isinstance(answer, str) else None
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict):
        if isinstance(value.get('operations'), list):
            metadata['final_typed_output'] = {'operations': [
                {key: operation[key] for key in ('kind', 'quote', 'target', 'turn') if key in operation}
                for operation in value['operations'] if isinstance(operation, dict)]}
        elif type(value.get('valid')) is bool:
            metadata['final_typed_output'] = {'valid': value['valid']}
    metadata['typed_output_available'] = 'final_typed_output' in metadata
    return metadata


def run_sut(source, repo, folder, *, semantic_corruption=False):
    """Real propose/publish/reconstruct surface; no canonical metadata argument.

    Deliberately does not claim the full flush/worker route. Those canonical
    cases must use their actual dispatcher, interruption and recovery paths.
    """
    scripts, hooks = repo / '.codex/scripts', repo / '.codex/hooks'
    sys.path[:0] = [str(scripts), str(hooks)]
    observer = LoaderObservation([scripts, hooks, Path(__file__).parent])
    observer.start()
    import flush
    import companion_memory
    import execution_state
    import hook
    import codex_runner
    native_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
    native_config = native_home / 'config.toml'
    native_config_before = file_digest(native_config) if native_config.is_file() else None
    root = new_fixture()
    fixture_home = root.parent / 'fixture-home'
    state = seed(root, fixture_home)
    unrelated = root / 'unrelated-user-source.md'
    unrelated.write_text('Sentetik bağımsız yeni kullanıcı kaydı; korunur.\n', encoding='utf-8')
    unrelated_before = file_digest(unrelated)
    source_path = folder / 'source.json'
    write_new(source_path, source)
    source_before = file_digest(source_path)
    with codex_home(fixture_home):
        companion_memory.migrate(root, state=state)
    baseline_bytes = fixture_bytes(root)
    fixture_identity = json.loads((fixture_home / 'cevdet-codex/.cevdet-codex-install-state.json').read_text(encoding='utf-8'))
    write_new(folder / 'fixture-identity.json', fixture_identity)
    seed_snapshot = {'source_sha256': source_before,
                     'files': {name: file_digest(root / name) for name in baseline_bytes},
                     'fixture_identity': fixture_identity,
                     'identity_manifest_sha256': file_digest(fixture_home / 'manifest.json')}
    write_new(folder / 'seed-snapshot.json', seed_snapshot)
    events, native_events = [], []
    progress_source_digest = None
    start = time.monotonic_ns()

    def model(prompt, vault, **kwargs):
        with codex_home(native_home):
            answer, reason = flush.run_codex(prompt, vault, **kwargs)
        events.append({'event': 'real_model_response', 'purpose': kwargs.get('purpose'),
                       'reason': reason, 'output_present': bool(answer)})
        write_new(folder / ('native-call-' + str(len(native_events)) + '.json'),
                  {'response': events[-1], 'native': native_events[-1] if native_events else None,
                   **synthetic_typed_metadata(prompt, answer)})
        return answer, reason

    with codex_home(fixture_home), observe_native(codex_runner, native_events):
        binding = execution_state.identity(root, str(root))
        validation_rejected = False
        before = companion_memory.execution_snapshot(root)
        batches, pending = [], []
        for message in source['messages']:
            pending.append(message)
            if message['role'] == 'user':
                batches.append(pending)
                pending = []
        for index, batch in enumerate(batches):
            stamp = dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc) + dt.timedelta(days=index)
            turns = [(message['role'], message['text']) for message in batch]
            if any(message['role'] == 'tool' and 'ATLAS-DOGRULAMA-RECEIPT' in message['text'] for message in batch):
                progress_source_digest = digest(batch).removeprefix('sha256:')
            before = companion_memory.execution_snapshot(root)
            try:
                update = execution_state.propose(root, binding, before, 'synthetic-source', stamp.isoformat(), turns, model)
            except ValueError as exc:
                last_model = next((event for event in reversed(events) if event['event'] == 'real_model_response'), None)
                if (isinstance(exc, json.JSONDecodeError)
                        or str(exc) in {'execution-proposal-unavailable', 'execution-proposal-invalid'}
                        or last_model is None or last_model['reason'] or not last_model['output_present']):
                    write_new(folder / 'incomplete-execution.json', {'reason': str(exc), 'events': events, 'native_events': native_events})
                    raise ValueError('EVAL_ENV_INVALID:' + str(exc)) from exc
                events.append({'event': 'production_proposal_rejected', 'reason': str(exc)})
                break
            events.append({'event': 'real_proposal', 'update_present': update is not None})
            if semantic_corruption and update is not None:
                proposed = deepcopy(update['event']['operations'])
                # The quote/hash remains identical; its interpretation is wrong.
                # This is adversarial input to the real independent validator,
                # never a stubbed response or precomputed success boolean.
                proposed[0]['kind'] = 'completed'
                proposed[0]['target'] = ''
                scope = before['scopes'].get(execution_state.digest(binding), {'tasks': {}})
                try:
                    execution_state.validate_proposal(root, scope['tasks'], [{'role': role, 'text': text} for role, text in turns], proposed, model)
                except ValueError as exc:
                    if str(exc) != 'execution-source-validation-failed':
                        raise
                    if events[-1].get('reason') or not events[-1].get('output_present'):
                        raise ValueError('EVAL_ENV_INVALID:source-validator-unavailable') from exc
                    validation_rejected = True
                events.append({'event': 'real_source_validator', 'adversarial_candidate': proposed,
                               'rejected': validation_rejected})
                break
            if update is not None:
                # The memory summary is source text, not expected state. Execution
                # state itself is entirely the production model+validator result.
                source_text = '\n'.join(message['role'] + ': ' + message['text'] for message in batch)
                summary = '\n'.join('## ' + heading + '\n' + (source_text if heading == flush.EXPECTED_SECTIONS[0] else 'Yok.') for heading in flush.EXPECTED_SECTIONS)
                companion_memory.publish(root, state, summary, stamp, digest([index, batch]).removeprefix('sha256:'), 'synthetic-source', frozenset(), execution_update=update)
                events.append({'event': 'production_companion_publish', 'revision': index})
        actual = companion_memory.execution_snapshot(root)
        # build_session_context consumes persisted state and the actual MemoryRead
        # route; no expected structure, state object, or model result is passed.
        context = hook.build_session_context(root, state, write_views=False, cwd=str(root))
        events.append({'event': 'production_session_context'})
        persisted = companion_memory.execution_snapshot(root)
    files = observer.finish()
    if (file_digest(native_config) if native_config.is_file() else None) != native_config_before:
        raise ValueError('EVIDENCE_BINDING_INVALID:native-config-drift')
    write_new(folder / 'loader.json', {'observed_paths': [item['path'] for item in files], 'native_events': native_events})
    config = {'native_home': str(native_home), 'fixture_home_role': 'synthetic-identity-only',
              'native_config_ref': str(native_config), 'native_config_sha256': native_config_before,
              'native_effective_observation': 'explicit argv plus native process banner; no credential/config values captured',
              'model': codex_runner.MEMORY_MODEL, 'effort': codex_runner.MEMORY_REASONING,
              'surface': 'production-propose-publish-reconstruct', 'semantic_output_stub': False}
    common_closure = folder.parent / 'loader-closure.json'
    closure = {'observed_paths': [item['path'] for item in files]}
    if common_closure.exists():
        if json.loads(common_closure.read_text(encoding='utf-8')) != closure:
            raise ValueError('EVIDENCE_BINDING_INVALID:closure-changed')
    else:
        write_new(common_closure, closure)
    manifest = implementation_manifest(files, config, 'working-tree', str(common_closure), native_events)
    write_new(folder / 'implementation.json', manifest)
    observation = {'catalog': actual, 'persisted_catalog': persisted, 'context': context,
                   'source_unchanged': source_before == file_digest(source_path) and unrelated_before == file_digest(unrelated),
                   'route': 'production', 'surface': config['surface'], 'events': events,
                   'native_events': native_events, 'wall_ms': (time.monotonic_ns() - start) // 1_000_000,
                   'catalog_before': before, 'source_validation_rejected': validation_rejected,
                   'fixture_seed_or_snapshot_ref': str(folder / 'seed-snapshot.json'), 'fixture_state_digest': digest(seed_snapshot),
                   'implementation_manifest_ref': str(folder / 'implementation.json'),
                   'implementation_digest': manifest['implementation_digest'], 'effective_config_digest': manifest['effective_config_digest']}
    observation['cleanup_verified'] = capture_and_restore(root, baseline_bytes, folder,
        allowed_roots=('.codex/scripts/.state',), allowed_files=(
            'daily/companion-sessions.json', '.codex/private-memory/controls/suppressions.lock',
            '🔮 850-Companion/Last-Session.md', '🔮 850-Companion/Journal.md', '🔮 850-Companion/Threads.md'))
    if (folder / 'verified-progress-receipt.json').is_file():
        receipt = json.loads((folder / 'verified-progress-receipt.json').read_text(encoding='utf-8'))
        observation['verified_progress_receipt'] = {'exit_code': receipt['exit_code'],
                                                   'input_unchanged': file_digest(folder / 'verified-progress-input.json') == receipt['input_sha256'],
                                                   'evidence_ref': str(folder / 'verified-progress-receipt.json')}
        observation['progress_source_digest'] = progress_source_digest
    write_new(folder / 'observation.json', observation)
    return observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--harness-tools', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--run', action='store_true', help='Execute the selected canonical repeat policy using real native calls.')
    parser.add_argument('--smoke', action='store_true', help='One real EVAL-029 run plus artifact corruption; not canonical acceptance.')
    parser.add_argument('--harness-evidence', type=Path, action='append', help='Successful same-implementation smoke trace; repeat for worker surfaces.')
    parser.add_argument('--case', choices=CASE_IDS, action='append')
    args = parser.parse_args()
    mapping, records = catalog(args.map)
    sys.path.insert(0, str(args.harness_tools))
    import harness_metrics
    harnesses = []
    if args.run:
        if args.harness_evidence is None:
            raise ValueError('HARNESS_EVIDENCE_REQUIRED')
        for reference in args.harness_evidence:
            harness = json.loads(reference.read_text(encoding='utf-8'))
            if harness.get('verdict') != 'PASS' or harness.get('harness_smoke', {}).get('sensitive') is not True:
                raise ValueError('HARNESS_EVIDENCE_INVALID')
            prior_manifest = json.loads(Path(harness['implementation_manifest_ref']).read_text(encoding='utf-8'))
            if any(file_digest(Path(prior_manifest['root']) / item['path']) != item['sha256'] for item in prior_manifest['files']):
                raise ValueError('EVIDENCE_BINDING_INVALID:harness-code-drift')
            harnesses.append((reference, harness))
    runs = {case: [] for case in CASE_IDS}
    selected = args.case or list(CASE_IDS)
    if args.smoke:
        selected = args.case or ['EVAL-029']
    if args.run or args.smoke:
        for case in selected:
            count = 1 if args.smoke else records[case]['run_count']
            for index in range(count):
                folder = args.output.parent / (('smoke-' if args.smoke else '') + case + '-' + str(index + 1))
                if folder.exists():
                    raise ValueError('evidence-folder-exists')
                folder.mkdir(parents=True)
                started = time.monotonic_ns()
                try:
                    if case in {'EVAL-075', 'EVAL-076', 'EVAL-077'}:
                        from worker_cases import run_worker_case
                        observation = run_worker_case(case, agent_input(case), args.repo, folder)
                    else:
                        observation = run_sut(prepare_source(case, agent_input(case), folder), args.repo, folder, semantic_corruption=case == 'EVAL-045')
                    if args.run and observation.get('cleanup_verified') is not True:
                        raise ValueError('EVAL_ENV_INVALID:fixture-reset-unverified')
                    run = grade(records[case], observation, str(folder / 'observation.json'))
                    run.update({key: observation[key] for key in ('wall_ms', 'implementation_digest', 'effective_config_digest',
                               'implementation_manifest_ref', 'fixture_seed_or_snapshot_ref', 'fixture_state_digest')})
                    run.update(case_id=case, catalog_digest=mapping['catalog_digest'], contract_digest=records[case]['contract_digest'],
                               trace_source='SYNTHETIC', execution_evidence='REAL_NATIVE_PRODUCTION_COMPONENTS')
                    if harnesses:
                        reference = next((reference for reference, harness in harnesses if all(run[key] == harness[key]
                                          for key in ('implementation_digest', 'effective_config_digest', 'catalog_digest'))), None)
                        if reference is None:
                            raise ValueError('EVIDENCE_BINDING_INVALID:no-matching-harness')
                        run['harness_evidence_ref'] = str(reference)
                    if args.smoke:
                        controls = smoke_controls(records[case], observation, folder)
                        write_new(folder / 'negative-controls.json', controls)
                        run['harness_smoke'] = {'scope': 'HARNESS_ONLY_NOT_CANONICAL_VERDICT', 'sensitive': all(value['sensitive'] for value in controls.values())}
                except (OSError, ValueError, ImportError, KeyError) as exc:
                    run = {'execution': 'INVALID', 'verdict': 'NO_VERDICT', 'reason': type(exc).__name__ + ':' + str(exc),
                           'wall_ms': (time.monotonic_ns() - started) // 1_000_000, 'components': {}}
                write_new(folder / 'trace.json', run)
                runs[case].append(run)
                print(json.dumps({'case': case, 'run': index + 1, 'execution': run['execution'], 'verdict': run['verdict']}), flush=True)
    result = {'catalog_digest': mapping['catalog_digest'],
              'results': {case: dict(harness_metrics.collapse(records[case], runs[case]),
                                    selection='SELECTED' if case in selected else 'NOT_SELECTED') for case in CASE_IDS},
              'planned_runs': 36, 'executed_runs': sum(run['execution'] == 'EXECUTED' for values in runs.values() for run in values),
              'smoke_only': args.smoke,
              'deferred': {'EVAL-098': '6.2', 'EVAL-100': '6.3'}}
    write_new(args.output, result)
    print(json.dumps({'output': str(args.output), 'executed_runs': result['executed_runs']}))
    if args.smoke:
        return 0 if all(runs[case] and runs[case][0].get('harness_smoke', {}).get('sensitive') is True
                        and runs[case][0]['verdict'] == 'PASS' for case in selected) else 2
    return 0 if all(result['results'][case]['verdict'] == 'PASS' for case in selected) else 2


if __name__ == '__main__':
    raise SystemExit(main())
