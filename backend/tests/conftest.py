"""Shared test fixtures.

`budget_repo` wires a real SQLite budget-accounting repository into the
process for tests that exercise paid execution paths: since Phase 2,
generation fails closed when accounting is unavailable, so provider-routing
tests must run with the ledger installed (docs/budget-accounting.md §10).
"""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import SQLiteUsageRepository, set_usage_repo


@pytest.fixture(autouse=True)
def _clear_repo_singletons():
    """Reset the module-level repository singletons (shared rate limiter,
    capacity leases, command admission, usage ledger) before EVERY test.

    Tests that build a RepositoryFactory install these globals; a later test
    that closes its database directly must never inherit a closed repo
    through a stale global — the engine and admission paths now read these
    on live request paths, so ordering-dependent pollution surfaces as
    'Database not initialized' failures that depend on run order."""
    from backend.security.shared_rate_limiter import set_shared_rate_repo
    from backend.repositories.capacity_repository import set_capacity_repo
    from backend.repositories.admission_repository import set_command_admission_repo
    from backend.services.budget_service import set_default_settings_repo
    # Phase 5–13 durable-store singletons: a test that closes its database
    # must never leak it into another test through a stale global.
    from backend.repositories.effect_repository import set_effect_repo
    from backend.repositories.execution_step_repository import set_execution_step_repo
    from backend.repositories.browser_session_repository import set_browser_session_repo
    from backend.repositories.recovery_episode_repository import set_recovery_episode_repo
    from backend.repositories.evaluation_repository import set_evaluation_repo
    from backend.repositories.evaluation_job_repository import set_evaluation_job_repo
    from backend.repositories.experiment_repository import set_experiment_repo
    from backend.repositories.routing_repository import set_routing_repo
    from backend.repositories.agent_task_repository import set_agent_task_repo
    from backend.repositories.wait_repository import set_wait_repo
    from backend.services.command_service import set_command_repo
    from backend.services.prompt_versions import (
        set_prompt_version_store, set_policy_snapshot_store)
    from backend.services.experiment_comparison import set_comparison_repo
    from backend.services.evaluation_datasets import set_dataset_repo
    phase13_resets = (
        set_effect_repo, set_execution_step_repo, set_browser_session_repo,
        set_recovery_episode_repo, set_evaluation_repo,
        set_evaluation_job_repo, set_experiment_repo, set_routing_repo,
        set_agent_task_repo, set_wait_repo, set_command_repo, set_prompt_version_store,
        set_policy_snapshot_store, set_comparison_repo, set_dataset_repo,
    )
    for reset in (set_shared_rate_repo, set_capacity_repo,
                  set_command_admission_repo, set_usage_repo,
                  set_default_settings_repo, *phase13_resets):
        reset(None)
    yield
    for reset in (set_shared_rate_repo, set_capacity_repo,
                  set_command_admission_repo, set_usage_repo,
                  set_default_settings_repo, *phase13_resets):
        reset(None)


@pytest_asyncio.fixture
async def budget_repo(tmp_path):
    manager = DatabaseManager(str(tmp_path / "budget-fixture.db"))
    await manager.initialize()
    repo = SQLiteUsageRepository(manager)
    set_usage_repo(repo)
    try:
        yield repo
    finally:
        set_usage_repo(None)
        await manager.close()


@pytest.fixture
def isolated_settings(monkeypatch):
    """Snapshot process-wide settings mutation so tests cannot leak state."""
    from backend.config.settings import settings as app_settings
    yield app_settings
