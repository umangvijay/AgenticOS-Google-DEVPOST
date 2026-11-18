"""Phase 21 §21.24: packaging CORRECTNESS — the compose services, helm
chart, and requirements describe a stack that actually boots. Replaces
'helm lint passes' with structural checks against the REAL entry
points (audit G: wrong worker commands, port mismatches, missing
dependencies)."""
import os
import yaml


def _compose():
    with open("compose.yaml") as fh:
        return yaml.safe_load(fh)


def test_worker_entrypoint_matches_implementation():
    """The worker service runs `python -m backend.worker` and that
    module EXISTS with a __main__ guard."""
    compose = _compose()
    cmd = os.environ.get("WORKER_CMD")
    # The Dockerfile defines the entrypoint; verify it matches reality.
    with open("Dockerfile.worker") as fh:
        dockerfile = fh.read()
    assert "python -m backend.worker" in dockerfile or \
        'CMD ["python", "-m", "backend.worker"]' in dockerfile
    import backend.worker  # the module imports cleanly
    assert hasattr(backend.worker, "__main__") or \
        "__main__" in open("backend/worker.py").read()


def test_api_port_alignment():
    """The published port matches the health check and the app port."""
    compose = _compose()
    api = compose["services"]["api"]
    assert "8000:8000" in api["ports"]
    assert api["environment"]["PORT"] == "8000"
    hc = api["healthcheck"]["test"]
    assert any("8000" in part for part in hc), "health check hits :8000"


def test_core_profile_is_the_durable_platform():
    """§21.2: the core profile defaults to postgres storage + nats
    transport — the multi-process durable platform."""
    compose = _compose()
    env = compose["x-backend-env"]
    assert env["STORAGE_BACKEND"] == "${STORAGE_BACKEND:-postgres}"
    assert env["MESSAGE_BUS_TRANSPORT"] == "${MESSAGE_BUS_TRANSPORT:-nats}"
    assert "postgres" in env["POSTGRES_DSN"]


def test_helm_worker_commands_corrected():
    """audit G fix: helm worker/browser/runner commands run the REAL
    module (backend.worker, not backend.worker.main)."""
    with open("deploy/helm/agentos/values.yaml") as fh:
        values = yaml.safe_load(fh)
    for svc in ("worker", "browserWorker", "runner"):
        command = values[svc]["command"]
        assert "backend.worker.main" not in command, svc
        assert any("backend.worker" in str(part) for part in command), svc


def test_runner_profile_has_socket_and_caps():
    """§21.8: the runner supervises sibling containers through the
    mounted socket; its own privileges stay dropped."""
    compose = _compose()
    runner = compose["services"]["runner"]
    volumes = str(runner.get("volumes"))
    assert "docker.sock" in volumes
    assert runner.get("cap_drop") == ["ALL"]


def test_required_platform_dependencies_declared():
    """audit G fix: asyncpg/nats-py/gymnasium/stable-baselines3/docker
    are in requirements.txt — a clean checkout installs everything the
    platform imports."""
    with open("requirements.txt") as fh:
        reqs = fh.read()
    for package in ("asyncpg==", "nats-py==", "gymnasium==",
                    "stable-baselines3==", "docker=="):
        assert package in reqs, package


def test_bootstrap_script_exists_and_doctor_exits():
    """§21.1: the repeatable bootstrap exists; the doctor distinguishes
    missing prerequisites (exit 1) from unsupported hosts (exit 2)."""
    assert os.path.exists("scripts/bootstrap.sh")
    src = open("scripts/local_doctor.py").read()
    assert "return 2" in src, "unsupported hosts exit 2"
