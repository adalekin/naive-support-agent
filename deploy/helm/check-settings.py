"""Fails when the rendered chart leaves out a setting the assistant cannot start without.

    helm template assistant deploy/helm/naive-support-agent -f <values> | python deploy/helm/check-settings.py

The required settings are the fields of ``Settings`` without a default, read from the source so
that the check needs nothing but the standard library.
"""

import ast
import re
import sys
from pathlib import Path

SETTINGS = Path(__file__).parents[2] / "server/internal/config/installation.py"


def required_settings() -> set[str]:
    tree = ast.parse(SETTINGS.read_text(encoding="utf-8"))
    settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings")
    required = set()
    for node in settings.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id.isupper() and (node.value is None or _field_without_default(node.value)):
            required.add(node.target.id)
    return required


def _field_without_default(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "Field"
        and not value.args
        and all(keyword.arg not in {"default", "default_factory"} for keyword in value.keywords)
    )


def main() -> None:
    rendered = set(re.findall(r"^\s*- name: ([A-Z][A-Z0-9_]*)\s*$", sys.stdin.read(), flags=re.MULTILINE))
    missing = sorted(required_settings() - rendered)
    if missing:
        sys.exit(f"the chart does not set: {', '.join(missing)}")
    print(f"the chart sets every required setting ({len(required_settings())})")


if __name__ == "__main__":
    main()
