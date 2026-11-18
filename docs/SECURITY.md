# Security

AgentOS handles logins and API keys. The controls below describe repository
mechanisms; their coverage and production configuration require verification.
See the 31 October amendment in [release evidence](RELEASE_EVIDENCE.md).

**Scope note:** these are repository-level controls, not a security certification.
Production configuration, deployment IAM, tenant isolation, threat modeling,
monitoring, and independent testing still need verification. See
[production readiness](production-readiness.md) for release gates.

## Account passwords

- New passwords use **Argon2id** (19 MiB memory, 2 iterations, parallelism 1,
  32-byte output, random 16-byte salt). These are OWASP minimum parameters and
  should be benchmarked under expected login concurrency before increasing.
- Passwords are one-way **hashed**, not encrypted. Password reset/recovery
  must issue a new password; the stored verifier cannot be decrypted.
- Policy: length 8–256, upper, lower, digit, special; common-password denylist.
- Lockout: **5** failed logins → **15** minutes.
- Optional **HMAC-SHA256 pepper** (`PASSWORD_PEPPER`) is applied before Argon2id.
- Existing bcrypt hashes remain verifiable; a successful login upgrades them to
  Argon2id. Keep the pepper stable: rotating/removing it requires a planned
  migration or users with peppered hashes will fail verification.
- Password hash work runs off the async event loop with bounded concurrency so
  memory-hard verification cannot stall the API or multiply memory use without
  limit under a burst.

## Vault (API keys, site logins)

- **AES-256-GCM** authenticated encryption.
- New vault writes use **AES-256-GCM** with a fresh random salt and nonce,
  purpose-separated **HKDF-SHA256** key derivation, a version marker, and
  authenticated additional data. Legacy unversioned AES-GCM records remain
  readable using the old PBKDF2-HMAC-SHA256, 480,000-iteration derivation.
- Production startup rejects a missing master key and rejects a configured key
  shorter than 32 bytes.
  Generate it randomly and store it in Secret Manager; never derive it from a
  human password.
- This application-level key derivation is not equivalent to KMS envelope
  encryption. Production GCP should move to Cloud KMS-wrapped data keys with
  key versioning and a tested rotation/recovery procedure.
- REST list returns **names only**. Values are never returned after save.
- Agents receive `{{secret:field}}` placeholders. Substitution happens at keystroke / HTTP header time, not in the model prompt.

The vault is for *agent* site/API secrets (for example a portal named `bharatenglish`). It is not a substitute for the contact-form SMTP App Password in `.env`.

## Sessions and API

- JWT **RS256**, 15-minute access tokens, and single-use refresh-token rotation
  backed by hashed refresh-token records. Production requires stable PEM keys
  from Secret Manager; auto-generated disk keys are for local development.
- CSRF token cookie + header on mutating requests.
- Rate limits on auth, workflow create, MCP build, contact, general.
- CORS allowlist from settings.

## Tool and browser safety

- OpenAPI executor **SSRF** checks: no private, loopback, or link-local targets.
- The inspected browser navigation guard uses exact allowed hosts; shared-domain
  sibling tenants are not implicitly trusted. This does not establish complete
  network egress or action-authorization coverage.
- Playwright refuses private hosts.
- Circuit breaker per MCP after repeated failures.
- Generated schemas and model plans are proposals; runtime policy and the
  executor must still validate what is called. Do not treat model-generated
  code or API documentation as trusted policy.

## Human-in-the-loop (CAPTCHA / OTP / MFA)

AgentOS **does not** solve CAPTCHA, intercept SMS, or bypass MFA.

When a page looks like a challenge, the workflow status becomes `WAITING_APPROVAL`. A headed browser is opened when the environment allows it. You complete the check, then **Resume**. Completing the challenge in a *different* browser than the agent’s session will not continue that session.

## Contact SMTP and Gmail

STARTTLS (port 587) or SMTPS (465). Failures return a reason code such as `smtp_not_configured` or `smtp_auth_failed`. The password is never logged and never returned by `GET /api/v1/contact/status`.

**Do not put your Google mailbox password in `.env`.** Gmail SMTP does not accept it, and that password unlocks Drive, Mail, and account recovery.

Use a **Gmail App Password** only:

1. Enable [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification).
2. Create an App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Set `CONTACT_SMTP_PASSWORD` in the **project-root** `.env`. Spaces are stripped. Save and send again — the contact router re-reads `.env` on each send.

`.env` is listed in `.gitignore`. It is not in GitHub. Do not paste it into chat, Dockerfiles, or Cloud Build substitutions.

On Google Cloud, store `CONTACT_SMTP_PASSWORD` in **Secret Manager** and mount it as a Cloud Run env var. Do not copy `.env` into the container image.

## Secrets on disk vs in git vs in GCP

| Secret | Local | Git | Cloud Run |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | `.env` | never | Secret Manager |
| `CONTACT_SMTP_PASSWORD` | `.env` (App Password) | never | Secret Manager |
| `SECRETS_MASTER_KEY` | auto or `.env` | never | Secret Manager (must be stable or vault ciphertext is unreadable) |
| `PASSWORD_PEPPER` | optional `.env` | never | Secret Manager |
| Vault site passwords | encrypted in SQLite / Firestore | never (ciphertext only) | same stores |
| JWT PEM files | `backend/security/keys/` | gitignored | persist via Secret Manager or they rotate per new instance |

## What we will not do

- Fill or defeat CAPTCHA / OTP / MFA.
- Invent a private API for a website that has none.
- Return vault secrets to the UI.
- Commit `.env`, App Passwords, or service-account JSON.

## Why these controls exist and what remains

The [full-stack roadmap](full-stack-roadmap.md) specifies remaining transaction,
tenant, spending, browser, generated-code and learning controls. In particular:

- Test invitation acceptance/revocation atomically in both stores and verify
  every membership path, including newer working-tree fixes.
- Enforce spending reservations before paid work; durable increments alone do
  not prevent concurrent overspend. Local rate counters are not shared quotas.
- Apply exact-action approval to consequential browser steps as well as tools.
  Browser sessions need ownership, cleanup, expiry and isolated execution.
- Treat generated-code execution as unsupported until its dedicated runner is
  proven; AST checks and protocol-status flags are not isolation evidence.
- Bind connector/artifact test evidence to immutable contents and credential
  versions. Treat model plans and recovery suggestions as untrusted proposals.
- Keep self-improvement and RL outside authorization decisions. Promotion,
  evaluation changes, and permissions need separate controlled ownership.

| Control | Risk reduced | Remaining work |
| --- | --- | --- |
| Argon2id + lockout + rate limits | Offline cracking and credential guessing | Verify limits use a shared store across replicas; add MFA/recovery hardening and benchmark concurrent memory use |
| RS256 sessions + CSRF + CORS allowlist | Token tampering, cross-site writes, unintended browser origins | Rotate keys with KMS/Secret Manager; validate issuer/audience and session revocation behavior |
| AES-GCM vault + name-only responses | Disclosure/tampering of stored credentials | KMS envelope encryption, rotation, tenant-specific key boundaries, recovery drills |
| SSRF checks + domain locks | Private-network access and browser navigation escape | Connection-time DNS pinning, redirect rechecks, egress firewall, metadata endpoint tests |
| HITL for CAPTCHA/OTP/MFA | Bypassing account security challenges | Extend approval to all consequential external writes and show exact action/payload |
| Pub/Sub separation + idempotency patterns | Lost in-process work and duplicate side effects | Dead-letter/replay, queue-age monitoring, duplicate-delivery and restart tests |

For Vertex AI on Cloud Run, attach a dedicated user-managed service identity and
grant only the required Vertex, Firestore, Pub/Sub, and Secret Manager roles.
Do not use service-account key files or ship credentials in container images.
The application-level checks here do not replace cloud IAM, organization
isolation, network egress policy, backups, audit retention, or incident response.
