from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from _fixtures import CODEX_DIR
import flush
import attachment_memory
from intake_contract import validate_note
from memory_ledger import (
    clear_read_only_turn,
    load_suppressed_hashes,
    mark_read_only_turn,
    mark_session_only,
    persistent_turns,
    suppress_derived_memory,
)


class AttachmentMemoryTests(unittest.TestCase):
    def test_empty_source_summary_is_skipped_without_poisoning_future_flushes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Merhaba', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summarize = mock.Mock(return_value='FLUSH_BOS')
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                for _ in range(2):
                    self.assertEqual(attachment_memory.capture_sources([('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize), [])
            self.assertEqual(summarize.call_count, 1)
            self.assertEqual(list((root / attachment_memory.SOURCE_DIR).glob('*.md')), [])

    def test_empty_source_receipt_retries_after_attachment_disappears(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('No durable content.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            state = root / 'state'
            summarize = mock.Mock(return_value='FLUSH_BOS')
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                self.assertEqual(
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    ),
                    [],
                )
                mapping_path = attachment_memory._mapping_path(
                    state,
                    '11111111-1111-4111-8111-111111111111',
                    attachment_memory._attachment_digest(text.rstrip()),
                )
                mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
                self.assertEqual(mapping['status'], 'empty')
                self.assertNotIn('note_relative', mapping)
                self.assertNotIn('note_sha256', mapping)
                self.assertEqual(list(state.glob('attachment-empty-*')), [])
                source.unlink()
                self.assertEqual(
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    ),
                    [],
                )
            self.assertEqual(summarize.call_count, 1)

    def test_readable_fully_suppressed_retry_refreshes_empty_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('No durable content.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summarize = mock.Mock(return_value='FLUSH_BOS')
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                def capture(hashes):
                    return attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, summarize, state_dir=root / 'state',
                    )
                self.assertEqual(capture(frozenset()), [])
                suppress_derived_memory(root / '.codex/private-memory', 'No durable content.')
                hashes = load_suppressed_hashes(root / '.codex/private-memory')
                self.assertEqual(capture(hashes), [])
                source.unlink()
                self.assertEqual(capture(hashes), [])
                self.assertEqual(summarize.call_count, 1)

    def test_empty_promotion_adopts_note_published_by_another_envelope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Shared source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nSummary.' for h in flush.EXPECTED_SECTIONS)
            summarize = mock.Mock(side_effect=['FLUSH_BOS', summary, summary])
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                def capture(envelope, day, hashes=frozenset()):
                    return attachment_memory.capture_sources(
                        [('user', envelope)], root, dt.datetime(2026, 9, day, tzinfo=dt.timezone.utc),
                        hashes, summarize, state_dir=state,
                    )
                self.assertEqual(capture(text, 1), [])
                other = capture(text + 'Preserve source.', 2)
                note = root / (other[0][0] + '.md')
                original = note.read_bytes()
                suppress_derived_memory(root / '.codex/private-memory', 'Unrelated preference.')
                hashes = load_suppressed_hashes(root / '.codex/private-memory')
                self.assertEqual(capture(text, 3, hashes), other)
                source.unlink()
                self.assertEqual(capture(text, 4, hashes), other)
                self.assertEqual(note.read_bytes(), original)
                self.assertEqual(summarize.call_count, 2)

    def test_observed_source_change_blocks_missing_source_recovery_until_reverified(self):
        summary = '\n\n'.join('## ' + h + '\nSummary.' for h in flush.EXPECTED_SECTIONS)
        for outcome in ('FLUSH_BOS', summary):
            with self.subTest(empty=outcome == 'FLUSH_BOS'), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / 'home'
                source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
                source.parent.mkdir(parents=True)
                source.write_text('Original source.', encoding='utf-8')
                text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
                summarize = mock.Mock(return_value=outcome)
                with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                    def capture():
                        return attachment_memory.capture_sources(
                            [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                            frozenset(), summarize, state_dir=root / 'state',
                        )
                    initial = capture()
                    source.write_text('Changed source.', encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                        capture()
                    self.assertEqual(source.read_text(encoding='utf-8'), 'Changed source.')
                    source.unlink()
                    with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                        capture()
                    source.write_text('Original source.', encoding='utf-8')
                    self.assertEqual(capture(), initial)
                    source.unlink()
                    self.assertEqual(capture(), initial)
                    self.assertEqual(summarize.call_count, 1)

    def test_source_change_during_summary_is_rejected_before_publication(self):
        summary = '\n\n'.join('## ' + h + '\nSummary.' for h in flush.EXPECTED_SECTIONS)
        for replacement_content in ('Changed source.', 'Original source.', None):
            for outcome in ('FLUSH_BOS', summary):
                with self.subTest(replacement_content=replacement_content, empty=outcome == 'FLUSH_BOS'), \
                     tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    home = root / 'home'
                    source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
                    source.parent.mkdir(parents=True)
                    source.write_text('Original source.', encoding='utf-8')
                    text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
                    state = root / 'state'

                    def summarize_and_change(_prompt: str) -> str:
                        if replacement_content is None:
                            source.unlink()
                        else:
                            replacement = source.with_name('replacement.txt')
                            replacement.write_text(replacement_content, encoding='utf-8')
                            replacement.replace(source)
                        return outcome

                    with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                        with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                            attachment_memory.capture_sources(
                                [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                                frozenset(), summarize_and_change, state_dir=state,
                            )

                    self.assertEqual(list((root / attachment_memory.SOURCE_DIR).glob('*.md')), [])
                    self.assertEqual(list(state.glob('attachment-memory-*.json')), [])

    def test_model_source_change_marks_mapping_and_blocks_missing_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), mock.Mock(return_value=summary), state_dir=state,
                )
                suppress_derived_memory(root / '.codex/private-memory', 'Stored summary.')
                hashes = load_suppressed_hashes(root / '.codex/private-memory')

                def summarize_fails(_prompt: str) -> str:
                    raise RuntimeError('model failed')

                with self.assertRaisesRegex(RuntimeError, 'model failed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, summarize_fails, state_dir=state,
                    )

                def summarize_and_remove(_prompt: str) -> str:
                    source.unlink()
                    return summary

                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, summarize_and_remove, state_dir=state,
                    )

                mapping_path = attachment_memory._mapping_path(
                    state, source.parent.name, attachment_memory._attachment_digest(text.rstrip()),
                )
                mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
                self.assertTrue(mapping['source_changed'])
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, mock.Mock(return_value=summary), state_dir=state,
                    )

    def test_model_failure_after_source_change_marks_mapping_and_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), mock.Mock(return_value=summary), state_dir=state,
                )
                suppress_derived_memory(root / '.codex/private-memory', 'Stored summary.')
                hashes = load_suppressed_hashes(root / '.codex/private-memory')

                def summarize_and_remove(_prompt: str) -> str:
                    source.unlink()
                    raise RuntimeError('model failed')

                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, summarize_and_remove, state_dir=state,
                    )

                mapping_path = attachment_memory._mapping_path(
                    state, source.parent.name, attachment_memory._attachment_digest(text.rstrip()),
                )
                mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
                self.assertTrue(mapping['source_changed'])
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, mock.Mock(return_value=summary), state_dir=state,
                    )

    def test_source_disappearance_during_preload_marks_mapping_and_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), mock.Mock(return_value=summary), state_dir=state,
                )
                real_open = Path.open
                removed = False

                def disappear_on_open(path, *args, **kwargs):
                    nonlocal removed
                    if path == source and not removed:
                        removed = True
                        source.unlink()
                    return real_open(path, *args, **kwargs)

                with mock.patch.object(Path, 'open', autospec=True, side_effect=disappear_on_open):
                    with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                        attachment_memory.capture_sources(
                            [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                            frozenset(), mock.Mock(return_value=summary), state_dir=state,
                        )

                mapping_path = attachment_memory._mapping_path(
                    state, source.parent.name, attachment_memory._attachment_digest(text.rstrip()),
                )
                mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
                self.assertTrue(mapping['source_changed'])
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), mock.Mock(return_value=summary), state_dir=state,
                    )

    def test_source_change_survives_concurrent_suppression_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), mock.Mock(return_value=summary), state_dir=state,
                )
                suppress_derived_memory(root / '.codex/private-memory', 'Stored summary.')
                hashes = load_suppressed_hashes(root / '.codex/private-memory')

                def summarize_and_update(_prompt: str) -> str:
                    source.unlink()
                    suppress_derived_memory(root / '.codex/private-memory', 'Concurrent preference.')
                    return summary

                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, summarize_and_update, state_dir=state,
                    )

                mapping_path = attachment_memory._mapping_path(
                    state, source.parent.name, attachment_memory._attachment_digest(text.rstrip()),
                )
                mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
                self.assertTrue(mapping['source_changed'])
                current_hashes = load_suppressed_hashes(root / '.codex/private-memory')
                self.assertNotEqual(current_hashes, hashes)
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        current_hashes, mock.Mock(return_value=summary), state_dir=state,
                    )

    def test_source_change_invalidates_all_envelope_mappings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            first_text = f'# Files pasted by the user:\n\n## "First": {source}\n\n## My request:\nFirst request.\n'
            second_text = f'# Files pasted by the user:\n\n## "Second": {source}\n\n## My request:\nSecond request.\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                attachment_memory.capture_sources(
                    [('user', first_text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), mock.Mock(return_value=summary), state_dir=state,
                )
                attachment_memory.capture_sources(
                    [('user', second_text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), mock.Mock(return_value=summary), state_dir=state,
                )
                suppress_derived_memory(root / '.codex/private-memory', 'Stored summary.')
                hashes = load_suppressed_hashes(root / '.codex/private-memory')

                def summarize_and_remove(_prompt: str) -> str:
                    source.unlink()
                    return summary

                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', second_text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, summarize_and_remove, state_dir=state,
                    )

                mapping_paths = [
                    attachment_memory._mapping_path(
                        state, source.parent.name,
                        attachment_memory._attachment_digest(envelope.rstrip()),
                    )
                    for envelope in (first_text, second_text)
                ]
                for mapping_path in mapping_paths:
                    mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
                    self.assertTrue(mapping['source_changed'])
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', first_text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, mock.Mock(return_value=summary), state_dir=state,
                    )

    def test_source_change_after_recheck_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            real_validate = attachment_memory._validate_destination_parent

            def validate_then_replace(vault_root: Path, destination: Path) -> None:
                real_validate(vault_root, destination)
                replacement = source.with_name('replacement.txt')
                replacement.write_text('Changed source.', encoding='utf-8')
                replacement.replace(source)

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(
                     attachment_memory,
                     '_validate_destination_parent',
                     side_effect=validate_then_replace,
                 ):
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), mock.Mock(return_value=summary), state_dir=state,
                    )

            self.assertEqual(list((root / attachment_memory.SOURCE_DIR).glob('*.md')), [])
            self.assertEqual(list(state.glob('attachment-memory-*.json')), [])
            self.assertTrue(
                attachment_memory._source_changed_marker_path(state, source.parent.name).is_file()
            )

    def test_source_change_during_note_staging_leaves_no_note(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            real_write = attachment_memory.atomic_write_text

            def write_then_replace(path: Path, value: str, **kwargs) -> None:
                real_write(path, value, **kwargs)
                if Path(path).suffix == '.staging':
                    replacement = source.with_name('replacement.txt')
                    replacement.write_text('Changed source.', encoding='utf-8')
                    replacement.replace(source)

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(
                     attachment_memory,
                     'atomic_write_text',
                     side_effect=write_then_replace,
                 ):
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), mock.Mock(return_value=summary), state_dir=state,
                    )

            self.assertEqual(list((root / attachment_memory.SOURCE_DIR).glob('*.md')), [])
            self.assertEqual(list(root.rglob('*.staging')), [])

    def test_stale_note_staging_is_removed_before_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            destination = root / attachment_memory._note_relative(
                source.parent.name, attachment_memory._attachment_digest('Stable source.'),
            )
            staging = destination.with_suffix('.staging')
            staging.parent.mkdir(parents=True)
            staging.write_text('abandoned staging', encoding='utf-8')

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                result = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), mock.Mock(return_value=summary), state_dir=state,
                )

            self.assertTrue(result)
            self.assertFalse(staging.exists())
            self.assertTrue(destination.is_file())

    def test_source_change_marker_survives_session_only_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'

            def summarize_and_exclude(_prompt: str) -> str:
                mark_session_only(state, 'session')
                source.unlink()
                return summary

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize_and_exclude,
                        state_dir=state, session_id='session',
                    )

                marker = attachment_memory._source_changed_marker_path(
                    state, source.parent.name,
                )
                self.assertTrue(marker.is_file())
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), mock.Mock(return_value=summary),
                        state_dir=state, session_id='other',
                    )

    def test_source_change_without_receipt_clears_marker_after_successful_retry(self):
        summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
        for outcome in ('FLUSH_BOS', summary):
            with self.subTest(empty=outcome == 'FLUSH_BOS'), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / 'home'
                source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
                source.parent.mkdir(parents=True)
                source.write_text('Stable source.', encoding='utf-8')
                text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
                state = root / 'state'

                def summarize_and_change(_prompt: str) -> str:
                    replacement = source.with_name('replacement.txt')
                    replacement.write_text('Changed source.', encoding='utf-8')
                    replacement.replace(source)
                    return outcome

                marker = attachment_memory._source_changed_marker_path(
                    state, source.parent.name,
                )
                with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                    with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                        attachment_memory.capture_sources(
                            [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                            frozenset(), summarize_and_change, state_dir=state,
                        )
                    self.assertTrue(marker.is_file())
                    source.write_text('Stable source.', encoding='utf-8')
                    retry = mock.Mock(return_value=outcome)
                    result = attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), retry, state_dir=state,
                    )
                    self.assertFalse(marker.exists())
                    source.unlink()
                    self.assertEqual(
                        attachment_memory.capture_sources(
                            [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                            frozenset(), retry, state_dir=state,
                        ),
                        result,
                    )
                retry.assert_called_once()

    def test_preload_source_change_invalidates_all_envelope_mappings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            first_text = f'# Files pasted by the user:\n\n## "First": {source}\n\n## My request:\nFirst request.\n'
            second_text = f'# Files pasted by the user:\n\n## "Second": {source}\n\n## My request:\nSecond request.\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            summarize = mock.Mock(return_value=summary)
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                attachment_memory.capture_sources(
                    [('user', first_text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), summarize, state_dir=state,
                )
                attachment_memory.capture_sources(
                    [('user', second_text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), summarize, state_dir=state,
                )
                source.write_text('Changed source.', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', first_text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    )

                mapping_paths = [
                    attachment_memory._mapping_path(
                        state, source.parent.name,
                        attachment_memory._attachment_digest(envelope.rstrip()),
                    )
                    for envelope in (first_text, second_text)
                ]
                for mapping_path in mapping_paths:
                    self.assertTrue(json.loads(mapping_path.read_text(encoding='utf-8'))['source_changed'])
                source.unlink()
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', second_text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    )

    def test_reused_receipt_rechecks_source_before_success(self):
        summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
        for outcome in ('FLUSH_BOS', summary):
            with self.subTest(empty=outcome == 'FLUSH_BOS'), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / 'home'
                source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
                source.parent.mkdir(parents=True)
                source.write_text('Stable source.', encoding='utf-8')
                text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
                state = root / 'state'
                with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), mock.Mock(return_value=outcome), state_dir=state,
                    )
                    real_scope = attachment_memory._publication_scope
                    calls = 0

                    @contextmanager
                    def replace_on_reuse_scope(scope_state: Path, session: str | None):
                        nonlocal calls
                        calls += 1
                        with real_scope(scope_state, session):
                            if calls == 2:
                                replacement = source.with_name('replacement.txt')
                                replacement.write_text('Changed source.', encoding='utf-8')
                                replacement.replace(source)
                            yield

                    with mock.patch.object(
                        attachment_memory,
                        '_publication_scope',
                        side_effect=replace_on_reuse_scope,
                    ):
                        with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                            attachment_memory.capture_sources(
                                [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                                frozenset(), mock.Mock(return_value=outcome), state_dir=state,
                            )

    def test_source_change_before_publication_guard_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Stable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nStored summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), mock.Mock(return_value=summary), state_dir=state,
                )
                real_scope = attachment_memory._publication_scope
                calls = 0

                @contextmanager
                def remove_before_guard(scope_state: Path, session: str | None):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        source.unlink()
                        mark_session_only(scope_state, session or 'session')
                    with real_scope(scope_state, session):
                        yield

                with mock.patch.object(
                    attachment_memory,
                    '_publication_scope',
                    side_effect=remove_before_guard,
                ):
                    with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                        attachment_memory.capture_sources(
                            [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                            frozenset(), mock.Mock(return_value=summary), state_dir=state,
                            session_id='session',
                        )

                marker = attachment_memory._source_changed_marker_path(
                    state, source.parent.name,
                )
                self.assertTrue(marker.is_file())
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), mock.Mock(return_value=summary), state_dir=state,
                    )

    def test_empty_source_receipt_does_not_cross_suppression_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('No durable content.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            state = root / 'state'
            summarize = mock.Mock(return_value='FLUSH_BOS')
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                self.assertEqual(
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    ),
                    [],
                )
                source.unlink()
                suppress_derived_memory(
                    root / '.codex/private-memory', 'No durable content.'
                )
                hashes = load_suppressed_hashes(root / '.codex/private-memory')
                with self.assertRaisesRegex(
                    ValueError, 'attachment-recovery-unavailable'
                ):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        hashes, summarize, state_dir=state,
                    )
            self.assertEqual(summarize.call_count, 1)

    def test_empty_receipt_keeps_first_date_when_reconsidered_and_promoted(self):
        for empty_retries in (0, 1):
            with self.subTest(empty_retries=empty_retries), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / 'home'
                source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
                source.parent.mkdir(parents=True)
                source.write_text('Shared source.', encoding='utf-8')
                text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
                state = root / 'state'
                summary = '\n\n'.join('## ' + h + '\nDurable summary.' for h in flush.EXPECTED_SECTIONS)
                summarize = mock.Mock(side_effect=['FLUSH_BOS'] * (1 + empty_retries) + [summary])
                first_date = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
                with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                    for attempt in range(2 + empty_retries):
                        if attempt:
                            suppress_derived_memory(root / '.codex/private-memory', f'Unrelated preference {attempt}.')
                        hashes = load_suppressed_hashes(root / '.codex/private-memory')
                        result = attachment_memory.capture_sources(
                            [('user', text)], root, first_date + dt.timedelta(days=attempt * 5),
                            hashes, summarize, state_dir=state,
                        )
                self.assertTrue(result)
                mapping_path = attachment_memory._mapping_path(
                    state, source.parent.name, attachment_memory._attachment_digest(text.rstrip()),
                )
                mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
                self.assertEqual(mapping['status'], 'committed')
                self.assertEqual(mapping['event_date'], '2026-09-01')
                note = (root / mapping['note_relative']).read_text(encoding='utf-8')
                self.assertIn('created: 2026-09-01\n', note)
                self.assertIn('updated: 2026-09-01\n', note)
                self.assertEqual(summarize.call_count, 2 + empty_retries)

    def test_empty_receipt_is_scoped_to_its_envelope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Shared source.', encoding='utf-8')
            empty_text = f'# Files pasted by the user:\n\n## "Empty": {source}\n\n## My request:\n'
            durable_text = f'# Files pasted by the user:\n\n## "Durable": {source}\n\n## My request:\nSave this.\n'
            summary = '\n\n'.join(
                '## ' + heading + '\nDurable summary.'
                for heading in flush.EXPECTED_SECTIONS
            )
            summarize = mock.Mock(side_effect=['FLUSH_BOS', summary])
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                self.assertEqual(
                    attachment_memory.capture_sources(
                        [('user', empty_text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    ),
                    [],
                )
                durable = attachment_memory.capture_sources(
                    [('user', durable_text)], root, dt.datetime.now(dt.timezone.utc),
                    frozenset(), summarize, state_dir=state,
                )
                self.assertTrue(durable)
                empty_mapping = json.loads(
                    attachment_memory._mapping_path(
                        state, source.parent.name,
                        attachment_memory._attachment_digest(empty_text.rstrip()),
                    ).read_text(encoding='utf-8')
                )
                durable_mapping = json.loads(
                    attachment_memory._mapping_path(
                        state, source.parent.name,
                        attachment_memory._attachment_digest(durable_text.rstrip()),
                    ).read_text(encoding='utf-8')
                )
                self.assertEqual(empty_mapping['status'], 'empty')
                self.assertEqual(durable_mapping['status'], 'committed')
                source.unlink()
                self.assertEqual(
                    attachment_memory.capture_sources(
                        [('user', empty_text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    ),
                    [],
                )
                self.assertEqual(
                    attachment_memory.capture_sources(
                        [('user', durable_text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    ),
                    durable,
                )
            self.assertEqual(summarize.call_count, 2)

    def test_session_only_during_summary_prevents_source_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Durable but now private source', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            def summarize(_):
                mark_session_only(root / 'state', 'session')
                return '\n\n'.join('## ' + h + '\nPrivate lesson.' for h in flush.EXPECTED_SECTIONS)
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                with self.assertRaisesRegex(ValueError, 'memory-session-excluded'):
                    attachment_memory.capture_sources([('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize, state_dir=root / 'state', session_id='session')
            self.assertEqual(list((root / attachment_memory.SOURCE_DIR).glob('*.md')), [])

    def test_capture_is_private_idempotent_and_fails_on_missing_or_oversized_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.codex').mkdir()
            shutil.copy2(CODEX_DIR / 'tag-taxonomy.json', root / '.codex' / 'tag-taxonomy.json')
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Useful retry rule.\npassword="secret-value"\n', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nRetry rule.' for h in flush.EXPECTED_SECTIONS)
            summarize = mock.Mock(return_value=summary)
            now = dt.datetime.now(dt.timezone.utc)
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                first = attachment_memory.capture_sources([('user', text)], root, now, frozenset(), summarize)
                second = attachment_memory.capture_sources([('user', text)], root, now, frozenset(), summarize)
                self.assertEqual(first, second)
                self.assertEqual(summarize.call_count, 1)
                self.assertNotIn('secret-value', summarize.call_args.args[0])
                saved = (root / (first[0][0] + '.md')).read_text(encoding='utf-8')
                self.assertNotIn('secret-value', saved)
                self.assertEqual(validate_note(root, Path(first[0][0] + '.md'), saved), ())
                from knowledge_schema import parse_frontmatter
                fields = parse_frontmatter(saved)
                self.assertEqual(fields.get('created'), now.date().isoformat())
                self.assertEqual(fields.get('updated'), now.date().isoformat())
                for request in ('Bunu kaydetme.', 'Bu konuşmada kalsın.'):
                    private = persistent_turns([('user', text + request)])
                    self.assertEqual(attachment_memory.capture_sources(private, root, now, frozenset(), summarize), [])
                self.assertEqual(attachment_memory.capture_sources([('assistant', text)], root, now, frozenset(), summarize), [])
                source.write_bytes(b'a' * (attachment_memory.MAX_SOURCE_BYTES + 1))
                with self.assertRaisesRegex(ValueError, 'budget-exceeded'):
                    attachment_memory.capture_sources([('user', text)], root, now, frozenset(), summarize)
                source.unlink()
                self.assertEqual(
                    attachment_memory.capture_sources([('user', text)], root, now, frozenset(), summarize),
                    first,
                )

    def test_long_source_reaches_child_and_generated_note_passes_intake(self):
        import hashlib
        import sys
        import codex_runner
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.codex').mkdir()
            shutil.copy2(CODEX_DIR / 'tag-taxonomy.json', root / '.codex' / 'tag-taxonomy.json')
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            original = ('# Uzun kaynak\n' + 'Kalıcı kaynak 🧠. ' * 7_000)[:100_000]
            source.write_text(original, encoding='utf-8', newline='\n')
            digest = hashlib.sha256(original.encode('utf-8')).hexdigest()
            summary = '\n\n'.join('## ' + h + '\nKaynak sentezi.' for h in flush.EXPECTED_SECTIONS)
            script = ("from pathlib import Path; import hashlib,sys; "
                      "data=sys.stdin.buffer.read().split(b'\\n\\n',1)[1]; "
                      f"assert hashlib.sha256(data).hexdigest()=={digest!r}; "
                      f"Path('last-message.md').write_text({summary!r},encoding='utf-8')")
            envelope = f'# Files pasted by the user:\n\n## "Source": {source}\n\n## My request:\n'
            def summarize(prompt):
                value, reason = codex_runner.run_exec(prompt, sandbox='read-only', timeout=10)
                self.assertIsNone(reason)
                return value
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(codex_runner, 'find_codex', return_value=sys.executable), \
                 mock.patch.object(codex_runner, '_exec_argv', return_value=['-c', script]):
                result = attachment_memory.capture_sources([('user', envelope)], root,
                    dt.datetime.now(dt.timezone.utc), frozenset(), summarize)
            note = root / (result[0][0] + '.md')
            saved = note.read_text(encoding='utf-8')
            self.assertIn(original, saved)
            self.assertEqual(validate_note(root, note, saved), ())

    def test_no_read_outside_attachment_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / 'pasted-text.txt'
            outside.write_text('must not reach the model', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {outside}\n\n## My request:\n'
            summarize = mock.Mock()
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(root / 'home')}):
                with self.assertRaises(ValueError):
                    attachment_memory.capture_sources([('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize)
            summarize.assert_not_called()

    def test_reuses_validated_note_when_original_attachment_is_gone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Durable source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nDurable summary.' for h in flush.EXPECTED_SECTIONS)
            summarize = mock.Mock(return_value=summary)
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                first = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                    summarize, state_dir=state,
                )
                source.unlink()
                second = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                    summarize, state_dir=state,
                )
            self.assertEqual(second, first)
            summarize.assert_called_once()

    def test_recovery_resanitizes_cached_source_before_summary_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('DATABASE_PASSWORD=LEGACY_SECRET\nKeep source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nSafe summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            old_policy = lambda value, max_chars=None: (value, ())

            def current_policy(value, max_chars=None):
                if 'LEGACY_SECRET' in value:
                    return value.replace('LEGACY_SECRET', '<REDACTED>'), ('credential',)
                return value, ()

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(attachment_memory, 'sanitize_text', side_effect=old_policy):
                first = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                    mock.Mock(return_value=summary), state_dir=state,
                )
            self.assertTrue(first)
            suppress_derived_memory(root / '.codex/private-memory', 'Keep source.')
            source.unlink()
            calls = []

            def summarize(prompt):
                calls.append(prompt)
                return summary

            hashes = load_suppressed_hashes(root / '.codex/private-memory')
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(attachment_memory, 'sanitize_text', side_effect=current_policy):
                recovered = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc), hashes,
                    summarize, state_dir=state,
                )
            self.assertTrue(recovered)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('LEGACY_SECRET', calls[0])
            self.assertIn('<REDACTED>', calls[0])

    def test_rejects_tampered_note_and_same_id_different_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Original source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nDurable summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                result = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                    mock.Mock(return_value=summary), state_dir=state,
                )
                receipt = attachment_memory._mapping_path(
                    state, source.parent.name,
                    attachment_memory._attachment_digest(text.rstrip()),
                )
                original_receipt = receipt.read_bytes()
                forged = json.loads(original_receipt)
                forged['envelope_sha256'] = '0' * 64
                receipt.write_text(json.dumps(forged), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'attachment-mapping-scope'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), mock.Mock(return_value=summary), state_dir=state,
                    )
                receipt.write_bytes(original_receipt)
                note = root / (result[0][0] + '.md')
                note.write_text(note.read_text(encoding='utf-8').replace('Durable summary.', 'Tampered summary.'), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'attachment-note'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                        mock.Mock(return_value=summary), state_dir=state,
                    )
                note.write_text(note.read_text(encoding='utf-8').replace('Tampered summary.', 'Durable summary.'), encoding='utf-8')
                source.write_text('Changed source.', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'attachment-content-changed'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                        mock.Mock(return_value=summary), state_dir=state,
                    )

    def test_prepared_mapping_recovers_note_write_gap_and_rejects_missing_both(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Prepared source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nPrepared summary.' for h in flush.EXPECTED_SECTIONS)
            retry_summary = '\n\n'.join('## ' + h + '\nRetry-generated summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            summarize = mock.Mock(side_effect=[summary, retry_summary])
            original_time = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
            real_write = attachment_memory.atomic_write_text
            def fail_note(path, value, **kwargs):
                if Path(path).name.startswith('kaynak-'):
                    raise OSError('note interrupted')
                return real_write(path, value, **kwargs)
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(attachment_memory, 'atomic_write_text', side_effect=fail_note):
                with self.assertRaises(OSError):
                    attachment_memory.capture_sources(
                        [('user', text)], root, original_time, frozenset(),
                        summarize, state_dir=state,
                    )
            mapping = attachment_memory._mapping_path(
                state, '11111111-1111-4111-8111-111111111111',
                attachment_memory._attachment_digest(text.rstrip()),
            )
            self.assertTrue(mapping.is_file())
            mapping_text = mapping.read_text(encoding='utf-8')
            self.assertNotIn('summary', mapping_text)
            self.assertNotIn('versions', mapping_text)
            self.assertNotIn('Prepared source.', mapping_text)
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                recovered = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
                    frozenset(), summarize, state_dir=state,
                )
                self.assertEqual(summarize.call_count, 2)
                note = root / (recovered[0][0] + '.md')
                self.assertIn('created: 2026-09-05', note.read_text(encoding='utf-8'))
                self.assertIn('Retry-generated summary.', note.read_text(encoding='utf-8'))
                mapping_values = json.loads(mapping.read_text(encoding='utf-8'))
                self.assertNotIn('summary', mapping_values)
                self.assertNotIn('versions', mapping_values)
                self.assertNotIn('Prepared source.', mapping.read_text(encoding='utf-8'))
                source.unlink()
                self.assertEqual(
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    ),
                    recovered,
                )
                note.unlink()
                with self.assertRaisesRegex(ValueError, 'attachment-recovery-unavailable'):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                        summarize, state_dir=state,
                    )

    def test_rebuilds_reused_summary_when_new_suppression_hides_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Forgotten source.\nKeep source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            old = '\n\n'.join('## ' + h + '\nOld summary.' for h in flush.EXPECTED_SECTIONS)
            new = '\n\n'.join('## ' + h + '\nRegenerated summary.' for h in flush.EXPECTED_SECTIONS)
            summarize = mock.Mock(side_effect=[old, new])
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                first = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                    summarize, state_dir=state,
                )
                suppress_derived_memory(root / '.codex/private-memory', 'Forgotten source.')
                source.unlink()
                second = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                    load_suppressed_hashes(root / '.codex/private-memory'), summarize,
                    state_dir=state,
                )
            self.assertNotEqual(second[0][0], first[0][0])
            self.assertIn('Regenerated summary.', second[0][1])
            self.assertEqual(summarize.call_count, 2)

    def test_empty_retry_replaces_unpublished_prepared_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-811111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Kept source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nSummary.' for h in flush.EXPECTED_SECTIONS)
            summarize = mock.Mock(side_effect=[summary, 'FLUSH_BOS', summary])
            state = root / 'state'
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                def capture():
                    return attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                        frozenset(), summarize, state_dir=state,
                    )
                with mock.patch.object(attachment_memory, 'atomic_write_text', side_effect=OSError('interrupted note write')):
                    with self.assertRaisesRegex(OSError, 'interrupted note write'):
                        capture()
                mapping_path = attachment_memory._mapping_path(
                    state, source.parent.name, attachment_memory._attachment_digest(text.rstrip()),
                )
                prepared = json.loads(mapping_path.read_text(encoding='utf-8'))
                self.assertEqual(prepared['status'], 'prepared')
                self.assertFalse((root / prepared['note_relative']).exists())
                self.assertEqual(capture(), [])
                self.assertEqual(capture(), [])
                source.unlink()
                self.assertEqual(capture(), [])
                self.assertEqual(summarize.call_count, 2)

    def test_empty_summary_rebuild_is_replayed_without_losing_note_recovery(self):
        for remove_source in (False, True):
            with self.subTest(remove_source=remove_source), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / 'home'
                source = home / 'attachments/11111111-1111-4111-8111-811111111111/pasted-text.txt'
                source.parent.mkdir(parents=True)
                source.write_text('Kept source.', encoding='utf-8')
                text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
                old = '\n\n'.join('## ' + h + '\nForgotten summary.' for h in flush.EXPECTED_SECTIONS)
                new = '\n\n'.join('## ' + h + '\nNew summary.' for h in flush.EXPECTED_SECTIONS)
                summarize = mock.Mock(side_effect=[old, 'FLUSH_BOS', new])
                state = root / 'state'
                with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                    def capture(hashes):
                        return attachment_memory.capture_sources(
                            [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                            hashes, summarize, state_dir=state,
                        )
                    first = capture(frozenset())
                    note = root / (first[0][0] + '.md')
                    original = note.read_bytes()
                    if remove_source:
                        source.unlink()
                    suppress_derived_memory(root / '.codex/private-memory', 'Forgotten summary.')
                    hashes = load_suppressed_hashes(root / '.codex/private-memory')
                    self.assertEqual(capture(hashes), [])
                    self.assertEqual(capture(hashes), [])
                    self.assertEqual(summarize.call_count, 2)
                    self.assertEqual(note.read_bytes(), original)
                    suppress_derived_memory(root / '.codex/private-memory', 'Unrelated preference.')
                    hashes = load_suppressed_hashes(root / '.codex/private-memory')
                    self.assertTrue(capture(hashes))
                    self.assertEqual(summarize.call_count, 3)
                    self.assertEqual(note.read_bytes(), original)

    def test_forget_only_in_stored_summary_returns_filtered_memory_without_note_rewrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Kept source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            old = '\n\n'.join('## ' + h + '\nForgotten summary.' for h in flush.EXPECTED_SECTIONS)
            new = '\n\n'.join('## ' + h + '\nRegenerated summary.' for h in flush.EXPECTED_SECTIONS)
            summarize = mock.Mock(side_effect=[old, new])
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                first = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                    summarize,
                )
                note = root / (first[0][0] + '.md')
                original_note = note.read_bytes()
                suppress_derived_memory(root / '.codex/private-memory', 'Forgotten summary.')
                second = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc),
                    load_suppressed_hashes(root / '.codex/private-memory'), summarize,
                )
            self.assertEqual(second[0][0], first[0][0])
            self.assertIn('Regenerated summary.', second[0][1])
            self.assertEqual(note.read_bytes(), original_note)
            self.assertEqual(summarize.call_count, 2)

    def test_legacy_summary_only_forget_reuses_note_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Kept legacy source.', encoding='utf-8')
            first_text = f'# Files pasted by the user:\n\n## "First": {source}\n\n## My request:\n'
            second_text = f'# Files pasted by the user:\n\n## "New": {source}\n\n## My request:\n'
            old = '\n\n'.join('## ' + h + '\nForgotten summary.' for h in flush.EXPECTED_SECTIONS)
            new = '\n\n'.join('## ' + h + '\nLegacy regenerated summary.' for h in flush.EXPECTED_SECTIONS)
            summarize = mock.Mock(side_effect=[old, new])
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                first = attachment_memory.capture_sources([('user', first_text)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize)
                note = root / (first[0][0] + '.md')
                original_note = note.read_bytes()
                suppress_derived_memory(root / '.codex/private-memory', 'Forgotten summary.')
                second = attachment_memory.capture_sources(
                    [('user', second_text)], root, dt.datetime.now(dt.timezone.utc),
                    load_suppressed_hashes(root / '.codex/private-memory'), summarize,
                )
            self.assertEqual(second[0][0], first[0][0])
            self.assertIn('Legacy regenerated summary.', second[0][1])
            self.assertEqual(note.read_bytes(), original_note)
            self.assertEqual(summarize.call_count, 2)

    def test_note_parser_anchors_source_to_terminal_fence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            raw_source = 'Raw source.\n## Kaynak metni — güvenilmeyen alıntı\nStill source.'
            source.write_text(raw_source, encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join(
                '## ' + h + '\n' + (
                    '```text\nSummary marker.' + attachment_memory.SOURCE_BODY + '```'
                    if h == 'Bağlam' else 'Summary.'
                )
                for h in flush.EXPECTED_SECTIONS
            )
            self.assertTrue(flush.validate_summary(summary))
            summarize = mock.Mock(return_value=summary)
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                first = attachment_memory.capture_sources([('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize)
                source.unlink()
                second = attachment_memory.capture_sources([('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize)
            self.assertEqual(second, first)
            note = root / (first[0][0] + '.md')
            saved = note.read_text(encoding='utf-8')
            self.assertIn(raw_source, saved)
            self.assertIn('Summary marker.' + attachment_memory.SOURCE_BODY, second[0][1])
            summarize.assert_called_once()

    def test_recovery_preserves_supported_heading_whitespace(self):
        for heading in ('##  Bağlam', '##\tBağlam'):
            with self.subTest(heading=heading), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / 'home'
                source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
                source.parent.mkdir(parents=True)
                source.write_text('Durable source.', encoding='utf-8')
                text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
                summary = '\n\n'.join(
                    '## ' + name + '\nDurable summary.' for name in flush.EXPECTED_SECTIONS
                ).replace('## Bağlam', heading, 1)
                self.assertTrue(flush.validate_summary(summary))
                summarize = mock.Mock(return_value=summary)
                now = dt.datetime.now(dt.timezone.utc)
                with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                    first = attachment_memory.capture_sources([('user', text)], root, now, frozenset(), summarize)
                    source.unlink()
                    second = attachment_memory.capture_sources([('user', text)], root, now, frozenset(), summarize)
                self.assertEqual(second, first)
                summarize.assert_called_once()

    def test_same_uuid_note_publication_serializes_concurrent_envelopes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Concurrent source.', encoding='utf-8')
            first_text = f'# Files pasted by the user:\n\n## "First": {source}\n\n## My request:\nFirst.\n'
            second_text = f'# Files pasted by the user:\n\n## "Second": {source}\n\n## My request:\nSecond.\n'
            summary = '\n\n'.join('## ' + h + '\nConcurrent summary.' for h in flush.EXPECTED_SECTIONS)
            started = threading.Event()
            active = 0
            maximum = 0
            calls = 0
            guard = threading.Lock()

            def summarize(_):
                nonlocal active, maximum, calls
                with guard:
                    calls += 1
                    active += 1
                    maximum = max(maximum, active)
                started.set()
                time.sleep(0.1)
                with guard:
                    active -= 1
                return summary

            results = []
            errors = []
            def capture(text):
                try:
                    results.append(attachment_memory.capture_sources([('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(), summarize))
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                first_thread = threading.Thread(target=capture, args=(first_text,))
                second_thread = threading.Thread(target=capture, args=(second_text,))
                first_thread.start()
                self.assertTrue(started.wait(1))
                second_thread.start()
                first_thread.join(2)
                second_thread.join(2)
            self.assertFalse(errors, errors)
            self.assertEqual(calls, 1)
            self.assertEqual(maximum, 1)
            self.assertEqual(len(results), 2)

    def test_session_only_marker_cannot_finish_inside_note_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Session race source.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
            summary = '\n\n'.join('## ' + h + '\nSession race summary.' for h in flush.EXPECTED_SECTIONS)
            state = root / 'state'
            real_write = attachment_memory.atomic_write_text
            real_mark = mark_session_only
            marker_done = threading.Event()
            marker_thread = None

            def write_note(path, value, **kwargs):
                nonlocal marker_thread
                if Path(path).name.startswith('kaynak-'):
                    marker_thread = threading.Thread(target=mark_session_only, args=(state, 'race'))
                    marker_thread.start()
                    self.assertFalse(marker_done.wait(0.05))
                return real_write(path, value, **kwargs)

            def mark_and_signal(state_dir, session_id):
                real_mark(state_dir, session_id)
                marker_done.set()

            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(attachment_memory, 'atomic_write_text', side_effect=write_note), \
                 mock.patch('test_attachment_memory.mark_session_only', side_effect=mark_and_signal):
                result = attachment_memory.capture_sources(
                    [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                    lambda _: summary, state_dir=state, session_id='race',
                )
            marker_thread.join(1)
            self.assertTrue(marker_done.is_set())
            self.assertEqual(len(result), 1)
            self.assertTrue((state / ('memory-session-only-' + attachment_memory._attachment_digest('race'))).is_file())

    def test_envelope_scoped_receipts_do_not_overwrite_same_uuid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Shared source.', encoding='utf-8')
            first_text = f'# Files pasted by the user:\n\n## "First": {source}\n\n## My request:\nFirst request.\n'
            second_text = f'# Files pasted by the user:\n\n## "Second": {source}\n\n## My request:\nSecond request.\n'
            summary = '\n\n'.join('## ' + h + '\nShared summary.' for h in flush.EXPECTED_SECTIONS)
            summarize = mock.Mock(return_value=summary)
            now = dt.datetime.now(dt.timezone.utc)
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                first = attachment_memory.capture_sources(
                    [('user', first_text), ('user', second_text)], root, now, frozenset(), summarize,
                )
                second = first
                source.unlink()
                self.assertEqual(attachment_memory.capture_sources([('user', first_text)], root, now, frozenset(), summarize), first)
                self.assertEqual(attachment_memory.capture_sources([('user', second_text)], root, now, frozenset(), summarize), second)
            self.assertEqual(summarize.call_count, 1)
            mapping_paths = [
                attachment_memory._mapping_path(root / '.codex/scripts/.state', '11111111-1111-4111-8111-111111111111', attachment_memory._attachment_digest(text.rstrip()))
                for text in (first_text, second_text)
            ]
            self.assertEqual(len({path.name for path in mapping_paths}), 2)
            for path in mapping_paths:
                self.assertTrue(path.is_file())
                self.assertNotIn('summary', path.read_text(encoding='utf-8'))

    def test_reused_source_obeys_read_only_and_session_only_scope(self):
        for scope, marker, error in (
            ('read-only', mark_read_only_turn, 'memory-read-only'),
            ('session-only', mark_session_only, 'memory-session-excluded'),
        ):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / 'home'
                source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
                source.parent.mkdir(parents=True)
                source.write_text('Scoped source.', encoding='utf-8')
                text = f'# Files pasted by the user:\n\n## "Example": {source}\n\n## My request:\n'
                summary = '\n\n'.join('## ' + h + '\nScoped summary.' for h in flush.EXPECTED_SECTIONS)
                state = root / 'state'
                summarize = mock.Mock(return_value=summary)
                with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}):
                    attachment_memory.capture_sources(
                        [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                        summarize, state_dir=state, session_id='scoped',
                    )
                    marker(state, 'scoped')
                    with self.assertRaisesRegex(ValueError, error):
                        attachment_memory.capture_sources(
                            [('user', text)], root, dt.datetime.now(dt.timezone.utc), frozenset(),
                            summarize, state_dir=state, session_id='scoped',
                        )
                    summarize.assert_called_once()
                    clear_read_only_turn(state, 'scoped')

    def test_real_flush_preserves_attachment_and_rejects_missing_source(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            source = home / 'attachments/11111111-1111-4111-8111-111111111111/pasted-text.txt'
            source.parent.mkdir(parents=True)
            source.write_text('SOURCE_ONLY: retry after interruption without duplicate writes.', encoding='utf-8')
            text = f'# Files pasted by the user:\n\n## "Example": {source}\n\nPasted text contains the user\'s request.\n\n## My request:\n'
            text = '\n' + text  # Native App attachment messages begin with a newline.
            trace = root / 'trace.jsonl'
            trace.write_text(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]
            }}), encoding='utf-8')
            hook = root / 'hook.json'
            hook.write_text(json.dumps({'session_id': 'attachment-test', 'transcript_path': str(trace)}), encoding='utf-8')
            prompts = []
            summary = '\n\n'.join('## ' + h + '\nKesinti sonrası tekrar güvenliği.' for h in flush.EXPECTED_SECTIONS)
            def summarize(prompt, vault, **kwargs):
                prompts.append(prompt)
                return summary, None
            args = Namespace(hook_input=hook, reason='turnend')
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(flush, 'run_codex', side_effect=summarize), \
                 mock.patch.object(flush, 'maybe_trigger_compile'):
                result = flush.flush_once(args, dt.datetime.now(dt.timezone.utc), root, root / 'state')
            self.assertEqual(result, 0)
            self.assertTrue(any('SOURCE_ONLY' in p for p in prompts), 'Attachment never reached the summarizer')
            notes = list((root / '📥 000-Inbox/Paylaşılan Kaynaklar').glob('*.md'))
            self.assertEqual(len(notes), 1)
            self.assertIn('SOURCE_ONLY', notes[0].read_text(encoding='utf-8'))
            daily = next((root / 'daily').glob('*.md')).read_text(encoding='utf-8')
            self.assertIn(notes[0].stem, daily)
            source.unlink()
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(flush, 'run_codex') as model, \
                 mock.patch.object(flush, 'maybe_trigger_compile'):
                replay = flush.flush_once(args, dt.datetime.now(dt.timezone.utc), root, root / 'state')
            self.assertEqual(replay, 0)
            model.assert_not_called()

            missing_text = (
                '# Files pasted by the user:\n\n'
                f'## "Missing": {source}\n\n'
                '## My request:\nYeni eksik eki oku.\n'
            )
            with trace.open('a', encoding='utf-8', newline='\n') as handle:
                handle.write('\n' + json.dumps({'type': 'response_item', 'payload': {
                    'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': missing_text}],
                }}))
            with mock.patch.dict('os.environ', {'CODEX_HOME': str(home)}), \
                 mock.patch.object(flush, 'run_codex') as model, \
                 mock.patch.object(flush, 'maybe_trigger_compile'):
                failed = flush.flush_once(args, dt.datetime.now(dt.timezone.utc), root, root / 'state')
            self.assertEqual(failed, 1)
            model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
