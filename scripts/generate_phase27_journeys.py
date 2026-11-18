#!/usr/bin/env python3
"""Phase 27 §27.1/§27.20 — build the J01–J24 journey evidence file the
release qualifier consumes, from REAL observations only:

  - covering stages' retained receipts (hashed) back every journey;
  - J19/J20 carry the product IDs from the restore trace / target
    identity receipts; J22 carries the executed browser spec outcomes;
  - J23/J24 REQUIRE the journey tests' own observation receipts
    (backend.tests.journey_observations.emit) — no synthetic IDs.

Phase 28's scenario-receipt generator (generate_journey_evidence.py)
owns the workspace-journey model; this producer owns the qualifier's
J01–J24 contract. Output: <attempt-dir>/journey-evidence-phase27.json.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from generate_journey_evidence import (  # noqa: E402
    JOURNEY_STAGES, JOURNEY_OUTCOMES, _observation,
    _stage_product_observation,
)

# §27.20: the qualifier's J23/J24 bind to their DEDICATED journey
# result files (the marketplace/modeling journeys) — not unit_contract.
COVERING = {**JOURNEY_STAGES, "J23": ["j23-journey"], "J24": ["j24-journey"]}
from qualify_release import junit_counts  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: generate_phase27_journeys.py <attempt-dir> [--output FILE]")
        return 2
    attempt_dir = Path(sys.argv[1]).resolve()
    report = json.loads((attempt_dir / "report.json").read_text())
    stages = report.get("stages", {})
    cand = (report.get("candidate_identity") or {}).get("content_sha256", "")
    target = (report.get("target_identity") or {}).get("profile", "")
    target_digest = (report.get("target_identity") or {}) \
        .get("image_digests_sha256", "")

    journeys = {}
    for jid in sorted(COVERING):
        outcome_desc, postcondition = JOURNEY_OUTCOMES[jid]
        covering = COVERING.get(jid, [])
        evidence = []
        all_ok = True
        for sid in covering:
            if sid.endswith("-journey"):
                jpath = attempt_dir / f"{sid}.xml"
                if not jpath.is_file():
                    all_ok = False
                    continue
                counts = junit_counts(jpath)
                j_ok = (counts.get("passed", 0) > 0
                        and not any(counts.get(k, 0) for k in
                                    ("failed", "errors", "skipped")))
                all_ok = all_ok and j_ok
                evidence.append({"stage": sid, "ok": j_ok,
                                 "result_file": str(jpath.relative_to(ROOT)),
                                 "result_sha256": hashlib.sha256(
                                     jpath.read_bytes()).hexdigest()})
                continue
            st = stages.get(sid)
            if st is None:
                all_ok = False
                continue
            stage_ok = st.get("ok") is True
            all_ok = all_ok and stage_ok
            evidence.append({"stage": sid, "ok": stage_ok,
                             "result_file": st.get("result_file", ""),
                             "result_sha256": st.get("result_sha256", "")})
        # Real product observations (never synthetic IDs).
        if any(sid.endswith("-journey") for sid in covering):
            obs = _observation(attempt_dir, jid)
        else:
            obs, _obs_path = _stage_product_observation(
                attempt_dir, stages, jid)
        if covering and any(sid.endswith("-journey") or sid in
                            ("restore", "target_deployment")
                            for sid in covering) and obs is None:
            all_ok = False
        obs_path = attempt_dir / f"{jid.lower()}-observation.json"
        if obs and obs_path.is_file():
            evidence.append({"stage": f"{jid.lower()}-observation", "ok": True,
                             "result_file": str(obs_path.relative_to(ROOT)),
                             "result_sha256": hashlib.sha256(
                                 obs_path.read_bytes()).hexdigest()})
        run_id = (obs or {}).get("run_id") \
            or (obs or {}).get("artifact_id") \
            or (obs or {}).get("build_id") or ""
        run_id = f"{jid}:{run_id}" if run_id else \
            (f"{jid}:stage:{next((e['result_sha256'][:12] for e in evidence
                                  if e.get('result_sha256')), 'unobserved')}")
        if "external_observation" in (obs or {}):
            observed = ("external values observed: "
                        + json.dumps(obs["external_observation"],
                                     default=str)[:200])
        elif "independent_reader" in (obs or {}):
            observed = (f"independent reader {obs['independent_reader']}; "
                        f"edit digest {str(obs.get('edit_sha256'))[:16]}…")
        elif (obs or {}).get("final_status"):
            observed = (f"ingress workflow {obs.get('run_id')} ended "
                        f"{obs.get('final_status')}")
        elif (obs or {}).get("checks_passed"):
            observed = (f"restore trace {obs['checks_passed']} checks ok; "
                        f"recovered workflow {obs.get('run_id')}")
        elif (obs or {}).get("browser_specs"):
            observed = (f"{obs['specs_passed']}/{obs['spec_count']} real "
                        "browser journeys passed")
        else:
            observed = (f"covered by {', '.join(covering)}: "
                        + ("all covering stages passed" if all_ok else
                           "a covering stage failed or was blocked"))
        journeys[jid] = {
            "journey_id": jid, "ok": all_ok and bool(evidence),
            "production_action": f"journey {jid}: {outcome_desc}",
            "run_id": run_id, "expected_postcondition": postcondition,
            "observed_result": observed, "candidate_sha256": cand,
            "target_id": target,
            "target_image_digests_sha256": target_digest,
            "evidence": evidence,
        }

    out_path = Path(sys.argv[sys.argv.index("--output") + 1]) \
        if "--output" in sys.argv else \
        attempt_dir / "journey-evidence-phase27.json"
    out_path.write_text(json.dumps(journeys, indent=2))
    passing = sum(1 for j in journeys.values() if j["ok"])
    print(f"phase27 journey evidence → {out_path}")
    print(f"  {passing}/{len(journeys)} journeys have passing evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
