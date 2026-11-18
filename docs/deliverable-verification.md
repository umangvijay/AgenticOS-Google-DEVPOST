# Deliverable verification

Phase 17's verification contract. Statuses follow the ledger convention.

## The typed verdict (§17.9)

`backend/services/artifact_verifier.py` returns an explicit verdict —
`pass | fail | needs_review | unavailable` — bound to the artifact's
revision and content digest. Results are NEVER converted to booleans; a
structured result carrying `verified: false` FAILS. Missing evidence is
`unavailable`/`needs_review`, never a pass.

Checks, in order:
1. **Existence + ownership** — resolved through the server-owned,
   owner-scoped repository. Wrong-user access looks identical to
   missing (§12.28).
2. **Integrity metadata** — every published file carries sha256 +
   byte length. A legacy record without metadata is `needs_review`
   under the migration policy (never a silent pass).
3. **Stored bytes** — hashed from the ACTUAL bytes and matched against
   the manifest's per-file digests. Tampering FAILS.

## Supported deliverables (§17.1)

| Type | Required verification | Not yet verified |
|---|---|---|
| Résumé HTML | structured data, factual claims reviewed, safe rendering, bytes registered | PDF layout |
| Résumé PDF | HTML requirements + PDF parses/extracts | visual layout review |
| Factual report | claim-to-source mapping, calculations checked, limitations explicit | source freshness beyond retrieval |
| Source project | manifest complete, integrity, HONEST execution status | execution (no managed runner) |
| Tested project | source requirements + isolated build/test evidence | runtime smoke test |

The lifecycle (§17.10) keeps generated / stored / verified / reviewed /
delivered DISTINCT — a user accepting a draft never establishes that
its factual claims were independently verified.

## Staged publication (§17.7)

`backend/services/deliverable_publisher.py`: allocate → stage →
validate → checksum final bytes → store immutable → verify stored →
publish manifest → emit completion. Every stage is durable; an injected
crash at ANY stage leaves an unpublished candidate (the reconciler
reports `resume_or_cleanup`), never a downloadable "completed" artifact.

## Field-level factual provenance (§17.11)

`backend/services/provenance.py` + `backend/services/deliverable_facts.py`:
claims carry field paths, claim types, and source references. Changed
quantities (10% → 90%), flipped negations, and changed values under an
existing field are CONTRADICTIONS — word overlap never proves support,
and a self-declared `source_fact` label without a verified fact id is
UNBACKED (§17.12: a generated draft cannot confirm its own claims).

## Rendering (§17.14)

`backend/services/rendered_document_verifier.py` verifies the FINAL
HTML: it parses, expected text survives, sections exist in order, sizes
are sane, links use safe schemes, and remote/local fetch vectors are
rejected. `sanitize_for_rendering` strips script/iframe/@import/remote
URLs BEFORE rendering. Visual layout review needs a viewer fixture and
remains named in each type's `not_yet_verified`.

## Repair (§17.18)

`backend/services/deliverable_recovery.py`: bounded, typed repairs
(retry, re-render, layout, regenerate-missing-file, request-source).
Missing source facts trigger CLARIFICATION, never invention; repair
exhaustion ESCALATES without success credit.
