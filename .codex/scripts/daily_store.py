from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from file_lock import LockUnavailable, locked
from memory_ledger import filter_suppressed_text, load_suppressed_hashes, suppression_guard
from graph_integrity import daily_with_graph_link
from state_store import atomic_write_json, atomic_write_text
from user_evidence import EVIDENCE


SCHEMA_VERSION = 1
COMPACT_SCHEMA_VERSION = 2
_NAMESPACE = re.compile(r"[a-z][a-z0-9-]*\Z")
_KEY = re.compile(r"[0-9a-f]{64}\Z")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _deduplicate_evidence(summary: str, existing: str) -> str:
    # Exact copies only: conflicting records still fail identity validation.
    known = {match[0] for match in EVIDENCE.finditer(existing)}
    def retain(match: re.Match[str]) -> str:
        if match[0] in known:
            return ''
        known.add(match[0])
        return match[0]
    return EVIDENCE.sub(retain, summary)


def _read_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("daily-operation-invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") not in (SCHEMA_VERSION, COMPACT_SCHEMA_VERSION):
        raise ValueError("daily-operation-invalid")
    if ('after_generation' in value
            and (type(value['after_generation']) is not int or value['after_generation'] < 1)):
        raise ValueError('daily-operation-invalid')
    if ('previous_after_generation' in value
            and (type(value.get('previous_after_generation')) is not int
                 or value['previous_after_generation'] < 0
                 or type(value.get('after_generation')) is not int
                 or value['previous_after_generation'] >= value['after_generation'])):
        raise ValueError('daily-operation-invalid')
    if ('previous_after_sha256' in value
            and (not isinstance(value['previous_after_sha256'], str)
                 or _KEY.fullmatch(value['previous_after_sha256']) is None)):
        raise ValueError('daily-operation-invalid')
    if value['schema_version'] == COMPACT_SCHEMA_VERSION and value.get('status') != 'committed':
        raise ValueError('daily-operation-invalid')
    return value


def _operation_id(namespace: str, key: str) -> str:
    return _sha256(f"{namespace}\0{key}".encode("utf-8"))


def _daily_path(vault_root: Path, receipt: dict[str, Any]) -> Path:
    date_text = receipt.get('date')
    daily_relative = receipt.get('daily_relative')
    if not isinstance(date_text, str) or not isinstance(daily_relative, str):
        raise ValueError('daily-operation-path-invalid')
    try:
        valid_date = dt.date.fromisoformat(date_text).isoformat() == date_text
    except ValueError:
        valid_date = False
    if not valid_date or daily_relative != f'daily/{date_text}.md':
        raise ValueError('daily-operation-path-invalid')
    path = vault_root / daily_relative
    if path.resolve().parent != vault_root.resolve() / 'daily':
        raise ValueError('daily-operation-path-invalid')
    return path


def _after_path(operation_dir: Path, operation_id: str,
                receipt: dict[str, Any]) -> Path:
    if not isinstance(operation_id, str) or _KEY.fullmatch(operation_id) is None:
        raise ValueError('daily-operation-path-invalid')
    generation = receipt.get('after_generation')
    if generation is None:
        name = f'{operation_id}.after.md'
    else:
        if type(generation) is not int or generation < 1:
            raise ValueError('daily-operation-invalid')
        name = f'{operation_id}.after.{generation}.md'
    path = operation_dir / name
    if path.resolve().parent != operation_dir.resolve():
        raise ValueError('daily-operation-path-invalid')
    return path


def _previous_after_path(operation_dir: Path, operation_id: str,
                         receipt: dict[str, Any]) -> Path | None:
    if 'previous_after_generation' not in receipt:
        generation = receipt.get('after_generation')
        if generation is None:
            return None
        generation -= 1
    else:
        generation = receipt['previous_after_generation']
    previous = {}
    if generation:
        previous['after_generation'] = generation
    return _after_path(operation_dir, operation_id, previous)


def _next_after_generation(operation_dir: Path, operation_id: str) -> int:
    prefix = f'{operation_id}.after.'
    generations = []
    for path in operation_dir.glob(f'{prefix}*.md'):
        suffix = path.name.removeprefix(prefix).removesuffix('.md')
        if suffix.isdecimal():
            generations.append(int(suffix))
    return max(generations, default=0) + 1


def _after_cleanup_paths(operation_dir: Path, operation_id: str,
                         receipt: dict[str, Any], after_path: Path) -> tuple[Path, ...]:
    paths = [after_path, _previous_after_path(operation_dir, operation_id, receipt)]
    if receipt.get('after_generation') is None or receipt.get('previous_after_generation') == 0:
        paths.append(operation_dir / f'{operation_id}.after.md')
    return tuple(path for index, path in enumerate(paths)
                 if path is not None and path not in paths[:index])


def _compact_values(receipt: dict[str, Any], after: bytes) -> dict[str, Any]:
    compact = {
        'schema_version': COMPACT_SCHEMA_VERSION, 'status': 'committed',
        'operation_id': receipt['operation_id'], 'date': receipt['date'],
        'daily_relative': receipt['daily_relative'],
        'after_sha256': _sha256(after), 'after_size': len(after),
    }
    if 'after_generation' in receipt:
        compact['after_generation'] = receipt['after_generation']
    if 'previous_after_generation' in receipt:
        compact['previous_after_generation'] = receipt['previous_after_generation']
    if 'previous_after_sha256' in receipt:
        compact['previous_after_sha256'] = receipt['previous_after_sha256']
    return compact


def _unlink_after(path: Path, expected_sha256: str | None) -> int:
    if expected_sha256 is None or not path.exists():
        return 0
    content = path.read_bytes()
    if _sha256(content) != expected_sha256:
        return 0
    path.unlink()
    return len(content)


def _finish_commit(receipt_path: Path, after_path: Path, receipt: dict[str, Any],
                   after: bytes, fail_after: str | None = None) -> None:
    # Persist the replacement proof before removing its redundant recovery image.
    atomic_write_json(receipt_path, _compact_values(receipt, after), sort_keys=True)
    if fail_after == 'committed':
        raise RuntimeError('daily-injected:committed')
    previous_after_path = _previous_after_path(
        receipt_path.parent, receipt['operation_id'], receipt,
    )
    for path in _after_cleanup_paths(
        receipt_path.parent, receipt['operation_id'], receipt, after_path,
    ):
        expected = _sha256(after) if path == after_path else (
            receipt.get('previous_after_sha256') if path == previous_after_path else None
        )
        _unlink_after(path, expected)


def _checked_compact_values(receipt: dict[str, Any], after_path: Path,
                            current: bytes) -> dict[str, Any]:
    if receipt['schema_version'] == COMPACT_SCHEMA_VERSION:
        size, digest = receipt.get('after_size'), receipt.get('after_sha256')
        if type(size) is not int or size <= 0 or not isinstance(digest, str) or not _KEY.fullmatch(digest):
            raise ValueError('daily-operation-invalid')
        if len(current) < size or _sha256(current[:size]) != digest:
            raise ValueError('daily-committed-drift')
        # A crash after the receipt write may leave an image. Never discard a
        # different image merely because the operation is marked committed.
        if after_path.exists() and after_path.read_bytes() != current[:size]:
            raise ValueError('daily-after-image-invalid')
        return receipt
    after = after_path.read_bytes()
    if not after or any(
        not isinstance(receipt.get(field), str) or not _KEY.fullmatch(receipt[field])
        for field in ('before_sha256', 'after_sha256', 'body_sha256', 'after_image_sha256')
    ):
        raise ValueError('daily-operation-invalid')
    if _sha256(after) != receipt['after_image_sha256'] or receipt['after_sha256'] != receipt['after_image_sha256']:
        raise ValueError('daily-after-image-invalid')
    if not current.startswith(after):
        raise ValueError('daily-committed-drift')
    return _compact_values(receipt, after)


def compact_completed(vault_root: Path, state_dir: Path, *, apply: bool = False) -> dict[str, Any]:
    """Validate old completed operations; only --apply replaces proofs and frees images.

    Dry runs do not create locks or modify files. Apply rechecks each operation
    under the same operation/day locks as publish, and skips busy or incomplete work.
    """
    report: dict[str, Any] = dict(eligible=0, compacted=0, already_compact=0, pending=0,
                                  busy=0, reclaimable_bytes=0, bytes_released=0, blocked=[])
    directory = state_dir / 'daily-operations'
    if directory.resolve().parent != state_dir.resolve():
        raise ValueError('daily-operation-path-invalid')
    for path in sorted(directory.glob('*.json')):
        try:
            if not _KEY.fullmatch(path.stem) or path.resolve().parent != directory.resolve():
                raise ValueError('daily-operation-path-invalid')
            guard = locked(state_dir / f'daily-operation-{path.stem}', timeout=0) if apply else nullcontext()
            with guard:
                receipt = _read_receipt(path)
                if receipt is None or receipt.get('operation_id') != path.stem:
                    raise ValueError('daily-operation-invalid')
                if receipt.get('status') in ('prepared', 'committing'):
                    report['pending'] += 1
                    continue
                if receipt.get('status') != 'committed':
                    raise ValueError('daily-operation-invalid')
                day = _daily_path(vault_root, receipt)
                after_path = _after_path(directory, path.stem, receipt)
                previous_after_path = _previous_after_path(directory, path.stem, receipt)
                cleanup_paths = _after_cleanup_paths(directory, path.stem, receipt, after_path)
                guard = locked(state_dir / f"daily-{receipt['date']}", timeout=0) if apply else nullcontext()
                with guard:
                    compact = _checked_compact_values(receipt, after_path, day.read_bytes())
                    validated = []
                    for stale in cleanup_paths:
                        expected = (receipt.get('after_image_sha256')
                                    or receipt.get('after_sha256')) if stale == after_path else (
                                        receipt.get('previous_after_sha256')
                                        if stale == previous_after_path else None
                                    )
                        if expected is None or not stale.exists():
                            validated.append((stale, expected, 0))
                            continue
                        content = stale.read_bytes()
                        if _sha256(content) != expected:
                            raise ValueError('daily-after-image-invalid')
                        validated.append((stale, expected, len(content)))
                    reclaimable = sum(item[2] for item in validated)
                    if not reclaimable and receipt['schema_version'] == COMPACT_SCHEMA_VERSION:
                        report['already_compact'] += 1
                        continue
                    report['eligible'] += 1
                    report['reclaimable_bytes'] += reclaimable
                    if apply:
                        atomic_write_json(path, compact, sort_keys=True)
                        released = 0
                        for stale, expected, _size in validated:
                            released += _unlink_after(stale, expected)
                        report['compacted'] += 1
                        report['bytes_released'] += released
        except LockUnavailable:
            report['busy'] += 1
        except (OSError, ValueError) as exc:
            report['blocked'].append({'operation': path.stem, 'error': str(exc)})
    return report


def publish(
    vault_root: Path,
    state_dir: Path,
    summary: str,
    reason: str,
    now: dt.datetime,
    *,
    idempotency_key: str,
    marker_namespace: str = 'flush',
    _fail_after: str | None = None,
    memory_hashes: frozenset[str] | None = None,
) -> bool:
    if reason not in {'sessionend', 'precompact', 'turnend'}:
        raise ValueError('daily-reason-invalid')
    if _NAMESPACE.fullmatch(marker_namespace) is None:
        raise ValueError('daily-marker-namespace-invalid')
    if _KEY.fullmatch(idempotency_key) is None:
        raise ValueError('daily-idempotency-key-invalid')
    if not isinstance(summary, str) or not summary:
        raise ValueError('daily-summary-invalid')
    private_root = vault_root / '.codex/private-memory'
    hashes = load_suppressed_hashes(private_root) if memory_hashes is None else memory_hashes
    projected = filter_suppressed_text(summary, hashes)
    # A prepared operation from an older preference set cannot be replayed.
    if hashes:
        idempotency_key = _sha256((idempotency_key + '\0' + ''.join(sorted(hashes))).encode())
    with suppression_guard(private_root, hashes):
        if not projected.strip():
            return False
        return _publish(vault_root, state_dir, projected, reason, now,
                        idempotency_key=idempotency_key, marker_namespace=marker_namespace,
                        _fail_after=_fail_after)


def _publish(
    vault_root: Path,
    state_dir: Path,
    summary: str,
    reason: str,
    now: dt.datetime,
    *,
    idempotency_key: str,
    marker_namespace: str = "flush",
    _fail_after: str | None = None,
) -> bool:
    state_dir.mkdir(parents=True, exist_ok=True)
    operation_id = _operation_id(marker_namespace, idempotency_key)
    operation_dir = state_dir / "daily-operations"
    receipt_path = operation_dir / f"{operation_id}.json"
    if (operation_dir.resolve().parent != state_dir.resolve()
            or receipt_path.resolve().parent != operation_dir.resolve()):
        raise ValueError('daily-operation-path-invalid')
    with locked(state_dir / f"daily-operation-{operation_id}"):
        receipt = _read_receipt(receipt_path)
        if receipt is not None and receipt.get('operation_id') != operation_id:
            raise ValueError('daily-operation-invalid')
        after_path = _after_path(
            operation_dir,
            operation_id,
            receipt if receipt is not None else {},
        )
        if receipt is None:
            date_text = now.date().isoformat()
            daily_path = vault_root / "daily" / f"{date_text}.md"
            with locked(state_dir / f"daily-{date_text}"):
                before = daily_path.read_bytes() if daily_path.is_file() else b""
                # Derive the newline from the locked read: an unlocked probe can
                # disagree with ``before`` and mix line endings into the image.
                newline = "\r\n" if b"\r\n" in before else "\n"
                base = daily_with_graph_link(
                    before.decode("utf-8") if before else "",
                    date_text,
                    newline,
                )
                suffix = ", compaction öncesi" if reason == "precompact" else ""
                # Carried decisions retain their original proof. Keep one exact copy
                # per day so identity lookup stays unique and forgetting sees the quote.
                summary = _deduplicate_evidence(summary, base)
                body = f"### Oturum ({now.strftime('%H:%M')}){suffix}{newline}{newline}{summary}{newline}"
                body_sha256 = _sha256(body.encode("utf-8"))
                marker = f"<!-- {marker_namespace}:{idempotency_key}:{body_sha256} -->"
                if f"<!-- {marker_namespace}:{idempotency_key}:" in base:
                    raise ValueError("daily-marker-body-drift")
                after_text = base.rstrip("\r\n") + newline * 2 + marker + newline + body
                atomic_write_text(after_path, after_text, newline="")
                receipt = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "prepared",
                    "operation_id": operation_id,
                    "date": date_text,
                    "daily_relative": f"daily/{date_text}.md",
                    "before_sha256": _sha256(before),
                    "after_sha256": _sha256(after_text.encode("utf-8")),
                    "body_sha256": body_sha256,
                    "after_image_sha256": _sha256(after_path.read_bytes()),
                }
                atomic_write_json(receipt_path, receipt, sort_keys=True)
            if _fail_after == "prepared":
                raise RuntimeError("daily-injected:prepared")
        daily_path = _daily_path(vault_root, receipt)
        date_text = receipt['date']
        with locked(state_dir / f"daily-{date_text}"):
            current = daily_path.read_bytes() if daily_path.is_file() else b""
            if receipt.get('status') == 'committed':
                compact = _checked_compact_values(receipt, after_path, current)
                if receipt != compact:
                    atomic_write_json(receipt_path, compact, sort_keys=True)
                previous_after_path = _previous_after_path(operation_dir, operation_id, receipt)
                for stale in _after_cleanup_paths(operation_dir, operation_id, receipt, after_path):
                    expected = (receipt.get('after_image_sha256')
                                or receipt.get('after_sha256')) if stale == after_path else (
                                    receipt.get('previous_after_sha256')
                                    if stale == previous_after_path else None
                                )
                    _unlink_after(stale, expected)
                return False
            if receipt.get('status') not in ('prepared', 'committing'):
                raise ValueError('daily-operation-invalid')
            after = after_path.read_bytes()
            if _sha256(after) != receipt.get("after_image_sha256"):
                raise ValueError("daily-after-image-invalid")
            current_sha256 = _sha256(current)
            if current_sha256 == receipt.get("after_sha256"):
                _finish_commit(receipt_path, after_path, receipt, after, _fail_after)
                return False
            if current_sha256 != receipt.get("before_sha256"):
                marker_prefix = f"<!-- {marker_namespace}:{idempotency_key}:"
                marker = f"{marker_prefix}{receipt.get('body_sha256')} -->"
                current_text = current.decode("utf-8")
                if marker in current_text:
                    compact_receipt = dict(receipt)
                    compact_receipt['after_generation'] = _next_after_generation(
                        operation_dir, operation_id,
                    )
                    compact_receipt['previous_after_generation'] = receipt.get('after_generation', 0)
                    compact_receipt['previous_after_sha256'] = receipt['after_image_sha256']
                    new_after_path = _after_path(operation_dir, operation_id, compact_receipt)
                    atomic_write_text(new_after_path, current_text, newline="")
                    _finish_commit(
                        receipt_path,
                        new_after_path,
                        compact_receipt,
                        current,
                        _fail_after,
                    )
                    return False
                if marker_prefix in current_text:
                    raise ValueError("daily-marker-body-drift")
                after_text = after.decode("utf-8")
                marker_offset = after_text.find(marker)
                if marker_offset < 0:
                    raise ValueError("daily-after-image-invalid")
                newline = "\r\n" if b"\r\n" in current else "\n"
                base = daily_with_graph_link(current_text, date_text, newline)
                body = after_text[marker_offset + len(marker):].split('\n', 1)[1]
                body = _deduplicate_evidence(body, base)
                body_sha256 = _sha256(body.encode('utf-8'))
                marker = f'{marker_prefix}{body_sha256} -->'
                rebased = (
                    base.rstrip("\r\n")
                    + newline * 2
                    + marker + newline + body
                )
                rebased_receipt = dict(receipt)
                rebased_receipt['after_generation'] = _next_after_generation(
                    operation_dir, operation_id,
                )
                rebased_receipt['previous_after_generation'] = receipt.get('after_generation', 0)
                rebased_receipt['previous_after_sha256'] = receipt['after_image_sha256']
                new_after_path = _after_path(operation_dir, operation_id, rebased_receipt)
                atomic_write_text(new_after_path, rebased, newline="")
                after = new_after_path.read_bytes()
                receipt.update(
                    status="prepared",
                    before_sha256=current_sha256,
                    body_sha256=body_sha256,
                    after_sha256=_sha256(after),
                    after_image_sha256=_sha256(after),
                    after_generation=rebased_receipt['after_generation'],
                    previous_after_generation=rebased_receipt['previous_after_generation'],
                    previous_after_sha256=rebased_receipt['previous_after_sha256'],
                )
                atomic_write_json(receipt_path, receipt, sort_keys=True)
                after_path = new_after_path
            receipt["status"] = "committing"
            atomic_write_json(receipt_path, receipt, sort_keys=True)
            if _fail_after == "committing":
                raise RuntimeError("daily-injected:committing")
            daily_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(daily_path, after.decode("utf-8"), newline="")
            if _fail_after == "replace":
                raise RuntimeError("daily-injected:replace")
            _finish_commit(receipt_path, after_path, receipt, after, _fail_after)
    return True


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Compact verified completed daily-operation copies.')
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Apply; the default is a read-only preview.')
    args = parser.parse_args()
    root = args.project_root.resolve()
    result = compact_completed(root, root / '.codex/scripts/.state', apply=args.apply)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(1 if result['blocked'] else 0)
