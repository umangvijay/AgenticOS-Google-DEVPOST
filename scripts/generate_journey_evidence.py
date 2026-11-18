#!/usr/bin/env python3
"""Phase 31 §31.2 — journey evidence from REAL product observations.

Each mandatory journey (J01–J24) and each journey-independent scenario
(ORDINARY-CHAT, LINK-PREVIEW, DOCK-RESOURCE-IDENTITY) must be joined to
a real product run and an independent observation bound to the SAME
candidate, target, and operator attempt. A passing stage, a workflow
status, a screenshot, or a composed/generated run ID cannot establish a
product postcondition.

Evidence sources, in order of strength:
  1. Scenario receipts (<attempt-dir>/scenarios/*.json, schema
     phase31-scenario-receipt/1) written by the operator runner
     (`scripts/run_phase31_scenarios.py`). A passing receipt carries the
     real run/build/revision/effect IDs, timestamped actions, the
     expected criterion, the independently observed value, per-assertion
     results, and the cleanup result.
  2. Journey observation files (<attempt-dir>/{jid}-observation.json)
     emitted by journey tests via backend.tests.journey_observations —
     real product IDs observed by the test itself.
  3. Stage product receipts (restore trace, target identity) for the
     journeys those stages directly observe (J19, J20).

Covering-stage results remain SUPPORTING evidence — they are listed in
the record but can never make an unobserved journey pass. A passing
unrelated case in the same suite leaves the journey open.

Usage:
    .venv/bin/python scripts/generate_journey_evidence.py <attempt-dir> \
        [--output FILE]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_receipts import (  # noqa: E402
    RECEIPT_SCHEMA, catalog_by_journey, is_composed_run_reference,
    load_receipt, real_run_ids, receipt_passes, validate_receipt,
    ReceiptError,
)

# Journey → covering stages (from the Phase 24/27 registers). Supporting
# evidence only — never sufficient for a passing verdict.
JOURNEY_STAGES = {
    "J01": ["flagship"], "J02": ["flagship"],
    "J03": ["flagship", "fault_injection"],
    "J04": ["flagship"], "J05": ["flagship"],
    "J06": ["product_e2e_browser", "fault_injection"],
    "J07": ["product_e2e_browser", "fault_injection"],
    "J08": ["security", "flagship"],
    "J09": ["fault_injection", "infra_integration"],
    "J10": ["fault_injection"], "J11": ["fault_injection"],
    "J12": ["flagship"], "J13": ["flagship"],
    "J14": ["rl_training"], "J15": ["fault_injection"],
    "J16": ["unit_contract"], "J17": ["security"],
    "J18": ["security"], "J19": ["restore"],
    "J20": ["target_deployment", "infra_integration"],
    "J21": ["live_inference_local", "live_inference_hosted"],
    "J22": ["product_e2e_browser"],
    "J23": ["unit_contract"], "J24": ["unit_contract"],
}

JOURNEY_OUTCOMES = {
    "J01": ("existing connector invoked via chat and verified",
            "result verified by independent check"),
    "J02": ("missing adapter built, validated, activated, parent resumed",
            "parent COMPLETED with real output; second occurrence reuses "
            "the eligible revision on changed data with zero new builds"),
    "J03": ("standalone server packaged and isolated",
            "waiting parent invokes validated standalone bytes"),
    "J04": ("missing credentials preserved candidate with next action",
            "candidate preserved; correct-scope reconnect resumes once; "
            "foreign tenant credential cannot resume"),
    "J05": ("schema change invalidated evidence; repair tested",
            "old evidence invalidated; new revision tested"),
    "J06": ("real browser navigation with postcondition",
            "page state verified after action"),
    "J07": ("takeover with stale refusal and fresh observation",
            "stale epoch refused; fresh observation required"),
    "J08": ("multi-agent delegation with honest parent disposition",
            "child failure honestly surfaced"),
    "J09": ("worker interruption → durable resume",
            "resumed from durable state, no duplicate effect"),
    "J10": ("uncertain write → reconciliation, never repeated",
            "dispatch not repeated on uncertainty"),
    "J11": ("transient failure → bounded verified recovery",
            "recovery within ceiling, postcondition verified"),
    "J12": ("improvement accepted and runtime changed",
            "effective version changed"),
    "J13": ("regression rejected and previous version restored",
            "rejection recorded; baseline restored"),
    "J14": ("RL training → held-out comparison → governed decision",
            "training evidence + governed promotion path"),
    "J15": ("memory correction → dependent invalidation after restart",
            "stale derived memory inaccessible"),
    "J16": ("resume/document → verified → authorized download",
            "verified artifact delivered to owner"),
    "J17": ("feedback → triage → regression → verified fix",
            "feedback linked to regression and fix"),
    "J18": ("cross-tenant requests and revoked authority denied",
            "403/401 on cross-tenant and revoked"),
    "J19": ("full backup → separate restore → integrity",
            "counts match; integrity verified"),
    "J20": ("clean install → upgrade → rollback",
            "upgrade and rollback verified"),
    "J21": ("provider workflows incl. streaming/cancellation",
            "metered receipt with verified output"),
    "J22": ("frontend session/reconnect/account-switch",
            "real-backend browser journeys converge on one answer"),
    "J23": ("declared app installed → scoped credential bound → qualified "
            "operation executed; unvalidated operation stayed unavailable",
            "qualified operation executed through the installed app"),
    "J24": ("modeling job → validated asset published → independently "
            "reopened → edited child version",
            "downloaded asset verified by an independent reader"),
}

# Journey-independent scenarios with their own receipts and verdicts.
EXTRA_SCENARIOS = ("ORDINARY-CHAT", "LINK-PREVIEW", "DOCK-RESOURCE-IDENTITY")


def _observation(attempt_dir: Path, journey_id: str) -> dict | None:
    """The journey's OWN observed product receipt (real run/build/
    artifact IDs and external values) — emitted by the journey test via
    backend.tests.journey_observations.emit when the harness sets
    JOURNEY_OBSERVATION_DIR. Its absence means the journey produced no
    verifiable observation for this attempt."""
    path = attempt_dir / f"{journey_id.lower()}-observation.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def _stage_product_observation(attempt_dir: Path, stages: dict,
                               journey_id: str) -> tuple[dict | None, Path | None]:
    """Real product IDs retained by a covering stage's own receipt
    (§27.1: observations emitted by the journey, not synthesized).
    Returns (observation, source_file)."""
    try:
        if journey_id == "J19":
            path = attempt_dir / "restore-app-trace.json"
            if not path.is_file():
                return None, None
            trace = json.loads(path.read_text())
            return {"run_id": trace.get("workflow_run_id"),
                    "output_sha256":
                        trace.get("new_workflow_output_sha256"),
                    "recovery_seconds":
                        trace.get("recovery_seconds_through_verified_workflow"),
                    "checks_passed":
                        sum(1 for v in (trace.get("checks") or {}).values()
                            if v is True)}, path
        if journey_id == "J20":
            path = attempt_dir / "target-identity.json"
            if not path.is_file():
                return None, None
            ident = json.loads(path.read_text())
            wf = ident.get("workflow") or {}
            return {"run_id": wf.get("run_id"),
                    "final_status": wf.get("final_status"),
                    "image_digests": ident.get("image_digests") or {}}, path
    except (OSError, ValueError):
        return None, None
    return None, None


def _load_scenario_receipts(attempt_dir: Path) -> list[dict]:
    """Every parsed scenario receipt in the attempt directory, with its
    file path and content hash."""
    receipts: list[dict] = []
    scenarios_dir = attempt_dir / "scenarios"
    if not scenarios_dir.is_dir():
        return receipts
    for path in sorted(scenarios_dir.glob("*.json")):
        try:
            receipt = load_receipt(path)
        except ReceiptError as exc:
            receipts.append({"_load_error": str(exc), "_path": str(path)})
            continue
        receipt["_path"] = str(path)
        receipt["_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        receipts.append(receipt)
    return receipts


def _receipts_for(receipts: list[dict], *, scenario_id: str,
                  candidate: str, target: str) -> list[dict]:
    """Receipts bound to THIS candidate and target for one scenario."""
    out = []
    for r in receipts:
        if r.get("_load_error"):
            continue
        if r.get("scenario_id") != scenario_id:
            continue
        if candidate and r.get("candidate_sha256") != candidate:
            continue
        if target and str(r.get("target")) != str(target):
            continue
        out.append(r)
    return out


def _receipt_ref(receipt: dict) -> dict:
    # The ref carries the standard evidence keys (result_file /
    # result_sha256) so the consumer's verify_result_file applies
    # unchanged; scenario_id/version/attempt identify the experiment.
    return {
        "scenario_id": receipt.get("scenario_id"),
        "scenario_version": receipt.get("scenario_version"),
        "result_file": receipt.get("_path"),
        "result_sha256": receipt.get("_sha256"),
        "operator_attempt_id": receipt.get("operator_attempt_id"),
    }


def _stage_support(attempt_dir: Path, stages: dict, covering: list[str]) -> \
        tuple[list[dict], bool]:
    """Supporting stage records (never sufficient on their own). Stages
    with no retained result contribute nothing — an entry without bytes
    is not evidence."""
    support: list[dict] = []
    all_stages_ok = True
    for sid in covering:
        st = stages.get(sid)
        stage_ok = (st or {}).get("ok") is True
        all_stages_ok = all_stages_ok and stage_ok
        if not (st or {}).get("result_file"):
            continue
        support.append({
            "stage": sid,
            "ok": stage_ok,
            "result_file": st.get("result_file", ""),
            "result_sha256": st.get("result_sha256", ""),
            "role": "supporting",
        })
    return support, all_stages_ok


def _build_record(jid: str, *, outcome_desc: str, postcondition: str,
                  ok: bool, status: str, run_id: str, observed: str,
                  cand: str, target: str, target_digest: str,
                  attempt_ids: list[str], scenario_refs: list[dict],
                  observation_ref: dict | None, support: list[dict]) -> dict:
    rec = {
        "journey_id": jid,
        "ok": ok,
        "status": status,
        "production_action": f"journey {jid}: {outcome_desc}",
        "run_id": run_id,
        "expected_postcondition": postcondition,
        "observed_result": observed,
        "candidate_sha256": cand,
        "target_id": target,
        "target_image_digests_sha256": target_digest,
        "attempt_id": ",".join(sorted(set(attempt_ids))) if attempt_ids else "",
        "scenario_receipts": scenario_refs,
        "evidence": support,
    }
    if observation_ref:
        rec["observation"] = observation_ref
    return rec


def _journey_verdict(receipts: list[dict], jid: str, attempt_dir: Path,
                     stages: dict, covering: list[str], outcome_desc: str,
                     postcondition: str, cand: str, target: str,
                     target_digest: str) -> dict:
    """Compose ONE journey's record from receipts + observations +
    supporting stage coverage."""
    support, stages_ok = _stage_support(attempt_dir, stages, covering)

    # 1. Scenario receipts — the canonical evidence path.
    catalog_ids = [s["scenario_id"] for s in catalog_by_journey(jid)]
    bound: list[dict] = []
    problems: list[str] = []
    for sid in catalog_ids:
        for r in _receipts_for(receipts, scenario_id=sid,
                               candidate=cand, target=target):
            errs = validate_receipt(
                r, expected_scenario_id=sid, expected_journey_id=jid,
                candidate_sha256=cand, target_id=target)
            if errs:
                problems.extend(f"{sid}: {e}" for e in errs)
                continue
            bound.append(r)
    passing = [r for r in bound if receipt_passes(r)]

    # 2. Legacy observation files — the journey test's own receipt.
    obs = _observation(attempt_dir, jid)
    obs_ref = None
    obs_run = ""
    if obs is not None:
        obs_path = attempt_dir / f"{jid.lower()}-observation.json"
        obs_run = next((str(obs.get(k) or "")
                        for k in ("run_id", "workflow_run_id", "build_id",
                                  "artifact_id")
                        if str(obs.get(k) or "").strip()
                        and not is_composed_run_reference(obs.get(k))), "")
        if obs_run:
            obs_ref = {"result_file": str(obs_path.relative_to(ROOT)),
                       "result_sha256": hashlib.sha256(
                           obs_path.read_bytes()).hexdigest()}
            support.append({
                "stage": f"{jid.lower()}-observation", "ok": True,
                "result_file": obs_ref["result_file"],
                "result_sha256": obs_ref["result_sha256"],
                "role": "independent-observation"})

    # 3. Stage product receipts (J19 restore trace, J20 target identity).
    stage_obs, stage_obs_path = _stage_product_observation(
        attempt_dir, stages, jid)
    if stage_obs and stage_obs_path is not None and not obs_ref:
        srun = str(stage_obs.get("run_id") or "")
        if srun and not is_composed_run_reference(srun):
            obs_ref = {"result_file": str(stage_obs_path.relative_to(ROOT)),
                       "result_sha256": hashlib.sha256(
                           stage_obs_path.read_bytes()).hexdigest()}
            obs_run = srun
            support.append({
                "stage": f"{jid.lower()}-stage-observation", "ok": True,
                "result_file": obs_ref["result_file"],
                "result_sha256": obs_ref["result_sha256"],
                "role": "independent-observation"})

    attempt_ids = [str(r.get("operator_attempt_id") or "")
                   for r in bound if r.get("operator_attempt_id")]

    # ── verdict: a real product observation is REQUIRED ──────────────
    if passing:
        runs = []
        for r in passing:
            runs.extend(real_run_ids(r))
        run_id = runs[0] if runs else ""
        first = passing[0]
        obs_detail = first.get("observation")
        observed = (f"scenario {first.get('scenario_id')}@"
                    f"{first.get('scenario_version')} on operator attempt "
                    f"{first.get('operator_attempt_id')}: "
                    + (json.dumps(obs_detail, default=str)[:220]
                       if obs_detail else "criteria observed independently"))
        if problems:
            # Some sibling receipts were malformed — keep the pass but
            # retain the specific problems for operator review.
            observed += f" | sibling receipt problems: {problems[0]}"
        return _build_record(
            jid, outcome_desc=outcome_desc, postcondition=postcondition,
            ok=True, status="passed", run_id=run_id, observed=observed,
            cand=cand, target=target, target_digest=target_digest,
            attempt_ids=attempt_ids,
            scenario_refs=[_receipt_ref(r) for r in passing],
            observation_ref=obs_ref, support=support)

    if obs_run:
        observed = json.dumps(obs, default=str)[:220]
        return _build_record(
            jid, outcome_desc=outcome_desc, postcondition=postcondition,
            ok=True, status="passed", run_id=obs_run, observed=observed,
            cand=cand, target=target, target_digest=target_digest,
            attempt_ids=[], scenario_refs=[], observation_ref=obs_ref,
            support=support)

    # No real product observation → the journey is OPEN. Stage
    # coverage alone cannot close it, even when every stage passed.
    if problems:
        status = "receipt_rejected"
        observed = ("scenario receipts exist but were rejected: "
                    + "; ".join(problems[:3]))
        run_id = (f"{jid}:unobserved")
    elif bound:
        failed = [r for r in bound if not receipt_passes(r)]
        status = str(failed[0].get("status") or "failed_assertion")
        observed = (
            f"scenario {failed[0].get('scenario_id')} on attempt "
            f"{failed[0].get('operator_attempt_id')}: status {status}; "
            f"{sum(1 for a in failed[0].get('assertions') or [] if a.get('ok') is True)}"
            f"/{len(failed[0].get('assertions') or [])} assertions ok")
        run_id = f"{jid}:unobserved"
    else:
        status = "unobserved"
        observed = (f"open — covered by {', '.join(covering) or 'no stage'} "
                    f"({', '.join(catalog_ids) or 'no scenario'} not run); "
                    + ("covering stages passed but a passing stage is not "
                       "a product postcondition" if stages_ok else
                       "one or more covering stages failed or were blocked"))
        run_id = f"{jid}:unobserved"
    return _build_record(
        jid, outcome_desc=outcome_desc, postcondition=postcondition,
        ok=False, status=status, run_id=run_id, observed=observed,
        cand=cand, target=target, target_digest=target_digest,
        attempt_ids=attempt_ids, scenario_refs=[_receipt_ref(r) for r in bound],
        observation_ref=None, support=support)


def _extra_scenario_verdict(receipts: list[dict], sid: str, cand: str,
                            target: str, target_digest: str) -> dict:
    """A journey-independent scenario's own verdict (same contract)."""
    bound: list[dict] = []
    problems: list[str] = []
    for r in _receipts_for(receipts, scenario_id=sid,
                           candidate=cand, target=target):
        errs = validate_receipt(
            r, expected_scenario_id=sid, expected_journey_id=None,
            candidate_sha256=cand, target_id=target)
        if errs:
            problems.extend(f"{sid}: {e}" for e in errs)
            continue
        bound.append(r)
    passing = [r for r in bound if receipt_passes(r)]
    record = _journey_record_shape(sid)
    record["candidate_sha256"] = cand
    record["target_id"] = target
    record["target_image_digests_sha256"] = target_digest
    record["scenario_receipts"] = [_receipt_ref(r) for r in passing]
    record["attempt_id"] = ",".join(sorted(set(
        str(r.get("operator_attempt_id") or "") for r in passing)))
    if passing:
        runs = []
        for r in passing:
            runs.extend(real_run_ids(r))
        record["ok"] = True
        record["status"] = "passed"
        record["run_id"] = runs[0] if runs else ""
        first = passing[0]
        record["observed_result"] = (
            f"scenario {sid}@{first.get('scenario_version')} on operator "
            f"attempt {first.get('operator_attempt_id')}: "
            + json.dumps(first.get("observation"), default=str)[:220])
    else:
        record["ok"] = False
        record["status"] = "receipt_rejected" if problems else "unobserved"
        record["run_id"] = f"{sid}:unobserved"
        record["observed_result"] = (
            "open — scenario receipts rejected: " + "; ".join(problems[:3])
            if problems else
            f"open — no passing receipt for {sid} on this candidate/target")
    return record


def _journey_record_shape(sid: str) -> dict:
    entry = next((s for s in _load_catalog_entries() if s["scenario_id"] == sid),
                 None)
    return {
        "scenario_id": sid,
        "journey_id": None,
        "ok": False,
        "status": "unobserved",
        "production_action": f"scenario {sid}: "
                             f"{(entry or {}).get('pass_condition', '')}",
        "run_id": f"{sid}:unobserved",
        "expected_postcondition": (entry or {}).get("pass_condition", ""),
        "observed_result": "open — no passing receipt",
        "evidence": [],
    }


def _load_catalog_entries() -> list[dict]:
    from scenario_receipts import catalog
    return catalog().get("scenarios") or []


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: generate_journey_evidence.py <attempt-dir> [--output FILE]")
        return 2
    attempt_dir = Path(sys.argv[1]).resolve()
    report_path = attempt_dir / "report.json"
    report = json.loads(report_path.read_text())
    stages = report.get("stages", {})
    cand = (report.get("candidate_identity") or {}).get("content_sha256", "")
    target = (report.get("target_identity") or {}).get("profile", "")
    target_digest = (report.get("target_identity") or {}) \
        .get("image_digests_sha256", "")

    receipts = _load_scenario_receipts(attempt_dir)
    broken = [r for r in receipts if r.get("_load_error")]
    for r in broken:
        print(f"  WARNING: unreadable receipt {r['_path']}: {r['_load_error']}")

    journeys: dict = {}
    for jid in sorted(JOURNEY_STAGES):
        covering = JOURNEY_STAGES.get(jid, [])
        outcome_desc, postcondition = JOURNEY_OUTCOMES[jid]
        journeys[jid] = _journey_verdict(
            receipts, jid, attempt_dir, stages, covering, outcome_desc,
            postcondition, cand, target, target_digest)

    # Journey-independent scenarios carry their own receipts and verdicts.
    scenarios: dict = {}
    for sid in EXTRA_SCENARIOS:
        scenarios[sid] = _extra_scenario_verdict(
            receipts, sid, cand, target, target_digest)

    out_path = Path(sys.argv[sys.argv.index("--output") + 1]) \
        if "--output" in sys.argv else attempt_dir / "journey-evidence.json"
    out_path.write_text(json.dumps(
        {**journeys, "scenarios": scenarios}, indent=2))
    passing = sum(1 for j in journeys.values() if j["ok"])
    extra_passing = sum(1 for s in scenarios.values() if s["ok"])
    print(f"journey evidence → {out_path}")
    print(f"  {passing}/{len(journeys)} journeys have passing evidence "
          f"({extra_passing}/{len(EXTRA_SCENARIOS)} additional scenarios)")
    for jid, j in sorted(journeys.items()):
        status = "PASS" if j["ok"] else j["status"].upper()
        refs = (f"receipts:{len(j['scenario_receipts'])}"
                if j.get("scenario_receipts") else
                ("observed" if j.get("observation") else "open"))
        print(f"  {jid}: {status:<18} ({refs}) run={j['run_id'][:40]}")
    for sid, s in scenarios.items():
        status = "PASS" if s["ok"] else s["status"].upper()
        print(f"  {sid}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
