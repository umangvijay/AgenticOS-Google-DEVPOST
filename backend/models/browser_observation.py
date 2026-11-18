"""Phase 9 §9.2/§9.3: structured observation contract + typed action union.

The observation replaces the positional snapshot as the primary action
contract. Each observed element carries semantic identity (role, accessible
name, container, business identity) and locator candidates; bounding boxes
are for visualization only, never the default target identity (§9.4).

§9.3 requirement 6: bounded, SCOPED observation — never report "target
absent" merely because the target was outside the first observed batch.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


def new_observation_id() -> str:
    return f"obs_{uuid.uuid4().hex[:24]}"


class SemanticRole(str, Enum):
    BUTTON = "button"
    LINK = "link"
    TEXTBOX = "textbox"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    SELECT = "select"
    HEADING = "heading"
    ROW = "row"
    CELL = "cell"
    DIALOG = "dialog"
    FORM = "form"
    IMAGE = "image"
    TAB = "tab"
    GENERIC = "generic"


@dataclass
class ObservedElement:
    """§9.3 — one observed control with semantic identity and locator
    candidates. Sensitive values are REDACTED before model input or
    persistence (§9.18)."""
    target_reference: str                 # display ref (e.g. "e12") — NOT an auth token
    semantic_role: str                    # button/link/textbox/…
    accessible_name: str = ""
    label: str = ""
    tag: str = ""
    input_type: str = ""
    visible: bool = True
    enabled: bool = True
    editable: bool = False
    checked_or_selected: Optional[bool] = None
    frame_origin: str = ""
    frame_id: str = ""
    semantic_container: str = ""          # row INV-1042 / dialog "Confirm" / form "login"
    business_identity: str = ""           # e.g. "INV-1042"
    locator_candidates: List[Dict[str, str]] = field(default_factory=list)
    bounding_box: Optional[Dict[str, float]] = None   # visualization only

    def to_model_dict(self, max_chars: int = 160) -> Dict[str, Any]:
        """§9.18: the REDACTED view for the model — no raw values from
        password fields, no secrets."""
        safe_name = self.accessible_name[:max_chars]
        return {
            "ref": self.target_reference, "role": self.semantic_role,
            "name": safe_name, "label": self.label[:max_chars],
            "tag": self.tag, "type": self.input_type,
            "visible": self.visible, "enabled": self.enabled,
            "editable": self.editable,
            "checked": self.checked_or_selected,
            "container": self.semantic_container[:max_chars],
            "identity": self.business_identity[:max_chars],
        }


@dataclass
class BrowserObservation:
    """§9.3 — one structured observation with explicit limits and
    truncation information."""
    observation_id: str
    session_id: str
    owner_generation: int
    controller_generation: int
    page_id: str
    frame_id: str = ""
    document_generation: int = 1
    observation_sequence: int = 1
    captured_at: int = 0
    sanitized_url: str = ""
    origin: str = ""
    page_title: str = ""
    elements: List[ObservedElement] = field(default_factory=list)
    elements_total: int = 0            # total controls seen before truncation
    truncated: bool = False
    scopes_observed: List[str] = field(default_factory=list)  # which scopes ran
    max_elements: int = 120

    def elements_remaining(self) -> int:
        return max(0, self.elements_total - len(self.elements))

    def find_by_ref(self, ref: str) -> Optional[ObservedElement]:
        for e in self.elements:
            if e.target_reference == ref:
                return e
        return None

    def to_model_dict(self, max_elements: int = 60,
                      max_chars: int = 160) -> Dict[str, Any]:
        """§9.18: the redacted model input — structurally separate from
        system instructions and user intent (page content is untrusted)."""
        return {
            "observation_id": self.observation_id,
            "url": self.sanitized_url, "origin": self.origin,
            "title": self.page_title[:max_chars],
            "truncated": self.truncated,
            "elements_total": self.elements_total,
            "elements_shown": min(len(self.elements), max_elements),
            "elements_remaining": self.elements_remaining(),
            "elements": [e.to_model_dict(max_chars)
                         for e in self.elements[:max_elements]],
        }


# ── §9.6: the typed action union ──────────────────────────────────────

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT_OPTION = "select_option"
    SET_CHECKED = "set_checked"
    PRESS_KEY = "press_key"
    SCROLL_CONTAINER = "scroll_container"
    SWITCH_PAGE = "switch_page"
    HANDLE_DIALOG = "handle_dialog"
    UPLOAD_ARTIFACT = "upload_artifact"
    DOWNLOAD_ARTIFACT = "download_artifact"
    OBSERVE = "observe"
    COMPLETE = "complete"


# Per-action validated arguments (§9.6): each action declares the fields it
# requires; the validator rejects unknown/missing fields.
ACTION_ARGUMENT_SCHEMA: Dict[ActionType, Dict[str, Any]] = {
    ActionType.NAVIGATE: {"required": ["url"], "optional": ["expected_title"]},
    ActionType.CLICK: {"required": ["target_ref"], "optional": ["expected_postcondition"]},
    ActionType.FILL: {"required": ["target_ref", "value"], "optional": ["secret", "verify_value"]},
    ActionType.SELECT_OPTION: {"required": ["target_ref", "value"], "optional": []},
    ActionType.SET_CHECKED: {"required": ["target_ref", "checked"], "optional": []},
    ActionType.PRESS_KEY: {"required": ["key"], "optional": ["target_ref"]},
    ActionType.SCROLL_CONTAINER: {"required": ["direction"], "optional": ["target_ref", "amount"]},
    ActionType.SWITCH_PAGE: {"required": ["page_id"], "optional": []},
    ActionType.HANDLE_DIALOG: {"required": ["dialog_action"], "optional": ["prompt_text"]},
    ActionType.UPLOAD_ARTIFACT: {"required": ["target_ref", "artifact_reference"], "optional": []},
    ActionType.DOWNLOAD_ARTIFACT: {"required": ["target_ref"], "optional": []},
    ActionType.OBSERVE: {"required": [], "optional": ["scope"]},
    ActionType.COMPLETE: {"required": ["result"], "optional": ["success"]},
}


def validate_action_arguments(action_type: ActionType,
                              arguments: Dict[str, Any]) -> tuple[bool, List[str]]:
    """§9.6: validate one action's arguments against its schema. The model
    proposes intent — it cannot issue unrestricted selectors, JavaScript,
    filesystem paths, or network permissions."""
    errors: List[str] = []
    schema = ACTION_ARGUMENT_SCHEMA.get(action_type)
    if schema is None:
        errors.append(f"unknown action type: {action_type}")
        return False, errors
    for req in schema.get("required", []):
        if req not in arguments or arguments[req] in (None, ""):
            errors.append(f"missing required argument: {req}")
    allowed = set(schema.get("required", [])) | set(schema.get("optional", []))
    for key in arguments:
        if key not in allowed:
            errors.append(f"unknown argument for {action_type.value}: {key}")
    # §9.6: JavaScript/selector injection is never accepted as an argument.
    for key, value in arguments.items():
        if isinstance(value, str) and value.strip().startswith(("javascript:", "file:")):
            errors.append(f"{key}: javascript:/file: schemes are not permitted")
    return (not errors), errors
