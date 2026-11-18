# Browser execution, viewing, and takeover (§20.15–§20.16)

## Real execution (§20.15)

`backend/services/browser_execution.py` drives REAL Playwright
chromium against a scoped origin list. Every action is executed for
real and recorded as evidence (`ExecutedAction`: status, url_after,
duration, screenshot sha256, or the REAL error on failure — a failed
selector is recorded `failed` with the actual Playwright timeout, never
rewritten to success). Origin scope is enforced BEFORE navigation; a
session without allowed origins is refused; unknown actions are
refused, not guessed.

Evidence: `pytest backend/tests/test_phase20_browser_service.py -q`
(7 tests against a real local HTTP app — navigation, form submission
with server round-trip, DOM state change, screenshot bytes, real error
capture).

## Viewing and takeover (§20.16)

`backend/api/routers/browser_viewer.py` exposes the session surface:

| Route | Purpose |
|---|---|
| `POST /api/browser/sessions` | open (owner = caller) |
| `GET  /api/browser/sessions` | list OWNED sessions only |
| `GET  /api/browser/sessions/{id}` | state + action evidence (owner or viewer) |
| `POST /api/browser/sessions/{id}/view` | attach READ-ONLY |
| `POST /api/browser/sessions/{id}/takeover` | explicit control transfer |
| `POST /api/browser/sessions/{id}/execute` | one real action |
| `DELETE /api/browser/sessions/{id}` | owner-only close |

Ownership is SERVER-ENFORCED: a stranger's execute/takeover/close gets
403; takeover rotates the controller epoch (stale-epoch targets are
refused downstream per §15.20) and appends an audited history entry;
viewer-role accounts are refused by the real `require_not_viewer`
guard before ownership checks. The frontend
(`frontend/src/components/BrowserSessionPanel.tsx`) renders the role
badge, current URL, action evidence with real errors, and an explicit
refusal message on 403 — a refusal never renders as control.

Evidence: `pytest backend/tests/test_phase20_viewer.py -q` (6) +
`cd frontend && npx vitest run tests/mounted/browser-session-panel.test.tsx` (4).
