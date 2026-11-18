# Runbook: deliverable failures

## Publication interrupted (§17.7)

Symptom: `PublicationTransaction.failures` non-empty; the deliverable
record is `generated` or `stored`, never `delivered`.

1. Read `reconcile_interrupted(tx)` → `interrupted_at` names the stage.
2. Before `stored`: re-run `publish_deliverable` (the candidate resumes).
3. After `stored`: verify stored bytes; re-publish the manifest.
4. If the candidate is unwanted: delete the record (it was never
   downloadable as completed).

## Renderer failure (§17.14)

`verify_rendered_document` returns `verdict: fail` with
`blocking_reasons`. The deliverable record stays `generated` — no ready
artifact exists. Fix the template/renderer, re-render, re-verify.

## Verification stale (§17.24)

A source correction/deletion/revocation marks dependent verifications
stale. Offer regeneration (a new revision) or re-review. The historical
revision is preserved for audit; do not rewrite published bytes.

## Repair exhaustion (§17.18)

After MAX_REPAIR_ATTEMPTS failed repairs the state is `escalated`.
Escalate to the user with the failure evidence; do not grant success
credit and do not loosen acceptance criteria.
