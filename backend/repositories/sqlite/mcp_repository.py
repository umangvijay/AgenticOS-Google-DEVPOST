"""
AgentOS — SQLite MCP Repository

Register, list, cache tools, manage trust tiers and health status.
"""

import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from backend.repositories.base import BaseMCPRepository
from backend.repositories.sqlite.database import DatabaseManager

logger = logging.getLogger(__name__)


class SQLiteMCPRepository(BaseMCPRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def register_mcp(self, manifest_data: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        m = manifest_data
        cursor = await self.db.execute(
            """
            INSERT INTO mcps (
                mcp_id, name, version, endpoint, transport, auth_json, scopes,
                health, health_updated_at, state, spec_hash, spec_version,
                source_uri, built_at, owner, is_enabled, created_at, updated_at,
                description, trust_tier, source_type, spec_json, is_public,
                artifact_type, test_coverage_json, capabilities_omitted, spec_provenance, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mcp_id) DO UPDATE SET
                name = excluded.name,
                version = excluded.version,
                endpoint = excluded.endpoint,
                transport = excluded.transport,
                auth_json = excluded.auth_json,
                scopes = excluded.scopes,
                state = excluded.state,
                spec_hash = excluded.spec_hash,
                spec_version = excluded.spec_version,
                source_uri = excluded.source_uri,
                built_at = excluded.built_at,
                owner = excluded.owner,
                is_enabled = excluded.is_enabled,
                description = excluded.description,
                trust_tier = excluded.trust_tier,
                source_type = excluded.source_type,
                spec_json = excluded.spec_json,
                is_public = excluded.is_public,
                artifact_type = excluded.artifact_type,
                test_coverage_json = excluded.test_coverage_json,
                capabilities_omitted = excluded.capabilities_omitted,
                spec_provenance = excluded.spec_provenance,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            WHERE mcps.owner = excluded.owner
            """,
            (
                m["mcp_id"],
                m["name"],
                m.get("version", "1.0.0"),
                m.get("endpoint", f"internal://openapi/{m['mcp_id']}"),
                m.get("transport", "internal"),
                json.dumps(m.get("auth", {})),
                json.dumps(m.get("scopes", [])),
                m.get("health", "UNKNOWN"),
                m.get("health_updated_at"),
                m.get("state", "DRAFT"),
                m.get("spec_hash"),
                m.get("spec_version"),
                m.get("source_uri"),
                m.get("built_at"),
                m.get("owner", "system"),
                1 if m.get("is_enabled", True) else 0,
                m.get("created_at", now),
                now,
                m.get("description", ""),
                m.get("trust_tier", "pending_review"),
                m.get("source_type", "openapi"),
                m.get("spec_json"),
                1 if m.get("is_public", False) else 0,
                m.get("artifact_type", "adapter"),
                json.dumps(m.get("test_coverage") or {}),
                int(m.get("capabilities_omitted") or 0),
                m.get("spec_provenance", "official_spec"),
                json.dumps(m.get("metadata") or {}),
            ),
        )
        await self.db.commit()
        if not cursor.rowcount:
            raise PermissionError("Connector belongs to another owner")

    async def get_mcp(self, mcp_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM mcps WHERE mcp_id = ?", (mcp_id,))
        return self._row_to_dict(row) if row else None

    async def list_mcps(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if user_id:
            rows = await self.db.fetch_all(
                "SELECT * FROM mcps WHERE owner = ? OR owner = 'system' ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            rows = await self.db.fetch_all("SELECT * FROM mcps ORDER BY created_at DESC")
        return [self._row_to_dict(r) for r in rows]

    async def update_mcp_health(self, mcp_id: str, status: str, timestamp: datetime) -> None:
        await self.db.execute(
            "UPDATE mcps SET health = ?, health_updated_at = ?, updated_at = ? WHERE mcp_id = ?",
            (status, timestamp.isoformat(), datetime.now(timezone.utc).isoformat(), mcp_id),
        )
        await self.db.commit()

    async def set_mcp_enabled(self, mcp_id: str, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE mcps SET is_enabled = ?, metadata_json = json_remove(metadata_json, '$.pending_publication_token'), updated_at = ? WHERE mcp_id = ?",
            (1 if enabled else 0, datetime.now(timezone.utc).isoformat(), mcp_id),
        )
        await self.db.commit()

    async def publish_managed_candidate(self, mcp_id, *, owner, revision_id, publication_token, metadata):
        cursor = await self.db.execute(
            "UPDATE mcps SET is_enabled=1, trust_tier='verified', metadata_json=?, updated_at=? "
            "WHERE mcp_id=? AND owner=? AND active_revision_id=? AND is_enabled=0 "
            "AND json_extract(metadata_json, '$.pending_publication_token')=?",
            (json.dumps(metadata), datetime.now(timezone.utc).isoformat(), mcp_id,
             owner, revision_id, publication_token))
        await self.db.commit()
        return cursor.rowcount == 1

    async def update_mcp_auth(self, mcp_id: str, auth: Dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE mcps SET auth_json = ?, updated_at = ? WHERE mcp_id = ?",
            (json.dumps(auth), datetime.now(timezone.utc).isoformat(), mcp_id),
        )
        await self.db.commit()

    async def update_mcp_state(self, mcp_id: str, state: str) -> None:
        await self.db.execute(
            "UPDATE mcps SET state = ?, updated_at = ? WHERE mcp_id = ?",
            (state, datetime.now(timezone.utc).isoformat(), mcp_id),
        )
        await self.db.commit()

    async def list_public_mcps(self) -> List[Dict[str, Any]]:
        """Shared catalog: every published integration, for any authenticated user."""
        rows = await self.db.fetch_all(
            "SELECT * FROM mcps WHERE is_public = 1 ORDER BY updated_at DESC"
        )
        return [dict(r) for r in rows]

    async def set_public(self, mcp_id: str, is_public: bool) -> bool:
        cursor = await self.db.execute(
            "UPDATE mcps SET is_public = ?, updated_at = ? WHERE mcp_id = ?",
            (1 if is_public else 0, datetime.now(timezone.utc).isoformat(), mcp_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def delete_mcp(self, mcp_id: str) -> bool:
        conn = await self.db.connection()
        # Cascade deletes mcp_tools via FK
        cursor = await conn.execute("DELETE FROM mcps WHERE mcp_id = ?", (mcp_id,))
        await conn.commit()
        return cursor.rowcount > 0

    async def cache_tools(self, mcp_id: str, tools: List[Dict[str, Any]]) -> None:
        conn = await self.db.connection()
        # Clear existing cache for this MCP
        await conn.execute("DELETE FROM mcp_tools WHERE mcp_id = ?", (mcp_id,))
        # Insert new tools
        for tool in tools:
            auth_reqs = tool.get("auth_requirements", [])
            if auth_reqs and hasattr(auth_reqs[0], "model_dump"):
                auth_reqs = [a.model_dump() if hasattr(a, "model_dump") else a for a in auth_reqs]
            operation = tool.get("operation") or {}
            if hasattr(operation, "model_dump"):
                operation = operation.model_dump()
            risk = tool.get("risk_level", 4)
            if hasattr(risk, "value"):
                risk = risk.value
            discovered = tool.get("discovered_at") or datetime.now(timezone.utc).isoformat()
            expires = tool.get("expires_at") or datetime.now(timezone.utc).isoformat()
            if hasattr(discovered, "isoformat"):
                discovered = discovered.isoformat()
            if hasattr(expires, "isoformat"):
                expires = expires.isoformat()
            await conn.execute(
                """
                INSERT INTO mcp_tools (
                    tool_name, description, input_schema, mcp_id, mcp_version,
                    discovered_at, expires_at, auth_requirements, risk_level, operation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool.get("tool_name") or tool.get("name") or "unknown",
                    tool.get("description", ""),
                    json.dumps(tool.get("input_schema") or tool.get("inputSchema") or {}),
                    mcp_id,
                    tool.get("mcp_version", "1.0.0"),
                    discovered,
                    expires,
                    json.dumps(auth_reqs),
                    int(risk) if risk is not None else 4,
                    json.dumps(operation),
                ),
            )
        await conn.commit()

    async def get_cached_tools(self, mcp_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if mcp_id:
            rows = await self.db.fetch_all(
                "SELECT * FROM mcp_tools WHERE mcp_id = ?", (mcp_id,)
            )
        else:
            rows = await self.db.fetch_all("SELECT * FROM mcp_tools")

        results = []
        for row in rows:
            r = dict(row)
            r["input_schema"] = json.loads(r.get("input_schema") or "{}")
            r["auth_requirements"] = json.loads(r.get("auth_requirements") or "[]")
            r["operation"] = json.loads(r.get("operation_json") or "{}")
            r["name"] = r.get("tool_name")
            r["inputSchema"] = r["input_schema"]
            results.append(r)
        return results

    async def create_build(self, build: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT INTO mcp_builds (
                build_id, user_id, name, method, source, status, stage,
                logs_json, mcp_id, tools_json, error,
                origin, parent_run_id, parent_workflow_id, parent_task_id,
                wait_reason, organization_id, credential_reference,
                required_operations_json, intake_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                build["build_id"],
                build["user_id"],
                build.get("name") or "",
                build.get("method") or "url",
                build.get("source") or "",
                build.get("status", "queued"),
                build.get("stage", "queued"),
                json.dumps(build.get("logs") or []),
                build.get("mcp_id"),
                json.dumps(build.get("tools") or []),
                build.get("error"),
                build.get("origin"),
                build.get("parent_run_id"),
                build.get("parent_workflow_id"),
                build.get("parent_task_id"),
                build.get("wait_reason"),
                build.get("organization_id"),
                build.get("credential_reference"),
                json.dumps(build.get("required_operations") or []),
                json.dumps(build.get("intake") or {}),
                now,
                now,
            ),
        )
        await self.db.commit()

    async def update_build(self, build_id: str, updates: Dict[str, Any]) -> None:
        if not updates:
            return
        mapping = {
            "logs": "logs_json",
            "tools": "tools_json",
            "intake": "intake_json",
        }
        set_clauses = []
        values = []
        for key, value in updates.items():
            col = mapping.get(key, key)
            set_clauses.append(f"{col} = ?")
            if key in ("logs", "tools") or isinstance(value, (dict, list)):
                values.append(json.dumps(value))
            else:
                values.append(value)
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(build_id)
        await self.db.execute(
            f"UPDATE mcp_builds SET {', '.join(set_clauses)}, updated_at = ? WHERE build_id = ?",
            tuple(values),
        )
        await self.db.commit()

    async def get_build(self, build_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM mcp_builds WHERE build_id = ?", (build_id,))
        return self._build_row(row) if row else None

    @staticmethod
    def _build_row(row: dict) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        r = dict(row)
        r["logs"] = json.loads(r.pop("logs_json", "[]") or "[]")
        r["tools"] = json.loads(r.pop("tools_json", "[]") or "[]")
        # §13.3: the declared required operations ride with the build.
        r["required_operations"] = json.loads(
            r.pop("required_operations_json", "[]") or "[]")
        # §29.14: the application-intake record rides with the build.
        try:
            r["intake"] = json.loads(r.pop("intake_json", "{}") or "{}")
        except ValueError:
            r["intake"] = {}
        return r

    @staticmethod
    def _row_to_dict(row: dict) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        result = dict(row)
        result["auth"] = json.loads(result.pop("auth_json", None) or "{}")
        result["metadata"] = json.loads(result.pop("metadata_json", None) or "{}")
        scopes_raw = result.get("scopes") or "[]"
        if isinstance(scopes_raw, str):
            try:
                result["scopes"] = json.loads(scopes_raw)
            except json.JSONDecodeError:
                result["scopes"] = []
        elif not isinstance(scopes_raw, list):
            result["scopes"] = []
        result["is_enabled"] = bool(result.get("is_enabled", 1))
        # Connector evidence fields (audit G02/G06/G03): deserialize the
        # coverage summary; default old rows to the adapter product.
        result.setdefault("artifact_type", "adapter")
        result.setdefault("spec_provenance", "official_spec")
        coverage_raw = result.pop("test_coverage_json", None)
        if isinstance(coverage_raw, str):
            try:
                result["test_coverage"] = json.loads(coverage_raw)
            except json.JSONDecodeError:
                result["test_coverage"] = {}
        else:
            result["test_coverage"] = coverage_raw or {}
        result["capabilities_omitted"] = int(result.get("capabilities_omitted") or 0)
        return result

    async def save_validation_run(self, connector_id: str, run: dict) -> None:
        """Phase 6 §6.8: durable validation-run records (idempotent upsert)."""
        import json as _json
        await self.db.execute(
            """
            INSERT INTO connector_validation_runs
                (validation_run_id, connector_id, revision_id, operation_id,
                 operation_digest, credential_binding_id, authorization_epoch,
                 provider_account_id, environment, runtime_digest,
                 validation_profile_version, case_results_json, started_at,
                 completed_at, valid_until, artifact_references_json, status,
                 superseded_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(validation_run_id) DO UPDATE SET
                case_results_json=excluded.case_results_json,
                completed_at=excluded.completed_at,
                status=excluded.status,
                superseded_reason=excluded.superseded_reason
            """,
            (run.get("validation_run_id"), connector_id,
             run.get("revision_id"), run.get("operation_id"),
             run.get("operation_digest"), run.get("credential_binding_id"),
             run.get("authorization_epoch", "e0"),
             run.get("provider_account_id"), run.get("environment", "production"),
             run.get("runtime_digest"), run.get("validation_profile_version",
                                                "p6.read.v1"),
             _json.dumps(run.get("case_results") or []),
             run.get("started_at"), run.get("completed_at"),
             run.get("valid_until"),
             _json.dumps(run.get("artifact_references") or []),
             run.get("status", "running"), run.get("superseded_reason")))
        await self.db.commit()

    async def list_validation_runs(self, connector_id: str,
                                   limit: int = 100) -> list:
        """§13.3: the connector's validation runs — the readiness gate
        reads PASSING evidence against the CURRENT revision and credential
        context from these durable rows."""
        import json as _json
        rows = await self.db.fetch_all(
            "SELECT * FROM connector_validation_runs WHERE connector_id=? "
            "ORDER BY started_at DESC LIMIT ?", (connector_id, limit))
        out = []
        for r in rows or []:
            d = dict(r)
            d["case_results"] = _json.loads(d.pop("case_results_json", "[]") or "[]")
            d["artifact_references"] = _json.loads(
                d.pop("artifact_references_json", "[]") or "[]")
            out.append(d)
        return out

    async def save_revision(self, mcp_id: str, revision: dict) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO mcp_revisions (mcp_id, revision_id, created_at, payload_json) VALUES (?, ?, ?, ?)",
            (mcp_id, revision["revision_id"], revision["created_at"], json.dumps(revision)),
        )
        await self.db.commit()

    async def list_revisions(self, mcp_id: str) -> list:
        rows = await self.db.fetch_all(
            "SELECT payload_json FROM mcp_revisions WHERE mcp_id = ? ORDER BY created_at DESC LIMIT 100", (mcp_id,)
        )
        return [json.loads(row["payload_json"]) for row in rows]

    async def get_revision(self, mcp_id: str, revision_id: str):
        row = await self.db.fetch_one(
            "SELECT payload_json FROM mcp_revisions WHERE mcp_id = ? AND revision_id = ?", (mcp_id, revision_id)
        )
        return json.loads(row["payload_json"]) if row else None
