"""Local Phase 26 recovery probe. Run with the repository's Python and an output JSON path."""
import asyncio
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

import asyncpg
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from source_identity import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]
BASE_DSN = "postgresql://agentos:agentos@127.0.0.1:55432/"
PYTHON = sys.executable


async def database(name, create):
    if not re.fullmatch(r"p26_drill_(src|dst)_[0-9a-f]{32}", name):
        raise ValueError("Expected a generated fixture database name")
    conn = await asyncpg.connect(BASE_DSN + "postgres", timeout=10)
    try:
        await conn.execute(('CREATE' if create else 'DROP') + f' DATABASE "{name}"')
    finally:
        await conn.close()


async def database_counts(name):
    tables = ("pg_users", "pg_api_keys", "pg_secrets", "pg_memories", "workflows",
              "tasks", "events", "schema_migrations", "pg_token_denylist")
    conn = await asyncpg.connect(BASE_DSN + name, timeout=10)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            return {table: await conn.fetchval(f"SELECT count(*) FROM {table}") for table in tables}
    finally:
        await conn.close()


def pg(*args, data=None):
    return subprocess.run(["docker", "exec", "-i", "agentos-postgres-1", *args], input=data,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=80, check=True).stdout


def environment(folder, db):
    return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG")} | {
        "PYTHONPATH": str(ROOT), "APP_ENV": "development", "RUN_WORKER": "1", "STORAGE_BACKEND": "postgres",
        "MESSAGE_BUS_TRANSPORT": "inprocess", "POSTGRES_DSN": BASE_DSN + db,
        "SQLITE_DB_PATH": str(folder / "data/local.db"), "JWT_PRIVATE_KEY_PATH": str(folder / "keys/private.pem"),
        "JWT_PUBLIC_KEY_PATH": str(folder / "keys/public.pem"), "SECRETS_MASTER_KEY": (folder / "keys/vault").read_text(),
        "LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": "http://127.0.0.1:11434", "OLLAMA_MODEL": "qwen2.5:3b-instruct"}


@contextmanager
def server(folder, env):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (folder / "server.log").open("wb") as log:
        proc = subprocess.Popen([PYTHON, "-m", "uvicorn", "backend.api.main:app", "--host", "127.0.0.1",
                                 "--port", str(port)], cwd=folder, env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=90, trust_env=False) as client:
                limit = time.monotonic() + 30
                while time.monotonic() < limit and proc.poll() is None:
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.15)
                else:
                    raise RuntimeError("Isolated application startup failed")
                yield client
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def request(client, method, path, status, token=None, body=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    result = client.request(method, "/api/v1" + path, headers=headers, json=body)
    if result.status_code != status:
        raise AssertionError(f"{method} {path}: expected {status}, received {result.status_code}")
    return result


def verified_workflow(client, token, marker):
    goal = "Check the health of https://example.com?restore=" + marker
    run = request(client, "POST", "/workflows", 201, token, {"goal": goal}).json()
    run_id = run["run_id"]
    limit = time.monotonic() + 180
    while time.monotonic() < limit:
        result = request(client, "GET", "/workflows/" + run_id, 200, token).json()
        if result["status"] in ("COMPLETED", "FAILED", "NEEDS_REVIEW", "CANCELLED"):
            break
        time.sleep(0.5)
    if result["status"] != "COMPLETED":
        raise AssertionError("Workflow ended " + result["status"])
    outputs = [task["output_data"] for task in result["tasks"]]
    encoded = json.dumps(outputs, sort_keys=True).encode()
    if b"example.com" not in encoded.lower() or not any(
        field in encoded.lower() for field in (b"tls", b"expires_at")
    ):
        raise AssertionError("Expected a TLS result for the requested host")
    return run_id, hashlib.sha256(encoded).hexdigest()


def child(folder, env, code, payload):
    proc = subprocess.run([PYTHON, "-c", code], cwd=folder, env=env, input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30)
    if proc.returncode:
        # Surface the helper's own traceback (a bare exit code cannot be
        # diagnosed — §32.21 drill diagnosability).
        raise RuntimeError("Application recovery helper exited "
                           + str(proc.returncode) + ": "
                           + (proc.stderr or "")[-400:])
    return proc.stdout.strip().splitlines()[-1]


ARTIFACT = """
import sys,json
from backend.services.artifact_builder import save_user_files
p=json.load(sys.stdin)
print(save_user_files(p['uid'],'recovery-fixture',[{'path':'index.html','content':p['body']}])['artifact_id'])
"""
LEGACY_SEED = """
import asyncio,sys,json
from backend.config.settings import settings
from backend.repositories.postgres.database import PostgresDatabaseManager
from backend.repositories.postgres.repositories import PostgresSecretsRepository
from backend.security.secrets_vault import SecretsVault
async def seed():
    p=json.load(sys.stdin)
    db=PostgresDatabaseManager(settings.POSTGRES_DSN)
    await db.initialize()
    try:
        vault = SecretsVault(); vault.initialize()
        repo = PostgresSecretsRepository(db)
        await repo.store_secret(p['uid'], 'cred:recovery-legacy',
                                vault.encrypt(json.dumps({'key': p['key']})))
        print('seeded')
    finally:
        await db.close()
asyncio.run(seed())
"""

VAULT_PROOF = """
import asyncio,sys,json,hmac,hashlib
from backend.config.settings import settings
from backend.repositories.postgres.database import PostgresDatabaseManager
from backend.repositories.postgres.repositories import PostgresSecretsRepository
from backend.api.routers.credentials import load_credential
async def verify():
    p=json.load(sys.stdin)
    db=PostgresDatabaseManager(settings.POSTGRES_DSN)
    await db.initialize()
    try:
        # Read the credential through the APPLICATION's own dual reader
        # (30.7: envelope / managed reference / legacy vault ciphertext)
        # — the drill must not reimplement decryption beside the product.
        values = await load_credential(PostgresSecretsRepository(db), p['uid'], 'recovery-probe')
        key = values['key']
        # 30.7/30.43: BOTH envelope generations must decrypt after restore —
        # the env1 record written by the API and the legacy v2 vault record
        # seeded directly into the pre-backup database.
        legacy = await load_credential(PostgresSecretsRepository(db), p['uid'], 'recovery-legacy')
        # A wrong-tenant open of the env1 record is refused before any
        # plaintext exists (the binding is authenticated, not readable).
        from backend.security.key_envelope import is_envelope, open_envelope, EnvelopeError
        repo = PostgresSecretsRepository(db)
        stored = await repo.get_secret(p['uid'], 'cred:recovery-probe')
        wrong_tenant_refused = False
        if is_envelope(stored):
            try:
                open_envelope(stored, tenant_id=p['peer_uid'], credential_id='cred:recovery-probe', purpose='credential-store')
            except EnvelopeError:
                wrong_tenant_refused = True
        print(json.dumps({
            'proof': hmac.new(key.encode(),p['challenge'].encode(),hashlib.sha256).hexdigest(),
            'legacy_key_sha256': hashlib.sha256(legacy['key'].encode()).hexdigest(),
            'wrong_tenant_refused': wrong_tenant_refused,
        }))
    finally:
        await db.close()
asyncio.run(verify())
"""


def drill(record, work):
    marker = uuid.uuid4().hex
    source_db, recovered_db = "p26_drill_src_" + marker, "p26_drill_dst_" + marker
    source, recovered = work / "source", work / "restored"
    (source / "keys").mkdir(parents=True)
    (source / "keys/vault").write_text(secrets.token_hex(32))
    recovered.mkdir()
    made = []
    checks = record["checks"]
    email, password = marker + "@example.test", "Recovery9!" + secrets.token_hex(16)
    body = "<p>Owned artifact " + marker + "</p>"
    credential = secrets.token_hex(32)
    try:
        asyncio.run(database(source_db, True))
        made.append(source_db)
        env = environment(source, source_db)
        with server(source, env) as client:
            signup = request(client, "POST", "/auth/signup", 201, body={"name":"Recovery Fixture","email":email,"password":password}).json()
            uid, token = signup["user"]["id"], signup["access_token"]
            checks["real_signup"] = True
            login = {"email":email, "password":password}
            revoked = request(client, "POST", "/auth/login", 200, body=login).json()["access_token"]
            request(client, "POST", "/auth/logout", 204, revoked)
            request(client, "GET", "/auth/me", 401, revoked)
            api_key = request(client, "POST", "/auth/api-keys", 201, token, {"name":"restore-live"}).json()
            retired_key = request(client, "POST", "/auth/api-keys", 201, token, {"name":"restore-retired"}).json()
            request(client, "GET", "/auth/me", 200, api_key["key"])
            request(client, "DELETE", "/auth/api-keys/" + retired_key["id"], 200, token)
            request(client, "GET", "/auth/me", 401, retired_key["key"])
            request(client, "POST", "/credentials", 201, token, {"name":"recovery-probe","values":{"key":credential}})
            legacy_credential = secrets.token_hex(32)
            # Seed the PREVIOUS envelope generation directly (a record that
            # pre-dates the env1 writer): the restore must keep it readable.
            child(source, env, LEGACY_SEED, {"uid":uid,"key":legacy_credential})
            artifact = child(source, env, ARTIFACT, {"uid":uid,"body":body})
            path = f"/artifacts/{artifact}/files/index.html"
            assert request(client, "GET", path, 200, token).content == body.encode()
            removed = request(client,"POST","/memory",201,token,{"content":"Retire " + marker}).json()["memory_id"]
            request(client,"DELETE","/memory/" + removed,200,token)
            previous_id, previous_digest = verified_workflow(client, token, "before-" + marker)
            record["previous_workflow_run_id"] = previous_id
            record["previous_workflow_output_sha256"] = previous_digest
            last = request(client,"POST","/memory",201,token,{"content":"Durable marker " + marker}).json()["memory_id"]
            last_ack = time.monotonic()
        # Quiesce fixture writes before capturing its database and files.
        snapshot = time.monotonic()
        record["source_counts"] = asyncio.run(database_counts(source_db))
        dump = pg("pg_dump", "-U", "agentos", "-d", source_db, "--no-owner", "--no-privileges")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            tar.add(source / "keys", arcname="keys")
            tar.add(source / "data/artifacts", arcname="data/artifacts")
        envelope_key = AESGCM.generate_key(bit_length=256)
        cipher, nonce = AESGCM(envelope_key), os.urandom(12)
        encrypted = cipher.encrypt(nonce, archive.getvalue(), marker.encode())
        checks["source_backed_up"] = True
        record["dump_sha256"] = hashlib.sha256(dump).hexdigest()
        started = time.monotonic()
        asyncio.run(database(recovered_db, True))
        made.append(recovered_db)
        pg("psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "agentos", "-d", recovered_db, data=dump)
        with tarfile.open(fileobj=io.BytesIO(cipher.decrypt(nonce, encrypted, marker.encode())), mode="r:gz") as tar:
            tar.extractall(recovered, filter="data")
        record["restored_counts"] = asyncio.run(database_counts(recovered_db))
        if record["source_counts"] != record["restored_counts"]:
            raise ValueError("Recovered table counts differ")
        checks["restored_database_integrity"] = True
        restored_env = environment(recovered, recovered_db)
        with server(recovered, restored_env) as client:
            assert request(client,"GET","/auth/me",200,token).json()["id"] == uid
            checks["previously_issued_token"] = True
            active = request(client,"POST","/auth/login",200,body=login).json()["access_token"]
            checks["new_login"] = True
            request(client,"GET","/auth/me",401,revoked)
            checks["logout_still_enforced"] = True
            request(client,"GET","/auth/me",200,api_key["key"])
            request(client,"GET","/auth/me",401,retired_key["key"])
            key_rows = request(client,"GET","/auth/api-keys",200,active).json()["keys"]
            retired = next(row for row in key_rows if row["id"] == retired_key["id"])
            assert retired["revoked_at"] and not retired["is_active"]
            assert all("key_hash" not in row and "key" not in row for row in key_rows)
            checks["api_key_use_and_revocation"] = True
            assert request(client,"GET",path,200,active).content == body.encode()
            record["download_sha256"] = hashlib.sha256(body.encode()).hexdigest()
            checks["previous_download"] = True
            previous_path = "/workflows/" + previous_id
            previous = request(client, "GET", previous_path, 200, active).json()
            outputs = [task["output_data"] for task in previous["tasks"]]
            restored_digest = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest()
            if previous["status"] != "COMPLETED" or restored_digest != previous_digest:
                raise AssertionError("Restored workflow state or task output differs")
            checks["previous_workflow_and_task_outputs"] = True
            peer_signup = request(client, "POST", "/auth/signup", 201, body={
                "name": "Recovery Peer", "email": "peer-" + email, "password": password
            }).json()
            peer_uid, peer = peer_signup["user"]["id"], peer_signup["access_token"]
            request(client, "GET", path, 404, peer)
            request(client, "GET", previous_path, 403, peer)
            request(client, "DELETE", "/auth/api-keys/" + api_key["id"], 404, peer)
            request(client, "GET", "/auth/me", 200, api_key["key"])
            checks["restored_ownership_enforced"] = True
            rows = request(client,"GET","/memory",200,active).json()["memories"]
            assert {m["id"] for m in rows} == {last} and rows[0]["content"] == "Durable marker " + marker
            checks["acknowledged_write_and_deletion"] = True
            challenge = secrets.token_hex(32)
            vault_result = json.loads(child(recovered, restored_env, VAULT_PROOF, {
                "uid": uid, "challenge": challenge, "peer_uid": peer_uid,
                "legacy_sha256": hashlib.sha256(legacy_credential.encode()).hexdigest(),
            }))
            assert hmac.compare_digest(vault_result["proof"], hmac.new(credential.encode(), challenge.encode(), hashlib.sha256).hexdigest())
            checks["restored_vault_credential_challenge"] = True
            # 30.7/30.43: both envelope generations decrypt after restore.
            assert hmac.compare_digest(vault_result["legacy_key_sha256"], hashlib.sha256(legacy_credential.encode()).hexdigest())
            checks["restored_both_envelope_generations"] = True
            assert vault_result["wrong_tenant_refused"] is True
            checks["restored_wrong_tenant_refused"] = True
            rid, digest = verified_workflow(client, active, "after-" + marker)
            checks["new_workflow_verified"] = True
            record["workflow_run_id"] = rid
            record["new_workflow_output_sha256"] = digest
            record["recovery_seconds_through_verified_workflow"] = round(time.monotonic()-started,3)
            record["last_ack_to_snapshot_seconds"] = round(snapshot-last_ack,3)
    finally:
        for name in reversed(made):
            asyncio.run(database(name, False))
        checks["temporary_databases_removed"] = True


if __name__ == "__main__":
    if len(sys.argv) != 2 or not __debug__:
        raise SystemExit("Run without Python optimization and supply one fresh JSON output path")
    os.umask(0o077)
    output = Path(sys.argv[1])
    if output.exists():
        raise SystemExit("Use a fresh evidence output path")
    result = {"profile":"local PostgreSQL, independent API and in-process worker, quiesced fixture",
              "checks":{}, "passed":False}
    result["source_sha256"] = source_fingerprint(ROOT)[0]
    try:
        with tempfile.TemporaryDirectory(prefix="agentos-p26-recovery-") as d:
            drill(result, Path(d))
        result["end_source_sha256"] = source_fingerprint(ROOT)[0]
        if result["source_sha256"] != result["end_source_sha256"]:
            raise RuntimeError("Source changed during the recovery drill")
        result["passed"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:300]
    with output.open("x") as receipt:
        json.dump(result, receipt, indent=2)
        receipt.write("\n")
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result["passed"] else 1)
