"""Mechanical coding standards that ruff cannot express."""

import ast
from pathlib import Path

from recon.schema import DECISIONS

ROOT = Path(__file__).resolve().parents[1]
ENGINE = [*(ROOT / "src" / "recon").rglob("*.py"), *(ROOT / "recon_app").rglob("*.py"),
          *(ROOT / "app_pages").rglob("*.py"), ROOT / "app.py"]
# schema.py defines the names; the generators write ground-truth labels as data.
EXEMPT = {"schema.py", "channeldata.py", "mockdata.py", "olistdata.py"}


def _decision_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exported = {id(node) for assign in ast.walk(tree) if isinstance(assign, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "__all__" for t in assign.targets)
                for node in ast.walk(assign.value)}
    return [f"{path.relative_to(ROOT)}:{node.lineno} {node.value}" for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value in DECISIONS and id(node) not in exported]


def test_decision_names_are_imported_from_schema():
    found = [hit for path in ENGINE if path.name not in EXEMPT for hit in _decision_literals(path)]
    assert not found, "use the constants in recon.schema:\n" + "\n".join(found)
