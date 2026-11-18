"""Live staging check. Reads VEYRUNE_TEST_TOKEN from environment; never saves it.

Creates two workflow runs and one MCP integration in the authenticated account.
Does not approve external writes or remove test records. Requires httpx.
"""
import argparse
import asyncio
import json
import os
import time
import uuid
from urllib.parse import urlparse
import httpx


async def wait_for_run(client, run_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get('/workflows/' + run_id)
        response.raise_for_status()
        run = response.json()
        if run.get('status') == 'COMPLETED':
            return run
        if run.get('status') in ('FAILED', 'CANCELLED', 'BLOCKED'):
            raise RuntimeError('Workflow ended with ' + run['status'])
        if any(t.get('status') == 'WAITING_APPROVAL' for t in run.get('tasks', [])):
            raise RuntimeError('Workflow requires human review; no approval was granted by this check')
        await asyncio.sleep(3)
    raise TimeoutError('Workflow did not finish within the test deadline')


def verify_health_result(run):
    tasks = run.get('tasks', [])
    health = [t.get('output_data') or {} for t in tasks if t.get('agent') == 'core.health']
    summaries = [t.get('output_data') or {} for t in tasks if t.get('agent') == 'core.chat']
    if not health or health[0].get('status_code') != 200:
        raise RuntimeError('No persisted successful live health result')
    if not summaries or not summaries[0].get('reply'):
        raise RuntimeError('No persisted model summary')


async def check(args):
    token = os.environ.get('VEYRUNE_TEST_TOKEN', '').strip()
    if not token:
        raise ValueError('Set VEYRUNE_TEST_TOKEN in the process environment')
    base = args.api_url.rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Provide an HTTPS API URL without credentials, query, or fragment')
    # A trailing slash plus relative requests preserves /api/v1.
    async with httpx.AsyncClient(base_url=base + '/', headers={'Authorization': 'Bearer ' + token},
                                 timeout=45, follow_redirects=False) as raw:
        class Client:
            async def get(self, path):
                return await raw.get(path.lstrip('/'))
        client = Client()
        response = await raw.post('workflows', json={'goal': 'Check health of https://example.com and summarize the observed result in one sentence.'})
        response.raise_for_status()
        run_id = response.json()['run_id']
        run = await wait_for_run(client, run_id, args.timeout)
        verify_health_result(run)
        print(json.dumps({'check': 'persisted_health_and_summary', 'status': 'passed', 'run_id': run_id}), flush=True)
        if not args.mcp:
            return
        name = 'release-check-' + uuid.uuid4().hex[:8]
        response = await raw.post('workflows', json={'goal':
            f'Create an MCP integration named {name} for the public HTTPBin API. '
            'Use base URL https://httpbin.org and read-only GET /uuid with no authentication or parameters. '
            'The endpoint returns JSON containing a uuid string. Then use the created tool to fetch a live UUID and report it.'})
        response.raise_for_status()
        run_id = response.json()['run_id']
        run = await wait_for_run(client, run_id, args.timeout)
        tasks = run.get('tasks', [])
        builds = [t for t in tasks if t.get('agent') == 'core.mcp_build']
        uses = [t for t in tasks if 'orchestrator' in str(t.get('agent', '')).lower()]
        if not builds or not uses or not any((t.get('output_data') or {}).get('tool_result') for t in uses):
            raise RuntimeError('Completed status lacks evidence of both MCP build and tool execution')
        print(json.dumps({'check': 'autonomous_mcp_build_and_use', 'status': 'passed', 'run_id': run_id}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', required=True, help='Deployed API base including /api/v1')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--mcp', action='store_true', help='Also create and use an MCP integration; incurs model calls')
    args = parser.parse_args()
    try:
        asyncio.run(check(args))
    except httpx.HTTPStatusError as exc:
        print(json.dumps({'status': 'failed', 'category': 'http', 'http_status': exc.response.status_code}))
        raise SystemExit(1)
    except Exception as exc:
        # Do not dump exception strings, URLs, headers or provider response bodies.
        print(json.dumps({'status': 'failed', 'category': type(exc).__name__}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
