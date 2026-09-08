"""Production callers reach flush only through its declared public surface."""

from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest


CODEX_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = CODEX_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import flush  # noqa: E402


# Production callers of flush. Test modules are exempt by ADR-0001: the contract
# binds shipped runtime code only. flush.py never imports itself, so globbing the
# whole script directory costs nothing and covers future callers automatically.
PRODUCTION_CALLERS = tuple(sorted(SCRIPTS_DIR.glob("*.py")))

# Residual private references deliberately not retired. Visible on purpose: a file
# absent from this map must be clean, and an entry that stops being used fails
# test_allowlist_has_no_stale_entries. Growing this map needs a reviewed reason,
# never a quiet append. Empty since T08 moved _managed_hook_input to its owner.
PRIVATE_ALLOWLIST: dict[str, frozenset[str]] = {}


def _flush_references(path: Path) -> tuple[set[str], set[str]]:
    """flush names this file pulls in, split into (private, public)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_aliases: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "flush":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "flush":
                names.update(alias.name for alias in node.names)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
        ):
            names.add(node.attr)
    private = {name for name in names if name.startswith("_")}
    return private, names - private


class FlushPublicSurfaceTests(unittest.TestCase):
    def test_production_callers_use_no_private_flush_names(self) -> None:
        violations: dict[str, list[str]] = {}
        for path in PRODUCTION_CALLERS:
            allowed = PRIVATE_ALLOWLIST.get(path.name, frozenset())
            residual = sorted(_flush_references(path)[0] - allowed)
            if residual:
                violations[path.name] = residual
        self.assertEqual(
            violations,
            {},
            "T07_PRIVATE_FLUSH_SURFACE: "
            + f"{sum(len(names) for names in violations.values())} references "
            + repr(violations),
        )

    def test_allowlist_has_no_stale_entries(self) -> None:
        by_name = {path.name: path for path in PRODUCTION_CALLERS}
        for file_name, allowed in PRIVATE_ALLOWLIST.items():
            path = by_name.get(file_name)
            self.assertIsNotNone(path, f"T07_ALLOWLIST_UNKNOWN_FILE: {file_name}")
            stale = sorted(allowed - _flush_references(path)[0])
            self.assertEqual(stale, [], f"T07_ALLOWLIST_STALE: {file_name}")

    def test_public_surface_is_declared_and_importable(self) -> None:
        declared = getattr(flush, "__all__", None)
        self.assertIsNotNone(declared, "T07_ALL_MISSING")
        self.assertEqual(sorted(declared), list(declared), "T07_ALL_NOT_SORTED")
        for name in declared:
            self.assertFalse(name.startswith("_"), f"T07_ALL_PRIVATE_NAME: {name}")
            self.assertTrue(hasattr(flush, name), f"T07_ALL_MISSING_ATTRIBUTE: {name}")

    def test_summary_and_turn_budget_owners_are_declared(self) -> None:
        """T12 bilinçli yüzey eklemesi: özet değer tipi + tek kırpma politikası."""
        declared = set(getattr(flush, "__all__", ()))
        self.assertLessEqual(
            {"SessionSummary", "chunk_turns", "fit_turns"},
            declared,
            "T12_SUMMARY_SURFACE_MISSING",
        )

    def test_production_callers_only_use_declared_public_names(self) -> None:
        declared = set(getattr(flush, "__all__", ()))
        undeclared: dict[str, list[str]] = {}
        for path in PRODUCTION_CALLERS:
            residual = sorted(_flush_references(path)[1] - declared)
            if residual:
                undeclared[path.name] = residual
        self.assertEqual(undeclared, {}, "T07_PUBLIC_NAME_NOT_IN_ALL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
