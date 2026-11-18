"""Live HTTP owner-control journey. Agent-worker resume is a separate gate.

Requires a current deployed API and an authorized public test application.
The input selector defaults to #f and may be set for a public test form.
No private-network exception is enabled on the API; the form is never submitted.
"""
import json
import os
import urllib.error
import urllib.request

import pytest

BASE = os.environ.get('QUALIFY_API_URL', 'http://127.0.0.1:8000').rstrip('/')


def api(method, path, token=None, body=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(
        f'{BASE}/api/v1{path}', method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b'{}')


def test_deployed_takeover_journey():
    app_url = os.environ.get('QUALIFY_BROWSER_APP_URL')
    input_selector = os.environ.get('QUALIFY_BROWSER_INPUT_SELECTOR', '#f')
    if not app_url:
        pytest.skip('BLOCKED: QUALIFY_BROWSER_APP_URL must name an authorized public test page')
    try:
        with urllib.request.urlopen(f'{BASE}/health', timeout=3) as response:
            assert response.status == 200
    except OSError:
        pytest.skip('BLOCKED: deployed API unavailable')
    code, auth = api('POST', '/auth/guest', body={})
    assert code in (200, 201)
    token = auth['access_token']
    code, other = api('POST', '/auth/guest', body={})
    assert code in (200, 201)
    stranger = other['access_token']
    code, created = api('POST', '/workflows', token,
                        {'goal': 'browser takeover journey probe'})
    assert code == 201, created
    run_id = created['run_id']
    code, run = api('GET', f'/workflows/{run_id}', token)
    assert code == 200 and run['tasks']
    code, opened = api('POST', '/browser/sessions', token, {
        'run_id': run_id, 'task_id': run['tasks'][0]['task_id'], 'allowed_origins': [app_url]})
    assert code in (200, 201), opened
    sid, epoch = opened['session_id'], opened['controller_epoch']
    path = f'/browser/sessions/{sid}'
    try:
        code, result = api('POST', path + '/execute', token, {
            'action': 'goto', 'target': app_url, 'controller_epoch': epoch})
        assert code == 200 and result['status'] == 'executed', result
        assert api('GET', path + '/frame', stranger)[0] == 403
        assert api('POST', path + '/takeover', stranger, {})[0] == 403
        assert api('POST', path + '/execute', token, {'action': 'click', 'target': input_selector})[0] == 422
        assert api('POST', path + '/pause', token, {})[0] == 200
        assert api('POST', path + '/execute', token, {
            'action': 'click', 'target': input_selector, 'controller_epoch': epoch})[0] == 409
        code, takeover = api('POST', path + '/takeover', token, {})
        assert code == 200 and takeover['controller_epoch'] != epoch
        assert api('POST', path + '/execute', token, {
            'action': 'click', 'target': input_selector, 'controller_epoch': epoch})[0] == 409
        code, typed = api('POST', path + '/execute', token, {
            'action': 'type', 'target': input_selector, 'text': 'live user input',
            'controller_epoch': takeover['controller_epoch']})
        assert code == 200 and typed['status'] == 'executed', typed
        code, frame = api('GET', path + '/frame', token)
        assert code == 200 and frame['frame_base64'] and frame['screenshot_sha256']
        code, handed = api('POST', path + '/return-control', token, {})
        assert code == 200 and handed['requires_fresh_observation']
        # An owner HTTP token cannot impersonate an internal agent even with
        # the current epoch. Internal observation enforcement has its own test.
        assert api('POST', path + '/execute', token, {
            'action': 'extract_text', 'controller_epoch': handed['controller_epoch']})[0] == 403
    finally:
        assert api('DELETE', path, token)[0] == 200
