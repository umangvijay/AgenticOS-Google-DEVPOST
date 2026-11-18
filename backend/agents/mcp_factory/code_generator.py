"""Candidate-code diagnostics (Phase 7 §7.10).

Purpose change: static analysis is an EARLY DIAGNOSTIC, not the execution
security boundary (§7.10 — Python behavior cannot be confined by a list of
forbidden imports). Returns STRUCTURED findings; a clean result never
substitutes for runtime isolation or protocol tests.

Useful checks: syntax validity, expected entrypoint, undeclared
dependencies, embedded credentials, dynamic installation, process/
filesystem capability requests, and protocol-output contamination.
"""
import ast
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

ALLOWED_IMPORTS = {
    "os", "json", "typing", "datetime", "httpx", "aiohttp", "mcp",
    "pydantic", "asyncio", "re", "urllib", "urllib.parse",
}

_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
_FORBIDDEN_ATTR_CALLS = {"system", "popen"}
# Dynamic package installation in generated code is a dependency-policy
# violation (§7.9: never install during ordinary tool invocation).
_INSTALL_CALLS = {"install", "main"}


def analyze_candidate(code: str, *, expected_entrypoint: str = "app",
                      declared_dependencies: Optional[set] = None) -> Dict[str, List[str]]:
    """§7.10: structured diagnostic findings. Never raises for code issues —
    problems appear in the findings lists. A clean result is diagnostic
    only."""
    findings: Dict[str, List[str]] = {
        "syntax_errors": [],
        "unsupported_imports": [],
        "forbidden_capability_requests": [],
        "unexpected_entrypoints": [],
        "suspicious_dynamic_behavior": [],
        "dependency_requirements": [],
    }
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        findings["syntax_errors"].append(f"line {e.lineno}: {e.msg}")
        return findings

    declared = declared_dependencies or set()
    has_app = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    findings["unsupported_imports"].append(alias.name)
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root and root not in ALLOWED_IMPORTS:
                findings["unsupported_imports"].append(node.module or "")
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _FORBIDDEN_CALLS or name in _FORBIDDEN_ATTR_CALLS:
                findings["forbidden_capability_requests"].append(name or "?")
            if name in _INSTALL_CALLS and isinstance(func, ast.Attribute) and \
                    isinstance(func.value, ast.Attribute):
                findings["suspicious_dynamic_behavior"].append(
                    f"dynamic package call: {name}()")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == expected_entrypoint:
                    has_app = True

    if not has_app:
        findings["unexpected_entrypoints"].append(
            f"expected module-level {expected_entrypoint!r} not found")

    # Embedded credentials (heuristic — real enforcement is the broker, §7.14).
    for token in ("sk-", "-----BEGIN", "AKIA", "password=", "Bearer "):
        if token in code:
            findings["suspicious_dynamic_behavior"].append(
                f"possible embedded credential pattern: {token!r}")

    return findings


def candidate_is_clean(findings: Dict[str, List[str]]) -> bool:
    """§7.10 acceptance: a clean diagnostic result is necessary but NEVER
    sufficient — the caller must still run runtime isolation + protocol
    tests. This predicate only reports 'no static findings'."""
    return not any(findings.values())


class CodeGenerator:
    """§7.10: static_security_check is retained for callers but documented as
    the early diagnostic — the structured `analyze_candidate` is preferred."""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    def static_security_check(self, code: str) -> bool:
        findings = analyze_candidate(code)
        clean = candidate_is_clean(findings)
        if not clean:
            logger.warning("Candidate diagnostics: %s",
                           {k: v for k, v in findings.items() if v})
        return clean
