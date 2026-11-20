"""Real local persistence and dispatch; only provider/browser boundaries are faked."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from backend.config.settings import settings
from backend.services import browser_connector_validation as browser_validation
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
from backend.agents.mcp_factory.mcp_factory_agent import MCPFactoryAgent
from backend.mcp.tool_router import ToolRouter, ToolRouterError
from backend.mcp.tool_policy import ToolPolicy
from backend.services.approvals_engine import ApprovalsEngine, ApprovalRequiredException
from backend.models.security import AutonomyLevel
from backend.services.connector_eligibility import evaluate_capability_readiness, evaluate_operation_eligibility


@pytest_asyncio.fixture
async def website(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / 'journey.db'))
    await db.initialize()
    repo = SQLiteMCPRepository(db)
    monkeypatch.setattr('backend.agents.mcp_factory.mcp_factory_agent.plan_website_connector', AsyncMock(return_value={
        'name': 'YouTube Music', 'tools': [{'name': 'runOnSite', 'start_url': 'https://music.youtube.com/', 'goal_template': 'Use this website'}]}))
    monkeypatch.setattr(browser_validation, 'probe_start_page', AsyncMock(return_value={'url': 'https://music.youtube.com/', 'http_status': 200}))
    try:
        yield repo
    finally:
        await db.close()


async def build(repo):
    result = await MCPFactoryAgent(repo).run_build(user_id='alice', method='website', source='Create MCP tools for https://music.youtube.com/')
    assert result['status'] == 'success', result
    return result


@pytest.mark.asyncio
async def test_youtube_build_is_persisted_discoverable_and_still_requires_action_approval(website, monkeypatch):
    result = await build(website)
    router = ToolRouter(website, ToolPolicy(), ApprovalsEngine())
    lookup = AsyncMock(wraps=website.get_mcp)
    monkeypatch.setattr(website, 'get_mcp', lookup)
    catalog = await router.get_tool_catalog('alice')
    lookup.assert_awaited_once_with(result['mcp_id'])
    assert [t['name'] for t in catalog] == ['readStartPage']
    assert await router.get_tool_catalog('bob') == []
    mcp = await website.get_mcp(result['mcp_id'])
    tools = await website.get_cached_tools(result['mcp_id'])
    assert mcp['active_revision_id']
    assert evaluate_operation_eligibility(next(t for t in tools if t['tool_name'] == 'readStartPage'), mcp).allowed
    assert not evaluate_operation_eligibility(next(t for t in tools if t['tool_name'] == 'runOnSite'), mcp).allowed
    assert evaluate_capability_readiness(mcp, {'status': 'success', 'required_operations': ['readStartPage']}, await website.list_validation_runs(mcp['mcp_id']))['ready']
    browser_validation.probe_start_page.reset_mock()
    context = {'user_id': 'alice', 'run_id': 'run', 'workflow_id': 'wf', 'task_id': 'task', 'autonomy_level': AutonomyLevel.L2_SEMI_AUTONOMOUS}
    with pytest.raises(ApprovalRequiredException):
        await router.execute_tool_safe(catalog[0]['agent_tool_name'], {'goal': 'Read home'}, context)
    browser_validation.probe_start_page.assert_not_awaited()
    autonomous = {**context, "autonomy_level": AutonomyLevel.L3_AUTONOMOUS}
    read = await router.execute_tool_safe(catalog[0]["agent_tool_name"], {}, autonomous)
    assert read["http_status"] == 200
    browser_validation.probe_start_page.assert_awaited_once_with("https://music.youtube.com/", "alice")
    with pytest.raises(ValueError, match="takes no arguments"):
        await router.execute_tool_safe(catalog[0]["agent_tool_name"], {"goal": "change page"}, autonomous)
    with pytest.raises(ToolRouterError, match='accessible'):
        await router.execute_tool_safe(catalog[0]['agent_tool_name'], {}, {**context, 'user_id': 'bob'})


@pytest.mark.asyncio
async def test_failed_browser_probe_never_creates_callable_tools(website, monkeypatch):
    monkeypatch.setattr(browser_validation, 'probe_start_page', AsyncMock(side_effect=RuntimeError('Chromium unavailable')))
    result = await MCPFactoryAgent(website).run_build(user_id='alice', method='website', source='https://music.youtube.com/')
    assert result['status'] == 'error'
    assert await ToolRouter(website, ToolPolicy()).get_tool_catalog('alice') == []


@pytest.mark.asyncio
async def test_browser_evidence_invalidates_on_definition_credentials_risk_and_expiry(website):
    result = await build(website)
    mcp = await website.get_mcp(result['mcp_id'])
    tool = next(t for t in await website.get_cached_tools(mcp['mcp_id']) if t['tool_name'] == 'readStartPage')
    assert browser_validation.has_valid_browser_probe(tool, mcp)
    for changed in [
        {**tool, 'risk_level': 1},
        {**tool, 'operation': {**tool['operation'], 'start_url': 'https://attacker.example/'}},
        {**tool, 'operation': {**tool['operation'], '_validation': {**tool['operation']['_validation'], 'valid_until': 0}}},
    ]:
        assert not browser_validation.has_valid_browser_probe(changed, mcp)
    assert not browser_validation.has_valid_browser_probe(tool, {**mcp, 'auth': {'type': 'API_KEY', 'credential_ref': 'new'}})


@pytest.mark.asyncio
async def test_changed_connector_during_probe_is_not_reactivated(website, monkeypatch):
    result = await build(website)
    mcp = await website.get_mcp(result['mcp_id'])
    tools = await website.get_cached_tools(mcp['mcp_id'])
    async def disable(*args):
        await website.set_mcp_enabled(mcp['mcp_id'], False)
        return {'http_status': 200}
    monkeypatch.setattr(browser_validation, 'probe_start_page', disable)
    with pytest.raises(ValueError, match='changed'):
        await browser_validation.validate_browser_connector(website, mcp, tools, 'alice')
    assert not (await website.get_mcp(mcp['mcp_id']))['is_enabled']


@pytest.mark.parametrize('goal,host', [('Create yt mcp tools', 'www.youtube.com'), ('Create YouTube Music MCP tools then use them', 'music.youtube.com')])
def test_youtube_names_route_to_browser(goal, host):
    from backend.engine.direct_plan import plan_from_goal
    task = plan_from_goal(goal).tasks[0]
    assert task.agent == 'core.mcp_build'
    assert task.input_data['method'] == 'website'
    assert host in task.input_data['source']


@pytest.mark.asyncio
async def test_studio_records_success_and_early_storage_failure(tmp_path, monkeypatch):
    from backend.api.routers import capabilities as api
    monkeypatch.setattr(settings, 'STORAGE_BACKEND', 'sqlite')
    monkeypatch.setattr(settings, 'ARTIFACTS_ROOT', str(tmp_path))
    job_id = str(uuid.uuid4())
    api._put_job({'job_id': job_id, 'user_id': 'alice', 'status': 'queued'})
    monkeypatch.setattr('backend.services.artifact_builder.generate_project', AsyncMock(return_value={'files': ['index.html']}))
    await api._run_generate_job(job_id, 'alice', 'Clothing site', 'website', '', 'compact')
    assert api._get_job(job_id)['status'] == 'completed'
    monkeypatch.setattr(api, '_get_job', Mock(side_effect=RuntimeError('storage read failed')))
    await api._run_generate_job(job_id, 'alice', 'Clothing site', 'website', '', 'compact')
    assert api._JOBS[job_id]['status'] == 'failed'


@pytest.mark.asyncio
async def test_studio_unavailable_saved_model_is_actionable(monkeypatch):
    from backend.api.routers import capabilities as api
    monkeypatch.setattr('backend.services.llm_context.load_user_llm_keys', AsyncMock())
    monkeypatch.setattr('backend.services.llm_context.load_user_model', AsyncMock(side_effect=ValueError('retired')))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=SimpleNamespace(secrets_repo=object(), settings_repo=object()))))
    with pytest.raises(HTTPException) as caught:
        await api._apply_user_key(request, 'alice')
    assert caught.value.status_code == 400
    assert 'Settings' in caught.value.detail


def test_studio_uses_the_configured_firestore_database(monkeypatch, tmp_path):
    from backend.api.routers import capabilities as api
    from backend.services import artifact_builder
    client = Mock()
    monkeypatch.setattr('google.cloud.firestore.Client', client)
    monkeypatch.setattr(settings, 'STORAGE_BACKEND', 'firestore')
    monkeypatch.setattr(settings, 'GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setattr(settings, 'FIRESTORE_DATABASE_ID', 'sandtable-test')
    monkeypatch.setattr(settings, 'ARTIFACTS_ROOT', str(tmp_path))
    artifact_builder._firestore_sync()
    api._put_job({'job_id': str(uuid.uuid4()), 'user_id': 'alice'})
    assert client.call_count == 2
    for call in client.call_args_list:
        assert call.kwargs == {'project': 'test-project', 'database': 'sandtable-test'}


@pytest.mark.asyncio
async def test_full_generation_repairs_a_missing_file_and_reports_progress(tmp_path, monkeypatch):
    from backend.services import artifact_builder as builder
    monkeypatch.setattr(settings, 'STORAGE_BACKEND', 'sqlite')
    monkeypatch.setattr(settings, 'ARTIFACTS_ROOT', str(tmp_path))
    responses = [
        {'name': 'shop', 'files': [{'path': 'index.html'}, {'path': 'styles.css'}, {'path': 'script.js'}]},
        {'files': [{'path': 'index.html', 'content': '<h1>Shop</h1>'}]},
        {'files': [{'path': 'styles.css', 'content': 'body {color: black}'}]},
        {'files': [{'path': 'script.js', 'content': 'console.log("shop")'}]},
    ]
    monkeypatch.setattr(builder.gemini_client, 'generate_json', AsyncMock(side_effect=responses))
    progress = AsyncMock()
    result = await builder.generate_project('alice', 'Clothing site', scale='full', on_progress=progress)
    assert set(result['files']) == {'index.html', 'styles.css', 'script.js'}
    assert any(call.args[0].get('completed_files') == 2 for call in progress.await_args_list)
    assert builder.read_artifact_content('alice', result['artifact_id'], 'styles.css') == 'body {color: black}'

@pytest.mark.asyncio
async def test_runner_progress_does_not_swallow_an_approval(monkeypatch, budget_repo):
    from backend.engine import engine as module
    from backend.models.schemas import WorkflowRun, Task
    from backend.services import gemini_client
    pending = ApprovalRequiredException('Approval required', None)
    async def stream(**kwargs):
        yield SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text='Starting work')]))
        raise pending
    runner = SimpleNamespace(session_service=SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id='session'))), run_async=stream)
    monkeypatch.setattr(module, 'InMemoryRunner', Mock(return_value=runner))
    monkeypatch.setattr(gemini_client, 'candidate_models', lambda: ['fixture'])
    ctor = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(module, 'get_orchestrator_agent', ctor)
    engine = module.WorkflowEngine(SimpleNamespace(save_event=AsyncMock()), SimpleNamespace(publish=AsyncMock()))
    task = Task(run_id='run', workflow_id='wf', task_id='work', agent='OrchestratorAgent')
    run = WorkflowRun(run_id='run', workflow_id='wf', user_id='alice', tasks=[task], goal='Use the connector')
    with pytest.raises(ApprovalRequiredException) as caught:
        await engine._execute_ai_agent(task, run, 'run', 'work')
    assert caught.value is pending
    assert ctor.call_args.kwargs['execution_context']['workflow_engine'] is engine


@pytest.mark.asyncio
async def test_old_studio_job_expires_and_reads_changes_from_another_worker(tmp_path, monkeypatch):
    import json
    import os
    from backend.api.routers import capabilities as api
    monkeypatch.setattr(settings, 'STORAGE_BACKEND', 'sqlite')
    monkeypatch.setattr(settings, 'ARTIFACTS_ROOT', str(tmp_path))
    job = {'job_id': str(uuid.uuid4()), 'user_id': 'alice', 'status': 'running', 'total_files': 41}
    api._put_job(job)
    saved = tmp_path / '_jobs' / (job['job_id'] + '.json')
    os.utime(saved, (1, 1))
    user = SimpleNamespace(user_id='alice')
    assert (await api.generate_status(job['job_id'], user))['status'] == 'failed'
    saved.write_text(json.dumps({**job, 'status': 'completed'}))
    assert (await api.generate_status(job['job_id'], user))['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_to", ["/home", "https://other.example/home"])
async def test_public_read_follows_only_same_origin_redirects(monkeypatch, redirect_to):
    import httpx
    from contextlib import asynccontextmanager
    from backend.services import website_health
    visited = []

    def respond(request):
        visited.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": redirect_to})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<title>YouTube Music</title>")

    @asynccontextmanager
    async def capacity(*args, **kwargs):
        yield

    real_client = httpx.AsyncClient
    monkeypatch.setattr(website_health.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(website_health, "assert_public_https_url", lambda url: "music.youtube.com")
    monkeypatch.setattr(website_health, "_tls_info", lambda *args: {"ok": True})
    monkeypatch.setattr("backend.services.capacity_service.capacity_slot", capacity)
    if redirect_to.startswith("/"):
        result = await browser_validation.probe_start_page("https://music.youtube.com/", "alice")
        assert result == {"url": "https://music.youtube.com/home", "http_status": 200, "title": "YouTube Music"}
    else:
        with pytest.raises(ValueError, match="outside"):
            await browser_validation.probe_start_page("https://music.youtube.com/", "alice")
        assert visited == ["https://music.youtube.com/"]


def test_chat_mention_selects_exact_agent_before_general_routing():
    from backend.engine.direct_plan import plan_from_goal
    task, = plan_from_goal("@yt-assistant Check the page title").tasks
    assert task.agent == "OrchestratorAgent" and task.agent_id == "yt-assistant"
    assert task.input_data == {"goal": "Check the page title"}
