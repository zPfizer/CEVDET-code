"""Synthetic invariants, not canonical EVAL evidence."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import companion_memory as companion
import execution_state as execution
import flush
import hook
from memory_ledger import memory_read, suppress_derived_memory


BINDING = {'vault': 'a' * 64, 'project': '.', 'manifest': 'synthetic-v1', 'manifest_sha256': 'b' * 64}
STAMP = '2026-09-03T12:00:00+00:00'


def event(*kinds, stamp=STAMP, session='session-a', target=''):
    value = {'session': session, 'stamp': stamp, 'source': 'c' * 64,
             'operations': [{'kind': kind, 'quote': kind + ' sentetik kullanıcı kaynağı', 'target': target, 'turn': 0} for kind in kinds]}
    return {**value, 'id': execution.digest(value)}


def install(root, home):
    manifest = home / 'manifest.json'
    manifest.write_text('{"synthetic":true}', encoding='utf-8')
    stat = root.stat()
    locator = home / 'cevdet-codex/.cevdet-codex-install-state.json'
    locator.parent.mkdir()
    locator.write_text(json.dumps({'PRE_CEVDET': {'note_universe': {
        'manifest_id': 'synthetic-v1', 'path': str(manifest),
        'manifest_file_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
        'source_vault_identity': {'canonical_root': root.as_posix(), 'device': stat.st_dev, 'directory_file_id': stat.st_ino},
        'role': 'FROZEN_INVENTORY_ONLY', 'retrieval_promotion_egress_authority': False,
    }}}), encoding='utf-8')


class ExecutionTests(unittest.TestCase):
    def test_mixed_role_turn_indices_are_explicit_without_changing_source_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            for prior_role in ('assistant', 'tool'):
                turns = [(prior_role, 'Önceki bağlam; kullanıcı talimatı değildir.'), ('user', 'Atlas raporunu hazırla.')]
                operation = {'kind': 'goal', 'quote': turns[1][1], 'target': '', 'turn': 1}
                seen = []
                def model(prompt, _root, *, purpose):
                    shown = json.loads(prompt.rsplit('\n', 1)[1])['turns']
                    seen.append(shown)
                    return (json.dumps({'operations': [operation]}) if purpose == 'execution-proposal'
                            else '{"valid":true}', None)
                update = execution.propose(Path(tmp), BINDING, execution.empty(), 'mixed', STAMP, turns, model)
                self.assertEqual(len(seen), 2)
                for displayed in seen:
                    self.assertEqual(displayed, [{'turn': 0, 'role': prior_role, 'text': turns[0][1]},
                                                 {'turn': 1, 'role': 'user', 'text': turns[1][1]}])
                self.assertEqual(update['event']['source'], execution.digest(
                    [{'role': role, 'text': text} for role, text in turns]))
                wrong = mock.Mock(return_value=(json.dumps({'operations': [{**operation, 'turn': 0}]}), None))
                with self.assertRaisesRegex(ValueError, 'source-unbound'):
                    execution.propose(Path(tmp), BINDING, execution.empty(), 'mixed', STAMP, turns, wrong)
                self.assertEqual(wrong.call_count, 1)

    def test_whole_task_terminal_targeting_goal_allows_a_separate_new_task(self):
        for kind in execution.TERMINAL:
            with self.subTest(kind=kind):
                catalog = execution.advance(execution.empty(), BINDING, 0, event('goal', 'open'))
                task = next(iter(next(iter(catalog['scopes'].values()))['tasks'].values()))
                old_id = task['id']
                goal = next(row['id'] for row in task['segments'].values() if row['kind'] == 'goal')
                catalog = execution.advance(catalog, BINDING, 1,
                    event(kind, target=goal, stamp='2026-09-04T12:00:00+00:00'))
                catalog = json.loads(json.dumps(catalog, sort_keys=True))
                catalog = execution.advance(catalog, BINDING, 2,
                    event('goal', stamp='2026-09-05T12:00:00+00:00'))
                tasks = next(iter(catalog['scopes'].values()))['tasks']
                self.assertEqual(tasks[old_id]['status'], kind)
                self.assertEqual(len(tasks), 2)
                self.assertEqual(sum(t['status'] not in execution.TERMINAL for t in tasks.values()), 1)
                self.assertTrue(any(row['kind'] == 'goal' for row in tasks[old_id]['history']))

    def test_targeted_subgoal_terminal_does_not_close_main_task(self):
        for target_kind in ('open', 'next'):
            catalog = execution.advance(execution.empty(), BINDING, 0, event('goal', 'open', 'next'))
            task = next(iter(next(iter(catalog['scopes'].values()))['tasks'].values()))
            target = next(row['id'] for row in task['segments'].values() if row['kind'] == target_kind)
            changed = execution.advance(catalog, BINDING, 1,
                event('completed', target=target, stamp='2026-09-04T12:00:00+00:00'))
            task = next(iter(next(iter(changed['scopes'].values()))['tasks'].values()))
            self.assertEqual(task['status'], 'active')
            self.assertEqual(task['segments'][target]['kind'], 'completed')
            self.assertTrue(any(row['kind'] == 'goal' for row in task['segments'].values()))

    def test_conditional_is_noncommittal_and_only_waiting_work_accepts_reply(self):
        catalog = execution.advance(execution.empty(), BINDING, 0, event('goal', 'open'))
        catalog = execution.advance(catalog, BINDING, 1,
            event('conditional', stamp='2026-09-04T12:00:00+00:00'))
        task = next(iter(next(iter(catalog['scopes'].values()))['tasks'].values()))
        conditional = next(row['id'] for row in task['segments'].values() if row['kind'] == 'conditional')
        self.assertEqual(sum(row['kind'] == 'open' for row in task['segments'].values()), 1)
        with self.assertRaisesRegex(ValueError, 'reply-target-invalid'):
            execution.advance(catalog, BINDING, 2,
                event('reply', target=conditional, stamp='2026-09-05T12:00:00+00:00'))
        catalog = execution.advance(catalog, BINDING, 2,
            event('blocked', stamp='2026-09-05T12:00:00+00:00'))
        task = next(iter(next(iter(catalog['scopes'].values()))['tasks'].values()))
        waiting = next(row['id'] for row in task['segments'].values() if row['kind'] == 'blocked')
        catalog = execution.advance(catalog, BINDING, 3,
            event('reply', target=waiting, stamp='2026-09-06T12:00:00+00:00'))
        task = next(iter(next(iter(catalog['scopes'].values()))['tasks'].values()))
        self.assertEqual(task['segments'][waiting]['kind'], 'reply')
        self.assertTrue(any(row['id'] == waiting and row['kind'] == 'blocked' for row in task['history']))
        self.assertTrue(any(row['kind'] == 'open' for row in task['segments'].values()))
        self.assertEqual(task['segments'][conditional]['kind'], 'conditional')

    def test_new_decision_cannot_overwrite_goal_segment(self):
        catalog = execution.advance(execution.empty(), BINDING, 0, event('goal'))
        task = next(iter(next(iter(catalog['scopes'].values()))['tasks'].values()))
        goal = next(iter(task['segments']))
        before = copy.deepcopy(catalog)
        with self.assertRaisesRegex(ValueError, 'segment-kind-conflict'):
            execution.advance(catalog, BINDING, 1,
                event('decision', target=goal, stamp='2026-09-04T12:00:00+00:00'))
        self.assertEqual(catalog, before)

    def test_semantic_validation_requires_exact_true_boolean(self):
        for reply in ('{"valid":false}', '{"valid":1}', '{"valid":1.0}', '{}',
                      '{"valid":"true"}', '{"valid":true,"extra":1}', 'true', '{'):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                execution.validate_proposal(Path('.'), {}, [], [], mock.Mock(return_value=(reply, None)))
        execution.validate_proposal(Path('.'), {}, [], [], mock.Mock(return_value=('{"valid":true}', None)))

    def test_revision_rejects_boolean_and_numeric_coercion(self):
        catalog = execution.advance(execution.empty(), BINDING, 0, event('goal'))
        for revision in (True, 1.0, '1', -1):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, 'revision-invalid'):
                execution.advance(catalog, BINDING, revision,
                    event('decision', stamp='2026-09-04T12:00:00+00:00'))

    def test_late_session_cannot_write_to_a_successor_task_with_fresh_cas(self):
        catalog = execution.advance(execution.empty(), BINDING, 0, event('goal', 'completed', session='sA'))
        catalog = execution.advance(catalog, BINDING, 1,
            event('goal', session='sB', stamp='2026-09-04T12:00:00+00:00'))
        before = copy.deepcopy(catalog)
        with self.assertRaisesRegex(ValueError, 'session-task-conflict'):
            execution.advance(catalog, BINDING, 2,
                event('decision', session='sA', stamp='2026-09-05T12:00:00+00:00'))
        self.assertEqual(catalog, before)
        joined = execution.advance(catalog, BINDING, 2,
            event('next', session='fresh-session', stamp='2026-09-05T12:00:00+00:00'))
        self.assertEqual(next(iter(joined['scopes'].values()))['revision'], 3)
        same = execution.advance(execution.empty(), BINDING, 0, event('goal', 'completed', session='same'))
        same = execution.advance(same, BINDING, 1,
            event('goal', session='same', stamp='2026-09-04T12:00:00+00:00'))
        reloaded = json.loads(json.dumps(same, sort_keys=True))
        changed = execution.advance(reloaded, BINDING, 2,
            event('decision', session='same', stamp='2026-09-05T12:00:00+00:00'))
        active = [task for task in next(iter(changed['scopes'].values()))['tasks'].values()
                  if task['status'] not in execution.TERMINAL]
        self.assertTrue(any(segment['kind'] == 'decision' for segment in active[0]['segments'].values()))

    def test_atomic_interruption_and_concurrent_cas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'vault'
            root.mkdir()
            home = Path(tmp) / 'home'
            home.mkdir()
            install(root, home)
            (root / '🔮 850-Companion').mkdir()
            state = root / '.codex/scripts/.state'
            summary = flush.SessionSummary({name: 'Yok.' for name in flush.EXPECTED_SECTIONS}).render()
            with mock.patch.dict(os.environ, {'CODEX_HOME': str(home)}):
                binding = execution.identity(root)
                initial = {'binding': binding, 'revision': 0, 'event': event('goal')}
                def publish(update, key):
                    companion.publish(root, state, summary, dt.datetime.fromisoformat(update['event']['stamp']),
                                      key * 64, update['event']['session'], frozenset(), execution_update=update)
                with mock.patch.object(companion, '_write_projection', side_effect=OSError('interrupted')):
                    with self.assertRaises(OSError):
                        publish(initial, 'a')
                committed = companion.execution_snapshot(root)
                publish(initial, 'a')
                self.assertEqual(companion.execution_snapshot(root), committed)
                updates = [{'binding': binding, 'revision': 1, 'event': event(kind, stamp='2026-09-04T12:00:00+00:00')}
                           for kind in ('open', 'next')]
                def contender(pair):
                    i, update = pair
                    try:
                        publish(update, 'b' if i == 0 else 'c')
                        return 'committed'
                    except ValueError as exc:
                        return str(exc)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(contender, enumerate(updates)))
                self.assertCountEqual(results, ['committed', 'execution-revision-conflict'])
                self.assertEqual(next(iter(companion.execution_snapshot(root)['scopes'].values()))['revision'], 2)

    def test_revision_idempotency_and_cross_scope(self):
        first = event('goal', 'open')
        catalog = execution.advance(execution.empty(), BINDING, 0, first)
        self.assertEqual(execution.advance(catalog, BINDING, 0, first), catalog)
        with self.assertRaisesRegex(ValueError, 'revision-conflict'):
            execution.advance(catalog, BINDING, 0, event('decision', stamp='2026-09-04T12:00:00+00:00'))
        other = {**BINDING, 'project': 'other'}
        separate = execution.advance(catalog, other, 0, event('goal'))
        self.assertEqual(separate['scopes'][execution.digest(BINDING)], catalog['scopes'][execution.digest(BINDING)])

    def test_local_correction_preserves_progress_and_history(self):
        catalog = execution.advance(execution.empty(), BINDING, 0, event('goal', 'decision', 'open'))
        task = next(iter(catalog['scopes'][execution.digest(BINDING)]['tasks'].values()))
        decision = next(s['id'] for s in task['segments'].values() if s['kind'] == 'decision')
        updated = execution.advance(catalog, BINDING, 1, event('decision', stamp='2026-09-04T12:00:00+00:00', target=decision))
        changed = next(iter(updated['scopes'][execution.digest(BINDING)]['tasks'].values()))
        self.assertEqual(len(changed['history']), 1)
        self.assertEqual(len(changed['segments']), 3)

    def test_terminal_never_resurrects_and_wrong_target_rejected(self):
        for terminal in execution.TERMINAL:
            catalog = execution.advance(execution.empty(), BINDING, 0, event('goal', terminal))
            updated = execution.advance(catalog, BINDING, 1, event('next', stamp='2026-09-04T12:00:00+00:00'))
            self.assertEqual(next(iter(updated['scopes'][execution.digest(BINDING)]['tasks'].values()))['status'], terminal)
        catalog = execution.advance(execution.empty(), BINDING, 0, event('goal'))
        with self.assertRaisesRegex(ValueError, 'outside-active-task'):
            execution.advance(catalog, BINDING, 1, event('decision', stamp='2026-09-04T12:00:00+00:00', target='foreign'))

    def test_corrupt_history_and_stale_event_fail_closed(self):
        catalog = execution.advance(execution.empty(), BINDING, 0, event('goal'))
        broken = copy.deepcopy(catalog)
        next(iter(broken['scopes'].values()))['revision'] = 9
        with self.assertRaises(ValueError):
            execution.validate(broken)
        with self.assertRaisesRegex(ValueError, 'stale-event'):
            execution.advance(catalog, BINDING, 1, event('open', stamp='2026-09-02T12:00:00+00:00'))

    def test_hash_match_does_not_bypass_semantic_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            op = {'kind': 'decision', 'quote': 'Belki sonra yaparız.', 'target': '', 'turn': 0}
            model = mock.Mock(side_effect=[(json.dumps({'operations': [op]}), None), ('{"valid":false}', None)])
            with self.assertRaisesRegex(ValueError, 'source-validation-failed'):
                execution.propose(root, BINDING, execution.empty(), 's', STAMP, [('user', op['quote'])], model)
            self.assertEqual(model.call_count, 2)

    def test_quoted_and_assistant_material_cannot_mint_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            for role, text in [('assistant', 'Yapalım'), ('user', '> Yapalım')]:
                op = {'kind': 'goal', 'quote': 'Yapalım', 'target': '', 'turn': 0}
                model = mock.Mock(return_value=(json.dumps({'operations': [op]}), None))
                with self.assertRaisesRegex(ValueError, 'source-unbound'):
                    execution.propose(Path(tmp), BINDING, execution.empty(), 's', STAMP, [(role, text)], model)

    def test_real_flush_persistence_restart_and_all_catalog_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'vault'
            root.mkdir()
            home = base / 'home'
            home.mkdir()
            install(root, home)
            (root / '🔮 850-Companion').mkdir()
            state = root / '.codex/scripts/.state'
            transcript = base / 'transcript.jsonl'
            text = 'Atlas raporunu hazırla.'
            transcript.write_text(json.dumps({'role': 'user', 'content': text}), encoding='utf-8')
            parts = {name: 'Yok.' for name in flush.EXPECTED_SECTIONS}
            parts['Bağlam'] = text
            summary = flush.SessionSummary(parts).render()
            proposal = json.dumps({'operations': [{'kind': 'goal', 'quote': text, 'target': '', 'turn': 0}]})
            args = argparse.Namespace(reason='turnend', hook_input=base / 'unused')
            payload = {'session_id': 's', 'transcript_path': str(transcript), 'cwd': str(root)}
            def model(prompt, *_args, purpose='flush', **_kwargs):
                return ({'flush': summary, 'execution-proposal': proposal, 'execution-source-validation': '{"valid":true}'}[purpose], None)
            with mock.patch.dict(os.environ, {'CODEX_HOME': str(home)}), mock.patch.object(flush, 'run_codex', side_effect=model), mock.patch.object(flush, 'maybe_trigger_compile'):
                self.assertEqual(flush.flush_once(args, dt.datetime.fromisoformat(STAMP), root, state, hook_input=payload), 0)
                before = companion.execution_snapshot(root)
                self.assertEqual(len(before['scopes']), 1)
                context = hook.build_session_context(root, state, write_views=False, cwd=str(root))
                self.assertIn('[Yürütme durumu]', context)
                self.assertIn(text, context)
                companion.ensure_views(root, state)
                companion.publish(root, state, summary, dt.datetime.fromisoformat(STAMP), 'd' * 64, 'other', frozenset())
                companion.migrate(root, state=state)
                self.assertEqual(companion.execution_snapshot(root), before)
                suppress_derived_memory(root / '.codex/private-memory', text)
                with memory_read(root) as memory:
                    self.assertNotIn(text, execution.render(before, execution.identity(root), memory))

    def test_manifest_binding_does_not_cross_vault_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'vault'
            root.mkdir()
            home = Path(tmp) / 'home'
            home.mkdir()
            other = Path(tmp) / 'other'
            other.mkdir()
            install(root, home)
            with mock.patch.dict(os.environ, {'CODEX_HOME': str(home)}):
                self.assertIsNotNone(execution.identity(root))
                self.assertIsNone(execution.identity(other))
                (home / 'manifest.json').write_text('changed', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'manifest-invalid'):
                    execution.identity(root)


if __name__ == '__main__':
    unittest.main()
