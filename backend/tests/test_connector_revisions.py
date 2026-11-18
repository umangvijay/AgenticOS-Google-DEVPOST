import pytest
import pytest_asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import HTTPException
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
from backend.services.connector_revisions import checkpoint, restore_copy


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / 'revisions.db'))
    await db.initialize()
    repository = SQLiteMCPRepository(db)
    await repository.register_mcp({'mcp_id': 'source', 'name': 'Example', 'owner': 'alice',
        'transport': 'internal', 'auth': {'api_key': 'never-copy-this'}, 'is_enabled': True})
    await repository.cache_tools('source', [{'tool_name': 'read', 'operation': {'http_method': 'GET'}}])
    yield repository
    await db.close()


@pytest.mark.asyncio
async def test_immutable_checkpoint_restore_is_disabled_and_credential_free(repo):
    first = await checkpoint(repo, 'source')
    second = await checkpoint(repo, 'source')
    assert first['revision_id'] == second['revision_id']
    assert len(await repo.list_revisions('source')) == 1
    assert 'never-copy-this' not in str(first)
    await repo.cache_tools('source', [{'tool_name': 'new'}])
    await checkpoint(repo, 'source')
    assert len(await repo.list_revisions('source')) == 2
    copy_id = await restore_copy(repo, 'source', first['revision_id'], 'bob')
    copy = await repo.get_mcp(copy_id)
    assert copy['owner'] == 'bob'
    assert copy['is_enabled'] is False
    assert copy['auth'] == {'type': 'NONE'}
    tools = await repo.get_cached_tools(copy_id)
    assert tools[0]['tool_name'] == 'read'
    assert tools[0]['mcp_version'] == first['revision_id']
    assert (await repo.get_mcp('source'))['is_enabled'] is True


@pytest.mark.asyncio
async def test_cross_user_history_and_restore_are_denied(repo):
    from backend.api.routers.integrations import list_connector_revisions, restore_connector_revision, checkpoint_connector
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=SimpleNamespace(mcp_repo=repo))))
    user = SimpleNamespace(user_id='bob', is_admin=lambda: False)
    for operation, args in [(list_connector_revisions, ('source',)),
                            (checkpoint_connector, ('source',)),
                            (restore_connector_revision, ('source', 'unknown'))]:
        with pytest.raises(HTTPException) as exc:
            await operation(*args, request, user)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_failed_restore_removes_partial_copy(repo, monkeypatch):
    revision = await checkpoint(repo, 'source')
    monkeypatch.setattr(repo, 'cache_tools', AsyncMock(side_effect=RuntimeError('storage failure')))
    with pytest.raises(RuntimeError):
        await restore_copy(repo, 'source', revision['revision_id'], 'alice')
    assert len(await repo.list_mcps()) == 1
