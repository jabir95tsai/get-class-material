"""Check core's static imports without loading application modules."""
import ast
import sys
import unittest
from collections.abc import Iterator
from importlib.util import resolve_name
from pathlib import Path


PACKAGE = "ntu_cool_materials"
CORE = f"{PACKAGE}.core"
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def forbidden_imports(source: str, package: str) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parent = resolve_name("." * node.level + (node.module or ""), package)
            names = [f"{parent}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            if name == CORE or name.startswith(CORE + "."):
                continue
            if name.split(".", 1)[0] not in sys.stdlib_module_names:
                yield node.lineno, name


class CoreImportTests(unittest.TestCase):
    def test_core_depends_only_on_core_and_the_standard_library(self):
        violations = []
        paths = sorted((SRC / PACKAGE / "core").rglob("*.py"))
        self.assertTrue(paths, "No core source files found")
        for path in paths:
            package = ".".join(path.parent.relative_to(SRC).parts)
            for line, name in forbidden_imports(path.read_text(encoding="utf-8"), package):
                violations.append(f"{path.relative_to(ROOT)}:{line} imports {name}")
        self.assertFalse(violations, "\n".join(violations))

    def test_resolves_absolute_relative_and_local_imports(self):
        source = (
            "from ntu_cool_materials import core\n"
            "from .. import naming\n"
            "from collections.abc import Mapping\n"
            "def choose():\n"
            "    from ... import cli\n"
            "    import ntu_cool_materials.storage as store\n"
            "    import requests\n"
        )
        self.assertEqual(list(forbidden_imports(source, CORE + ".nested")), [
            (5, PACKAGE + ".cli"), (6, PACKAGE + ".storage"), (7, "requests"),
        ])
