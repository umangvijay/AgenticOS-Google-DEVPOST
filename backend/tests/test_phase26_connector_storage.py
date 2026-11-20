"""Storage parity only; fixture receipts are not live protocol attestations."""
import pytest

from backend.tests.test_phase26_controller_contract import store  # noqa: F401


def connector_repo(store):
    name = type(store).__name__
    if name.startswith("Postgres"):
        from backend.repositories.postgres.repositories import PostgresMCPRepository
        repo = PostgresMCPRepository(store.db)
    elif name.startswith("Firestore"):
        from backend.repositories.firestore.mcp_repository import FirestoreMCPRepository
        repo = FirestoreMCPRepository()
    else:
        from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
        repo = SQLiteMCPRepository(store.db)
    return repo


@pytest.mark.asyncio
async def test_revision_validation_and_activation_contract_on_each_store(store):
    repo = connector_repo(store)
    from backend.services.connector_revisions import checkpoint
    from backend.services.connector_activation import activate_revision, ActivationConflictError
    from backend.services.connector_validation_service import start_validation_run, complete_validation_run
    from backend.services.tool_validation import definition_digest
    manifest = {"mcp_id": "connector", "name": "Fixture", "owner": "owner", "transport": "internal",
                "spec_hash": "fixture-hash", "auth": {"type": "NONE"}, "is_enabled": False,
                "metadata": {"immutable_bundle": {"file": "fixture-only"}}}
    tool = {"tool_name": "read", "mcp_id": "connector", "mcp_version": "1.0",
            "input_schema": {"type": "object"}, "risk_level": 1,
            "operation": {"http_method": "GET", "path": "/read", "servers": ["https://example.com"]}}
    await repo.register_mcp(manifest)
    await repo.cache_tools("connector", [tool])
    actual = await repo.get_mcp("connector")
    assert actual["is_enabled"] is False
    assert actual["auth"]["type"] == "NONE"
    assert actual["spec_hash"] == "fixture-hash"
    assert actual["metadata"] == manifest["metadata"]
    with pytest.raises(PermissionError):
        await repo.register_mcp({**manifest, "owner": "stranger"})
    assert (await repo.get_mcp("connector"))["owner"] == "owner"
    tools = await repo.get_cached_tools("connector")
    assert tools[0]["operation"] == tool["operation"]
    revision = await checkpoint(repo, "connector")
    assert (await repo.get_revision("connector", revision["revision_id"]))["revision_id"] == revision["revision_id"]
    assert len(await repo.list_revisions("connector")) == 1
    run = await start_validation_run(repo, connector_id="connector",
        mcp={**actual, "active_revision_id": revision["revision_id"]}, tool=tools[0],
        credential_binding_id="fixture-only", environment="contract-test")
    await complete_validation_run(repo, run=run, passed=False,
        case_results=[{"case": "storage_fixture", "status": "failed"}],
        current_definition_digest=definition_digest(tools[0], "fixture-hash"))
    assert (await repo.list_validation_runs("connector"))[0]["status"] == "failed"
    denied = await activate_revision(repo, mcp_id="connector", candidate_revision_id=revision["revision_id"],
        expected_current_revision_id="", eligibility_allowed=False)
    assert denied["activated"] is False
    assert not (await repo.get_mcp("connector")).get("active_revision_id")
    # Exercise CAS independently of eligibility; this isolated fixture is
    # deliberately disabled and can never execute a tool.
    await activate_revision(repo, mcp_id="connector", candidate_revision_id=revision["revision_id"],
        expected_current_revision_id="", eligibility_allowed=True, actor="contract-test")
    with pytest.raises(ActivationConflictError):
        await activate_revision(repo, mcp_id="connector", candidate_revision_id=revision["revision_id"],
            expected_current_revision_id="", eligibility_allowed=True)
    assert (await repo.get_mcp("connector"))["is_enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("intervention", ["none", "disable", "delete"])
async def test_managed_publication_cannot_override_user_control(store, intervention):
    repo = connector_repo(store)
    from backend.services.connector_revisions import checkpoint
    from backend.services.connector_activation import activate_revision
    await repo.register_mcp({"mcp_id": "candidate", "name": "Fixture", "owner": "owner",
        "transport": "internal", "is_enabled": False,
        "metadata": {"pending_publication_token": "one-use-token"}})
    await repo.cache_tools("candidate", [])
    revision = await checkpoint(repo, "candidate")
    await activate_revision(repo, mcp_id="candidate", candidate_revision_id=revision["revision_id"],
        expected_current_revision_id="", eligibility_allowed=True, actor="contract-fixture")
    if intervention == "disable":
        await repo.set_mcp_enabled("candidate", False)
    elif intervention == "delete":
        await repo.delete_mcp("candidate")
    options = dict(owner="owner", revision_id=revision["revision_id"],
                   publication_token="one-use-token", metadata={"receipt": "storage-fixture-only"})
    assert not await repo.publish_managed_candidate("candidate", **{**options, "owner": "stranger"})
    assert await repo.publish_managed_candidate("candidate", **options) is (intervention == "none")
    assert not await repo.publish_managed_candidate("candidate", **options)
    current = await repo.get_mcp("candidate")
    if intervention == "delete":
        assert current is None
    else:
        assert current["is_enabled"] is (intervention == "none")


@pytest.mark.asyncio
async def test_existing_website_can_be_retested_without_enabling_it(store, monkeypatch):
    from unittest.mock import AsyncMock
    from backend.services import browser_connector_validation as validation
    repo = connector_repo(store)
    site = "https://music.youtube.com/"
    await repo.register_mcp({"mcp_id": "website", "name": "YouTube Music", "owner": "alice",
                             "transport": "internal", "source_type": "website", "source_uri": site,
                             "spec_hash": "website-fixture", "auth": {"type": "NONE"}, "is_enabled": False})
    await repo.cache_tools("website", [{"mcp_id": "website", "tool_name": "runOnSite",
                                        "mcp_version": "v1", "risk_level": 3, "input_schema": {},
                                        "operation": {"kind": "browser", "origin": site, "start_url": site}}])
    monkeypatch.setattr(validation, "probe_start_page", AsyncMock(return_value={"http_status": 200, "title": "Music", "url": site}))
    manifest = await repo.get_mcp("website")
    await validation.validate_browser_connector(repo, manifest, await repo.get_cached_tools("website"), "alice")
    fresh = await repo.get_mcp("website")
    assert not fresh["is_enabled"]
    assert fresh["trust_tier"] == "browser_ready" and fresh["active_revision_id"]
    cached = {t["tool_name"]: t for t in await repo.get_cached_tools("website")}
    assert set(cached) == {"readStartPage", "runOnSite"}
    assert validation.has_valid_browser_probe(cached["readStartPage"], fresh)
    assert not validation.has_valid_browser_probe(cached["runOnSite"], fresh)
