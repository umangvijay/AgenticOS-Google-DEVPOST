"""A remote worker must authenticate even when it knows a live session ID."""
import httpx

from backend.tests.test_phase25_cross_replica_browser import replicas, start_on_a


def test_remote_view_requires_service_and_user_authority(tmp_path):
    with replicas(tmp_path) as (urls, _, origin):
        sid = start_on_a(urls, origin)["session_id"]
        path = urls[0]+"/internal/browser/view"
        body = {"session_id": sid, "actor_user_id":"owner-u"}
        assert httpx.post(path, json=body).status_code == 403
        headers = {"x-internal-token":"local-drill-token"}
        assert httpx.post(path, headers=headers, json={**body, "actor_user_id":"other"}).status_code == 403
        assert httpx.post(path, headers=headers, json=body).is_success
        assert httpx.delete(urls[1]+"/browser/sessions/"+sid).is_success
