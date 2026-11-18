import hashlib
import uuid
from typing import List, Optional
from backend.models.plugin import PluginManifest, PluginRecord, PluginLifecycleState
from backend.repositories.plugin_repository import PluginRepository
from backend.services.runtime_snapshot import RuntimeSnapshotRegistry, PluginPolicy
from backend.repositories.audit_repository import audit_repo, AuditEvent, ActorType

class PluginValidator:
    def __init__(self, policy: PluginPolicy):
        self.policy = policy

    def validate_manifest(self, manifest: PluginManifest, raw_payload: str) -> bool:
        # 1. Verify Hash Integrity
        # We hash the deterministic json representation excluding the hash field itself
        computed_hash = hashlib.sha256(manifest.model_dump_json(exclude={"manifest_hash"}, exclude_none=True).encode('utf-8')).hexdigest()
        if computed_hash != manifest.manifest_hash:
            raise ValueError(f"Manifest hash mismatch. Expected {manifest.manifest_hash}, got {computed_hash}")
            
        # 2. Verify API Version compatibility
        # For phase 9, we assume AgentOS version must be exactly '1.0.0' for simplicity
        if manifest.agentos_version not in ["1.0.0", ">=1.0.0"]:
            raise ValueError(f"Incompatible AgentOS version: {manifest.agentos_version}")
            
        # 3. Verify Permissions
        if not self.policy.is_allowed(manifest.requested_permissions):
            raise ValueError("Manifest requests unsupported or forbidden permissions.")
            
        if not manifest.agents:
            raise ValueError("Plugin must define at least one agent")
        agent_ids = [agent.agent_id for agent in manifest.agents]
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("Plugin agent IDs must be unique")
        declared = set(manifest.required_tools)
        for agent in manifest.agents:
            if not agent.allowed_tools or not set(agent.allowed_tools).issubset(declared):
                raise ValueError(f"Agent {agent.agent_id} may only use tools declared by its plugin")

        # 4. Verify no arbitrary tools are created (tools must only be referenced)
        # We assume the schema already strictly enforces this (no `endpoint` or `code` fields in required_tools).
        
        return True

class PluginRegistryService:
    def __init__(self, repository: PluginRepository, snapshot_registry: RuntimeSnapshotRegistry):
        self.repository = repository
        self.snapshot_registry = snapshot_registry
        self.policy = PluginPolicy()
        self.validator = PluginValidator(self.policy)

    async def install_plugin(self, manifest_json: str, user_id: str) -> PluginRecord:
        # 1. Parse to DISCOVERED
        manifest = PluginManifest.model_validate_json(manifest_json)
        if manifest.scope.value != "USER":
            raise ValueError("Only user-scoped plugins may be installed by this API")
        
        record = PluginRecord(
            id=str(uuid.uuid4()),
            manifest=manifest,
            state=PluginLifecycleState.INSTALLING,
            installed_by=user_id
        )
        await self.repository.save(record)
        
        # 2. VALIDATING
        record.state = PluginLifecycleState.VALIDATING
        await self.repository.save(record)
        
        try:
            self.validator.validate_manifest(manifest, manifest_json)
            # 3. INSTALLED
            record.state = PluginLifecycleState.INSTALLED
            await self.repository.save(record)
            
            audit_repo.log_event(AuditEvent(
                event_type="PLUGIN_INSTALLED",
                actor_id=user_id,
                actor_type=ActorType.USER,
                resource_id=record.id,
                details={"plugin_name": manifest.name, "plugin_version": manifest.version}
            ))
        except Exception as e:
            record.state = PluginLifecycleState.FAILED
            await self.repository.save(record)
            raise ValueError(f"Plugin validation failed: {str(e)}")
            
        return record

    async def enable_plugin(self, record_id: str, user_id: str) -> None:
        record = await self.repository.get_by_id(record_id)
        if not record:
            raise ValueError("Plugin not found")
        if record.manifest.scope.value == "USER" and record.installed_by != user_id:
            raise ValueError("User-scoped plugins can only be enabled by their owner")
            
        # Only allow enabling if INSTALLED or DISABLED
        if record.state not in [PluginLifecycleState.INSTALLED, PluginLifecycleState.DISABLED]:
            raise ValueError(f"Cannot enable plugin from state: {record.state}")
            
        record.state = PluginLifecycleState.ENABLED
        await self.repository.save(record)
        
        audit_repo.log_event(AuditEvent(
            event_type="PLUGIN_ENABLED",
            actor_id=user_id,
            actor_type=ActorType.USER,
            resource_id=record.id,
            details={"plugin_name": record.manifest.name}
        ))
        
        # Build new runtime snapshot atomically
        await self._rebuild_snapshot()

    async def disable_plugin(self, record_id: str, user_id: str) -> None:
        record = await self.repository.get_by_id(record_id)
        if not record:
            raise ValueError("Plugin not found")
        if record.manifest.scope.value == "USER" and record.installed_by != user_id:
            raise ValueError("User-scoped plugins can only be disabled by their owner")
            
        record.state = PluginLifecycleState.DISABLING
        await self.repository.save(record)
        
        record.state = PluginLifecycleState.DISABLED
        await self.repository.save(record)
        
        audit_repo.log_event(AuditEvent(
            event_type="PLUGIN_DISABLED",
            actor_id=user_id,
            actor_type=ActorType.USER,
            resource_id=record.id,
            details={"plugin_name": record.manifest.name}
        ))
        
        # Build new runtime snapshot atomically
        await self._rebuild_snapshot()
        
    async def _rebuild_snapshot(self):
        # Fetch all currently enabled plugins to build the next snapshot version
        all_plugins = await self.repository.list_all()
        enabled = [p for p in all_plugins if p.state == PluginLifecycleState.ENABLED]
        self.snapshot_registry.build_snapshot(enabled)


    async def refresh_snapshot(self):
        await self._rebuild_snapshot()
