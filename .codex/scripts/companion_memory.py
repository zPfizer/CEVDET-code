"""Persist session records once and render the three Companion views."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from file_lock import locked
from memory_ledger import (
    MemoryRead, contains_suppressed_unit, filter_suppressed_text, is_session_only,
    memory_read, memory_write_guard, sanitize_text, suppression_guard,
)
from state_store import atomic_write_bytes, atomic_write_json, atomic_write_text
from user_evidence import EVIDENCE, SOURCE


BEGIN = '<!-- cevo-auto-session '
END = '<!-- /cevo-auto-session -->'
BLOCK = re.compile(r'<!-- cevo-auto-session ([0-9.]+) ([a-f0-9]{64}) -->.*?'
                   r'<!-- /cevo-auto-session -->', re.S)
SESSION = re.compile(r'<!-- cevo-session ([a-f0-9]{64}) (\S+) ([a-f0-9]{64}) -->\r?\n'
                     r'(.*?)\r?\n<!-- /cevo-session -->', re.S)
_BLOCK_BYTES = re.compile(rb'<!-- cevo-auto-session ([0-9.]+) ([a-f0-9]{64}) -->.*?'
                           rb'<!-- /cevo-auto-session -->', re.S)

CANONICAL_SCHEMA = 'companion-sessions-v1'
CANONICAL_RELATIVE = Path('daily/companion-sessions.json')
VIEW_NAMES = ('Last-Session.md', 'Journal.md', 'Threads.md')
SOURCE_RELATIVE = Path('🔮 850-Companion') / 'Sources'
_SOURCE_PREFIX = '<!-- cevo-generated-source-split:v1:'


def source_marker(name: str) -> bytes:
    if name not in VIEW_NAMES:
        raise ValueError('companion-view-invalid')
    return f'{_SOURCE_PREFIX}{name} -->'.encode()


def _safe_path(root: Path, parts: tuple[str, ...], error: str) -> Path:
    root = root.resolve(strict=True)
    current = root
    for part in parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise ValueError(error)
    if current.exists() and not current.is_file():
        raise ValueError(error)
    return current


def _source_path(root: Path, name: str) -> Path:
    if name not in VIEW_NAMES:
        raise ValueError('companion-view-invalid')
    return _safe_path(root, ('🔮 850-Companion', 'Sources', name), 'companion-source-path-invalid')


def _view_path(root: Path, name: str) -> Path:
    if name not in VIEW_NAMES:
        raise ValueError('companion-view-invalid')
    return _safe_path(root, ('🔮 850-Companion', name), 'companion-view-path-invalid')


def _canonical_path(root: Path) -> Path:
    root = root.resolve(strict=True)
    daily = root / 'daily'
    if daily.is_symlink() or daily.is_junction() or (daily.exists() and not daily.is_dir()):
        raise ValueError('companion-canonical-path-invalid')
    return _safe_path(root, ('daily', CANONICAL_RELATIVE.name), 'companion-canonical-path-invalid')


def _without_evidence_metadata(text: str) -> str:
    text = re.sub(r'&lt;!-- user-(?:evidence|source):[^\n]*?-->', '', text)
    return SOURCE.sub('', EVIDENCE.sub('', text))


def _decode_escaped_evidence(text: str) -> str:
    return re.sub(r'&lt;!--\s*(user-(?:evidence|source):[^\n]*?)-->', r'<!-- \1-->', text)


def _parse_sessions(text: str) -> dict[str, tuple[dt.datetime, str, str]]:
    from flush import SessionSummary
    records: dict[str, tuple[dt.datetime, str, str]] = {}
    matches = list(SESSION.finditer(text))
    for match in matches:
        identity, stamp, key, value = match.groups()
        if identity in records:
            raise ValueError('companion-session-duplicate')
        event = dt.datetime.fromisoformat(stamp)
        if event.tzinfo is None:
            raise ValueError('companion-session-time-invalid')
        SessionSummary.parse(value)
        records[identity] = event, key, value.replace('\r\n', '\n')
    if text.count('<!-- cevo-session ') != len(matches):
        raise ValueError('companion-session-invalid')
    return records


def _block_matches(
    payload: bytes,
    expected: dict[str, tuple[dt.datetime, str, str]] | None = None,
) -> list[tuple[re.Match[bytes], dict[str, tuple[dt.datetime, str, str]]]]:
    matches = list(_BLOCK_BYTES.finditer(payload))
    if payload.count(BEGIN.encode('ascii')) != len(matches):
        raise ValueError('companion-block-invalid')
    valid = []
    for match in matches:
        if expected:
            try:
                if any(match.group(2).decode() == key and float(match.group(1)) == event.timestamp()
                       for event, key, _value in expected.values()):
                    valid.append((match, {}))
                    continue
            except (UnicodeDecodeError, ValueError):
                pass
        try:
            found = _parse_sessions(match.group(0).decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            continue
        if found:
            valid.append((match, found))
    if len(valid) > 1:
        raise ValueError('companion-block-duplicate')
    return valid


def _records(text: str) -> dict[str, tuple[dt.datetime, str, str]]:
    """Read the old generated shape while keeping anonymous snapshots unattributed."""
    from flush import EXPECTED_SECTIONS, SessionSummary
    text = _without_evidence_metadata(text)
    blocks = list(BLOCK.finditer(text))
    if text.count(BEGIN) != len(blocks):
        raise ValueError('companion-block-invalid')
    canonical = [_parse_sessions(block.group(0)) for block in blocks if SESSION.search(block.group(0))]
    if len(canonical) > 1:
        raise ValueError('companion-block-duplicate')
    if canonical:
        return canonical[0]
    if len(blocks) != 1:
        if blocks:
            raise ValueError('companion-block-invalid')
        return {}
    body = blocks[0].group(0).replace('\r\n', '\n')
    old = body.split('### Bağlam\n', 1)
    if len(old) != 2:
        return {}
    value = '### Bağlam\n' + old[1].split('## Previous Sessions', 1)[0].split(END, 1)[0]
    for name in EXPECTED_SECTIONS:
        value = value.replace(f'### {name}\n', f'## {name}\n')
    value = SessionSummary.parse(value.strip()).render()
    stamp = re.search(r'^## Session: (\S+)', body, re.M)
    event = dt.datetime.fromisoformat(stamp[1]) if stamp else dt.datetime.fromtimestamp(
        float(blocks[0][1]), dt.timezone.utc,
    )
    return {blocks[0][2]: (event if event.tzinfo else event.replace(tzinfo=dt.timezone.utc), blocks[0][2], value)}


def _manual_parts(name: str, payload: bytes) -> tuple[bytes, bytes]:
    parts = payload.split(source_marker(name))
    if len(parts) != 2:
        raise ValueError('companion-source-split-invalid')
    return parts[0], parts[1]


def _join_manual(name: str, prefix: bytes, suffix: bytes) -> bytes:
    marker = source_marker(name)
    if marker in prefix or marker in suffix:
        raise ValueError('companion-source-split-collision')
    return prefix + marker + suffix


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _catalog_payload(records, manual) -> dict[str, Any]:
    return {
        'schema': CANONICAL_SCHEMA,
        'records': {identity: {'event': event.isoformat(), 'key': key, 'summary': value}
                    for identity, (event, key, value) in sorted(records.items())},
        'manual': {name: manual[name] for name in sorted(manual)},
    }


def _load_catalog(root: Path):
    path = _canonical_path(root)
    if not path.is_file():
        return {}, {}, False
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('companion-canonical-unreadable') from exc
    if not isinstance(payload, dict) or payload.get('schema') != CANONICAL_SCHEMA:
        raise ValueError('companion-canonical-invalid')
    records = {}
    for identity, row in payload.get('records', {}).items() if isinstance(payload.get('records'), dict) else ():
        if not re.fullmatch(r'[a-f0-9]{64}', identity) or not isinstance(row, dict):
            raise ValueError('companion-canonical-invalid')
        if not all(isinstance(row.get(field), str) for field in ('event', 'key', 'summary')):
            raise ValueError('companion-canonical-invalid')
        event = dt.datetime.fromisoformat(row['event'])
        if event.tzinfo is None or not re.fullmatch(r'[a-f0-9]{64}', row['key']):
            raise ValueError('companion-canonical-invalid')
        from flush import SessionSummary
        SessionSummary.parse(row['summary'])
        records[identity] = event, row['key'], row['summary'].replace('\r\n', '\n')
    if not isinstance(payload.get('records'), dict):
        raise ValueError('companion-canonical-invalid')
    manual = payload.get('manual', {})
    if not isinstance(manual, dict):
        raise ValueError('companion-canonical-invalid')
    for name, row in manual.items():
        if name not in VIEW_NAMES or not isinstance(row, dict) or not all(
                isinstance(row.get(field), str) and re.fullmatch(r'[a-f0-9]{64}', row[field])
                for field in ('source_sha256', 'outside_sha256')):
            raise ValueError('companion-canonical-invalid')
    return records, {name: dict(row) for name, row in manual.items()}, True


def _manual_meta(name: str, source: bytes, prefix: bytes, suffix: bytes) -> dict[str, str]:
    return {'path': str(SOURCE_RELATIVE / name).replace('\\', '/'),
            'source_sha256': _sha(source), 'outside_sha256': _sha(prefix + suffix)}


def _manual_for_view(root: Path, name: str, meta, expected, *, write_source: bool, canonical: bool):
    source_path, view_path = _source_path(root, name), _view_path(root, name)
    if canonical and not source_path.is_file():
        raise ValueError('companion-manual-source-missing')
    if source_path.is_file():
        source = source_path.read_bytes()
        prefix, suffix = _manual_parts(name, source)
    else:
        current = view_path.read_bytes() if view_path.is_file() else b''
        valid = _block_matches(current, expected or None) if current else []
        prefix, suffix = (current[:valid[0][0].start()], current[valid[0][0].end():]) if valid else (current, b'')
        source = _join_manual(name, prefix, suffix)
        if write_source:
            source_path.parent.mkdir(parents=True, exist_ok=True)
            _write_manual(source_path, source)
    current = view_path.read_bytes() if view_path.is_file() else None
    if current is not None:
        valid = _block_matches(current, expected or None)
        view_prefix, view_suffix = (
            (current[:valid[0][0].start()], current[valid[0][0].end():])
            if valid else (current, b'')
        )
        view_identity = _join_manual(name, view_prefix, view_suffix)
        if view_identity != source:
            previous = meta or {}
            current_hash, wanted_hash = _sha(view_identity), _sha(source)
            previous_hash = previous.get('source_sha256')
            if previous_hash == current_hash:
                pass  # source-only edit; the view is stale and will be regenerated
            elif previous_hash == wanted_hash:
                prefix, suffix = view_prefix, view_suffix
                source = _join_manual(name, prefix, suffix)
                if write_source:
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    _write_manual(source_path, source)
            elif current_hash != wanted_hash:
                raise ValueError('companion-manual-view-conflict')
    source = _join_manual(name, prefix, suffix)
    return prefix, suffix, _manual_meta(name, source, prefix, suffix)


def _bodies(records):
    from flush import SessionSummary
    if not records:
        return {name: '' for name in VIEW_NAMES}
    ordered = sorted(records.items(), key=lambda item: (item[1][0], item[0]), reverse=True)
    latest = ordered[0][1][0].isoformat()
    last = [f'## Session: {latest}', 'Oturumların güncel özetleri; kaynaklar ayrı tutulur.']
    threads = ['## Oturumların açık konuları', 'Özetler eylem yetkisi veya canlı proje durumu değildir. Tamamlanan ve iptal edilen işler yeniden başlatılmaz.']
    journal = ''
    for identity, (event, key, value) in ordered:
        stamp = event.isoformat()
        link = f'Kaynak: [[daily/{event.date().isoformat()}|Konuşma kaydı]] · {stamp}'
        rendered = value.replace('<!--', '&lt;!--')
        parts = SessionSummary.parse(rendered).sections
        last.append(f'{link}\n\n<!-- cevo-session {identity} {stamp} {key} -->\n{rendered}\n<!-- /cevo-session -->')
        threads.append(f'### Oturum — {stamp}\n\n{parts["Yapılacaklar"]}\n\n{link}')
        if not journal:
            journal = f'## Güncel\n\n### {stamp}\n\n<!-- journal-latest: {stamp} -->\n\n{parts["Bağlam"]}\n\n{parts["Alınan Kararlar"]}\n\n{link}\n'
    return {'Last-Session.md': '\n\n'.join(last) + '\n\n## Previous Sessions\n',
            'Threads.md': '\n\n'.join(threads) + '\n', 'Journal.md': journal}


def _visible(records, hashes):
    from flush import SessionSummary
    visible = {}
    for identity, (event, key, value) in records.items():
        if contains_suppressed_unit(f'daily/{event.date().isoformat()}.md', hashes):
            continue
        text = filter_suppressed_text(_decode_escaped_evidence(value), hashes)
        try:
            visible[identity] = event, key, SessionSummary.parse(text).render()
        except ValueError:
            pass
    return visible


def _render(records, manuals, hashes):
    visible = _visible(records, hashes)
    if not visible:
        return {name: prefix + suffix for name, (prefix, suffix) in manuals.items()}
    newest, newest_key, _ = max(visible.values(), key=lambda value: (value[0], value[1]))
    header = f'{BEGIN}{newest.timestamp()} {newest_key} -->\n'.encode()
    bodies = _bodies(visible)
    return {name: prefix + header + bodies[name].encode() + END.encode() + suffix
            for name, (prefix, suffix) in manuals.items()}


def _raw_views(root: Path, hashes: frozenset[str]):
    records, metadata, canonical = _load_catalog(root)
    if not canonical:
        result = {}
        for name in VIEW_NAMES:
            if contains_suppressed_unit(f'🔮 850-Companion/{name}', hashes):
                continue
            path = _view_path(root, name)
            if path.is_file():
                result[name] = path.read_bytes().decode('utf-8')
            else:
                source = _source_path(root, name)
                if source.is_file():
                    prefix, suffix = _manual_parts(name, source.read_bytes())
                    result[name] = (prefix + suffix).decode('utf-8')
        return result
    records = dict(records)
    _reconcile_current(root, records, hashes, strict=False)
    manuals = {}
    for name in VIEW_NAMES:
        if not contains_suppressed_unit(f'🔮 850-Companion/{name}', hashes):
            prefix, suffix, _ = _manual_for_view(root, name, metadata.get(name), records, write_source=False, canonical=True)
            manuals[name] = prefix, suffix
    return {name: value.decode('utf-8') for name, value in _render(records, manuals, hashes).items()}


def _project_views(values: dict[str, str], memory: MemoryRead) -> dict[str, str]:
    result = {}
    for name, value in values.items():
        text = memory.project_text(f'🔮 850-Companion/{name}', value)
        if text is not None:
            result[name] = text
    return result


def render_views(root: Path, *, hashes: frozenset[str] = frozenset(), memory: MemoryRead | None = None) -> dict[str, str]:
    if memory is None:
        with memory_read(root) as current:
            return _project_views(_raw_views(root, current._hashes), current)
    if hashes and hashes != memory._hashes:
        raise ValueError('memory-preferences-changed')
    return _project_views(_raw_views(root, memory._hashes), memory)


def ensure_views(root: Path, state: Path | None = None, *, write: bool = True,
                 hashes: frozenset[str] = frozenset(), memory: MemoryRead | None = None) -> dict[str, str]:
    state = state or root / '.codex/scripts/.state'
    if memory is None:
        with memory_read(root) as current:
            return ensure_views(root, state, write=write, hashes=current._hashes, memory=current)
    if hashes and hashes != memory._hashes:
        raise ValueError('memory-preferences-changed')
    hashes = memory._hashes
    if not write:
        return _project_views(_raw_views(root, hashes), memory)
    state.mkdir(parents=True, exist_ok=True)
    if not (root / '🔮 850-Companion').is_dir():
        return {}
    with suppression_guard(root / '.codex/private-memory', hashes), locked(state / 'companion-publish'):
        records, metadata, canonical = _load_catalog(root)
        if not canonical:
            return _project_views(_raw_views(root, hashes), memory)
        _reconcile_current(root, records, hashes)
        previous_metadata = dict(metadata)
        manuals = {}
        for name in VIEW_NAMES:
            if contains_suppressed_unit(f'🔮 850-Companion/{name}', hashes):
                continue
            prefix, suffix, meta = _manual_for_view(root, name, metadata.get(name), records, write_source=True, canonical=True)
            manuals[name] = prefix, suffix
            metadata[name] = meta
        atomic_write_json(_canonical_path(root), _catalog_payload(records, previous_metadata), sort_keys=True)
        result = {}
        for name, payload in _render(records, manuals, hashes).items():
            path = _view_path(root, name)
            if not path.is_file() or path.read_bytes() != payload:
                _write_projection(path, payload)
            result[name] = payload.decode('utf-8')
        atomic_write_json(_canonical_path(root), _catalog_payload(records, metadata), sort_keys=True)
        return _project_views(result, memory)


def _write_projection(path: Path, payload: bytes) -> None:
    try:
        atomic_write_text(path, payload.decode('utf-8'), newline='')
    except UnicodeDecodeError:
        atomic_write_bytes(path, payload)


def _write_manual(path: Path, payload: bytes) -> None:
    atomic_write_bytes(path, payload)


def _discover_current(root: Path, hashes: frozenset[str] = frozenset(), *, strict: bool = True):
    records = {}
    for name in VIEW_NAMES:
        if contains_suppressed_unit(f'🔮 850-Companion/{name}', hashes):
            continue
        path = _view_path(root, name)
        if not path.is_file():
            continue
        for _match, found in _block_matches(path.read_bytes()):
            for identity, (event, key, value) in found.items():
                record = event, key, _decode_escaped_evidence(value)
                if strict and identity in records and records[identity] != record:
                    raise ValueError('companion-session-conflict')
                if identity in records:
                    continue
                records[identity] = record
    return records


def _reconcile_current(root: Path, records, hashes: frozenset[str], *, strict: bool = True) -> None:
    if contains_suppressed_unit('🔮 850-Companion/Last-Session.md', hashes):
        return
    for identity, candidate in _discover_current(root, hashes, strict=strict).items():
        event, _key, _value = candidate
        if contains_suppressed_unit(f'daily/{event.date().isoformat()}.md', hashes):
            continue
        previous = records.get(identity)
        if previous is None or event > previous[0]:
            records[identity] = candidate
        elif strict and event == previous[0] and candidate != previous:
            previous_view = _bodies(_visible({identity: previous}, hashes))['Last-Session.md']
            candidate_view = _bodies(_visible({identity: candidate}, hashes))['Last-Session.md']
            if candidate[1] != previous[1] or candidate_view != previous_view:
                raise ValueError('companion-session-conflict')


def migrate(root: Path, *, state: Path | None = None) -> dict[str, int | str]:
    root = root.resolve(strict=True)
    state = state or root / '.codex/scripts/.state'
    canonical_path = _canonical_path(root)
    if canonical_path.is_file():
        records, _manual, _ = _load_catalog(root)
        for name in VIEW_NAMES:
            source = _source_path(root, name)
            if not source.is_file():
                raise ValueError('companion-manual-source-missing')
            _manual_parts(name, source.read_bytes())
        return {'status': 'already-migrated', 'records': len(records), 'sources': len(VIEW_NAMES)}
    if not (root / '🔮 850-Companion').is_dir():
        raise ValueError('companion-missing')
    state.mkdir(parents=True, exist_ok=True)
    with locked(state / 'companion-publish'):
        snapshots = {name: (_view_path(root, name).read_bytes() if _view_path(root, name).is_file() else b'') for name in VIEW_NAMES}
        records = {}
        plans = {}
        for name, current in snapshots.items():
            try:
                valid = _block_matches(current, records or None) if current else []
            except ValueError as exc:
                if str(exc) != 'companion-block-invalid':
                    raise
                valid = []
            if valid:
                match, found = valid[0]
                for identity, record in found.items():
                    if identity in records and records[identity] != record:
                        raise ValueError('companion-session-conflict')
                    records[identity] = record
                prefix, suffix = current[:match.start()], current[match.end():]
            else:
                prefix, suffix = current, b''
            source = _join_manual(name, prefix, suffix)
            source_path = _source_path(root, name)
            if source_path.is_file() and source_path.read_bytes() != source:
                raise ValueError('companion-source-exists')
            plans[name] = source_path, source, _manual_meta(name, source, prefix, suffix)
        if any(_view_path(root, name).is_file() and _view_path(root, name).read_bytes() != snapshots[name] for name in VIEW_NAMES):
            raise ValueError('companion-view-changing-during-migration')
        manual = {name: plan[2] for name, plan in plans.items()}
        for source_path, source, _meta in plans.values():
            if not source_path.is_file():
                source_path.parent.mkdir(parents=True, exist_ok=True)
                _write_manual(source_path, source)
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(canonical_path, _catalog_payload(records, manual), sort_keys=True)
    return {'status': 'migrated', 'records': len(records), 'sources': len(VIEW_NAMES)}


def previous_summary(root: Path, session_id: str, *, state: Path | None = None) -> str:
    state = state or root / '.codex/scripts/.state'
    if is_session_only(state, session_id):
        return ''
    with memory_read(root) as memory:
        if memory.excludes('🔮 850-Companion/Last-Session.md'):
            return ''
        records, _manual, canonical = _load_catalog(root)
        if canonical:
            _reconcile_current(root, records, memory._hashes, strict=False)
        else:
            path = root / '🔮 850-Companion/Last-Session.md'
            if path.is_file():
                _relative, text = memory.read_source(path)
                records = _records(text or '')
        value = records.get(hashlib.sha256(session_id.encode()).hexdigest())
        if value is None or memory.excludes(f'daily/{value[0].date().isoformat()}.md'):
            return ''
        text = memory.project_text('🔮 850-Companion/Last-Session.md', _decode_escaped_evidence(value[2]))
        return _without_evidence_metadata(text) if text is not None else ''


def publish(root: Path, state: Path, summary: str, event: dt.datetime,
            key: str, session_id: str, hashes: frozenset[str]) -> None:
    from flush import SessionSummary
    companion = root / '🔮 850-Companion'
    if not companion.is_dir():
        return
    if companion.resolve() != root.resolve() / companion.name:
        raise ValueError('companion-path-invalid')
    if contains_suppressed_unit(f'daily/{event.date().isoformat()}.md', hashes):
        return
    safe = re.sub(r'&lt;!-- user-(?:evidence|source):[^\n]*?-->', '', sanitize_text(summary, max_chars=max(1, len(summary)))[0])
    safe = filter_suppressed_text(safe, hashes)
    parts = SessionSummary.parse(safe).sections
    if event.tzinfo is None:
        event = event.replace(tzinfo=dt.timezone.utc)
    session_key = hashlib.sha256(session_id.encode()).hexdigest()
    with memory_write_guard(state, session_id), locked(state / f'memory-session-only-{session_key}'):
        if is_session_only(state, session_id):
            raise ValueError('memory-session-excluded')
        with suppression_guard(root / '.codex/private-memory', hashes), locked(state / 'companion-publish'):
            records, metadata, canonical = _load_catalog(root)
            if not canonical:
                discovered = _discover_current(root, hashes)
                records.update(discovered)
            else:
                _reconcile_current(root, records, hashes)
            previous_metadata = dict(metadata)
            manuals = {}
            for name in VIEW_NAMES:
                if contains_suppressed_unit(f'🔮 850-Companion/{name}', hashes):
                    continue
                prefix, suffix, meta = _manual_for_view(root, name, metadata.get(name), records, write_source=True, canonical=canonical)
                manuals[name] = prefix, suffix
                metadata[name] = meta
            current = records.get(session_key)
            value = SessionSummary(parts).render()
            if current is None or current[0] <= event:
                records[session_key] = event, key, value
            canonical_path = _canonical_path(root)
            canonical_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(canonical_path, _catalog_payload(records, previous_metadata), sort_keys=True)
            for name, payload in _render(records, manuals, hashes).items():
                path = _view_path(root, name)
                if not path.is_file() or path.read_bytes() != payload:
                    _write_projection(path, payload)
            atomic_write_json(canonical_path, _catalog_payload(records, metadata), sort_keys=True)
            reflection = state / 'needs-reflection'
            if reflection.is_file() and f'session={session_key}' in reflection.read_text(encoding='utf-8'):
                reflection.unlink()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('migrate', help='seed canonical Companion sources')
    command.add_argument('--root', type=Path, required=True)
    command.add_argument('--state', type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(migrate(args.root, state=args.state), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
