# Website Browser Tools (apps with no API)

For sites without any API, the Forge registers **origin-locked Playwright browser tools**: open pages, click, fill, read content — a real browser, restricted to that site's origin.

**Typical flow**
1. Save a login in **Vault** (e.g. name `mysite`, username + password).
2. Chat: *"Create MCP tools for https://mysite.com"*.
3. Then: *"Log in with vault credential mysite and open the dashboard"*.

CAPTCHA / OTP / MFA pause the run for you — AgentOS never solves those itself; you finish them and press **Resume**.
