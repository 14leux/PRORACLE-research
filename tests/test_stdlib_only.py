"""PRORACLE has no third-party dependencies; any new one must be a deliberate, reviewed change.

This test fails if a module under ``proracle/`` or ``tests/`` imports anything outside the standard
library and the project itself. If a dependency is genuinely needed, justify it in the pull request,
check its licence, and update this test together with the dependency notices in the same change.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY = {"proracle", "tests"}


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class StandardLibraryOnlyTest(unittest.TestCase):
    def test_only_standard_library_and_first_party_imports(self) -> None:
        allowed = set(sys.stdlib_module_names) | FIRST_PARTY
        offenders = {}
        for folder in ("proracle", "tests"):
            for path in sorted((ROOT / folder).rglob("*.py")):
                extra = imported_roots(path) - allowed
                if extra:
                    offenders[str(path.relative_to(ROOT))] = sorted(extra)
        self.assertEqual(offenders, {}, "third-party imports need a reviewed, documented change")

    def test_the_check_would_catch_a_third_party_import(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text("import json\nimport requests\nfrom . import sibling\n", encoding="utf-8")
            roots = imported_roots(sample)
        self.assertEqual(roots - set(sys.stdlib_module_names) - FIRST_PARTY, {"requests"})


if __name__ == "__main__":
    unittest.main()
