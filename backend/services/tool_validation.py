"""Revision-bound evidence for declarative operations, never whole-app verification.

Phase 6 (§6.4/§6.5/§6.12):
- Provenance describes WHERE a definition came from and NEVER upgrades by
  being tested. Missing/unverifiable provenance defaults to `unknown`.
- Evidence is only valid inside an explicit validation context: a definition
  digest AND a credential binding identity. Context-free summaries may
  describe historical records but never label operations executable.
- One authoritative evidence rule feeds policy, coverage, UI, and dispatch.
"""
import hashlib
import json
from datetime import datetime, timezone

# §6.4: explicit provenance categories. Import method (url/spec/docs/prompt/
# website) is NOT proof of publisher identity — a pasted document is not an
# official publisher document.
PROVENANCE_CATEGORIES = (
    "official_spec",        # verified publisher specification
    "user_supplied_spec",   # user pasted/uploaded a document
    "extracted_docs",       # extracted from documentation (existing name kept, §6.22)
    "inferred_description", # model-inferred sketch (existing name kept, §6.22)
    "unknown",              # missing or unverifiable — the DEFAULT
)

# Methods that yield verified-publisher provenance. Everything else must not
# silently inherit it.
_VERIFIED_SPEC_METHODS = ("openapi", "url", "spec")

# §6.12: execution-eligible evidence requires BOTH parts of the validation
# context. `require_context=False` only describes history.
def validation_provenance(tool: dict) -> str:
    """Where this operation's definition came from (audit G06, §6.4): the
    official spec, documentation extraction, model-inferred, or unknown."""
    raw = ((tool.get("operation") or {}).get("_validation") or {}).get("provenance")
    if raw in PROVENANCE_CATEGORIES:
        return raw
    return "unknown"


def spec_source_provenance(method: str) -> str:
    """Map an import method to its provenance category. Unknown methods
    default to `unknown` — never to official_spec (§6.4). 'url'/'spec' mean
    the import CHANNEL, not publisher proof, but they are the documented
    verified-spec channels for this deployment; everything else is not."""
    if method in _VERIFIED_SPEC_METHODS:
        return "official_spec"
    if method == "docs":
        return "extracted_docs"
    if method in ("prompt", "website"):
        return "inferred_description"
    return "unknown"


def definition_digest(tool: dict, spec_hash: str | None) -> str:
    """§6.5: the IMMUTABLE definition digest — execution-relevant fields
    only. Mutable validation metadata (`_validation`), timestamps, health,
    and activation flags are excluded so testing can never change identity."""
    operation = {k: v for k, v in (tool.get("operation") or {}).items()
                 if k != "_validation"}
    definition = {"operation": operation, "schema": tool.get("input_schema") or {},
                  "name": tool.get("tool_name"), "version": tool.get("mcp_version"),
                  "risk": tool.get("risk_level"), "spec_hash": spec_hash,
                  "digest_format": "v2"}
    return hashlib.sha256(json.dumps(definition, sort_keys=True, default=str).encode()).hexdigest()


def record_read_probe(tool: dict, spec_hash: str | None, credential_binding_id=None) -> dict:
    """Record a passed read probe bound to the FULL validation context:
    the definition digest AND the credential binding (not a raw ref)."""
    existing = (tool.get("operation") or {}).get("_validation") or {}
    evidence = {"kind": "read_probe", "status": "passed", "digest": definition_digest(tool, spec_hash),
                "credential_binding_id": credential_binding_id,
                "tested_at": datetime.now(timezone.utc).isoformat(),
                # Provenance survives the probe: an operation's origin never
                # upgrades itself by being tested (audit G06, §6.4).
                "provenance": existing.get("provenance", "unknown")}
    return {**tool, "operation": {**(tool.get("operation") or {}), "_validation": evidence}}


def has_valid_read_probe(tool: dict, spec_hash: str | None,
                         credential_binding_id=None, *,
                         require_context: bool = True) -> bool:
    """§6.12: execution-eligible evidence requires the explicit validation
    context — the definition digest AND the credential binding identity.
    With require_context=False the check only describes a historical record
    and MUST NOT be used to label an operation currently executable."""
    op = tool.get("operation") or {}
    evidence = op.get("_validation") or {}
    base = (op.get("http_method") in ("GET", "HEAD")
            and evidence.get("status") == "passed" and evidence.get("kind") == "read_probe"
            and evidence.get("digest") == definition_digest(tool, spec_hash))
    if not base:
        return False
    if require_context:
        return (credential_binding_id is not None
                and evidence.get("credential_binding_id") == credential_binding_id)
    # History-only view: a legacy record may carry the old credential_ref.
    return ("credential_binding_id" in evidence
            or evidence.get("credential_ref") is not None)


def validation_context_valid(tool: dict, spec_hash: str | None,
                             credential_binding_id) -> bool:
    """§6.8: evidence must match the CURRENT validation context exactly —
    definition digest and credential binding. Stale evidence is untested."""
    return has_valid_read_probe(tool, spec_hash, credential_binding_id,
                                require_context=True)


def credential_binding_id_from(auth) -> str | None:
    """Derive the validation-context binding id from an auth blob. The
    binding changes when the credential identity changes (§6.7) — a stale
    ref must never satisfy fresh evidence."""
    if isinstance(auth, str):
        try:
            auth = json.loads(auth)
        except (ValueError, TypeError):
            auth = None
    if not isinstance(auth, dict):
        auth = {}
    auth_type = str(auth.get("type", "NONE")).upper()
    ref = auth.get("credential_ref")
    # §17.5: None values (from a parsed AuthMetadata) mean MISSING — the
    # same defaults as a dict without the keys, so the binding id is
    # identical whether the caller passes a raw dict or a parsed model.
    epoch = auth.get("authorization_epoch") or "e0"
    version = auth.get("secret_version") or "v0"
    if not ref and auth_type in ("NONE", "", "NO_AUTH"):
        # No-credential context: a deterministic binding so evidence still
        # binds to "this connector runs without credentials" (§6.7) — and
        # attaching a credential later invalidates the evidence.
        return hashlib.sha256(f"no-auth|{epoch}|{version}".encode()).hexdigest()[:32]
    if not ref:
        return None
    # The binding id IS the (ref, version, epoch) triple — rotating secrets
    # or switching accounts changes it, invalidating account-bound evidence.
    return hashlib.sha256(f"{ref}|{version}|{epoch}".encode()).hexdigest()[:32]


def coverage_from_tools(tools: list, spec_hash: str | None = None,
                        credential_binding_id=None, *,
                        require_context: bool = True) -> dict:
    """Per-operation test coverage summary (audit G02; §6.12 fix).

    With require_context=True (the DEFAULT and the only mode that may label
    operations currently executable), evidence is digest-verified AND bound
    to the current credential binding — the §6.1 bypass where omitting
    spec_hash counted an unverified probe is closed. require_context=False
    yields a HISTORICAL description that never claims current eligibility."""
    # §6.12: "current" context requires BOTH the spec digest and the
    # credential binding. Without them the summary describes history only.
    current_context = require_context and spec_hash is not None \
        and credential_binding_id is not None
    dumped = [t.model_dump(mode="json") if hasattr(t, "model_dump") else t for t in tools]
    probed = 0
    mutating_untested = 0
    for d in dumped:
        op = d.get("operation") or {}
        evidence = op.get("_validation") or {}
        if current_context:
            passed = has_valid_read_probe(d, spec_hash, credential_binding_id,
                                          require_context=True)
        else:
            # Historical description (§6.12): counts the record, never
            # claims current eligibility — `context` marks it.
            passed = (evidence.get("kind") == "read_probe"
                      and evidence.get("status") == "passed")
        if passed:
            probed += 1
        elif op.get("http_method") not in ("GET", "HEAD"):
            mutating_untested += 1
    return {
        "total": len(dumped),
        "read_probe_passed": probed,
        "untested": len(dumped) - probed,
        "mutating_untested": mutating_untested,
        # §6.12: coverage summaries NEVER claim current eligibility without
        # the full validation context.
        "context": "current" if current_context else "historical",
        "credential_binding_id": credential_binding_id if current_context else None,
    }
