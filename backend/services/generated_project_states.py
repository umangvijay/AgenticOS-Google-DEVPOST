"""Generated-project execution states (Phase 17 §17.17) — honest states
for generated code. The managed runner backend is UNAVAILABLE (§7): the
runner stays explicitly unavailable; generated code is never executed
inside the API process."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

PROJECT_STATES = ("source_generated", "manifest_validated", "build_not_executed",
                  "build_passed", "build_failed", "tests_passed", "tests_failed",
                  "runtime_smoke_passed", "runtime_smoke_failed",
                  "execution_unavailable")


def project_execution_record(*, source_digest: str,
                             dependency_lock_digest: str,
                             runner_id: Optional[str] = None,
                             runner_version: Optional[str] = None,
                             commands: Optional[List[Dict[str, Any]]] = None,
                             declared_test_scope: str = "",
                             ) -> Dict[str, Any]:
    """The build/test evidence record (§17.17) — complete or HONESTLY
    absent: without a runner the state is execution_unavailable, and a
    project can be downloaded as source but never badged tested/runnable."""
    executed = runner_id is not None
    return {
        "source_digest": source_digest,
        "dependency_lock_digest": dependency_lock_digest,
        "runner_id": runner_id,
        "runner_version": runner_version,
        "commands": commands or [],
        "declared_test_scope": declared_test_scope,
        "state": ("build_not_executed" if not executed
                  else "execution_unavailable"),
        "badges": [],
        "note": ("an unexecuted project downloads as source but CANNOT "
                 "receive a tested/runnable badge (§17.17)"),
    }


def award_badges(record: Dict[str, Any]) -> List[str]:
    """Badges only from EXECUTED evidence — the current unproven runner
    means no badges, no matter how polished the source looks."""
    if record.get("state") in ("build_not_executed", "execution_unavailable"):
        return []
    badges = []
    if record.get("state") == "build_passed":
        badges.append("build-passed")
    if record.get("state") == "tests_passed":
        badges.append("tests-passed")
    return badges
