#!/usr/bin/env python3
"""Phase 22 §22.5/§22.6/§22.8/§22.9 — LIVE qualification probes against
the production stack (compose core). Every probe exercises a PRODUCTION
entry point over HTTP with real accounts. Results land in
data/phase22_live_probes.json. No secrets printed.

Probes:
  B5.1 anonymous denied on protected resources
  B5.2 cross-user denial (run IDs are not capabilities)
  B5.3 session revocation (logout invalidates the access token)
  B5.4 viewer role cannot act (org viewer boundary)
  B6.1 credential round-trip stores a secret without echoing it
  B8.1 budget exhaustion returns an explicit, actionable 429
  B9.1 rate limiting returns 429 with an actionable message
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("QUALIFY_API_URL", "http://127.0.0.1:8000").rstrip("/")
OUT = Path(__file__).resolve().parents[1] / "data" / "phase23_live_probes.json"
RESULTS: list[dict] = []


def req(method, url, token=None, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_text": raw[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"_error": str(e)[:200]}


def check(name, ok, detail=None):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail or {}})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} "
          f"{json.dumps(detail, default=str)[:220] if detail else ''}")
    return ok


def signup(i):
    ts = int(time.time() * 1000)
    code, u = req("POST", f"{BASE}/api/v1/auth/signup", body={
        "email": f"probe{i}-{ts}@agentos.local",
        "password": f"Qual!{ts}x{i}", "name": f"Probe {i}"})
    return u.get("access_token"), u.get("user_id") or u.get("id") or u.get("sub")


def main() -> int:
    RESULTS.clear()
    # readiness
    code, health = req("GET", f"{BASE}/health")
    if code != 200:
        print("api not running:", code, health)
        return 2

    tok_a, uid_a = signup("a")
    tok_b, uid_b = signup("b")
    check("B.setup_two_users", bool(tok_a and tok_b),
          {"a": bool(tok_a), "b": bool(tok_b)})

    # ── B5.1 anonymous denied ─────────────────────────────────────────
    for path in ("/api/v1/workflows", "/api/v1/integrations",
                 "/api/v1/memory", "/api/v1/usage/budget"):
        code, body = req("GET", f"{BASE}{path}")
        check(f"B5.1.anonymous_denied {path}",
              code in (401, 403), {"status": code})

    # ── B5.2 cross-user denial ────────────────────────────────────────
    code, run_a = req("POST", f"{BASE}/api/v1/workflows", tok_a,
                      {"goal": "GET https://httpbin.org/json"})
    rid = (run_a or {}).get("run_id")
    check("B5.2.userA_creates_run", code == 201 and bool(rid),
          {"status": code})
    # user B must NOT read or act on A's run
    code_b, body_b = req("GET", f"{BASE}/api/v1/workflows/{rid}", tok_b)
    check("B5.2.cross_user_read_denied", code_b in (401, 403, 404),
          {"status": code_b})
    code_b2, _ = req("POST", f"{BASE}/api/v1/workflows/{rid}/cancel", tok_b)
    check("B5.2.cross_user_cancel_denied", code_b2 in (401, 403, 404),
          {"status": code_b2})
    code_b3, _ = req("GET", f"{BASE}/api/v1/workflows/{rid}/thread", tok_b)
    check("B5.2.cross_user_thread_denied", code_b3 in (401, 403, 404),
          {"status": code_b3})

    # ── B5.3 revocation ───────────────────────────────────────────────
    code, out = req("POST", f"{BASE}/api/v1/auth/logout", tok_a)
    check("B5.3.logout_accepted", code in (200, 204, 201), {"status": code})
    code, _ = req("GET", f"{BASE}/api/v1/workflows/{rid}", tok_a)
    check("B5.3.revoked_token_refused", code in (401, 403),
          {"status": code})
    # refresh with a revoked refresh token must fail too
    code, _ = req("POST", f"{BASE}/api/v1/auth/refresh", body={})
    check("B5.3.empty_refresh_refused", code in (400, 401, 403, 422),
          {"status": code})

    # A viewer invitation is not proof of effective viewer authority.
    tok_c, _ = signup("c")
    tok_v = os.environ.get("QUALIFY_VIEWER_TOKEN")
    if tok_v:
        code, me = req("GET", f"{BASE}/api/v1/auth/me", tok_v)
        actual_viewer = code == 200 and me.get("role") == "viewer"
        check("B5.4.viewer_identity", actual_viewer, {"status": code})
        code, _ = req("POST", f"{BASE}/api/v1/credentials", tok_v,
                       {"name": "viewer-probe", "values": {"api_key": "non-secret-fixture"}})
        check("B5.4.viewer_write_denied", actual_viewer and code == 403, {"status": code})
    else:
        check("B5.4.viewer_write_denied", False,
              {"blocker": "Set QUALIFY_VIEWER_TOKEN for an actual viewer account"})

    # ── B6.1 credential round-trip (names only, never the secret) ────
    code, stored = req("POST", f"{BASE}/api/v1/credentials", tok_b, {
        "name": "probe-secret",
        "values": {"api_key": f"sk-probe-{int(time.time())}-DO-NOT-PRINT"}})
    if code in (200, 201):
        blob = json.dumps(stored)
        check("B6.1.secret_not_echoed",
              "sk-probe-" not in blob, {"keys": list(stored)[:8]})
        code2, listed = req("GET", f"{BASE}/api/v1/credentials", tok_b)
        blob2 = json.dumps(listed)
        check("B6.1.list_names_only",
              code2 == 200 and "sk-probe-" not in blob2, {})
        # cross-user: A cannot see B's credentials
        code3, other_list = req("GET", f"{BASE}/api/v1/credentials", tok_c)
        check("B6.1.cross_user_secret_hidden",
              bool(tok_c) and code3 == 200 and "probe-secret" not in json.dumps(other_list)
              and "sk-probe-" not in json.dumps(other_list),
              {"note": "checked via token C listing"})
    else:
        check("B6.1.credential_store", False,
              {"status": code,
               "note": "credentials endpoint shape differs — recorded"})

    # ── B8.1 budget exhaustion → explicit actionable 429 ─────────────
    tok_d, _ = signup("d")
    if tok_d:
        # Settings PUT takes a full record; read-modify-write the daily
        # token budget through the production settings API.
        code, cur = req("GET", f"{BASE}/api/v1/settings", tok_d)
        rec = cur if isinstance(cur, dict) else {}
        if "settings" in rec and isinstance(rec["settings"], dict):
            rec = rec["settings"]
        rec.update({"daily_token_limit": 0})
        code, lim = req("PUT", f"{BASE}/api/v1/settings", tok_d, rec)
        check("B8.1.zero_budget_saved", code == 200, {"status": code})
        code2, run2 = req("POST", f"{BASE}/api/v1/workflows", tok_d,
                          {"goal": "Write an original explanation of why tides change, using model inference"})
        msg = json.dumps(run2)
        check("B8.1.budget_or_admission_explicit",
              code == 200 and code2 == 429 and ("budget" in msg.lower() or "token" in msg.lower()),
              {"submit_status": code2,
               "note": "A successful submission cannot prove budget exhaustion"})
        if code2 == 429:
            check("B8.1.budget_message_actionable",
                  "budget" in msg.lower() or "token" in msg.lower(),
                  {"message": msg[:200]})
    else:
        check("B8.1.budget_or_admission_explicit", False, {"blocker": "budget probe account creation failed"})

    # ── B9.1 rate limiting ────────────────────────────────────────────
    seen429 = 0
    for i in range(30):
        code, body = req("POST", f"{BASE}/api/v1/auth/guest")
        if code == 429:
            seen429 += 1
            msg = json.dumps(body)
            check("B9.1.rate_limit_actionable",
                  "retry" in msg.lower() or "limit" in msg.lower(),
                  {"message": msg[:160]})
            break
    else:
        check("B9.1.no_limit_hit_at_30_guests", False,
              {"note": "guest burst below configured limits; the shared "
                       "limiter is proven by test_admission_policy.py"})

    out = {"recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "base": BASE, "results": RESULTS,
           "passed": sum(1 for r in RESULTS if r["ok"]),
           "total": len(RESULTS)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    print(f"\n{out['passed']}/{out['total']} probes passed → {OUT}")
    return 0 if out["passed"] == out["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
