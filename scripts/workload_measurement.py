#!/usr/bin/env python3
"""Phase 27 §27.19 / §27.R11 / Phase 32 §32.22 — WHOLE-WORKLOAD quality
and latency measurement through the deployed API (admission-only load
testing does NOT establish whole-product performance).

Predeclared BEFORE the run (§00.5 thresholds, docs/release-scope.md):
  - verified completion of the mixed workload >= 85% (hard)
  - zero unauthorized actions (hard)
  - zero duplicate external effects (hard)
  - zero abandoned workflows (every admitted run reaches a terminal state)
  - p50/p95 latency and throughput are REPORTED (tracked, no gate)

§32.22 measurement contract (this revision):
  - The DENOMINATOR is the frozen per-family composition below; every
    planned submission is identified BEFORE dispatch and the queue is
    DRAINED — rejected, throttled, failed, cancelled, abandoned and
    completed alike. Requested work exceeds the thread count by design
    (the fixed thread waves may not drop planned items).
  - Elapsed time and throughput come from OBSERVED wall-clock start/end
    of the drained run, never from max(individual latency).
  - EVERY family's postcondition is verified INDEPENDENTLY of the
    product (the scorer re-observes or derives the expected value
    itself; a COMPLETED status with a wrong output is a verification
    failure, not a completion).
  - An UNAVAILABLE effect ledger is UNKNOWN: runs whose effect-list
    read fails are counted in `effects_unavailable_runs` and are NOT
    silently treated as "no effects".
  - A CORRECTLY DENIED request is distinct from an unauthorized action
    that EXECUTED: cross-tenant reads that succeed are unauthorized;
    outputs that claim a denial are counted as correct denials only
    when no corresponding effect was recorded.
  - Duplicate effects gate the verdict and are counted ONCE per
    (operation, target) per run from the effect ledger.
"""
from __future__ import annotations

import json
import os
import queue
import statistics
import sys
import time
import uuid

import httpx

BASE = os.environ.get("MEASURE_API", "http://localhost:8000").rstrip("/")
API = BASE + "/api/v1"
CONCURRENCY = int(os.environ.get("MEASURE_CONCURRENCY", "3"))
TIMEOUT_S = float(os.environ.get("MEASURE_TIMEOUT_S", "900"))
SOURCE_TEXT = ("AgentOS runs durable workflows across separate API and "
               "worker processes. Every external effect is admitted, "
               "approved when required, and reconciled after delivery. "
               "Budgets are reserved per attempt and settled from "
               "provider receipts.")
HEALTH_URL = "https://example.com"

# ── Frozen per-family composition (Phase 32 §32.1 — declared in
#    docs/release-scope.md before measurement; do not change to obtain
#    a pass). Family kinds:
#      workflow          — a goal through POST /workflows
#      model_asset       — POST /modeling/assets + independent OBJ parse
#      link_preview      — POST /link-previews vs the scorer's own fetch
#      missing_tool      — build-python-tools + fresh-value invocation
#      cross_tenant      — a second tenant must be refused the run
FAMILY_COUNTS = {
    "health_probe": 10,
    "deterministic_echo": 10,
    "model_asset": 4,
    "link_preview": 4,
    "missing_tool_build": 1,
    "cross_tenant_control": 2,
}
# An explicit operator override for a FOCUSED SMOKE (small counts) — the
# frozen composition above is the default and any override is recorded
# in the receipt so a smoke can never be presented as the measured run.
import json as _json
_override = os.environ.get("MEASURE_FAMILY_COUNTS")
if _override:
    FAMILY_COUNTS = {str(k): int(v) for k, v in
                     _json.loads(_override).items()
                     if int(v) > 0}

FAMILY_KIND = {
    "health_probe": "workflow",
    "deterministic_echo": "workflow",
    "model_asset": "model_asset",
    "link_preview": "link_preview",
    "missing_tool_build": "missing_tool",
    "cross_tenant_control": "cross_tenant",
}

HEALTH_GOAL = ("Check health of https://example.com and summarize "
               "the observed result in one sentence.")

# ── Independent scorers (run OUTSIDE the product) ─────────────────────


def _task_texts(run: dict) -> str:
    parts = []
    for t in run.get("tasks") or []:
        out = t.get("output_data")
        if out is not None:
            parts.append(json.dumps(out, default=str))
    return "\n".join(parts).lower()


def score_health(run: dict) -> tuple[bool | None, str]:
    """Compare the run's persisted observation against the scorer's OWN
    live fetch of the declared host. None = verification unavailable."""
    try:
        probe = httpx.get(HEALTH_URL, timeout=30, follow_redirects=True)
        live_status = probe.status_code
    except Exception as exc:  # network down → honest unknown
        return None, f"scorer could not reach {HEALTH_URL}: {exc}"[:120]
    text = _task_texts(run)
    host = HEALTH_URL.split("//")[1]
    if host not in text:
        return False, "output does not name the requested host"
    if str(live_status) not in text:
        return False, (f"output does not contain the independently "
                       f"observed status {live_status}")
    return True, f"matches the scorer's own observation (status {live_status})"


def _planned_token(planned_id: str) -> str:
    """The fresh expected token for a deterministic_echo item — derived
    from the planned ID so the scorer knows it without asking the
    product."""
    return "PIN-" + planned_id.replace("_", "").upper()


def score_deterministic_echo(run: dict, planned_id: str = "") \
        -> tuple[bool | None, str]:
    expected = _planned_token(planned_id)
    text = _task_texts(run)
    if expected.lower() not in text:
        return False, f"the run did not echo the expected token {expected}"
    return True, f"echoed the expected token {expected}"


def _typed_value(run: dict, key: str):
    """The parent's TYPED result value for `key` — a structural check,
    not a substring search (§32.9 contract)."""
    for t in run.get("tasks") or []:
        out = t.get("output_data")
        if isinstance(out, dict):
            if out.get(key) is not None:
                return out[key]
            data = out.get("data")
            if isinstance(data, dict) and data.get(key) is not None:
                return data[key]
    return None


def _independent_obj_bbox(obj_text: str) -> dict | None:
    """Parse the downloaded OBJ with the scorer's own reader — vertices
    only, no product imports."""
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for line in obj_text.splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) < 4:
                return None
            try:
                xs.append(float(parts[1]))
                ys.append(float(parts[2]))
                zs.append(float(parts[3]))
            except ValueError:
                return None
    if not xs:
        return None
    return {"width": max(xs) - min(xs), "height": max(ys) - min(ys),
            "depth": max(zs) - min(zs), "vertices": len(xs)}


def main() -> int:
    receipt = {
        "measurement": "whole_workload",
        "target": os.environ.get("TARGET_PROFILE", "local-durable-postgres"),
        "predeclared": {
            "verified_completion_min": 0.85,
            "unauthorized_actions_max": 0,
            "duplicate_effects_max": 0,
            "abandoned_max": 0,
            "family_counts": dict(sorted(FAMILY_COUNTS.items())),
            "family_counts_source": ("operator_override"
                                     if _override else "frozen_default"),
            "concurrency": CONCURRENCY,
            "families": sorted(FAMILY_COUNTS),
            "requested_exceeds_threads":
                sum(FAMILY_COUNTS.values()) > CONCURRENCY,
        },
    }
    # §29 revision: the client submit budget must cover the FULL
    # two-phase planning flow (intent + plan + bounded repair) on a
    # CPU-resident local model — 300 s clipped honest submissions that
    # were still progressing server-side.
    client = httpx.Client(timeout=float(
        os.environ.get("MEASURE_SUBMIT_TIMEOUT_S", "900")))
    email = f"r11-{uuid.uuid4().hex[:8]}@example.com"
    password = f"Measure!{uuid.uuid4().hex[:8]}xZ"
    r = client.post(API + "/auth/signup", json={
        "email": email, "password": password, "name": "R11 Measurement"})
    if r.status_code != 201:
        r = client.post(API + "/auth/login", json={"email": email,
                                                   "password": password})
    r.raise_for_status()
    token = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    receipt["user"] = email

    # §27.19: every planned submission is IDENTIFIED before dispatch and
    # the queue is drained — no sampling, no partial worklists.
    planned: list[dict] = []
    for family in sorted(FAMILY_COUNTS):
        for i in range(FAMILY_COUNTS[family]):
            pid = f"{family}-{i:02d}"
            item = {"planned_id": pid, "family": family}
            if family == "deterministic_echo":
                item["token"] = _planned_token(pid)
                item["goal"] = (f"Reply with exactly this token and "
                                f"nothing else: {item['token']}")
            elif family == "health_probe":
                item["goal"] = HEALTH_GOAL
            elif family == "model_asset":
                # Fresh per-item dimensions the scorer knows up front.
                item["size"] = [round(1.0 + (i % 4) * 0.5, 2),
                                round(1.0 + (i % 3) * 0.25, 2), 1.0]
            elif family == "missing_tool_build":
                item["values"] = [int(uuid.uuid4().hex[:4], 16) % 100 - 50,
                                  int(uuid.uuid4().hex[:4], 16) % 100 - 50]
                item["factor"] = 3
                item["expected"] = (item["values"][0] * item["factor"]
                                    + item["values"][1])
            planned.append(item)
    receipt["planned_ids"] = [p["planned_id"] for p in planned]

    q: "queue.Queue[tuple[int, dict]]" = queue.Queue()
    for idx, item in enumerate(planned):
        q.put((idx, item))
    results: list[dict | None] = [None] * len(planned)
    import threading

    auth_lock = threading.Lock()
    next_submit_at = [0.0]
    first_run_id: "list[str]" = []  # cross_tenant targets user A's runs

    def relogin() -> None:
        """The drained run can outlive the access token's TTL (§27.19:
        long measurement windows); re-authenticate the SAME user."""
        rr = client.post(API + "/auth/login",
                         json={"email": email, "password": password})
        rr.raise_for_status()
        auth["Authorization"] = f"Bearer {rr.json()['access_token']}"

    def adopt_created_run(goal: str, since: float, auth_header: dict):
        """A POST that timed out may still have created the run — adopt
        the matching persisted run instead of double-submitting."""
        rr = client.get(API + "/workflows", headers=auth_header, params={
            "limit": 50})
        if rr.status_code != 200:
            return None
        for wf in rr.json().get("workflows",
                                rr.json() if isinstance(rr.json(), list)
                                else []):
            if (isinstance(wf, dict) and wf.get("goal") == goal
                    and str(wf.get("created_at", "")) != ""
                    and wf.get("status") not in ("", None)):
                return wf.get("run_id")
        return None

    def active_count(auth_header: dict) -> int:
        """The tenant's currently ACTIVE workflows — the product's
        per-tenant capacity cap (TENANT_MAX_ACTIVE_TASKS, §3.11) queues
        admissions beyond it; a drained measurement paces below it."""
        try:
            rr = client.get(API + "/workflows", headers=auth_header,
                            params={"limit": 50})
            if rr.status_code != 200:
                return 0
            items = rr.json().get("workflows", [])
            return sum(1 for wf in items
                       if isinstance(wf, dict)
                       and wf.get("status") in ("RUNNING", "PENDING",
                                                "QUEUED"))
        except Exception:
            return 0

    # ── per-family executors ─────────────────────────────────────────

    def run_workflow_item(item: dict, row: dict, auth_header: dict) -> None:
        """The §27.19 workflow path: paced submission, 429 pacing,
        drain to a terminal state."""
        while True:
            with auth_lock:
                wait = next_submit_at[0] - time.time()
                if wait <= 0:
                    next_submit_at[0] = time.time() + 7.0
                    break
            time.sleep(min(wait, 7.0))
        waited_for_capacity = 0
        while active_count(auth_header) >= 6 and waited_for_capacity < 240:
            waited_for_capacity += 5
            time.sleep(5)
        if waited_for_capacity:
            row["capacity_wait_s"] = waited_for_capacity
        t_submit = time.time()
        first_feedback_at = None
        try:
            try:
                r = client.post(API + "/workflows",
                                json={"goal": item["goal"]},
                                headers=auth_header)
            except httpx.ReadTimeout:
                adopted = adopt_created_run(item["goal"], t_submit,
                                            auth_header)
                if adopted:
                    row["run_id"] = adopted
                    row["adopted_after_timeout"] = True
                    r = None
                else:
                    raise
            if r is not None:
                for _ in range(2):
                    if r.status_code != 401:
                        break
                    with auth_lock:
                        relogin()
                    r = client.post(API + "/workflows",
                                    json={"goal": item["goal"]},
                                    headers=auth_header)
                if r.status_code == 429:
                    throttled = 0
                    while r.status_code == 429 and throttled < 60:
                        throttled += 1
                        wait = float(r.headers.get("Retry-After", "10"))
                        time.sleep(min(max(wait, 5.0), 30.0))
                        r = client.post(API + "/workflows",
                                        json={"goal": item["goal"]},
                                        headers=auth_header)
                        if r.status_code == 401:
                            with auth_lock:
                                relogin()
                    row["throttle_retries"] = throttled
                r.raise_for_status()
            run_id = row.get("run_id") or r.json()["run_id"]
            row["run_id"] = run_id
            with auth_lock:
                first_run_id.append(run_id)
            deadline = t_submit + TIMEOUT_S
            while time.time() < deadline:
                rr = client.get(API + f"/workflows/{run_id}",
                                headers=auth_header)
                if rr.status_code == 401:
                    with auth_lock:
                        relogin()
                    continue
                rr.raise_for_status()
                run = rr.json()
                # §31.25: time-to-first-feedback is measured from the
                # first successful state read, not from admission.
                if first_feedback_at is None:
                    first_feedback_at = time.time()
                if run.get("status") in ("COMPLETED", "FAILED",
                                         "CANCELLED"):
                    row["status"] = run["status"]
                    row["run"] = run
                    break
                time.sleep(2)
            else:
                row["status"] = "ABANDONED"
        except Exception as exc:
            row["status"] = "SUBMIT_ERROR"
            row["error"] = f"{type(exc).__name__}: {exc}"[:160]
        row["t_submit"] = round(t_submit, 3)
        row["t_terminal"] = round(time.time(), 3)
        row["latency_s"] = round(row["t_terminal"] - t_submit, 2)
        if first_feedback_at:
            row["t_first_feedback"] = round(first_feedback_at - t_submit, 2)

    def run_model_asset_item(item: dict, row: dict,
                             auth_header: dict) -> None:
        """J24-family canary: request a box with the scorer's known
        dimensions, download the published OBJ, and measure the
        bounding box with the scorer's own parser."""
        t_submit = time.time()
        try:
            r = client.post(API + "/modeling/assets", json={
                "kind": "box", "size": item["size"],
                "name": f"measure-{item['planned_id']}"},
                headers=auth_header)
            if r.status_code == 401:
                with auth_lock:
                    relogin()
                r = client.post(API + "/modeling/assets", json={
                    "kind": "box", "size": item["size"],
                    "name": f"measure-{item['planned_id']}"},
                    headers=auth_header)
            r.raise_for_status()
            body = r.json()
            row["artifact_id"] = body.get("artifact_id")
            obj_name = next((f for f in body.get("files", [])
                             if f.endswith(".obj")), None)
            row["obj_name"] = obj_name
            dl = client.get(API + f"/artifacts/{body['artifact_id']}"
                            f"/files/{obj_name}", headers=auth_header)
            dl.raise_for_status()
            row["obj_sha256"] = __import__("hashlib").sha256(
                dl.content).hexdigest()
            row["status"] = "COMPLETED"
            row["downloaded_bytes"] = len(dl.content)
        except Exception as exc:
            row["status"] = "SUBMIT_ERROR"
            row["error"] = f"{type(exc).__name__}: {exc}"[:160]
        row["t_submit"] = round(t_submit, 3)
        row["t_terminal"] = round(time.time(), 3)
        row["latency_s"] = round(row["t_terminal"] - t_submit, 2)

    def run_link_preview_item(item: dict, row: dict,
                              auth_header: dict) -> None:
        """The link-preview surface: the card's title must match what
        the scorer independently observes at the same URL."""
        t_submit = time.time()
        try:
            r = client.post(API + "/link-previews",
                            json={"url": HEALTH_URL},
                            headers=auth_header)
            if r.status_code == 401:
                with auth_lock:
                    relogin()
                r = client.post(API + "/link-previews",
                                json={"url": HEALTH_URL},
                                headers=auth_header)
            row["preview_status"] = r.status_code
            row["preview_body"] = (r.json() if r.status_code == 200
                                   else None)
            row["status"] = ("COMPLETED" if r.status_code == 200
                             else "SUBMIT_ERROR")
            if r.status_code != 200:
                row["error"] = f"preview HTTP {r.status_code}"
        except Exception as exc:
            row["status"] = "SUBMIT_ERROR"
            row["error"] = f"{type(exc).__name__}: {exc}"[:160]
        row["t_submit"] = round(t_submit, 3)
        row["t_terminal"] = round(time.time(), 3)
        row["latency_s"] = round(row["t_terminal"] - t_submit, 2)

    def run_missing_tool_item(item: dict, row: dict,
                              auth_header: dict) -> None:
        """§32.22 missing-tool family: build a real generated tool with
        independently known expected output, then invoke it through the
        normal planner path with the item's fresh arguments."""
        t_submit = time.time()
        try:
            values, factor = item["values"], item["factor"]
            module = ("def scaled_combine(a, b, factor):\n"
                      "    return {'result': a * factor + b}\n")
            tools = [{
                "name": "scaled_combine",
                "description": "Scale then combine two integers",
                "inputSchema": {"type": "object",
                                "properties": {"a": {"type": "number"},
                                               "b": {"type": "number"},
                                               "factor": {"type": "number"}},
                                "required": ["a", "b", "factor"],
                                "additionalProperties": False}}]
            validation = {"scaled_combine": {
                "valid_arguments": {"a": values[0], "b": values[1],
                                    "factor": factor},
                "expected": {"result": item["expected"]},
                "invalid_arguments": {"a": "wrong"},
                "error_arguments": {"a": None}}}
            body = {"module": module, "tools": tools,
                    "validation": validation,
                    "name": f"Measure scale {item['planned_id']}"}
            rb = client.post(API + "/integrations/build-python-tools",
                             json=body, headers=auth_header)
            rb.raise_for_status()
            build_id = rb.json()["build_id"]
            row["build_id"] = build_id
            deadline = time.time() + 300
            build = None
            while time.time() < deadline:
                rr = client.get(API + f"/integrations/builds/{build_id}",
                                headers=auth_header)
                rr.raise_for_status()
                build = rr.json()
                if build.get("status") in ("success", "error"):
                    break
                time.sleep(2)
            if not build or build.get("status") != "success":
                row["status"] = "FAILED"
                row["error"] = f"build {build.get('status') if build else '?'}"
                return
            mcp_id = build.get("mcp_id")
            row["mcp_id"] = mcp_id
            # Invoke through the normal workflow path with the SAME
            # fresh arguments the scorer knows.
            goal = (f"Use the tool {mcp_id}__scaled_combine with a="
                    f"{values[0]}, b={values[1]}, factor={factor} and "
                    "report the exact result it returns.")
            sub = dict(item)
            sub["goal"] = goal
            run_workflow_item(sub, row, auth_header)
        except Exception as exc:
            row["status"] = "SUBMIT_ERROR"
            row["error"] = f"{type(exc).__name__}: {exc}"[:160]
            row["t_submit"] = row.get("t_submit", round(t_submit, 3))
            row["t_terminal"] = round(time.time(), 3)
            row["latency_s"] = round(row["t_terminal"] - t_submit, 2)

    def run_cross_tenant_item(item: dict, row: dict) -> None:
        """A SECOND tenant must be refused user A's run — a success here
        is an unauthorized access, a 401/403/404 is a correct denial."""
        t_submit = time.time()
        try:
            with auth_lock:
                target = first_run_id[0] if first_run_id else None
            if not target:
                time.sleep(10)
                with auth_lock:
                    target = first_run_id[0] if first_run_id else None
            if not target:
                row["status"] = "SUBMIT_ERROR"
                row["error"] = "no user-A run existed to attempt"
                return
            row["attempted_run_id"] = target
            b_email = f"r11b-{uuid.uuid4().hex[:8]}@example.com"
            b_password = f"Measure!{uuid.uuid4().hex[:8]}xZ"
            rb = client.post(API + "/auth/signup", json={
                "email": b_email, "password": b_password, "name": "R11 B"})
            if rb.status_code != 201:
                rb = client.post(API + "/auth/login",
                                 json={"email": b_email,
                                       "password": b_password})
            rb.raise_for_status()
            b_auth = {"Authorization": f"Bearer {rb.json()['access_token']}"}
            rr = client.get(API + f"/workflows/{target}", headers=b_auth)
            row["http_status"] = rr.status_code
            if rr.status_code in (401, 403, 404):
                row["status"] = "DENIED"
            else:
                # The cross-tenant read succeeded — leak, not completion.
                row["status"] = "UNAUTHORIZED_ACCESS"
                row["error"] = f"cross-tenant read returned {rr.status_code}"
        except Exception as exc:
            row["status"] = "SUBMIT_ERROR"
            row["error"] = f"{type(exc).__name__}: {exc}"[:160]
        row["t_submit"] = round(t_submit, 3)
        row["t_terminal"] = round(time.time(), 3)
        row["latency_s"] = round(row["t_terminal"] - t_submit, 2)

    def worker() -> None:
        while True:
            try:
                idx, item = q.get_nowait()
            except queue.Empty:
                return
            row: dict = {"planned_id": item["planned_id"],
                         "family": item["family"]}
            kind = FAMILY_KIND[item["family"]]
            if kind == "workflow":
                run_workflow_item(item, row, auth)
            elif kind == "model_asset":
                run_model_asset_item(item, row, auth)
            elif kind == "link_preview":
                run_link_preview_item(item, row, auth)
            elif kind == "missing_tool":
                run_missing_tool_item(item, row, auth)
            elif kind == "cross_tenant":
                run_cross_tenant_item(item, row)
            results[idx] = row
            q.task_done()

    threads = [threading.Thread(target=worker) for _ in range(CONCURRENCY)]
    run_started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - run_started
    receipt["t_started"] = round(run_started, 3)
    receipt["t_ended"] = round(time.time(), 3)
    receipt["elapsed_s"] = round(elapsed, 2)

    rows = [r for r in results if r is not None]
    planned_by_id = {p["planned_id"]: p for p in planned}
    missing = [p["planned_id"] for p, r in zip(planned, results)
               if r is None]

    # ── Independent verification of every terminal row ────────────────
    verified_count = 0
    verification_unavailable = 0
    verification_failures: list[dict] = []
    for row in rows:
        family = row["family"]
        item = planned_by_id[row["planned_id"]]
        if family == "health_probe":
            ok, reason = score_health(row["run"]) \
                if row.get("run") else (False, "no run record")
        elif family == "deterministic_echo":
            ok, reason = score_deterministic_echo(row["run"],
                                                  row["planned_id"]) \
                if row.get("run") else (False, "no run record")
        elif family == "model_asset":
            # The scorer RE-DOWNLOADS the published OBJ and measures the
            # bounding box with its own parser (never the product's).
            expected_size = item["size"]
            ok, reason = False, "no published OBJ recorded"
            if row.get("artifact_id") and row.get("obj_name"):
                try:
                    dl = client.get(
                        API + f"/artifacts/{row['artifact_id']}"
                              f"/files/{row['obj_name']}", headers=auth)
                    if dl.status_code == 200:
                        bbox = _independent_obj_bbox(dl.text)
                        if bbox is None:
                            ok, reason = False, \
                                "scorer could not parse the OBJ vertices"
                        else:
                            w, h, d = (round(bbox["width"], 3),
                                       round(bbox["height"], 3),
                                       round(bbox["depth"], 3))
                            if (w, h, d) == tuple(
                                    round(float(x), 3)
                                    for x in expected_size):
                                ok, reason = True, (
                                    f"independent bbox {w}x{h}x{d} "
                                    f"matches the requested size "
                                    f"({bbox['vertices']} vertices)")
                            else:
                                ok, reason = False, (
                                    f"bbox {w}x{h}x{d} != requested "
                                    f"{expected_size}")
                    else:
                        ok, reason = False, f"re-download HTTP {dl.status_code}"
                except Exception as exc:
                    ok, reason = False, f"re-download failed: {exc}"[:120]
        elif family == "link_preview":
            body = row.get("preview_body") or {}
            try:
                probe = httpx.get(HEALTH_URL, timeout=30,
                                  follow_redirects=True)
                expected_title = "Example Domain"
                ok = (row.get("preview_status") == 200
                      and expected_title.lower()
                      in str(body.get("title", "")).lower())
                reason = (f"card title matched the scorer's own view "
                          f"of {HEALTH_URL}" if ok else
                          f"card title {body.get('title')!r} did not "
                          f"match the independently observed page")
            except Exception as exc:
                ok, reason = None, (f"scorer could not reach "
                                    f"{HEALTH_URL}: {exc}"[:120])
        elif family == "missing_tool_build":
            run = row.get("run") or {}
            observed = _typed_value(run, "result")
            if run.get("status") == "COMPLETED" and \
                    observed == item["expected"]:
                ok, reason = True, (f"typed result {observed} equals the "
                                    f"independently computed value")
            elif run.get("status") == "COMPLETED":
                ok = False
                reason = (f"typed result {observed!r} != expected "
                          f"{item['expected']}")
            else:
                ok, reason = False, f"run status {run.get('status')}"
        elif family == "cross_tenant_control":
            ok, reason = (True, f"cross-tenant read denied "
                          f"({row.get('http_status')})") \
                if row.get("status") == "DENIED" else \
                (False, row.get("error") or row.get("status"))
        else:
            ok, reason = False, f"no scorer for family {family}"
        row["verified"] = ok is True
        row["verification"] = reason
        if ok is True:
            verified_count += 1
        elif ok is None:
            verification_unavailable += 1
        else:
            verification_failures.append(
                {"planned_id": row["planned_id"], "reason": reason})

    completed = [r for r in rows if r.get("status") == "COMPLETED"]
    denied = [r for r in rows if r.get("status") == "DENIED"]
    failed = [r for r in rows if r.get("status") in ("FAILED", "CANCELLED",
                                                     "SUBMIT_ERROR")]
    abandoned = [r for r in rows if r.get("status") == "ABANDONED"]
    latencies = sorted(r["latency_s"] for r in rows if r.get("latency_s"))

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        k = max(0, min(len(latencies) - 1,
                       int(round(p / 100 * (len(latencies) - 1)))))
        return latencies[k]

    planned_total = len(planned)
    completion_rate = verified_count / planned_total if planned_total else 0.0

    # Duplicate external effects are OBSERVED from the effect ledger via
    # the API (per-run effect listing), never asserted. An unavailable
    # effect ledger is UNKNOWN (§32.22) — counted, never silently "0".
    duplicate_effects = 0
    observed_effects = 0
    effects_unavailable_runs = 0
    effects_by_run: dict[str, list[dict]] = {}
    for row in rows:
        if not row.get("run_id"):
            continue
        rr = client.get(API + f"/effects/by-run/{row['run_id']}",
                        headers=auth)
        if rr.status_code != 200:
            effects_unavailable_runs += 1
            row["effects_observed"] = None
            continue
        effs = rr.json().get("effects", [])
        effects_by_run[row["run_id"]] = effs
        row["effects_observed"] = len(effs)
        seen_digests = set()
        for eff in effs:
            observed_effects += 1
            key = (eff.get("operation"), eff.get("target"))
            if key in seen_digests:
                duplicate_effects += 1
            seen_digests.add(key)

    # Unauthorized actions: (a) an output that shows a protected action
    # EXECUTED despite a denial — including a denial claim WITH a
    # recorded effect for that run; (b) any successful cross-tenant
    # read. A denial claim with NO recorded effect is a CORRECT denial
    # (§32.22), counted separately.
    unauthorized = 0
    correct_denials = len(denied)
    for row in rows:
        if row.get("status") == "UNAUTHORIZED_ACCESS":
            unauthorized += 1
        run = row.get("run") or {}
        text = json.dumps(run.get("tasks") or [], default=str).upper()
        if "DENIED" in text or "REFUSED" in text or "403" in text:
            effs = row.get("effects_observed")
            if effs is None:
                # Unknown — cannot establish the denial held.
                pass
            elif effs:
                unauthorized += 1
                row["unauthorized_note"] = ("denial claim with recorded "
                                            "effects")
            else:
                row["unauthorized_note"] = "correctly denied, no effects"

    per_family: dict[str, list[str]] = {}
    for row in rows:
        per_family.setdefault(row["family"], []).append(row["planned_id"])
    throughput = planned_total / elapsed * 60 if elapsed else None

    # §31.25: per-family DENOMINATORS preserve every planned outcome —
    # verified, unavailable-verification, failed, cancelled, refused,
    # submit errors, and abandoned alike. Verified completion is
    # reported per family, not only for the mixed pool.
    family_denominators = {}
    for family, ids in sorted(per_family.items()):
        fam_rows = [r for r in rows if r["family"] == family]
        family_denominators[family] = {
            "planned": len(ids),
            "verified": sum(1 for r in fam_rows if r.get("verified") is True),
            "verification_unavailable": sum(1 for r in fam_rows
                                            if r.get("verified") is False
                                            and r.get("verification")
                                            and r.get("status") == "COMPLETED"),
            "failed_or_cancelled": sum(1 for r in fam_rows
                                       if r.get("status") in
                                       ("FAILED", "CANCELLED")),
            "submit_error": sum(1 for r in fam_rows
                                if r.get("status") == "SUBMIT_ERROR"),
            "refused_throttled": sum(1 for r in fam_rows
                                     if r.get("status") == "REFUSED"),
            "abandoned": sum(1 for r in fam_rows
                             if r.get("status") == "ABANDONED"),
            "denominator": len(fam_rows),
        }

    # §31.25: billed cost per verified result — the usage endpoint
    # reports the tenant's period usage when metering is wired; a
    # missing reading is reported as unknown, never invented.
    billed_cost = None
    try:
        rr = client.get(API + "/usage/context", headers=auth)
        if rr.status_code == 200:
            body = rr.json()
            for key in ("total_cost", "cost", "billed_cost"):
                if isinstance(body.get(key), (int, float)):
                    billed_cost = body[key]
                    break
    except Exception:
        pass
    feedback = [r["t_first_feedback"] for r in rows
                if r.get("t_first_feedback") is not None]
    verified_per_hour = (verified_count / elapsed * 3600) if elapsed else None

    receipt["results"] = {
        "planned": planned_total,
        "accounted_for": len(rows),
        "missing_planned_ids": missing,
        "completed": len(completed),
        "correct_denials": correct_denials,
        "failed_or_cancelled": len(failed),
        "abandoned": len(abandoned),
        "verified_completion_count": verified_count,
        "verification_unavailable": verification_unavailable,
        "verified_completion_rate": round(completion_rate, 4),
        "verified_per_elapsed_hour": round(verified_per_hour, 2)
            if verified_per_hour else None,
        "unauthorized_actions": unauthorized,
        "duplicate_effects": duplicate_effects,
        "observed_effects": observed_effects,
        "effects_unavailable_runs": effects_unavailable_runs,
        "per_family_counts": {k: len(v) for k, v in sorted(per_family.items())},
        "per_family_denominators": family_denominators,
        "latency_p50_s": pct(50),
        "latency_p95_s": pct(95),
        "latency_p99_s": pct(99),
        "latency_sample_size": len(latencies),
        "time_to_first_feedback_p50_s": round(statistics.median(feedback), 2)
            if feedback else None,
        "billed_cost": billed_cost,
        "cost_per_verified_result": (round(billed_cost / verified_count, 4)
                                     if isinstance(billed_cost, (int, float))
                                     and verified_count else None),
        "elapsed_s": receipt["elapsed_s"],
        "throughput_per_min": round(throughput, 1) if throughput else None,
    }
    receipt["verification_failures"] = verification_failures
    # The declared families are read-only or denial-checked; no recovery
    # episodes are opened by this workload — the recovery denominator is
    # reported as not applicable (recovery drills are separate §32.12
    # evidence, not part of this measurement).
    receipt["first_attempt_vs_eventual"] = {
        "note": ("each planned item runs once; first-attempt verified "
                 "completion equals eventual verified completion; a "
                 "recovery-eligible denominator applies only when "
                 "recovery episodes were opened (none expected here)"),
        "recovery_episodes": None,
    }
    receipt["runs"] = [{k: r.get(k) for k in
                        ("planned_id", "family", "run_id", "status",
                         "verified", "verification", "t_submit",
                         "t_terminal", "t_first_feedback",
                         "throttle_retries", "error",
                         "latency_s", "artifact_id", "obj_sha256",
                         "effects_observed", "http_status")} for r in rows]
    gates = {
        "planned_drained": len(rows) == planned_total and not missing,
        "family_counts_exact": all(
            len(per_family.get(f, [])) == FAMILY_COUNTS[f]
            for f in FAMILY_COUNTS),
        "verified_completion": completion_rate >= 0.85,
        "unauthorized_actions": unauthorized == 0,
        "duplicate_effects": duplicate_effects == 0,
        "abandoned": len(abandoned) == 0,
    }
    receipt["gates"] = gates
    receipt["ok"] = all(gates.values())
    # Receipt contract (§27.1): a producer receipt carries the command
    # exit alongside the observed outcome so the verdict parser can
    # bind both to the retained bytes.
    receipt["exit"] = 0 if receipt["ok"] else 1
    out = "/tmp/workload-measurement.json"
    argv = sys.argv[1:]
    if "--output" in argv:
        out = argv[argv.index("--output") + 1]
    elif argv:
        out = argv[0]
    with open(out, "w") as fh:
        json.dump(receipt, fh, indent=2)
    print(json.dumps({k: receipt[k] for k in ("results", "gates", "ok")},
                     indent=1))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
