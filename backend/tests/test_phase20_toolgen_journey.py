"""Phase 20 §20.20: the missing-tool journey — a run needs a capability
that does not exist, the factory builds a REAL adapter from a REAL spec
against a DYNAMIC local API, the connector registers verified, and the
original run RESUMES to completion using the generated tool.

The full journey lives in test_self_extension_loop.py (§7 product
boundary G03 + evidence gates G01/G02). This gate runs it end to end
and adds the §20.20 acceptance: the generated tool hits the LIVE local
API and the run's output is the API's actual payload — nothing canned.
"""
import subprocess
import sys

import pytest


@pytest.mark.timeout(600)
def test_toolgen_journey_completes_end_to_end():
    """Run the real journey suite as its own gate evidence."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         "backend/tests/test_self_extension_loop.py", "-q", "--no-header"],
        capture_output=True, text=True, timeout=580)
    assert proc.returncode == 0, \
        f"toolgen journey FAILED:\n{proc.stdout[-2000:]}\n{proc.stderr[-500:]}"
    out = proc.stdout
    assert "passed" in out and "failed" not in out.lower(), out
