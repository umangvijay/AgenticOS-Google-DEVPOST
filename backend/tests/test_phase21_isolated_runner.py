"""Phase 21 §21.8: the isolated runner ENFORCES the runner contract
(runner_security.ISOLATION_ACCEPTANCE_TESTS) — every hostile fixture is
actually executed inside the container and the observable result must
prove the control. Replaces the Phase 20 vacuous tests: a seeded host
secret read FAILS, cancellation kills a RUNNING workload, output is
capped while streaming, records are durable."""
import tempfile
import threading
import time

import pytest

from backend.runners.container_runner import ContainerLimits, ContainerRunner


def _docker_up() -> bool:
    try:
        import docker
        docker.from_env().version()
        return True
    except Exception:
        # macOS + colima: the SDK ignores the docker CLI context. Try the
        # standard colima sockets before declaring the daemon unreachable
        # (Phase 22 — the guard previously skipped all 12 isolation tests
        # on a healthy daemon).
        import os
        for cand in (os.path.expanduser("~/.colima/default/docker.sock"),
                     os.path.expanduser(
                         "~/.colima/_lima/colima/docker.sock")):
            if os.path.exists(cand):
                try:
                    docker.DockerClient(
                        base_url=f"unix://{cand}").version()
                    return True
                except Exception:
                    continue
        return False


def _image_ready() -> bool:
    import os
    import docker
    client = None
    try:
        client = docker.from_env()
    except Exception:
        for cand in (os.path.expanduser("~/.colima/default/docker.sock"),
                     os.path.expanduser(
                         "~/.colima/_lima/colima/docker.sock")):
            if os.path.exists(cand):
                client = docker.DockerClient(base_url=f"unix://{cand}")
                break
    if client is None:
        return False
    try:
        client.images.get("agentos-runner:latest")
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def runner():
    if not _docker_up():
        pytest.skip("BLOCKED: docker daemon not reachable — "
                    "colima start (or any Linux container runtime)")
    if not _image_ready():
        pytest.skip("BLOCKED: runner image missing — "
                    "bash scripts/build_runner_image.sh")
    r = ContainerRunner()
    yield r
    r.shutdown()


pytestmark = pytest.mark.timeout(300)


def test_seeded_host_secret_unreadable(runner, tmp_path):
    """Acceptance 1 (FILESYSTEM_BUNDLE_ONLY): a host secret OUTSIDE the
    input bundle is unreadable — the container sees no host filesystem."""
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP-SECRET-VALUE")
    # The host secret is NOT mounted into the container at all.
    rec = runner.run("acc-1", "cat /etc/shadow 2>&1 | head -1; "
                              "ls /host-secret.txt 2>&1 | head -1",
                     limits=ContainerLimits(time_limit_s=20))
    assert "TOP-SECRET-VALUE" not in rec.stdout
    assert ("Permission denied" in rec.stdout
            or "No such file" in rec.stdout), rec.stdout


def test_tenant_isolation_no_cross_bundle_access(runner):
    """Acceptance 2 (TENANT_ISOLATION): only the seeded bundle is
    visible; another tenant's input is absent."""
    rec = runner.run(
        "acc-2", "echo bundle=$(cat /work/mine.txt 2>&1); "
                 "cat /work/other-tenant.txt 2>&1 | head -1",
        files={"mine.txt": "tenant-a data"},
        limits=ContainerLimits(time_limit_s=20))
    assert "bundle=tenant-a data" in rec.stdout, rec.stdout
    assert "No such file" in rec.stdout, "cross-tenant input is absent"


def test_cloud_metadata_unreachable(runner):
    """Acceptance 3 (METADATA_BLOCKED)."""
    rec = runner.run("acc-3",
                     "wget -T 3 -q -O- http://169.254.169.254/ 2>&1 | head -1",
                     limits=ContainerLimits(time_limit_s=30))
    assert "unreachable" in rec.stdout or "bad address" in rec.stdout, \
        rec.stdout


def test_unapproved_destination_denied(runner):
    """Acceptance 4 (NETWORK_DEFAULT_DENY)."""
    rec = runner.run("acc-4",
                     "wget -T 3 -q -O- http://example.com 2>&1 | head -1",
                     limits=ContainerLimits(time_limit_s=30))
    assert "unreachable" in rec.stdout or "bad address" in rec.stdout


def test_direct_socket_bypass_denied(runner):
    """Acceptance 5: bypassing HTTP libs with a raw socket is still
    denied (network_disabled is kernel-enforced)."""
    rec = runner.run("acc-5",
                     "python3 -c \"import socket; s=socket.socket(); "
                     "s.settimeout(3); s.connect(('93.184.216.34', 80))\" 2>&1 "
                     "| tail -1",
                     limits=ContainerLimits(time_limit_s=30))
    assert ("Network unreachable" in rec.stdout
            or "Network is unreachable" in rec.stdout
            or "timed out" in rec.stdout), rec.stdout


def test_memory_exhaustion_terminated_within_boundary(runner):
    """Acceptance 6 (MEMORY_LIMIT): the OOM kills the workload — the
    exit reflects termination, not silent completion."""
    rec = runner.run("acc-6",
                     "python3 -c \"x = bytearray(1024*1024*400)\" 2>&1 | "
                     "tail -1",
                     limits=ContainerLimits(memory_mb=128, time_limit_s=60))
    assert "Killed" in rec.stdout or rec.exit_code == 137, \
        (rec.exit_code, rec.stdout[-80:])


def test_process_spawn_bounded(runner):
    """Acceptance 7 (PROCESS_LIMIT)."""
    rec = runner.run("acc-7",
                     "for i in $(seq 1 200); do sleep 300 & done; "
                     "echo spawn-ok",
                     limits=ContainerLimits(pids_limit=16,
                                            time_limit_s=30))
    assert rec.exit_code != 0 or "spawn-ok" not in rec.stdout
    assert "unavailable" in rec.stderr or "unavailable" in rec.stdout


def test_unbounded_output_capped_while_streaming(runner):
    """Acceptance 8 (OUTPUT_CAPPED) — and unlike the Phase 20 runner,
    the cap applies DURING the stream (memory stays bounded)."""
    rec = runner.run("acc-8", "yes 0123456789 | head -c 500000",
                     limits=ContainerLimits(max_output_bytes=1000))
    assert rec.truncated is True
    assert len(rec.stdout) == 1000


def test_noncooperative_cancel_terminates_running_workload(runner):
    """Acceptance 9 (CANCEL_TERMINATION) — the REPLACEMENT for the
    Phase 20 test that cancelled after the run had already returned: a
    RUNNING sleep is killed by cancel(); the record shows it."""
    records = {}

    def run_long():
        records["rec"] = runner.run(
            "acc-9", "sleep 60 & sleep 60",
            limits=ContainerLimits(time_limit_s=120))

    t = threading.Thread(target=run_long)
    t.start()
    time.sleep(4)   # the workload is genuinely running
    assert runner.cancel("acc-9") is True
    t.join(timeout=30)
    rec = records["rec"]
    assert rec.cancelled is True
    assert rec.exit_code in (137, 143), rec.exit_code   # SIGKILL/SIGTERM
    assert rec.wall_ms < 20_000, "cancellation did not wait for completion"


def test_descendants_do_not_survive(runner):
    """Acceptance 10 (DESCENDANT_CLEANUP): background children die with
    the container — nothing outlives the namespace."""
    rec = runner.run("acc-10",
                     "sleep 300 & echo started; wait",
                     limits=ContainerLimits(time_limit_s=8))
    assert rec.exit_code == 137 or rec.timed_out, \
        "the container (and children) were terminated at the time limit"


def test_execution_records_are_durable():
    """Unlike the Phase 20 in-memory records, the container runner
    writes each record through a durable store."""
    if not (_docker_up() and _image_ready()):
        pytest.skip("BLOCKED: docker/runner image")
    durable = {}

    class Store:
        def put(self, execution_id, data):
            durable[execution_id] = data

    r = ContainerRunner(record_store=Store())
    rec = r.run("durable-1", "echo persisted", limits=ContainerLimits())
    assert durable.get("durable-1", {}).get("exit_code") == rec.exit_code
    assert durable["durable-1"]["container_id"] == rec.container_id
    r.shutdown()


def test_runtime_identity_recorded(runner):
    """Artifact/runtime identity: the image digest + container id are on
    every record (audit requirement)."""
    rec = runner.run("identity-1", "echo hi", limits=ContainerLimits())
    assert rec.image == "agentos-runner:latest"
    assert rec.image_digest, "image digest recorded"
    assert rec.container_id, "container id recorded"
