# Runbook — Artifact Revocation (Phase 7 §7.24)

## Revoke one artifact

```
controls.revoke_artifact(digest, reason)
```

Blocks new dispatches; already-submitted effects keep their truthful Phase 5
state; evidence is never deleted. Deployment eligibility re-evaluates from
current records and reports the revocation.

## Revoke a runtime image

`controls.revoke_runtime_image(image_digest, reason)` — every artifact
tested on that image is blocked until re-validated on a patched image (new
digest → new attestation required, §7.19).

## Tenant-wide cancellation

`cancel_tenant_executions(repo, tenant_id, reason)` — cancels active
executions; preserves submitted-effect truth.

## After the incident

Evidence stays. Re-enable in stages (§7.24 rollout): packaging → fixture
validation → pure compute → controlled reads → controlled writes (Phase 5
boundary verified) → limited cohorts → wider availability.
