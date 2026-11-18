"""Cloud transaction intent tests. These do not replace emulator contention tests."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_firestore_uses_configured_database(monkeypatch):
    from backend.repositories.firestore.database import FirestoreDB
    from backend.repositories.firestore import database
    from backend.config.settings import settings
    monkeypatch.setattr(FirestoreDB, "_instance", None)
    monkeypatch.setattr(settings, "GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(settings, "FIRESTORE_DATABASE_ID", "test-database")
    client = Mock()
    monkeypatch.setattr(database.firestore, "AsyncClient", client)
    await FirestoreDB.get_client()
    client.assert_called_once_with(project="test-project", database="test-database")


@pytest.mark.asyncio
async def test_firestore_rejects_stale_execution_write(monkeypatch):
    from backend.repositories.firestore import workflow_repository as module
    from backend.models.exceptions import StaleTaskLeaseError
    transaction = SimpleNamespace(update=Mock())
    ref = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(exists=True, to_dict=lambda: {"tasks": [{"task_id": "t", "attempt": 2}]})))
    db = SimpleNamespace(collection=lambda _: SimpleNamespace(document=lambda _: ref), transaction=lambda: transaction)
    repo = module.FirestoreWorkflowRepository()
    monkeypatch.setattr(module.firestore, "async_transactional", lambda f: f)
    monkeypatch.setattr(repo, "_get_db", AsyncMock(return_value=db))
    with pytest.raises(StaleTaskLeaseError):
        await repo.update_task("run", "t", {"attempt": 1, "status": "COMPLETED"})
    transaction.update.assert_not_called()


@pytest.mark.asyncio
async def test_generated_code_tester_never_executes_on_host(tmp_path):
    from backend.agents.mcp_factory.sandbox_tester import SandboxTester
    marker = tmp_path / "executed"
    result = await SandboxTester(str(tmp_path)).run_test("untrusted", f"open({str(marker)!r}, 'w').write('unsafe')")
    assert result["status"] == "unsupported"
    assert not marker.exists()


def test_sandbox_log_excludes_command_and_environment_values(caplog):
    from backend.mcp.sandbox.audit_logger import audit_logger
    import logging
    with caplog.at_level(logging.INFO, logger="agentos.security.audit"):
        audit_logger.log_sandbox_execution("mcp", "call", "docker -e API_KEY=seeded-secret-token")
        audit_logger.log_sandbox_violation("mcp", "ERROR", "seeded-secret-token")
    assert "SANDBOX_SPAWN" in caplog.text
    assert "seeded-secret-token" not in caplog.text
