
## Controlled browser app for the deployed takeover journey

```bash
docker cp scripts/controlled_app.py agentos-api-1:/tmp/ca.py
docker compose --profile core exec -d api python /tmp/ca.py
# serves input #f on the container's loopback :8777
```

The deployed browser refuses private-network navigation. Serving this fixture
on loopback alone does not satisfy the public-page requirement. Set
`QUALIFY_BROWSER_APP_URL` to an authorized public test page, and optionally
`QUALIFY_BROWSER_INPUT_SELECTOR` to its input selector (default `#f`). The
ownership/takeover test types synthetic text and never submits the form. A
local smoke test can use [httpbin's public test form](https://httpbin.org/forms/post)
with selector `input[name=custname]`; production qualification should use the
release operator's controlled fixture and target. A separate successful smoke
run does not retroactively remove a skipped check from an earlier report.

## Retain reviewed hosted and pilot observations

Set `QUALIFY_HOSTED_EVIDENCE` or `QUALIFY_PILOT_EVIDENCE` to the JSON manifest exported by the release operator. Supply its independently reviewed SHA-256 through the corresponding `_SHA256` variable. Run `.venv/bin/python scripts/qualify_release.py --stage live_inference --stage pilot` for a rehearsal, then run the complete qualifier for a release decision. A subset cannot qualify a release. Use redacted, evidence-only exports: the attempt retains the original manifest and all referenced JSON trace bytes.

Each manifest has `stage` (`live_inference_hosted` or `pilot`), `ok`, integer `exit`, `candidate_sha256`, `target_profile` and `image_digests_sha256`. The image-set digest uses the qualifier's deployment convention: SHA-256 of `json.dumps(image_digests, sort_keys=True).encode()`. The final verdict compares this with the deployment stage's observed digest. Each trace lives beside its manifest and carries the same three identity fields. Each row references its trace by basename in `result_file` and hex `result_sha256`.

- Hosted manifests carry a `providers` object keyed by `vertex`, `bedrock` and `azure`. Each row names the actual `run_id`, `model`, `provider_request_id` and observed boolean checks for workflow, tool_call, usage, cancellation, rotation, revocation, streaming and error. The trace names the provider and matches the row's request, run, model and checks. The trace also records provider-reported input/output token counts in `provider_usage`, `tool_call_id`, `tool_result_sha256`, positive `stream_events`, `cancelled_run_id` and `cancelled_status`, plus `rotation_request_id`, `revocation_request_id`, `error_request_id`, `rotated_credential_succeeded` and `revoked_credential_denied`. Retain redacted provider evidence for reviewer inspection.
- Pilot manifests carry `sessions` and optional `unsuccessful_sessions`. Each row and trace identify the session, run, pseudonymous participant and consent reference. The trace records `verified_outcome`, three boolean `unassisted_tasks_1_to_3` values, `takeover_attempted`, `takeover_succeeded`, `stale_action_refused`, `tenant_isolation_verified` and `failures_reconciled`. Retain the entire enrolled cohort, including failures. The [pilot plan](../qualification/pilot-plan.md) defines the six actual tasks and participant consent process.

A matching hash establishes integrity relative to the reviewed export. The release reviewer must establish the origin of the provider receipts and the reality of the consented sessions before pinning the digest.

## Verify a Helm deployment in a managed cloud cluster

Select `TARGET_PROFILE=gcp-gke`, `aws-eks` or `azure-aks` for the corresponding GKE, EKS or AKS deployment of the existing AgentOS chart. This stage reads cloud and Kubernetes state and submits one authenticated read-only workflow. It requires an already deployed staging cluster. The current probe supports direct HTTPS ingress on the chart's load balancer and single-platform digest-pinned application images. Cloud Run, ECS, external CDNs and multi-architecture index digests require different readback logic.

Supply a reviewed JSON release manifest via `QUALIFY_CLOUD_TARGET`, its independently reviewed SHA-256 via `QUALIFY_CLOUD_TARGET_SHA256`, and an account token injected from the secret store via `QUALIFY_STAGING_TOKEN`. Make these paths available to the local qualification process. Keep the token out of the manifest. The manifest fields are:

- `target_profile`, `candidate_sha256` and `image_digests` with the expected `api`, `worker` and `frontend` registry manifest digests from the release build.
- `cluster`, `context`, `namespace` and `fullname` identify the provider cluster, authenticated kubectl context, namespace and chart resource-name prefix.
- GKE needs `project` and `location`, EKS needs `region` and the expected `cluster_arn`, and AKS needs the full `resource_id`.
- `ingress_origin` is the staging HTTPS origin. `fixture_url` is an operator-controlled HTTPS endpoint that accepts `?challenge=<nonce>` and returns the same JSON object for repeated GETs with that nonce, including the nonce in `challenge` and a nonempty `value`. It receives no staging token.

Run `.venv/bin/python scripts/qualify_release.py --stage target_deployment`. Inspect the attempt's target identity for the cluster, ingress, live pods, image digests, run ID and independently verified output hash. The stage re-reads rollout state after the workflow to detect changes. A subset run produces a deployment receipt only. Restore, hosted model qualification, canary, rollback, alerting and a consented pilot have their own acceptance criteria.

The retained target receipt embeds the exact reviewed manifest text and its hash. Do not put secret values in that manifest. The verifier rejects contradictory manifest/source/image/namespace/origin claims. Pod identities must belong to ReplicaSets controlled by the observed Deployment, and pod specifications and running image IDs must match the pinned images. This proves observed deployment identity relative to the reviewed manifest; it does not itself establish source-to-image build provenance.

The current infrastructure, fault-injection, browser, flagship, restore and performance stage producers run against local services. A cloud profile cannot promote those local receipts into cloud qualification: the verdict explicitly rejects that combination. The managed-cluster probe currently qualifies only the target-deployment stage. Complete target-specific operational execution and evidence integration before promoting any cloud release. The three required application components do not cover optional runner/browser-worker deployments; those need their own operational qualification.

Provider readback reference: [GKE API server authentication](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/api-server-authentication), [EKS describe-cluster](https://docs.aws.amazon.com/cli/latest/reference/eks/describe-cluster.html), and [AKS cluster listing and identity fields](https://learn.microsoft.com/en-us/azure/aks/list-aks-clusters).
