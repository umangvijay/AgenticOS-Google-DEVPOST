"""Cloud catalog repository contract, with no external database access."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from google.api_core.exceptions import NotFound, ServiceUnavailable
from backend.repositories.firestore.mcp_repository import FirestoreMCPRepository


def repository(rows=()):
    async def stream():
        for row in rows:
            yield SimpleNamespace(to_dict=lambda row=row: row)
    collection = Mock()
    collection.where.return_value.stream = stream
    ref = SimpleNamespace(update=AsyncMock())
    collection.document.return_value = ref
    repo = FirestoreMCPRepository()
    repo._get_db = AsyncMock(return_value=SimpleNamespace(collection=Mock(return_value=collection)))
    return repo, collection, ref


@pytest.mark.asyncio
async def test_only_public_entries_are_listed_newest_first_without_composite_index():
    repo, collection, _ = repository([
        {"mcp_id": "old", "is_public": True, "updated_at": "2026-01-01"},
        {"mcp_id": "private", "is_public": False},
        {"mcp_id": "missing-visibility"},
        {"mcp_id": "new", "is_public": True, "updated_at": "2026-11-19"},
    ])
    assert [row["mcp_id"] for row in await repo.list_public_mcps()] == ["new", "old"]
    collection.where.assert_called_once_with("is_public", "==", True)
    collection.where.return_value.order_by.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_database_failure_is_not_disguised_as_empty():
    repo, collection, _ = repository()
    collection.where.side_effect = ServiceUnavailable("offline")
    with pytest.raises(ServiceUnavailable):
        await repo.list_public_mcps()


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [True, False])
async def test_publish_and_unpublish_update_existing_document_only(public):
    repo, collection, ref = repository()
    assert await repo.set_public("connector", public) is True
    collection.document.assert_called_once_with("connector")
    values = ref.update.await_args.args[0]
    assert values["is_public"] is public
    assert values["updated_at"]
    ref.update.side_effect = NotFound("missing")
    assert await repo.set_public("missing", public) is False
    ref.update.side_effect = ServiceUnavailable("offline")
    with pytest.raises(ServiceUnavailable):
        await repo.set_public("connector", public)
