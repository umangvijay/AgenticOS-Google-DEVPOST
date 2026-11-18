"""Exercise the cloud qualifier contract. All cloud responses here are fixtures."""
import hashlib
import json
import socket

import httpx
import pytest

from scripts import qualify_cloud_target as cloud
from scripts import qualify_release as release


@pytest.fixture
def target(tmp_path, monkeypatch):
    def setup(profile, fault=None):
        digest = "sha256:" + "a"*64
        src = "c"*64
        cfg = dict(target_profile=profile, candidate_sha256=src, cluster="staging", context="staging",
                   namespace="test", fullname="app", project="p", location="loc", region="us-east-2",
                   cluster_arn="arn:aws:eks:us-east-2:123456789012:cluster/staging",
                   resource_id="/subscriptions/id/resourceGroups/g/providers/Microsoft.ContainerService/managedClusters/staging",
                   ingress_origin="https://stage.example", fixture_url="https://fixture.example/value",
                   image_digests={k:digest for k in cloud.COMPONENTS})
        path = tmp_path / "target.json"
        path.write_text(json.dumps(cfg))
        monkeypatch.setenv("QUALIFY_CLOUD_TARGET", str(path))
        monkeypatch.setenv("QUALIFY_CLOUD_TARGET_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
        monkeypatch.setenv("QUALIFY_STAGING_TOKEN", "test-only")
        endpoint = "control.example"
        calls = []
        def command(*args):
            calls.append(args)
            cluster_name = "other" if fault == "provider" else "staging"
            if args[0] == "aws":
                return {"cluster":dict(status="ACTIVE",name=cluster_name,arn=cfg["cluster_arn"],endpoint="https://"+endpoint)}
            if args[0] == "az":
                return dict(id=cfg["resource_id"],name=cluster_name,provisioningState="Succeeded",
                            powerState={"code":"Running"},fqdn=endpoint)
            if args[0] == "gcloud":
                return dict(name=cluster_name,location="loc",status="RUNNING",endpoint=endpoint,selfLink="/projects/p/clusters/staging")
            if "config" in args:
                return {"clusters":[{"cluster":{"server":"https://"+("other" if fault=="context" else endpoint),
                    "insecure-skip-tls-verify": fault=="tls"}}]}
            kind, label = args[args.index("get")+1:args.index("get")+3]
            if kind == "ingress":
                paths=[{"path":p,"backend":{"service":{"name":"app-"+c}}} for p,c in [("/","frontend"),("/api","api")]]
                return {"metadata":{"uid":"ingress"},"spec":{"tls":[{"hosts":["stage.example"]}],
                    "rules":[{"host":"stage.example","http":{"paths":paths}}]},
                    "status":{"loadBalancer":{"ingress":[{"ip":"10.1.2.4" if fault=="dns" else "10.1.2.3"}]}}}
            if kind == "service":
                return {"spec":{"selector":{"app":"other" if fault=="service" else label}}}
            if kind == "deployment":
                return {"metadata":{"generation":2,"uid":label},"spec":{"replicas":1,"selector":{"matchLabels":{"app":label}},
                    "template":{"spec":{"containers":[{"name":label[4:],"image":"registry/app@"+digest}]}}},
                    "status":dict(replicas=1,updatedReplicas=1,readyReplicas=1,availableReplicas=1,
                                  observedGeneration=1 if fault=="generation" else 2)}
            c = args[args.index("-l")+1].split("app-")[1]
            if kind == "replicasets":
                return {"items":[{"metadata":{"uid":"rs-"+c,"ownerReferences":[{
                    "kind":"Deployment","uid":"app-"+c,"controller":True}]}}]}
            assert kind == "pods"
            uid = "replacement" if fault=="rollout" and sum("ingress" in call for call in calls)>1 else c
            return {"items":[{"metadata":{"uid":uid,"ownerReferences":[{
                "kind":"ReplicaSet","uid":"other" if fault=="owner" else "rs-"+c,"controller":True}]},
                "spec":{"containers":[{"name":c,"image":"registry/app@"+(
                    "sha256:"+"f"*64 if fault=="pod_spec" else digest)}]},
                "status":{"conditions":[{"type":"Ready","status":"True"}],
                "phase":"Running","containerStatuses":[{"name":c,"state":{"running":{}},"ready":True,
                "imageID":"registry/app@"+("sha256:"+"d"*64 if fault=="pod" else digest)}]}}]}
        monkeypatch.setattr(cloud, "cli", command)
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a,**kw:[(None,None,None,None,("10.1.2.3",443))])
        fixture = {}
        def request(req):
            if req.url.host == "fixture.example":
                assert req.headers.get("authorization") is None
                fixture.update(challenge=req.url.params["challenge"],value="from remote")
                return httpx.Response(200,json=fixture)
            assert req.url.host=="stage.example" and req.headers["authorization"]=="Bearer test-only"
            if req.method=="POST":
                if fault == "auth":
                    return httpx.Response(401, text="do not retain response body")
                if fault == "redirect":
                    return httpx.Response(302, headers={"location":"https://untrusted.example/"})
                return httpx.Response(201,json={"run_id":"r"})
            url=cfg["fixture_url"]+"?challenge="+fixture["challenge"]
            return httpx.Response(200,json={"run_id":"r","status":"COMPLETED","tasks":[{
                "agent":"core.http","status":"COMPLETED","input_data":{"url":url},
                "output_data":{"status_code":200,"data":{} if fault=="output" else fixture}}]})
        client=httpx.Client
        monkeypatch.setattr(cloud.httpx,"Client",lambda **kw:client(transport=httpx.MockTransport(request),**kw))
        return src, calls
    return setup


@pytest.mark.parametrize("profile",list(cloud.PROFILES))
def test_cloud_stage_through_qualifier(profile, target, tmp_path, monkeypatch):
    src, calls = target(profile)
    monkeypatch.setenv("TARGET_PROFILE", profile)
    monkeypatch.setattr(release,"ROOT",tmp_path)
    q=release.Qualifier.__new__(release.Qualifier)
    q.attempt_dir=tmp_path/"attempt"
    q.attempt_dir.mkdir()
    q.report={"source_identity":{"content_sha256":src},"stages":{}}
    q.s_target_deployment()
    record=q.report["stages"]["target_deployment"]
    assert record["ok"] and not release.parse_retained_counts("target_deployment",record,q.attempt_dir)
    assert len(record["image_digests"])==3 and sum("ingress" in call for call in calls)==2


@pytest.mark.parametrize("fault",["context","dns","pod","output","rollout","owner",
                                  "tls","service","generation","pod_spec","auth","redirect"])
def test_cloud_probe_refuses_mismatched_observations(target, fault):
    src, _ = target("aws-eks", fault)
    with pytest.raises(cloud.ProbeError):
        cloud.probe("aws-eks",src)


@pytest.mark.parametrize("profile", list(cloud.PROFILES))
def test_wrong_provider_identity_prevents_kubernetes_and_http_access(target, profile):
    src, calls = target(profile, "provider")
    with pytest.raises(cloud.ProbeError, match="identity or readiness mismatch"):
        cloud.probe(profile, src)
    assert len(calls) == 1 and calls[0][0] != "kubectl"


@pytest.mark.parametrize("fault", ["hash", "candidate", "token", "duplicate"])
def test_cloud_probe_refuses_unreviewed_inputs_before_contacting_target(target, fault, monkeypatch):
    from pathlib import Path
    src, calls = target("gcp-gke")
    if fault == "hash":
        monkeypatch.setenv("QUALIFY_CLOUD_TARGET_SHA256", "0" * 64)
    elif fault == "candidate":
        src = "e" * 64
    elif fault == "token":
        monkeypatch.delenv("QUALIFY_STAGING_TOKEN")
    else:
        path = Path(cloud.os.environ["QUALIFY_CLOUD_TARGET"])
        path.write_text(path.read_text().replace('{', '{"cluster":"hidden",', 1))
        monkeypatch.setenv("QUALIFY_CLOUD_TARGET_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(cloud.ProbeError):
        cloud.probe("gcp-gke", src)
    assert calls == []


@pytest.mark.parametrize("field", ["manifest_json", "namespace", "image_digests", "candidate_sha256"])
def test_retained_cloud_manifest_cannot_disagree_with_observations(target, tmp_path, field):
    src, _ = target("aws-eks")
    outcome = cloud.probe("aws-eks", src)
    if field == "manifest_json":
        outcome[field] += " "
    elif field == "namespace":
        outcome["cloud"][field] = "other"
    elif field == "image_digests":
        outcome[field]["api"] = "sha256:" + "f" * 64
    else:
        outcome[field] = "f" * 64
    path = tmp_path / "result.json"
    path.write_text(json.dumps(outcome))
    record = dict(kind="shell", ok=True, profile="aws-eks", candidate_sha256=src,
                  image_digests=outcome["image_digests"], result_file=str(path),
                  result_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert release.parse_retained_counts("target_deployment", record, tmp_path)


@pytest.mark.parametrize("profile", list(cloud.PROFILES))
@pytest.mark.parametrize("stage", sorted(release.LOCAL_RUNTIME_STAGES))
def test_local_runtime_results_never_qualify_cloud(stage, profile, tmp_path):
    from backend.tests.test_phase25_verdict_evidence import base_report, qualifier
    q = qualifier(base_report(tmp_path))
    q.report["target_identity"]["profile"] = profile
    problems = q._stage_problems(stage, q.report["stages"][stage])
    assert any("producer exercises local infrastructure" in p for p in problems)
