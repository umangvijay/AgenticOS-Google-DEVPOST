"""
AgentOS — Repository Factory

The ONE place that picks implementations based on STORAGE_BACKEND.
Nothing else in the codebase should import concrete repository classes directly.

Usage:
    from backend.repositories.factory import RepositoryFactory
    
    factory = RepositoryFactory()
    await factory.initialize()
    
    user_repo = factory.user_repo
    workflow_repo = factory.workflow_repo
    ...
"""

import logging
from backend.config.settings import settings

logger = logging.getLogger(__name__)


class RepositoryFactory:
    """
    Creates and holds all repository instances.
    
    STORAGE_BACKEND='sqlite'    → SQLite implementations (local dev)
    STORAGE_BACKEND='firestore' → Firestore implementations (Google Cloud)
    """

    def __init__(self):
        self._initialized = False

        # All repository instances — populated by initialize()
        self.user_repo = None
        self.workflow_repo = None
        self.mcp_repo = None
        self.memory_repo = None
        self.secrets_repo = None
        self.schedule_repo = None
        self.refresh_token_repo = None
        self.token_denylist_repo = None
        self.notification_repo = None
        self.settings_repo = None
        self.idempotency_repo = None
        self.audit_repo = None
        self.plugin_repo = None
        self.adaptive_policy_repo = None
        self.agent_task_repo = None
        self.api_keys_repo = None
        self.org_repo = None
        self.content_repo = None
        self.feedback_repo = None
        self.approval_repo = None

        # Infrastructure
        self.message_bus = None
        self.db_manager = None  # Only for SQLite

    async def _select_message_bus(self):
        """§21.4: explicit transport selection, independent of storage.
        'nats' requires a reachable server — a failed connect raises
        (bounded by the adapter's startup timeout) rather than silently
        degrading to the in-process bus (work would be silently
        non-durable across processes)."""
        transport = settings.MESSAGE_BUS_TRANSPORT.lower()
        if transport == "nats":
            from backend.repositories.nats_message_bus import NatsMessageBus
            bus = NatsMessageBus(settings.NATS_SERVERS.split(","))
            connected = await bus.connect()
            if not connected:
                raise RuntimeError(
                    "MESSAGE_BUS_TRANSPORT=nats but no NATS server is "
                    f"reachable at {settings.NATS_SERVERS} — start one with "
                    "`docker compose --profile core up nats` or unset the "
                    "transport for in-process delivery")
            return bus
        if transport == "pubsub":
            from backend.repositories.pubsub_message_bus import PubSubMessageBus
            return PubSubMessageBus()
        from backend.repositories.in_memory_message_bus import InMemoryMessageBus
        return InMemoryMessageBus()

    async def initialize(self) -> None:
        """Initialize all repositories based on STORAGE_BACKEND config."""
        backend = settings.STORAGE_BACKEND.lower()
        logger.info(f"Initializing repositories with backend: {backend}")

        if backend == "firestore":
            await self._init_firestore()
        elif backend == "sqlite":
            await self._init_sqlite()
        elif backend == "postgres":
            await self._init_postgres()
        else:
            raise ValueError(
                f"Unknown STORAGE_BACKEND: '{backend}'. Must be 'sqlite', "
                "'firestore', or 'postgres'.")

        from backend.services.runner_bridge import configure_runner
        self._managed_runner = await configure_runner()
        from backend.services.agent_authority import (
            set_authorization_provider, set_parent_owner_provider)
        set_authorization_provider(self._specialist_account)
        set_parent_owner_provider(self._parent_owner)
        self._initialized = True
        logger.info(f"All repositories initialized ({backend})")

    async def _parent_owner(self, parent_kind: str, parent_id: str):
        """§27.2: the persisted owner of the parent work — a run ID
        identifies work, never an actor; the derived principal comes
        from the run's recorded owner or nothing."""
        if parent_kind != "run" or not parent_id:
            return None
        run = await self.workflow_repo.get_run(parent_id)
        if not run:
            return None
        return run.get("user_id") or None

    async def _specialist_account(self, principal_id: str):
        # The two typed runners handle evidence and test proposals. The principal
        # is the workflow owner. A storage error must stop protected work.
        record = await self.user_repo.get_by_id(principal_id)
        return {"user_active": bool(record and record.get("is_active", True))}

    async def _init_sqlite(self) -> None:
        """Initialize SQLite repositories."""
        from backend.repositories.sqlite.database import DatabaseManager
        from backend.repositories.sqlite.user_repository import SQLiteUserRepository
        from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
        from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
        from backend.repositories.sqlite.memory_repository import SQLiteMemoryRepository
        from backend.repositories.sqlite.secrets_repository import SQLiteSecretsRepository
        from backend.repositories.sqlite.schedule_repository import SQLiteScheduleRepository
        from backend.repositories.sqlite.refresh_token_repository import SQLiteRefreshTokenRepository
        from backend.repositories.sqlite.token_denylist_repository import SQLiteTokenDenylistRepository
        from backend.repositories.sqlite.adaptive_policy_repository import SQLiteAdaptivePolicyRepository
        from backend.repositories.sqlite.api_keys_repository import SQLiteApiKeysRepository
        from backend.repositories.sqlite.org_repository import SQLiteOrgRepository
        from backend.repositories.sqlite.notification_repository import SQLiteNotificationRepository
        from backend.repositories.sqlite.settings_repository import SQLiteSettingsRepository
        from backend.repositories.sqlite.idempotency_repository import SQLiteIdempotencyRepository
        from backend.repositories.sqlite.audit_repository import SQLiteAuditRepository
        from backend.repositories.plugin_repository import SQLitePluginRepository
        from backend.repositories.in_memory_message_bus import InMemoryMessageBus

        # Initialize the database
        self.db_manager = DatabaseManager(settings.SQLITE_DB_PATH)
        await self.db_manager.initialize()

        # Create all repository instances with the shared db manager
        self.user_repo = SQLiteUserRepository(self.db_manager)
        self.workflow_repo = SQLiteWorkflowRepository(self.db_manager)
        self.mcp_repo = SQLiteMCPRepository(self.db_manager)
        self.memory_repo = SQLiteMemoryRepository(self.db_manager)
        self.secrets_repo = SQLiteSecretsRepository(self.db_manager)
        self.schedule_repo = SQLiteScheduleRepository(self.db_manager)
        self.refresh_token_repo = SQLiteRefreshTokenRepository(self.db_manager)
        self.token_denylist_repo = SQLiteTokenDenylistRepository(self.db_manager)
        self.notification_repo = SQLiteNotificationRepository(self.db_manager)
        self.settings_repo = SQLiteSettingsRepository(self.db_manager)
        self.idempotency_repo = SQLiteIdempotencyRepository(self.db_manager)
        self.audit_repo = SQLiteAuditRepository(self.db_manager)
        self.plugin_repo = SQLitePluginRepository(self.db_manager)
        self.adaptive_policy_repo = SQLiteAdaptivePolicyRepository(self.db_manager)
        from backend.repositories.sqlite.adaptive_policy_repository import (
            set_adaptive_policy_repo)
        set_adaptive_policy_repo(self.adaptive_policy_repo)
        self.api_keys_repo = SQLiteApiKeysRepository(self.db_manager)
        self.org_repo = SQLiteOrgRepository(self.db_manager)
        from backend.repositories.content_repository import SQLiteContentRepository
        self.content_repo = SQLiteContentRepository(self.db_manager)
        from backend.repositories.sqlite.feedback_repository import SQLiteFeedbackRepository
        self.feedback_repo = SQLiteFeedbackRepository(self.db_manager)
        from backend.repositories.usage_repository import SQLiteUsageRepository, set_usage_repo
        self.usage_repo = SQLiteUsageRepository(self.db_manager)
        set_usage_repo(self.usage_repo)
        from backend.services.budget_service import set_default_settings_repo
        set_default_settings_repo(self.settings_repo)
        from backend.repositories.command_repository import SQLiteCommandRepository
        from backend.services.command_service import set_command_repo
        self.command_repo = SQLiteCommandRepository(self.db_manager)
        set_command_repo(self.command_repo)
        from backend.repositories.wait_repository import SQLiteWaitRepository, set_wait_repo
        self.wait_repo = SQLiteWaitRepository(self.db_manager)
        set_wait_repo(self.wait_repo)
        # Phase 5 §5.4: the effect ledger (one source of truth for external actions).
        from backend.repositories.effect_repository import SQLiteEffectRepository, set_effect_repo
        self.effect_repo = SQLiteEffectRepository(self.db_manager)
        set_effect_repo(self.effect_repo)
        # Phase 8 §8.5: durable browser-session metadata.
        from backend.repositories.browser_session_repository import (
            BrowserSessionRepository, set_browser_session_repo,
        )
        self.browser_session_repo = BrowserSessionRepository(self.db_manager)
        set_browser_session_repo(self.browser_session_repo)
        # Phase 10 §10.5: durable execution-step records.
        from backend.repositories.execution_step_repository import (
            ExecutionStepRepository, set_execution_step_repo,
        )
        self.execution_step_repo = ExecutionStepRepository(self.db_manager)
        set_execution_step_repo(self.execution_step_repo)
        # Phase 11 §11.2: durable recovery episodes.
        from backend.repositories.recovery_episode_repository import (
            RecoveryEpisodeRepository, set_recovery_episode_repo,
        )
        self.recovery_episode_repo = RecoveryEpisodeRepository(self.db_manager)
        set_recovery_episode_repo(self.recovery_episode_repo)
        # Phase 12: durable evaluation records.
        from backend.repositories.evaluation_repository import (
            EvaluationRepository, set_evaluation_repo,
        )
        self.evaluation_repo = EvaluationRepository(self.db_manager)
        set_evaluation_repo(self.evaluation_repo)
        # Phase 13: controlled self-improvement experiments.
        from backend.repositories.experiment_repository import (
            ExperimentRepository, set_experiment_repo,
        )
        self.experiment_repo = ExperimentRepository(self.db_manager)
        set_experiment_repo(self.experiment_repo)
        # Phase 13 §13.5: versioned prompt store + policy snapshots.
        from backend.services.prompt_versions import (
            PromptVersionStore, PolicySnapshotStore,
            set_prompt_version_store, set_policy_snapshot_store,
        )
        self.prompt_version_store = PromptVersionStore(self.db_manager)
        self.policy_snapshot_store = PolicySnapshotStore(self.db_manager)
        set_prompt_version_store(self.prompt_version_store)
        set_policy_snapshot_store(self.policy_snapshot_store)
        # Phase 14: the routing decision log (contextual bandit data).
        from backend.repositories.routing_repository import (
            RoutingDecisionRepository, set_routing_repo,
        )
        self.routing_repo = RoutingDecisionRepository(self.db_manager)
        set_routing_repo(self.routing_repo)
        # Phase 15: agent task contracts (bounded delegation).
        from backend.repositories.agent_task_repository import (
            AgentTaskRepository, set_agent_task_repo,
        )
        self.agent_task_repo = AgentTaskRepository(self.db_manager)
        set_agent_task_repo(self.agent_task_repo)
        # Phase 21: versioned workflow templates.
        from backend.services.workflow_templates import WorkflowTemplateService
        self.workflow_template_service = WorkflowTemplateService(self.db_manager)
        # Phase 12 §13.2: durable evaluation jobs.
        from backend.repositories.evaluation_job_repository import (
            EvaluationJobRepository, set_evaluation_job_repo,
        )
        self.evaluation_job_repo = EvaluationJobRepository(self.db_manager)
        set_evaluation_job_repo(self.evaluation_job_repo)
        # Phase 13 §13.13: durable experiment comparisons.
        from backend.services.experiment_comparison import (
            ExperimentComparisonRepository, set_comparison_repo,
        )
        self.comparison_repo = ExperimentComparisonRepository(self.db_manager)
        set_comparison_repo(self.comparison_repo)
        # Phase 16: memory lifecycle over the SQLite memory repo.
        from backend.services.memory_lifecycle import MemoryLifecycleService
        self.memory_lifecycle = MemoryLifecycleService(self.memory_repo)
        # Phase 17 §17.6: the durable deliverable contract (review gap:
        # the repository must be FACTORY-INITIALIZED, not test-only).
        from backend.repositories.deliverable_repository import (
            DeliverableRepository, set_deliverable_repo,
        )
        self.deliverable_repo = DeliverableRepository(self.db_manager)
        set_deliverable_repo(self.deliverable_repo)
        from backend.services.memory_workflows import DependencyMap
        self.memory_dependency_map = DependencyMap()
        # §17.4 (finding C): the lifecycle service exists on BOTH stores.
        if not hasattr(self, "_memory_lifecycle_parity_wired"):
            self._memory_lifecycle_parity_wired = True
        from backend.security.shared_rate_limiter import SQLiteSharedRateRepository, set_shared_rate_repo
        self.shared_rate_repo = SQLiteSharedRateRepository(self.db_manager)
        set_shared_rate_repo(self.shared_rate_repo)
        from backend.repositories.capacity_repository import SQLiteCapacityRepository, set_capacity_repo
        self.capacity_repo = SQLiteCapacityRepository(self.db_manager)
        set_capacity_repo(self.capacity_repo)
        from backend.repositories.admission_repository import (
            SQLiteCommandAdmissionRepository, set_command_admission_repo,
        )
        self.command_admission_repo = SQLiteCommandAdmissionRepository(self.db_manager)
        set_command_admission_repo(self.command_admission_repo)
        from backend.services.resource_authorization import (
            ResourceAuthorizationService, set_resource_authorization_service,
        )
        self.resource_authorization = ResourceAuthorizationService(self.org_repo)
        set_resource_authorization_service(self.resource_authorization)

        # §21.4: transport selected by MESSAGE_BUS_TRANSPORT (in-process
        # by default; NATS connects the multi-process durable profile).
        self.message_bus = await self._select_message_bus()
        set_active_bus(self.message_bus)

    async def _init_postgres(self) -> None:
        """§21.3: the full repository inventory on PostgreSQL. The same
        services and accessors as the SQLite path; separate processes
        share the server's state."""
        from backend.repositories.postgres.database import PostgresDatabaseManager
        from backend.repositories.postgres import repositories as pgrepo

        self.db_manager = PostgresDatabaseManager(settings.POSTGRES_DSN)
        await self.db_manager.initialize()
        db = self.db_manager

        from backend.repositories.postgres.rate_repository import PostgresSharedRateRepository
        from backend.security.shared_rate_limiter import set_shared_rate_repo
        self.shared_rate_repo = PostgresSharedRateRepository(db)
        set_shared_rate_repo(self.shared_rate_repo)

        from backend.repositories.agent_task_repository import set_agent_task_repo
        from backend.repositories.postgres.agent_task_repository import PostgresAgentTaskRepository
        self.agent_task_repo = PostgresAgentTaskRepository(db)
        set_agent_task_repo(self.agent_task_repo)
        self.user_repo = pgrepo.PostgresUserRepository(db)
        from backend.repositories.postgres.workflow_repository import (
            PostgresWorkflowEngineRepository)
        self.workflow_repo = PostgresWorkflowEngineRepository(db)
        self.mcp_repo = pgrepo.PostgresMCPRepository(db)
        self.memory_repo = pgrepo.PostgresMemoryRepository(db)
        self.secrets_repo = pgrepo.PostgresSecretsRepository(db)
        self.schedule_repo = pgrepo.PostgresScheduleRepository(db)
        self.refresh_token_repo = pgrepo.PostgresRefreshTokenRepository(db)
        self.token_denylist_repo = pgrepo.PostgresTokenDenylistRepository(db)
        self.notification_repo = pgrepo.PostgresNotificationRepository(db)
        self.settings_repo = pgrepo.PostgresSettingsRepository(db)
        self.audit_repo = pgrepo.PostgresAuditRepository(db)
        self.org_repo = pgrepo.PostgresOrgRepository(db)
        from backend.repositories.postgres.command_repository import (
            PostgresCommandRepositoryV2)
        self.command_repo = PostgresCommandRepositoryV2(db)
        self.command_repo.db = db
        self.effect_repo = pgrepo.PostgresEffectRepository(db)
        self.browser_session_repo = pgrepo.PostgresBrowserSessionRepository(db)
        self.recovery_episode_repo = pgrepo.PostgresRecoveryEpisodeRepository(db)
        self.evaluation_repo = pgrepo.PostgresEvaluationRepository(db)
        self.deliverable_repo = pgrepo.PostgresDeliverableRepository(db)
        self.feedback_repo = pgrepo.PostgresFeedbackRepository(db)
        self.execution_step_repo = pgrepo.PostgresExecutionStepRepository(db)
        self.usage_repo = pgrepo.PostgresBudgetRepository(db)
        self.idempotency_repo = pgrepo.PostgresIdempotencyRepository(db)
        self.experiment_repo = pgrepo.PostgresExperimentRepository(db)
        self.api_keys_repo = pgrepo.PostgresApiKeysRepository(db)
        self.approval_repo = pgrepo.PostgresApprovalRepository(db)
        self.plugin_repo = pgrepo.PostgresPluginRepository(db)
        self.feedback_repo = pgrepo.PostgresFeedbackRepository(db)
        from backend.repositories.wait_repository import set_wait_repo
        self.wait_repo = pgrepo.PostgresWaitRepository(db)
        set_wait_repo(self.wait_repo)
        self.schedule_repo = pgrepo.PostgresScheduleEngineRepository(db)
        from backend.repositories.sqlite.adaptive_policy_repository import (
            set_adaptive_policy_repo)
        self.adaptive_policy_repo = pgrepo.PostgresAdaptivePolicyRepository(db)
        set_adaptive_policy_repo(self.adaptive_policy_repo)
        from backend.repositories.content_repository import (
            SQLiteContentRepository as _UnusedContent)
        self.content_repo = pgrepo.PostgresContentRepository(db)

        # Global accessors shared with the services layer.
        from backend.repositories.usage_repository import set_usage_repo
        set_usage_repo(self.usage_repo)
        from backend.services.budget_service import set_default_settings_repo
        set_default_settings_repo(self.settings_repo)
        from backend.services.command_service import set_command_repo
        set_command_repo(self.command_repo)
        from backend.repositories.effect_repository import set_effect_repo
        set_effect_repo(self.effect_repo)
        from backend.repositories.browser_session_repository import set_browser_session_repo
        set_browser_session_repo(self.browser_session_repo)
        from backend.repositories.execution_step_repository import set_execution_step_repo
        set_execution_step_repo(self.execution_step_repo)
        from backend.repositories.recovery_episode_repository import set_recovery_episode_repo
        set_recovery_episode_repo(self.recovery_episode_repo)
        from backend.repositories.evaluation_repository import set_evaluation_repo
        set_evaluation_repo(self.evaluation_repo)
        from backend.repositories.experiment_repository import set_experiment_repo
        set_experiment_repo(self.experiment_repo)
        from backend.repositories.deliverable_repository import set_deliverable_repo
        set_deliverable_repo(self.deliverable_repo)

        from backend.services.memory_lifecycle import MemoryLifecycleService
        self.memory_lifecycle = MemoryLifecycleService(self.memory_repo)
        # §21.19: DURABLE dependency map (edges in the repo, not process state).
        from backend.services.memory_workflows import DurableDependencyMap
        self.memory_dependency_map = DurableDependencyMap(self.memory_repo)

        from backend.services.resource_authorization import (
            ResourceAuthorizationService, set_resource_authorization_service)
        self.resource_authorization = ResourceAuthorizationService(self.org_repo)
        set_resource_authorization_service(self.resource_authorization)

        # §21.4: transport selection (nats by default for the postgres
        # multi-process profile; in-process only when explicitly chosen).
        self.message_bus = await self._select_message_bus()
        set_active_bus(self.message_bus)

    async def _init_firestore(self) -> None:
        """Initialize Firestore repositories. Stage 3."""
        # Validate required config
        if not settings.GOOGLE_CLOUD_PROJECT:
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT must be set when STORAGE_BACKEND=firestore"
            )

        from backend.repositories.firestore.database import FirestoreDB
        await FirestoreDB.get_client()
        
        from backend.repositories.firestore.user_repository import FirestoreUserRepository
        from backend.repositories.firestore.workflow_repository import FirestoreWorkflowRepository
        from backend.repositories.firestore.mcp_repository import FirestoreMCPRepository
        from backend.repositories.firestore.idempotency_repository import FirestoreIdempotencyRepository
        from backend.repositories.firestore.memory_repository import FirestoreMemoryRepository
        from backend.repositories.pubsub_message_bus import PubSubMessageBus
        from backend.repositories.in_memory_message_bus import InMemoryMessageBus
        
        self.user_repo = FirestoreUserRepository()
        from backend.repositories.firestore.feedback_repository import FirestoreFeedbackRepository
        self.feedback_repo = FirestoreFeedbackRepository()
        from backend.repositories.content_repository import FirestoreContentRepository
        self.content_repo = FirestoreContentRepository()
        from backend.repositories.usage_repository import FirestoreUsageRepository, set_usage_repo
        self.usage_repo = FirestoreUsageRepository()
        set_usage_repo(self.usage_repo)
        from backend.security.shared_rate_limiter import FirestoreSharedRateRepository, set_shared_rate_repo
        self.shared_rate_repo = FirestoreSharedRateRepository()
        set_shared_rate_repo(self.shared_rate_repo)
        from backend.repositories.capacity_repository import FirestoreCapacityRepository, set_capacity_repo
        self.capacity_repo = FirestoreCapacityRepository()
        set_capacity_repo(self.capacity_repo)
        from backend.repositories.admission_repository import (
            FirestoreCommandAdmissionRepository, set_command_admission_repo,
        )
        self.command_admission_repo = FirestoreCommandAdmissionRepository()
        set_command_admission_repo(self.command_admission_repo)
        from backend.services.resource_authorization import (
            ResourceAuthorizationService as _RAS, set_resource_authorization_service,
        )
        from backend.repositories.plugin_repository import AsyncFirestorePluginRepository
        self.plugin_repo = AsyncFirestorePluginRepository(await FirestoreDB.get_client())
        self.workflow_repo = FirestoreWorkflowRepository()
        self.mcp_repo = FirestoreMCPRepository()
        self.idempotency_repo = FirestoreIdempotencyRepository()
        self.memory_repo = FirestoreMemoryRepository()
        from backend.services.memory_lifecycle import MemoryLifecycleService
        self.memory_lifecycle = MemoryLifecycleService(self.memory_repo)
        # Phase 17 §17.6: the deliverable contract on Firestore too.
        from backend.repositories.firestore.deliverable_repository import (
            FirestoreDeliverableRepository,
        )
        from backend.repositories.deliverable_repository import set_deliverable_repo
        self.deliverable_repo = FirestoreDeliverableRepository()
        set_deliverable_repo(self.deliverable_repo)
        from backend.services.memory_workflows import DependencyMap
        self.memory_dependency_map = DependencyMap()

        from backend.repositories.firestore.sidecar import (
            FirestoreAuditRepository,
            FirestoreNotificationRepository,
            FirestoreRefreshTokenRepository,
            FirestoreScheduleRepository,
            FirestoreSecretsRepository,
            FirestoreSettingsRepository,
            FirestoreTokenDenylistRepository,
            FirestoreAdaptivePolicyRepository,
            FirestoreApiKeysRepository,
            FirestoreOrgRepository,
        )
        self.secrets_repo = FirestoreSecretsRepository()
        self.schedule_repo = FirestoreScheduleRepository()
        self.refresh_token_repo = FirestoreRefreshTokenRepository()
        self.token_denylist_repo = FirestoreTokenDenylistRepository()
        self.notification_repo = FirestoreNotificationRepository()
        self.settings_repo = FirestoreSettingsRepository()
        from backend.services.budget_service import set_default_settings_repo
        set_default_settings_repo(self.settings_repo)   # §4.0C (R4)
        # §4.7: Firestore parity for the command lifecycle.
        from backend.repositories.firestore.command_repository import FirestoreCommandRepository
        from backend.services.command_service import set_command_repo
        self.command_repo = FirestoreCommandRepository()
        set_command_repo(self.command_repo)
        from backend.repositories.firestore.wait_repository import FirestoreWaitRepository
        from backend.repositories.wait_repository import set_wait_repo
        self.wait_repo = FirestoreWaitRepository()
        set_wait_repo(self.wait_repo)
        # Phase 5 parity: the durable effect ledger on Firestore — the ONE
        # source of truth for intended external actions (§5.4).
        from backend.repositories.firestore.effect_repository import (
            FirestoreEffectRepository,
        )
        from backend.repositories.effect_repository import set_effect_repo
        self.effect_repo = FirestoreEffectRepository()
        set_effect_repo(self.effect_repo)
        # Phase 8/10/11 parity: durable browser sessions, execution steps,
        # and recovery episodes on Firestore — the SAME contracts as the
        # SQLite implementations.
        from backend.repositories.firestore.browser_session_repository import (
            FirestoreBrowserSessionRepository,
        )
        from backend.repositories.browser_session_repository import (
            set_browser_session_repo,
        )
        self.browser_session_repo = FirestoreBrowserSessionRepository()
        set_browser_session_repo(self.browser_session_repo)
        from backend.repositories.firestore.execution_step_repository import (
            FirestoreExecutionStepRepository,
        )
        from backend.repositories.execution_step_repository import (
            set_execution_step_repo,
        )
        self.execution_step_repo = FirestoreExecutionStepRepository()
        set_execution_step_repo(self.execution_step_repo)
        from backend.repositories.firestore.recovery_episode_repository import (
            FirestoreRecoveryEpisodeRepository,
        )
        from backend.repositories.recovery_episode_repository import (
            set_recovery_episode_repo,
        )
        self.recovery_episode_repo = FirestoreRecoveryEpisodeRepository()
        set_recovery_episode_repo(self.recovery_episode_repo)
        # Phase 12: durable evaluation records (Firestore parity).
        from backend.repositories.firestore.evaluation_repository import (
            FirestoreEvaluationRepository,
        )
        from backend.repositories.evaluation_repository import set_evaluation_repo
        self.evaluation_repo = FirestoreEvaluationRepository()
        set_evaluation_repo(self.evaluation_repo)
        # Phase 13: controlled self-improvement experiments (parity).
        from backend.repositories.firestore.experiment_repository import (
            FirestoreExperimentRepository,
        )
        from backend.repositories.experiment_repository import set_experiment_repo
        self.experiment_repo = FirestoreExperimentRepository()
        set_experiment_repo(self.experiment_repo)
        # Phase 13 §13.5: versioned prompt store + policy snapshots
        # (Firestore contract mirrors the SQLite store).
        from backend.services.prompt_versions import (
            set_prompt_version_store, set_policy_snapshot_store,
        )
        from backend.repositories.firestore.prompt_versions import (
            FirestorePromptVersionStore, FirestorePolicySnapshotStore,
        )
        self.prompt_version_store = FirestorePromptVersionStore()
        self.policy_snapshot_store = FirestorePolicySnapshotStore()
        set_prompt_version_store(self.prompt_version_store)
        set_policy_snapshot_store(self.policy_snapshot_store)
        # Phase 14: routing decision log (Firestore parity).
        from backend.repositories.firestore.routing_repository import (
            FirestoreRoutingDecisionRepository,
        )
        from backend.repositories.routing_repository import set_routing_repo
        self.routing_repo = FirestoreRoutingDecisionRepository()
        set_routing_repo(self.routing_repo)
        # Phase 15: agent task contracts (Firestore parity).
        from backend.repositories.firestore.agent_task_repository import (
            FirestoreAgentTaskRepository,
        )
        from backend.repositories.agent_task_repository import set_agent_task_repo
        self.agent_task_repo = FirestoreAgentTaskRepository()
        set_agent_task_repo(self.agent_task_repo)
        # Phase 13 §13.2: durable evaluation jobs (Firestore parity).
        from backend.repositories.firestore.evaluation_job_repository import (
            FirestoreEvaluationJobRepository,
        )
        from backend.repositories.evaluation_job_repository import (
            set_evaluation_job_repo,
        )
        self.evaluation_job_repo = FirestoreEvaluationJobRepository()
        set_evaluation_job_repo(self.evaluation_job_repo)
        self.audit_repo = FirestoreAuditRepository()
        self.adaptive_policy_repo = FirestoreAdaptivePolicyRepository()
        from backend.repositories.sqlite.adaptive_policy_repository import (
            set_adaptive_policy_repo)
        set_adaptive_policy_repo(self.adaptive_policy_repo)
        self.api_keys_repo = FirestoreApiKeysRepository()
        self.org_repo = FirestoreOrgRepository()
        self.resource_authorization = _RAS(self.org_repo)
        set_resource_authorization_service(self.resource_authorization)

        # Do not silently use a process-local queue in production: the API and
        # worker pool are separate processes, so that fallback strands tasks.
        try:
            self.message_bus = PubSubMessageBus()
        except Exception as exc:
            if settings.APP_ENV.lower() == "production":
                raise RuntimeError("Pub/Sub is required in production; refusing an in-memory fallback") from exc
            logger.warning("Pub/Sub unavailable in development; using the local in-memory bus (%s)", type(exc).__name__)
            self.message_bus = InMemoryMessageBus()
        set_active_bus(self.message_bus)

        logger.info("Firestore repositories initialized")

    async def shutdown(self) -> None:
        """Clean shutdown of all resources. Also clears the module-level
        singletons this factory registered (shared limiter, usage, capacity,
        command admission) so a closed repository can never be reached by a
        later caller through a stale global."""
        managed_runner = getattr(self, "_managed_runner", None)
        if managed_runner is not None:
            from backend.services.isolated_runner import get_runner, set_runner
            await managed_runner.shutdown()
            if get_runner() is managed_runner:
                set_runner(None)
            self._managed_runner = None
        from backend.security.shared_rate_limiter import set_shared_rate_repo
        from backend.repositories.capacity_repository import set_capacity_repo
        from backend.repositories.admission_repository import set_command_admission_repo
        from backend.repositories.usage_repository import set_usage_repo
        set_shared_rate_repo(None)
        set_capacity_repo(None)
        set_command_admission_repo(None)
        set_usage_repo(None)
        from backend.repositories.wait_repository import get_wait_repo, set_wait_repo
        if get_wait_repo() is getattr(self, "wait_repo", None):
            set_wait_repo(None)
        from backend.repositories.agent_task_repository import get_agent_task_repo, set_agent_task_repo
        from backend.services.agent_authority import get_authorization_provider, set_authorization_provider
        if get_agent_task_repo() is self.agent_task_repo:
            set_agent_task_repo(None)
        if get_authorization_provider() == self._specialist_account:
            set_authorization_provider(None)
        if self.message_bus and hasattr(self.message_bus, "close"):
            await self.message_bus.close()
        if self.db_manager:
            await self.db_manager.close()
        self._initialized = False
        logger.info("Repository factory shut down")

    def ensure_initialized(self) -> None:
        """Raise if not initialized. Call in health checks."""
        if not self._initialized:
            raise RuntimeError("RepositoryFactory not initialized. Call await factory.initialize() first.")


_active_bus = None


def set_active_bus(bus) -> None:
    global _active_bus
    _active_bus = bus


def get_dispatcher_bus():
    """The initialized message bus for the outbox dispatcher (avoids a
    factory→worker import cycle)."""
    return _active_bus
