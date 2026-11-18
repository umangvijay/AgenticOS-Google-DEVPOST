"""Immutable connector definitions (Phase 6 §6.5).

Three separated concerns:
1. IMMUTABLE DEFINITION — execution-relevant fields, canonically hashed.
2. Mutable validation history — lives on the tool's `_validation`, excluded
   from identity.
3. Activation history — tracked separately (§6.14).

Canonicalization (§6.5): operations sort by stable identity, object keys
sort, array order is preserved (order can be meaningful), unsupported values
are rejected (no silent `default=str`), and the digest format is versioned.
Test timestamps, probe outcomes, health, and credentials NEVER enter the
identity — testing a connector cannot appear to change its definition.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

MANIFEST_FIELDS = ('name', 'description', 'version', 'transport', 'source_type',
                   'spec_json', 'spec_hash', 'spec_version', 'source_uri')
TOOL_FIELDS = ('tool_name', 'name', 'description', 'input_schema', 'inputSchema',
               'operation', 'risk_level')

# Mutable/secret fields that must NEVER enter an immutable definition.
_OPERATION_EXCLUDED = ("_validation", "_activation", "_health", "_last_used")
_DIGEST_FORMAT = "rev2"


class RevisionCanonicalizationError(ValueError):
    """A definition contained values that cannot be canonically hashed
    (§6.5: reject rather than silently coerce via default=str)."""


def _reject_unencodable(value, path="$"):
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_unencodable(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_unencodable(v, f"{path}[{i}]")
    elif isinstance(value, (set, frozenset, bytes, object)) and not isinstance(
            value, (str, int, float, bool, type(None))):
        raise RevisionCanonicalizationError(
            f"Unsupported value at {path}: {type(value).__name__}")


def _canonical_operation(tool: dict) -> dict:
    """The immutable execution-relevant slice of one operation (§6.5)."""
    op = {k: v for k, v in (tool.get("operation") or {}).items()
          if k not in _OPERATION_EXCLUDED}
    return {
        "name": tool.get("tool_name"),
        "operation": op,
        "schema": tool.get("input_schema") or {},
        "risk": tool.get("risk_level"),
        "version": tool.get("mcp_version"),
    }


def canonical_definition(manifest: dict, tools: list) -> dict:
    """The canonical immutable definition: operations sorted by stable
    identity, keys sorted at hash time, mutable evidence excluded."""
    ops = sorted((_canonical_operation(t) for t in tools),
                 key=lambda o: (o.get("name") or "", json.dumps(
                     o.get("operation") or {}, sort_keys=True, default=str)))
    return {
        "manifest": {k: manifest[k] for k in MANIFEST_FIELDS if k in manifest},
        "operations": ops,
        "digest_format": _DIGEST_FORMAT,
    }


def canonical_definition_digest(manifest: dict, tools: list) -> str:
    definition = canonical_definition(manifest, tools)
    encoded = json.dumps(definition, sort_keys=True, default=str)
    _reject_unencodable(definition)
    return hashlib.sha256(encoded.encode()).hexdigest()


async def checkpoint(repo, mcp_id):
    manifest = await repo.get_mcp(mcp_id)
    if not manifest:
        raise ValueError('Connector not found')
    if manifest.get('transport') != 'internal':
        raise ValueError('Revision restore currently supports internal connectors only')
    tools = await repo.get_cached_tools(mcp_id=mcp_id)
    definition = canonical_definition(manifest, tools)
    digest = canonical_definition_digest(manifest, tools)
    # §6.5: the revision id derives from the canonical definition digest —
    # NOT from a hash of its own storage envelope (no circularity), and the
    # operation-level digests stay independent via tool_validation.
    encoded = json.dumps(definition, sort_keys=True, default=str)
    if len(encoded.encode()) > 800_000:
        raise ValueError('Connector definition exceeds revision storage limit')
    revision = {**definition,
                'tools': tools,   # full tools stored for restore; identity above
                'definition_digest': digest,
                'revision_id': digest,
                'created_at': datetime.now(timezone.utc).isoformat()}
    await repo.save_revision(mcp_id, revision)
    return revision


async def restore_copy(repo, mcp_id, revision_id, owner):
    """§6.15: restore-as-copy — a DISABLED, credential-free draft. Copied
    execution evidence (`_validation`) is stripped explicitly: a restored
    draft requires fresh eligibility evaluation and never inherits the
    original's authority."""
    revision = await repo.get_revision(mcp_id, revision_id)
    if not revision:
        raise ValueError('Revision not found')
    new_id = str(uuid.uuid4())
    manifest = {
        **revision['manifest'], 'mcp_id': new_id, 'owner': owner,
        'name': revision['manifest']['name'] + ' (restored)',
        'endpoint': f'internal://openapi/{new_id}',
        'is_enabled': False, 'state': 'DRAFT', 'health': 'UNKNOWN',
        'trust_tier': 'pending_review', 'auth': {'type': 'NONE'},
    }
    # §6.15: strip copied execution evidence — restore preserves the
    # definition, never the validation authority.
    tools = []
    for tool in revision.get('tools') or revision['tools']:
        t = {**tool, 'mcp_id': new_id, 'mcp_version': revision_id}
        op = t.get("operation") or {}
        if "_validation" in op:
            t["operation"] = {k: v for k, v in op.items() if k != "_validation"}
        tools.append(t)
    await repo.register_mcp(manifest)
    try:
        await repo.cache_tools(new_id, tools)
        await checkpoint(repo, new_id)
    except Exception:
        await repo.delete_mcp(new_id)
        raise
    return new_id
