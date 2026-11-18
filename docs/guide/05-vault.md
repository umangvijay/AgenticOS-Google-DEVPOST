# Vault — Your Secrets

AES-256-GCM encrypted storage for logins and API keys. Agents see only `{{secret:name}}` placeholders — never raw values.

- Save a **site credential** (name + username/email + password) for browser logins.
- Save **API keys** — the agent attaches them to integrations automatically when you ask.
- Model keys (Gemini, Claude, Grok, Z.ai, OpenAI) are validated **live with the provider before saving** — invalid keys are rejected.
