#!/usr/bin/env python3
"""Prepare verified GKE build inputs; publish images only with --build.

No cluster, database, secret or deployment is created by this command.
Build receipts record observations, not signed supply-chain attestations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.verify_candidate import reconstruct_candidate, _unique_object
from scripts.source_identity import source_fingerprint

BUILDS = {"api": (".", "Dockerfile.api"), "worker": (".", "Dockerfile.worker"),
          "frontend": ("frontend", "Dockerfile.frontend")}
MEDIA_TYPES = {"application/vnd.oci.image.manifest.v1+json",
               "application/vnd.docker.distribution.manifest.v2+json"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(args):
    for value, pattern, name in (
        (args.candidate, r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", "candidate"),
        (args.project, r"[a-z][a-z0-9-]{4,28}[a-z0-9]", "project"),
        (args.region, r"[a-z]+-[a-z]+[0-9]+", "region"),
        (args.repository, r"[a-z][a-z0-9_-]{0,62}", "repository"),
    ):
        require(isinstance(value, str) and re.fullmatch(pattern, value), f"Invalid {name}")
    parts = urlsplit(args.api_url)
    require(parts.scheme == "https" and parts.hostname and not parts.username
            and not parts.password and not parts.query and not parts.fragment
            and parts.path == "/api/v1" and not any(c.isspace() for c in args.api_url),
            "api-url must be a credential-free HTTPS URL ending in /api/v1")


def write_json(path, body):
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


def run(argv, *, cwd, log):
    """Bounded commands; caller retains output without echoing account details."""
    with log.open("xb") as output:
        subprocess.run(argv, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                       timeout=3600, check=True)


def read_registry(argv, *, cwd, log):
    # Keep stdout unmodified: its digest is the registry manifest identity.
    with log.with_suffix(".stderr.log").open("xb") as errors:
        result = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=errors, timeout=60, check=True)
    log.write_bytes(result.stdout)
    return result.stdout


def prepare(args, *, candidates=None):
    validate(args)
    output = Path(args.output).resolve()
    # A new attempt never overwrites a successful or failed receipt.
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    receipt = {"candidate_id": args.candidate, "target_profile": "gcp-gke",
               "status": "preparing", "deployment_verified": False,
               "production_qualified": False, "images": {}}
    try:
        with tempfile.TemporaryDirectory(prefix="agentos-gcp-build-") as directory:
            source = Path(directory) / "source"
            manifest = reconstruct_candidate(
                (candidates or ROOT / "docs/evidence/candidates") / args.candidate, source)
            require(manifest.get("candidate_id") == args.candidate, "Candidate identity mismatch")
            sha = manifest["verification"]["expected_content_sha256"]
            receipt.update(candidate_sha256=sha,
                           archive_sha256=manifest["source_archive"]["sha256"],
                           platform=args.platform, api_url=args.api_url)
            registry = f"{args.region}-docker.pkg.dev/{args.project}/{args.repository}"
            plan = {"registry": registry, "platform": args.platform, "commands": {}}
            for component, (context, dockerfile) in BUILDS.items():
                require((source / context / dockerfile).is_file(), f"Missing {component} Dockerfile")
                image = f"{registry}/{component}:{args.candidate}"
                command = ["docker", "buildx", "build", "--platform", args.platform,
                           "--provenance=false", "--sbom=false", "--push", "--tag", image,
                           "--file", dockerfile, "--metadata-file", str(output / f"{component}-metadata.json"),
                           "--label", f"io.agentos.source-sha256={sha}"]
                if component == "frontend":
                    command += ["--build-arg", f"NEXT_PUBLIC_API_URL={args.api_url}"]
                command += ["."]
                plan["commands"][component] = {"context": context, "argv": command}
            write_json(output / "build-plan.json", plan)
            if not args.build:
                receipt["status"] = "prepared_only"
                return receipt
            # The named repository must already exist and be accessible. Do
            # not hide create errors, choose a default project, or alter IAM.
            run(["gcloud", "artifacts", "repositories", "describe", args.repository,
                 "--project", args.project, "--location", args.region, "--format=json"],
                cwd=source, log=output / "repository.log")
            for component, spec in plan["commands"].items():
                require(source_fingerprint(source)[0] == sha, "Build source changed before execution")
                run(spec["argv"], cwd=source / spec["context"], log=output / f"{component}-build.log")
                require(source_fingerprint(source)[0] == sha, "Build source changed during execution")
                meta = json.loads((output / f"{component}-metadata.json").read_text(),
                                  object_pairs_hook=_unique_object)
                digest = meta.get("containerimage.digest", "")
                require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
                        "Builder did not return a registry manifest digest")
                pinned = f"{registry}/{component}@{digest}"
                raw = read_registry(["docker", "buildx", "imagetools", "inspect", "--raw", pinned],
                                    cwd=source, log=output / f"{component}-manifest.json")
                require(any("sha256:" + hashlib.sha256(value).hexdigest() == digest
                            for value in (raw, raw.removesuffix(b"\n"))), "Registry manifest digest mismatch")
                observed = json.loads(raw, object_pairs_hook=_unique_object)
                require(observed.get("mediaType") in MEDIA_TYPES and "manifests" not in observed,
                        "Expected a single-platform manifest; indexes need separate qualification")
                config = json.loads(read_registry(
                    ["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Image}}", pinned],
                    cwd=source, log=output / f"{component}-config.json"), object_pairs_hook=_unique_object)
                require(f"{config.get('os')}/{config.get('architecture')}" == args.platform,
                        "Registry image architecture differs from requested platform")
                require(config.get("config", {}).get("Labels", {}).get("io.agentos.source-sha256") == sha,
                        "Registry image does not retain the candidate label")
                receipt["images"][component] = {"reference": pinned, "digest": digest}
                write_json(output / "build-receipt.json", receipt)
            values = {"global": {"imageRegistry": registry}}
            for component, image in receipt["images"].items():
                values[component] = {"image": component, "imageDigest": image["digest"]}
            # Both optional roles use the same worker image, not a mutable tag.
            for component in ("browserWorker", "runner"):
                values[component] = dict(values["worker"])
            write_json(output / "image-values.json", values)
            receipt["status"] = "images_published"
            return receipt
    except BaseException as exc:
        receipt.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        write_json(output / "build-receipt.json", receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--repository", default="agentos")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    parser.add_argument("--output", required=True, help="Fresh local attempt directory")
    parser.add_argument("--build", action="store_true", help="Build and push to the existing Artifact Registry repository")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        receipt = prepare(args)
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"Release preparation failed ({type(exc).__name__}); inspect the retained attempt.", file=sys.stderr)
        return 1
    print(f"Release preparation: {receipt['status']}. No deployment or production qualification performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
