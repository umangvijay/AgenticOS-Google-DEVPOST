"""Phase 6 §6.22 — conservative migration for existing connectors.

Run once (idempotent): computes canonical revision identities for existing
connectors, preserves legacy test results as historical records, marks
missing provenance as `unknown`, and produces a migration report. Legacy
`verified` trust labels are NOT converted into per-operation evidence.

Idempotency: re-running produces no new revisions for unchanged definitions
(revision ids are content-derived) and re-marks only rows still missing
fields. Secrets never enter revision history (only references).
"""
import json
import logging
from datetime import datetime, timezone

from backend.services.connector_revisions import checkpoint
from backend.services.tool_validation import validation_provenance

logger = logging.getLogger(__name__)


async def migrate_connectors(repo, limit: int = 500) -> dict:
    report = {"connectors": 0, "revisions_created": 0, "provenance_unknown": 0,
              "legacy_probes_preserved": 0, "already_migrated": 0, "errors": []}
    mcps = await repo.list_mcps()
    for mcp in mcps[:limit]:
        mcp_id = mcp.get("mcp_id")
        if not mcp_id or mcp.get("transport") != "internal":
            continue
        report["connectors"] += 1
        try:
            revisions_before = len(await repo.list_revisions(mcp_id))
            await checkpoint(repo, mcp_id)
            revisions_after = len(await repo.list_revisions(mcp_id))
            if revisions_after > revisions_before:
                report["revisions_created"] += 1
            else:
                report["already_migrated"] += 1

            tools = await repo.get_cached_tools(mcp_id=mcp_id)
            changed = False
            for t in tools:
                evidence = (t.get("operation") or {}).get("_validation") or {}
                if not evidence.get("provenance"):
                    # §6.22: missing provenance becomes unknown; legacy probe
                    # results are PRESERVED as historical records (status and
                    # timestamps stay), never converted into per-op evidence.
                    if evidence.get("kind") == "read_probe":
                        report["legacy_probes_preserved"] += 1
                    evidence["provenance"] = "unknown"
                    t["operation"]["_validation"] = evidence
                    changed = True
                    report["provenance_unknown"] += 1
            if changed:
                await repo.cache_tools(mcp_id, tools)
        except Exception as exc:
            report["errors"].append({"mcp_id": mcp_id, "error": str(exc)})
            logger.warning("Migration failed for %s: %s", mcp_id, exc)
    report["migrated_at"] = datetime.now(timezone.utc).isoformat()
    logger.info("Phase 6 migration report: %s", report)
    return report
