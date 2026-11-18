"""Catch Cloud Build configuration rejection before uploading any source."""
import json
from pathlib import Path
import re

import pytest
import yaml


@pytest.mark.parametrize("filename", ["cloudbuild.yaml", "cloudbuild.frontend.yaml"])
def test_custom_substitutions_are_defined_and_used(filename):
    path = Path(__file__).resolve().parents[2] / filename
    config = yaml.safe_load(path.read_text())
    declared = set(config.get("substitutions", {}))
    template = {k: v for k, v in config.items() if k != "substitutions"}
    used = set(re.findall(r"\$\{(_[A-Z0-9_]+)\}|\$(_[A-Z0-9_]+)", json.dumps(template)))
    used = {a or b for a, b in used}
    assert declared == used, f"unused={declared-used}, undefined={used-declared}"
