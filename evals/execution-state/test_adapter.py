"""Adapter unit tests. These are not executions of canonical product EVALs."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import adapter
from evidence import LoaderObservation, write_new, new_fixture, fixture_bytes, capture_and_restore
from fixtures import CASE_IDS, agent_input


def observation():
    old = {'id': 'old', 'kind': 'decision', 'quote': 'KARAR-A', 'target': ''}
    current = {'id': 'old', 'kind': 'decision', 'quote': 'KARAR-B', 'target': 'old'}
    catalog = {'schema': 1, 'scopes': {'scope': {'tasks': {'task': {
        'status': 'active', 'history': [old], 'segments': {'old': current}}}}}}
    return {'catalog': catalog, 'persisted_catalog': deepcopy(catalog),
            'source_unchanged': True, 'events': ['publication'], 'route': 'production', 'context': ''}


class AdapterTests(unittest.TestCase):
    def test_completed_fixture_starts_the_same_report_task(self):
        messages = agent_input('EVAL-049')['messages']
        self.assertIn('ATLAS-KAPALI rapor', messages[0]['text'])
        self.assertIn('ATLAS-KAPALI rapor', messages[1]['text'])
        self.assertEqual(messages[-1]['text'], 'Devam.')

    def test_typed_metadata_excludes_unrequested_model_fields_and_raw_invalid_output(self):
        prompt = 'Instructions\n' + json.dumps({'turns': [{'role': 'user', 'text': 'synthetic'}]})
        answer = json.dumps({'operations': [{'kind': 'decision', 'quote': 'synthetic', 'target': '',
                                            'turn': 0, 'reasoning': 'do-not-record'}],
                             'reasoning': 'do-not-record'})
        metadata = adapter.synthetic_typed_metadata(prompt, answer)
        self.assertTrue(metadata['typed_output_available'])
        self.assertIsNotNone(metadata['source_digest'])
        self.assertNotIn('do-not-record', json.dumps(metadata))
        invalid = adapter.synthetic_typed_metadata(prompt, 'raw-invalid-output')
        self.assertFalse(invalid['typed_output_available'])
        self.assertNotIn('raw-invalid-output', json.dumps(invalid))

    def test_worker_smoke_reuses_canonical_trace_injection_without_overwrite(self):
        record = {'acceptance_id': 'EVAL-075', 'grader': 'STATE+TRACE'}
        observed = dict(observation(), unresolved=1, warning_visible=True)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            controls = adapter.smoke_controls(record, observed, folder)
            self.assertTrue(all(value['sensitive'] for value in controls.values()))
            self.assertEqual(controls['TRACE']['negative']['components']['TRACE']['verdict'], 'FAIL')
            self.assertEqual(len(list(folder.glob('injected-TRACE.json'))), 1)

    def test_smoke_adds_trace_probe_when_canonical_grader_has_no_trace(self):
        record = {'acceptance_id': 'EVAL-029', 'grader': 'STATE+FS'}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            controls = adapter.smoke_controls(record, observation(), folder)
            self.assertEqual(controls['TRACE']['scope'], 'HARNESS_ONLY')
            self.assertTrue(all(value['sensitive'] for value in controls.values()))
            self.assertTrue((folder / 'injected-TRACE.json').is_file())

    def test_grader_detects_each_component_corruption(self):
        record = {'acceptance_id': 'EVAL-029', 'grader': 'STATE+FS+TRACE'}
        controls = adapter.negative_control(record, observation())
        self.assertEqual(set(controls), {'STATE', 'FS', 'TRACE'})
        self.assertTrue(all(row['sensitive'] for row in controls.values()))

    def test_currentness_history_and_source_are_independent(self):
        record = {'acceptance_id': 'EVAL-029', 'grader': 'STATE+FS'}
        observed = observation()
        observed['catalog']['scopes']['scope']['tasks']['task']['history'] = []
        observed['persisted_catalog'] = deepcopy(observed['catalog'])
        result = adapter.grade(record, observed, 'actual-observation')
        self.assertEqual(result['components']['STATE']['verdict'], 'FAIL')
        self.assertEqual(result['components']['FS']['verdict'], 'PASS')

    def test_model_input_has_only_source_and_query(self):
        for case in CASE_IDS:
            source = agent_input(case)
            self.assertEqual(set(source), {'messages', 'query'})
            self.assertTrue(source['messages'])
            self.assertTrue(all(set(message) == {'role', 'text'} for message in source['messages']))

    def test_loaded_file_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / 'config.toml'
            file.write_text('x=1', encoding='utf-8')
            observer = LoaderObservation([root])
            observer.start()
            file.read_text(encoding='utf-8')
            file.write_text('x=2', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'loaded-file-drift'):
                observer.finish()

    def test_evidence_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'evidence.json'
            write_new(path, {'original': True})
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_new(path, {'original': False})
            self.assertEqual(path.read_bytes(), before)

    def test_real_seed_and_scoped_reset_preserve_sentinel_and_captured_output(self):
        import sys
        repo = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo / '.codex/scripts'))
        import companion_memory
        root = new_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            fixture_home = root.parent / 'fixture-home'
            state = adapter.seed(root, fixture_home)
            with adapter.codex_home(fixture_home):
                companion_memory.migrate(root, state=state)
            sentinel = root / 'unrelated.md'
            sentinel.write_bytes(b'unchanged')
            mutable = root / '.codex/scripts/.state/existing.json'
            mutable.write_bytes(b'{"seed":1}')
            baseline = fixture_bytes(root)
            mutable.write_bytes(b'{"runtime":2}')
            generated = root / '.codex/scripts/.state/new-runtime-file.json'
            generated.write_bytes(b'{"runtime":1}')
            self.assertTrue(capture_and_restore(root, baseline, evidence,
                            allowed_roots=('.codex/scripts/.state',)))
            self.assertEqual(sentinel.read_bytes(), b'unchanged')
            self.assertEqual((evidence / 'post-fixture/.codex/scripts/.state/new-runtime-file.json').read_bytes(), b'{"runtime":1}')
            self.assertEqual(fixture_bytes(root), baseline)

    def test_changed_sentinel_refuses_cleanup_without_restoring_or_deleting(self):
        root = new_fixture()
        sentinel = root / 'unrelated.md'
        sentinel.write_bytes(b'original')
        baseline = fixture_bytes(root)
        sentinel.write_bytes(b'newer-user-data')
        owned = root / '.state/result.json'
        owned.parent.mkdir()
        owned.write_bytes(b'run-output')
        before = fixture_bytes(root)
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'unrelated-state-changed'):
                capture_and_restore(root, baseline, evidence, allowed_roots=('.state',))
            self.assertEqual(fixture_bytes(root), before)
            self.assertTrue((evidence / 'cleanup.json').is_file())

    def test_new_unrelated_file_refuses_cleanup_without_any_mutation(self):
        root = new_fixture()
        (root / 'source.md').write_bytes(b'immutable-source')
        baseline = fixture_bytes(root)
        (root / 'new-user-note.md').write_bytes(b'new-user-data')
        owned = root / '.state/result.json'
        owned.parent.mkdir()
        owned.write_bytes(b'run-output')
        before = fixture_bytes(root)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'unrelated-state-changed'):
                capture_and_restore(root, baseline, Path(temporary), allowed_roots=('.state',))
            self.assertEqual(fixture_bytes(root), before)

    def test_cleanup_rejects_nonowned_root_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / 'untouched.md'
            sentinel.write_bytes(b'protected')
            with self.assertRaises((ValueError, FileNotFoundError)):
                capture_and_restore(root, {}, root / 'evidence')
            self.assertEqual(sentinel.read_bytes(), b'protected')


if __name__ == '__main__':
    unittest.main()
