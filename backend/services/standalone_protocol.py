"""Host-observed validation and invocation of isolated MCP stdio artifacts."""
import json
import uuid

from backend.mcp.builder.compute_server import artifact_digest
from backend.services.isolated_runner import ExecutionRequest, get_runner
from backend.services.mcp_protocol_validator import PROTOCOL_VERSION, classify_protocol_run, REQUIRED_PROTOCOL_CASES


def _initialize(channel, tools):
    response = channel.request("initialize", {"protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"experimental": {"agentos/http_read": {"version": 1}}}
            if channel.server_request_handler else {},
        "clientInfo": {"name": "AgentOS validator", "version": "1"}})
    result = response.get("result", {})
    if result.get("protocolVersion") != PROTOCOL_VERSION or "tools" not in result.get("capabilities", {}):
        raise ValueError("MCP version or tool capability negotiation failed")
    channel.notify("notifications/initialized")
    listed, cursor, seen = [], None, set()
    for _ in range(20):
        response = channel.request("tools/list", {"cursor": cursor} if cursor else {})
        page = response.get("result", {})
        if not isinstance(page.get("tools"), list):
            raise ValueError("MCP tool listing failed")
        listed.extend(page["tools"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
        if cursor in seen:
            raise ValueError("Repeated MCP page cursor")
        seen.add(cursor)
    else:
        raise ValueError("MCP tool listing page limit exceeded")
    if sorted(listed, key=lambda t: t["name"]) != sorted(tools, key=lambda t: t["name"]):
        raise ValueError("MCP advertised tools differ from packaged definitions")


def _call(channel, name, arguments):
    return channel.request("tools/call", {"name": name, "arguments": arguments})


def _value(response):
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        raise ValueError("MCP tool returned an error")
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content", [])
    if len(content) != 1 or content[0].get("type") != "text":
        raise ValueError("Expected one JSON result block")
    return json.loads(content[0]["text"])


async def _execute(artifact, tenant_id, client, *, operations):
    runner = get_runner()
    if not getattr(runner, "profile_digest", None) or getattr(runner, "image", None) != artifact["runtime_image_digest"]:
        raise ValueError("Approved runner and artifact image must match")
    if artifact_digest(artifact) != artifact.get("content_digest"):
        raise ValueError("Artifact content changed")
    execution_id = "mcp-stdio-" + uuid.uuid4().hex
    grants = artifact.get("read_grants") or []
    def supervised_client(channel):
        observations = []
        if grants:
            from backend.runners.egress_broker import EgressBroker, EgressGrant
            def still_running():
                execution = runner.container.get_record(execution_id)
                if execution is None or execution.cancelled or execution.finished_at is not None:
                    return False
                row = runner.store.get(execution_id)
                return row is not None and row["state"] == "running"
            broker = EgressBroker(EgressGrant([(g["host"], g["port"]) for g in grants]),
                                  deadline=channel.deadline, authority_check=still_running)
            def broker_request(message):
                if message.get("method") != "agentos/http_read":
                    raise ValueError("Client capability not available")
                if len(observations) >= 20:
                    raise ValueError("Broker request budget exhausted")
                observation = {"method": "GET", "outcome": "denied"}
                observations.append(observation)
                url = (message.get("params") or {}).get("url")
                if not isinstance(url, str) or len(url) > 8000:
                    raise ValueError("Invalid broker URL")
                response = broker.request("GET", url)
                observation.update(outcome="completed", host=response.request.url.host,
                                   status_code=response.status_code, response_bytes=len(response.content))
                return {"status_code": response.status_code, "body": response.text}
            channel.server_request_handler = broker_request
        observed = client(channel)
        observed["broker_observations"] = observations
        return observed
    await runner.submit_execution(ExecutionRequest(execution_id=execution_id, tenant_id=tenant_id,
        artifact_digest=artifact["content_digest"], runtime_image_digest=artifact["runtime_image_digest"],
        policy_profile_id="read_connector" if grants else "protocol_test", operation_allowlist=operations,
        network_grants=[g["host"] + ":" + str(g["port"]) for g in grants]))
    record = await runner.run_to_completion(execution_id, "python -u server.py", artifact["files"], protocol_client=supervised_client)
    outputs = await runner.collect_outputs(execution_id)
    cleaned = await runner.cleanup_execution(execution_id)
    observed = (outputs[0].get("protocol_result") or {}) if outputs else {}
    if record.state.value != "completed" or not cleaned or not observed.get("shutdown_observed"):
        raise ValueError(f"MCP execution/shutdown failed ({execution_id}): " + str(record.exit_reason))
    return execution_id, observed


async def validate_artifact(artifact, tenant_id, cases):
    from jsonschema import Draft202012Validator
    names = {tool["name"] for tool in artifact["tools"]}
    if not isinstance(cases, dict) or set(cases) != names:
        raise ValueError("Every declared tool requires valid, invalid and tool-error fixtures")
    for tool in artifact["tools"]:
        case = cases[tool["name"]]
        validator = Draft202012Validator(tool["inputSchema"])
        validator.validate(case["valid_arguments"])
        validator.validate(case["error_arguments"])
        if validator.is_valid(case["invalid_arguments"]):
            raise ValueError("Invalid-input fixture must violate the declared schema")
        if "expected" not in case:
            raise ValueError("Valid call requires an independently declared expected result")

    def probe(channel):
        _initialize(channel, artifact["tools"])
        observed = {"initialize": "passed", "tools_list": "passed"}
        results = {}
        for name in sorted(names):
            case = cases[name]
            result = _call(channel, name, case["valid_arguments"])
            if _value(result) != case["expected"]:
                raise ValueError("MCP output failed the declared postcondition: " + name)
            invalid = _call(channel, name, case["invalid_arguments"])
            if not isinstance(invalid.get("error"), dict) or invalid["error"].get("code") != -32602:
                raise ValueError("MCP accepted invalid input: " + name)
            failure = _call(channel, name, case["error_arguments"])
            if failure.get("result", {}).get("isError") is not True or not failure["result"].get("content"):
                raise ValueError("MCP tool error was not preserved: " + name)
            results[name] = {"valid": result, "invalid": invalid, "tool_error": failure}
        unknown = _call(channel, "unknown_" + uuid.uuid4().hex, {})
        if not isinstance(unknown.get("error"), dict) or unknown["error"].get("code") != -32602:
            raise ValueError("MCP accepted an unknown tool")
        observed.update({key: "passed" for key in ("tools_call_valid", "tools_call_invalid_input",
                        "tools_call_unknown_tool", "tool_error_preserved")})
        return {"cases": observed, "exchanges": results, "unknown": unknown}

    execution_id, observed = await _execute(artifact, tenant_id, probe, operations=sorted(names))
    observed["cases"]["shutdown"] = "passed"
    verdict = classify_protocol_run([{"case": case["case"], "status": observed["cases"][case["case"]]}
                                    for case in REQUIRED_PROTOCOL_CASES])
    return {**verdict, "execution_id": execution_id, "observed": observed}


async def invoke_artifact(artifact, tenant_id, name, arguments):
    from jsonschema import Draft202012Validator
    tool = next((entry for entry in artifact["tools"] if entry["name"] == name), None)
    if tool is None:
        raise ValueError("Unknown packaged MCP tool")
    Draft202012Validator(tool["inputSchema"]).validate(arguments)
    def invoke(channel):
        _initialize(channel, artifact["tools"])
        response = _call(channel, name, arguments)
        _value(response)  # preserve error semantics instead of accepting exit zero
        return {"result": response["result"]}
    execution_id, observed = await _execute(artifact, tenant_id, invoke, operations=[name])
    return {"executed": True, "execution_id": execution_id,
            "artifact_digest": artifact["content_digest"], **observed["result"]}
