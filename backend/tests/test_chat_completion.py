"""The completion notification must point at an already durable chat reply."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from backend.engine.engine import WorkflowEngine
from backend.models.schemas import WorkflowRun, Task
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository


@pytest.mark.asyncio
@pytest.mark.parametrize('task_status,run_status', [
    ('COMPLETED', 'COMPLETED'), ('FAILED', 'FAILED'), ('NEEDS_REVIEW', 'NEEDS_REVIEW'),
])
async def test_terminal_notification_can_read_persisted_status_and_reply(tmp_path, monkeypatch, task_status, run_status):
    db = DatabaseManager(str(tmp_path / 'chat.db'))
    await db.initialize()
    repo = SQLiteWorkflowRepository(db)
    task = Task(task_id='chat', run_id='run', workflow_id='wf', user_id='alice',
                agent='core.chat', status=task_status, output_data={'reply': 'Persisted reply'})
    await repo.save_run(WorkflowRun(run_id='run', workflow_id='wf', user_id='alice',
        goal='Hello', status='RUNNING', tasks=[task]).model_dump(mode='json'))
    engine = WorkflowEngine(repo, SimpleNamespace(publish=AsyncMock()))
    observed = []
    async def on_event(event_type, run_id, workflow_id, **kwargs):
        saved = await repo.get_run(run_id)
        observed.append(saved)
        assert saved['status'] == run_status
        assert saved['tasks'][0]['output_data']['reply'] == 'Persisted reply'
    monkeypatch.setattr(engine, '_emit_event_sync', on_event)
    monkeypatch.setattr(engine, '_sync_command_terminal', AsyncMock())
    monkeypatch.setattr(engine, '_on_run_finished', AsyncMock())
    try:
        await engine.evaluate_dag('run')
        assert len(observed) == 1
    finally:
        await db.close()
