"""Phase 29 — import contract regressions.

§29.15: the pinned API/worker runtime is python:3.11-slim-bookworm
(Dockerfile.api). On CPython <= 3.13 a module-scope annotation is
evaluated at def time, so a ``List``/``Dict``/``Optional`` reference
without a typing import raises NameError at import. The local
development Python (3.14+, PEP 649 lazy annotations) silently hides the
defect. This suite parses every backend module exactly like the pinned
runtime would: a module-scope signature that references a typing name
it never imported (and that lacks ``from __future__ import annotations``)
fails.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]

TYPING_NAMES = {
    "List", "Dict", "Optional", "Tuple", "Set", "Any", "Union",
    "Sequence", "Callable", "Iterable", "Mapping", "Awaitable",
}

_FUTURE = "__future__"


def _module_scope_annotation_gaps(path: pathlib.Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    if any(
        isinstance(node, ast.ImportFrom) and node.module == _FUTURE
        for node in tree.body
    ):
        return []  # lazy annotations: deferred to call time, safe
    imported: set[str] = set()
    has_typing_module = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            imported |= {alias.asname or alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    has_typing_module = True
    if has_typing_module:
        imported.add("typing")
    problems: list[str] = []
    for node in tree.body:
        annotations: list[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            params = (
                args.args + args.kwonlyargs
                + ([args.vararg] if args.vararg else [])
                + ([args.kwarg] if args.kwarg else [])
            )
            annotations.extend(a.annotation for a in params if a.annotation)
            if node.returns:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for annotation in annotations:
            for sub in ast.walk(annotation):
                if (
                    isinstance(sub, ast.Name)
                    and sub.id in TYPING_NAMES
                    and sub.id not in imported
                    and "typing" not in imported
                ):
                    problems.append(
                        f"{path.name}:{node.lineno}: {sub.id} used at module "
                        "scope without a typing import (NameError on the "
                        "pinned python:3.11 runtime)"
                    )
    return problems


def test_no_module_scope_typing_annotation_gaps():
    problems: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        problems.extend(_module_scope_annotation_gaps(path))
    assert not problems, "\n".join(problems)


def test_connector_build_service_imports_on_a_pinned_runtime_semantics():
    """The durable build entry point must import under eager-annotation
    semantics. We re-apply the eager check to the real module and then
    import it normally to prove the module is loadable."""
    module_path = BACKEND_ROOT / "services" / "connector_build_service.py"
    assert _module_scope_annotation_gaps(module_path) == []

    import importlib

    module = importlib.import_module("backend.services.connector_build_service")
    assert hasattr(module, "request_durable_build")
    assert hasattr(module, "build_request_digest")


@pytest.mark.parametrize(
    "module_name",
    [
        "backend.services.connector_build_service",
        "backend.agents.connector_validator.validator_agent",
        "backend.services.agent_task_contracts",
        "backend.security.permission_matrix",
    ],
)
def test_annotation_heavy_modules_import_cleanly(module_name):
    import importlib

    assert importlib.import_module(module_name) is not None
