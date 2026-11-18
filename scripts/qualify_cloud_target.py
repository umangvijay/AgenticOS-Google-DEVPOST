"""Observe a Helm deployment on GKE, EKS or AKS and exercise its HTTPS ingress."""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

PROFILES = {"gcp-gke": "gcp", "aws-eks": "aws", "azure-aks": "azure"}
COMPONENTS = ("api", "worker", "frontend")
DIGEST = r"sha256:[0-9a-f]{64}"


class ProbeError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ProbeError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate field in cloud release manifest")
        result[key] = value
    return result


def cli(*args):
    try:
        process = subprocess.run(args, capture_output=True, timeout=45, check=True)
        return json.loads(process.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ProbeError(f"{args[0]} readback failed ({type(exc).__name__})") from exc


def origin(value):
    parts = urlsplit(value)
    require(parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
            and not parts.query and not parts.fragment and parts.path in ("", "/"), "Expected HTTPS origin")
    return f"https://{parts.netloc.lower()}"


def cluster_endpoints(profile, config):
    name = config["cluster"]
    if profile == "gcp-gke":
        project, location = config["project"], config["location"]
        r = cli("gcloud", "container", "clusters", "describe", name, "--project", project,
                "--location", location, "--format=json(name,status,location,endpoint,selfLink)")
        require(r["status"] == "RUNNING" and r["name"] == name and r["location"] == location
                and f"/projects/{project}/" in r["selfLink"], "GKE identity or readiness mismatch")
        return r["selfLink"], {origin("https://" + r["endpoint"])}
    if profile == "aws-eks":
        r = cli("aws", "eks", "describe-cluster", "--name", name, "--region", config["region"],
                "--output", "json")["cluster"]
        require(r["status"] == "ACTIVE" and r["name"] == name
                and r["arn"] == config["cluster_arn"]
                and r["arn"].split(":")[3] == config["region"], "EKS identity or readiness mismatch")
        return r["arn"], {origin(r["endpoint"])}
    require(profile == "azure-aks", "Unsupported cloud profile")
    resource = config["resource_id"]
    r = cli("az", "aks", "show", "--ids", resource, "--output", "json")
    require(r["id"].lower() == resource.lower() and r["name"] == name
            and r["provisioningState"] == "Succeeded" and r["powerState"]["code"] == "Running",
            "AKS identity or readiness mismatch")
    return r["id"], {origin("https://" + r[key]) for key in ("fqdn", "privateFqdn") if r.get(key)}


def image_readback(deployment, pods, replicasets, component, expected):
    metadata, spec, status = (deployment[k] for k in ("metadata", "spec", "status"))
    count = spec.get("replicas", 1)
    require(count > 0 and status.get("observedGeneration") == metadata["generation"]
            and all(status.get(k) == count for k in ("replicas", "updatedReplicas", "readyReplicas",
                                                       "availableReplicas")), f"{component} rollout incomplete")
    containers = spec["template"]["spec"]["containers"]
    images = [c["image"] for c in containers if c["name"] == component]
    require(len(images) == 1 and images[0].endswith("@" + expected), f"{component} desired digest mismatch")
    live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    require(len(live) == count, f"{component} live replica count mismatch")
    receipts = []
    owned_replicasets = {
        rs["metadata"]["uid"] for rs in replicasets
        if any(owner.get("kind") == "Deployment" and owner.get("uid") == metadata["uid"]
               and owner.get("controller") is True
               for owner in rs["metadata"].get("ownerReferences", []))}
    for pod in live:
        require(any(owner.get("kind") == "ReplicaSet" and owner.get("uid") in owned_replicasets
                    and owner.get("controller") is True
                    for owner in pod["metadata"].get("ownerReferences", [])),
                f"{component} pod does not belong to the observed deployment")
        require([c["image"] for c in pod["spec"]["containers"] if c["name"] == component] == images,
                f"{component} pod specification differs from the deployment")
        ps = pod["status"]
        require(ps["phase"] == "Running" and any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in ps.get("conditions", [])), f"{component} pod unavailable")
        rows = [c for c in ps["containerStatuses"] if c["name"] == component]
        require(len(rows) == 1 and rows[0].get("ready") is True and "running" in rows[0].get("state", {}),
                f"{component} container unavailable")
        actual = re.search(r"(?:^|@|://)(sha256:[0-9a-f]{64})$", rows[0]["imageID"])
        require(actual and actual.group(1) == expected, f"{component} running digest mismatch")
        receipts.append(pod["metadata"]["uid"])
    return {"image_digest": expected, "deployment_uid": metadata["uid"], "pod_uids": sorted(receipts)}


def observe(config, profile):
    resource, endpoints = cluster_endpoints(profile, config)
    context = config["context"]
    kube = ["kubectl", "--context", context]
    cluster = cli(*kube, "config", "view", "--minify", "-o", "json")["clusters"][0]["cluster"]
    require(not cluster.get("insecure-skip-tls-verify") and not cluster.get("proxy-url")
            and not cluster.get("tls-server-name") and origin(cluster["server"]) in endpoints,
            "Kubernetes context differs from the provider endpoint or TLS configuration")
    namespace, chart = config["namespace"], config["fullname"]
    def get(*args):
        return cli(*kube, "--namespace", namespace, "get", *args, "-o", "json")
    ing = get("ingress", chart)
    host = urlsplit(origin(config["ingress_origin"])).hostname
    rules = [r for r in ing["spec"]["rules"] if r.get("host") == host]
    require(len(rules) == 1 and any(host in t.get("hosts", []) for t in ing["spec"].get("tls", [])),
            "HTTPS ingress host missing on selected cluster")
    # Tie the public origin to the load balancer observed on THIS cluster.
    def addresses(name):
        return {row[4][0] for row in socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)}
    lb_addresses = set()
    for lb in ing.get("status", {}).get("loadBalancer", {}).get("ingress", []):
        if lb.get("ip"):
            lb_addresses.add(lb["ip"])
        if lb.get("hostname"):
            lb_addresses.update(addresses(lb["hostname"]))
    resolved = addresses(host)
    require(resolved and resolved <= lb_addresses, "Ingress DNS differs from the cluster load balancer")
    paths = {p["path"]: p["backend"]["service"]["name"] for p in rules[0]["http"]["paths"]}
    require(paths.get("/api") == chart + "-api" and paths.get("/") == chart + "-frontend",
            "Ingress routes do not match the application services")
    observed = {}
    for component in COMPONENTS:
        name = f"{chart}-{component}"
        dep = get("deployment", name)
        require(dep["spec"]["selector"] == {"matchLabels": {"app": name}}, "Deployment selector mismatch")
        if component != "worker":
            require(get("service", name)["spec"]["selector"] == {"app": name}, "Service selector mismatch")
        pods = get("pods", "-l", f"app={name}")["items"]
        replicasets = get("replicasets", "-l", f"app={name}")["items"]
        observed[component] = image_readback(dep, pods, replicasets, component, config["image_digests"][component])
    return {"profile": profile, "resource": resource, "namespace": namespace, "ingress_uid": ing["metadata"]["uid"],
            "ingress_origin": origin(config["ingress_origin"]), "components": observed}


def workflow_probe(config, api):
    fixture = config["fixture_url"]
    parts = urlsplit(fixture)
    origin(f"https://{parts.netloc}")
    require(parts.scheme == "https" and not parts.query and not parts.fragment, "Expected clean HTTPS fixture URL")
    marker = uuid.uuid4().hex
    url = fixture + "?" + urlencode({"challenge": marker})
    def get_json(method, endpoint, **kwargs):
        response = api.request(method, endpoint, **kwargs)
        require(200 <= response.status_code < 300, f"HTTP {response.status_code} during cloud workflow probe")
        return response.json()
    expected = get_json("GET", url)
    require(isinstance(expected, dict) and expected.get("challenge") == marker
            and isinstance(expected.get("value"), str) and expected["value"], "Invalid challenge response")
    base = origin(config["ingress_origin"]) + "/api/v1/workflows"
    headers = {"Authorization": "Bearer " + os.environ["QUALIFY_STAGING_TOKEN"]}
    rid = get_json("POST", base, headers=headers, json={"goal": "GET " + url})["run_id"]
    require(isinstance(rid, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", rid), "Invalid run identifier")
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        run = get_json("GET", base + "/" + rid, headers=headers)
        require(run.get("run_id") == rid, "Ingress returned a different run")
        if run.get("status") == "COMPLETED":
            outputs = [t.get("output_data") or {} for t in run.get("tasks", [])
                       if t.get("agent") == "core.http" and t.get("status") == "COMPLETED"
                       and (t.get("input_data") or {}).get("url") == url]
            require(any(o.get("status_code") == 200 and o.get("data") == expected for o in outputs),
                    "Workflow output differs from independent fixture read")
            return {"run_id": rid, "submitted": True, "completed": True, "final_status": "COMPLETED",
                    "challenge": marker, "output_sha256": hashlib.sha256(json.dumps(expected, sort_keys=True)
                                                                        .encode()).hexdigest()}
        require(run.get("status") not in ("FAILED", "CANCELLED", "NEEDS_REVIEW"), "Ingress workflow stopped")
        time.sleep(2)
    raise ProbeError("Ingress workflow did not finish in time")


def probe(profile, candidate):
    require(profile in PROFILES, "Unsupported deployment profile")
    path = os.environ.get("QUALIFY_CLOUD_TARGET")
    require(path, "Configure QUALIFY_CLOUD_TARGET with the authorized staging release manifest")
    raw = Path(path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    require(sha == os.environ.get("QUALIFY_CLOUD_TARGET_SHA256"), "Cloud manifest digest mismatch")
    config = json.loads(raw, object_pairs_hook=unique_object)
    require(config["target_profile"] == profile and config["candidate_sha256"] == candidate,
            "Cloud manifest references a different release")
    require(set(config["image_digests"]) == set(COMPONENTS) and all(
        re.fullmatch(DIGEST, d) for d in config["image_digests"].values()), "Expected three pinned image digests")
    require(os.environ.get("QUALIFY_STAGING_TOKEN"), "Configure a scoped staging identity")
    for field in ("cluster", "context", "namespace", "fullname"):
        require(isinstance(config[field], str) and config[field] and not config[field].startswith("-"),
                f"Invalid {field} reference")
    before = observe(config, profile)
    with httpx.Client(follow_redirects=False, trust_env=False, timeout=15) as api:
        try:
            workflow = workflow_probe(config, api)
        except httpx.HTTPError as exc:
            raise ProbeError(f"Ingress or fixture transport failed ({type(exc).__name__})") from exc
    require(observe(config, profile) == before, "Deployment changed during the verified workflow")
    return {"ok": True, "exit": 0, "cloud": before, "candidate_sha256": candidate,
            "manifest_sha256": sha, "manifest_json": raw.decode("utf-8"),
            "image_digests": config["image_digests"], "workflow": workflow}
