import logging
from datetime import datetime, timezone

class SecurityAuditLogger:
    def __init__(self):
        self.logger = logging.getLogger("agentos.security.audit")
        self.logger.setLevel(logging.INFO)
        
        # Use the application's logging handlers (stdout in containers).
        # Importing a security component must not require a writable working directory.

    def log_sandbox_execution(self, mcp_id: str, tool_name: str, docker_cmd: str):
        self.logger.info(f"SANDBOX_SPAWN | MCP={mcp_id} | TOOL={tool_name}")

    def log_sandbox_violation(self, mcp_id: str, reason: str, details: str = ""):
        self.logger.warning(f"SANDBOX_VIOLATION | MCP={mcp_id} | REASON={reason}")

    def log_sandbox_timeout(self, mcp_id: str, timeout_sec: int):
        self.logger.warning(f"SANDBOX_TIMEOUT | MCP={mcp_id} | TIMEOUT={timeout_sec}s")

audit_logger = SecurityAuditLogger()
