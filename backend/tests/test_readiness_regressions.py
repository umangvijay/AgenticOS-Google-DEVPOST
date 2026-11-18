"""Regression scenarios from the readiness audit; no paid APIs or production data."""
import asyncio
import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.engine.direct_plan import plan_from_goal
from backend.engine.idempotency_guard import IdempotencyGuard
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.idempotency_repository import SQLiteIdempotencyRepository
from backend.repositories.in_memory_message_bus import InMemoryMessageBus
from backend.services.auth_challenges import ChallengePause, classify_auth_challenge
from backend.security.webhook_signature import verify_signature

@pytest.mark.parametrize('snapshot', [
    {'url': 'https://catfact.ninja/#/Facts/getRandomFact', 'text': 'Swagger UI API Documentation'},
    {'text': 'Our API supports MFA and OTP authentication. Cloudflare protects our site.'},
    {'text': 'GET /hotpot Returns data', 'url': 'https://example.org/mfa-docs'},
    {'text': 'Access denied: your API key does not have this permission'},
])
def test_public_pages_do_not_trigger_challenges(snapshot):
    assert classify_auth_challenge(snapshot) is None

@pytest.mark.parametrize('field,expected', [('totp', 'mfa'), ('otp', 'otp'), ('one-time-code', 'otp')])
def test_real_credential_fields_still_pause(field, expected):
    assert classify_auth_challenge({'elements': [{'tag': 'input', 'name': field}]}) == expected

def test_all_health_urls_fan_out():
    plan = plan_from_goal('Check health of https://example.com, https://news.ycombinator.com and https://api.github.com')
    assert [t.input_data['url'] for t in plan.tasks] == ['https://example.com', 'https://news.ycombinator.com', 'https://api.github.com']
    assert all(not t.dependencies for t in plan.tasks)

def test_mixed_goal_not_truncated():
    assert plan_from_goal('Check health of https://example.com then fetch Bitcoin and save it to memory') is None
    assert plan_from_goal('Research the latest AI news and create a PDF') is None

def test_multiple_get_urls_preserved():
    plan = plan_from_goal('Fetch https://example.com/a and https://example.com/b')
    assert len(plan.tasks) == 2

def test_build_and_use_has_dependency():
    plan = plan_from_goal('Create MCP for https://example.com/openapi.json then use it to list posts')
    assert plan.tasks[-1].agent == 'OrchestratorAgent'
    assert plan.tasks[-1].dependencies == ['mcp-1']

@pytest_asyncio.fixture
async def ledger(tmp_path):
    db = DatabaseManager(str(tmp_path/'test.db'))
    await db.initialize()
    yield SQLiteIdempotencyRepository(db)
    await db.close()

@pytest.mark.asyncio
async def test_pause_resume_and_completed_result_replay(ledger):
    guard = IdempotencyGuard(ledger)
    call = AsyncMock(side_effect=[ChallengePause('otp', 'https://example.com', 'paused'), {'live': 42}])
    with pytest.raises(ChallengePause):
        await guard.execute_once('wf', 'task', 'tool', {}, call)
    resumed = await guard.execute_once('wf', 'task', 'tool', {}, call)
    cached = await guard.execute_once('wf', 'task', 'tool', {}, call)
    assert resumed.result == {'live': 42}
    assert cached.from_cache and call.await_count == 2

@pytest.mark.asyncio
async def test_failure_requires_explicit_retry_and_success_is_preserved(ledger):
    guard = IdempotencyGuard(ledger)
    call = AsyncMock(side_effect=[RuntimeError('transient'), {'ok': True}])
    with pytest.raises(RuntimeError):
        # Only a call explicitly known to be safe to replay may be released by Retry.
        await guard.execute_once('wf', 'task', 'tool', {}, call, retry_safe=True)
    assert (await guard.execute_once('wf', 'task', 'tool', {}, call)).blocked
    await ledger.reset_failed_task('wf', 'task')
    assert (await guard.execute_once('wf', 'task', 'tool', {}, call)).executed
    await ledger.reset_failed_task('wf', 'task')
    assert (await guard.execute_once('wf', 'task', 'tool', {}, call)).from_cache

@pytest.mark.asyncio
async def test_concurrent_reclaim_only_one_owner(ledger):
    await ledger.atomic_insert('key', 'wf', 'task')
    await ledger.commit_pause('key')
    claims = await asyncio.gather(*[ledger.atomic_insert('key', 'wf', 'task') for _ in range(8)])
    assert sum(claims) == 1

@pytest.mark.asyncio
async def test_worker_independent_tasks_overlap(monkeypatch):
    from backend.config.settings import settings
    monkeypatch.setattr(settings, 'LOCAL_WORKER_CONCURRENCY', 2)
    bus = InMemoryMessageBus()
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = []
    async def handler(message, ctx):
        started.append(message)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        await ctx.ack()
        await ctx.ack()  # settlement is idempotent
    consumer = asyncio.create_task(bus.consume('tasks', handler))
    try:
        await bus.publish('tasks', {'task_id':'a'})
        await bus.publish('tasks', {'task_id':'b'})
        await asyncio.wait_for(both_started.wait(), 2)
        release.set()
        await asyncio.wait_for(bus._get_queue('tasks').join(), 2)
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

@pytest.mark.asyncio
async def test_in_memory_worker_bounds_retries_and_dead_letters_persistent_failure(monkeypatch):
    from backend.config.settings import settings
    monkeypatch.setattr(settings, 'LOCAL_WORKER_CONCURRENCY', 1)
    bus = InMemoryMessageBus(max_deliveries=3, retry_base_seconds=0)
    attempts = []

    async def failing_handler(message, ctx):
        attempts.append(message['task_id'])
        await ctx.nack()

    consumer = asyncio.create_task(bus.consume('tasks', failing_handler))
    try:
        await bus.publish('tasks', {'task_id': 'persistent-failure'})
        await asyncio.wait_for(bus._get_queue('tasks').join(), 2)
        dead_letter = await asyncio.wait_for(bus.dead_letters.get(), 1)
        assert attempts == ['persistent-failure'] * 3
        assert dead_letter['topic'] == 'tasks'
        assert dead_letter['message']['task_id'] == 'persistent-failure'
        assert dead_letter['delivery_attempt'] == 3
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

@pytest.mark.asyncio
async def test_failed_workflow_attempt_is_preserved_in_chat_history():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.api.routers.workflows import _persist_failed_attempt

    repository = SimpleNamespace(
        get_run=AsyncMock(return_value=None),
        save_run=AsyncMock(),
        save_event=AsyncMock(),
    )
    factory = SimpleNamespace(workflow_repo=repository)
    await _persist_failed_attempt(factory, 'run-1', 'wf-run-1', 'user-1', 'prepare a report', 'thread-1', None)
    repository.save_run.assert_awaited_once()
    saved = repository.save_run.await_args.args[0]
    assert saved['goal'] == 'prepare a report'
    assert saved['thread_id'] == 'thread-1'
    assert saved['status'] == 'FAILED'
    assert saved['tasks'][0]['status'] == 'FAILED'
    assert saved['tasks'][0]['error']
    assert repository.save_event.await_args.args[0]['type'] == 'WORKFLOW_FAILED'

def test_webhook_rejects_tamper_and_expiry():
    body = b'{"goal":"test"}'
    signature = 'sha256=' + hmac.new(b'secret', b'1000.'+body, hashlib.sha256).hexdigest()
    assert verify_signature('secret','1000',signature,body,now=1001)
    assert not verify_signature('secret','1000',signature,b'changed',now=1001)
    assert not verify_signature('secret','1000',signature,body,now=1301)
    assert not verify_signature('secret','bad',signature,body,now=1001)

@pytest.mark.asyncio
async def test_memory_outage_keeps_real_content_and_keyword_search(monkeypatch):
    from backend.api.routers.memory import add_memory, search_memory, AddMemoryRequest, SearchMemoryRequest
    from backend.services.embedding_service import GoogleCloudEmbeddingService
    monkeypatch.setattr(GoogleCloudEmbeddingService, 'embed_text', lambda *a: (_ for _ in ()).throw(RuntimeError('private provider detail')))
    repo = SimpleNamespace(store_memory=AsyncMock(return_value='m1'), list_memories=AsyncMock(return_value=[{'id':'m1','content':'Remember blue widgets'}]))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=SimpleNamespace(memory_repo=repo))))
    user = SimpleNamespace(user_id='owner')
    saved = await add_memory(AddMemoryRequest(content='Remember blue widgets'), request, user)
    # The governed write path (Phase 16) surfaces the policy decision
    # with the save — the outage degrades embedding, not governance.
    assert saved["memory_id"] == 'm1'
    assert saved["embedded"] is False
    assert saved["policy_decision"] == 'stored'
    assert saved["verification_status"] == 'confirmed'
    assert repo.store_memory.call_args.kwargs['content'] == 'Remember blue widgets'
    found = await search_memory(SearchMemoryRequest(query='widgets'), request, user)
    # The governed retrieval path (Phase 16) degrades to keyword-scoring
    # inside the SAME governed mode when embeddings are unavailable.
    assert found['retrieval_mode'] == 'governed'
    assert found['results'][0]['memory_id'] == 'm1'
    # The substring hit reports as 'exact' inside the governed mode —
    # the OUTAGE point is that real content still retrieves.
    assert found['results'][0]['ranking']['method'] in ('exact', 'keyword')
    # governed_search passes the owner positionally — ownership-first.
    owner_args = (repo.list_memories.call_args.args
                  or repo.list_memories.call_args.kwargs.values())
    assert 'owner' in tuple(owner_args)

@pytest.mark.asyncio
async def test_model_selection_is_isolated_and_not_silently_overridden():
    from backend.services.llm_context import load_user_model
    from backend.services.gemini_client import candidate_models
    repo = SimpleNamespace(get_settings=AsyncMock(return_value={'default_model':'gemini-3.1-pro-preview'}))
    try:
        await load_user_model(repo,'u')
        assert candidate_models() == ['gemini-3.1-pro-preview']
        with pytest.raises(ValueError):
            candidate_models('totally-fake-model')
        await load_user_model(None,'other')
        assert candidate_models()[0] != 'gemini-3.1-pro-preview'
    finally:
        await load_user_model(None,'')

def test_spec_alias_route_registered():
    from backend.api.routers.integrations import router
    assert any(r.path == '/integrations/build-from-spec' and 'POST' in r.methods for r in router.routes)

def test_recursive_spec_does_not_overflow():
    import json
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    parser = OpenAPIParser()
    parser._validate_ssrf = lambda url: None
    spec = {'openapi':'3.0.0','info':{'title':'Tree','version':'1'},'servers':[{'url':'https://example.com'}],
            'paths':{'/tree':{'get':{'operationId':'getTree','responses':{'200':{'description':'tree','content':{'application/json':{'schema':{'$ref':'#/components/schemas/Tree'}}}}}}}},
            'components':{'schemas':{'Tree':{'type':'object','properties':{'child':{'$ref':'#/components/schemas/Tree'}}}}}}
    assert len(parser.parse_text(json.dumps(spec)).operations) == 1

@pytest.mark.asyncio
async def test_portable_server_real_mcp_handshake(tmp_path):
    import io, zipfile, sys
    from backend.mcp.builder.portable_server import export_server
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    tool = {'tool_name':'listPosts','description':'List posts','input_schema':{'type':'object','properties':{}},
            'operation':{'http_method':'GET','path':'/posts','servers':[{'url':'https://jsonplaceholder.typicode.com'}]}}
    archive = export_server({'name':'Test API','auth':{'type':'NONE','credential_ref':'secret-ref'}},[tool])
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert b'secret-ref' not in bundle.read('tools.json')
        bundle.extractall(tmp_path)
    async with asyncio.timeout(15):
        async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(tmp_path/'server.py')])) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.list_tools()
                assert [t.name for t in result.tools] == ['listPosts']

@pytest.mark.asyncio
async def test_resume_never_fills_invented_email(monkeypatch):
    from backend.services import gemini_client
    from backend.services.resume_builder import parse_resume_from_text
    monkeypatch.setattr(gemini_client,'generate_json',AsyncMock(return_value={
        'contact':{'name':'Sam','email':'invented@example.com'},'skills':[],
    }))
    resume = await parse_resume_from_text('Sam is a Python developer.')
    assert resume.contact.email == ''
    resume = await parse_resume_from_text('Sam. Contact sam@real.example')
    assert resume.contact.email == 'sam@real.example'

@pytest.mark.asyncio
async def test_research_uses_grounding_metadata_not_model_text(monkeypatch, budget_repo):
    from backend.services import gemini_client
    response = SimpleNamespace(text='https://invented.invalid',candidates=[SimpleNamespace(
        grounding_metadata=SimpleNamespace(grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri='https://verified.example/page',title='Source'))]))])
    generate = AsyncMock(return_value=response)
    monkeypatch.setattr(gemini_client,'_client',lambda:None)
    monkeypatch.setattr(gemini_client,'_generate_content_async',generate)
    found = await gemini_client.search_web('current news')
    assert found == [{'url':'https://verified.example/page','title':'Source','snippet':''}]
    assert generate.call_args.kwargs['config'].tools[0].google_search is not None

@pytest.mark.asyncio
async def test_research_no_sources_fails_truthfully(monkeypatch):
    from backend.agents.research.research_agent import ResearchAgent
    agent = ResearchAgent()
    monkeypatch.setattr(agent,'_search',AsyncMock(return_value=[]))
    with pytest.raises(RuntimeError,match='No source content'):
        await agent.research('latest news')

def test_resume_tailoring_cannot_change_identity_or_dates():
    from backend.models.resume import Resume
    from backend.services.resume_tailor_service import AntiFabricationValidator
    source = Resume(id='r', contact={'name':'Sam','email':''}, experience=[{'company':'Acme','title':'Engineer','start_date':'2020','bullets':[]}])
    changed = source.model_copy(deep=True)
    changed.experience[0].start_date='N/A'
    assert not AntiFabricationValidator().validate(source,changed)
    changed=source.model_copy(deep=True);changed.contact.email='fake@example.com'
    assert not AntiFabricationValidator().validate(source,changed)

@pytest.mark.asyncio
async def test_generation_reports_real_stages_and_rejects_missing_files(monkeypatch,tmp_path):
    from backend.services import artifact_builder as builder
    from backend.services import gemini_client
    monkeypatch.setattr(builder,'artifacts_root',lambda:tmp_path)
    monkeypatch.setattr(builder,'_persist_artifact_remote',lambda *args:None)
    monkeypatch.setattr(gemini_client,'generate_json',AsyncMock(side_effect=[
        {'name':'Demo','files':[{'path':'index.html','purpose':'Home'},{'path':'styles.css','purpose':'Styles'}]},
        {'files':[{'path':'index.html','content':'<h1>Hello</h1>'}]},
    ]))
    progress=AsyncMock()
    with pytest.raises(builder.ArtifactError,match='do not match'):
        await builder.generate_project('user','A website',scale='standard',on_progress=progress)
    assert progress.call_args_list[0].args[0]['stage']=='planning'
    assert progress.call_args_list[1].args[0]['stage']=='writing_files'

@pytest.mark.asyncio
async def test_job_state_survives_local_cache_clear(monkeypatch,tmp_path):
    from backend.api.routers import capabilities
    from backend.services import artifact_builder
    from backend.config.settings import settings
    monkeypatch.setattr(settings,'STORAGE_BACKEND','sqlite')
    monkeypatch.setattr(artifact_builder,'artifacts_root',lambda:tmp_path)
    job={'job_id':'f02a2297-609f-4df3-8e50-12db92dcf67c','user_id':'u','status':'completed'}
    capabilities._put_job(job)
    capabilities._JOBS.clear()
    assert capabilities._get_job(job['job_id'])==job
    assert capabilities._get_job('../secrets') is None

@pytest.mark.parametrize('url', ['http://example.com', 'https:///missing', 'https://user:pass@example.com', 'https://localhost.', 'https://metadata.google.internal'])
def test_network_targets_require_public_https(url):
    from backend.mcp.builder.openapi_parser import OpenAPIParser, SSRFViolationError
    with pytest.raises(SSRFViolationError):
        OpenAPIParser()._validate_ssrf(url)

@pytest.mark.parametrize('address', ['::1', '::ffff:127.0.0.1', '10.0.0.1', '169.254.169.254', 'fe80::1'])
def test_network_validation_checks_all_dns_answers(monkeypatch, address):
    import socket
    from backend.mcp.builder.openapi_parser import OpenAPIParser, SSRFViolationError
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, '', (address, 443)),
    ])
    with pytest.raises(SSRFViolationError):
        OpenAPIParser()._validate_ssrf('https://mixed.example')

@pytest.mark.asyncio
async def test_http_workflow_rejects_private_target_before_request():
    from backend.engine.engine import WorkflowEngine
    from backend.mcp.builder.openapi_parser import SSRFViolationError
    engine = WorkflowEngine(None, None)
    with pytest.raises(SSRFViolationError):
        await engine._execute_core_node(SimpleNamespace(agent='core.http'), {'url': 'https://127.0.0.1'}, None)

@pytest.mark.asyncio
@pytest.mark.parametrize('initial,expected,run_status', [('FAILED', 'BLOCKED', 'FAILED'), ('SKIPPED', 'SKIPPED', 'COMPLETED')])
async def test_terminal_dependencies_propagate_in_reverse_dag_order(tmp_path, initial, expected, run_status):
    from backend.engine.engine import WorkflowEngine
    from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
    db = DatabaseManager(str(tmp_path/'dag.db'))
    await db.initialize()
    try:
        repo = SQLiteWorkflowRepository(db)
        await repo.save_run({'run_id': 'run', 'goal': 'test branches', 'status': 'RUNNING', 'tasks': [
            {'task_id': 'grandchild', 'agent': 'core.set', 'dependencies': ['child']},
            {'task_id': 'child', 'agent': 'core.set', 'dependencies': ['root']},
            {'task_id': 'root', 'agent': 'core.set', 'status': initial},
        ]})
        bus = SimpleNamespace(publish=AsyncMock())
        engine = WorkflowEngine(repo, bus)
        engine._emit_event = lambda *a, **k: None
        await engine.evaluate_dag('run')
        run = await repo.get_run('run')
        assert run['status'] == run_status
        assert {t['status'] for t in run['tasks'] if t['task_id'] != 'root'} == {expected}
        bus.publish.assert_not_awaited()
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_unknown_core_node_cannot_complete():
    from backend.engine.engine import WorkflowEngine
    with pytest.raises(ValueError, match='Unknown core node'):
        await WorkflowEngine(None, None)._execute_core_node(SimpleNamespace(agent='core.missing'), {}, None)

@pytest.mark.asyncio
async def test_browser_prompt_redacts_reflected_vault_secret(monkeypatch):
    from backend.services.web_agent import WebAgent
    from backend.services import gemini_client
    model = AsyncMock(return_value={'action': 'done', 'success': True, 'result': 'ok'})
    monkeypatch.setattr(gemini_client, 'generate_json', model)
    await WebAgent()._decide('read page', {'url': 'https://example.com', 'title': 'title',
        'text': 'reflected secret-password-123', 'elements': [{'idx': 1, 'tag': 'input', 'text': 'secret-password-123'}]},
        ['typed secret-password-123'], {'password': 'secret-password-123'}, 1, 2)
    prompt = model.call_args.args[0]
    assert 'secret-password-123' not in prompt
    assert '{{secret:password}}' in prompt

def test_browser_navigation_does_not_trust_sibling_tenants():
    from backend.services.web_agent import WebAgent, WebAgentError, _registrable_domain
    allowed = {_registrable_domain('alice.github.io')}
    WebAgent._check_domain('https://alice.github.io/page', allowed)
    with pytest.raises(WebAgentError):
        WebAgent._check_domain('https://mallory.github.io/page', allowed)
    with pytest.raises(WebAgentError):
        WebAgent._check_domain('https://user:password@alice.github.io', allowed)

@pytest.mark.asyncio
async def test_browser_request_guard_blocks_private_subresources(monkeypatch):
    from backend.services.web_agent import WebAgent
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    monkeypatch.setattr(OpenAPIParser, '_validate_ssrf', lambda self, url: (_ for _ in ()).throw(ValueError('private')))
    context = SimpleNamespace(route=AsyncMock())
    await WebAgent()._guard_context(context, {'example.com'})
    guard = context.route.call_args.args[1]
    route = SimpleNamespace(request=SimpleNamespace(url='https://127.0.0.1', is_navigation_request=lambda: False), abort=AsyncMock(), continue_=AsyncMock())
    await guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()

@pytest.mark.asyncio
async def test_failed_tool_result_is_not_cached_as_success(ledger):
    from backend.mcp.tool_router import ToolRouter, ToolRouterError
    router = ToolRouter(SimpleNamespace(get_mcp=AsyncMock(return_value={'owner':'user'})), None, idempotency_repo=ledger)
    router.execute_tool = AsyncMock(return_value={'ok':False, 'error':'upstream unavailable'})
    with pytest.raises(ToolRouterError, match='upstream unavailable'):
        await router.execute_tool_safe('unique-test-mcp__list', {}, {'user_id':'user','workflow_id':'wf','task_id':'task'})
    key = IdempotencyGuard.compute_key('wf','task','unique-test-mcp__list',{})
    # A remote error does not establish that a side effect did not occur.
    assert (await ledger.get_record(key))['status'] == 'uncertain'

@pytest.mark.asyncio
async def test_schedule_creation_persists_tasks_atomically_and_deduplicates(tmp_path):
    from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
    db = DatabaseManager(str(tmp_path/'schedule.db'))
    await db.initialize()
    try:
        repo = SQLiteWorkflowRepository(db)
        payload = {'run_id':'scheduled-run','goal':'check health','tasks':[{'task_id':'root','agent':'core.health'}]}
        claims = await asyncio.gather(*[repo.create_if_absent(payload) for _ in range(4)])
        assert sum(claims) == 1
        assert len((await repo.get_run('scheduled-run'))['tasks']) == 1
        with pytest.raises(KeyError):
            await repo.create_if_absent({'run_id':'broken','goal':'invalid','tasks':[{'agent':'core.health'}]})
        assert await repo.get_run('broken') is None
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_browser_preview_masks_form_fields_and_removes_url_tokens():
    from backend.services.web_agent import WebAgent
    mask = object()
    page = SimpleNamespace(url='https://example.com/account?token=secret#key',
        screenshot=AsyncMock(return_value=b'image-bytes'), locator=lambda selector: mask)
    frame = await WebAgent._capture_preview(page, 3)
    assert frame['url'] == 'https://example.com/account'
    assert frame['step'] == 3
    assert frame['image'].startswith('data:image/jpeg;base64,')
    assert page.screenshot.call_args.kwargs['mask'] == [mask]

@pytest.mark.asyncio
async def test_website_builder_does_not_fabricate_tools_when_model_fails(monkeypatch):
    from backend.mcp.website_mcp import plan_website_connector
    from backend.services import gemini_client
    monkeypatch.setattr(gemini_client, 'generate_json', AsyncMock(side_effect=RuntimeError('model unavailable')))
    with pytest.raises(RuntimeError, match='model unavailable'):
        await plan_website_connector('build tools', 'https://example.com', 'https://example.com')

@pytest.mark.asyncio
async def test_specialist_agents_persist_scope_enable_and_dispatch(tmp_path):
    import hashlib
    from backend.repositories.plugin_repository import SQLitePluginRepository
    from backend.services.runtime_snapshot import RuntimeSnapshotRegistry
    from backend.services.plugin_registry_service import PluginRegistryService
    from backend.agents.agent_factory import AgentFactory
    from backend.models.plugin import PluginManifest, PluginAgentDefinition, PluginScope, PluginLifecycleState
    db = DatabaseManager(str(tmp_path/'plugins.db'))
    await db.initialize()
    try:
        repo = SQLitePluginRepository(db)
        snapshots = RuntimeSnapshotRegistry()
        registry = PluginRegistryService(repo, snapshots)
        manifest = PluginManifest(plugin_id='writer', name='Writer', version='1.0.0',
            description='Writes summaries', author='test', agentos_version='1.0.0',
            requested_permissions=[], required_tools=['user-mcp__read'],
            agents=[PluginAgentDefinition(agent_id='writer.summarize', display_name='Summarizer',
                instructions='Summarize source material.', allowed_tools=['user-mcp__read'])],
            scope=PluginScope.USER, manifest_hash='')
        manifest.manifest_hash = hashlib.sha256(manifest.model_dump_json(exclude={'manifest_hash'}, exclude_none=True).encode()).hexdigest()
        saved = await registry.install_plugin(manifest.model_dump_json(), 'alice')
        with pytest.raises(ValueError, match='only be enabled by their owner'):
            await registry.enable_plugin(saved.id, 'bob')
        await registry.enable_plugin(saved.id, 'alice')
        assert (await repo.get_by_id(saved.id)).state == PluginLifecycleState.ENABLED
        assert snapshots.get_snapshot().active_plugins[0].installed_by == 'alice'
        agent_factory = AgentFactory(snapshots, tool_router=SimpleNamespace(execute_tool_safe=AsyncMock()))
        assert agent_factory.build_agent('writer.summarize', context={'user_id':'alice'}) is not None
        assert agent_factory.build_agent('writer.summarize', context={'user_id':'bob'}) is None
        with pytest.raises(ValueError, match='only be disabled by their owner'):
            await registry.disable_plugin(saved.id, 'bob')
        await registry.disable_plugin(saved.id, 'alice')
        assert snapshots.get_snapshot().active_plugins == []
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_plugin_registry_rejects_tool_privilege_expansion(tmp_path):
    import hashlib
    from backend.repositories.plugin_repository import SQLitePluginRepository
    from backend.services.runtime_snapshot import RuntimeSnapshotRegistry
    from backend.services.plugin_registry_service import PluginRegistryService
    from backend.models.plugin import PluginManifest, PluginAgentDefinition
    db = DatabaseManager(str(tmp_path/'bad-plugin.db'))
    await db.initialize()
    try:
        repo=SQLitePluginRepository(db); registry=PluginRegistryService(repo, RuntimeSnapshotRegistry())
        manifest=PluginManifest(plugin_id='bad',name='Bad',version='1',description='',author='test',agentos_version='1.0.0',
            required_tools=['safe__read'],agents=[PluginAgentDefinition(agent_id='bad.agent',display_name='Agent',instructions='x',allowed_tools=['other__delete'])],manifest_hash='')
        manifest.manifest_hash=hashlib.sha256(manifest.model_dump_json(exclude={'manifest_hash'},exclude_none=True).encode()).hexdigest()
        with pytest.raises(ValueError, match='may only use tools declared'):
            await registry.install_plugin(manifest.model_dump_json(),'alice')
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_agent_plugin_api_installs_lists_enables_and_disables_user_agent(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.api.dependencies.auth import AuthenticatedUser
    from backend.api.routers.agents import (
        AgentToolsRequest, InstallAgentRequest, disable_agent_plugin,
        enable_agent_plugin, install_agent_plugin, list_agent_plugins,
    )
    from backend.repositories.plugin_repository import SQLitePluginRepository
    from backend.services.plugin_registry_service import PluginRegistryService
    from backend.services.runtime_snapshot import RuntimeSnapshotRegistry

    db = DatabaseManager(str(tmp_path/'agent-api.db'))
    await db.initialize()
    try:
        repo = SQLitePluginRepository(db)
        registry = PluginRegistryService(repo, RuntimeSnapshotRegistry())
        tools = SimpleNamespace(get_tool_catalog=AsyncMock(return_value=[
            {'agent_tool_name':'demo__read', 'name':'read'}
        ]))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            plugin_registry=registry, tool_router=tools
        )))
        user = AuthenticatedUser(user_id='alice', email='a@example.test', name='Alice', role='user', auth_provider='test')
        monkeypatch.setattr('backend.api.routers.agents.check_rate_limit', lambda *_args, **_kwargs: None)
        response = await install_agent_plugin(InstallAgentRequest(manifest={
            'plugin_id':'summarizer', 'name':'Summarizer', 'version':'1.0.0',
            'description':'Summarizes connected resources.', 'author':'Alice',
            'agentos_version':'1.0.0', 'requested_permissions':[],
            'required_tools':['demo__read'], 'scope':'USER',
            'agents':[{'agent_id':'alice.summarizer','display_name':'Summarizer',
                       'instructions':'Summarize the selected resource.', 'allowed_tools':['demo__read']}],
        }), request, user)
        plugin_id = response['plugin']['id']
        listed = await list_agent_plugins(request, user)
        assert [item['id'] for item in listed['plugins']] == [plugin_id]
        enabled = await enable_agent_plugin(plugin_id, AgentToolsRequest(required_tools=['demo__read']), request, user)
        assert enabled['status'] == 'enabled'
        assert registry.snapshot_registry.get_snapshot().active_plugins[0].installed_by == 'alice'
        await disable_agent_plugin(plugin_id, request, user)
        assert (await repo.get_by_id(plugin_id)).state.value == 'DISABLED'
        stranger = AuthenticatedUser(user_id='bob', email='b@example.test', name='Bob', role='user', auth_provider='test')
        assert (await list_agent_plugins(request, stranger))['plugins'] == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_catalog_fallback_propagates_tool_failure(monkeypatch):
    from backend.engine import mcp_catalog
    from backend.engine.engine import WorkflowEngine

    monkeypatch.setattr(mcp_catalog, 'pick_catalog_tool', lambda _goal, _catalog: {
        'agent_tool_name': 'demo__read', 'input_schema': {'type': 'object', 'properties': {}}
    })

    class BrokenRouter:
        async def execute_tool_safe(self, *_args, **_kwargs):
            raise RuntimeError('upstream unavailable')

    engine = WorkflowEngine(workflow_repo=None, message_bus=None)
    with pytest.raises(RuntimeError, match='upstream unavailable'):
        await engine._call_matching_catalog_tool(
            SimpleNamespace(goal='read the current status'),
            [{'agent_tool_name': 'demo__read'}], BrokenRouter(), {'user_id': 'alice'}
        )


@pytest.mark.asyncio
async def test_integration_delete_enforces_owner_check():
    from fastapi import HTTPException
    from backend.api.dependencies.auth import AuthenticatedUser
    from backend.api.routers.integrations import delete_integration

    repo = SimpleNamespace(
        get_mcp=AsyncMock(return_value={'mcp_id': 'private', 'owner': 'alice'}),
        delete_mcp=AsyncMock(return_value=True),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        factory=SimpleNamespace(mcp_repo=repo)
    )))
    bob = AuthenticatedUser(user_id='bob', email='b@example.test', name='Bob', role='user', auth_provider='test')
    with pytest.raises(HTTPException) as exc:
        await delete_integration('private', request, bob)
    assert exc.value.status_code == 404
    repo.delete_mcp.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_persistence_failure_does_not_leave_task_waiting(monkeypatch):
    from backend.engine.engine import WorkflowEngine
    from backend.models.schemas import WorkflowRun, Task
    from backend.services.approvals_engine import ApprovalRequiredException

    task = Task(task_id='send', workflow_id='wf', run_id='run', user_id='alice', agent='OrchestratorAgent')
    run = WorkflowRun(run_id='run', workflow_id='wf', user_id='alice', goal='send an email', tasks=[task])
    claimed_run = run.model_dump(mode='json')
    claimed_run['tasks'][0]['attempt'] = 1
    claimed_run['tasks'][0]['status'] = 'RUNNING'
    repo = SimpleNamespace(
        get_run=AsyncMock(side_effect=[run.model_dump(mode='json'), claimed_run]),
        claim_task=AsyncMock(return_value=True),
        update_task=AsyncMock(),
        save_approval=AsyncMock(side_effect=OSError('database unavailable')),
        save_event=AsyncMock(),
    )
    engine = WorkflowEngine(repo, SimpleNamespace())
    monkeypatch.setattr('backend.services.llm_context.load_user_model', AsyncMock())
    approval = SimpleNamespace(approval_id='approval-1', model_dump=lambda **_kwargs: {})
    engine._execute_ai_agent = AsyncMock(side_effect=ApprovalRequiredException('Approval required', approval))
    engine._handle_task_failure = AsyncMock()

    await engine.execute_task('run', 'send')

    engine._handle_task_failure.assert_awaited_once()
    assert 'Could not persist required approval' in engine._handle_task_failure.await_args.args[2]


@pytest.mark.asyncio
async def test_vertex_health_does_not_expose_project_id(monkeypatch):
    from backend.services.gemini_client import probe_key_status

    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'private-project-123')
    result = await probe_key_status()
    assert result['status'] == 'vertex'
    assert 'private-project-123' not in result['detail']


@pytest.mark.asyncio
async def test_gemini_probe_does_not_return_provider_exception_text(monkeypatch):
    from backend.services.gemini_client import probe_key_status

    monkeypatch.setenv('GEMINI_API_KEY', 'test-secret-key')
    monkeypatch.delenv('GOOGLE_CLOUD_PROJECT', raising=False)

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            raise RuntimeError('request contained test-secret-key and private-project-123')

    import httpx
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_kwargs: FailingClient())
    result = await probe_key_status()
    assert result['status'] == 'unknown'
    assert 'test-secret-key' not in result['detail']
    assert 'private-project-123' not in result['detail']


@pytest.mark.asyncio
async def test_gemini_probe_sends_key_in_header_not_request_url(monkeypatch):
    from backend.services.gemini_client import probe_key_status

    monkeypatch.setenv('GEMINI_API_KEY', 'test-secret-key')
    monkeypatch.delenv('GOOGLE_CLOUD_PROJECT', raising=False)
    calls = {}

    class Response:
        status_code = 200

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            calls['url'] = url
            calls.update(kwargs)
            return Response()

    import httpx
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_kwargs: Client())
    result = await probe_key_status()

    assert result['status'] == 'catalog_ok'
    assert calls['url'] == 'https://generativelanguage.googleapis.com/v1beta/models'
    assert 'test-secret-key' not in calls['url']
    assert calls['headers']['x-goog-api-key'] == 'test-secret-key'


def test_ephemeral_vault_key_is_never_written_to_logs(monkeypatch, caplog):
    from backend.security.secrets_vault import SecretsVault, settings

    # The ephemeral key is a development path; production correctly refuses
    # to start without a real master key (clean-image run caught this).
    monkeypatch.setattr(settings, 'APP_ENV', 'development')
    monkeypatch.setattr(settings, 'SECRETS_MASTER_KEY', None)
    monkeypatch.delenv('VAULT_USE_SECRET_MANAGER', raising=False)
    vault = SecretsVault()
    with caplog.at_level('WARNING'):
        vault.initialize()

    assert vault._master_key
    assert vault._master_key not in caplog.text
    assert 'ephemeral development key' in caplog.text


def test_model_registry_includes_live_flash_default_and_explicit_pro(monkeypatch):
    from backend.config.settings import settings
    from backend.services import gemini_client, llm_context

    monkeypatch.setattr(settings, 'GEMINI_MODEL', 'gemini-3.8-flash')
    monkeypatch.setattr(settings, 'GEMINI_ALLOWED_MODELS',
                        'gemini-3.8-flash,gemini-3.7-flash,gemini-3.1-pro-preview')
    monkeypatch.setattr(llm_context, 'get_user_model', lambda: None)
    assert gemini_client.candidate_models()[0] == 'gemini-3.8-flash'
    assert gemini_client.candidate_models('gemini-3.1-pro-preview') == ['gemini-3.1-pro-preview']
    with pytest.raises(ValueError, match='not enabled'):
        gemini_client.candidate_models('made-up-model')


def test_provider_permission_errors_are_not_retried_as_model_fallbacks():
    from backend.services.gemini_client import is_quota_error, is_retryable_model_error

    denied = RuntimeError('403 PERMISSION_DENIED: project access denied')
    assert not is_quota_error(denied)
    assert not is_retryable_model_error(denied)
    assert is_retryable_model_error(RuntimeError('404 NOT_FOUND: model unavailable'))


@pytest.mark.asyncio
async def test_webhook_rejects_oversized_content_length_before_reading_body(monkeypatch):
    from fastapi import HTTPException
    from backend.api.routers import webhooks

    stream_called = False
    async def body_stream():
        nonlocal stream_called
        stream_called = True
        yield b'{}'

    factory = SimpleNamespace(
        schedule_repo=SimpleNamespace(get_schedule=AsyncMock(return_value={
            'schedule_id':'s1', 'user_id':'alice', 'goal':'run'
        })),
        secrets_repo=SimpleNamespace(get_secret=AsyncMock(return_value='encrypted')),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(factory=factory, workflow_engine=object())),
        headers={'content-length':'1000001'}, stream=body_stream,
    )
    from backend.security import rate_limiter
    monkeypatch.setattr(rate_limiter, 'check_rate_limit', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rate_limiter, 'get_client_identifier', lambda _request: 'test-client')
    with pytest.raises(HTTPException) as exc:
        await webhooks.trigger_webhook('s1', request)
    assert exc.value.status_code == 413
    assert not stream_called


@pytest.mark.asyncio
async def test_approval_cannot_resume_a_task_that_is_no_longer_waiting():
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    from backend.api.dependencies.auth import AuthenticatedUser
    from backend.api.routers.approvals import approve_request
    from backend.models.schemas import Task, TaskStatus, WorkflowRun

    run = WorkflowRun(run_id='run', workflow_id='wf', user_id='alice', goal='send email', tasks=[
        Task(task_id='send', workflow_id='wf', run_id='run', user_id='alice',
             agent='OrchestratorAgent', status=TaskStatus.COMPLETED)
    ])
    repo = SimpleNamespace(
        get_approval=AsyncMock(return_value={
            'user_id':'alice', 'run_id':'run', 'task_id':'send', 'workflow_id':'wf',
            'expires_at':(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }),
        get_run=AsyncMock(return_value=run.model_dump(mode='json')),
        resolve_approval=AsyncMock(return_value=True),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        factory=SimpleNamespace(workflow_repo=repo)
    )))
    alice = AuthenticatedUser(user_id='alice', email='a@example.test', name='Alice', role='user', auth_provider='test')
    with pytest.raises(HTTPException) as exc:
        await approve_request('approval-1', request, alice)
    assert exc.value.status_code == 409
    repo.resolve_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_approval_is_marked_expired_and_not_resumed():
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    from backend.api.dependencies.auth import AuthenticatedUser
    from backend.api.routers.approvals import approve_request
    from backend.models.schemas import Task, TaskStatus, WorkflowRun

    run = WorkflowRun(run_id='run', workflow_id='wf', user_id='alice', goal='send email', tasks=[
        Task(task_id='send', workflow_id='wf', run_id='run', user_id='alice',
             agent='OrchestratorAgent', status=TaskStatus.WAITING_APPROVAL)
    ])
    repo = SimpleNamespace(
        get_approval=AsyncMock(return_value={
            'user_id':'alice', 'run_id':'run', 'task_id':'send', 'workflow_id':'wf',
            'expires_at':(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }),
        get_run=AsyncMock(return_value=run.model_dump(mode='json')),
        resolve_approval=AsyncMock(return_value=True),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        factory=SimpleNamespace(workflow_repo=repo)
    )))
    alice = AuthenticatedUser(user_id='alice', email='a@example.test', name='Alice', role='user', auth_provider='test')
    with pytest.raises(HTTPException) as exc:
        await approve_request('approval-1', request, alice)
    assert exc.value.status_code == 410
    repo.resolve_approval.assert_awaited_once_with(
        'approval-1', 'EXPIRED', decision_by='SYSTEM'
    )


def test_email_approval_is_exact_argument_bound():
    from datetime import datetime, timedelta, timezone
    from backend.models.security import ApprovalStatus, AutonomyLevel, RiskLevel
    from backend.services.action_approval import require_approval_for_action
    from backend.services.approvals_engine import ApprovalRequiredException

    context = {'workflow_id':'wf', 'run_id':'run', 'task_id':'email',
               'autonomy_level':AutonomyLevel.L2_SEMI_AUTONOMOUS}
    args = {'to':['person@example.test'], 'subject':'Status', 'body':'Ready', 'html':False}
    with pytest.raises(ApprovalRequiredException) as requested:
        require_approval_for_action(context=context, user_id='alice', tool_name='core.email', arguments=args)
    assert requested.value.pending_approval.arguments == args

    approved = requested.value.pending_approval
    approved.status = ApprovalStatus.APPROVED
    approved.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    resumed = {**context, 'approved_request':approved}
    require_approval_for_action(context=resumed, user_id='alice', tool_name='core.email', arguments=args)

    changed = {**args, 'to':['other@example.test']}
    with pytest.raises(ApprovalRequiredException) as requested_again:
        require_approval_for_action(context=resumed, user_id='alice', tool_name='core.email', arguments=changed)
    assert requested_again.value.pending_approval.arguments == changed
