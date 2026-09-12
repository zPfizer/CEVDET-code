"""Transcript indeksinin doğrulama ve sürüklenme korkuluklarını sürer.

%100 kapsam sözleşmesinin dördüncü dilimi. İndeks durumu gerçek bir
transkriptten üretilir; her korkuluk o gerçek durumun tekil bozulmasıyla
tetiklenir. Yarış pencereleri (okuma sırasında değişen kaynak) dosyayla
üretilemediği yerde dar mock ile açılır.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _fixtures import CODEX_DIR  # noqa: F401
import flush
import transcript_index


SESSION = "oturum-1"


def _sealed(payload):
    payload["metadata_sha256"] = transcript_index._metadata_digest(payload)
    return payload


def _role_none_retained(base):
    import copy
    payload = copy.deepcopy(base)
    payload["rows"][0]["role"] = None
    return _sealed(payload)


def _row_bytes_grown(base):
    import copy
    payload = copy.deepcopy(base)
    payload["rows"][0]["line_bytes"] += 1
    return _sealed(payload)


def _row_bytes_grown_with_complete_tail(base):
    import copy
    payload = copy.deepcopy(base)
    payload["rows"][0]["line_bytes"] += 1
    size = payload["source_bytes"]
    payload["partial_tail"] = {
        "offset": size - 1, "line_bytes": 1, "sha256": "a" * 64, "complete": True,
    }
    return _sealed(payload)


def _write_transcript(path: Path, lines: list[object]) -> None:
    payload = "\n".join(
        line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
        for line in lines
    )
    path.write_text(payload + "\n", encoding="utf-8")


def _user(message: str) -> dict:
    return {"type": "event_msg", "payload": {"type": "user_message", "message": message}}


def _open(state: Path, transcript: Path, **overrides):
    options = dict(
        hashes=frozenset(),
        parser=flush._message_parts_for_index,
        text_from_content=flush._text_from_content,
        max_line_bytes=1024 * 1024,
        force_session_only=False,
    )
    options.update(overrides)
    return transcript_index.open_or_update(state, SESSION, transcript, **options)


class IndexBuildEdges(unittest.TestCase):
    def test_argument_and_state_dir_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Merhaba")])
            with self.assertRaises(ValueError):
                transcript_index.open_or_update(
                    root, "", transcript,
                    hashes=frozenset(), parser=flush._message_parts_for_index,
                    text_from_content=flush._text_from_content,
                    max_line_bytes=1, force_session_only=False,
                )
            with self.assertRaises(ValueError):
                _open(root, transcript, max_line_bytes=0)
            blocker = root / "dosya"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                _open(blocker / "alt", transcript)

    def test_string_source_path_is_coerced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Merhaba dünya")])
            index = _open(root, str(transcript))
        self.assertGreater(index.indexed_bytes, 0)

    def test_blank_and_non_object_lines_are_classified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Önce"), "", "123", _user("Sonra")])
            index = _open(root, transcript)
        classes = [row["privacy_classification"] for row in index.rows]
        self.assertIn("blank", classes)
        self.assertIn("ignored", classes)

    def test_text_extraction_failures_are_jsonl_errors(self) -> None:
        def patlayan(_content):
            raise TypeError("bozuk içerik")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Merhaba")])
            with self.assertRaises(ValueError):
                _open(root, transcript, text_from_content=patlayan)

    def test_non_string_text_becomes_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Merhaba")])
            index = _open(root, transcript, text_from_content=lambda _c: None)
        self.assertTrue(all(row["filtered_length"] == 0 for row in index.rows))

    def test_empty_transcript_indexes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            transcript.write_bytes(b"")
            index = _open(root, transcript)
        self.assertEqual(list(index.rows), [])

    def test_force_session_only_clears_fresh_and_cached_builds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Kalıcı olabilirdi")])
            fresh = _open(root, transcript, force_session_only=True)
            self.assertTrue(all(not row["retained"] for row in fresh.rows))

            other = Path(temporary) / "iki"
            other.mkdir()
            normal = _open(other, transcript)
            self.assertTrue(any(row["retained"] for row in normal.rows))
            cached = _open(other, transcript, force_session_only=True)
            self.assertTrue(all(not row["retained"] for row in cached.rows))

    def test_source_change_between_hashes_is_detected(self) -> None:
        real = transcript_index._hash_source
        calls = {"n": 0}

        def tampered(path, prefix_length):
            calls["n"] += 1
            size, full, prefix = real(path, prefix_length)
            if calls["n"] >= 2:
                return size, "0" * 64, prefix
            return size, full, prefix

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Merhaba")])
            with mock.patch.object(transcript_index, "_hash_source", tampered):
                with self.assertRaises(ValueError):
                    _open(root, transcript)


class IndexValidatorEdges(unittest.TestCase):
    def _index_payload(self, root: Path) -> dict:
        transcript = root / "t.jsonl"
        _write_transcript(transcript, [_user("Merhaba dünya, kalıcı karar.")])
        _open(root, transcript)
        path = transcript_index.index_path(root, SESSION)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_validate_state_rejects_each_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = self._index_payload(Path(temporary))

        def mutate(**changes):
            payload = copy.deepcopy(base)
            payload.update(changes)
            # Üstveri özeti bilinçli olarak tazelenir: amaç özet uyuşmazlığı
            # değil, hedeflenen alan korkuluğunu tetiklemek.
            payload["metadata_sha256"] = transcript_index._metadata_digest(payload)
            return payload

        row = copy.deepcopy(base["rows"][0])
        bad_key_row = {**row, "fazla_alan": 1}
        bad_type_row = {**row, "line_bytes": "çok"}
        bad_chunk_row = {**row, "filtered_chunks": [{"length": 0, "sha256": "a" * 64}]}
        bad_role_row = {**row, "role": "uzayli", "retained": True}

        cases = [
            [1, 2],
            mutate(schema_version=99),
            mutate(partial_tail={"complete": True}),
            mutate(indexed_bytes=base["indexed_bytes"] + 1),
            mutate(rows="liste-degil"),
            mutate(rows=[bad_key_row]),
            mutate(rows=[bad_type_row]),
            mutate(rows=[bad_chunk_row]),
            mutate(rows=[bad_role_row]),
            mutate(counters={"bozuk": 1}),
            mutate(counters={**base["counters"], "recognized_messages": 999}),
            mutate(reducer={"bozuk": 1}),
            mutate(reducer={**base["reducer"], "retained_rows": "liste-degil"}),
            mutate(reducer={**base["reducer"], "retained_rows": [999]}),
            mutate(reducer={**base["reducer"], "session_only": True}),
            _role_none_retained(base),
            _row_bytes_grown(base),
            _row_bytes_grown_with_complete_tail(base),
        ]
        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    transcript_index._validate_state(payload)

    def test_partial_tail_consistency_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = self._index_payload(Path(temporary))

        def with_partial(partial, **changes):
            payload = copy.deepcopy(base)
            payload["partial_tail"] = partial
            payload.update(changes)
            payload["metadata_sha256"] = transcript_index._metadata_digest(payload)
            return payload

        size = base["source_bytes"]
        cases = [
            # complete kuyruk indekslenmiş bölgenin gerisinde kalamaz
            with_partial(
                {"offset": size - 1, "line_bytes": 1, "sha256": "a" * 64,
                 "complete": True},
                indexed_bytes=size - 1,
            ),
            # eksik kuyruk tam indekslenen sınırdan başlamalı
            with_partial(
                {"offset": size - 1, "line_bytes": 1, "sha256": "a" * 64,
                 "complete": False},
                indexed_bytes=size - 2,
            ),
            # kuyruk yoksa indekslenen == kaynak olmalı
            with_partial(None, indexed_bytes=size - 1),
        ]
        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    transcript_index._validate_state(payload)

    def test_grown_source_reuses_cache_with_session_only_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Kalıcı ilk tur.")])
            first = _open(root, transcript)
            self.assertTrue(any(row["retained"] for row in first.rows))
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_user("İkinci tur.")) + "\n")
            grown = _open(root, transcript, force_session_only=True)
        self.assertTrue(all(not row["retained"] for row in grown.rows))

    def test_offset_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = self._index_payload(Path(temporary))
        payload = copy.deepcopy(base)
        payload["rows"][0]["line_bytes"] += 1
        payload["source_bytes"] += 1
        with self.assertRaises(ValueError):
            transcript_index._validate_state(payload)

    def test_hash_source_and_partial_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Merhaba")])
            with self.assertRaises(ValueError):
                transcript_index._hash_source(transcript, -1)
            self.assertFalse(
                transcript_index._partial_tail_matches(
                    root / "yok.jsonl",
                    {"offset": 0, "line_bytes": 4, "sha256": "a" * 64},
                )
            )
            kucuk = root / "kucuk.jsonl"
            kucuk.write_bytes(b"ab")
            buyuk_stat = transcript.stat()
            with mock.patch(
                "pathlib.Path.stat", side_effect=[buyuk_stat, kucuk.stat()]
            ):
                with self.assertRaises(ValueError):
                    transcript_index._hash_source(transcript, 0)

    def test_accessor_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "t.jsonl"
            _write_transcript(transcript, [_user("Merhaba")])
            index = _open(root, transcript)
            with self.assertRaises(ValueError):
                index.coverage_digest(-1)
            with self.assertRaises(ValueError):
                index.previous_retained_row(999)


class ReadSelectedRowsEdges(unittest.TestCase):
    def _build(self, root: Path) -> tuple:
        transcript = root / "t.jsonl"
        _write_transcript(transcript, [_user("Merhaba dünya, kalıcı karar.")])
        index = _open(root, transcript)
        row_ids = [reference.row_index for reference in index.chunks]
        return transcript, index, row_ids

    def _read(self, index, transcript, row_ids, **overrides):
        options = dict(
            hashes=frozenset(),
            parser=flush._message_parts_for_index,
            text_from_content=flush._text_from_content,
            max_line_bytes=1024 * 1024,
            verify_source=False,
        )
        options.update(overrides)
        return transcript_index.read_selected_rows(
            index, transcript, row_ids, **options
        )

    def test_identity_row_and_size_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, index, row_ids = self._build(root)

            other = root / "baska.jsonl"
            _write_transcript(other, [_user("Bambaşka içerik burada.")])
            with self.assertRaises(ValueError):
                self._read(index, other, row_ids)

            with self.assertRaises(ValueError):
                self._read(index, transcript, [999])

            with self.assertRaises(ValueError):
                self._read(index, transcript, row_ids, max_line_bytes=4)

    def test_row_drift_and_privacy_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript, index, row_ids = self._build(root)

            original = transcript.read_bytes()
            tampered = original.replace(b"kal", b"KAL", 1)
            self.assertEqual(len(tampered), len(original))
            transcript.write_bytes(tampered)
            with mock.patch.object(
                transcript_index, "source_identity",
                return_value=index.state["source_identity_sha256"],
            ):
                with self.assertRaises(ValueError):
                    self._read(index, transcript, row_ids)
            transcript.write_bytes(original)

            def patlayan_parser(_record):
                raise ValueError("bozuk")

            with self.assertRaises(ValueError):
                self._read(index, transcript, row_ids, parser=patlayan_parser)

            with self.assertRaises(ValueError):
                self._read(
                    index, transcript, row_ids,
                    parser=lambda record: (None, None, False),
                )

            from memory_ledger import memory_text_hash
            with self.assertRaises(ValueError):
                self._read(
                    index, transcript, row_ids,
                    hashes=frozenset({memory_text_hash("Merhaba dünya, kalıcı karar.")}),
                )


if __name__ == "__main__":
    unittest.main()
