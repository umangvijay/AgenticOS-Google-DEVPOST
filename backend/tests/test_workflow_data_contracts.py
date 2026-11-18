"""Execution contracts for real workflow data; no provider credentials required."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from backend.engine.engine import WorkflowEngine
from backend.engine.dag_validator import prepare_dag, DAGValidationError
from backend.models.schemas import WorkflowDefinition


def run_with(output, status='COMPLETED'):
    return SimpleNamespace(goal='Summarize health', tasks=[SimpleNamespace(
        task_id='probe', status=status, output_data=output)])


@pytest.mark.parametrize('value', [0, False, None, [], {}, {'status_code': 200}])
def test_whole_reference_preserves_type(value):
    assert WorkflowEngine(None, None)._interpolate(
        '{{ tasks.probe.output.value }}', run_with({'value': value})) == value


@pytest.mark.parametrize('expression', ['{{ tasks.probe.output.missing }}',
    '{{ tasks.unknown.output }}', '{{ env.SECRET }}'])
def test_unresolvable_reference_fails(expression):
    with pytest.raises(ValueError):
        WorkflowEngine(None, None)._interpolate(expression, run_with({}))


def test_reference_requires_completed_source():
    with pytest.raises(ValueError, match='unavailable'):
        WorkflowEngine(None, None)._interpolate('{{ tasks.probe.output }}', run_with({}, 'FAILED'))


def test_nested_list_reference():
    assert WorkflowEngine(None, None)._interpolate('{{ tasks.probe.output.items.0.id }}',
        run_with({'items': [{'id': 12}]})) == 12


@pytest.mark.parametrize('tasks', [[],
    [{'task_id': 'a', 'agent': 'core.health', 'input_data': {}}],
    [{'task_id': 'a', 'agent': 'core.chat', 'dependencies': ['missing']}],
    [{'task_id': 'a', 'agent': 'core.chat'}, {'task_id': 'a', 'agent': 'core.chat'}],
    [{'task_id': 'a', 'agent': 'core.unknown'}],
    [{'task_id': 'a', 'agent': 'core.chat'}, {'task_id': 'b', 'agent': 'core.chat',
      'input_data': {'prompt': '{{ tasks.a.output }}'}}],
])
def test_invalid_plans_rejected_before_execution(tasks):
    with pytest.raises(DAGValidationError):
        prepare_dag(WorkflowDefinition(tasks=tasks))


@pytest.mark.asyncio
async def test_summary_receives_observed_dependency_values_even_with_empty_input(monkeypatch):
    from backend.services import gemini_client
    generate = AsyncMock(return_value='Observed HTTP 503')
    monkeypatch.setattr(gemini_client, 'generate_text', generate)
    task = SimpleNamespace(agent='core.chat', dependencies=['probe'])
    result = await WorkflowEngine(None, None)._execute_core_node(task, {}, run_with({'status_code': 503}))
    assert '"status_code": 503' in generate.call_args.args[0]
    assert 'untrusted' in generate.call_args.args[0]
    assert result['reply'] == 'Observed HTTP 503'


def test_explicit_vertex_ignores_stored_api_key(monkeypatch):
    from backend.config.settings import settings
    from backend.services import gemini_client, llm_context
    from backend.services.embedding_service import GoogleCloudEmbeddingService
    from google import genai
    calls = []
    monkeypatch.setattr(settings, 'LLM_PROVIDER', 'vertex')
    monkeypatch.setattr(settings, 'GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setattr(llm_context, 'effective_gemini_key', lambda: 'unused-test-key')
    monkeypatch.setattr(gemini_client, 'effective_gemini_key', lambda: 'unused-test-key')
    monkeypatch.setattr(genai, 'Client', lambda **kw: calls.append(kw))
    gemini_client._client()
    GoogleCloudEmbeddingService()._initialize()
    assert len(calls) == 2
    assert all(c.get('vertexai') and 'api_key' not in c for c in calls)

@pytest.mark.asyncio
async def test_http_write_requires_approval_before_network(monkeypatch):
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    from backend.services.approvals_engine import ApprovalRequiredException
    monkeypatch.setattr(OpenAPIParser, '_validate_ssrf', lambda *a: None)
    task = SimpleNamespace(agent='core.http', task_id='write')
    run = SimpleNamespace(user_id='alice', run_id='run', workflow_id='wf')
    with pytest.raises(ApprovalRequiredException) as error:
        await WorkflowEngine(None, None)._execute_core_node(task, {
            'url': 'https://example.com/items', 'method': 'POST',
            'headers': {'Authorization': 'Bearer do-not-persist'}, 'body': {'name': 'item'},
        }, run)
    approval = error.value.pending_approval
    assert approval.arguments['method'] == 'POST'
    assert 'do-not-persist' not in str(approval.arguments)
    assert approval.user_id == 'alice'

@pytest.mark.asyncio
async def test_invalid_plan_repairs_once_and_revalidates():
    from backend.engine.plan_repair import validate_or_repair
    invalid = WorkflowDefinition(tasks=[{'task_id': 'probe', 'agent': 'core.health'}])
    repair = AsyncMock(return_value={'tasks': [{'task_id': 'probe', 'agent': 'core.health',
        'input_data': {'url': 'https://example.com'}}]})
    result = await validate_or_repair(invalid, goal='Check health', intent={}, catalog=[], repair=repair)
    assert result.tasks[0].input_data['url'] == 'https://example.com'
    repair.assert_awaited_once()
    repair.reset_mock()
    repair.return_value = {'tasks': []}
    with pytest.raises(DAGValidationError):
        await validate_or_repair(invalid, goal='Check health', intent={}, catalog=[], repair=repair)
    repair.assert_awaited_once()

@pytest.mark.asyncio
async def test_persisted_workflow_passes_dependency_results_and_survives_reopen(tmp_path, monkeypatch):
    import asyncio
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
    from backend.models.schemas import Task, WorkflowRun
    from backend.services import gemini_client
    db_path = str(tmp_path / 'workflow.db')
    db = DatabaseManager(db_path)
    await db.initialize()
    repo = SQLiteWorkflowRepository(db)
    bus = SimpleNamespace(publish=AsyncMock())
    engine = WorkflowEngine(repo, bus)
    generate = AsyncMock(return_value='The observed status was 503.')
    monkeypatch.setattr(gemini_client, 'generate_text', generate)
    tasks = [Task(task_id='probe', run_id='run', workflow_id='wf', user_id='alice',
                  agent='core.set', input_data={'fields': {'status_code': 503}}),
             Task(task_id='summary', run_id='run', workflow_id='wf', user_id='alice',
                  agent='core.chat', dependencies=['probe'])]
    await repo.save_run(WorkflowRun(run_id='run', workflow_id='wf', user_id='alice',
        goal='Summarize the observed result', status='RUNNING', tasks=tasks).model_dump(mode='json'))
    try:
        await engine.execute_task('run', 'probe')
        await engine.execute_task('run', 'summary')
        await engine.execute_task('run', 'summary')
        assert generate.await_count == 1
        assert '"status_code": 503' in generate.call_args.args[0]
        await asyncio.sleep(0.02)
    finally:
        await db.close()
    reopened = DatabaseManager(db_path)
    await reopened.initialize()
    try:
        saved = await SQLiteWorkflowRepository(reopened).get_run('run')
        assert saved['status'] == 'COMPLETED'
        assert saved['tasks'][1]['output_data']['reply'] == 'The observed status was 503.'
    finally:
        await reopened.close()


def test_health_and_summary_plan_includes_all_sources():
    from backend.engine.direct_plan import plan_from_goal
    plan = plan_from_goal('Check health of https://example.com and https://example.org then summarize in one sentence')
    prepare_dag(plan)
    assert len(plan.tasks) == 3
    assert plan.tasks[-1].agent == 'core.chat'
    assert plan.tasks[-1].dependencies == ['health-1', 'health-2']


@pytest.mark.asyncio
async def test_premature_queue_delivery_cannot_execute_dependent_task(monkeypatch):
    import backend.engine.engine as module
    task = SimpleNamespace(task_id='summary', dependencies=['probe'])
    task.status = 'PENDING'
    run = SimpleNamespace(status='RUNNING', tasks=[task, SimpleNamespace(task_id='probe', status='PENDING')])
    monkeypatch.setattr(module, 'load_run', AsyncMock(return_value=run))
    repo = SimpleNamespace(claim_task=AsyncMock())
    engine = WorkflowEngine(repo, None)
    engine.evaluate_dag = AsyncMock()
    await engine.execute_task('run', 'summary')
    repo.claim_task.assert_not_awaited()
    engine.evaluate_dag.assert_awaited_once()


def test_workflow_execution_limits(monkeypatch):
    from backend.config.settings import settings
    monkeypatch.setattr(settings, 'MAX_WORKFLOW_TASKS', 1)
    with pytest.raises(DAGValidationError, match='task limit'):
        prepare_dag(WorkflowDefinition(tasks=[{'task_id': 'a', 'agent': 'core.chat'},
                                             {'task_id': 'b', 'agent': 'core.chat'}]))
    with pytest.raises(DAGValidationError, match='execution limits'):
        prepare_dag(WorkflowDefinition(tasks=[{'task_id': 'a', 'agent': 'core.chat', 'max_retries': 999}]))

@pytest.mark.asyncio
async def test_workflow_api_accepts_adk_content_events_without_output_attribute(tmp_path, monkeypatch, budget_repo):
    import json
    from backend.api.routers.workflows import create_workflow, GoalRequest
    from backend.services import gemini_client
    from backend.api.routers import workflows
    def event(value):
        return SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text=json.dumps(value))]))
    monkeypatch.setattr(workflows, 'check_rate_limit', lambda *a: None)
    model = AsyncMock(side_effect=[
        [event({'action': 'classify', 'target': 'observations', 'context': 'use categories'})],
        [event({'tasks': [{'task_id': 'classify', 'agent': 'core.chat',
                          'input_data': {'prompt': 'Classify the observations'}}]})],
    ])
    monkeypatch.setattr(gemini_client, 'run_adk_debug', model)
    repo = SimpleNamespace(save_run=AsyncMock(), save_event=AsyncMock())
    # C5: workflow creation runs through the command-acceptance contract —
    # the test installs a real command repo and checks the receipt.
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.command_repository import SQLiteCommandRepository
    from backend.services.command_service import set_command_repo
    cmddb = DatabaseManager(str(tmp_path / "cmd.db"))
    await cmddb.initialize()
    set_command_repo(SQLiteCommandRepository(cmddb))
    factory = SimpleNamespace(secrets_repo=None, settings_repo=SimpleNamespace(get_settings=AsyncMock(return_value={})),
                              workflow_repo=repo, audit_repo=SimpleNamespace(log_event=AsyncMock()))
    engine = SimpleNamespace(evaluate_dag=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=factory, workflow_engine=engine)), headers={})
    user = SimpleNamespace(user_id='test-user', is_admin=lambda: False)
    result = await create_workflow(GoalRequest(goal='Classify observations into categories'), request, user)
    assert result['task_count'] == 1
    assert result['command_id']
    assert repo.save_run.call_args.args[0]['tasks'][0]['input_data']['prompt'] == 'Classify the observations'
    engine.evaluate_dag.assert_awaited_once()
    set_command_repo(None)
    await cmddb.close()
