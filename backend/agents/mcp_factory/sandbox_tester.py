"""Generated Python must be validated by a dedicated isolated runner."""
from pathlib import Path


class SandboxTester:
    """The legacy host-subprocess harness is intentionally unavailable.

    A Python venv is dependency isolation, not a security boundary. This API
    returns an explicit unsupported result until a dedicated runner is wired in.
    """
    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)

    async def run_test(self, mcp_id: str, code: str) -> dict:
        return {
            "status": "unsupported",
            "message": "Generated code execution requires a dedicated isolated runner; host execution is disabled.",
            "mcp_id": mcp_id,
        }
