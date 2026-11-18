# Document provenance

## Source separation (§17.12)

Facts carry one of: user supplied, user confirmed, independently
evidenced, extracted-but-unconfirmed, generated suggestion, disputed,
superseded. Preferences control tone/layout only — they cannot
establish employment history or qualifications. A generated résumé
stored in memory is NOT evidence for its own claims on a later run.

Derived calculations preserve input references, units, formula, and
calculation version.

## Correction and staleness (§17.24)

`propagate_source_change` connects deliverables to Phase 16's durable
dependency graph: correcting/deleting/revoking a source marks dependent
verifications STALE (`verification.stale = true` with reason) without
rewriting the historical revision or the published bytes.

Definitions:
- **Source deleted** — the memory is gone; the deliverable's provenance
  cites a missing source.
- **Deliverable deleted** — the record and access are removed.
- **Verification invalidated** — the verdict is stale; bytes and
  history remain.

## Review workflow (§17.22)

`POST /deliverables/{id}/review` binds every action to the EXACT
revision: a stale review is a 409 `REVISION_MISMATCH`. Actions: confirm
as user-attested, remove, edit. Factual changes are recorded separately
from formatting changes in the verification record.
