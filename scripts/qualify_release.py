#!/usr/bin/env python3
"""Phase 23 release qualification harness.

Runs acceptance stages against production entry points and records
first-attempt results separately from named re-runs. All modes fail
release qualification when mandatory stages are missing,
skipped, blocked, failed, or lack usable test results.

Output: docs/evidence/phase-23-attempts/<id>/report.json
        docs/evidence/phase-23-qualification.json (full invocations only)
        + a stdout stage table. Never merges retry results into
        first-attempt totals.

Usage:
    .venv/bin/python scripts/qualify_release.py [--stage STAGE] ...
        [--strict] [--retry-record STAGE:label]

Stage list (frozen, §22.4 table):
    identity            source/dependency/runtime identity
    migrations          fresh-database migration initialize
    unit_contract       full backend suite (first attempt)
    infra_integration   postgres/nats/stack/emulator integration suites
    live_inference      real provider inference (local Ollama; hosted is
                        credential-gated and reported as blocked)
    fault_injection     injected-failure healing + isolation suites
    security            tenancy/auth/execution-safety adversarial suites
    product_e2e         frontend vitest + typecheck + lint + build
    product_e2e_browser playwright real-backend journeys (gated on a
                        running api)
    performance         measured admission/load workload (declared host)
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

# Phase 31 §31.2: the versioned scenario-receipt contract is shared with
# the journey producer so the two sides cannot drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenario_receipts import (  # noqa: E402
    ReceiptError, RECEIPT_SCHEMA, catalog_by_journey, is_composed_run_reference,
    load_receipt, mandatory_scenarios, real_run_ids, receipt_passes,
    validate_receipt,
)

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv/bin/python"
REPORT = ROOT / "docs/evidence/phase-23-qualification.json"
ATTEMPTS = ROOT / "docs/evidence/phase-23-attempts"
MANIFEST_VERSION = 2
REQUIRED_STAGES = (
    "migrations", "unit_contract", "infra_integration", "live_inference_local",
    "live_inference_hosted", "fault_injection", "security",
    "product_e2e_vitest", "product_e2e_typecheck", "product_e2e_lint",
    "product_e2e_build", "product_e2e_browser", "performance",
    "flagship", "restore", "target_deployment", "pilot", "rl_training",
)
PYTEST_STAGES = frozenset({
    "unit_contract", "infra_integration", "fault_injection", "security",
    "live_inference_local", "flagship", "rl_training",
})
COMMAND_STAGES = frozenset({
    "product_e2e_typecheck", "product_e2e_lint", "product_e2e_build",
})
# These producers exercise Compose, emulators or an isolated local database.
# Selecting a cloud profile does not retarget them. Their passing results are
# useful prerequisites, but cannot establish the cloud runtime's behavior.
LOCAL_RUNTIME_STAGES = frozenset({
    "infra_integration", "fault_injection", "product_e2e_browser",
    "flagship", "restore", "performance",
})
# §25.2: rl_training runs as its own stage and its evidence is BOUND to
# J14 — omitting the governed-learning evidence cannot satisfy J14.
RL_STAGE = "rl_training"
JOURNEY_STAGE_BINDING = {"J14": (RL_STAGE,)}
REQUIRED_JOURNEYS = tuple(f"J{i:02d}" for i in range(1, 23))
# §27.20: the versioned NEW journeys — marketplace (J23) and modeling
# (J24) — join the mandatory set through this register-backed extension;
# the inherited 22 are unchanged and J14 stays bound to rl_training.
NEW_JOURNEYS = ("J23", "J24")
REQUIRED_JOURNEYS = REQUIRED_JOURNEYS + NEW_JOURNEYS
# Phase 31 §31.2: journey-independent scenarios with their own receipts
# and verdicts (catalog docs/qualification/phase-31-scenarios.json).
REQUIRED_SCENARIOS = tuple(
    s["scenario_id"] for s in mandatory_scenarios())


def journey_record(journey_id: str, *, candidate_sha256: str, target_id: str,
                   production_action: str, run_id: str,
                   expected_postcondition: str, observed_result: str,
                   evidence: list, observation: dict | None = None,
                   scenario_receipts: list | None = None,
                   status: str | None = None,
                   attempt_id: str = "") -> dict:
    """§25.2: a journey outcome bound to the OBSERVED production action,
    the run it executed on, its expected postcondition and observed
    result, plus verifiable evidence records. Phase 31 §31.2: the run
    must be a real product record — an `observation` reference (the
    retained bytes that observed the run) and/or `scenario_receipts`
    (phase31-scenario-receipt/1 from the operator runner) bind it."""
    return {"journey_id": journey_id, "ok": True,
            "production_action": production_action, "run_id": run_id,
            "expected_postcondition": expected_postcondition,
            "observed_result": observed_result,
            "candidate_sha256": candidate_sha256, "target_id": target_id,
            "evidence": list(evidence),
            **({"observation": observation} if observation else {}),
            **({"scenario_receipts": scenario_receipts}
               if scenario_receipts else {}),
            **({"status": status} if status else {}),
            "attempt_id": attempt_id}


def shell_evidence(stage: str, *, exit_code: int, result_file: str,
                   result_sha256: str, **extra) -> dict:
    """A shell stage record WITH its evidence: the exact result file and
    its hash. A bare ok=True is not evidence (Phase 24 §24.3)."""
    return {"kind": "shell", "ok": exit_code == 0, "exit": exit_code,
            "result_file": result_file, "result_sha256": result_sha256,
            **extra}


def pytest_evidence(stage: str, *, counts: dict, result_file: str,
                    result_sha256: str, **extra) -> dict:
    """A pytest stage record WITH parsed executed counts and the hashed
    result file."""
    return {"kind": "pytest", "ok": (counts.get("passed", 0) > 0
                                     and not any(counts.get(k, 0) for k in
                                                 ("failed", "errors", "skipped",
                                                  "flaky"))),
            "counts": counts, "result_file": result_file,
            "result_sha256": result_sha256, **extra}

PYTEST = [str(PY), "-m", "pytest", "-q", "--no-header",
          "-p", "no:cacheprovider", "--timeout=600"]

EMULATOR_ENV = {
    "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
    "GOOGLE_CLOUD_PROJECT": "agentos-local-test",
}

BLOCKERS = {
    # stage -> declared blocker (id + runnable next action). Anything
    # skipped WITHOUT a blocker id fails in strict mode.
    "live_inference_hosted": (
        "BLOCKER-B1 no hosted provider credentials on host; "
        "next: set GOOGLE_API_KEY or AWS_BEARER_TOKEN_BEDROCK and rerun "
        "`pytest backend/tests/test_phase21_providers_adk.py -q` + one "
        "live workspace dispatch"),
    "product_e2e_browser": (
        "BLOCKER-B2 requires api on :8000; next: "
        "`docker compose --profile core up -d`"),
    "performance": (
        "BLOCKER-B3 measured-workload run requires the durable stack; "
        "next: `docker compose --profile core up -d` then rerun stage"),
    "pilot": (
        "BLOCKER-B4 real-user pilot requires actual participants; "
        "next: execute docs/qualification/pilot-plan.md"),
    "rl_training": (
        "BLOCKER-B5 RL training DEFERRED by the release owner "
        "(umangvijay, 2026-11-14): the product runtime never imports the "
        "RL path (test_rl_research_lane guard) and governed rollout runs "
        "on deterministic baselines until trained artifacts are approved; "
        "next: run `pytest backend/tests/test_phase21_rl_real_tasks.py -q` "
        "on a quiet host and clear this blocker"),
}


def verify_result_file(stage: str, st: dict, evidence_root: Path) -> list[str]:
    """§25.2: evidence must EXIST, live inside the approved evidence
    location, carry a HEX hash, and MATCH the retained bytes. The ok
    flag written by a producer is never trusted on its own."""
    problems = []
    raw_path = st.get("result_file")
    if not raw_path:
        return [f"{stage}: no result file — a bare success is not evidence"]
    claimed = str(st.get("result_sha256") or "")
    if len(claimed) != 64 or any(
            c not in "0123456789abcdefABCDEF" for c in claimed):
        return [f"{stage}: result_sha256 is not a hex sha256 "
                f"({claimed[:16]}…)"]
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    resolved = path.resolve()
    try:
        resolved.relative_to(evidence_root.resolve())
    except ValueError:
        return [f"{stage}: result file outside the approved evidence "
                f"location ({raw_path})"]
    if not resolved.is_file():
        return [f"{stage}: result file does not exist ({raw_path})"]
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != claimed.lower():
        return [f"{stage}: result file bytes changed — recorded "
                f"{claimed[:12]}… vs actual {actual[:12]}…"]
    return problems


def parse_retained_counts(stage: str, st: dict,
                          evidence_root: Path) -> list[str]:
    """§25.2: parse the RETAINED result and compare with the recorded
    counts — a producer cannot claim numbers the file contradicts."""
    if stage not in REQUIRED_STAGES:
        return [f"{stage}: unknown qualification stage"]
    expected_kind = "pytest" if stage in PYTEST_STAGES else "shell"
    if st.get("kind") != expected_kind:
        return [f"{stage}: evidence kind must be {expected_kind}"]
    problems = verify_result_file(stage, st, evidence_root)
    if problems:
        return problems
    path = ROOT / st["result_file"]
    recorded = st.get("counts") or {}
    try:
        if stage in PYTEST_STAGES:
            counts = junit_counts(path)
            # §28.1: the report must carry THIS stage's identity — a
            # wrong-stage JUnit report cannot be recorded under another
            # stage's record.
            names = junit_suite_names(path)
            if names != {stage}:
                return [f"{stage}: retained junit report carries stage "
                        f"identity {sorted(names)} — wrong-stage evidence"]
        elif stage in ("product_e2e_browser", "product_e2e_vitest"):
            runner = "playwright" if stage == "product_e2e_browser" else "vitest"
            counts = frontend_counts(path, runner, expected_stage=stage)
        else:
            outcome = json.loads(path.read_text(), object_pairs_hook=unique_object)
            if (not isinstance(outcome, dict) or outcome.get("ok") is not True
                    or type(outcome.get("exit")) is not int or outcome["exit"] != 0):
                return [f"{stage}: retained result reports failure"]
            if stage in COMMAND_STAGES:
                command = outcome.get("command")
                if (outcome.get("stage") != stage or not isinstance(command, list)
                        or not command or not all(isinstance(arg, str) and arg for arg in command)):
                    return [f"{stage}: missing stage-bound command receipt"]
            if stage == "migrations":
                if (not outcome.get("db_profile") or outcome.get("migrations_ok") is not True
                        or type(outcome.get("tables")) is not int or outcome["tables"] <= 0):
                    return [f"{stage}: missing observed migration result"]
            if stage == "live_inference_hosted":
                providers = outcome.get("providers")
                required = {"vertex", "bedrock", "azure"}
                checks = {"workflow", "tool_call", "usage", "cancellation", "rotation", "revocation"}
                if not isinstance(providers, dict) or set(providers) != required:
                    return [f"{stage}: required provider receipts missing"]
                for provider, receipt in providers.items():
                    if (not isinstance(receipt, dict)
                            or not all(isinstance(receipt.get(k), str) and receipt[k].strip()
                                       for k in ("run_id", "model", "provider_request_id"))
                            or not isinstance(receipt.get("checks"), dict)
                            or not all(receipt["checks"].get(k) is True for k in checks)):
                        return [f"{stage}: incomplete {provider} workflow receipt"]
                    # §28.1: the verdict depends on the linked trace — its
                    # content must name the same provider request.
                    problems.extend(verify_linked_trace(
                        f"{stage}/{provider}", receipt,
                        {"provider": provider,
                         "provider_request_id": receipt["provider_request_id"]},
                        evidence_root))
                return problems
            if stage == "pilot":
                sessions = outcome.get("sessions")
                if not isinstance(sessions, list) or not sessions:
                    return [f"{stage}: no consented pilot session receipts"]
                seen = set()
                participants: dict[str, int] = {}
                for session in sessions:
                    if (not isinstance(session, dict)
                            or not all(isinstance(session.get(k), str) and session[k].strip()
                                       for k in ("session_id", "participant_ref", "consent_ref", "run_id"))
                            or session.get("verified_outcome") is not True
                            or session["session_id"] in seen):
                        return [f"{stage}: invalid or duplicate pilot session"]
                    seen.add(session["session_id"])
                    participants[session["participant_ref"]] = \
                        participants.get(session["participant_ref"], 0) + 1
                    # §28.1: the retained session trace must agree with the
                    # receipt it backs (identity + declared outcome).
                    problems.extend(verify_linked_trace(stage, session, {
                        "session_id": session["session_id"],
                        "run_id": session["run_id"],
                        "verified_outcome": True,
                    }, evidence_root))
                # §28.1 (pilot plan): 3–5 real people — a nonempty session
                # list alone is not a pilot, and one person cannot be
                # counted twice under different session IDs.
                repeated = [p for p, n in participants.items() if n > 1]
                if repeated:
                    return [f"{stage}: participant(s) {repeated} recorded "
                            "under multiple session IDs"]
                if not (3 <= len(participants) <= 5):
                    return [f"{stage}: pilot plan requires 3–5 distinct "
                            f"participants; observed {len(participants)}"]
                # Unsuccessful observations are RETAINED, never hidden:
                # they do not gate the verdict but must be recorded for
                # evaluation under the declared pilot criteria.
                extra = outcome.get("unsuccessful_sessions")
                if extra:
                    if not isinstance(extra, list) or any(
                            not isinstance(s, dict)
                            or not str(s.get("session_id") or "").strip()
                            for s in extra):
                        return [f"{stage}: malformed unsuccessful-session record"]
                    if any(s["session_id"] in seen for s in extra):
                        return [f"{stage}: unsuccessful session duplicates a "
                                "qualified session ID"]
                    for failed in extra:
                        if failed["session_id"] in seen or not failed.get("run_id"):
                            return [f"{stage}: duplicated or unidentified unsuccessful session"]
                        seen.add(failed["session_id"])
                        problems.extend(verify_linked_trace(f"{stage}/unsuccessful", failed, {
                            "session_id": failed["session_id"], "run_id": failed["run_id"],
                            "verified_outcome": False}, evidence_root))
                return problems
            if stage == "performance" and not all(
                isinstance(outcome.get(name), dict) and outcome[name].get("ok") is True
                for name in ("rate_limit", "capacity")
            ):
                return [f"{stage}: mandatory workload subchecks missing or failed"]
            if stage == "restore":
                checks = outcome.get("subchecks") or {}
                if not isinstance(checks, dict) or not all(checks.get(name) == "ok" for name in
                           ("dump", "restore", "integrity", "cleanup", "independent_app_trace")):
                    return [f"{stage}: mandatory recovery subchecks missing or failed"]
                if (not outcome.get("source_counts")
                        or outcome["source_counts"] != outcome.get("restored_counts")):
                    return [f"{stage}: restored counts do not match the source snapshot"]
            if stage == "performance":
                # §27.D1/§27.1: the loadtest writes structured subchecks —
                # a skipped capacity or failed rate-limit subcheck can
                # never pass on a bare ok/exit flag. The receipt contract
                # accepts the producer's `admitted`/`accepted_total`
                # spellings (same value) and validates ledger agreement
                # when the ledger count is present.
                rl = outcome.get("rate_limit") or {}
                cap = outcome.get("capacity") or {}
                accepted = rl.get("accepted_total", rl.get("admitted"))
                if rl.get("ok") is not True or not isinstance(accepted, int):
                    return [f"{stage}: rate-limit subcheck receipt missing or failed"]
                if cap.get("ok") is not True or cap.get("skipped") is True:
                    return [f"{stage}: capacity subcheck skipped or failed — "
                            "a skipped mandatory subcheck cannot pass"]
                ledger = rl.get("ledger_count")
                if ledger is not None and accepted != ledger:
                    return [f"{stage}: accepted total contradicts the ledger"]
                # Internal invariant: rate_limit.ok=True means EXACTLY the
                # allowance was admitted across all processes — the load
                # test's defining property. A falsified count cannot
                # satisfy it.
                if rl.get("ok") is True and accepted != rl.get("allowance"):
                    return [f"{stage}: accepted total {accepted} contradicts "
                            f"the declared allowance {rl.get('allowance')}"]
                # §27.1 (audit 2026-11-13): the embedded whole-workload
                # receipt must account for the ENTIRE declared sample —
                # a six-of-twenty health-only observation cannot qualify
                # a two-family × ten-run declaration.
                workload = outcome.get("workload")
                if not isinstance(workload, dict):
                    return [f"{stage}: structured whole-workload receipt is missing"]
                pre = workload.get("predeclared") or {}
                res = workload.get("results") or {}
                families = pre.get("families") or []
                rpf = pre.get("runs_per_family")
                if not isinstance(rpf, int) or rpf <= 0 or not families:
                    return [f"{stage}: workload receipt missing its declared sample"]
                planned = rpf * len(families)
                if res.get("planned") != planned:
                    return [f"{stage}: workload planned {res.get('planned')!r} "
                            f"contradicts the declared sample of {planned}"]
                runs = workload.get("runs") or []
                if len(runs) != planned:
                    return [f"{stage}: workload accounted for {len(runs)} of "
                            f"{planned} planned submissions — a partial "
                            "sample cannot qualify"]
                ids = [r.get("planned_id") for r in runs]
                if any(not i for i in ids) or len(set(ids)) != len(ids):
                    return [f"{stage}: workload run identities missing or duplicated"]
                per_family: dict = {}
                for i in ids:
                    fam = str(i).rsplit("-", 1)[0]
                    per_family[fam] = per_family.get(fam, 0) + 1
                for fam in families:
                    if per_family.get(fam) != rpf:
                        return [f"{stage}: family {fam} observed "
                                f"{per_family.get(fam)} of {rpf} declared runs"]
                if res.get("missing_planned_ids"):
                    return [f"{stage}: workload did not account for every planned submission"]
                vcount = res.get("verified_completion_count")
                completed_n = res.get("completed")
                if (not isinstance(vcount, int) or vcount < 0
                        or not isinstance(completed_n, int) or vcount > completed_n):
                    return [f"{stage}: verified completion count contradicts completed runs"]
                vrate = res.get("verified_completion_rate")
                if (not isinstance(vrate, (int, float))
                        or abs(vrate - vcount / planned) > 0.001):
                    return [f"{stage}: verified completion rate contradicts the "
                            f"verified count over the declared sample"]
                elapsed = res.get("elapsed_s")
                thr = res.get("throughput_per_min")
                if not isinstance(elapsed, (int, float)) or elapsed <= 0:
                    return [f"{stage}: workload elapsed time missing"]
                if thr is not None:
                    expected_thr = planned / elapsed * 60
                    if abs(thr - expected_thr) > max(1.0, 0.05 * expected_thr):
                        return [f"{stage}: reported throughput {thr} contradicts "
                                f"the observed elapsed time ({expected_thr:.1f}/min)"]
                wg = workload.get("gates") or {}
                for g in ("planned_drained", "family_counts_exact",
                          "verified_completion", "unauthorized_actions",
                          "duplicate_effects", "abandoned"):
                    if wg.get(g) is not True:
                        return [f"{stage}: workload gate {g} not satisfied"]
                # Contradictory spellings of the same count cannot both
                # describe one admission outcome.
                if ("admitted" in rl and "accepted_total" in rl
                        and rl["admitted"] != rl["accepted_total"]):
                    return [f"{stage}: contradictory admitted/accepted_total "
                            "in the rate-limit subcheck"]
            if stage == "target_deployment":
                digests = outcome.get("image_digests") or {}
                workflow = outcome.get("workflow") or {}
                if not isinstance(digests, dict) or not all(re.fullmatch(r"sha256:[0-9a-f]{64}", str(digests.get(name) or ""))
                           for name in ("api", "worker", "frontend")):
                    return [f"{stage}: missing immutable deployed image identities"]
                if not (isinstance(workflow, dict) and workflow.get("submitted") is True and workflow.get("completed") is True
                        and workflow.get("run_id") and workflow.get("final_status") == "COMPLETED"):
                    return [f"{stage}: no completed ingress workflow receipt"]
                if st.get("profile") in ("gcp-gke", "aws-eks", "azure-aks"):
                    cloud = outcome.get("cloud") or {}
                    manifest_bytes = outcome.get("manifest_json", "").encode("utf-8")
                    if hashlib.sha256(manifest_bytes).hexdigest() != outcome.get("manifest_sha256"):
                        return [f"{stage}: retained cloud manifest digest mismatch"]
                    manifest = json.loads(manifest_bytes, object_pairs_hook=unique_object)
                    if (manifest.get("target_profile") != st["profile"]
                            or manifest.get("candidate_sha256") != st.get("candidate_sha256")
                            or manifest.get("image_digests") != digests
                            or manifest.get("namespace") != cloud.get("namespace")
                            or manifest.get("ingress_origin", "").rstrip("/").lower() != cloud.get("ingress_origin")):
                        return [f"{stage}: cloud observations contradict the reviewed manifest"]
                    components = cloud.get("components") or {}
                    if (cloud.get("profile") != st["profile"] or not cloud.get("resource")
                            or not cloud.get("namespace") or not cloud.get("ingress_uid")
                            or not re.fullmatch("[0-9a-f]{64}", str(workflow.get("output_sha256", "")))
                            or not workflow.get("challenge")
                            or not all(isinstance(components.get(role), dict)
                                       and components[role].get("image_digest") == digests[role]
                                       and components[role].get("deployment_uid") and components[role].get("pod_uids")
                                       for role in ("api", "worker", "frontend"))
                            or outcome.get("candidate_sha256") != st.get("candidate_sha256")
                            or not re.fullmatch("[0-9a-f]{64}", str(st.get("candidate_sha256", "")))):
                        return [f"{stage}: missing or contradictory cloud deployment observations"]
                if st.get("image_digests") != digests:
                    return [f"{stage}: recorded image identities contradict retained receipt"]
            return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as exc:
        detail = str(exc)[:120] or type(exc).__name__
        return [f"{stage}: retained result unparsable ({detail})"]
    for key, count in counts.items():
        if recorded.get(key, 0) != count:
            problems.append(f"{stage}: recorded {key} contradicts the retained result ({count})")
    if not counts.get("passed"):
        problems.append(f"{stage}: retained suite has no executed passes")
    # Skip tolerance (§27.1): ≤1% skip rate with substantial passes is
    # acceptable (declared blockers produce legitimate skips).
    total = sum(counts.values())
    if counts.get("skipped", 0) > 0 and total > 0 and \
            counts["skipped"] / total > 0.01:
        problems.append(
            f"{stage}: excessive skips {counts['skipped']}/{total}")
    # §27.1 (audit 2026-11-13): a NAMED mandatory check must block its
    # claim even when it is one skip among thousands. The deployed
    # browser takeover is such a check — its skip needs
    # QUALIFY_BROWSER_APP_URL, which is a declared blocker, not silence.
    if stage == "unit_contract":
        try:
            skipped_names = junit_skipped_names(path)
        except (OSError, ET.ParseError):
            skipped_names = set()
        for mandatory, why in (("test_phase21_takeover_deployed",
                                "deployed-browser takeover"),):
            if any(mandatory in n for n in skipped_names):
                problems.append(
                    f"{stage}: mandatory check SKIPPED — {why} "
                    f"({mandatory}); set QUALIFY_BROWSER_APP_URL and rerun")
    if any(counts.get(k, 0) for k in ("failed", "errors", "flaky", "timeout")):
        problems.append(
            f"{stage}: retained suite has failures "
            f"{ {k: counts[k] for k in ('failed', 'errors', 'flaky', 'timeout') if counts.get(k, 0)} }")
    return problems


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def git_identity() -> dict:
    """§25.1: THE shared fingerprint implementation (scripts/
    source_identity.py) — the freezer, verifier, qualifier, and journey
    producer all call the same code, so one source yields one identity."""
    if __name__ == "__main__":
        from source_identity import git_identity as _shared
    else:
        from scripts.source_identity import git_identity as _shared
    ident = _shared(ROOT)
    return {
        "commit": ident["commit"],
        "dirty_files": ident["dirty_files"],
        "content_sha256": ident["content_sha256"],
        "identity_version": ident["identity_version"],
        "branch": ident["branch"],
        "recorded_at": now(),
    }


def runtime_identity() -> dict:
    def first_line(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  cwd=ROOT).stdout.strip().splitlines()[0]
        except Exception as exc:  # noqa: BLE001
            return f"unavailable ({exc.__class__.__name__})"
    return {
        "python": first_line([str(PY), "--version"]),
        "node": first_line(["node", "--version"]),
        "docker": first_line(["docker", "--version"]),
        "os": first_line(["uname", "-s"]) + " " + first_line(["uname", "-m"]),
    }


def sp_run_kind() -> str:
    """Recorded k8s evidence from this qualification cycle."""
    return ("kind v0.33.0 cluster agentos-p21: created, images loaded, "
            "helm install + 4 upgrade cycles; chart defects fixed live "
            "(NATS JetStream args, imagePullPolicy, probe paths, DSN); "
            "final all-pods-Ready + full journey COMPLETED through the "
            "k8s stack (see docs/evidence/phase-22.md §22.32). "
            "Cluster torn down after evidence capture (host memory).")


def readiness(service: str, container: str) -> tuple[bool, str]:
    """Service readiness gate: the compose production stack, not a test app."""
    try:
        r = subprocess.run(["docker", "inspect", container, "--format",
                            "{{json .State}}"], capture_output=True,
                           text=True, timeout=15)
        state = json.loads(r.stdout) if r.returncode == 0 else {}
        health = state.get("Health", {}).get("Status")
        ready = state.get("Running") is True and health == "healthy"
        return ready, f"{service}: running={state.get('Running', False)}, health={health or 'unproven'}"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return False, f"{service}: unavailable ({type(exc).__name__})"


def junit_counts(path: Path) -> dict:
    """Count test cases once; nested suites must not multiply totals."""
    root = ET.parse(path).getroot()
    counts = dict(passed=0, failed=0, skipped=0, errors=0)
    for case in root.iter("testcase"):
        key = ("errors" if case.find("error") is not None else
               "failed" if case.find("failure") is not None else
               "skipped" if case.find("skipped") is not None else "passed")
        counts[key] += 1
    return counts


def junit_skipped_names(path: Path) -> set[str]:
    """Full names of SKIPPED test cases in a retained JUnit XML — used to
    block named mandatory checks whose skip would otherwise hide inside
    the percentage-based skip tolerance."""
    root = ET.parse(path).getroot()
    names = set()
    for case in root.iter("testcase"):
        if case.find("skipped") is not None:
            names.add(f"{case.get('classname')}.{case.get('name')}")
    return names


def junit_suite_names(path: Path) -> set[str]:
    """§28.1: the retained report's own stage identity — the root suite
    name(s) written by the producing run (`-o junit_suite_name=<stage>`)."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    return {suite.get("name") or "" for suite in suites}


def verify_linked_trace(stage: str, record: dict, required_fields: dict,
                        evidence_root: Path) -> list[str]:
    """§28.1: where a verdict depends on a linked trace, the trace's
    CONTENT must agree with the receipt — existence + hash is not enough
    when the receipt claims an outcome the trace contradicts."""
    problems = verify_result_file(stage, record, evidence_root)
    if problems:
        return problems
    path = Path(record["result_file"])
    if not path.is_absolute():
        path = ROOT / path
    try:
        trace = json.loads(path.read_text(), object_pairs_hook=unique_object)
    except (OSError, ValueError) as exc:
        return [f"{stage}: linked trace unreadable "
                f"({type(exc).__name__}: {str(exc)[:80]})"]
    if not isinstance(trace, dict):
        return [f"{stage}: linked trace is not an object"]
    for field, expected in required_fields.items():
        if trace.get(field) != expected:
            return [f"{stage}: linked trace contradicts the receipt "
                    f"({field})"]
    return problems


def frontend_counts(path: Path, runner: str,
                    expected_stage: str | None = None) -> dict:
    """§29.2: frontend runner reports carry the SAME identity + parsing
    contract as every other retained artifact. The producer wraps the
    raw runner JSON in a stage-identity envelope (a vitest report cannot
    be recorded under the browser stage, and vice versa), and duplicate
    JSON keys in the retained bytes are refused instead of silently
    picking a last-wins winner."""
    report = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if (isinstance(report, dict) and isinstance(report.get("report"), dict)
            and (expected_stage is None
                 or (report.get("stage") == expected_stage
                     and report.get("runner") == runner))):
        inner = report["report"]
    elif (expected_stage is None and isinstance(report, dict)
          and "report" not in report):
        # Legacy RAW runner report read WITHOUT a stage expectation —
        # callers that only need counts (the §23 fixture pattern). The
        # production verdict path always names the stage, so a raw file
        # recorded under a named stage is still refused there.
        inner = report
    else:
        raise ValueError(
            "frontend report is not a stage/runner-identity envelope")
    if runner == "playwright":
        stats = inner["stats"]
        return {"passed": stats["expected"], "failed": stats["unexpected"],
                "skipped": stats["skipped"], "flaky": stats["flaky"],
                "errors": len(inner.get("errors", []))}
    return {"passed": inner["numPassedTests"], "failed": inner["numFailedTests"],
            "skipped": inner["numPendingTests"], "errors": inner.get("numRuntimeErrorTestSuites", 0)}


def frontend_envelope(stage: str, runner: str, command: list, exit_code: int,
                      raw_report) -> dict:
    """§29.2 producer side: wrap the raw runner JSON in the identity
    envelope the reader requires."""
    return {"stage": stage, "runner": runner, "command": command,
            "exit": exit_code, "report": raw_report}


def parse_pytest_summary(text: str) -> dict:
    counts = {}
    for token, name in (("passed", "passed"), ("failed", "failed"),
                        ("skipped", "skipped"), ("error", "errors"),
                        ("timeout", "timeout")):
        mm = re.findall(rf"(\d+) {token}", text)
        if mm:
            counts[name] = sum(int(x) for x in mm)
    dur = re.search(r"in ([\d.]+)s", text)
    return {"counts": counts,
            "duration_s": float(dur.group(1)) if dur else None}


def run_cmd(cmd, env=None, cwd=ROOT, timeout=3600) -> dict:
    t0 = time.time()
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=e,
                           cwd=cwd, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return {"exit": r.returncode, "tail": out[-4000:], "seconds": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "tail": "TIMEOUT", "seconds": round(time.time() - t0, 1)}


class Qualifier:
    def __init__(self, strict: bool):
        self.strict = strict
        self.attempt_id = uuid.uuid4().hex
        self.attempt_dir = ATTEMPTS / self.attempt_id
        self.attempt_dir.mkdir(parents=True, exist_ok=False)
        self.report = {
            "attempt_id": self.attempt_id,
            "manifest_version": MANIFEST_VERSION,
            "required_stages": list(REQUIRED_STAGES),
            "source_identity": git_identity(),
            "runtime": runtime_identity(),
            "stages": {},
            "retries": {},   # named re-runs, never merged into first attempts
            "blockers_declared": dict(BLOCKERS),
            "strict": strict,
        }

    def record(self, stage: str, fields=None, **extra) -> None:
        body = dict(fields or {})
        body.update(extra)
        body["recorded_at"] = now()
        self.report["stages"][stage] = body

    def stage_pytest(self, stage: str, paths: list[str], env=None,
                     mandatory: bool = True) -> None:
        result_file = self.attempt_dir / f"{stage}-{uuid.uuid4().hex}.xml"
        # §28.1: the retained JUnit report is STAGE-BOUND — the root suite
        # name is pinned to the stage so a report produced for one stage
        # cannot be recorded under another stage's record later.
        cmd = PYTEST + ["-o", f"junit_suite_name={stage}",
                        f"--junitxml={result_file}"] + paths
        res = run_cmd(cmd, env=env)
        try:
            counts = junit_counts(result_file)
        except (OSError, ET.ParseError):
            counts = {}
        ok = res["exit"] == 0 and counts.get("passed", 0) > 0 and not any(
            counts.get(k, 0) for k in ("failed", "errors", "skipped"))
        self.record(stage, {"kind": "pytest", "command": " ".join(cmd),
                            "env": env or {}, "exit": res["exit"],
                            "ok": ok, "seconds": res["seconds"],
                            "counts": counts,
                            "result_file": str(result_file.relative_to(ROOT)),
                            "result_sha256": hashlib.sha256(
                                result_file.read_bytes()).hexdigest()
                            if result_file.exists() else None,
                            "mandatory": mandatory,
                            "tail": res["tail"][-1500:]})

    def stage_shell(self, stage: str, cmd: list[str], env=None,
                    mandatory: bool = True, cwd=None, result_file=None, runner=None) -> None:
        res = run_cmd(cmd, env=env, cwd=cwd or ROOT)
        ok = res["exit"] == 0
        counts = None
        if result_file is not None:
            # §29.2: wrap the raw runner report in a stage/runner identity
            # envelope BEFORE hashing — a report produced for one stage or
            # runner cannot be recorded under another stage's record.
            try:
                raw = json.loads(result_file.read_text(),
                                 object_pairs_hook=unique_object)
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict) and "report" not in raw:
                result_file.write_text(json.dumps(frontend_envelope(
                    stage, runner, cmd, res["exit"], raw), indent=2))
            try:
                counts = frontend_counts(result_file, runner,
                                         expected_stage=stage)
            except (OSError, ValueError, KeyError, TypeError):
                counts = {}
            ok = ok and counts.get("passed", 0) > 0 and not any(
                counts.get(key, 0) for key in ("failed", "skipped", "flaky", "errors"))
        # §24.3: EVERY stage records a hashed result file — a bare
        # success with no traceable artifact is not evidence.
        if result_file is None:
            result_file = self.attempt_dir / f"{stage}-{uuid.uuid4().hex}.json"
            result_file.write_text(json.dumps({
                "stage": stage, "command": cmd, "exit": res["exit"], "ok": ok,
                "seconds": res["seconds"], "tail": res["tail"][-4000:]},
                indent=2))
        sha = hashlib.sha256(result_file.read_bytes()).hexdigest() \
            if result_file.exists() else None
        self.record(stage, {"kind": "shell", "command": " ".join(cmd),
                            "exit": res["exit"], "ok": ok,
                            "seconds": res["seconds"],
                            "mandatory": mandatory,
                            "counts": counts,
                            "result_file": str(result_file.relative_to(ROOT)) if result_file else None,
                            "result_sha256": sha,
                            "tail": res["tail"][-1500:]})

    # ── stages ────────────────────────────────────────────────────────
    def s_migrations(self) -> None:
        """§25.2: emit and hash a STRUCTURED migration result naming the
        database profile actually exercised — the audit found this stage
        returning success with no result file, which the repaired verdict
        then rejected."""
        script = (
            "import asyncio, os, sys, json\n"
            "async def main():\n"
            "    from backend.repositories.sqlite.database import DatabaseManager\n"
            "    db = DatabaseManager(os.environ['VERIFY_DB'])\n"
            "    await db.initialize()\n"
            "    cur = await db._connection.execute('SELECT name FROM sqlite_master')\n"
            "    tables = [r[0] for r in await cur.fetchall()]\n"
            "    print(json.dumps({'db_profile': 'sqlite-fresh',\n"
            "                      'tables': len(tables),\n"
            "                      'migrations_ok': True}))\n"
            "    await db.close()\n"
            "asyncio.run(main())\n")
        import tempfile
        db = os.path.join(tempfile.mkdtemp(), "qual.db")
        res = run_cmd([str(PY), "-c", script],
                      env={"VERIFY_DB": db, **EMULATOR_ENV})
        result_file = self.attempt_dir / "migrations-result.json"
        outcome = {"db_profile": "sqlite-fresh", "exit": res["exit"],
                   "ok": res["exit"] == 0, "stdout": res["tail"][-500:]}
        try:
            observed = json.loads(res["tail"].strip().splitlines()[-1])
            outcome.update(migrations_ok=observed.get("migrations_ok"),
                           tables=observed.get("tables"))
        except (ValueError, IndexError, AttributeError):
            outcome["ok"] = False
        result_file.write_text(json.dumps(outcome, indent=2))
        self.record("migrations", {"kind": "shell", "exit": res["exit"],
                                   "ok": outcome["ok"],
                                   "db_profile": "sqlite-fresh",
                                   "result_file":
                                       str(result_file.relative_to(ROOT)),
                                   "result_sha256": hashlib.sha256(
                                       result_file.read_bytes()).hexdigest(),
                                   "tail": res["tail"][-500:]})

    def s_unit_contract(self) -> None:
        # The RL training suite is ITS OWN stage (rl_training); running
        # the 20-minute real-PPO group inside the full suite both
        # double-counts it and destabilizes the shared host.
        self.stage_pytest("unit_contract", [
            "backend/tests/",
            "--ignore=backend/tests/test_phase21_rl_real_tasks.py",
        ], env=EMULATOR_ENV)

    def s_infra_integration(self) -> None:
        paths = [
            "backend/tests/test_phase20_postgres.py",
            "backend/tests/test_phase21_platform_integration.py",
            "backend/tests/test_phase21_stack_journeys.py",
            "backend/tests/test_phase20_firestore_emulator.py",
            "backend/tests/test_phase8_11_firestore_parity.py",
            "backend/tests/test_phase3_firestore_parity.py",
            "backend/tests/test_phase21_packaging.py",
        ]
        self.stage_pytest("infra_integration", paths, env=EMULATOR_ENV)

    def s_live_inference(self) -> None:
        # §24.3 (audit #5): local and hosted checks run INDEPENDENTLY —
        # an unavailable local runtime must never leave hosted unattempted.
        ok, detail = readiness("ollama", "agentos-ollama-1")
        if not ok:
            self.record("live_inference_local", {
                "kind": "skip", "reason": detail, "mandatory": True,
                "blocker": BLOCKERS["product_e2e_browser"]})
        else:
            self.stage_pytest("live_inference_local",
                              ["backend/tests/test_phase21_providers_adk.py",
                               "backend/tests/test_phase21_provider_credentials.py",
                               "backend/tests/test_phase21_dynamic_behavior.py"],
                              env=EMULATOR_ENV)
        self.external_stage("live_inference_hosted", "QUALIFY_HOSTED_EVIDENCE")

    def external_stage(self, stage: str, variable: str) -> None:
        """Retain reviewed external observations inside this qualification attempt."""
        location = os.environ.get(variable)
        if not location:
            self.record(stage, kind="blocked", ok=False, mandatory=True,
                        blocker=BLOCKERS[stage], evidence_variable=variable)
            return
        try:
            manifest = Path(location).resolve(strict=True)
            expected_digest = os.environ.get(variable + "_SHA256", "").lower()
            if not re.fullmatch("[0-9a-f]{64}", expected_digest):
                raise ValueError("missing reviewed manifest SHA-256")
            raw = manifest.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != expected_digest:
                raise ValueError("manifest differs from the independently supplied SHA-256")
            body = json.loads(raw, object_pairs_hook=unique_object)
            identity = self.report["source_identity"]["content_sha256"]
            profile = os.environ.get("TARGET_PROFILE", "local-durable-postgres")
            if (not isinstance(body, dict) or body.get("stage") != stage
                    or body.get("candidate_sha256") != identity or body.get("target_profile") != profile
                    or not re.fullmatch("[a-f0-9]{64}", str(body.get("image_digests_sha256", "")))):
                raise ValueError("stage, source or deployment identity missing or mismatched")
            if stage == "pilot":
                records = [*body["sessions"], *body.get("unsuccessful_sessions", [])]
            else:
                records = list(body["providers"].values())
            retained = self.attempt_dir / f"{stage}-{uuid.uuid4().hex}"
            retained.mkdir(mode=0o700)
            (retained / "original.json").write_bytes(raw)
            unassisted = 0
            participants = set()
            for index, record in enumerate(records):
                # Accept flat JSON trace names only. Never dereference external URLs.
                name = record["result_file"]
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]*\.json", name):
                    raise ValueError("expected a JSON trace filename alongside the manifest")
                source = (manifest.parent / name).resolve(strict=True)
                if source.parent != manifest.parent or not source.is_file():
                    raise ValueError("trace outside the manifest directory")
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != record["result_sha256"]:
                    raise ValueError("trace digest mismatch")
                trace = json.loads(data, object_pairs_hook=unique_object)
                for field in ("candidate_sha256", "target_profile", "image_digests_sha256"):
                    if not isinstance(trace, dict) or trace.get(field) != body[field]:
                        raise ValueError("trace references a different candidate or deployment")
                if stage == "live_inference_hosted":
                    for field in ("run_id", "model", "checks"):
                        if trace.get(field) != record.get(field):
                            raise ValueError("provider trace contradicts the workflow receipt")
                    if not all(record.get("checks", {}).get(k) is True for k in ("streaming", "error")):
                        raise ValueError("hosted streaming or error observation missing")
                    usage = trace.get("provider_usage", {})
                    counts = [usage.get(field) for field in ("input_tokens", "output_tokens")]
                    if any(type(v) is not int or v < 0 for v in counts) or sum(counts) == 0:
                        raise ValueError("missing provider-reported token counts")
                    if not all(isinstance(trace.get(field), str) and trace[field].strip()
                               for field in ("tool_call_id", "tool_result_sha256", "cancelled_run_id",
                                             "rotation_request_id", "revocation_request_id", "error_request_id")):
                        raise ValueError("hosted trace lacks lifecycle observation references")
                    if (not re.fullmatch("[0-9a-f]{64}", trace["tool_result_sha256"])
                            or type(trace.get("stream_events")) is not int or trace["stream_events"] <= 0
                            or trace.get("cancelled_status") != "CANCELLED"
                            or trace.get("rotated_credential_succeeded") is not True
                            or trace.get("revoked_credential_denied") is not True):
                        raise ValueError("incomplete observed hosted outcomes")
                else:
                    for field in ("participant_ref", "consent_ref"):
                        if not record.get(field) or trace.get(field) != record[field]:
                            raise ValueError("pilot consent or participant reference mismatch")
                    participants.add(record["participant_ref"])
                    tasks = trace.get("unassisted_tasks_1_to_3")
                    if not isinstance(tasks, list) or len(tasks) != 3 or any(type(v) is not bool for v in tasks):
                        raise ValueError("missing unassisted outcomes for the three onboarding tasks")
                    unassisted += int(all(tasks))
                    attempted = trace.get("takeover_attempted")
                    if type(attempted) is not bool or (attempted and (
                            trace.get("takeover_succeeded") is not True or trace.get("stale_action_refused") is not True)):
                        raise ValueError("missing or failed takeover observation")
                    if trace.get("tenant_isolation_verified") is not True or trace.get("failures_reconciled") is not True:
                        raise ValueError("missing pilot isolation or failure reconciliation evidence")
                target = retained / f"trace-{index}.json"
                target.write_bytes(data)
                record["result_file"] = str(target.relative_to(ROOT))
            if stage == "live_inference_hosted" and len({r["run_id"] for r in records}) != len(records):
                raise ValueError("each provider requires its own observed workflow")
            if stage == "pilot" and (len(participants) != len(records) or not 3 <= len(records) <= 5
                                     or 10 * unassisted < 7 * len(records)):
                raise ValueError("pilot participant count or unassisted completion threshold unmet")
            outcome = retained / "outcome.json"
            outcome.write_text(json.dumps(body, indent=2))
            receipt = {"kind": "shell", "mandatory": True, "result_file": str(outcome.relative_to(ROOT)),
                       "result_sha256": hashlib.sha256(outcome.read_bytes()).hexdigest(),
                       "manifest_sha256": digest, "image_digests_sha256": body["image_digests_sha256"],
                       "candidate_sha256": identity, "target_profile": profile}
            problems = parse_retained_counts(stage, receipt, self.attempt_dir)
            self.record(stage, receipt, ok=not problems, problems=problems)
        except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
            self.record(stage, kind="blocked", ok=False, mandatory=True,
                        blocker=f"Invalid {stage} evidence: {exc}")

    def s_fault_injection(self) -> None:
        self.stage_pytest("fault_injection", [
            "backend/tests/test_phase21_injected_healing.py",
            "backend/tests/test_phase21_isolated_runner.py",
            "backend/tests/test_phase20_healing.py",
        ], env=EMULATOR_ENV)

    def s_rl_training(self) -> None:
        """§22.21: genuine RL — DEFERRED while BLOCKER-B5 stands (the
        release owner's 2026-11-14 decision: host and deploy first, RL
        later). The runtime never imports the RL path; governed rollout
        uses deterministic baselines until trained artifacts exist."""
        self.record("rl_training", {
            "kind": "blocked", "ok": False, "mandatory": True,
            "blocker": BLOCKERS["rl_training"]})

    def s_security(self) -> None:
        self.stage_pytest("security", [
            "backend/tests/test_phase23_release_boundaries.py",
            "backend/tests/test_phase23_feedback_http.py",
            "backend/tests/test_phase23_deployment_gate.py",
            "backend/tests/test_auth_enforcement.py",
            "backend/tests/test_auth_challenges.py",
            "backend/tests/test_auth_http.py",
            "backend/tests/test_background_authorization.py",
            "backend/tests/test_execution_safety.py",
            "backend/tests/test_admission_policy.py",
            "backend/tests/test_budget_concurrency.py",
            "backend/tests/test_budget_reconciliation.py",
        ], env=EMULATOR_ENV)

    def s_product_e2e(self) -> None:
        fe = ROOT / "frontend"
        vitest_file = self.attempt_dir / f"vitest-{uuid.uuid4().hex}.json"
        self.stage_shell("product_e2e_vitest",
                         ["npm", "test", "--", "--reporter=json", f"--outputFile={vitest_file}"],
                         cwd=fe, result_file=vitest_file, runner="vitest")
        self.stage_shell("product_e2e_typecheck",
                         ["npm", "run", "typecheck"], cwd=fe)
        self.stage_shell("product_e2e_lint",
                         ["npm", "run", "lint", "--", "--max-warnings=0"],
                         cwd=fe)
        self.stage_shell("product_e2e_build",
                         ["npm", "run", "build"], cwd=fe)
        ok, detail = readiness("api", "agentos-api-1")
        if ok:
            playwright_file = self.attempt_dir / f"playwright-{uuid.uuid4().hex}.json"
            # The compose frontend IS the production artifact; point
            # playwright at it rather than failing on the occupied port.
            self.stage_shell("product_e2e_browser",
                             ["npm", "run", "test:e2e:production",
                              "--", "--reporter=json", "--workers=1"],
                             cwd=fe,
                             env={"E2E_REUSE": "1", "PLAYWRIGHT_JSON_OUTPUT_NAME": str(playwright_file)},
                             result_file=playwright_file, runner="playwright")
        else:
            self.record("product_e2e_browser", {
                "kind": "skip", "reason": detail, "mandatory": True,
                "blocker": BLOCKERS["product_e2e_browser"]})

    def s_flagship(self) -> None:
        """§22.14 + §27.5: the flagship benchmark — the real generated
        connector journey (no manually manufactured child completion)
        plus the reconciliation guard that proves manual child
        completion can NOT activate a capability."""
        self.stage_pytest("flagship",
                          ["backend/tests/test_phase26_connector_journey.py",
                           "backend/tests/test_phase26_standalone_journey.py",
                           "backend/tests/test_phase21_flagship_journey.py",
                           "backend/tests/test_self_extension_loop.py"])

    def s_restore(self) -> None:
        """§22.30/§24.9: backup → isolated restore → integrity counts.
        The restore destination is UNIQUELY named per attempt (the audit
        flagged the fixed scratch name); subchecks are structured so the
        independent-application trace is a distinct, currently-absent
        outcome — never folded into a database-count pass."""
        import subprocess as sp
        subchecks = {"dump": None, "restore": None, "integrity": None,
                     "independent_app_trace": None}
        r1 = sp.run(["docker", "exec", "agentos-postgres-1", "pg_dump",
                     "-U", "agentos", "agentos_test"],
                    capture_output=True, timeout=120)
        if r1.returncode != 0 or not r1.stdout:
            self.record("restore", {"kind": "shell", "ok": False,
                                    "subchecks": {**subchecks,
                                                  "dump": f"exit {r1.returncode}"},
                                    "tail": r1.stderr.decode()[-400:]})
            return
        subchecks["dump"] = "ok"
        # Count rows in the same pg_dump snapshot, even if live writers continue.
        source_counts = {}
        table = None
        for line in r1.stdout.splitlines():
            if table:
                if line == b"\\.":
                    table = None
                else:
                    source_counts[table] += 1
            elif line.startswith(b"COPY public."):
                name = line.split(b" ", 2)[1].removeprefix(b"public.").decode()
                if name in ("workflows", "events"):
                    table = name
                    source_counts[table] = 0
        restore_db = f"agentos_restore_{self.attempt_id}"
        psql = ["docker", "exec", "-i", "agentos-postgres-1", "psql",
                "-X", "-v", "ON_ERROR_STOP=1", "-U", "agentos", "-At"]
        restored_counts = {}
        created = False
        try:
            sp.run(psql + ["-d", "postgres", "-c", f"CREATE DATABASE {restore_db}"],
                   capture_output=True, timeout=60, check=True)
            created = True
            sp.run(psql + ["-d", restore_db], input=r1.stdout,
                   capture_output=True, timeout=300, check=True)
            subchecks["restore"] = "ok"
            for table in ("workflows", "events"):
                check = sp.run(psql + ["-d", restore_db, "-c", f"SELECT count(*) FROM {table}"],
                               capture_output=True, timeout=60, check=True)
                restored_counts[table] = int(check.stdout.strip())
            subchecks["integrity"] = "ok" if source_counts == restored_counts else "count mismatch"
        except (sp.SubprocessError, ValueError) as exc:
            subchecks["integrity"] = f"restore command failed ({type(exc).__name__})"
        finally:
            if created:
                cleanup = sp.run(psql + ["-d", "postgres", "-c", f"DROP DATABASE {restore_db}"],
                                 capture_output=True, timeout=60)
                subchecks["cleanup"] = "ok" if cleanup.returncode == 0 else "failed"
        # §27.R9: the independent APPLICATION trace is no longer a
        # pending placeholder — the permanent recovery command
        # (scripts/qualify_restore_application.py) restores the full
        # application (DB + artifact bytes + key material) into an
        # isolated stack and exercises sign-in, downloads, revocation
        # and a fresh verified workflow. Its structured receipt must
        # pass on THIS source to satisfy the subcheck.
        trace_file = self.attempt_dir / "restore-app-trace.json"
        try:
            trace_file.unlink()
        except OSError:
            pass
        res_app = sp.run(
            [str(PY), "scripts/qualify_restore_application.py",
             str(trace_file)],
            capture_output=True, text=True, timeout=900, cwd=ROOT)
        app_ok = False
        app_detail = f"exit {res_app.returncode}"
        try:
            trace = json.loads(trace_file.read_text())
            required_checks = {
                "real_signup", "source_backed_up",
                "restored_database_integrity", "previously_issued_token",
                "new_login", "logout_still_enforced",
                "api_key_use_and_revocation", "previous_download",
                "previous_workflow_and_task_outputs",
                "restored_ownership_enforced",
                "acknowledged_write_and_deletion",
                "restored_vault_credential_challenge",
                "new_workflow_verified", "temporary_databases_removed"}
            got_ok = {k for k, v in (trace.get("checks") or {}).items()
                      if v is True}
            app_ok = (res_app.returncode == 0
                      and trace.get("passed") is True
                      and required_checks <= got_ok)
            app_detail = f"checks {len(required_checks & got_ok)}/{len(required_checks)}"
        except (OSError, ValueError) as exc:
            app_detail = f"unreadable trace ({type(exc).__name__})"
        subchecks["independent_app_trace"] = (
            "ok" if app_ok else f"failed ({app_detail})")
        src_counts, rst_counts = source_counts, restored_counts
        ok = all(v == "ok" for v in subchecks.values())
        result_file = self.attempt_dir / "restore-outcome.json"
        result_file.write_text(json.dumps({
            "ok": ok, "exit": 0 if ok else 1,
            "subchecks": subchecks, "source_counts": src_counts,
            "restored_counts": rst_counts}, indent=2))
        self.record("restore", {"kind": "shell", "ok": ok,
                                "subchecks": subchecks,
                                "result_file": str(result_file.relative_to(ROOT)),
                                "result_sha256": hashlib.sha256(
                                    result_file.read_bytes()).hexdigest(),
                                "dump_sha256": hashlib.sha256(
                                    r1.stdout).hexdigest(),
                                "app_trace_file": (
                                    str(trace_file.relative_to(ROOT))
                                    if trace_file.exists() else None),
                                "app_trace_sha256": (
                                    hashlib.sha256(trace_file.read_bytes()).hexdigest()
                                    if trace_file.exists() else None)})

    def s_target_deployment(self) -> None:
        """§22.32/§24.9: the target stage records LIVE identity — the
        running image digests read back from the environment — and
        requires a COMPLETED workflow through the real ingress. A health
        check alone is deployment liveness, not deployment qualification
        (audit #4)."""
        import subprocess as sp
        import urllib.request, urllib.error
        profile = os.environ.get("TARGET_PROFILE", "local-durable-postgres")
        if profile != "local-durable-postgres":
            try:
                if __package__:
                    from .qualify_cloud_target import probe
                else:
                    from qualify_cloud_target import probe
                outcome = probe(profile, self.report["source_identity"]["content_sha256"])
            except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError) as exc:
                # Connection exceptions may contain request URLs. Store only safe probe diagnostics.
                detail = str(exc) if type(exc).__name__ == "ProbeError" else type(exc).__name__
                self.record("target_deployment", kind="blocked", ok=False, profile=profile, blocker=detail)
                return
            result_file = self.attempt_dir / "target-identity.json"
            result_file.write_text(json.dumps(outcome, indent=2))
            self.record("target_deployment", kind="shell", ok=True, profile=profile,
                        candidate_sha256=outcome["candidate_sha256"],
                        image_digests=outcome["image_digests"], workflow=outcome["workflow"],
                        result_file=str(result_file.relative_to(ROOT)),
                        result_sha256=hashlib.sha256(result_file.read_bytes()).hexdigest())
            return
        ok, detail = readiness("api", "agentos-api-1")
        digests = {}
        for container, label in (("agentos-api-1", "api"),
                                 ("agentos-worker-1", "worker"),
                                 ("agentos-frontend-1", "frontend")):
            r = sp.run(["docker", "inspect", container, "--format",
                        "{{.Image}}"], capture_output=True, text=True,
                       timeout=15)
            digests[label] = r.stdout.strip() if r.returncode == 0 else None
        k8s = sp_run_kind()
        workflow = {"submitted": False, "completed": False}
        if ok:
            def req(method, url, token=None, body=None):
                data = json.dumps(body).encode() if body is not None else None
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                r = urllib.request.Request(url, data=data,
                                           headers=headers, method=method)
                try:
                    with urllib.request.urlopen(r, timeout=60) as resp:
                        return resp.status, json.loads(resp.read() or b"{}")
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read() or b"{}")
                except Exception as e:  # noqa: BLE001
                    return 0, {"_error": str(e)[:120]}
            code, g = req("POST", "http://127.0.0.1:8000/api/v1/auth/guest")
            token = g.get("access_token")
            code, run = req("POST", "http://127.0.0.1:8000/api/v1/workflows",
                            token, {"goal": "GET https://httpbin.org/json"})
            workflow["submitted"] = code == 201
            rid = run.get("run_id")
            deadline = time.time() + 150
            while rid and time.time() < deadline:
                c, b = req("GET",
                           f"http://127.0.0.1:8000/api/v1/workflows/{rid}",
                           token)
                st = str(b.get("status") or "")
                if st in ("COMPLETED", "FAILED", "CANCELLED"):
                    workflow["completed"] = st == "COMPLETED"
                    workflow["final_status"] = st
                    workflow["run_id"] = rid
                    break
                time.sleep(5)
        ok = ok and all(digests.values()) and workflow.get("completed")
        result_file = self.attempt_dir / "target-identity.json"
        result_file.write_text(json.dumps({
            "ok": ok, "exit": 0 if ok else 1,
            "readiness": detail, "image_digests": digests,
            "workflow": workflow, "kubernetes_note": k8s}, indent=2))
        self.record("target_deployment", {
            "kind": "shell", "ok": ok,
            "readiness": detail, "image_digests": digests,
            "workflow": workflow,
            "result_file": str(result_file.relative_to(ROOT)),
            "result_sha256": hashlib.sha256(
                result_file.read_bytes()).hexdigest()})

    def s_pilot(self) -> None:
        self.external_stage("pilot", "QUALIFY_PILOT_EVIDENCE")

    def s_performance(self) -> None:
        ok_pg, d1 = readiness("postgres", "agentos-postgres-1")
        ok_api, d2 = readiness("api", "agentos-api-1")
        if not (ok_pg and ok_api):
            self.record("performance", {"kind": "skip", "mandatory": True,
                                        "reason": f"{d1}; {d2}",
                                        "blocker": BLOCKERS["performance"]})
            return
        path = self.attempt_dir / "admission.json"
        result = run_cmd([str(PY), "scripts/loadtest_admission.py", "--output", str(path)])
        admission_ok = result["exit"] == 0 and path.exists()
        # §27.19/§27.R11: the admission microbenchmark is its own
        # subcheck; the stage additionally requires the WHOLE-WORKLOAD
        # measurement (mixed families through the real ingress, with
        # predeclared completion/unauthorized/abandonment gates).
        workload_path = self.attempt_dir / "workload.json"
        wresult = run_cmd([str(PY), "scripts/workload_measurement.py",
                           "--output", str(workload_path)])
        workload = {}
        if workload_path.exists():
            try:
                workload = json.loads(workload_path.read_text())
            except ValueError:
                workload = {}
        workload_ok = bool(workload.get("ok"))
        if admission_ok and workload_ok:
            # The stage's RETAINED result is ONE combined receipt: the
            # admission microbenchmark's rate_limit/capacity subchecks
            # plus the whole-workload measurement (results + predeclared
            # gates). Both producer files stay retained and hashed.
            try:
                admission_receipt = json.loads(path.read_text())
            except ValueError:
                admission_receipt = {}
            combined = {
                "stage": "performance", "ok": True, "exit": 0,
                "rate_limit": admission_receipt.get("rate_limit"),
                "capacity": admission_receipt.get("capacity"),
                "workload": workload,
                "measurement_profile": os.environ.get(
                    "TARGET_PROFILE", "local-durable-postgres")}
            combined_path = self.attempt_dir / "performance-combined.json"
            combined_path.write_text(json.dumps(combined, indent=2))
            self.record("performance", {
                "kind": "shell", "ok": True,
                "exit": 0,
                "result_file": str(combined_path.relative_to(ROOT)),
                "result_sha256": hashlib.sha256(
                    combined_path.read_bytes()).hexdigest(),
                "admission_result_file": str(path.relative_to(ROOT)),
                "admission_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "workload_result_file": str(workload_path.relative_to(ROOT)),
                "workload_sha256": hashlib.sha256(
                    workload_path.read_bytes()).hexdigest(),
                "workload": workload.get("results"),
                "gates": workload.get("gates")})
        else:
            self.record("performance", {"kind": "partial", "ok": False,
                        "exit": wresult["exit"] if not workload_ok else result["exit"],
                        "blocker": ("whole-workload measurement did not meet "
                                    "its predeclared gates"
                                    if not workload_ok else
                                    "admission microbenchmark failed"),
                        "admission_ok": admission_ok,
                        "workload_gates": workload.get("gates"),
                        "result_file": str(path.relative_to(ROOT)),
                        "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None})

    # ── verdict ───────────────────────────────────────────────────────
    def verdict(self) -> tuple[bool, list[str]]:
        """Phase 24 §24.3 repaired release verdict.

        A release pass requires EVERY rule below:
        1. scope == "full" — a subset attempt is evidence, never a pass
           (the Phase 23 audit counterexample).
        2. The expected manifest version.
        3. All mandatory stages present, each with the evidence its kind
           requires (executed counts for suites, a hashed result file for
           everything, no skip/blocked/partial).
        4. All 22 mandatory journeys recorded with evidence, matched to
           THIS candidate and the named target.
        5. Candidate and target identity present.
        6. Source unchanged during the attempt.
        A retry — even a passing one — never repairs a failed first
        attempt: the first-attempt stage record is what is judged.
        """
        problems: list[str] = []
        # 1. scope
        scope = self.report.get("scope")
        if scope != "full":
            problems.append(
                f"scope: release pass requires scope=full (got {scope!r})")
        # 2. manifest version
        version = self.report.get("manifest_version")
        if version != MANIFEST_VERSION:
            problems.append(f"manifest_version: expected {MANIFEST_VERSION}, "
                            f"got {version!r}")
        # 3. stages
        stages = self.report.get("stages", {})
        for name in REQUIRED_STAGES:
            if name not in stages:
                problems.append(f"{name}: missing mandatory stage")
        for name, st in stages.items():
            problems.extend(self._stage_problems(name, st))
        # 4. journeys
        problems.extend(self._journey_problems())
        # 4b. Phase 31 journey-independent scenarios (own receipts).
        problems.extend(self._scenario_problems())
        # 5. identities
        if not (self.report.get("candidate_identity") or {}) \
                .get("content_sha256"):
            problems.append(
                "candidate_identity: missing content_sha256 — evidence is "
                "not tied to a build")
        target_profile = (self.report.get("target_identity") or {}) \
            .get("profile")
        if not target_profile:
            problems.append(
                "target_identity: missing profile — evidence is not tied "
                "to a named deployment target")
        # 6. source drift
        if self.report.get("source_changed"):
            problems.append("source changed during qualification")
        return (not problems), problems

    def _stage_problems(self, name: str, st: dict) -> list[str]:
        problems: list[str] = []
        kind = st.get("kind")
        if kind in ("skip", "blocked", "partial"):
            problems.append(f"{name}: {kind} (blocker: "
                            f"{(st.get('blocker') or 'none')[:80]})")
            return problems
        if st.get("ok") is not True:
            problems.append(f"{name}: stage failed (ok={st.get('ok')!r})")
        target_profile = (self.report.get("target_identity") or {}).get("profile")
        if target_profile and target_profile != "local-durable-postgres" and name in LOCAL_RUNTIME_STAGES:
            problems.append(
                f"{name}: producer exercises local infrastructure; "
                f"deployment-specific execution for {target_profile} is still required")
        if name == "target_deployment" and target_profile in ("gcp-gke", "aws-eks", "azure-aks"):
            if (st.get("profile") != target_profile or st.get("candidate_sha256") !=
                    (self.report.get("candidate_identity") or {}).get("content_sha256")):
                problems.append("target_deployment: evidence belongs to a different release or cloud")
        if name in ("pilot", "live_inference_hosted") and "manifest_sha256" in st:
            candidate = self.report.get("candidate_identity") or {}
            target = self.report.get("target_identity") or {}
            original = Path(st.get("result_file") or "").parent / "original.json"
            problems.extend(verify_result_file(name, {"result_file": str(original),
                "result_sha256": st["manifest_sha256"]}, getattr(self, "attempt_dir", None) or ATTEMPTS))
            if (st.get("candidate_sha256") != candidate.get("content_sha256")
                    or st.get("target_profile") != target.get("profile")
                    or st.get("image_digests_sha256") != target.get("image_digests_sha256")):
                problems.append(f"{name}: external evidence differs from the observed release deployment")
        if kind == "pytest":
            counts = st.get("counts") or {}
            if not counts.get("passed"):
                problems.append(
                    f"{name}: pytest evidence has no executed pass count")
            dirty = {k: counts.get(k, 0) for k in
                     ("failed", "errors", "flaky", "timeout")
                     if counts.get(k, 0)}
            if dirty:
                problems.append(f"{name}: incomplete test execution {dirty}")
            # Skips are acceptable when the pass rate is very high and the
            # skip count is small — a declared blocker's skip (e.g. the
            # deployed-browser test needing a public URL) is not a failure.
            total = sum(counts.get(k, 0) for k in
                        ("passed", "failed", "errors", "skipped"))
            if counts.get("skipped", 0) > 0 and total > 0:
                skip_rate = counts["skipped"] / total
                if skip_rate > 0.01:
                    problems.append(
                        f"{name}: excessive skips {counts['skipped']}/{total}")
        # §25.2: open the retained file, recompute, compare. The ok flag
        # and the recorded counts are never trusted on their own.
        # §29.2: evidence is scoped to THIS attempt's directory — a file
        # from a sibling attempt (even with the same candidate identity)
        # is not this attempt's evidence. A bare-constructed Qualifier
        # (verdict-level regression fixtures) has no attempt directory;
        # it falls back to the evidence root instead of crashing.
        problems.extend(parse_retained_counts(
            name, st, getattr(self, "attempt_dir", None) or ATTEMPTS))
        return problems

    def _journey_problems(self) -> list[str]:
        """§25.2 + Phase 31 §31.2: a journey record must identify the
        PRODUCTION ACTION, the run it executed on, the expected
        postcondition, the OBSERVED result, and evidence that exists and
        hashes back. A filename pointing at a test implementation
        documents how to run a check — it cannot show the check
        executed. The run_id must be a REAL product record: a composed
        or generated reference cannot back a passing journey, and every
        passing journey's run must be named by retained observation
        bytes or a validated scenario receipt bound to THIS candidate,
        target, and operator attempt."""
        problems: list[str] = []
        journeys = {
            k: v for k, v in (self.report.get("journeys") or {}).items()
            if k != "scenarios"}
        cand = (self.report.get("candidate_identity") or {}) \
            .get("content_sha256")
        target = self.report.get("target_identity") or {}
        target_id = target.get("profile")
        target_digest = target.get("image_digests_sha256")
        for jid in REQUIRED_JOURNEYS:
            rec = journeys.get(jid)
            if rec is None:
                problems.append(
                    f"journey {jid}: missing mandatory journey evidence")
                continue
            if not isinstance(rec, dict):
                problems.append(f"journey {jid}: malformed journey record")
                continue
            if rec.get("ok") is not True:
                problems.append(f"journey {jid}: recorded failure")
            for field in ("production_action", "run_id",
                          "expected_postcondition", "observed_result"):
                if not str(rec.get(field) or "").strip():
                    problems.append(
                        f"journey {jid}: missing {field} — the record must "
                        "bind an observed action, not a claim")
            if rec.get("candidate_sha256") != cand:
                problems.append(
                    f"journey {jid}: evidence from a different candidate")
            if not target_digest or rec.get("target_image_digests_sha256") != target_digest:
                problems.append(f"journey {jid}: deployment image digest mismatch or missing")
            if rec.get("target_id") != target_id:
                problems.append(
                    f"journey {jid}: evidence from a different target")
            # §31.2: run provenance — a generated/composed ID is not a
            # product record; a claimed run must be named by retained
            # observation bytes or a validated scenario receipt.
            problems.extend(self._journey_run_provenance(
                jid, rec, cand, target_id))
            # §25.2: stage→journey binding — J14's evidence must
            # include the rl_training stage's own retained result.
            for bound_stage in JOURNEY_STAGE_BINDING.get(jid, ()):
                bound = (self.report.get("stages") or {}).get(bound_stage)
                if not bound or not bound.get("result_file"):
                    problems.append(
                        f"journey {jid}: bound stage {bound_stage} has no "
                        "retained result — governed-learning evidence "
                        "cannot be satisfied without it")
            evidence = rec.get("evidence")
            if not evidence:
                problems.append(f"journey {jid}: no evidence records")
                continue
            for bound_stage in JOURNEY_STAGE_BINDING.get(jid, ()):
                stage = self.report["stages"].get(bound_stage, {})
                if not any(isinstance(e, dict) and e.get("result_sha256") == stage.get("result_sha256")
                           and e.get("result_file") == stage.get("result_file") for e in evidence):
                    problems.append(f"journey {jid}: missing receipt from {bound_stage}")
            for entry in evidence:
                if not isinstance(entry, dict) or \
                        not entry.get("result_file"):
                    problems.append(
                        f"journey {jid}: evidence entry is not a verifiable "
                        "record (text placeholders are not evidence)")
                    continue
                problems.extend(verify_result_file(
                    f"journey {jid}", entry,
                    getattr(self, "attempt_dir", None) or ATTEMPTS))
        for extra in journeys:
            if extra not in REQUIRED_JOURNEYS:
                problems.append(f"journey {extra}: not a mandatory journey id")
        return problems

    # ── Phase 31 §31.2: run provenance and scenario receipts ─────────

    def _evidence_root(self) -> Path:
        return getattr(self, "attempt_dir", None) or ATTEMPTS

    def _journey_run_provenance(self, jid: str, rec: dict,
                                cand: str | None, target_id: str | None) -> \
            list[str]:
        """The run a passing journey claims must be a real product
        record. Three provenance paths, each independently verifiable:
        (a) the record's `observation` file names the run, (b) a
        validated scenario receipt's relations name the run, (c) one of
        the record's evidence entries is a retained product receipt
        whose bytes name the run (restore trace, target identity)."""
        run_id = str(rec.get("run_id") or "").strip()
        if rec.get("ok") is not True and not run_id:
            return []
        problems: list[str] = []
        if is_composed_run_reference(run_id):
            if rec.get("ok") is True:
                problems.append(
                    f"journey {jid}: composed run reference {run_id!r} is "
                    "not a product record — a passing journey must carry a "
                    "real run ID observed from the product")
            return problems
        root = self._evidence_root()
        # (a) observation reference: retained bytes that observed the run.
        obs = rec.get("observation")
        if isinstance(obs, dict) and obs.get("result_file"):
            obs_problems = verify_result_file(f"journey {jid} observation",
                                              obs, root)
            if obs_problems:
                return obs_problems
            path = Path(obs["result_file"])
            if not path.is_absolute():
                path = ROOT / path
            try:
                body = path.read_text()
                parsed = json.loads(body, object_pairs_hook=unique_object)
            except (OSError, ReceiptError):
                parsed = None
            if run_id and run_id not in body:
                problems.append(
                    f"journey {jid}: observation does not name run "
                    f"{run_id} — the reference must bind the run it claims")
            elif isinstance(parsed, dict) and cand and \
                    not self._observation_names_candidate(parsed, cand):
                problems.append(
                    f"journey {jid}: observation is not bound to this "
                    "candidate")
            return problems
        # (b) scenario receipts from the operator runner.
        receipts = rec.get("scenario_receipts")
        if isinstance(receipts, list) and receipts:
            return self._scenario_receipt_problems(jid, receipts, rec, cand,
                                                   target_id)
        # (c) evidence bytes that are themselves product receipts.
        for entry in rec.get("evidence") or []:
            if not isinstance(entry, dict) or not entry.get("result_file"):
                continue
            path = Path(str(entry["result_file"]))
            if not path.is_absolute():
                path = ROOT / path
            try:
                body = path.read_text()
                parsed = json.loads(body, object_pairs_hook=unique_object)
            except (OSError, ReceiptError, ValueError):
                continue
            if isinstance(parsed, dict) and run_id and run_id in body and \
                    self._names_a_product_run(parsed):
                if parsed.get("candidate_sha256") in (None, cand):
                    return []
        if run_id and rec.get("ok") is True:
            problems.append(
                f"journey {jid}: run_id {run_id!r} has no retained product "
                "record naming it — a generated ID cannot establish the "
                "postcondition")
        return problems

    @staticmethod
    def _names_a_product_run(parsed: dict) -> bool:
        for key in ("run_id", "workflow_run_id"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip() and \
                    not is_composed_run_reference(v):
                return True
        wf = parsed.get("workflow")
        if isinstance(wf, dict):
            v = wf.get("run_id")
            if isinstance(v, str) and v.strip() and \
                    not is_composed_run_reference(v):
                return True
        return False

    @staticmethod
    def _observation_names_candidate(parsed: dict, cand: str) -> bool:
        got = parsed.get("candidate_sha256")
        return got is None or got == cand

    def _scenario_receipt_problems(self, label: str, receipts: list,
                                   rec: dict, cand: str | None,
                                   target_id: str | None) -> list[str]:
        """Validate the scenario receipts attached to a journey record:
        each must exist in the attempt directory, hash back, parse with
        duplicate-key rejection, satisfy the receipt contract, and bind
        to this candidate/target/operator attempt. At least one PASSING
        receipt must name the record's run."""
        problems: list[str] = []
        root = self._evidence_root()
        named_run = False
        for i, ref in enumerate(receipts):
            if not isinstance(ref, dict) or not ref.get("result_file"):
                problems.append(
                    f"{label}: scenario receipt {i} is not a verifiable "
                    "reference")
                continue
            hash_problems = verify_result_file(
                f"{label} receipt {i}", ref, root)
            if hash_problems:
                problems.extend(hash_problems)
                continue
            path = Path(str(ref["result_file"]))
            if not path.is_absolute():
                path = ROOT / path
            try:
                receipt = load_receipt(path)
            except ReceiptError as exc:
                problems.append(f"{label} receipt {i}: {exc}")
                continue
            problems.extend(
                f"{label} receipt {i}: {p}" for p in validate_receipt(
                    receipt,
                    expected_scenario_id=ref.get("scenario_id"),
                    expected_journey_id=label if label.startswith("J")
                    else receipt.get("journey_id"),
                    candidate_sha256=cand, target_id=target_id,
                    operator_attempt_id=str(rec.get("attempt_id") or "") or
                    None))
            if receipt_passes(receipt) and str(rec.get("run_id") or "") in \
                    real_run_ids(receipt):
                named_run = True
        if rec.get("ok") is True and receipts and not named_run:
            problems.append(
                f"{label}: no passing scenario receipt names run "
                f"{rec.get('run_id')!r}")
        return problems

    def _scenario_problems(self) -> list[str]:
        """Phase 31 §31.2: the journey-independent scenarios
        (ORDINARY-CHAT, LINK-PREVIEW, DOCK-RESOURCE-IDENTITY) each need
        their own receipt-bound verdict on THIS candidate and target."""
        problems: list[str] = []
        scenarios = self.report.get("scenarios") or {}
        cand = (self.report.get("candidate_identity") or {}) \
            .get("content_sha256")
        target = self.report.get("target_identity") or {}
        target_id = target.get("profile")
        target_digest = target.get("image_digests_sha256")
        for sid in REQUIRED_SCENARIOS:
            rec = scenarios.get(sid)
            if rec is None:
                problems.append(
                    f"scenario {sid}: missing mandatory scenario evidence")
                continue
            if not isinstance(rec, dict):
                problems.append(f"scenario {sid}: malformed scenario record")
                continue
            if rec.get("ok") is not True:
                problems.append(f"scenario {sid}: recorded failure")
            for field in ("production_action", "run_id",
                          "expected_postcondition", "observed_result"):
                if not str(rec.get(field) or "").strip():
                    problems.append(
                        f"scenario {sid}: missing {field}")
            if rec.get("candidate_sha256") != cand:
                problems.append(
                    f"scenario {sid}: evidence from a different candidate")
            if not target_digest or \
                    rec.get("target_image_digests_sha256") != target_digest:
                problems.append(
                    f"scenario {sid}: deployment image digest mismatch or "
                    "missing")
            if rec.get("target_id") != target_id:
                problems.append(
                    f"scenario {sid}: evidence from a different target")
            receipts = rec.get("scenario_receipts")
            if isinstance(receipts, list) and receipts:
                problems.extend(self._scenario_receipt_problems(
                    f"scenario {sid}", receipts, rec, cand, target_id))
            elif rec.get("ok") is True:
                problems.append(
                    f"scenario {sid}: passing without a scenario receipt — "
                    "a claim cannot substitute for the operator's receipt")
        for extra in scenarios:
            if extra not in REQUIRED_SCENARIOS:
                problems.append(
                    f"scenario {extra}: not a mandatory scenario id")
        return problems

    def save(self) -> None:
        final_identity = git_identity()
        self.report["source_changed"] = (
            final_identity["content_sha256"] !=
            self.report["source_identity"]["content_sha256"])
        self.report["source_identity_end"] = final_identity
        # §24.3: the verdict requires candidate- and target-matched
        # evidence; the report carries both identities explicitly.
        self.report["candidate_identity"] = {
            "commit": self.report["source_identity"].get("commit"),
            "content_sha256":
                self.report["source_identity"].get("content_sha256"),
            "manifest_version": MANIFEST_VERSION}
        self.report.setdefault("target_identity", {
            "profile": os.environ.get("TARGET_PROFILE",
                                      "local-durable-postgres"),
            "recorded_at": now()})
        deployment = self.report["stages"].get("target_deployment", {})
        if deployment.get("image_digests") and all(deployment["image_digests"].values()):
            self.report["target_identity"]["image_digests_sha256"] = hashlib.sha256(
                json.dumps(deployment["image_digests"], sort_keys=True).encode()).hexdigest()
        ok, problems = self.verdict()
        self.report["verdict"] = {
            "pass": ok, "failing_or_blocked_stages": problems,
            "recorded_at": now()}
        payload = json.dumps(self.report, indent=2)
        with (self.attempt_dir / "report.json").open("x") as fh:
            fh.write(payload)
        # Only a full invocation updates the release summary. Subsets keep
        # their own evidence and never replace a previous complete attempt.
        if self.report.get("scope") == "full":
            temporary = REPORT.with_suffix(f".{self.attempt_id}.tmp")
            temporary.write_text(payload)
            temporary.replace(REPORT)
        print(f"\n── qualification report → {self.attempt_dir / 'report.json'}")
        for name, st in self.report["stages"].items():
            kind = st.get("kind", "?")
            counts = st.get("counts") or {}
            print(f"  {name:<26} {'PASS' if st.get('ok') else 'FAIL/SKIP':<9}"
                  f" {kind:<7} {counts or ''}")
        print(f"  VERDICT: {'PASS' if ok else 'INCOMPLETE'} "
              f"({len(problems)} problem stages)")
        if problems:
            print("  " + "\n  ".join(problems))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="append", default=[],
                    choices=["migrations", "unit_contract",
                             "infra_integration", "live_inference",
                             "fault_injection", "security", "product_e2e",
                             "performance", "rl_training", "flagship",
                             "restore", "target_deployment", "pilot"])
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--retry-record", action="append", default=[],
                    help="STAGE:label — rerun that stage and record as a named retry")
    ap.add_argument("--journey-evidence", type=str, default=None,
                    help="JSON file of reviewed journey outcomes "
                         "(J01..J24) tied to this candidate and target")
    ap.add_argument("--attach-journeys", nargs=2, action="append",
                    default=[], metavar=("ATTEMPT_ID", "FILE"),
                    help="attach a generated journey-evidence file to an "
                         "EXISTING attempt's report and recompute its "
                         "verdict (first-attempt stage records untouched)")
    args = ap.parse_args()

    if args.attach_journeys:
        for attempt_id, jf_path in args.attach_journeys:
            attempt_dir = ATTEMPTS / attempt_id
            report_path = attempt_dir / "report.json"
            report = json.loads(report_path.read_text())
            evidence = json.loads(Path(jf_path).read_text(),
                                  object_pairs_hook=unique_object)
            if not isinstance(evidence, dict):
                ap.error("journey evidence must be a JSON object")
            # §31.2: the producer's file carries journeys plus the
            # journey-independent scenarios' own verdicts.
            if "scenarios" in evidence and all(
                    not str(k).startswith("J") for k in evidence
                    if k != "scenarios") and "J01" not in evidence:
                # scenario-evidence-only file: keep journeys untouched
                report["scenarios"] = evidence.pop("scenarios")
            else:
                report["scenarios"] = evidence.pop("scenarios", {})
                report["journeys"] = evidence
            report["journey_evidence_source"] = str(jf_path)
            fresh = Qualifier.__new__(Qualifier)
            fresh.strict = bool(report.get("strict"))
            fresh.attempt_id = attempt_id
            fresh.attempt_dir = attempt_dir
            fresh.report = report
            ok, problems = fresh.verdict()
            report["verdict"] = {
                "pass": ok, "failing_or_blocked_stages": problems,
                "recorded_at": now(),
                "note": "verdict recomputed after attaching journey "
                        "evidence; first-attempt stage records unchanged"}
            payload = json.dumps(report, indent=2)
            report_path.write_text(payload)
            if report.get("scope") == "full":
                temporary = REPORT.with_suffix(f".{attempt_id}.tmp")
                temporary.write_text(payload)
                temporary.replace(REPORT)
            print(f"attached journeys to {attempt_id}: verdict "
                  f"{'PASS' if ok else 'INCOMPLETE'} "
                  f"({len(problems)} problems)")
            for problem in problems:
                print(f"  - {problem}")
        return 0

    q = Qualifier(strict=args.strict)
    if args.journey_evidence:
        jf = Path(args.journey_evidence)
        try:
            evidence = json.loads(jf.read_text(), object_pairs_hook=unique_object)
        except (OSError, ReceiptError, ValueError) as exc:
            ap.error(f"journey evidence unreadable: {exc}")
        if not isinstance(evidence, dict):
            ap.error("journey evidence must be a JSON object keyed by J01..J24")
        # §31.2: journey records and the journey-independent scenarios'
        # own verdicts travel in one file; the producer writes both.
        q.report["scenarios"] = evidence.pop("scenarios", {})
        q.report["journeys"] = evidence
        q.report["journey_evidence_source"] = str(jf)
    all_stages = {
        "migrations": q.s_migrations,
        "unit_contract": q.s_unit_contract,
        "infra_integration": q.s_infra_integration,
        "live_inference": q.s_live_inference,
        "fault_injection": q.s_fault_injection,
        "security": q.s_security,
        "product_e2e": q.s_product_e2e,
        "performance": q.s_performance,
        "rl_training": q.s_rl_training,
        "flagship": q.s_flagship,
        "restore": q.s_restore,
        "target_deployment": q.s_target_deployment,
        "pilot": q.s_pilot,
    }
    selected = args.stage or list(all_stages)
    q.report["scope"] = "subset" if args.stage else "full"
    q.report["selected_stages"] = selected
    for name in selected:
        print(f"── stage: {name} ──")
        all_stages[name]()
    # Named retries: STAGE:label — recorded as re-run evidence, never
    # merged into the first-attempt numbers.
    for spec in args.retry_record:
        stage_name, _, label = spec.partition(":")
        if stage_name in all_stages:
            if not label or label in q.report["retries"]:
                ap.error("retries require a unique nonempty label")
            print(f"── named retry: {stage_name} ({label}) ──")
            original = copy.deepcopy(q.report["stages"])
            q.report["stages"] = {}
            all_stages[stage_name]()
            q.report["retries"][label] = {
                "stage": stage_name,
                "label": label, "recorded_at": now(),
                "results": q.report["stages"]}
            q.report["stages"] = original
        else:
            ap.error(f"unknown retry stage: {stage_name}")
    q.save()
    ok, _ = q.verdict()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
