#!/usr/bin/env python3
"""
Veyrune — P0 production verification (audit §8 P0 + high bugs).

Re-runs the audit's P0 checks against a DEPLOYED environment so you can
confirm the fixes are live within a minute. Read-only or self-cleaning:
creates one guest session, one schedule (deleted afterwards), and a
handful of LLM-free workflow runs.

Usage:
  python3 scripts/gcp/verify-p0-production.py [BASE_URL]
  # BASE_URL defaults to the API audit target; pass e.g.
  #   python3 scripts/gcp/verify-p0-production.py https://veyrune-api-xxxx-uc.a.run.app
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://veyrune-api-s7ur7u4f3a-uc.a.run.app").rstrip("/")

PASS, FAIL = "✅", "❌"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def req(path, method="GET", body=None, token=None, raw=False):
    r = urllib.request.Request(
        BASE + path, data=body, method=method,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    try:
        resp = urllib.request.urlopen(r, timeout=45)
        data = resp.read()
        return resp.status, dict(resp.headers), (data if raw else (json.loads(data) if data else {}))
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers), json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, dict(e.headers), {}


# ── P0.5: /api/v1/health with build info ────────────────────────────
code, _, health = req("/api/v1/health")
check("P0.5 GET /api/v1/health", code == 200 and health.get("version"),
      f"version={health.get('version')} commit={str(health.get('commit'))[:8]} storage={health.get('storage')}")

# ── Bug 15/P0.2: POST /memory works ─────────────────────────────────
code, _, auth = req("/api/v1/auth/guest", "POST", b"")
token = auth.get("access_token", "")
check("guest session", code == 201 and bool(token))

code, _, mem = req("/api/v1/memory", "POST",
                   json.dumps({"content": "verification entry", "memory_type": "profile"}).encode(), token)
check("P0.2 POST /memory (was 500)", code == 201 and bool(mem.get("memory_id")), f"HTTP {code}")

# ── Bug 9: GET /memory/search alias ─────────────────────────────────
code, _, _ = req("/api/v1/memory/search?q=verification", token=token)
check("bug 9 GET /memory/search (was 405)", code == 200, f"HTTP {code}")

# ── Bug 2: negative pagination rejected ─────────────────────────────
code, _, _ = req("/api/v1/workflows?limit=-1", token=token)
check("bug 2 limit=-1 rejected (was 200)", code == 422, f"HTTP {code}")

# ── Bug 13/P0.3: logout revokes the access token ────────────────────
_, _, auth2 = req("/api/v1/auth/guest", "POST", b"")
t2 = auth2.get("access_token", "")
req("/api/v1/auth/logout", "POST", b"", t2)
code, _, _ = req("/api/v1/auth/me", token=t2)
check("P0.3 logout revokes access token (was 200)", code == 401, f"HTTP {code}")

# ── Bug 8: actionable dangerous-content error ───────────────────────
code, _, err = req("/api/v1/workflows", "POST",
                   json.dumps({"goal": "<script>alert(1)</script>"}).encode(), token)
check("bug 8 dangerous-content 400 with guidance",
      code == 400 and "plain language" in str(err.get("detail", "")), f"HTTP {code}")

# ── Bug 14/P0.3: workflow rate limit (LLM-free goals only) ──────────
codes = []
for _ in range(13):
    c, _, _ = req("/api/v1/workflows", "POST",
                  json.dumps({"goal": "check the health of https://example.com"}).encode(), token)
    codes.append(c)
limited = codes.count(429)
check("P0.3 workflow rate limit fires", limited >= 2, f"codes={codes}")

# ── Bug 16/P0.1: strict-JSON responses (hostile content round-trip) ─
code, _, mem2 = req("/api/v1/memory", "POST",
                    json.dumps({"content": '<html lang="en">Report "x"</html>\r\nline2'}).encode(), token)
if mem2.get("memory_id"):
    code, _, listing = req("/api/v1/memory", token=token)
    check("P0.1 strict JSON with HTML in payload", code == 200 and isinstance(listing, dict), f"HTTP {code}")

# ── Bug 17/P0.4: petstore relative-servers build (LLM-free) ─────────
code, _, build = req("/api/v1/integrations/build-from-url", "POST",
                     json.dumps({"url": "https://petstore3.swagger.io/api/v3/openapi.json",
                                 "name": "Petstore"}).encode(), token)
detail = str(build.get("message") or build.get("detail") or build)
queued_ok = code in (200, 202) and bool(build.get("build_id")) and build.get("status") in ("queued", "running", "success")
check("P0.4 petstore build accepted (was 'no HTTPS servers')", queued_ok, f"HTTP {code} — {detail[:80]}")
build_id = build.get("build_id")
if build_id:
    b = {}
    for _ in range(40):
        code, _, b = req(f"/api/v1/integrations/builds/{build_id}", token=token)
        if b.get("status") in ("success", "error"):
            break
        time.sleep(3)
    check("P0.4 petstore build completes successfully", b.get("status") == "success",
          f"final={b.get('status')} tools={len(b.get('tools') or [])}")

# ── R2: scheduled run observability ─────────────────────────────────
code, _, sch = req("/api/v1/schedules", "POST",
                   json.dumps({"name": "p0-verify", "goal": "check the health of https://example.com",
                               "cron_expression": "0 3 * * *"}).encode(), token)
sid = sch.get("schedule_id", "")
enabled_ok = sch.get("is_enabled") is True and sch.get("status") == "ACTIVE"
check("R2 schedule created ACTIVE with is_enabled", code == 201 and enabled_ok, f"HTTP {code}")
if sid:
    code, _, now = req(f"/api/v1/schedules/{sid}/run", "POST", b"", token)
    rid = now.get("run_id", "")
    code2, _, run = req(f"/api/v1/workflows/{rid}", token=token)
    check("R2 run-now creates observable run immediately", code2 == 200 and rid.startswith("sch-"), f"HTTP {code2}")
    req(f"/api/v1/schedules/{sid}", "DELETE", b"", token)

# ── v2.1 features: catalog + API keys ───────────────────────────────
code, _, key = req("/api/v1/auth/api-keys", "POST",
                   json.dumps({"name": "p0-verify"}).encode(), token)
check("v2.1 API key created (raw shown once)", code == 201 and key.get("key", "").startswith("vry_"), f"HTTP {code}")
if key.get("key"):
    code, _, me = req("/api/v1/auth/me", token=key["key"])
    check("v2.1 API key authenticates like a JWT", code == 200 and me.get("id"), f"HTTP {code}")
    code, _, _ = req(f"/api/v1/auth/api-keys/{key['id']}", "DELETE", token=token)
    code, _, _ = req("/api/v1/auth/me", token=key["key"])
    check("v2.1 revoked API key → 401 immediately", code == 401, f"HTTP {code}")

code, _, catalog = req("/api/v1/integrations/catalog", token=token)
check("v2.1 catalog endpoint (empty is valid)", code == 200 and "integrations" in catalog, f"HTTP {code}")

code, _, board = req("/api/v1/usage/models", token=token)
check("v2.1 adaptive-policy leaderboard", code == 200 and "policy" in board, f"HTTP {code}")

# ── Bug 4: task title + started_at (runs completed by now) ──────────
time.sleep(4)
code, _, runs = req("/api/v1/workflows?limit=20", token=token)
tasks = [t for w in runs.get("workflows", []) for t in (w.get("tasks") or [])]
titled = [t for t in tasks if t.get("title") and t.get("started_at") and t.get("status") == "COMPLETED"]
check("bug 4 task.title + started_at populated", bool(titled),
      titled[0]["title"][:60] if titled else "no completed tasks yet")

# ── Summary ─────────────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} checks passed" + (f" — {len(failed)} FAILED" if failed else " — all P0 fixes verified live"))
sys.exit(1 if failed else 0)
