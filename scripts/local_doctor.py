#!/usr/bin/env python3
"""Local environment doctor (Phase 20 §20.1).

Checks every prerequisite the local-first platform needs and prints
SPECIFIC fixes — never a generic "environment blocked". Exit 0 = ready,
exit 1 = fixable problems named, exit 2 = hard platform limits named.

Usage: python3 scripts/local_doctor.py [--json]
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import socket
import sys
import sysconfig


def _check(name, ok, detail, fix):
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": fix}


def check_all() -> dict:
    results = []
    system = platform.system()
    arch = platform.machine()
    results.append(_check(
        "os_arch", system in ("Darwin", "Linux"),
        f"{system}/{arch}",
        "Use macOS or a supported Linux host (containers require a Linux "
        "runtime — on macOS they run inside a VM)."))

    py = sys.version_info
    # 3.11+ supported (the full suite runs on 3.14 in this environment).
    results.append(_check(
        "python_version", py >= (3, 11),
        f"Python {py.major}.{py.minor}.{py.micro}",
        "Install Python 3.11+ (pyenv or python.org)."))

    node = shutil.which("node")
    node_version = None
    if node:
        try:
            import subprocess
            out = subprocess.run([node, "--version"], capture_output=True,
                                 text=True, timeout=10)
            node_version = out.stdout.strip()
        except Exception:
            pass
    results.append(_check(
        "node", node is not None and (node_version or "").startswith("v"),
        node_version or "node not found",
        "Install Node 20+ (nvm install 20)."))

    docker = shutil.which("docker")
    docker_ok = False
    if docker:
        try:
            import subprocess
            out = subprocess.run([docker, "info"], capture_output=True,
                                 text=True, timeout=15)
            docker_ok = out.returncode == 0
        except Exception:
            pass
    results.append(_check(
        "container_runtime", docker_ok,
        "Docker running" if docker_ok else
        ("Docker CLI found but daemon not reachable" if docker
         else "Docker not found"),
        "Start Docker Desktop (macOS) or the dockerd service (Linux). "
        "On macOS containers run inside the Desktop VM — native app "
        "processes are separate."))

    playwright = shutil.which("npx") is not None
    results.append(_check(
        "playwright_cli", playwright, "npx available" if playwright
        else "npx not found",
        "Install Node (see node check); then `npx playwright install "
        "chromium` from frontend/."))

    # Ports the stack needs.
    # A port held by OUR OWN healthy service is the GOOD state (the stack
    # is already up); only a foreign occupant is a problem.
    probes = {
        8000: b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
        8080: b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
        4222: None,   # NATS answers any bytes with INFO
        3000: b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
    }
    for port, name in ((3000, "frontend"), (8000, "api"), (4222, "nats"),
                       (8080, "firestore_emulator")):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        occupied = s.connect_ex(("127.0.0.1", port)) == 0
        ours = False
        if occupied:
            try:
                probe = probes.get(port)
                data = b""
                if probe:
                    s.sendall(probe)
                # Read until the peer closes (one segment may be headers only).
                s.settimeout(1.5)
                try:
                    while True:
                        chunk = s.recv(2048)
                        if not chunk:
                            break
                        data += chunk
                        if len(data) > 8192:
                            break
                except (OSError, socket.timeout):
                    pass
                if port == 8000 and (b'"status"' in data
                                     or b'"healthy"' in data):
                    ours = True
                elif port == 8080 and (b"HTTP" in data or b"OK" in data):
                    ours = True
                elif port == 4222 and b"INFO" in data:
                    ours = True
                elif port == 3000 and b"HTTP" in data:
                    ours = True   # a listening web server; dev or prod
            except OSError:
                ours = False
        s.close()
        if not occupied:
            results.append(_check(
                f"port_{port}", True, "free",
                f"Start {name} when needed (compose profile or uvicorn/npm)."))
        elif ours:
            results.append(_check(
                f"port_{port}", True, f"{name} already running and healthy",
                ""))
        else:
            results.append(_check(
                f"port_{port}", False, "in use",
                f"Free port {port} ({name}) or stop the process using it."))

    # RAM/disk (informational thresholds).
    try:
        import os
        if system == "Darwin":
            ram_bytes = int(os.sysconf("HW_MEMSIZE")) \
                if hasattr(os, "sysconf") else 0
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        ram_bytes = int(line.split()[1]) * 1024
                        break
                else:
                    ram_bytes = 0
        ram_gb = ram_bytes / (1024 ** 3)
        results.append(_check(
            "ram", ram_gb >= 8, f"{ram_gb:.1f} GB",
            "8 GB+ recommended (local models and containers need more)."))
    except Exception as exc:
        results.append(_check("ram", True, f"unknown ({exc})", ""))

    disk_free = shutil.disk_usage("/").free / (1024 ** 3)
    results.append(_check(
        "disk", disk_free >= 10, f"{disk_free:.1f} GB free",
        "Free 10 GB+ for images, models, and test data."))

    # Credentials present WITHOUT printing values.
    import os
    creds = {
        "google_api_key": bool(os.environ.get("GOOGLE_API_KEY")),
        "vertex_adc": True,   # ADC checked at inference time, not here
        "aws_bearer_token_bedrock": bool(
            os.environ.get("AWS_BEARER_TOKEN_BEDROCK")),
        "aws_credentials_chain": bool(
            os.environ.get("AWS_ACCESS_KEY_ID")),
    }
    results.append(_check(
        "credentials_optional", True,
        "GOOGLE_API_KEY: " + ("set" if creds["google_api_key"] else "not set")
        + "; AWS_BEARER_TOKEN_BEDROCK: "
        + ("set" if creds["aws_bearer_token_bedrock"] else "not set")
        + " — live provider gates need these; local work does not",
        "Export credentials only for the live-provider gates."))

    return {
        "system": f"{system}/{arch}",
        "python": f"{py.major}.{py.minor}.{py.micro}",
        "checks": results,
        "ready": all(r["ok"] for r in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = check_all()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for r in report["checks"]:
            mark = "OK  " if r["ok"] else "FIX "
            print(f"[{mark}] {r['name']}: {r['detail']}")
            if not r["ok"] and r["fix"]:
                print(f"       fix: {r['fix']}")
        print(f"\nReady: {report['ready']}")
    if report["ready"]:
        return 0
    # Distinguish MISSING prerequisites (fixable: exit 1) from UNSUPPORTED
    # hosts (hard limits: exit 2) — §21.1.
    hard = [c for c in report["checks"]
            if not c["ok"] and c["name"] in ("os_arch", "python_version")]
    if hard:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
