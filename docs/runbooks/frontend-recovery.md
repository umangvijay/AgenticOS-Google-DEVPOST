# Runbook: frontend recovery

## Draft lost after refresh
1. Check `agentos.draft.v2.{userId}...` in localStorage — drafts are
   identity-scoped (§18.19).
2. Restoration is async (`restored` flag in useDraft) — ChatComposer
   adopts the stored draft when it arrives; a user keystroke before
   restoration intentionally wins.
3. Storage failures surface via `storageError` (private mode, quota).

## Session expired mid-work
1. Refresh rotates via the cross-tab lock (§18.12): the non-owner tab
   re-reads the rotated session instead of reusing the single-use token.
2. Definitive 401/403 → expired state; network errors → temporarily
   unavailable (keep working with stale data).

## Submission acceptance unknown
A timeout after submit leaves acceptance UNKNOWN (§18.10): the SAME
idempotency key is kept, so retrying cannot duplicate the run. The
composer shows the acceptance-unknown notice.

## Stream down
The coordinated poller reconciles after 20s of stream silence. If the
API itself is unreachable, the connection state shows offline; work is
preserved locally.

## Evidence panel shows stale verification
A source correction marks the verification stale (§17.24). Regenerate
or re-review — the badge renders as STALE, never as an unqualified pass.
