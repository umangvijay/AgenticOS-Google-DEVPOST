"""Phase 3 §3.21: admission policy inventory + typed decisions (§3.3/§3.8/
§3.9). Verifies that every protected group resolves to a KNOWN policy, that
unknown groups fail loudly, and that the four enforcement modes (shared,
local_only, local_emergency, disabled_by_policy) plus fail-closed outage
behavior are observable in the decision — not just a raised exception."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.models.admission import (
    AdmissionPolicy,
    AdmissionPolicyError,
    AdmissionReason,
    EnforcementMode,
)
from backend.security.admission_policy import (
    RATE_LIMIT_GROUPS,
    register_policy,
    resolve_policy,
)
from backend.security.shared_rate_limiter import (
    SQLiteSharedRateRepository,
    admission_metrics,
    set_shared_rate_repo,
)


@pytest_asyncio.fixture
async def shared_db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "policy.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def with_shared_repo(shared_db):
    set_shared_rate_repo(SQLiteSharedRateRepository(shared_db))
    yield shared_db
    set_shared_rate_repo(None)


# ── §3.3: the inventory is total and loud about gaps ──────────────────

def test_every_protected_group_resolves_to_a_policy():
    for group in ("auth_login", "auth_signup", "auth_refresh", "contact_submit",
                  "workflow_create", "integration_build", "resume_generate",
                  "studio_generate", "webhook_ingress", "browser_start",
                  "provider_request", "event_stream", "general_read"):
        policy = resolve_policy(group)
        assert policy.group == group
        assert policy.limit >= 0 and policy.window_seconds > 0


def test_legacy_aliases_map_to_canonical_policies():
    for alias, canonical in (("auth", "auth_login"), ("contact", "contact_submit"),
                             ("webhook", "webhook_ingress"),
                             ("workflow", "workflow_create"),
                             ("mcp_build", "integration_build"),
                             ("general", "general_read"),
                             ("resume_analyze", "resume_generate"),
                             ("resume_tailor", "resume_generate"),
                             ("capabilities", "studio_generate")):
        assert resolve_policy(alias).group == canonical


def test_unknown_group_is_a_configuration_error():
    with pytest.raises(AdmissionPolicyError):
        resolve_policy("totally_made_up")


@pytest.mark.asyncio
async def test_alias_shares_the_canonical_allowance(with_shared_repo):
    """`workflow` and `workflow_create` must count against ONE window, not two."""
    from backend.security.rate_limiter import admission_decision
    d1 = await admission_decision("user:alias", "workflow")
    d2 = await admission_decision("user:alias", "workflow_create")
    assert d1.allowed and d2.allowed
    assert d1.enforcement == EnforcementMode.SHARED
    # Second call consumed slot 2 of the SAME counter (limit 10 by default).
    assert d2.remaining == d1.remaining - 1


def test_rate_limit_groups_compat_map_matches_registry():
    assert RATE_LIMIT_GROUPS["general"] == resolve_policy("general").limit
    assert RATE_LIMIT_GROUPS["auth"] == resolve_policy("auth_login").limit


def test_registered_policy_validates_on_insert():
    with pytest.raises(AdmissionPolicyError):
        register_policy(AdmissionPolicy(group="bad", limit=-5))


# ── §3.8: typed decisions and their HTTP contract ─────────────────────

@pytest.mark.asyncio
async def test_shared_denial_carries_reason_headers_and_body(with_shared_repo):
    from backend.security.rate_limiter import admission_decision
    policy = resolve_policy("integration_build")
    decision = None
    for _ in range(policy.limit + 1):
        decision = await admission_decision("user:d", "integration_build")
    assert decision.allowed is False
    assert decision.reason == AdmissionReason.RATE_LIMITED
    assert decision.enforcement == EnforcementMode.SHARED
    assert decision.retry_after_seconds >= 1
    assert decision.headers()["Retry-After"] == str(decision.retry_after_seconds)
    body = decision.to_detail()
    assert body["code"] == "RATE_LIMITED"
    assert body["accepted"] is False
    assert body["decision_id"] == decision.decision_id


@pytest.mark.asyncio
async def test_empty_identity_is_policy_invalid_not_unlimited(with_shared_repo):
    from backend.security.rate_limiter import admission_decision
    decision = await admission_decision("   ", "workflow_create")
    assert decision.allowed is False
    assert decision.reason == AdmissionReason.POLICY_INVALID


@pytest.mark.asyncio
async def test_zero_limit_policy_denies_everything(with_shared_repo):
    from backend.security.rate_limiter import admission_decision
    register_policy(AdmissionPolicy(group="maintenance_hold", limit=0))
    try:
        decision = await admission_decision("user:any", "maintenance_hold")
    finally:
        from backend.security.admission_policy import _POLICIES
        _POLICIES.pop("maintenance_hold", None)
    assert decision.allowed is False
    assert decision.reason == AdmissionReason.RATE_LIMITED
    assert decision.enforcement == EnforcementMode.DISABLED_BY_POLICY


# ── §3.9: outage behavior per workload ────────────────────────────────

class _BrokenRepo:
    async def admit(self, *args, **kwargs):
        raise RuntimeError("simulated store outage")

    async def purge_expired(self, older_than_epoch):
        return 0


@pytest.mark.asyncio
async def test_fail_open_group_degrades_to_local_emergency():
    from backend.security.rate_limiter import admission_decision
    set_shared_rate_repo(_BrokenRepo())
    try:
        decision = await admission_decision("user:o", "auth_login")
    finally:
        set_shared_rate_repo(None)
    assert decision.allowed is True                      # availability preserved
    assert decision.degraded is True                     # …but labeled degraded
    assert decision.enforcement == EnforcementMode.LOCAL_EMERGENCY
    assert admission_metrics()["counters"].get(
        "local_fallback_activations{group=auth_login}") >= 1


@pytest.mark.asyncio
async def test_fail_closed_group_refuses_with_admission_unavailable():
    from backend.security.rate_limiter import admission_decision
    set_shared_rate_repo(_BrokenRepo())
    try:
        decision = await admission_decision("user:o", "workflow_create")
    finally:
        set_shared_rate_repo(None)
    assert decision.allowed is False
    assert decision.reason == AdmissionReason.ADMISSION_UNAVAILABLE
    assert decision.degraded is True
    assert admission_metrics()["counters"].get(
        "store_errors{group=workflow_create}") >= 1


@pytest.mark.asyncio
async def test_local_only_group_never_touches_the_store():
    from backend.security.rate_limiter import admission_decision
    set_shared_rate_repo(_BrokenRepo())
    try:
        decision = await admission_decision("user:o", "general_read")
    finally:
        set_shared_rate_repo(None)
    assert decision.allowed is True
    assert decision.enforcement == EnforcementMode.LOCAL_ONLY
    assert decision.degraded is False


@pytest.mark.asyncio
async def test_no_store_configured_is_local_only():
    from backend.security.rate_limiter import admission_decision
    set_shared_rate_repo(None)
    decision = await admission_decision("user:o", "workflow_create")
    assert decision.allowed is True
    assert decision.enforcement == EnforcementMode.LOCAL_ONLY


# ── §3.7/§3.21: trustworthy identities ────────────────────────────────

def _fake_request(headers: dict[str, str], client_host: str = "203.0.113.9"):
    """A Request stand-in: ASGI adapters normalize headers to lowercase and
    ignore forged X-Forwarded-* for request.client — the production property
    under test is that get_client_identifier never prefers header values."""
    class _Client:
        host = client_host
    return SimpleNamespace(client=_Client(), headers={
        k.lower(): v for k, v in headers.items()})


def test_forged_forwarding_headers_cannot_change_identity():
    """§3.21 'Forged forwarding headers': the identity comes from the ASGI
    peer, never from caller-controlled X-Forwarded-For / X-Real-IP."""
    from backend.security.rate_limiter import get_client_identifier
    forged = _fake_request({
        "X-Forwarded-For": "1.2.3.4",
        "X-Real-IP": "5.6.7.8",
        "Forwarded": "for=9.9.9.9",
    }, client_host="203.0.113.9")
    identity = get_client_identifier(forged)
    assert identity == "ip:203.0.113.9"
    assert "1.2.3.4" not in identity and "9.9.9.9" not in identity
    # An authenticated user id always wins over any client identity.
    assert get_client_identifier(forged, user_id="u-77") == "user:u-77"


def test_client_identifier_without_peer_is_explicit():
    from backend.security.rate_limiter import get_client_identifier
    assert get_client_identifier(SimpleNamespace(client=None)) == "ip:unknown"


@pytest.mark.asyncio
async def test_multiple_api_keys_share_the_owner_allowance():
    """§3.21 'Several API keys owned by one user': API-key authentication
    resolves to the OWNER's user_id (dependencies/auth.py), so every key of
    one owner draws from the SAME user:{owner} allowance — creating more
    keys cannot bypass it."""
    from backend.api.dependencies.auth import _authenticate_api_key

    class _KeysRepo:
        async def resolve_key(self, key_hash):
            # Two DIFFERENT keys (distinct hashes), one owner.
            return {"user_id": "owner-1"} if key_hash.endswith("a") else {"user_id": "owner-1"}

    class _UserRepo:
        async def get_by_id(self, user_id):
            return {"id": "owner-1", "email": "o@x.io", "name": "O",
                    "role": "user", "is_active": True}

    class _Factory:
        api_keys_repo = _KeysRepo()
        user_repo = _UserRepo()

    class _App:
        state = SimpleNamespace(factory=_Factory())

    request = SimpleNamespace(app=_App())
    user_a = await _authenticate_api_key(request, "vry_key_a")
    user_b = await _authenticate_api_key(request, "vry_key_b")
    # Both keys produce the SAME limiter identity.
    assert f"user:{user_a.user_id}" == f"user:{user_b.user_id}" == "user:owner-1"
    assert user_a.auth_provider == user_b.auth_provider == "api_key"


@pytest.mark.asyncio
async def test_one_tenant_flooding_does_not_consume_others_rate_limit():
    """§3.21 'One tenant floods requests': per-identity counters mean user
    B's allowance is untouched by user A's exhaustion (shared store)."""
    from backend.security.rate_limiter import admission_decision
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.security.shared_rate_limiter import SQLiteSharedRateRepository, set_shared_rate_repo
    db = DatabaseManager(str(_tmpdir() / "flood.db"))
    await db.initialize()
    set_shared_rate_repo(SQLiteSharedRateRepository(db))
    try:
        policy = resolve_policy("workflow_create")
        for _ in range(policy.limit + 1):
            decision = await admission_decision("user:flooder", "workflow_create")
        assert decision.allowed is False           # flooder exhausted
        fresh = await admission_decision("user:innocent-bystander", "workflow_create")
        assert fresh.allowed is True               # bystander unaffected
        assert fresh.enforcement == EnforcementMode.SHARED
    finally:
        set_shared_rate_repo(None)
        await db.close()


def _tmpdir():
    import tempfile
    return Path(tempfile.mkdtemp(prefix="admission-flood-"))


# ── §3.20: audited admin policy disable/enable ────────────────────────

@pytest.mark.asyncio
async def test_admin_disable_stops_admission_and_is_audited():
    from backend.security.admission_policy import _POLICIES, resolve_policy
    original = resolve_policy("integration_build")
    try:
        from backend.security.admission_policy import register_policy
        from backend.models.admission import AdmissionPolicy
        disabled = AdmissionPolicy(
            group=original.group, limit=0, window_seconds=original.window_seconds,
            identity=original.identity, shared=original.shared,
            on_store_failure=original.on_store_failure,
            policy_version=original.policy_version + "+disabled")
        register_policy(disabled)
        assert resolve_policy("integration_build").disabled is True
        set_shared_rate_repo(None)
        from backend.security.rate_limiter import admission_decision
        decision = await admission_decision("user:any", "integration_build")
        assert decision.allowed is False
        assert decision.enforcement == EnforcementMode.DISABLED_BY_POLICY
    finally:
        register_policy(original)


def test_disabled_policy_is_explicit_not_magic():
    """§3.5: limit==0 is a REAL disabled policy with a distinct enforcement
    mode — never conflated with 'missing' or 'unlimited'."""
    from backend.models.admission import AdmissionPolicy
    assert AdmissionPolicy(group="x", limit=0).disabled is True
    assert AdmissionPolicy(group="x", limit=5).disabled is False
