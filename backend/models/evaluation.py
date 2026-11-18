from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional

class AgentEvaluationRecord(BaseModel):
    evaluation_id: str
    run_id: str
    workflow_id: str
    agent_name: str
    model: str
    task_type: str
    
    # Core Outcomes
    success: bool
    recovery_attempts: int
    successful_recoveries: int
    
    # Tool accuracy (§12.9: None = unavailable/unknown, never zero)
    tool_calls: int
    unknown_tool_call_rate: Optional[float] = None
    invalid_argument_rate: Optional[float] = None
    tool_schema_violation_rate: Optional[float] = None
    
    # Latency & Cost (§12.9: None = unavailable/unknown, never zero)
    latency_ms: Optional[float] = None
    token_usage_input: Optional[int] = None
    token_usage_output: Optional[int] = None
    token_usage_total: Optional[int] = None
    
    # Approvals
    approval_latency_ms: Optional[float] = None
    rejection_rate: Optional[float] = None
    
    # Versioning
    evaluator_version: str = "1.0.0"
    prompt_version: str = "1.0.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
