"""Specialist availability must agree with the published execution path."""
import importlib

from backend.models.agent_task import AgentRole
from backend.models.browser_observation import BrowserObservation, ObservedElement
from backend.services.agent_task_contracts import SPECIALIST_REGISTRY
from backend.services.delegation_policy import eligible_specialist_actions, specialist_inventory


def test_dom_resolver_inventory_names_the_executable_service():
    entry = next(row for row in specialist_inventory() if row["role"] == "dom_resolver")
    assert entry["callable"] and not entry["placeholder"]
    assert "dom_resolver" in eligible_specialist_actions(AgentRole.DOM_RESOLVER)
    _, module, name = entry["execution_path"].split(":")
    resolver = getattr(importlib.import_module(module), name)
    observation = BrowserObservation(
        observation_id="inventory-observation", session_id="inventory-session",
        owner_generation=1, controller_generation=1, page_id="page",
        sanitized_url="https://fixture.example/", origin="https://fixture.example",
        elements=[ObservedElement(target_reference="button-1", semantic_role="button",
                                  accessible_name="Details")])
    resolved, failure, _ = resolver(observation, target_ref="button-1")
    assert failure is None and resolved.element.target_reference == "button-1"


def test_missing_execution_path_is_placeholder_and_cannot_be_selected(monkeypatch):
    disconnected = dict(SPECIALIST_REGISTRY[AgentRole.DOM_RESOLVER], runner="", service="")
    monkeypatch.setitem(SPECIALIST_REGISTRY, AgentRole.DOM_RESOLVER, disconnected)
    entry = next(row for row in specialist_inventory() if row["role"] == "dom_resolver")
    assert entry["placeholder"] and not entry["callable"]
    assert entry["execution_path"] is None
    assert "dom_resolver" not in eligible_specialist_actions(AgentRole.DOM_RESOLVER)
