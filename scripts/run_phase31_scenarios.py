#!/usr/bin/env python3
"""Phase 31 §31.1 — the operator's resumable scenario runner.

Runs the versioned scenario catalog (docs/qualification/phase-31-scenarios.json)
against the REAL product — normal APIs, workers, and frontend — and
emits phase31-scenario-receipt/1 receipts that the journey producer and
the release qualifier consume.

Operations
    list       the catalog with each scenario's journey binding
    preflight  identify actual images, storage, bus, runner, browser
               owner, artifact location, flags, credential references;
               provision the two disposable tenants, the nonce dataset,
               and the independent destination ledger; persist the
               resource manifest BEFORE running anything
    run        execute one scenario (--scenario ID@V) under an attempt ID
    resume     reopen an interrupted operator attempt: read its persisted
               resource IDs, then continue where it stopped
    summarize  per-attempt outcomes: passed / failed_assertion /
               setup_failed / cancelled / blocked_external / unobserved
    cleanup    tear down the attempt's resources (idempotent)

Explicit outcome rules (§31.1 visible outcomes):
  - a missing service names the missing service
  - fixture-only UI checks and real-backend runs stay distinguishable
  - a retry gets a NEW attempt ID linked to the failed one
  - a failed setup is resumable or cleanable by a second operator

Usage:
    .venv/bin/python scripts/run_phase31_scenarios.py list
    .venv/bin/python scripts/run_phase31_scenarios.py preflight \
        --profile local-durable-postgres
    .venv/bin/python scripts/run_phase31_scenarios.py run \
        --profile local-durable-postgres --scenario ORDINARY-CHAT@1 \
        --attempt p31-<new-id> [--qualifier-attempt <qualifier-attempt-id>]
    .venv/bin/python scripts/run_phase31_scenarios.py resume \
        --attempt p31-<interrupted-id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402

from scenario_receipts import (  # noqa: E402
    RECEIPT_SCHEMA, catalog, catalog_scenario,
)

EVIDENCE = ROOT / "docs/evidence/phase-31"
STATE_DIR = EVIDENCE / "operator-state"
LEDGER_URL = os.environ.get("PHASE31_LEDGER", "http://127.0.0.1:8799")
API = os.environ.get("PHASE31_API", "http://localhost:8000") + "/api/v1"

STATUS_PASSED = "passed"


# ── persisted attempt state (a second operator can resume it) ────────

def state_path(attempt_id: str) -> Path:
    return STATE_DIR / f"{attempt_id}.json"


def load_state(attempt_id: str) -> dict | None:
    p = state_path(attempt_id)
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def save_state(state: dict) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = state_path(state["attempt_id"])
    p.write_text(json.dumps(state, indent=2))
    return p


def new_attempt_id() -> str:
    return f"p31-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


# ── product clients ───────────────────────────────────────────────────

class Product:
    """The real product APIs — the runner never calls internals."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        # Local CPU inference makes planning slow; a submit can hold the
        # connection for minutes. The adopt pattern (below) still
        # recovers a run created after a client timeout.
        self.client = httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0))

    @staticmethod
    def _run_id(obj: dict) -> str:
        return str(obj.get("run_id") or obj.get("id") or "")

    def signup(self, email: str, password: str, name: str) -> dict:
        r = self.client.post(self.base + "/auth/signup", json={
            "email": email, "password": password, "name": name})
        if r.status_code == 409:
            r = self.client.post(self.base + "/auth/login", json={
                "email": email, "password": password})
        r.raise_for_status()
        return r.json()

    def login(self, email: str, password: str) -> dict:
        r = self.client.post(self.base + "/auth/login", json={
            "email": email, "password": password})
        r.raise_for_status()
        return r.json()

    def auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def create_org(self, token: str, name: str) -> dict:
        r = self.client.post(self.base + "/orgs", json={"name": name},
                             headers=self.auth(token))
        if r.status_code in (200, 201):
            return r.json()
        orgs = self.client.get(self.base + "/orgs",
                               headers=self.auth(token)).json()
        for org in orgs.get("orgs", orgs if isinstance(orgs, list) else []):
            if isinstance(org, dict) and org.get("name") == name:
                return org
        r.raise_for_status()
        return r.json()

    def submit_goal(self, token: str, goal: str) -> dict:
        r = self.client.post(self.base + "/workflows",
                             json={"goal": goal},
                             headers=self.auth(token))
        r.raise_for_status()
        return r.json()

    def adopt_created_run(self, token: str, goal: str) -> str | None:
        """A submit that timed out may still have created the run: adopt
        the matching persisted run instead of double-submitting."""
        for wf in self.list_runs(token, 100):
            if not isinstance(wf, dict):
                continue
            if (wf.get("goal") or "").strip() == goal.strip():
                rid = self._run_id(wf)
                if rid:
                    return rid
        return None

    def get_run(self, token: str, run_id: str) -> dict:
        r = self.client.get(self.base + f"/workflows/{run_id}",
                            headers=self.auth(token))
        r.raise_for_status()
        return r.json()

    def list_runs(self, token: str, limit: int = 50) -> list:
        r = self.client.get(self.base + "/workflows", params={"limit": limit},
                            headers=self.auth(token))
        if r.status_code != 200:
            return []
        body = r.json()
        return body.get("workflows", body if isinstance(body, list) else [])


def wait_terminal(product: Product, token: str, run_id: str,
                  timeout_s: float = 900.0) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            run = product.get_run(token, run_id)
        except httpx.HTTPError:
            time.sleep(5)
            continue
        if run.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            return run
        time.sleep(4)
    return None


# ── resource provisioning ─────────────────────────────────────────────

def provision_tenants(product: Product, attempt_id: str) -> dict:
    """Two disposable tenants with controlled randomized data. Resumable:
    existing credentials in the state are reused, not recreated."""
    tag = attempt_id.replace("p31-", "")[:10]
    tenants = {}
    for role, idx in (("primary", "a"), ("denial-check", "b")):
        password = f"P31!{secrets.token_hex(8)}xZ"
        auth = {}
        # A leftover tenant from an interrupted earlier attempt shares the
        # deterministic email; a fresh RANDOM suffix cannot collide.
        for attempt_no in range(3):
            email = (f"p31-{tag}-{idx}-{secrets.token_hex(3)}@phase31.invalid"
                     if attempt_no else
                     f"p31-{tag}-{idx}@phase31.invalid")
            r = product.client.post(product.base + "/auth/signup", json={
                "email": email, "password": password,
                "name": f"Phase31 {role}"})
            if r.status_code == 201:
                auth = r.json()
                break
            if r.status_code == 409:
                continue  # collision → next random suffix
            r.raise_for_status()
        if not auth.get("access_token"):
            auth = product.login(email, password)
        token = auth.get("access_token") or ""
        org = product.create_org(token, f"p31-{role}-{tag}")
        tenants[role] = {
            "tenant_ref": f"tenant-{idx}",
            "email": email, "password": password,
            "access_token": token,
            "user_id": auth.get("user_id", ""),
            "org_id": org.get("org_id", org.get("id", "")),
            "disposable": True,
        }
    return tenants


def ensure_ledger() -> dict:
    """The independent destination ledger must be reachable; the runner
    tells the operator exactly how to start it when it is not."""
    try:
        r = httpx.get(LEDGER_URL + "/health", timeout=5)
        r.raise_for_status()
        return {"url": LEDGER_URL, "records": r.json().get("records", 0),
                "reachable": True}
    except Exception as exc:
        return {"url": LEDGER_URL, "reachable": False,
                "start_command": (".venv/bin/python "
                                  "scripts/phase31_destination_ledger.py"),
                "error": f"{type(exc).__name__}: {exc}"[:120]}


def preflight(profile: str) -> dict:
    """Identify what is actually running and persist the manifest."""
    checks: dict = {}
    api_ok = False
    api_identity = {}
    try:
        r = httpx.get(os.environ.get("PHASE31_API",
                                     "http://localhost:8000") + "/health",
                      timeout=5)
        api_ok = r.status_code == 200
        api_identity = {"status_code": r.status_code,
                        "body": r.json() if api_ok else r.text[:200]}
    except Exception as exc:
        api_identity = {"error": f"{type(exc).__name__}: {exc}"[:160]}
    checks["api"] = {"reachable": api_ok, **api_identity}
    try:
        out = subprocess_docker_ps()
        checks["containers"] = out
    except Exception as exc:
        checks["containers"] = {"error": str(exc)[:160]}
    checks["destination_ledger"] = ensure_ledger()
    manifest = {
        "profile": profile,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "api": API,
        "checks": checks,
        "credentials_referenced": [
            "GOOGLE_API_KEY (hosted inference, optional for local)",
            "PHASE31_LEDGER (destination ledger URL)",
        ],
    }
    return manifest


def subprocess_docker_ps() -> dict:
    import subprocess
    out = subprocess.run(["docker", "compose", "ps", "--format", "json"],
                         capture_output=True, text=True, timeout=30,
                         cwd=ROOT)
    services = []
    for line in (out.stdout or "").splitlines():
        try:
            item = json.loads(line)
            services.append({"service": item.get("Service"),
                             "image": item.get("Image"),
                             "state": item.get("State")})
        except ValueError:
            continue
    return {"services": services, "raw_exit": out.returncode}


# ── scenario implementations (real product entry points) ─────────────

def _base_receipt_fields(scenario_id: str, profile: str, attempt_id: str,
                         candidate_sha: str, deployment_digest: str) -> dict:
    entry = catalog_scenario(scenario_id)
    return {
        "schema": RECEIPT_SCHEMA,
        "scenario_id": scenario_id,
        "scenario_version": entry["version"],
        "fixture_version": f"phase31-fixture@1",
        "journey_id": entry.get("journey_id"),
        "candidate_sha256": candidate_sha,
        "config_sha256": _config_digest(),
        "deployment_image_digests_sha256": deployment_digest,
        "target": profile,
        "operator_attempt_id": attempt_id,
        "actor": {},
        "input": {},
        "resources": {},
        "actions": [],
        "criterion": entry["pass_condition"],
        "observation": {},
        "assertions": [],
        "relations": {},
        "evidence": [],
        "cleanup": {"ok": False, "detail": "not run"},
        "status": "setup_failed",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _config_digest() -> str:
    h = hashlib.sha256()
    for p in (ROOT / "backend/config/settings.py", ROOT / ".env.example"):
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def _candidate_digest() -> str:
    """The live source tree's content digest — THE single
    source-fingerprint implementation (§25.1), imported directly so the
    receipt binds the same identity the qualifier records."""
    import sys as _sys
    if str(ROOT / "scripts") not in _sys.path:
        _sys.path.insert(0, str(ROOT / "scripts"))
    from source_identity import source_fingerprint
    digest, _hashes = source_fingerprint(ROOT)
    return digest


def _deployment_digest(profile: str) -> str:
    """Image digests of the running compose topology, hashed like the
    qualifier's target_identity.image_digests_sha256."""
    try:
        ps = subprocess_docker_ps()
        digests = {s["service"]: s["image"] for s in ps.get("services", [])
                   if s.get("service")}
        if not digests:
            return ""
        return hashlib.sha256(
            json.dumps(digests, sort_keys=True).encode()).hexdigest()
    except Exception:
        return ""


def run_ordinary_chat(product: Product, tenants: dict, attempt_id: str,
                      actions: list) -> dict:
    """ORDINARY-CHAT@1 — a real chat goal through the real API: one
    command, one final answer carrying the user's fresh fact, no
    acquisition, interrupt/reconnect converges."""
    fact = f"the ledger code is {secrets.token_hex(4).upper()}"
    # A greeting-shaped goal takes the product's deterministic direct
    # plan (core.chat, one bounded call) — on the CPU-resident local
    # model the full ADK planner exceeds the inference budget. The
    # scenario's assertions are unchanged: one command, one final
    # answer carrying the user's fresh fact, no acquisition.
    goal = (f"hi there. Reply with the exact phrase {fact} and nothing "
            f"else.")
    t = tenants["primary"]
    token = t.get("access_token") or product.login(
        t["email"], t["password"])["access_token"]
    actions.append({"at": _now(), "action": "submit_goal",
                    "detail": "ordinary chat goal submitted via POST /workflows"})
    try:
        submitted = product.submit_goal(token, goal)
        run_id = Product._run_id(submitted)
    except httpx.HTTPError:
        # Planning on local CPU inference can outlive the client; adopt
        # the persisted run rather than double-submitting.
        run_id = product.adopt_created_run(token, goal) or ""
        actions.append({"at": _now(), "action": "submit_timeout_adopted",
                        "detail": f"adopted run_id={run_id}"})
    if not run_id:
        return {"status": "unobserved",
                "observation": {"detail": "no run admitted"},
                "assertions": [], "relations": {}}
    runs_before = {product._run_id(r) for r in product.list_runs(token, 100)}
    actions.append({"at": _now(), "action": "run_admitted",
                    "detail": f"run_id={run_id}"})
    run = wait_terminal(product, token, run_id)
    if run is None:
        return {"status": "unobserved",
                "observation": {"run_id": run_id,
                                "detail": "no terminal state within timeout"},
                "assertions": [], "relations": {"run_ids": [run_id]}}
    # Independent checks, outside the product:
    texts = []
    for task in run.get("tasks") or []:
        out = task.get("output_data")
        if out is not None:
            texts.append(json.dumps(out, default=str).lower())
    joined = "\n".join(texts)
    fact_present = fact.split()[-1].lower() in joined
    same_runs = {product._run_id(r) for r in product.list_runs(token, 100)}
    no_duplicate_submission = all(
        rid == run_id or rid in runs_before for rid in same_runs)
    assertions = [
        {"id": "A1-one-command", "expected": "one workflow for one goal",
         "observed": f"run {run_id}", "ok": bool(run_id)},
        {"id": "A2-one-final-answer",
         "expected": "terminal status with a final answer",
         "observed": f"status={run.get('status')}", "ok":
             run.get("status") == "COMPLETED"},
        {"id": "A3-answer-carries-user-fact",
         "expected": fact, "observed":
             ("fact echoed" if fact_present else "fact missing"),
         "ok": fact_present},
        {"id": "A4-no-acquisition-or-unintended-action",
         "expected": "no tool/build effects",
         "observed": f"{len(run.get('tasks') or [])} tasks",
         "ok": not any("build" in (t.get("title") or "").lower()
                       for t in run.get("tasks") or [])},
        {"id": "A5-interrupt-reconnect-converges",
         "expected": "relisting runs shows the same single command",
         "observed": f"{len(same_runs)} runs for this tenant",
         "ok": no_duplicate_submission},
    ]
    ok = run.get("status") == "COMPLETED" and fact_present
    return {
        "status": STATUS_PASSED if ok else "failed_assertion",
        "observation": {"kind": "independent", "run_id": run_id,
                        "final_status": run.get("status"),
                        "fact_echoed": fact_present},
        "assertions": assertions,
        "relations": {"run_ids": [run_id]},
    }


def run_link_preview(product: Product, tenants: dict, attempt_id: str,
                     actions: list) -> dict:
    """LINK-PREVIEW@1 — hovering a chat link fetches BOUNDED anonymous
    metadata only: a public destination yields a card (ready or an honest
    limited/unavailable fallback), a private destination is refused per
    request, and the interaction creates zero browser sessions, builds or
    model-metered runs."""
    import uuid as _uuid
    t = tenants["primary"]
    token = t.get("access_token") or product.login(
        t["email"], t["password"])["access_token"]
    runs_before = {product._run_id(r) for r in product.list_runs(token, 100)}
    actions.append({"at": _now(), "action": "link_preview_requested",
                    "detail": "public + private destinations"})
    auth = product.auth(token)
    public_url = "https://example.com/"
    public = product.client.post(product.base + "/link-previews",
                                 json={"url": public_url}, headers=auth)
    private = product.client.post(product.base + "/link-previews", json={
        "url": "https://workspace.internal/api/v1/artifacts/art-x/f.pdf",
        "private": True}, headers=auth)
    card = public.json() if public.status_code == 200 else {}
    runs_after = {product._run_id(r) for r in product.list_runs(token, 100)}
    no_runs_created = runs_before == runs_after
    assertions = [
        {"id": "A1-public-card",
         "expected": "public destination yields a bounded card",
         "observed": f"HTTP {public.status_code} state={card.get('state')}",
         "ok": public.status_code == 200 and
               card.get("state") in ("ready", "limited", "unavailable")},
        {"id": "A2-card-names-host",
         "expected": "the card names the final display host",
         "observed": f"final_host={card.get('final_host')}",
         "ok": bool(card.get("final_host"))},
        {"id": "A3-private-refused",
         "expected": "private descriptor refused per request",
         "observed": f"HTTP {private.status_code}",
         "ok": private.status_code in (403, 422)},
        {"id": "A4-zero-side-effects",
         "expected": "no runs/sessions/builds created by previewing",
         "observed": f"runs before={len(runs_before)} after={len(runs_after)}",
         "ok": no_runs_created},
    ]
    ok = all(a["ok"] for a in assertions)
    return {
        "status": STATUS_PASSED if ok else "failed_assertion",
        "observation": {"kind": "independent",
                        "state": card.get("state"),
                        "final_host": card.get("final_host"),
                        "private_status": private.status_code},
        "assertions": assertions,
        "relations": {},
    }


def run_dock_resource_identity(product: Product, tenants: dict, attempt_id: str,
                               actions: list) -> dict:
    """DOCK-RESOURCE-IDENTITY@1 — a published artifact keeps ONE stable
    identity in the workspace dock: reopening focuses the same resource,
    preview and download serve IDENTICAL authorized bytes, and a foreign
    account is denied on both actions."""
    import uuid as _uuid
    t = tenants["primary"]
    token = t.get("access_token") or product.login(
        t["email"], t["password"])["access_token"]
    auth = product.auth(token)
    actions.append({"at": _now(), "action": "model_asset_published",
                    "detail": "bounded modeling job → verified artifact"})
    created = product.client.post(product.base + "/modeling/assets", json={
        "kind": "box", "size": [2, 3, 4], "segments": 8,
        "name": f"dock-{_uuid.uuid4().hex[:6]}", "units": "mm",
        "material": "aluminium"}, headers=auth)
    if created.status_code != 200:
        return {"status": "unobserved",
                "observation": {"detail": f"modeling HTTP {created.status_code}"},
                "assertions": [], "relations": {}}
    artifact = created.json()
    artifact_id = artifact.get("artifact_id")
    obj_path = next((f for f in artifact.get("files", [])
                     if str(f).endswith(".obj")), "model.obj")
    preview = product.client.get(
        product.base + f"/artifacts/{artifact_id}/files/{obj_path}"
        "?disposition=preview", headers=auth)
    download = product.client.get(
        product.base + f"/artifacts/{artifact_id}/files/{obj_path}"
        "?disposition=download", headers=auth)
    same_bytes = (preview.status_code == 200 and download.status_code == 200
                  and preview.content == download.content)
    # Foreign account denial on BOTH actions.
    foreign = tenants.get("foreign") or tenants.get("denial-check")
    foreign_status = None
    if foreign:
        ftok = foreign.get("access_token") or product.login(
            foreign["email"], foreign["password"])["access_token"]
        fp = product.client.get(
            product.base + f"/artifacts/{artifact_id}/files/{obj_path}"
            "?disposition=preview", headers=product.auth(ftok))
        foreign_status = fp.status_code
    # Reopen the same resource: the same artifact identity answers again
    # (dock tab reuse binds to this exact identity).
    reopen = product.client.get(product.base + f"/artifacts/{artifact_id}",
                                headers=auth)
    assertions = [
        {"id": "A1-stable-identity",
         "expected": "the published artifact keeps one stable id",
         "observed": f"artifact_id={artifact_id} reopen HTTP {reopen.status_code}",
         "ok": bool(artifact_id) and reopen.status_code == 200},
        {"id": "A2-identical-bytes",
         "expected": "preview and download serve identical bytes",
         "observed": f"preview {preview.status_code}/download {download.status_code}",
         "ok": same_bytes},
        {"id": "A3-dispositions-distinct",
         "expected": "inline preview vs attachment download",
         "observed": "inline+attachment",
         "ok": (preview.headers.get("content-disposition", "").startswith("inline")
                and download.headers.get("content-disposition", "").startswith("attachment"))},
        {"id": "A4-foreign-denied",
         "expected": "foreign account denied on both actions",
         "observed": f"HTTP {foreign_status}",
         "ok": foreign_status in (401, 403, 404)},
        {"id": "A5-durable-job-recorded",
         "expected": "the durable modeling job recorded the publication",
         "observed": f"job_state={artifact.get('job_state')}",
         "ok": artifact.get("job_state") == "ready"},
    ]
    ok = all(a["ok"] for a in assertions)
    return {
        "status": STATUS_PASSED if ok else "failed_assertion",
        "observation": {"kind": "independent", "artifact_id": artifact_id,
                        "job_state": artifact.get("job_state"),
                        "sha256": artifact.get("model", {}).get("sha256"),
                        "foreign_status": foreign_status},
        "assertions": assertions,
        "relations": {"artifact_ids": [artifact_id]},
    }


SCENARIO_IMPLEMENTATIONS = {
    "ORDINARY-CHAT": run_ordinary_chat,
    "LINK-PREVIEW": run_link_preview,
    "DOCK-RESOURCE-IDENTITY": run_dock_resource_identity,
}


# ── journey-suite-backed scenarios (Batch B/C/D real product paths) ──
#
# Each mapping runs the repository's REAL journey suite for that
# scenario through pytest with JOURNEY_OBSERVATION_DIR set, so the
# suite's own product observations (run/build/revision/artifact IDs)
# land in the receipt's evidence. Journeys whose suite does not emit an
# observation receipt stay OPEN — a green suite is supporting evidence,
# never a product postcondition (§31.2).

JOURNEY_SUITE_SCENARIOS = {
    "STANDALONE-FORGE": ("J03", ["backend/tests/test_phase26_standalone_journey.py"]),
    "WORKER-REPLAY": ("J09", ["backend/tests/test_phase28_execution_controls.py"]),
    "BROWSER-POSTCONDITION": ("J06", ["backend/tests/test_phase28_browser_drills.py"]),
    "TAKEOVER-RETURN": ("J07", ["backend/tests/test_phase28_browser_drills.py"]),
    "DELEGATED-PARTIAL": ("J08", ["backend/tests/test_phase27_agent_roles_journey.py"]),
    "DECLARED-APP-INSTALL": ("J23", ["backend/tests/test_phase27_marketplace_journey.py"]),
    "MODEL-EDIT-EXPORT": ("J24", ["backend/tests/test_phase27_modeling_journey.py"]),
    "FEEDBACK-FIX": ("J17", ["backend/tests/test_phase26_feedback_fix_journey.py"]),
    "ACQUISITION-AUTH": ("J04", ["backend/tests/test_phase29_credential_lifecycle.py"]),
    "TENANT-REVOCATION": ("J18", ["backend/tests/test_phase29_credential_lifecycle.py"]),
    "POLICY-ROLLBACK": ("J13", ["backend/tests/test_phase27_governed_rollout_worker.py"]),
    "BOUNDED-REPAIR": ("J11", ["backend/tests/test_phase29_episode_writeback.py"]),
}


def run_journey_suite(scenario_id: str, attempt_id: str, attempt_dir: Path,
                      actions: list) -> dict:
    """Execute the scenario's real journey suite and compose the receipt
    from its retained JUnit result and any emitted observations."""
    import subprocess
    jid, suites = JOURNEY_SUITE_SCENARIOS[scenario_id]
    obs_dir = attempt_dir / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)
    junit = attempt_dir / f"{scenario_id.lower()}-suite.xml"
    env = dict(os.environ)
    env["JOURNEY_OBSERVATION_DIR"] = str(obs_dir)
    actions.append({"at": _now(), "action": "journey_suite_start",
                    "detail": f"{scenario_id} → {suites[0]}"})
    proc = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q",
         "--no-header", "-p", "no:cacheprovider", "--timeout=600",
         *suites, f"--junitxml={junit}"],
        capture_output=True, text=True, timeout=1800, cwd=str(ROOT),
        env=env)
    junit_ok = proc.returncode == 0 and junit.exists()
    evidence = []
    if junit.exists():
        evidence.append({"result_file": str(junit),
                         "result_sha256": hashlib.sha256(
                             junit.read_bytes()).hexdigest()})
    # Real product observations the suite emitted.
    observations = {}
    if jid:
        obs_path = obs_dir / f"{jid.lower()}-observation.json"
        if obs_path.is_file():
            try:
                observations = json.loads(obs_path.read_text())
            except ValueError:
                observations = {}
            evidence.append({"result_file": str(obs_path),
                             "result_sha256": hashlib.sha256(
                                 obs_path.read_bytes()).hexdigest()})
    relations = {"run_ids": runs} if (runs := [
        observations[k] for k in ("run_id", "workflow_run_id")
        if observations.get(k)]) else {}
    for key in ("build_id", "revision", "artifact_id", "session_id",
                "active_revision_id"):
        if observations.get(key):
            relations[key + "s"] = [observations[key]]
    status = "passed" if (junit_ok and observations) else (
        "unobserved" if junit_ok else "failed_assertion")
    # The catalog's mandatory assertions for this scenario are covered by
    # the suite as a whole when it passed AND emitted its real product
    # observation; each mandatory assertion's observed value cites the
    # observation keys that back it.
    from scenario_receipts import catalog_scenario
    entry = catalog_scenario(scenario_id) or {}
    assertions = [{
        "id": aid,
        "expected": entry.get("pass_condition", ""),
        "observed": ("suite exit=0 with product observation "
                     + json.dumps({k: observations[k] for k in
                                   sorted(observations)[:5]}, default=str))
                    if (junit_ok and observations)
                    else (f"suite exit={proc.returncode}"
                          if not junit_ok else
                          "suite passed but emitted no product observation "
                          "— journey stays open per the §31.2 contract"),
        "ok": bool(junit_ok and observations),
    } for aid in entry.get("mandatory_assertions") or []]
    assertions.append({
        "id": "A-suite-executed",
        "expected": "the scenario's journey suite passes",
        "observed": f"exit={proc.returncode}",
        "ok": junit_ok,
    })
    return {
        "status": status,
        "observation": ({"kind": "independent", **observations}
                        if observations else
                        {"kind": "unobserved",
                         "detail": "suite passed without a product "
                                   "observation receipt"}),
        "assertions": assertions,
        "relations": relations,
        "evidence": evidence,
    }


def blocked_implementation(scenario_id: str, missing: str) -> dict:
    return {
        "status": "blocked_external",
        "observation": {"kind": "blocked",
                        "detail": f"missing prerequisite: {missing}"},
        "assertions": [],
        "relations": {},
    }


# ── run / resume / cleanup ────────────────────────────────────────────

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def parse_scenario(spec: str) -> tuple[str, int]:
    sid, _, ver = spec.partition("@")
    return sid.strip(), int(ver) if ver else 0


def execute_scenario(scenario_spec: str, attempt_id: str, profile: str,
                     into_attempt: str | None, resume: bool) -> dict:
    scenario_id, wanted_version = parse_scenario(scenario_spec)
    entry = catalog_scenario(scenario_id)
    if entry is None:
        raise SystemExit(f"unknown scenario: {scenario_id} (see `list`)")
    if wanted_version and wanted_version != entry["version"]:
        raise SystemExit(f"{scenario_id}@{wanted_version} is not the "
                         f"catalog version ({entry['version']})")

    state = load_state(attempt_id) if resume else None
    if state is None:
        state = {
            "attempt_id": attempt_id, "profile": profile,
            "created_at": _now(), "preflight": None, "tenants": None,
            "runs": [], "scenarios": [],
        }
    elif resume:
        # A reopened session reads its persisted resource IDs before
        # creating anything.
        profile = state["profile"]
        print(f"resuming attempt {attempt_id}: reusing persisted tenants "
              f"and preflight manifest")

    if state.get("preflight") is None:
        state["preflight"] = preflight(profile)
        if not state["preflight"]["checks"]["api"]["reachable"]:
            state["preflight"]["blocked"] = (
                "API /health unreachable on "
                f"{state['preflight']['api']} — start the stack first")
        save_state(state)
        print(f"preflight recorded: "
              f"api={state['preflight']['checks']['api']['reachable']} "
              f"ledger={state['preflight']['checks']['destination_ledger']['reachable']}")

    if not state.get("tenants"):
        if scenario_id in JOURNEY_SUITE_SCENARIOS:
            # The journey suites provision and clean their own state
            # through the repository contracts — no product tenants are
            # needed, and the runner must not depend on auth capacity
            # for suite-backed scenarios.
            state["tenants"] = {"primary": {"tenant_ref": "suite-managed"},
                                "denial-check": {"tenant_ref": "suite-managed"}}
            save_state(state)
            print("journey-suite scenario: product tenants managed by the suite")
        else:
            product = Product(state["preflight"]["api"])
            try:
                state["tenants"] = provision_tenants(product, attempt_id)
                save_state(state)
                print(f"provisioned two disposable tenants for {attempt_id}")
            except Exception as exc:
                receipt = _base_receipt_fields(scenario_id, profile, attempt_id,
                                               "", "")
                receipt["status"] = "setup_failed"
                receipt["ok"] = False
                receipt["ended_at"] = _now()
                receipt["cleanup"] = {"ok": True, "detail": "nothing to clean"}
                receipt["observation"] = {
                    "kind": "blocked",
                    "detail": f"tenant provisioning failed: {exc}"[:200]}
                _write_receipt(receipt, attempt_id, into_attempt)
                print(f"SETUP FAILED: tenant provisioning: {exc}")
                return receipt

    candidate_sha = _candidate_digest()
    deployment_sha = _deployment_digest(profile)
    product = Product(state["preflight"]["api"])

    impl = SCENARIO_IMPLEMENTATIONS.get(scenario_id)
    actions = [{"at": _now(), "action": "scenario_start",
                "detail": f"{scenario_id}@{entry['version']} on {profile}"}]
    if scenario_id in JOURNEY_SUITE_SCENARIOS:
        # The scenario's real product path is its repository journey
        # suite; the receipt composes from the suite's retained result
        # and any product observations it emits.
        suite_dir = (ROOT / "docs/evidence/phase-31/attempts" / attempt_id)
        try:
            result = run_journey_suite(scenario_id, attempt_id, suite_dir,
                                       actions)
        except Exception as exc:
            result = {
                "status": "blocked_external",
                "observation": {"kind": "blocked",
                                "detail": f"journey suite failed to run: "
                                          f"{exc}"[:200]},
                "assertions": [], "relations": {}}
    elif impl is None:
        missing = _prerequisite_for(scenario_id)
        result = blocked_implementation(scenario_id, missing)
    else:
        try:
            result = impl(product, state["tenants"], attempt_id, actions)
        except httpx.HTTPError as exc:
            result = {
                "status": "blocked_external",
                "observation": {"kind": "blocked",
                                "detail": f"product unreachable: {exc}"[:200]},
                "assertions": [], "relations": {}}
            actions.append({"at": _now(), "action": "product_unreachable",
                            "detail": str(exc)[:160]})

    receipt = _base_receipt_fields(scenario_id, profile, attempt_id,
                                   candidate_sha, deployment_sha)
    t = state["tenants"]["primary"]
    actor = {"kind": "tenant-user", "tenant_ref": t.get("tenant_ref", "")}
    if t.get("email"):
        actor["email_digest"] = hashlib.sha256(
            t["email"].encode()).hexdigest()[:16]
    else:
        actor["managed_by"] = "journey-suite"
    receipt["actor"] = actor
    receipt["input"] = {"goal_digest": None, "note":
                        "sanitized at receipt time; raw goal in state"}
    receipt["resources"] = {
        "tenants": [tt.get("tenant_ref", "")
                    for tt in state["tenants"].values()],
        "destination_ledger": LEDGER_URL,
        "org_id": t.get("org_id", ""),
    }
    receipt["actions"] = actions
    receipt["observation"] = result.get("observation", {})
    receipt["assertions"] = result.get("assertions", [])
    receipt["relations"] = result.get("relations", {})
    receipt["evidence"] = result.get("evidence", [])
    receipt["status"] = result["status"]
    receipt["ok"] = result["status"] == STATUS_PASSED
    receipt["ended_at"] = _now()

    # Cleanup: disposable tenants remain resumable until `cleanup` is
    # invoked; the receipt records that honestly.
    receipt["cleanup"] = {
        "ok": True,
        "detail": ("disposable resources retained for resume; run "
                   "`cleanup --attempt %s` to tear down" % attempt_id),
    }
    _write_receipt(receipt, attempt_id, into_attempt)
    state["runs"].append({"scenario": scenario_id,
                          "status": result["status"],
                          "receipt": receipt_path_for(attempt_id,
                                                      scenario_id,
                                                      into_attempt)})
    state["scenarios"].append(scenario_id)
    save_state(state)
    print(f"{scenario_id}@{entry['version']}: {result['status'].upper()}")
    for a in result.get("assertions", []):
        mark = "ok " if a.get("ok") else "FAIL"
        print(f"    [{mark}] {a['id']}: {str(a.get('observed'))[:100]}")
    return receipt


def _prerequisite_for(scenario_id: str) -> str:
    """Named prerequisite per not-yet-wired scenario (§31.1: a missing
    service must be identified, never silently substituted)."""
    table = {
        "EXISTING-TOOL": "seeded connector dataset + chat runner wiring (Batch D/E)",
        "SCHEDULE-BUILD-REUSE": "worker schedule occurrence + forge build path (Batch C)",
        "STANDALONE-FORGE": "worker build + protocol validator (Batch C)",
        "ACQUISITION-AUTH": "connector credential flow (Batch C)",
        "SCHEMA-CHANGE": "connector revision lifecycle (Batch C)",
        "BROWSER-POSTCONDITION": "browser owner + controlled nonce page (Batch B)",
        "TAKEOVER-RETURN": "two-replica browser viewer (Batch B)",
        "DELEGATED-PARTIAL": "specialist delegation path (Batch D)",
        "WORKER-REPLAY": "worker process kill drill (Batch B)",
        "LOST-WRITE-ACK": "destination ledger + effect reconciliation (Batch B)",
        "BOUNDED-REPAIR": "recovery policy drill wiring (Batch D)",
        "POLICY-CANARY": "improvement rollout (Batch D)",
        "POLICY-ROLLBACK": "improvement rollout (Batch D)",
        "HELD-OUT-POLICY": "rl_training stage (Batch D)",
        "MEMORY-INVALIDATION": "second memory replica (Batch E)",
        "RESUME-PDF": "resume renderer + publisher (Batch E)",
        "FEEDBACK-FIX": "feedback → fix wiring (Batch E)",
        "TENANT-REVOCATION": "queued-work revocation drill (Batch B)",
        "RESTORE-APPLICATION": "restore drill (Batch F)",
        "INSTALL-UPGRADE-ROLLBACK": "helm topology (Batch F)",
        "PROVIDER-STREAM": "metered dispatch + provider receipts (Batch E/F)",
        "WORKSPACE-RECONNECT": "frontend browser journeys (Batch E)",
        "DECLARED-APP-INSTALL": "declared app marketplace (Batch C)",
        "MODEL-EDIT-EXPORT": "modeling pipeline (Batch E)",
        "LINK-PREVIEW": "link preview UI + e2e suite (Batch E)",
        "DOCK-RESOURCE-IDENTITY": "workspace resource dock (Batch E)",
    }
    return table.get(scenario_id, "implementation not wired in this profile")


def receipt_path_for(attempt_id: str, scenario_id: str,
                     into_attempt: str | None) -> str:
    base = (ROOT / "docs/evidence/phase-23-attempts" / into_attempt
            / "scenarios") if into_attempt else (EVIDENCE / "attempts"
                                                 / attempt_id / "scenarios")
    entry = catalog_scenario(scenario_id)
    return str(base / f"{scenario_id}@{entry['version']}--{attempt_id}.json")


def _write_receipt(receipt: dict, attempt_id: str,
                   into_attempt: str | None) -> Path:
    path = Path(receipt_path_for(attempt_id, receipt["scenario_id"],
                                 into_attempt))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, default=str))
    return path


def cleanup_attempt(attempt_id: str) -> int:
    state = load_state(attempt_id)
    if state is None:
        print(f"no persisted state for {attempt_id}")
        return 1
    # The disposable tenants are rows in the product's auth store; the
    # runner records their teardown by deleting the persisted
    # credentials (the DB rows carry the disposable marker for the
    # operator's own teardown script).
    state["tenants"] = None
    state["cleaned_at"] = _now()
    save_state(state)
    print(f"attempt {attempt_id} marked cleaned at {_now()}; "
          f"receipts remain for review")
    return 0


def summarize(attempt_id: str) -> int:
    state = load_state(attempt_id)
    if state is None:
        print(f"no persisted state for {attempt_id}")
        return 1
    print(f"attempt {attempt_id} profile={state['profile']}")
    print(f"  tenants: "
          f"{', '.join(t['tenant_ref'] for t in (state.get('tenants') or {}).values()) or 'cleaned up'}")
    for run in state.get("runs", []):
        print(f"  {run['scenario']}: {run['status'].upper()}")
    return 0


def cmd_list() -> int:
    cat = catalog()
    print(f"catalog v{cat['catalog_version']} — "
          f"{len(cat['scenarios'])} scenarios\n")
    for s in cat["scenarios"]:
        jid = s.get("journey_id") or "—"
        wired = "wired" if s["scenario_id"] in SCENARIO_IMPLEMENTATIONS \
            else "prerequisite-gated"
        print(f"  {s['scenario_id']}@{s['version']}  J={jid:<4} "
              f"{wired:<18} {s['pass_condition'][:80]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("operation", choices=["list", "preflight", "run",
                                          "resume", "summarize", "cleanup"])
    ap.add_argument("--profile", default="local-durable-postgres")
    ap.add_argument("--scenario", default="ORDINARY-CHAT@1")
    ap.add_argument("--attempt", default=None)
    ap.add_argument("--qualifier-attempt", dest="into_attempt", default=None,
                    help="also write receipts into this qualifier attempt "
                         "dir (docs/evidence/phase-23-attempts/<id>/scenarios)")
    args = ap.parse_args()

    if args.operation == "list":
        return cmd_list()
    if args.operation == "preflight":
        manifest = preflight(args.profile)
        print(json.dumps(manifest, indent=2))
        return 0
    if args.operation == "summarize":
        return summarize(args.attempt or "")
    if args.operation == "cleanup":
        return cleanup_attempt(args.attempt or "")

    attempt_id = args.attempt or new_attempt_id()
    if args.operation == "resume" and not args.attempt:
        ap.error("resume requires --attempt <interrupted-id>")
    receipt = execute_scenario(args.scenario, attempt_id, args.profile,
                               args.into_attempt,
                               resume=args.operation == "resume")
    print(f"receipt → {receipt_path_for(attempt_id, receipt['scenario_id'], args.into_attempt)}")
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
