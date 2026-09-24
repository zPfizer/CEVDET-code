"""CEVDET's native CRG watcher owns writes; Git requests, MCP only reads.

Uses the pinned upstream watcher and the existing scheduled task, not a daemon.
Vault is never a source. This module is installed outside both repositories.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

CODE_ROOT = Path.home() / "Desktop/CEVDET/CEVDET-code"
RUNTIME_ROOT = Path.home() / ".codex/cevdet-codex/integrations/crg"


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE, timeout=10, text=True, encoding="utf-8",
    ).strip()


def code_root(value, canonical=CODE_ROOT):
    """Reject protected roots before Git, parsing, traversal or source reads."""
    canonical = Path(os.path.abspath(canonical))
    for component in (canonical, *canonical.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("CANONICAL_ROOT_REPARSE")
    root, canonical = Path(value).resolve(strict=True), canonical.resolve(strict=True)
    if root != canonical and not (
        root.parent == canonical.parent and root.name.startswith(canonical.name + "-")
    ):
        raise ValueError("PROTECTED_SOURCE")
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if common != (canonical / ".git").resolve():
        raise ValueError("UNREGISTERED_CODE_ROOT")
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("EXACT_ROOT_REQUIRED")
    return root


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def receipt(event, **fields):
    value = {"event": event, "pid": os.getpid(), "executable": sys.executable,
             "time": time.time(), **fields}
    print(json.dumps(value), flush=True)
    return value


def request(root, event, canonical=CODE_ROOT):
    # A coalescing request is not a graph write. A request racing a build is
    # retained because the watcher captures its token BEFORE starting the build.
    value = receipt("hook_request", repo=str(root), hook=event, token=uuid.uuid4().hex)
    directory = canonical / ".code-review-graph/refresh-requests"
    if directory.resolve() != canonical.resolve() / ".code-review-graph/refresh-requests":
        raise ValueError("EXTERNAL_GRAPH_SCOPE_NOT_AUTHORIZED")
    name = hashlib.sha256(str(root.resolve()).encode()).hexdigest() + ".json"
    atomic_json(directory / name, value)


def requests(directory):
    """Small task-owned metadata only; reject links before opening anything."""
    if directory.resolve() != directory.absolute():
        raise ValueError("LINKED_REQUEST_DIRECTORY")
    for claimed in directory.glob("*.claim"):
        # A crash after claiming but before acknowledgment must replay safely.
        name, generation, suffix = claimed.name.rsplit(".", 2)
        if not name.endswith(".json") or len(generation) != 32:
            continue
        if claimed.is_symlink() or claimed.resolve() != claimed.absolute():
            raise ValueError("LINKED_REQUEST")
        try:
            os.link(claimed, directory / name)
        except FileExistsError:
            pass  # An already-pending refresh covers this root's current state.
        claimed.unlink()
    result = {}
    for path in directory.glob("*.json"):
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("LINKED_REQUEST")
        if len(result) >= 256:
            break  # Bounded batches; the consumer acknowledges before the next batch.
        if path.stat().st_size > 4096:
            raise ValueError("REQUEST_LIMIT")
        result[path.name] = path.read_bytes()
    return result


class RefreshRequested:
    """Native watch's existing stop_event interface, without another timer."""
    def __init__(self, marker, initial):
        self.marker, self.initial = marker, initial

    def wait(self, seconds):
        time.sleep(seconds)
        return requests(self.marker) != self.initial


def acknowledge(directory, name, expected):
    """Claim atomically; never delete a generation that arrived during a build."""
    path = directory / name
    claimed = directory / (name + "." + uuid.uuid4().hex + ".claim")
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return
    if claimed.read_bytes() != expected:
        try:
            # A newer pending generation is put back without replacing any
            # further request. Any pending request refreshes current source.
            os.link(claimed, path)
        except FileExistsError:
            pass
    # An error above intentionally leaves the claim recoverable on restart.
    claimed.unlink()


@contextmanager
def owner(db):
    # The existing global freshness guard respects this same exclusive marker.
    # Never steal an orphan: an interrupted owner requires verified cleanup.
    marker = db.with_name("crg-freshness.lock")
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, json.dumps({"owner": "cevdet-native-watch", "pid": os.getpid()}).encode())
        yield
    finally:
        os.close(fd)
        marker.unlink()


def build(root, db):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build
    from code_review_graph.postprocessing import run_post_processing
    publish(root, db, ready=False)
    with GraphStore(db) as store:
        result = full_build(root, store)
        if result.get("errors"):
            raise RuntimeError("BUILD_INCOMPLETE")
        processed = run_post_processing(store)
        if processed.get("warnings"):
            raise RuntimeError("POSTPROCESS_INCOMPLETE")
        publish(root, db)
        receipt("index_built", repo=str(root), head=git(root, "rev-parse", "HEAD"),
                files=result.get("files_parsed", result.get("files_updated")))


def graph_path(root):
    from code_review_graph.incremental import get_db_path
    db = get_db_path(root, read_only=True).resolve()
    if not db.is_relative_to(root):
        raise ValueError("EXTERNAL_GRAPH_SCOPE_NOT_AUTHORIZED")
    return db


def consume_requests(snapshot, processed, canonical):
    """The existing Code task serially refreshes requested registered worktrees."""
    for name, data in snapshot.items():
        try:
            value = json.loads(data)
            root = code_root(value["repo"], canonical)
            if name != hashlib.sha256(str(root).encode()).hexdigest() + ".json":
                raise ValueError("REQUEST_ROOT_MISMATCH")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            # Requests outlive disposable worktrees. Report that request once
            # per process, retain it, and keep the valid repositories running.
            receipt("request_rejected", request=name, reason=type(exc).__name__)
            processed[name] = data
            acknowledge(canonical / ".code-review-graph/refresh-requests", name, data)
            continue
        if root != canonical:
            db = graph_path(root)
            with owner(db):
                build(root, db)
        processed[name] = data
        acknowledge(canonical / ".code-review-graph/refresh-requests", name, data)
    # This is bounded observation of the last batch, not another durable queue.
    for name in list(processed):
        if name not in snapshot:
            del processed[name]


def watch(root):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import watch as native_watch
    from code_review_graph.postprocessing import run_post_processing
    db = graph_path(root)
    marker = root / ".code-review-graph/refresh-requests"
    processed = {}
    with owner(db):
        receipt("watch_owner", repo=str(root), database=str(db))
        while True:
            initial = requests(marker)
            consume_requests(initial, processed, root)
            build(root, db)
            if requests(marker):
                continue
            initial = {}
            with native_store_type(root, db)(db) as store:
                def updated(current):
                    publish(root, db, ready=False)
                    processed = run_post_processing(current)
                    if not processed.get("warnings"):
                        publish(root, db)
                    receipt("index_updated", repo=str(root), warnings=len(processed.get("warnings", [])))
                    return processed

                try:
                    native_watch(root, store, on_files_updated=updated,
                                 stop_event=RefreshRequested(marker, initial))
                except BaseException:
                    publish(root, db, ready=False)
                    raise
                if requests(marker) == initial:
                    # Native watch handles Ctrl+C itself. A normal return with
                    # no request must exit, not immediately restart a build.
                    return
                receipt("refresh_requested", repo=str(root))


def native_store_type(root, db):
    from code_review_graph.graph import GraphStore

    class NativeWriteStore(GraphStore):
        def store_file_nodes_edges(self, *args, **kwargs):
            publish(root, db, ready=False)
            return super().store_file_nodes_edges(*args, **kwargs)

        def remove_file_data(self, *args, **kwargs):
            publish(root, db, ready=False)
            return super().remove_file_data(*args, **kwargs)
    return NativeWriteStore


def readonly_store_type():
    from code_review_graph.graph import GraphStore

    class ReadOnlyGraphStore(GraphStore):
        """Upstream 2.3.8 has no readonly constructor; retain its query methods.

        An actual SQLite mode=ro connection prevents schema and data mutations.
        No installed package file is modified. Tests bind this adapter to 2.3.8.
        """
        def __init__(self, db_path):
            self.db_path = Path(db_path)
            self._conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro",
                                         uri=True, timeout=5, check_same_thread=False,
                                         isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            # Upstream SQL impact traversal uses connection-local TEMP tables.
            # mode=ro protects the graph; temp_store keeps scratch work in RAM.
            self._conn.execute("PRAGMA temp_store=MEMORY")
            schema = self._conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if not schema or schema[0] != "9":
                self._conn.close()
                raise ValueError("PINNED_SCHEMA_REQUIRED")
            self._nxg_cache = None
            self._cache_lock = threading.Lock()
    return ReadOnlyGraphStore


def native_matches(source, graph):
    """Upstream may recognize a file extension yet intentionally emit no nodes.

    Only actually empty parser results may explain missing File rows. Retain
    all source hashes in the caller's before/after identity, including these.
    """
    if not graph or graph["head"] != source["head"] or graph["branch"] != source["branch"]:
        return False
    if any(source["files"].get(path) != value for path, value in graph["files"].items()):
        return False
    from code_review_graph.parser import CodeParser
    parser = CodeParser(Path(source["root"]))
    for path in source["files"].keys() - graph["files"].keys():
        data = Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest() != source["files"][path]:
            return False
        nodes, edges = parser.parse_bytes(Path(path), data)
        if nodes or edges:
            return False
    return bool(graph["files"])


def identity_digest(source):
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def publish(root, db, ready=True):
    # Publish only after all upstream postprocessing. Changed source cannot use
    # an older receipt; rebuilding unchanged source invalidates it beforehand.
    sys.path.insert(0, str(RUNTIME_ROOT))
    from serve_guarded import source_identity, graph_identity
    value = {"ready": False, "generation": uuid.uuid4().hex}
    if ready:
        try:
            source = source_identity(str(root))
            if native_matches(source, graph_identity(db)):
                value.update(ready=True, source_sha256=identity_digest(source))
        except (ValueError, OSError, subprocess.SubprocessError):
            pass  # A source race stays unready until the native watcher catches up.
    atomic_json(db.with_name("crg-publication.json"), value)


def publication(db):
    path = db.with_name("crg-publication.json")
    if path.is_symlink() or path.resolve() != path.absolute() or path.stat().st_size > 4096:
        raise ValueError("INVALID_PUBLICATION")
    return path.read_bytes()


def serve():
    # Reuse the installed freshness identity checks, but never its refresh path.
    sys.path.insert(0, str(RUNTIME_ROOT))
    from serve_guarded import source_identity, graph_identity, database_path, TOOLS
    from fastmcp.server.middleware import Middleware
    from fastmcp.exceptions import ToolError
    from code_review_graph.tools import _common
    _common.GraphStore = readonly_store_type()
    from code_review_graph import main as upstream

    class ReadOnlyGuard(Middleware):
        async def on_call_tool(self, context, call_next):
            if context.message.name not in TOOLS:
                raise ToolError("NON_READ_TOOL_DENIED")
            try:
                root = code_root((context.message.arguments or {}).get("repo_root", ""))
                before = source_identity(str(root))
                db = database_path(before)
                published = publication(db)
                bound = json.loads(published)
                if not bound.get("ready") or bound.get("source_sha256") != identity_digest(before):
                    raise ValueError("SOURCE_FALLBACK_REQUIRED")
                if not native_matches(before, graph_identity(db)):
                    raise ValueError("SOURCE_FALLBACK_REQUIRED")
                result = await call_next(context)
                if published != publication(db) or before != source_identity(str(root)) or not native_matches(before, graph_identity(db)):
                    raise ValueError("SOURCE_CHANGED_DURING_QUERY")
                return result
            except Exception as exc:
                raise ToolError(str(exc)) from exc

    upstream.mcp.add_middleware(ReadOnlyGuard())
    upstream.main(tools=",".join(sorted(TOOLS)), auto_watch=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["watch", "hook", "protected-status", "serve"])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--event", default="manual")
    args = parser.parse_args()
    # pythonw has no standard streams; all receipts stay outside the Vault.
    if sys.stdout is None:
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        sys.stdin = open(os.devnull)
        sys.stdout = sys.stderr = open(RUNTIME_ROOT / "cevdet-runtime.log", "a", encoding="utf-8")
    if args.role == "protected-status":
        receipt("vault_protected", indexed=False)
        return
    try:
        root = code_root(args.repo)
    except ValueError:
        if args.role == "hook":
            receipt("protected_hook", indexed=False)
            return
        raise
    if args.role == "hook":
        request(root, args.event)
    elif args.role == "watch":
        watch(root)
    else:
        # Startup and tool arguments must both remain in the code universe.
        serve()


if __name__ == "__main__":
    main()
