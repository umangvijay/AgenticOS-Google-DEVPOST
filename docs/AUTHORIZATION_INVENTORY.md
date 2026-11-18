# Authorization Inventory (Phase 1.1)

Complete list of identity/access entry points, the actor, the protected
resource, the required permission, and the enforcement location + test.
Compiled 31 October 2026 at revision `v6.1.0`, extended at `v6.4.0`
(ownership transfer, background org-membership recheck) — keep this file
updated when adding any protected operation (Phase 1.1 deliverable).

| # | Entry point | Actor | Resource | Required permission | Enforcement | Test |
|---|---|---|---|---|---|---|
| 1 | Password signup | Anonymous | Account | — (rate-limited, validated) | `auth.signup` | `test_auth_http.py` |
| 2 | Password login | Anonymous | Session tokens | Active account, not locked | `auth.login` (is_active before/after verify) | `test_audit_security.py` |
| 3 | Google login/link | Google identity | Session tokens | Active account; verified provider claims | `auth.google_auth` | `test_audit_security.py` |
| 4 | Access-token auth | User/API key | Any protected route | Valid RS256 + not revoked + active owner | `dependencies/auth.get_current_user` | `test_auth_http.py` |
| 5 | Refresh rotation | User | New tokens | Active account + live refresh record | `auth.refresh` | `test_auth_http.py` |
| 6 | API-key auth | Programmatic | Any protected route | Key hash + active owner | `dependencies/auth._authenticate_api_key` | `test_api_keys.py` |
| 7 | Guest session | Anonymous | Guest workspace | Rate-limited ephemeral | `auth.loginAsGuest` | e2e |
| 8 | Organization create | User (not viewer) | Organization | `workflow.create`-level write access | `orgs.create_org` | `test_orgs_rbac.py` |
| 9 | Invitation create (both routes) | Owner/Admin | Invitation | `organization.invite_member` / `invite_admin` | `organization_invitation_service` + matrix | `test_org_invitations.py` |
| 10 | Invitation accept | Authenticated user | Invitation | Verified recipient email + pending + unexpired | shared service atomic accept | `test_org_invitations.py`, `test_background_authorization.py` |
| 11 | Invitation revoke | Owner/Admin | Invitation | `organization.remove_member` | `orgs.revoke_invitation` | `test_org_invitations.py` |
| 12 | Role change | Owner | Membership | `organization.manage_roles` | `orgs.set_member_role` | `test_orgs_rbac.py` |
| 12b | **Ownership transfer** (Phase 1.8) | Current owner only | Organization | `organization.transfer_ownership` (dedicated route; ordinary role endpoints exclude the owner row) | `orgs.transfer_ownership` — guarded atomic swap, single-owner invariant, audit in-transaction | `test_ownership_transfer.py` |
| 17b | **Background org-run execution** (Phase 1.12) | Worker acting for owner | Org-owned run + org resources | Owner account ACTIVE **and** CURRENT org membership (removed member / deleted org → denied) | `worker.authorize_background_execution` before `execute_task` | `test_ownership_transfer.py` (real gate), `test_background_authorization.py` |
| 13 | Member remove | Admin (owner for admins) | Membership | `organization.remove_member` | `orgs.remove_member` | `test_orgs_rbac.py` |
| 14 | Account disable/enable | Platform admin | Account | `require_admin` + audit | `admin.disable_user` | `test_audit_security.py` |
| 15 | Password change | User | Credentials | Current password + revokes all refresh tokens | `auth.change_password` | `test_auth_http.py` |
| 16 | Workflow read | Owner only | Run | Owner match (query scoped) | `workflows` repo | `test_tenant_isolation.py` |
| 17 | Schedule read/execute | Owner only | Schedule + credentials | Owner match; worker re-checks owner active | `schedules`, `worker.handle_event` | `test_tenant_isolation.py`, `test_background_authorization.py` |
| 18 | Artifact download | Owner only | Artifact files | Owner path containment | `artifacts.get_artifact_file` | `test_tenant_isolation.py` |
| 19 | Credential read/use | Owner only | Secret | Owner-scoped store lookup | `secrets_repo.get_secret` | `test_tenant_isolation.py` |
| 20 | MCP tool execution | Task actor (for owner) | Connector + credential | ToolPolicy (trust tier + evidence) + approval binding | `tool_router` | `test_connector_coverage.py` |
| 21 | Browser consequential action | Task actor (for owner) | External site state | Argument-bound approval (`browser.action`) | `web_agent._execute` gate | `test_v56_review_regressions.py` |
| 22 | Webhook trigger | External signer | Schedule goal | HMAC signature + active schedule + rate limit | `webhooks.trigger_webhook` | `test_readiness_regressions.py` |
| 23 | Org oversight (runs) | Owner/Admin | Member runs (org-owned only) | Current membership + org scope | `orgs.org_run_oversight` | `test_audit_security.py` |
| 24 | Logout / session revoke | User | Session | Ownership of session | `auth.logout` + denylist | `test_audit_security.py` |

## Revocation coverage (Phase 1.11)

| Change | Effect on issued credentials | Latency |
|---|---|---|
| Account disable | Login/Google/refresh/API-key all refused; queued work denied pre-execution | Immediate (per-request current-state load) |
| Password change | All refresh tokens revoked; access tokens ≤ TTL, denylist on logout | ≤ access-token TTL |
| Logout | jti denylisted | Immediate (fail-closed store) |
| API-key revocation | Key hash removed | Immediate |
| Org removal / role change | Org endpoints re-check current membership per request; oversight denied; queued org-run execution denied (`org_membership_lost`); frontend clears stale org state on 403/404 | Immediate |
| Ownership transfer | Old owner demoted to admin atomically with the swap; old owner's sessions remain valid (still an admin) | Immediate |
