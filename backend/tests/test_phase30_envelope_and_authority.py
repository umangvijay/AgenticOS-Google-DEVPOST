"""Phase 30 Batch 2 — envelope encryption, operator authority, classifier
disposition and settlement-conflict reconciliation (30.6–30.8, 30.28).

Covers:
  - env1 tenant/purpose-bound envelopes: roundtrip, wrong tenant/purpose/
    key/tamper refusal, rewrap (wrapping-key rotation keeps the record
    readable without touching the upstream credential)
  - version-qualified managed references (parse + pinned read contract)
  - credentials router dual reader (env1 + legacy vault generations) and
    per-owner isolation
  - bounded resumable envelope backfill: migration, replay idempotence,
    concurrent-conflict classification, corrupt classification, checkpoint
  - named operator actions with separated decrypt/key-admin/approval
    permissions and redacted support receipts
  - failure classifier: ack-lost vs timeout-before-dispatch vs stale page
  - settlement conflict: original preserved, delta booked once through the
    idempotent correction path, replay changes nothing
"""
import os
import json

import pytest
import pytest_asyncio

os.environ.setdefault("SECRETS_MASTER_KEY", "phase30-test-master-key-0123456789abcdef")

from backend.security.key_envelope import (  # noqa: E402
    seal_envelope, open_envelope, rewrap_envelope, is_envelope,
    parse_managed_reference, seal_for_credential, EnvelopeError,
    canonical_aad, decrypt_credential_payload,
)
from backend.security.secrets_vault import SecretsVault  # noqa: E402
from backend.security.permission_matrix import operator_role_allows  # noqa: E402
from backend.services.operator_actions import redact_text  # noqa: E402
from backend.engine.failure_classifier import classify_failure  # noqa: E402
from backend.services.credential_envelope_migration import (  # noqa: E402
    backfill_owner_credentials, MigrationCheckpoint,
)
from backend.models.usage import (  # noqa: E402
    SettlementReceipt, MeasurementSource, BudgetScope, ReservationRequest,
)
from backend.repositories.sqlite.database import DatabaseManager  # noqa: E402
from backend.repositories.usage_repository import (  # noqa: E402
    SQLiteUsageRepository, BucketLimit,
)
from backend.services.budget_service import BudgetService  # noqa: E402
from backend.api.routers.credentials import load_credential  # noqa: E402

BIND = dict(tenant_id="tenant-a", credential_id="cred:app", purpose="credential-store")


@pytest.fixture(autouse=True)
def _initialized_vault_singleton():
    """The credentials router reads through the vault singleton. Other
    suites may have initialized it with an ephemeral/other master — pin
    BOTH the singleton and the envelope's ephemeral master to this file's
    test key so ordering cannot poison the crypto."""
    from backend.security.secrets_vault import secrets_vault
    from backend.security import key_envelope
    secrets_vault._master_key = os.environ["SECRETS_MASTER_KEY"]
    secrets_vault._initialized = True
    secrets_vault.use_gcp = False
    key_envelope._EPHEMERAL_MASTER = None
    yield


class FakeGCPClient:
    """Matches the GCPSecretManager surface decrypt_credential_payload uses."""

    def __init__(self):
        self.versions = {}
        self.reads = []
        self.counter = 0

    def get_secret(self, secret_id):
        self.reads.append((secret_id, "latest"))
        return self.versions[secret_id][-1][1]

    def get_secret_version(self, secret_id, version):
        self.reads.append((secret_id, version))
        for v, val in self.versions[secret_id]:
            if v == version:
                return val
        raise KeyError(f"version {version} not found")

    def set_secret(self, secret_id, payload):
        self.counter += 1
        version = str(self.counter)
        self.versions.setdefault(secret_id, []).append((version, payload))
        return version


# ── envelope basics (30.7 step 2) ──────────────────────────────────────

def test_envelope_roundtrip_and_binding_refusals():
    sealed = seal_envelope("payload-value", **BIND)
    assert is_envelope(sealed)
    assert open_envelope(sealed, **BIND) == "payload-value"
    with pytest.raises(EnvelopeError):
        open_envelope(sealed, tenant_id="tenant-b", **{k: v for k, v in BIND.items() if k != "tenant_id"})
    with pytest.raises(EnvelopeError):
        open_envelope(sealed, **{**BIND, "purpose": "different-purpose"})
    with pytest.raises(EnvelopeError):
        open_envelope(sealed, **{**BIND, "credential_id": "cred:other"})


def test_envelope_wrong_key_and_tamper_refused(monkeypatch):
    sealed = seal_envelope("payload-value", **BIND)
    tampered = sealed[:-6] + ("AAAAAA" if not sealed.endswith("AAAAAA") else "BBBBBB")
    with pytest.raises(EnvelopeError):
        open_envelope(tampered, **BIND)
    # A different master key (fresh process state) must not open the record.
    from backend.security import key_envelope as ke
    monkeypatch.setattr(ke, "_local_kek", lambda version: os.urandom(32))
    with pytest.raises(EnvelopeError):
        open_envelope(sealed, **BIND)


def test_rewrap_rotates_wrapping_key_without_changing_credential():
    sealed = seal_envelope("rotate-me", **BIND)
    rewrapped = rewrap_envelope(sealed, to_kid="local:v2")
    assert rewrapped != sealed
    assert open_envelope(rewrapped, **BIND) == "rotate-me"
    # The old wrapping is still readable while both keys exist (dual read).
    assert open_envelope(sealed, **BIND) == "rotate-me"


def test_managed_reference_parsing_and_pinned_read(monkeypatch):
    assert parse_managed_reference("gcp://cred-u1-stripe") == ("cred-u1-stripe", None)
    assert parse_managed_reference("gcp://cred-u1-stripe@7") == ("cred-u1-stripe", "7")

    fake = FakeGCPClient()
    fake.set_secret("cred-u1-stripe", "value-v1")
    fake.set_secret("cred-u1-stripe", "value-v2")
    from backend.security import gcp_secret_manager as gcp_mod
    monkeypatch.setattr(gcp_mod, "GCPSecretManager", lambda: fake)
    # A pinned reference keeps resolving to the version it was written
    # with even after upstream rotation added version 2.
    assert decrypt_credential_payload(
        "gcp://cred-u1-stripe@1", tenant_id="t", credential_id="c", purpose="p",
    ) == "value-v1"
    assert fake.reads[-1] == ("cred-u1-stripe", "1")


def _fresh_vault() -> SecretsVault:
    vault = SecretsVault()
    vault._master_key = os.environ["SECRETS_MASTER_KEY"]
    vault._initialized = True
    vault.use_gcp = False
    return vault


def test_dual_reader_reads_every_generation():
    vault = _fresh_vault()
    legacy_v2 = vault.encrypt("legacy-secret")
    envelope = seal_envelope("envelope-secret", **BIND)
    assert decrypt_credential_payload(envelope, vault=vault, **BIND) == "envelope-secret"
    assert decrypt_credential_payload(legacy_v2, vault=vault, **BIND) == "legacy-secret"


def test_canonical_aad_binds_every_component():
    a1 = canonical_aad("t", "c", "p")
    assert a1 != canonical_aad("t2", "c", "p")
    assert a1 != canonical_aad("t", "c2", "p")
    assert a1 != canonical_aad("t", "c", "p2")
    with pytest.raises(EnvelopeError):
        canonical_aad("", "c", "p")


# ── credentials router dual reader (30.7 step 3) ───────────────────────

class FakeSecretsRepo:
    def __init__(self):
        self.rows = {}

    async def store_secret(self, user_id, key, value):
        self.rows[(user_id, key)] = value

    async def get_secret(self, user_id, key):
        return self.rows.get((user_id, key))

    async def list_secret_keys(self, user_id):
        return [k for (u, k) in self.rows if u == user_id]

    async def delete_secret(self, user_id, key):
        return self.rows.pop((user_id, key), None) is not None

    async def store_secret_if_unchanged(self, user_id, key, new, expected):
        if self.rows.get((user_id, key)) == expected:
            self.rows[(user_id, key)] = new
            return True
        return False


@pytest.mark.asyncio
async def test_load_credential_reads_envelope_and_isolates_owners():
    repo = FakeSecretsRepo()
    await repo.store_secret("owner-1", "cred:stripe", seal_for_credential(
        json.dumps({"api_key": "sk-1"}), owner_id="owner-1", name="stripe"))
    values = await load_credential(repo, "owner-1", "stripe")
    assert values == {"api_key": "sk-1"}
    # A second owner's namespace has no such credential at all.
    with pytest.raises(ValueError):
        await load_credential(repo, "owner-2", "stripe")


@pytest.mark.asyncio
async def test_load_credential_still_reads_legacy_vault_generation():
    repo = FakeSecretsRepo()
    vault = _fresh_vault()
    await repo.store_secret("owner-1", "cred:smtp", vault.encrypt(json.dumps({"password": "p"})))
    assert await load_credential(repo, "owner-1", "smtp") == {"password": "p"}


# ── bounded resumable backfill (30.7 step 3) ───────────────────────────

@pytest.mark.asyncio
async def test_backfill_migrates_legacy_and_is_idempotent_on_replay():
    repo = FakeSecretsRepo()
    vault = _fresh_vault()
    legacy = {name: vault.encrypt(json.dumps({"api_key": f"key-{name}"}))
              for name in ("stripe", "smtp", "gemini")}
    for name, enc in legacy.items():
        await repo.store_secret("owner-1", f"cred:{name}", enc)

    report = await backfill_owner_credentials(repo, "owner-1", page_size=2)
    assert report.counts["scanned"] == 2
    assert report.counts["migrated"] == 2
    assert not report.done
    report2 = await backfill_owner_credentials(
        repo, "owner-1", page_size=2,
        checkpoint=MigrationCheckpoint.from_dict(report.checkpoint.to_dict()))
    assert report2.counts["migrated"] == 1
    assert report2.done

    # Every migrated record decrypts to the identical plaintext.
    for name in legacy:
        values = await load_credential(repo, "owner-1", name)
        assert values == {"api_key": f"key-{name}"}

    # Replay of a COMPLETED page: everything already migrated, zero writes.
    report3 = await backfill_owner_credentials(repo, "owner-1", page_size=10)
    assert report3.counts["already_envelope"] == 3
    assert report3.counts["migrated"] == 0


@pytest.mark.asyncio
async def test_backfill_classifies_concurrent_conflict_and_corrupt():
    repo = FakeSecretsRepo()
    vault = _fresh_vault()
    await repo.store_secret("owner-1", "cred:good", vault.encrypt('{"api_key":"g"}'))
    await repo.store_secret("owner-1", "cred:bad", "not-valid-ciphertext")

    # Concurrent rotation: CAS expected value no longer matches.
    async def rotating_cas(user_id, key, new, expected):
        await repo.store_secret(user_id, key, "rotated-value")
        return False
    repo.store_secret_if_unchanged = rotating_cas
    report = await backfill_owner_credentials(repo, "owner-1", page_size=10)
    assert report.counts["conflict"] == 1
    assert report.counts["corrupt"] == 1


@pytest.mark.asyncio
async def test_backfill_skips_managed_references():
    repo = FakeSecretsRepo()
    await repo.store_secret("owner-1", "cred:gcp", "gcp://cred-owner-1-gcp@3")
    report = await backfill_owner_credentials(repo, "owner-1", page_size=10)
    assert report.counts["managed"] == 1
    assert report.counts["migrated"] == 0


# ── named operator actions (30.6) ──────────────────────────────────────

def test_operator_actions_are_named_and_separated():
    assert operator_role_allows("admin", "ops.incident.inspect")
    assert operator_role_allows("admin", "ops.job.cancel")
    assert operator_role_allows("admin", "ops.effect.reconcile")
    assert operator_role_allows("admin", "ops.credential.rotate")
    assert operator_role_allows("admin", "ops.policy.approve")
    # Decrypt, key administration and restore are ABOVE plain admin.
    assert not operator_role_allows("admin", "ops.key.administer")
    assert not operator_role_allows("admin", "ops.backup.restore")
    assert operator_role_allows("super_admin", "ops.key.administer")
    assert operator_role_allows("super_admin", "ops.backup.restore")
    # Non-operators and unknown actions are denied.
    assert not operator_role_allows("user", "ops.incident.inspect")
    assert not operator_role_allows("viewer", "ops.job.cancel")
    assert not operator_role_allows("admin", "ops.made.up")


def test_support_receipts_are_redacted():
    text = redact_text(
        "Authorization: Bearer sk-live-0123456789abcdef api_key=xyz token: q-w-e-r "
        "gcp://proj/secret@1 password=hunter2 count=3")
    assert "sk-live-0123456789abcdef" not in text
    assert "xyz" not in text and "hunter2" not in text and "q-w-e-r" not in text
    assert "gcp://" in text and "proj/secret" not in text
    assert "count=3" in text  # ordinary fields survive


# ── classifier disposition (30.28) ─────────────────────────────────────

def test_timeout_before_dispatch_is_retryable_but_after_dispatch_is_not():
    before = classify_failure("request timed out", dispatched=False)
    assert before.category == "timeout_before_dispatch" and before.retryable
    after = classify_failure("request timed out", dispatched=True)
    assert after.category == "ack_lost" and not after.retryable
    assert "reconcile" in after.recovery_hint.lower()
    # Unspecified dispatch keeps the historical generic timeout behaviour.
    assert classify_failure("request timed out").category == "timeout"


def test_lost_acknowledgement_and_stale_page_classes():
    lost = classify_failure("acknowledgement lost after the write was sent")
    assert lost.category == "ack_lost" and not lost.retryable
    stale = classify_failure("action refused: document generation changed (stale)")
    assert stale.category == "stale_page" and not stale.retryable
    assert "fresh observation" in stale.recovery_hint.lower()


# ── settlement conflict reconciliation (30.8/30.32) ────────────────────

@pytest_asyncio.fixture
async def ledger(tmp_path):
    manager = DatabaseManager(str(tmp_path / "conflict.db"))
    await manager.initialize()
    repo = SQLiteUsageRepository(manager)
    service = BudgetService(repo)
    yield repo, service
    await manager.close()


@pytest.mark.asyncio
async def test_conflicting_receipt_preserves_original_and_books_delta_once(ledger):
    repo, service = ledger
    reservation = await repo.reserve(
        ReservationRequest(user_id="u-conflict", units=1500),
        [BucketLimit(BudgetScope.USER, "u-conflict", 10000)],
    )
    await repo.mark_dispatching(reservation.reservation_id)
    await repo.settle(reservation.reservation_id, SettlementReceipt(
        reservation_id=reservation.reservation_id, units=1200,
        measurement_source=MeasurementSource.PROVIDER_REPORTED))
    original_digest = (await repo.get_reservation(reservation.reservation_id)).receipt_digest

    # The conflicting final receipt does NOT overwrite the settlement…
    with pytest.raises(Exception):
        await service.settle(reservation.reservation_id, SettlementReceipt(
            reservation_id=reservation.reservation_id, units=900,
            measurement_source=MeasurementSource.PROVIDER_REPORTED))
    # …it enters reconciliation: the delta (−300) is booked exactly once.
    outcome = await service.reconcile_conflicting_receipt(
        reservation.reservation_id, SettlementReceipt(
            reservation_id=reservation.reservation_id, units=900,
            measurement_source=MeasurementSource.PROVIDER_REPORTED))
    assert outcome["delta"] == -300
    assert outcome["original_settled_units"] == 1200
    assert outcome["original_receipt_digest"] == original_digest

    snapshot = await service.get_user_snapshot("u-conflict")
    assert snapshot.consumed == 900  # 1200 booked − 300 correction

    # Two reconcilers processing the same late receipt: no double correction.
    await service.reconcile_conflicting_receipt(
        reservation.reservation_id, SettlementReceipt(
            reservation_id=reservation.reservation_id, units=900,
            measurement_source=MeasurementSource.PROVIDER_REPORTED))
    snapshot_again = await service.get_user_snapshot("u-conflict")
    assert snapshot_again.consumed == 900


@pytest.mark.asyncio
async def test_conflicting_receipt_reconciliation_requires_settled_reservation(ledger):
    repo, service = ledger
    reservation = await repo.reserve(
        ReservationRequest(user_id="u-unsettled", units=100),
        [BucketLimit(BudgetScope.USER, "u-unsettled", 10000)],
    )
    from backend.models.usage import BudgetError, BudgetErrorCode
    with pytest.raises(BudgetError) as exc:
        await service.reconcile_conflicting_receipt(
            reservation.reservation_id, SettlementReceipt(
                reservation_id=reservation.reservation_id, units=50))
    assert exc.value.code == BudgetErrorCode.RESERVATION_STATE_CONFLICT
