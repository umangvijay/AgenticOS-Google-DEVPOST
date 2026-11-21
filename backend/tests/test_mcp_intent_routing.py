"""Website vs HTTP vs OpenAPI routing for chat MCP builds."""

import pytest

from backend.engine.direct_plan import plan_from_goal
from backend.engine.mcp_catalog import arguments_for_catalog_tool, pick_catalog_tool, sketch_openapi_from_prompt
from backend.mcp.website_mcp import looks_like_http_api_url, looks_like_website_without_api


def test_example_com_create_tools_is_website():
    goal = "Create MCP tools for https://example.com then open home and summarize the page"
    assert looks_like_website_without_api(goal, "https://example.com")
    plan = plan_from_goal(goal)
    assert plan is not None
    agents = [t.agent for t in plan.tasks]
    assert "core.mcp_build" in agents
    mcp = next(t for t in plan.tasks if t.agent == "core.mcp_build")
    assert mcp.input_data["method"] == "website"


def test_openapi_url_is_url_method():
    goal = "Create MCP tools from https://raw.githubusercontent.com/PokeAPI/pokeapi/master/openapi.yml then list pokemon"
    plan = plan_from_goal(goal)
    mcp = next(t for t in plan.tasks if t.agent == "core.mcp_build")
    assert mcp.input_data["method"] == "url"
    assert "openapi.yml" in mcp.input_data["source"]


def test_github_http_without_openapi_is_prompt():
    goal = "Build MCP tools for GitHub so I can list public events, then list them"
    assert not looks_like_website_without_api(goal, None)
    plan = plan_from_goal(goal)
    mcp = next(t for t in plan.tasks if t.agent == "core.mcp_build")
    assert mcp.input_data["method"] == "prompt"


def test_pokeapi_url_is_http_not_website():
    url = "https://pokeapi.co/api/v2"
    assert looks_like_http_api_url(url)
    assert not looks_like_website_without_api("Create MCP tools for https://pokeapi.co/api/v2", url)


def test_follow_up_reuses_orchestrator():
    prior = "Create MCP tools for https://example.com"
    plan = plan_from_goal("Log in with vault credential bharatenglish and open home / use runOnSite", prior_goal=prior)
    assert plan is not None
    assert plan.tasks[0].agent == "OrchestratorAgent"


def test_sketch_github_events():
    spec = sketch_openapi_from_prompt("Build MCP tools for GitHub so I can list public events")
    assert spec and "api.github.com" in spec
    assert "/events" in spec


def test_pick_catalog_tool_prefers_matching_name():
    catalog = [
        {"name": "runOnSite", "mcp_name": "example.com", "agent_tool_name": "w__runOnSite", "description": "browser"},
        {"name": "list_events", "mcp_name": "GitHub", "agent_tool_name": "g__list_events", "description": "List events"},
    ]
    picked = pick_catalog_tool("list the github events", catalog)
    assert picked["name"] == "list_events"
    args = arguments_for_catalog_tool(
        {"name": "login", "input_schema": {"properties": {"credential_name": {}}, "required": ["credential_name"]}},
        "Log in with vault credential bharatenglish and open home",
    )
    assert args["credential_name"] == "bharatenglish"


def test_capability_question_never_starts_a_connector_build():
    plan = plan_from_goal("can you create any mcp tool and which things do you need in order to create it")
    assert [task.agent for task in plan.tasks] == ["core.chat"]
    assert plan.tasks[0].input_data["topic"] == "connector_requirements"
    assert plan_from_goal("What is photosynthesis?").tasks[0].agent == "core.chat"


def test_build_and_automate_uses_real_source_and_orders_the_consumer():
    from backend.engine.dag_validator import prepare_dag
    plan = prepare_dag(plan_from_goal("Build MCP tools for Petstore and automate a lookup"))
    build, consumer = plan.tasks
    assert build.input_data["source"].startswith("https://")
    assert consumer.dependencies == [build.task_id]
    assert consumer.agent == "OrchestratorAgent"


def test_followup_url_resolves_an_mcp_request():
    plan = plan_from_goal("https://example.org/openapi.json", prior_goal="Create an MCP tool")
    assert plan.tasks[0].input_data["method"] == "url"
    assert plan.tasks[0].input_data["source"] == "https://example.org/openapi.json"


def test_device_git_request_is_a_handoff_only():
    plan = plan_from_goal("push my local files to the repo https://github.com/example/app.git")
    fields = plan.tasks[0].input_data["fields"]
    assert fields["git_request"] == {"operation": "push", "url": "https://github.com/example/app.git", "branch": None}


def test_independent_health_check_can_run_alongside_build():
    from backend.engine.dag_validator import prepare_dag
    plan = prepare_dag(plan_from_goal("Build MCP tools for Petstore and separately check the health of https://example.org"))
    assert [(t.agent, t.dependencies) for t in plan.tasks] == [("core.mcp_build", []), ("core.health", [])]


def test_unknown_service_requests_documentation_without_creating_a_stamp():
    plan = plan_from_goal("Build an MCP connector for RareWidgets")
    assert plan.tasks[0].agent == "core.set"
    assert "URL" in plan.tasks[0].input_data["fields"]["reply"]


def test_docs_links_stay_on_provider_origin():
    from backend.agents.mcp_factory.mcp_factory_agent import SpecLinks
    from backend.mcp.website_mcp import looks_like_documentation_url
    url = "https://docs.example.org/api/"
    assert looks_like_documentation_url(url)
    assert plan_from_goal("Build an MCP for " + url).tasks[0].input_data["method"] == "url"
    links = SpecLinks(url)
    links.feed('<a href="../spec/v2.yaml">API</a><a href="https://other.example/test.json">Other</a>')
    assert links.links == ["https://docs.example.org/spec/v2.yaml"]


def test_standalone_public_url_starts_a_connector_build():
    task, = plan_from_goal("https://docs.example.org/api/").tasks
    assert task.agent == "core.mcp_build"
    assert task.input_data["source"] == "https://docs.example.org/api/"


@pytest.mark.asyncio
async def test_requirements_reply_is_available_during_a_model_outage(monkeypatch):
    from types import SimpleNamespace
    from backend.engine.engine import WorkflowEngine
    from backend.services import gemini_client

    async def unavailable(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(gemini_client, "generate_text", unavailable)
    task, = plan_from_goal("can you create any mcp tool and which things do you need in order to create it").tasks
    result = await WorkflowEngine(None, None)._execute_core_node(task, task.input_data, SimpleNamespace(tasks=[]))
    assert "API documentation URL" in result["reply"]
    assert "Vault" in result["reply"]
