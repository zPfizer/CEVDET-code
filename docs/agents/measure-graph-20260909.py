"""Replay three fixed retrieval tasks; no model calls or product imports.

Run with code-review-graph 2.3.8, rg, and a clean checkout of BASELINE.
Graph build/update must be timed separately. Output includes raw probe payloads.
"""
import ast
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from importlib.metadata import version

from code_review_graph.tools import detect_changes_func, get_minimal_context, query_graph

BASELINE = "16d5e2139fb750045bb0a6717d284fd611f7bc96"
ROOT = Path(sys.argv[1]).resolve()
OUTPUT = Path(sys.argv[2])
FILES = [".codex/hooks", ".codex/scripts", ".codex/tests"]


def command(*args):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def source(path, start, end):
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    return "\n".join(f"{path}:{i + 1}:{lines[i]}" for i in range(start - 1, end))


def truth_callers(symbol):
    """Source oracle for these two unaliased symbols; dynamic calls excluded."""
    found = set()
    for folder in FILES[:2]:
        for path in (ROOT / folder).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            class Visitor(ast.NodeVisitor):
                scope = ()

                def visit_FunctionDef(self, node):
                    previous = self.scope
                    self.scope += (node.name,)
                    self.generic_visit(node)
                    self.scope = previous

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_ClassDef(self, node):
                    previous = self.scope
                    self.scope += (node.name,)
                    self.generic_visit(node)
                    self.scope = previous

                def visit_Call(self, node):
                    if isinstance(node.func, ast.Name) and node.func.id == symbol:
                        found.add(path.relative_to(ROOT).as_posix() + "::" + ".".join(self.scope))
                    self.generic_visit(node)
            Visitor().visit(tree)
    return sorted(found)


def query(pattern, target, payloads):
    result = query_graph(pattern, ROOT.as_posix() + "/" + target,
                         repo_root=str(ROOT), detail_level="minimal", max_results=100)
    payloads.append(result)
    assert result["status"] == "ok", result
    if result.get("results_omitted", 0):
        result = query_graph(pattern, ROOT.as_posix() + "/" + target,
                             repo_root=str(ROOT), detail_level="standard", max_results=100)
        payloads.append(result)
        assert result["status"] == "ok" and not result.get("results_omitted", 0), result
    return result


def graph(case):
    payloads = [get_minimal_context(task=case, repo_root=str(ROOT), base="HEAD~1")]
    assert payloads[0]["status"] == "ok", payloads[0]
    if case == "discovery":
        target = ".codex/scripts/file_lock.py::locked"
        query("callers_of", target, payloads)
        query("tests_for", target, payloads)
    elif case == "debug":
        target = ".codex/scripts/vault_retrieval.py::_stable_source_snapshot"
        for pattern in ("callers_of", "callees_of", "tests_for"):
            query(pattern, target, payloads)
    else:
        # Minimal omits the full symbol list: pay for the expansion too.
        for detail in ("minimal", "standard"):
            result = detect_changes_func(base="HEAD~1", repo_root=str(ROOT),
                                         detail_level=detail, max_results=100, max_flows=5)
            payloads.append(result)
            assert result["status"] == "ok" and not result.get("truncated", False), result
    return payloads


def baseline(case):
    # Explicit tracked files avoid multi-root ignore traversal differences.
    paths = command("git", "ls-files", "--", *FILES).splitlines()
    assert paths, "No tracked source files"
    if case == "discovery":
        return [command("rg", "--no-ignore", "-n", r"\blocked\s*\(", *paths)]
    if case == "debug":
        return [command("rg", "--no-ignore", "-n", "_stable_source_snapshot|_source_snapshot|_source_signature", *paths)]
    return [command("git", "diff", "--no-ext-diff", "--unified=3", "HEAD~1", "HEAD"),
            command("rg", "--no-ignore", "-n", r"\bhandle_user_prompt\s*\(", *paths)]


def main():
    assert command("git", "rev-parse", "HEAD").strip() == BASELINE
    assert not command("git", "diff", "HEAD", "--", ".codex/hooks", ".codex/scripts", ".codex/tests")
    assert version("code-review-graph") == "2.3.8"
    shared = {
        "discovery": [(".codex/scripts/file_lock.py", 1, 80),
                      (".codex/tests/test_file_lock.py", 1, 122)],
        "debug": [(".codex/scripts/vault_retrieval.py", 473, 544),
                  (".codex/scripts/vault_retrieval.py", 573, 612),
                  (".codex/scripts/vault_retrieval.py", 966, 1005),
                  (".codex/tests/test_concurrency_readers.py", 200, 229)],
        "pr": [(".codex/hooks/hook.py", 603, 848),
               (".codex/tests/test_concurrency_readers.py", 21, 43),
               (".codex/tests/test_concurrency_readers.py", 280, 306)],
    }
    report = {"commit": BASELINE, "version": version("code-review-graph"),
              "measurement": "retrieval replay; not end-to-end agent time or billed tokens",
              "root": str(ROOT), "cases": {},
              "source_truth": {s: truth_callers(s) for s in ("locked", "_stable_source_snapshot")}}
    assert len(report["source_truth"]["locked"]) == 44
    assert len(report["source_truth"]["_stable_source_snapshot"]) == 1
    for case, spans in shared.items():
        samples = {"rg": [], "graph": []}
        for repeat in range(3):
            for method in (("rg", "graph") if repeat % 2 == 0 else ("graph", "rg")):
                started = time.perf_counter()
                try:
                    payloads = baseline(case) if method == "rg" else graph(case)
                    discovery_seconds = time.perf_counter() - started
                    # Both routes must still inspect identical implementation/test text.
                    verification = "\n".join(source(*span) for span in spans)
                    payload = "\n".join(p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
                                        for p in payloads)
                    samples[method].append({"status": "ok", "seconds": time.perf_counter() - started,
                        "retrieval_seconds": discovery_seconds, "retrieval_chars": len(payload),
                        "source_chars": len(verification), "total_chars": len(payload) + len(verification),
                        "payloads": payloads})
                except Exception as error:
                    samples[method].append({"status": "error", "error": str(error)})
        report["cases"][case] = samples
    quality = {}
    for case, symbol in (("discovery", "locked"), ("debug", "_stable_source_snapshot")):
        first = report["cases"][case]["graph"][0]
        if first["status"] != "ok":
            continue
        results = [p for p in first["payloads"] if p.get("pattern") == "callers_of"][-1]["results"]
        actual = {n.get("qualified_name", n.get("file_path", "") + "::" + n["name"])
                  .removeprefix(ROOT.as_posix() + "/")
                  for n in results if "/tests/" not in n.get("file_path", "")}
        expected = set(report["source_truth"][symbol])
        quality[case] = {"expected": len(expected), "found": len(actual & expected),
                         "missing": sorted(expected - actual), "extra": sorted(actual - expected)}
    report["quality"] = quality
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for case, methods in report["cases"].items():
        for method, samples in methods.items():
            if any(s["status"] != "ok" for s in samples):
                print(case, method, "ERROR", [s for s in samples if s["status"] != "ok"])
                continue
            print(case, method, {k: round(statistics.median(s[k] for s in samples), 4)
                                 for k in ("seconds", "retrieval_chars", "source_chars", "total_chars")})
    assert all(s["status"] == "ok" for methods in report["cases"].values()
               for samples in methods.values() for s in samples), "Failed probes are not savings"
    print("quality", quality)
    assert all(not row["missing"] and not row["extra"] for row in quality.values()), \
        "Graph accuracy differs from source; negative results remain in the JSON output"


if __name__ == "__main__":
    main()
