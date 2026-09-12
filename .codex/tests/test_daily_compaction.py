from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import daily_store


NOW = dt.datetime(2026, 9, 7, 12)


class DailyCompactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / '.state'
        self.day = self.root / 'daily/2026-09-07.md'

    def publish(self, key='a', text='Kalıcı karar.', **kwargs):
        return daily_store.publish(self.root, self.state, text, 'turnend', NOW,
                                   idempotency_key=key * 64, **kwargs)

    def receipt_path(self):
        return next((self.state / 'daily-operations').glob('*.json'))

    def legacy(self, status='committed'):
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:prepared'):
            self.publish(_fail_after='prepared')
        path = self.receipt_path()
        receipt = json.loads(path.read_text(encoding='utf-8'))
        image = path.with_suffix('.after.md')
        if status == 'committed':
            self.day.parent.mkdir(parents=True, exist_ok=True)
            self.day.write_bytes(image.read_bytes())
        receipt['status'] = status
        path.write_text(json.dumps(receipt), encoding='utf-8')
        return path, image

    def test_completed_operations_keep_small_receipts_and_replay_after_append(self):
        self.assertTrue(self.publish(text='Özel bilgi. ' * 1000))
        self.assertFalse(list((self.state / 'daily-operations').glob('*.after.md')))
        receipt = self.receipt_path().read_text(encoding='utf-8')
        self.assertNotIn('Özel bilgi', receipt)
        self.assertLess(len(receipt), 800)
        self.assertTrue(self.publish('b', 'İkinci karar.'))
        before = self.day.read_bytes()
        self.assertFalse(self.publish(text='Özel bilgi. ' * 1000))
        self.assertEqual(self.day.read_bytes(), before)

    def test_compact_receipt_rejects_edited_or_truncated_committed_prefix(self):
        self.publish()
        before = self.day.read_bytes()
        for changed in (before.replace(b'karar', b'yanit'), before[:-4]):
            with self.subTest(changed=changed[-20:]):
                self.day.write_bytes(changed)
                with self.assertRaisesRegex(ValueError, 'daily-committed-drift'):
                    self.publish()
                self.assertEqual(self.day.read_bytes(), changed)

    def test_each_crash_boundary_recovers_once_without_retaining_full_copy(self):
        for index, phase in enumerate(('prepared', 'committing', 'replace', 'committed')):
            key = chr(ord('b') + index)
            text = f'Karar {phase}'
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(RuntimeError, f'daily-injected:{phase}'):
                    self.publish(key, text, _fail_after=phase)
                self.publish(key, text)
                self.assertEqual(self.day.read_text(encoding='utf-8').count(text), 1)
        self.assertFalse(list((self.state / 'daily-operations').glob('*.after.md')))

    def test_prepared_replay_rejects_summary_drift_without_rewriting_daily_prefix(self):
        legacy_summary = 'DATABASE_PASSWORD=LEGACY_SECRET'
        current_summary = 'DATABASE_PASSWORD=<REDACTED>'
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:prepared'):
            self.publish('a', legacy_summary, _fail_after='prepared')
        self.day.parent.mkdir(parents=True, exist_ok=True)
        self.day.write_text('# Günlük Log: 2026-09-07\nKullanıcı öneki.\n', encoding='utf-8')
        before = self.day.read_bytes()
        with self.assertRaisesRegex(ValueError, 'daily-prepared-summary-drift'):
            self.publish('a', current_summary)
        self.assertEqual(self.day.read_bytes(), before)
        self.assertNotIn(legacy_summary, self.day.read_text(encoding='utf-8'))

    def test_rebase_receipt_failure_keeps_old_image_for_exactly_once_retry(self):
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:prepared'):
            self.publish('a', 'Operation A', _fail_after='prepared')
        self.publish('b', 'Operation B')
        receipt = self.state / 'daily-operations' / (
            f"{daily_store._operation_id('flush', 'a' * 64)}.json"
        )
        image = receipt.with_suffix('.after.md')
        orphan = receipt.with_name(f'{receipt.stem}.after.1.md')
        original_image = image.read_bytes()
        real_write = daily_store.atomic_write_json

        def fail_rebase(path, payload, **kwargs):
            if path == receipt and payload.get('status') == 'prepared':
                raise OSError('simulated rebase receipt failure')
            return real_write(path, payload, **kwargs)

        with mock.patch.object(daily_store, 'atomic_write_json', side_effect=fail_rebase):
            with self.assertRaisesRegex(OSError, 'simulated rebase receipt failure'):
                self.publish('a', 'Operation A')
        self.assertEqual(image.read_bytes(), original_image)
        orphan_image = orphan.read_bytes()
        self.assertTrue(self.publish('a', 'Operation A'))
        self.assertFalse(self.publish('a', 'Operation A'))
        text = self.day.read_text(encoding='utf-8')
        self.assertEqual(text.count('Operation A'), 1)
        self.assertEqual(text.count('Operation B'), 1)
        self.assertEqual(orphan.read_bytes(), orphan_image)
        self.assertFalse(image.exists())

    def test_repeated_rebase_failures_keep_orphans_and_interleaved_entries(self):
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:prepared'):
            self.publish('a', 'Operation A', _fail_after='prepared')
        self.publish('b', 'Operation B')
        receipt = self.state / 'daily-operations' / (
            f"{daily_store._operation_id('flush', 'a' * 64)}.json"
        )
        image = receipt.with_suffix('.after.md')
        original_image = image.read_bytes()
        real_write = daily_store.atomic_write_json

        def fail_rebase(path, payload, **kwargs):
            if path == receipt and payload.get('status') == 'prepared':
                raise OSError('simulated rebase receipt failure')
            return real_write(path, payload, **kwargs)

        with mock.patch.object(daily_store, 'atomic_write_json', side_effect=fail_rebase):
            with self.assertRaisesRegex(OSError, 'simulated rebase receipt failure'):
                self.publish('a', 'Operation A')
        orphan_one = receipt.with_name(f'{receipt.stem}.after.1.md')
        orphan_one_bytes = orphan_one.read_bytes()
        self.publish('c', 'Operation C')
        with mock.patch.object(daily_store, 'atomic_write_json', side_effect=fail_rebase):
            with self.assertRaisesRegex(OSError, 'simulated rebase receipt failure'):
                self.publish('a', 'Operation A')
        orphan_two = receipt.with_name(f'{receipt.stem}.after.2.md')
        orphan_two_bytes = orphan_two.read_bytes()
        self.assertEqual(image.read_bytes(), original_image)
        self.assertTrue(self.publish('a', 'Operation A'))
        self.assertFalse(self.publish('a', 'Operation A'))
        text = self.day.read_text(encoding='utf-8')
        self.assertEqual(text.count('Operation A'), 1)
        self.assertEqual(text.count('Operation B'), 1)
        self.assertEqual(text.count('Operation C'), 1)
        self.assertEqual(orphan_one.read_bytes(), orphan_one_bytes)
        self.assertEqual(orphan_two.read_bytes(), orphan_two_bytes)
        self.assertFalse(image.exists())
        self.assertFalse(receipt.with_name(f'{receipt.stem}.after.3.md').exists())

    def test_marker_already_current_receipt_failure_keeps_old_image(self):
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:replace'):
            self.publish('a', 'Operation A', _fail_after='replace')
        self.publish('b', 'Operation B')
        receipt = self.state / 'daily-operations' / (
            f"{daily_store._operation_id('flush', 'a' * 64)}.json"
        )
        image = receipt.with_suffix('.after.md')
        orphan = receipt.with_name(f'{receipt.stem}.after.1.md')
        original_image = image.read_bytes()
        real_write = daily_store.atomic_write_json

        def fail_compaction(path, payload, **kwargs):
            if path == receipt and payload.get('schema_version') == daily_store.COMPACT_SCHEMA_VERSION:
                raise OSError('simulated compact receipt failure')
            return real_write(path, payload, **kwargs)

        with mock.patch.object(daily_store, 'atomic_write_json', side_effect=fail_compaction):
            with self.assertRaisesRegex(OSError, 'simulated compact receipt failure'):
                self.publish('a', 'Operation A')
        self.assertEqual(image.read_bytes(), original_image)
        orphan_image = orphan.read_bytes()
        self.assertFalse(self.publish('a', 'Operation A'))
        text = self.day.read_text(encoding='utf-8')
        self.assertEqual(text.count('Operation A'), 1)
        self.assertEqual(text.count('Operation B'), 1)
        self.assertEqual(orphan.read_bytes(), orphan_image)
        self.assertFalse(image.exists())

    def test_rebased_compact_receipt_retains_generation_for_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:replace'):
            self.publish('a', 'Operation A', _fail_after='replace')
        self.publish('b', 'Operation B')
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:committed'):
            self.publish('a', 'Operation A', _fail_after='committed')
        receipt = self.state / 'daily-operations' / (
            f"{daily_store._operation_id('flush', 'a' * 64)}.json"
        )
        data = json.loads(receipt.read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'], daily_store.COMPACT_SCHEMA_VERSION)
        self.assertEqual(data['after_generation'], 1)
        previous = receipt.with_suffix('.after.md')
        previous_bytes = previous.read_bytes()
        previous.write_bytes(previous_bytes + b'corrupt')
        self.assertFalse(self.publish('a', 'Operation A'))
        self.assertTrue(previous.is_file())
        receipt_before = receipt.read_bytes()
        report = daily_store.compact_completed(self.root, self.state, apply=True)
        self.assertEqual(report['blocked'][0]['error'], 'daily-after-image-invalid')
        self.assertEqual(report['compacted'], 0)
        self.assertEqual(report['bytes_released'], 0)
        self.assertEqual(receipt.read_bytes(), receipt_before)
        self.assertEqual(previous.read_bytes()[-7:], b'corrupt')
        previous.write_bytes(previous_bytes)
        preview = daily_store.compact_completed(self.root, self.state)
        self.assertEqual(preview['reclaimable_bytes'], len(previous_bytes))
        self.assertTrue(previous.is_file())
        report = daily_store.compact_completed(self.root, self.state, apply=True)
        self.assertEqual(report['bytes_released'], len(previous_bytes))
        self.assertFalse(previous.exists())

    def test_legacy_compaction_is_dry_by_default_and_preserves_daily_bytes(self):
        _receipt, image = self.legacy()
        self.day.write_bytes(self.day.read_bytes() + b'\nLater independent append.\n')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report = daily_store.compact_completed(self.root, self.state)
        self.assertEqual(report['eligible'], 1)
        self.assertEqual(report['blocked'], [])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        report = daily_store.compact_completed(self.root, self.state, apply=True)
        self.assertEqual(report['compacted'], 1)
        self.assertEqual(report['bytes_released'], len(before[image]))
        self.assertFalse(image.exists())
        self.assertEqual(self.day.read_bytes(), before[self.day])
        self.assertFalse(self.publish())
        self.assertEqual(daily_store.compact_completed(self.root, self.state, apply=True)['compacted'], 0)

    def test_migration_keeps_incomplete_recovery_images(self):
        for status in ('prepared', 'committing'):
            with self.subTest(status=status):
                if status == 'prepared':
                    receipt, image = self.legacy(status)
                else:
                    data = json.loads(receipt.read_text(encoding='utf-8'))
                    data['status'] = status
                    receipt.write_text(json.dumps(data), encoding='utf-8')
                before = image.read_bytes()
                report = daily_store.compact_completed(self.root, self.state, apply=True)
                self.assertEqual(report['pending'], 1)
                self.assertEqual(image.read_bytes(), before)
        self.assertTrue(self.publish())

    def test_migration_preserves_invalid_snapshot_and_edited_daily(self):
        receipt, image = self.legacy()
        original_image = image.read_bytes()
        original_day = self.day.read_bytes()
        for corrupt_image in (True, False):
            image.write_bytes(original_image + b'bad' if corrupt_image else original_image)
            self.day.write_bytes(original_day if corrupt_image else original_day.replace(b'karar', b'yanit'))
            before = {p: p.read_bytes() for p in (receipt, image, self.day)}
            report = daily_store.compact_completed(self.root, self.state, apply=True)
            self.assertEqual(report['compacted'], 0)
            self.assertEqual(len(report['blocked']), 1)
            self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_compact_receipt_published_before_image_removal_can_resume(self):
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:committed'):
            self.publish(_fail_after='committed')
        receipt = json.loads(self.receipt_path().read_text(encoding='utf-8'))
        self.assertEqual(receipt['schema_version'], 2)
        self.assertTrue(list((self.state / 'daily-operations').glob('*.after.md')))
        before = self.day.read_bytes()
        self.assertFalse(self.publish())
        self.assertEqual(self.day.read_bytes(), before)
        self.assertFalse(list((self.state / 'daily-operations').glob('*.after.md')))

    def test_migration_rejects_receipt_path_escape(self):
        receipt, image = self.legacy()
        data = json.loads(receipt.read_text(encoding='utf-8'))
        data['date'] = '../outside'
        data['daily_relative'] = 'daily/../outside.md'
        receipt.write_text(json.dumps(data), encoding='utf-8')
        report = daily_store.compact_completed(self.root, self.state, apply=True)
        self.assertEqual(len(report['blocked']), 1)
        self.assertTrue(image.exists())

    def test_migration_does_not_convert_empty_or_malformed_legacy_proof(self):
        receipt, image = self.legacy()
        original = json.loads(receipt.read_text(encoding='utf-8'))
        original_image = image.read_bytes()
        for field in ('empty-image', 'before_sha256', 'after_sha256', 'body_sha256'):
            with self.subTest(field=field):
                data = dict(original)
                image.write_bytes(original_image)
                if field == 'empty-image':
                    import hashlib
                    image.write_bytes(b'')
                    data['after_sha256'] = data['after_image_sha256'] = hashlib.sha256(b'').hexdigest()
                else:
                    data[field] = 'invalid'
                receipt.write_text(json.dumps(data), encoding='utf-8')
                before = receipt.read_bytes(), image.read_bytes()
                report = daily_store.compact_completed(self.root, self.state, apply=True)
                self.assertEqual(report['compacted'], 0)
                self.assertEqual(len(report['blocked']), 1)
                self.assertEqual((receipt.read_bytes(), image.read_bytes()), before)

    def test_legacy_replay_compacts_with_crlf_byte_lengths(self):
        self.day.parent.mkdir(parents=True)
        self.day.write_bytes('# Günlük Log: 2026-09-07\r\nÖnceki bilgi.\r\n'.encode('utf-8'))
        receipt, image = self.legacy()
        before = self.day.read_bytes()
        self.assertIn(b'\r\n', before)
        self.assertFalse(self.publish())
        compact = json.loads(receipt.read_text(encoding='utf-8'))
        self.assertEqual(compact['after_size'], len(before))
        self.assertFalse(image.exists())
        self.assertEqual(self.day.read_bytes(), before)

    def test_recovery_after_other_append_survives_compact_commit_crash(self):
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:replace'):
            self.publish(_fail_after='replace')
        self.publish('b', 'Sonraki bilgi.')
        before = self.day.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'daily-injected:committed'):
            self.publish(_fail_after='committed')
        self.assertFalse(self.publish())
        self.assertEqual(self.day.read_bytes(), before)
        self.assertFalse(list((self.state / 'daily-operations').glob('*.after.md')))

    def test_parallel_writers_replays_and_compaction_keep_every_entry_once(self):
        code = (
            'import sys,datetime; from pathlib import Path; '
            f'sys.path.insert(0, {str(Path(daily_store.__file__).parent)!r}); '
            'import daily_store; root=Path(sys.argv[1]); index=int(sys.argv[2]); '
            'daily_store.publish(root,root/".state",f"Karar-{index}","turnend",'
            'datetime.datetime(2026,9,7,12),idempotency_key=str(index)*64); '
            'daily_store.compact_completed(root,root/".state",apply=True)'
        )
        processes = []
        try:
            for index in (1, 2, 3, 1, 2, 3):
                processes.append(subprocess.Popen(
                    [sys.executable, '-B', '-c', code, str(self.root), str(index)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
            for process in processes:
                out, err = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, (out, err))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        text = self.day.read_text(encoding='utf-8')
        for index in (1, 2, 3):
            self.assertEqual(text.count(f'Karar-{index}'), 1)
        self.assertFalse(list((self.state / 'daily-operations').glob('*.after.md')))


if __name__ == '__main__':
    unittest.main()
