"""Execution receipts bind the resolved input, never caller-supplied policy IDs."""
import hashlib
import json

import pytest

from backend.tests.test_phase13_chat1 import stack, _save_run  # noqa: F401


@pytest.mark.asyncio
async def test_warm_engine_dispatch_tracks_activation_and_rollback(stack):
    from backend.services.prompt_versions import PromptVersionStore, set_prompt_version_store
    store = PromptVersionStore(stack["db"])
    set_prompt_version_store(store)
    baseline = await store.checkout("instructions", "BASELINE: inspect supplied facts", created_by="operator:test")
    candidate = await store.checkout("instructions", "CANDIDATE: inspect and cross-check supplied facts", created_by="agent:test")
    try:
        for index, version in enumerate([baseline, candidate, baseline]):
            await store.activate("instructions", version["version_id"], activated_by="operator:test")
            run = await _save_run(stack["repo"], "policy-run-" + str(index))
            inputs = {"fields": {"ok": True}, "_prompt_id": "instructions", "_effective_prompt_version": "forged"}
            await stack["repo"].update_task(run.run_id, "t1", {"input_data": inputs})
            await stack["engine"].execute_task(run.run_id, "t1")
            steps = await stack["steps"].steps_for_run(run.run_id)
            assert steps[0]["state"] == "VERIFIED"
            assert steps[0]["policy_revision"] == version["version_id"]
            effective = {**inputs, "_instructions": version["template"], "_effective_prompt_version": version["version_id"]}
            expected = hashlib.sha256(json.dumps(effective, sort_keys=True, default=str).encode()).hexdigest()[:32]
            assert steps[0]["validated_action_digest"] == expected
    finally:
        set_prompt_version_store(None)


@pytest.mark.asyncio
async def test_caller_cannot_forge_an_effective_policy_version(stack):
    run = await _save_run(stack["repo"], "forged-policy")
    await stack["repo"].update_task(run.run_id, "t1", {"input_data": {
        "fields": {"ok": True}, "_effective_prompt_version": "attacker-version"}})
    await stack["engine"].execute_task(run.run_id, "t1")
    steps = await stack["steps"].steps_for_run(run.run_id)
    assert not steps[0]["policy_revision"]
