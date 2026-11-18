import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('release_smoke', Path(__file__).parents[2] / 'scripts/verify_deployed_workflows.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_smoke_requires_persisted_tool_result_and_model_summary():
    with pytest.raises(RuntimeError):
        smoke.verify_health_result({'status': 'COMPLETED', 'tasks': []})
    smoke.verify_health_result({'tasks': [
        {'agent': 'core.health', 'output_data': {'status_code': 200}},
        {'agent': 'core.chat', 'output_data': {'reply': 'Live result'}},
    ]})
