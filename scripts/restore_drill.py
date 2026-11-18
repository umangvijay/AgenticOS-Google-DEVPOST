#!/usr/bin/env python3
"""Phase 27 §27.18 / §27.R9 — the INDEPENDENT APPLICATION restore drill.

The qualifier's DB-level restore proves counts match; this drill proves
the RECOVERED APPLICATION WORKS: DB + artifact bytes + secret material
are restored into a separate stack (separate postgres container, separate
artifact directory, separate key material, separate port), access to the
original application's data services is not used, and real probes run
against the restored app:

  1. health + storage identity          (isolation)
  2. sign-in of a pre-backup user       (identity/secret material)
  3. download a pre-backup artifact     (bytes digest match)
  4. revoked user sign-in refused       (revocation enforced)
  5. deleted feedback stays deleted     (deletion enforced)
  6. NEW verified workflow completes    (the app actually works)
  7. RTO measured, RPO measured against the watermark

Outcome: a JSON file with structured subchecks and probe receipts.
Exit 0 only when every probe passed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess as sp
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_API = os.environ.get("R9_SOURCE_API", "http://localhost:8000")
RESTORE_PORT = int(os.environ.get("R9_RESTORE_PORT", "8001"))
RESTORE_PG_PORT = int(os.environ.get("R9_RESTORE_PG_PORT", "55433"))
WORKDIR = Path(os.environ.get("R9_WORKDIR", "/tmp/agentos-r9-drill"))
SECRET = os.environ.get("SECRETS_MASTER_KEY", "local-only-master-key-0123456789abcdef")
RUN_TAG = uuid.uuid4().hex[:8]


def sh(args, **kw):
    return sp.run(args, capture_output=True, timeout=kw.pop("timeout", 120), **kw)


def source_pg_dsn() -> str:
    return os.environ.get("AGENTOS_TEST_PG_DSN",
                          "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test")


async def _pg(sql: str, *args) -> str:
    import asyncpg
    conn = await asyncpg.connect(source_pg_dsn(), timeout=15)
    try:
        val = await conn.fetchval(sql, *args)
        return str(val) if val is not None else ""
    finally:
        await conn.close()


class Drill:
    def __init__(self):
        self.subchecks: dict[str, object] = {}
        self.receipts: dict[str, object] = {}
        self.started = time.time()
        self.rto_seconds: float | None = None
        self.completed = False

    def ok(self, name: str, detail: object = "ok") -> None:
        self.subchecks[name] = detail

    def fail(self, name: str, detail: object) -> None:
        self.subchecks[name] = f"FAILED: {detail}"
        raise DrillFailure(f"{name}: {detail}")


class DrillFailure(RuntimeError):
    pass


def http(method: str, url: str, **kw):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, method=method, **kw)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def api(method: str, path: str, base: str = SOURCE_API, token: str | None = None,
        body: dict | None = None) -> tuple[int, dict | bytes]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    status, raw = http(method, base.rstrip("/") + "/api/v1" + path,
                       headers=headers, data=data)
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw


def main() -> int:
    drill = Drill()
    outcome_path = Path(sys.argv[1]) if len(sys.argv) > 1 else WORKDIR / "drill-outcome.json"
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import asyncio
        asyncio.run(run(drill))
    except DrillFailure as exc:
        print(f"[r9] drill failed: {exc}", flush=True)
    except Exception as exc:  # unexpected: record, never mask
        drill.subchecks["unexpected_error"] = f"{type(exc).__name__}: {exc}"
    drill.rto_seconds = round(time.time() - drill.started, 1)
    elapsed_independent = drill.receipts.get("rto_independent_seconds")
    outcome = {
        "drill_completed": drill.completed,
        "drill": "independent_app_restore",
        "run_tag": RUN_TAG,
        "started_at": drill.started,
        "rto_seconds": drill.rto_seconds,
        "rto_independent_seconds": elapsed_independent,
        "subchecks": drill.subchecks,
        "receipts": drill.receipts,
        "ok": all(
            (v == "ok")
            or (isinstance(v, dict) and v.get("status") in (None, "passed"))
            for k, v in drill.subchecks.items()
            if not k.startswith("_")
        ),
    }
    # Failures recorded as "FAILED: ..." make ok False automatically;
    # a drill that ended early (any DrillFailure) can never be ok.
    outcome["ok"] = (outcome["ok"] and drill.completed
                     and not any(isinstance(v, str) and v.startswith("FAILED")
                                 for v in drill.subchecks.values()))
    outcome_path.write_text(json.dumps(outcome, indent=2, default=str))
    print(json.dumps({"ok": outcome["ok"], "subchecks": drill.subchecks,
                      "rto_seconds": drill.rto_seconds}, default=str, indent=1))
    return 0 if outcome["ok"] else 1


async def run(d: Drill) -> None:
    # ── 0. Preconditions ─────────────────────────────────────────
    status, body = http("GET", SOURCE_API.rstrip("/") + "/health")
    if status != 200:
        raise DrillFailure(f"source API unhealthy: {status}")
    d.ok("_source_api", "ok")

    # ── 1. Seed identifiable pre-backup records through the REAL API ──
    stamp = uuid.uuid4().hex[:6]
    user_a = {"email": f"r9-owner-{stamp}@example.com",
              "password": f"Drill!{stamp}x9Zq", "name": "R9 Owner"}
    user_b = {"email": f"r9-revoked-{stamp}@example.com",
              "password": f"Drill!{stamp}y8Zp", "name": "R9 Revoked"}
    s, a_reg = api("POST", "/auth/signup", body=user_a)
    if s != 201:
        raise DrillFailure(f"signup A failed: {s} {a_reg}")
    token_a = a_reg["access_token"]
    s, b_reg = api("POST", "/auth/signup", body=user_b)
    if s != 201:
        raise DrillFailure(f"signup B failed: {s} {b_reg}")
    d.ok("_seed_users", "ok")

    # Revoked user: deactivate AFTER signup, BEFORE backup.
    email_b = user_b["email"]
    deactivated = await _pg("UPDATE pg_users SET is_active=false "
                            "WHERE email=$1 RETURNING email", email_b)
    if deactivated != email_b:
        raise DrillFailure(f"deactivation updated no row for {email_b}")
    s, b_login = api("POST", "/auth/login", body={"email": email_b,
                                                  "password": user_b["password"]})
    if s != 401 and s != 403:
        raise DrillFailure(f"deactivated user could still log in on source: {s}")
    d.ok("_seed_revocation", f"deactivated pre-backup; source login now {s}")

    # Old artifact with a KNOWN hash — through the real modeling endpoint.
    s, asset = api("POST", "/modeling/assets", token=token_a,
                   body={"kind": "box", "size": [3.0, 2.0, 1.0],
                         "name": f"r9-artifact-{stamp}"})
    if s != 200:
        raise DrillFailure(f"modeling artifact creation failed: {s} {asset}")
    artifact_id = asset["artifact_id"]
    obj_name = next(f for f in asset["files"] if f.endswith(".obj"))
    s, obj_bytes = http("GET", f"{SOURCE_API.rstrip()}/api/v1/artifacts/"
                               f"{artifact_id}/files/{obj_name}",
                        headers={"Authorization": f"Bearer {token_a}"})
    if s != 200:
        raise DrillFailure(f"artifact download failed on source: {s}")
    artifact_sha = hashlib.sha256(obj_bytes).hexdigest()
    d.receipts["artifact"] = {"artifact_id": artifact_id, "file": obj_name,
                              "sha256": artifact_sha, "bytes": len(obj_bytes)}

    # Durable workflow that must survive and be downloadable after restore.
    s, wf = api("POST", "/workflows", token=token_a,
                body={"goal": "Check health of https://example.com and "
                              "summarize the observed result in one sentence."})
    if s not in (200, 201):
        raise DrillFailure(f"workflow submission failed: {s} {wf}")
    run_id = wf["run_id"]
    deadline = time.time() + 240
    final = None
    while time.time() < deadline:
        s, run = api("GET", f"/workflows/{run_id}", token=token_a)
        if s == 200 and run.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            final = run
            break
        time.sleep(3)
    if not final or final.get("status") != "COMPLETED":
        raise DrillFailure(f"seed workflow did not complete: {final and final.get('status')}")
    d.receipts["seed_workflow"] = {"run_id": run_id, "status": "COMPLETED"}
    d.ok("_seed_workflow", "ok")

    # Deleted item: feedback created then deleted BEFORE backup.
    s, fb = api("POST", "/feedback", token=token_a,
                body={"category": "other", "helpful": True,
                      "comment": "r9 deleted-item probe", "share_diagnostics": False})
    if s != 201:
        raise DrillFailure(f"feedback creation failed: {s} {fb}")
    feedback_id = fb.get("feedback_id") or fb.get("id")
    s, _ = api("DELETE", f"/feedback/{feedback_id}", token=token_a)
    if s not in (200, 204):
        raise DrillFailure(f"feedback deletion failed on source: {s}")
    d.ok("_seed_deletion", f"feedback {feedback_id} deleted pre-backup")

    # ── 2. Watermark + backup ────────────────────────────────────
    watermark_events = int(await _pg("SELECT count(*) FROM events"))
    watermark_workflows = int(await _pg("SELECT count(*) FROM workflows"))
    d.receipts["watermark"] = {"events": watermark_events,
                               "workflows": watermark_workflows}
    # A clean scratch workspace BEFORE the backup lands in it.
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    dump_path = WORKDIR / "dump.sql"
    r = sh(["docker", "exec", "agentos-postgres-1", "pg_dump", "-U", "agentos",
            "agentos_test"], timeout=180)
    if r.returncode != 0 or not r.stdout:
        raise DrillFailure(f"pg_dump failed: {r.stderr.decode()[:200]}")
    dump_path.write_bytes(r.stdout)
    dump_sha = hashlib.sha256(r.stdout).hexdigest()
    d.ok("_backup", {"dump_sha256": dump_sha, "bytes": len(r.stdout)})

    # Artifact bytes + secret material (JWT keys) from the RUNNING app.
    restore_root = WORKDIR / "restored"
    (restore_root / "data").mkdir(parents=True)
    r = sh(["docker", "cp", f"agentos-api-1:{os.environ.get('ARTIFACTS_ROOT', '/app/artifacts')}",
            str(restore_root / "data" / "artifacts")], timeout=300)
    if r.returncode != 0:
        raise DrillFailure(f"artifact bytes copy failed: {r.stderr.decode()[:200]}")
    r = sh(["docker", "cp", "agentos-api-1:/app/backend/security/keys",
            str(restore_root / "keys")], timeout=60)
    if r.returncode != 0:
        raise DrillFailure(f"key material copy failed: {r.stderr.decode()[:200]}")
    d.ok("_backup_bytes_keys", "artifact bytes + JWT key material captured")

    # ── 3. Restore into a SEPARATE stack ─────────────────────────
    sh(["docker", "rm", "-f", "agentos-r9-restore-pg"])
    r = sh(["docker", "run", "-d", "--name", "agentos-r9-restore-pg",
            "-p", f"{RESTORE_PG_PORT}:5432",
            "-e", "POSTGRES_USER=agentos", "-e", "POSTGRES_PASSWORD=agentos",
            "-e", "POSTGRES_DB=agentos_restored", "postgres:16-alpine"], timeout=120)
    if r.returncode != 0:
        raise DrillFailure(f"restore container start failed: {r.stderr.decode()[:200]}")
    deadline = time.time() + 60
    ready = False
    while time.time() < deadline and not ready:
        chk = sh(["docker", "exec", "agentos-r9-restore-pg", "pg_isready",
                  "-U", "agentos"])
        ready = chk.returncode == 0
        if not ready:
            time.sleep(1)
    if not ready:
        raise DrillFailure("restore postgres never became ready")
    r = sh(["docker", "exec", "-i", "agentos-r9-restore-pg", "psql", "-X",
            "-v", "ON_ERROR_STOP=1", "-U", "agentos", "-d", "agentos_restored"],
           input=dump_path.read_bytes(), timeout=300)
    if r.returncode != 0:
        raise DrillFailure(f"restore psql failed: {r.stderr.decode()[:300]}")
    rst_workflows = int(sh(["docker", "exec", "agentos-r9-restore-pg", "psql",
                            "-X", "-At", "-U", "agentos", "-d", "agentos_restored",
                            "-c", "SELECT count(*) FROM workflows"]).stdout)
    rst_events = int(sh(["docker", "exec", "agentos-r9-restore-pg", "psql",
                         "-X", "-At", "-U", "agentos", "-d", "agentos_restored",
                         "-c", "SELECT count(*) FROM events"]).stdout)
    d.receipts["restored_counts"] = {"workflows": rst_workflows,
                                     "events": rst_events}
    if (rst_workflows, rst_events) != (watermark_workflows, watermark_events):
        raise DrillFailure("restored counts diverge from the watermark "
                           f"({rst_workflows}/{rst_events} vs "
                           f"{watermark_workflows}/{watermark_events})")
    d.ok("_restore", "counts match watermark")

    # ── 4. Independent application on :8001 (isolation enforced) ──
    env = dict(os.environ)
    env.update({
        "STORAGE_BACKEND": "postgres",
        "POSTGRES_DSN": f"postgresql://agentos:agentos@localhost:{RESTORE_PG_PORT}/agentos_restored",
        "SQLITE_DB_PATH": str(restore_root / "data" / "agentos.db"),
        "JWT_PRIVATE_KEY_PATH": str(restore_root / "keys" / "private.pem"),
        "JWT_PUBLIC_KEY_PATH": str(restore_root / "keys" / "public.pem"),
        "SECRETS_MASTER_KEY": SECRET,
        "APP_ENV": "development",
        "RUN_WORKER": "1",
        "MESSAGE_BUS_TRANSPORT": "memory",
        "LLM_PROVIDER": "ollama",
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "OLLAMA_MODEL": "qwen2.5:3b-instruct",
        "PORT": str(RESTORE_PORT),
    })
    log = open(WORKDIR / "restored-api.log", "wb")
    api_proc = sp.Popen([str(ROOT / ".venv" / "bin" / "python"), "-m",
                         "uvicorn", "backend.api.main:app",
                         "--host", "127.0.0.1", "--port", str(RESTORE_PORT)],
                        cwd=ROOT, env=env, stdout=log, stderr=sp.STDOUT)
    restore_base = f"http://127.0.0.1:{RESTORE_PORT}"
    try:
        deadline = time.time() + 90
        up = False
        while time.time() < deadline and not up:
            try:
                s, body = http("GET", restore_base + "/health")
                up = s == 200
            except Exception:
                pass
            if not up:
                time.sleep(2)
        if not up:
            raise DrillFailure("restored API never became healthy "
                               "(see restored-api.log)")
        d.ok("_independent_api", {"base": restore_base,
                                  "dsn": env["POSTGRES_DSN"]})
        t_independent = time.time()

        # ── 5. Probes against the RESTORED app ───────────────────
        # 5.1 sign-in with pre-backup credentials (secret material works).
        s, login = api("POST", "/auth/login", base=restore_base,
                       body={"email": user_a["email"],
                             "password": user_a["password"]})
        if s != 200:
            raise DrillFailure(f"restored sign-in failed: {s} {str(login)[:200]}")
        token_r = login["access_token"]
        d.ok("probe_signin", "ok")

        # 5.2 old artifact bytes with the SAME digest.
        s, rb = http("GET", f"{restore_base}/api/v1/artifacts/{artifact_id}"
                            f"/files/{obj_name}",
                     headers={"Authorization": f"Bearer {token_r}"})
        if s != 200:
            raise DrillFailure(f"restored artifact download failed: {s}")
        if hashlib.sha256(rb).hexdigest() != artifact_sha:
            raise DrillFailure("restored artifact bytes diverge from the "
                               "recorded digest")
        d.ok("probe_artifact_digest", {"sha256": artifact_sha,
                                       "bytes": len(rb)})

        # 5.3 revoked user STAYS revoked.
        s, _ = api("POST", "/auth/login", base=restore_base,
                   body={"email": user_b["email"],
                         "password": user_b["password"]})
        if s in (200,):
            raise DrillFailure("revoked user can sign in after restore")
        d.ok("probe_revocation_enforced", f"login refused ({s})")

        # 5.4 deleted feedback STAYS deleted (owner list omits the id).
        s, mine = api("GET", "/feedback/mine", base=restore_base,
                      token=token_r)
        if s != 200:
            raise DrillFailure(f"restored feedback list failed: {s}")
        items = mine.get("feedback") if isinstance(mine, dict) else mine
        ids = {it.get("feedback_id") or it.get("id")
               for it in (items or [])}
        if feedback_id in ids:
            raise DrillFailure("deleted feedback reappeared after restore")
        d.ok("probe_deletion_enforced", "deleted id absent from owner list")

        # 5.5 a NEW verified workflow completes on the restored app.
        s, nwf = api("POST", "/workflows", base=restore_base, token=token_r,
                     body={"goal": "Check health of https://example.com and "
                                   "summarize the observed result in one "
                                   "sentence."})
        if s not in (200, 201):
            raise DrillFailure(f"new workflow submission failed: {s} {nwf}")
        new_run = nwf["run_id"]
        deadline = time.time() + 240
        nfinal = None
        while time.time() < deadline:
            s, run = api("GET", f"/workflows/{new_run}", base=restore_base,
                         token=token_r)
            if s == 200 and run.get("status") in ("COMPLETED", "FAILED",
                                                  "CANCELLED"):
                nfinal = run
                break
            time.sleep(3)
        if not nfinal or nfinal.get("status") != "COMPLETED":
            raise DrillFailure(f"new workflow on restored app: "
                               f"{nfinal and nfinal.get('status')}")
        summaries = [t.get("output_data") or {} for t in nfinal.get("tasks", [])
                     if t.get("agent") == "core.chat"]
        if not summaries or not summaries[0].get("reply"):
            raise DrillFailure("new workflow completed without a persisted "
                               "model reply")
        d.receipts["new_workflow"] = {"run_id": new_run,
                                      "status": "COMPLETED"}
        d.ok("probe_new_verified_workflow", "ok")

        d.receipts["rto_independent_seconds"] = round(time.time() - t_independent, 1)
    finally:
        api_proc.terminate()
        try:
            api_proc.wait(timeout=20)
        except sp.TimeoutExpired:
            api_proc.kill()
        log.close()
        sh(["docker", "rm", "-f", "agentos-r9-restore-pg"])
        d.ok("_cleanup", "restore container removed; independent API stopped")
    d.completed = True


if __name__ == "__main__":
    sys.exit(main())
