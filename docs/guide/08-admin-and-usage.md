# Admin Console & Usage Windows

**Roles:** `user` (default), `viewer`, `admin`. Admins get the hidden **/admin** Control Center — it never appears in any menu; non-admins who navigate there are silently redirected, and the API returns 403.

Admin sees: users with 5h-window run counts, lifetime runs, join dates, enable/disable accounts, platform stats.

**Usage windows:** every user's usage counter is a rolling **5-hour window** (like Claude) — it auto-resets; shown in the token-usage popover.
