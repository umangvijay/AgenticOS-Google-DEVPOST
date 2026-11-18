#!/usr/bin/env python3
"""Phase 27 §27.R2 / Phase 32 §32.9 — the generated STANDALONE MCP
journey through the DEPLOYED stack (separate API and worker containers
on the durable PostgreSQL profile).

Journey: sign up through the deployed API → POST /integrations/
build-python-tools with a fresh module and random expected values → the
DEPLOYED runner container builds the artifact in the isolated runtime →
the protocol validator observes real stdio exchanges → activation → a
workflow through the deployed planner invokes the generated tool and
the parent's TYPED output equals the expected total (structural check —
a numeric substring does not satisfy the postcondition) → a SECOND
invocation with FRESH independently derived values must return the
freshly computed total → a build with a WRONG expected value must FAIL
(the validator refuses workload claims).

Outcome: JSON receipt on stdout; exit 0 only when every stage passed.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("DEPLOYED_API", "http://localhost:8000").rstrip("/")
API = BASE + "/api/v1"


def main() -> int:
    receipt: dict = {"journey": "standalone_deployed_mcp", "stages": {}}
    client = httpx.Client(timeout=60)

    email = f"r2-{uuid.uuid4().hex[:8]}@example.com"
    password = f"Journey!{uuid.uuid4().hex[:8]}xZ"
    r = client.post(API + "/auth/signup", json={
        "email": email, "password": password, "name": "R2 Journey"})
    if r.status_code != 201:
        r = client.post(API + "/auth/login", json={"email": email,
                                                   "password": password})
    r.raise_for_status()
    token = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    receipt["stages"]["auth"] = "ok"

    values = [int(uuid.uuid4().hex[:4], 16) % 1000 - 500,
              int(uuid.uuid4().hex[:4], 16) % 1000 - 500, 42]
    expected = {"total": sum(values)}
    module = ("def ledger_total(entries):\n"
              "    if not entries:\n"
              "        raise ValueError('No ledger entries')\n"
              "    return {'total': sum(entries)}\n")
    tools = [{"name": "ledger_total",
              "description": "Total supplied ledger entries",
              "inputSchema": {"type": "object",
                              "properties": {"entries": {
                                  "type": "array",
                                  "items": {"type": "number"}}},
                              "required": ["entries"],
                              "additionalProperties": False}}]
    validation = {"ledger_total": {
        "valid_arguments": {"entries": values},
        "expected": expected,
        "invalid_arguments": {"entries": "wrong type"},
        "error_arguments": {"entries": []}}}

    def request_build(expected_total: dict) -> str:
        body = {"module": module, "tools": tools,
                "validation": {**validation,
                               "ledger_total": {**validation["ledger_total"],
                                                "expected": expected_total}},
                "name": f"Deployed ledger {uuid.uuid4().hex[:6]}"}
        r = client.post(API + "/integrations/build-python-tools",
                        json=body, headers=auth)
        r.raise_for_status()
        return r.json()["build_id"]

    def await_build(build_id: str, timeout_s: float = 240.0) -> dict:
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            r = client.get(API + f"/integrations/builds/{build_id}",
                           headers=auth)
            r.raise_for_status()
            last = r.json()
            if last.get("status") in ("success", "error"):
                return last
            time.sleep(2)
        raise RuntimeError(f"build {build_id} did not settle: {last}")

    # ── 1. The REAL build through the deployed runner ────────────
    build_id = request_build(expected)
    receipt["build_id"] = build_id
    build = await_build(build_id)
    if build.get("status") != "success":
        receipt["stages"]["build"] = {"status": build.get("status"),
                                      "error": build.get("error"),
                                      "logs": (build.get("logs") or [])[-6:]}
        print(json.dumps(receipt, indent=2))
        return 1
    mcp_id = build.get("mcp_id")
    receipt["stages"]["build"] = {"status": "success", "mcp_id": mcp_id}

    detail = client.get(API + f"/integrations/{mcp_id}", headers=auth)
    detail.raise_for_status()
    mcp = detail.json()
    evidence = (mcp.get("metadata") or {}).get("protocol_evidence") or {}
    if evidence.get("status") != "passed" or not mcp.get("active_revision_id"):
        receipt["stages"]["activation"] = {
            "active_revision_id": mcp.get("active_revision_id"),
            "evidence": evidence}
        print(json.dumps(receipt, indent=2))
        return 1
    receipt["stages"]["activation"] = {
        "active_revision_id": mcp.get("active_revision_id"),
        "protocol_evidence": {"status": evidence.get("status"),
                              "execution_id": evidence.get("execution_id")}}

    def find_typed_total(run: dict) -> "int | str | None":
        """§32.9: the parent's typed result — an output_data that IS the
        structured {"total": N} (or carries it as the task's own result
        value). A number appearing anywhere inside serialized text is NOT
        the typed postcondition; the check is structural."""
        for t in run.get("tasks") or []:
            out = t.get("output_data")
            if isinstance(out, dict) and out.get("total") is not None:
                return out["total"]
            if isinstance(out, dict) and isinstance(out.get("data"), dict) \
                    and out["data"].get("total") is not None:
                return out["data"]["total"]
        return None

    def invoke_tool(entries: "list[int]") -> "tuple[str, dict]":
        """One invocation through the deployed planner path; returns
        (run_id, terminal run dict)."""
        goal = (f"Use the tool {mcp_id}__ledger_total with the entries "
                f"{entries} and report the exact total it returns.")
        r = client.post(API + "/workflows", json={"goal": goal},
                        headers=auth)
        r.raise_for_status()
        rid = r.json()["run_id"]
        deadline = time.time() + 420
        run = None
        while time.time() < deadline:
            rr = client.get(API + f"/workflows/{rid}", headers=auth)
            rr.raise_for_status()
            run = rr.json()
            if run.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            time.sleep(3)
        return rid, (run or {"status": "TIMEOUT"})

    # ── 2. Invoke through the deployed planner path ──────────────
    run_id, run = invoke_tool(values)
    receipt["invocation_run_id"] = run_id
    if run.get("status") != "COMPLETED":
        receipt["stages"]["invocation"] = {
            "status": run.get("status"),
            "tasks": [{"agent": t.get("agent"), "status": t.get("status")}
                      for t in run.get("tasks", [])]}
        print(json.dumps(receipt, indent=2))
        return 1
    observed_total = find_typed_total(run)
    if observed_total != expected["total"]:
        receipt["stages"]["invocation"] = {
            "error": ("parent's typed output does not equal the expected "
                      "total"),
            "expected": expected["total"], "observed": observed_total}
        print(json.dumps(receipt, indent=2))
        return 1
    receipt["stages"]["invocation"] = {"status": "COMPLETED",
                                       "total": expected["total"]}

    # ── 2b. FRESH independent values: the same generated tool must
    #      compute the NEW total (computation on arguments, not a
    #      cached/coincidental constant). The scorer derives the expected
    #      value from ITS OWN fresh numbers. ──────────────────────────
    fresh = [int(uuid.uuid4().hex[:4], 16) % 1000 - 500,
             int(uuid.uuid4().hex[:4], 16) % 1000 - 500,
             int(uuid.uuid4().hex[:4], 16) % 1000 - 500]
    fresh_expected = {"total": sum(fresh)}
    fresh_id, fresh_run = invoke_tool(fresh)
    receipt["fresh_invocation_run_id"] = fresh_id
    if fresh_run.get("status") != "COMPLETED":
        receipt["stages"]["fresh_invocation"] = {
            "status": fresh_run.get("status")}
        print(json.dumps(receipt, indent=2))
        return 1
    fresh_observed = find_typed_total(fresh_run)
    if fresh_observed != fresh_expected["total"] or \
            fresh_observed == expected["total"]:
        receipt["stages"]["fresh_invocation"] = {
            "error": ("fresh arguments did not produce the independently "
                      "computed fresh total"),
            "expected": fresh_expected["total"], "observed": fresh_observed}
        print(json.dumps(receipt, indent=2))
        return 1
    receipt["stages"]["fresh_invocation"] = {
        "status": "COMPLETED", "total": fresh_expected["total"],
        "entries": fresh}

    # ── 3. The validator refuses workload claims: a WRONG expected
    #      value must fail the build and leave no active revision ──
    bad_id = request_build({"total": expected["total"] + 1})
    receipt["negative_build_id"] = bad_id
    bad = await_build(bad_id)
    if bad.get("status") != "error":
        receipt["stages"]["negative"] = {
            "error": f"wrong-expected build did not fail: {bad.get('status')}"}
        print(json.dumps(receipt, indent=2))
        return 1
    receipt["stages"]["negative"] = {"status": "error",
                                     "error": (bad.get("error") or "")[:160]}

    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
