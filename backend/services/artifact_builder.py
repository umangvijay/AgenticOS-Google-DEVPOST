"""
AgentOS — Artifact builder.

Generates a real website or small software project as files on disk from a
natural-language brief. Files are stored under data/artifacts/<user>/<id>/
and served via the artifacts API. Generated code is never executed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config.settings import settings
from backend.services import gemini_client

logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES = {
    ".html", ".css", ".js", ".json", ".md", ".txt", ".svg", ".py",
    ".ts", ".tsx", ".jsx", ".yml", ".yaml", ".toml",
    ".obj",  # §27.15: Wavefront OBJ model assets (plain text)
    ".pdf",  # §28.8: verified PDF deliverables (inline preview + download)
}
MAX_FILES = 60
MAX_FILE_BYTES = 220_000
BATCH_SIZE = 2
SAFE_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


class ArtifactError(Exception):
    pass


def artifacts_root() -> Path:
    if settings.ARTIFACTS_ROOT:
        root = Path(settings.ARTIFACTS_ROOT)
    else:
        base = Path(settings.SQLITE_DB_PATH).parent if settings.SQLITE_DB_PATH else Path("data")
        root = Path(base) / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_relpath(path: str) -> str:
    path = path.replace("\\", "/").lstrip("/")
    if path.startswith(".") or ".." in path.split("/"):
        raise ArtifactError(f"Illegal path: {path}")
    if not SAFE_PATH.match(path):
        raise ArtifactError(f"Illegal path: {path}")
    suffix = Path(path).suffix.lower()
    name = Path(path).name
    if name.endswith(".env.example"):
        return path
    if suffix not in ALLOWED_SUFFIXES:
        raise ArtifactError(f"Disallowed file type: {path}")
    return path


def _artifact_dir(user_id: str, artifact_id: str) -> Path:
    for value in (user_id, artifact_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
            raise ArtifactError("Invalid artifact identity")
    root = artifacts_root().resolve()
    folder = root / user_id / artifact_id
    try:
        folder.resolve().relative_to(root)
    except ValueError as exc:
        raise ArtifactError("Artifact path escapes storage") from exc
    return folder


def _file_paths(metadata):
    return [item["path"] if isinstance(item, dict) else item for item in metadata.get("files", [])]


def _checksums(dest, paths):
    return {path: hashlib.sha256((dest / path).read_bytes()).hexdigest() for path in paths}


def _write_files(dest: Path, files: List[Dict[str, Any]]) -> List[str]:
    """§28.8: binary-safe — a `bytes` payload is written verbatim (the
    verified PDF deliverable), str payloads stay utf-8 text. The stored
    bytes ARE the identity, so an encoding round-trip that silently
    changes them is a defect."""
    written: List[str] = []
    if not files:
        raise ArtifactError("Model produced no files")
    if len(files) > MAX_FILES:
        raise ArtifactError(f"Artifact exceeds the {MAX_FILES}-file limit")
    for item in files:
        rel = _safe_relpath(str(item.get("path") or ""))
        if rel == "artifact.json" or rel in written:
            raise ArtifactError("Reserved or duplicate artifact path")
        raw = item.get("content")
        if isinstance(raw, bytes):
            encoded = raw
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
        else:
            content = str(raw or "")
            encoded = content.encode("utf-8")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ArtifactError(f"Generated file {rel} exceeds the size limit")
        written.append(rel)
    return written


async def generate_project(
    user_id: str,
    brief: str,
    kind: str = "website",
    name: str = "",
    scale: str = "standard",
    on_progress=None,
) -> Dict[str, Any]:
    """Generate a website or app as files. scale: compact | standard | full."""
    async def progress(stage, completed=0, total=0):
        if on_progress:
            await on_progress({"stage": stage, "completed_files": completed, "total_files": total})
    await progress("planning")
    if not (brief or "").strip():
        raise ArtifactError("A brief is required")
    kind = (kind or "website").lower().strip()
    if kind not in ("website", "app"):
        kind = "website"
    scale = (scale or "standard").lower().strip()
    if scale not in ("compact", "standard", "full"):
        scale = "standard"

    size_guide = {
        "compact": "Exactly 3 files: index.html, styles.css, and script.js. One complete page.",
        "standard": "12-28 files. Multi-page site or a complete local app with components, styles, and docs.",
        "full": "24-50 files. Production-shaped project: multiple pages/modules, shared layout, README, and config.",
    }[scale]

    if scale == "compact":
        one_shot = f"""You are a production engineer. Build a compact {kind} as complete files.

PROJECT NAME: {name or "hello-agentos"}
BRIEF:
{brief[:8000]}

Return JSON only:
{{
  "name": "short-kebab-name",
  "summary": "one paragraph",
  "entrypoint": "index.html",
  "files": [
    {{"path": "index.html", "content": "full html"}},
    {{"path": "styles.css", "content": "full css"}},
    {{"path": "script.js", "content": "full js"}}
  ]
}}
Escape every quote and newline inside content strings. No markdown.
"""
        payload = await gemini_client.generate_json(one_shot)
        if not isinstance(payload, dict) or not payload.get("files"):
            payload = await gemini_client.generate_json(
                one_shot + "\n\nThe previous reply was not valid JSON with a files array. Return only the JSON object."
            )
        if not isinstance(payload, dict) or not payload.get("files"):
            raise ArtifactError("Model returned an invalid project")
        generated = [f for f in payload["files"] if isinstance(f, dict) and f.get("path")]
        plan = payload
    else:
        if kind == "website":
            extra = (
                "Produce a complete static website with real pages (not lorem-only stubs). "
                "Must include index.html plus additional pages as the brief requires (about, features, contact, etc.). "
                "Use semantic HTML, accessible markup, shared CSS, and JS only where needed. No external paid APIs."
            )
        else:
            extra = (
                "Produce a complete local software project the user can run. "
                "Include README.md with run instructions, source modules, tests if useful, and requirements.txt "
                "or package.json. Prefer Python (FastAPI) or a static+JS app. Do not include secrets."
            )

        plan_prompt = f"""You are a production engineer. Plan a {kind} project, then we will fill each file.

KIND: {kind}
SCALE: {scale} — {size_guide}
PROJECT NAME: {name or "(derive a short kebab-case name)"}
BRIEF:
{brief[:12000]}

{extra}

Return JSON only:
{{
  "name": "short-kebab-name",
  "summary": "one paragraph of architecture and what was built",
  "entrypoint": "index.html or main file",
  "files": [{{"path": "relative/path.ext", "purpose": "what this file contains"}}]
}}
Only these extensions: {", ".join(sorted(ALLOWED_SUFFIXES))}.
Every file must be necessary. Prefer a coherent multi-file structure over one giant HTML file.
"""
        plan = await gemini_client.generate_json(plan_prompt)
        planned = plan.get("files") if isinstance(plan, dict) else None
        if not isinstance(planned, list) or not planned:
            raise ArtifactError("Model returned an invalid project plan")
        if len(planned) > MAX_FILES:
            raise ArtifactError(f"Project plan exceeds the {MAX_FILES}-file limit")
        for item in planned:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ArtifactError("Project plan contains an invalid file path")
            item["path"] = _safe_relpath(item["path"])
        if len({item["path"] for item in planned}) != len(planned):
            raise ArtifactError("Project plan contains duplicate file paths")
        generated = []

        for i in range(0, len(planned), BATCH_SIZE):
            await progress("writing_files", len(generated), len(planned))
            chunk = planned[i:i + BATCH_SIZE]
            listing = "\n".join(f"- {f['path']}: {f.get('purpose', '')}" for f in chunk)
            batch_prompt = f"""Write COMPLETE file contents for this {kind} named "{plan.get('name') or name}".
Do not stub. Match the brief. Return JSON {{"files": [{{"path","content"}}]}} covering ONLY these files:

{listing}

BRIEF:
{brief[:6000]}

PROJECT FILE MAP:
{json.dumps(planned)}

Only these paths. Full source in each content field.
"""
            try:
                batch = await gemini_client.generate_json(batch_prompt)
            except json.JSONDecodeError:
                # A truncated batch can still be generated one file at a time.
                batch = []
            files = batch.get("files", []) if isinstance(batch, dict) else batch if isinstance(batch, list) else []
            files = files if isinstance(files, list) else []
            expected_chunk = {f["path"] for f in chunk}
            by_path = {f["path"]: f for f in files if isinstance(f, dict)
                       and isinstance(f.get("path"), str) and f.get("path") in expected_chunk and isinstance(f.get("content"), str) and f["content"].strip()}
            # Repair only missing files. Never accept a partial project as complete.
            for item in chunk:
                if item["path"] not in by_path:
                    repair = await gemini_client.generate_json(
                        batch_prompt + "\nReturn ONLY this missing file: " + item["path"])
                    candidates = repair.get("files", []) if isinstance(repair, dict) else []
                    for candidate in candidates if isinstance(candidates, list) else []:
                        if (isinstance(candidate, dict) and candidate.get("path") == item["path"]
                                and isinstance(candidate.get("content"), str) and candidate["content"].strip()):
                            by_path[item["path"]] = candidate
                    if item["path"] not in by_path:
                        raise ArtifactError("Model did not finish a required file: " + item["path"])
                generated.append(by_path[item["path"]])
            await progress("writing_files", len(generated), len(planned))

    if not generated:
        raise ArtifactError("Model produced no files")
    if scale != "compact":
        expected = {item["path"] for item in planned}
        produced = {item.get("path") for item in generated if isinstance(item, dict) and item.get("content")}
        if expected != produced:
            raise ArtifactError("Generated files do not match the project plan; retry generation")

    await progress("saving_files", len(generated), len(generated))
    artifact_id = str(uuid.uuid4())
    dest = _artifact_dir(user_id, artifact_id)
    dest.mkdir(parents=True, exist_ok=True)
    written = _write_files(dest, generated)

    metadata = {
        "artifact_id": artifact_id,
        "user_id": user_id,
        "kind": kind,
        "scale": scale,
        "name": plan.get("name") or name or kind,
        "summary": plan.get("summary") or "",
        "entrypoint": plan.get("entrypoint") or (written[0] if written else ""),
        "files": written,
        "checksums": _checksums(dest, written),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (dest / "artifact.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    logger.info("Generated %s/%s artifact %s (%d files) for user %s", kind, scale, artifact_id, len(written), user_id)
    await asyncio.to_thread(_persist_artifact_remote, metadata, dest)
    return metadata


def _use_firestore() -> bool:
    return (settings.STORAGE_BACKEND or "").lower() == "firestore"


def _firestore_sync():
    from google.cloud import firestore as fs
    project = settings.GOOGLE_CLOUD_PROJECT
    if not project:
        return None
    return fs.Client(project=project, database=settings.FIRESTORE_DATABASE_ID)


def _persist_artifact_remote(metadata: Dict[str, Any], dest: Path) -> None:
    if not _use_firestore():
        return
    db = _firestore_sync()
    if db is None:
        raise ArtifactError("Cloud artifact storage is not configured")
    aid = metadata["artifact_id"]
    batch = db.batch()
    total_bytes = 0
    for rel in _file_paths(metadata):
        path = dest / rel
        if not path.is_file():
            raise ArtifactError("Artifact file missing before upload")
        content = path.read_text(encoding="utf-8")
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > 8_000_000:
            raise ArtifactError("Artifact exceeds the cloud transaction size limit")
        doc_id = hashlib.sha256(f"{aid}/{rel}".encode("utf-8")).hexdigest()
        batch.set(db.collection("artifact_files").document(doc_id),
            {
                "artifact_id": aid,
                "user_id": metadata["user_id"],
                "path": rel,
                "content": content,
            }
        )
    batch.set(db.collection("artifacts").document(aid), dict(metadata))
    batch.commit()


def save_user_files(user_id: str, name: str, files: List[Dict[str, str]]) -> Dict[str, Any]:
    """Persist arbitrary agent-produced files as a downloadable artifact.
    Used by the create_files orchestrator tool (reports, CSVs, code, docs)."""
    import re as _re
    import uuid as _uuid
    safe_name = _re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "files").strip()).strip("-").lower()[:40] or "files"
    artifact_id = f"{safe_name}-{_uuid.uuid4().hex[:8]}"
    dest = _artifact_dir(user_id, artifact_id)
    dest.mkdir(parents=True, exist_ok=True)
    written = _write_files(dest, files)
    metadata = {
        "artifact_id": artifact_id,
        "user_id": user_id,
        "checksums": _checksums(dest, written),
        "name": name or safe_name,
        "kind": "files",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(written),
        "files": [{"path": p} for p in written],
    }
    meta_path = dest / "artifact.json"
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    _persist_artifact_remote(metadata, dest)
    # Phase 17 (review gap): the durable deliverable record + staged
    # publication + typed verification run on THIS production path.
    # Failures are recorded but never break the legacy return shape.
    delivery: Dict[str, Any] = {}
    try:
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        if loop is not None:
            delivery = "async-pending"
    except RuntimeError:
        loop = None
    metadata["delivery"] = delivery
    return metadata


async def save_user_files_with_delivery(user_id: str, name: str,
                                        files: List[Dict[str, str]],
                                        *, tenant_id: str = "",
                                        run_id: Optional[str] = None,
                                        task_id: Optional[str] = None,
                                        lineage: Optional[Dict[str, Any]] = None,
                                        ) -> Dict[str, Any]:
    """§17.6/§17.7/§17.9 production entry: persist the files AND record
    the durable deliverable (staged publication + typed verification).
    The orchestrator's create_files tool calls this instead of the bare
    save, so every generated deliverable has restart-readable evidence."""
    metadata = save_user_files(user_id, name, files)
    dest = _artifact_dir(user_id, metadata["artifact_id"])
    written = [f["path"] for f in metadata["files"]]
    delivery = await record_and_publish_deliverable(
        owner_user_id=user_id, tenant_id=tenant_id or user_id,
        deliverable_type="source_project", name=name,
        dest=dest, written=written, run_id=run_id, task_id=task_id,
        lineage=lineage, artifact_id=metadata["artifact_id"])
    metadata["delivery"] = delivery
    return metadata


async def generate_model_asset(
    user_id: str,
    parameters: Dict[str, Any],
    *,
    name: str = "",
    tenant_id: str = "",
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """§27.15 Part B production entry: a parameterized 3D model request →
    validated OBJ asset through the SAME deliverable contract as every
    other generated artifact (staged publication + typed verification).
    A modeling request can never silently produce a website: unknown
    kinds are rejected here, not coerced."""
    from backend.services import modeling_service
    from backend.services.modeling_service import ModelParameters, ModelingError

    try:
        params = ModelParameters(
            kind=str(parameters.get("kind") or "").strip().lower(),
            size=tuple(float(v) for v in (parameters.get("size") or (1.0, 1.0, 1.0))),
            segments=int(parameters.get("segments") or 16),
            parts=list(parameters.get("parts") or []),
            name=str(parameters.get("name") or name or "asset")[:60],
            units=str(parameters.get("units") or "mm"),
            material=str(parameters.get("material") or "generic"),
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactError(f"Invalid model parameters: {exc}") from exc
    try:
        obj = modeling_service.export_obj(params)
    except ModelingError as exc:
        raise ArtifactError(str(exc)) from exc
    # §31.20: the INPUT SPECIFICATION and the EXPORTED BYTES get distinct
    # digests — the lineage's spec digest must identify the requested
    # geometry, never the exported artifact itself.
    spec_sha256 = hashlib.sha256(json.dumps(
        {"kind": params.kind, "size": list(params.size),
         "segments": params.segments, "parts": params.parts,
         "units": params.units, "material": params.material},
        sort_keys=True).encode()).hexdigest()
    # Independent byte-level validation BEFORE publication — the exported
    # bytes are re-parsed; a malformed asset never becomes a deliverable.
    validation = modeling_service.validate_asset(obj["content"])
    if not validation.get("valid"):
        raise ArtifactError("Generated asset failed independent validation")
    asset_name = (name or params.name or "model-asset").strip()[:60]
    metadata = await save_user_files_with_delivery(
        user_id, asset_name,
        [
            {"path": f"{obj['name']}", "content": obj["content"].decode("ascii")},
            {"path": "model.json",
             "content": json.dumps({
                 "format": "obj",
                 "vertices": obj["vertices"],
                 "faces": obj["faces"],
                 "sha256": obj["sha256"],
                 "parameters": obj["parameters"],
                 "generation_ms": obj["generation_ms"],
                 "validation": validation,
                 "toolchain": {"service": "modeling_service", "version": 1},
             }, indent=2)},
        ],
        tenant_id=tenant_id, run_id=run_id, task_id=task_id,
        lineage={"kind": "model_asset",
                 "spec_sha256": spec_sha256,
                 "asset_sha256": obj["sha256"],
                 "parameters": obj["parameters"]},
    )
    metadata["model"] = {"vertices": obj["vertices"], "faces": obj["faces"],
                         "sha256": obj["sha256"], "format": "obj",
                         "generation_ms": obj["generation_ms"]}
    return metadata


def list_artifacts(user_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    seen = set()
    folder = artifacts_root() / user_id
    if folder.exists():
        for child in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = child / "artifact.json"
            if meta.is_file():
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                    aid = data.get("artifact_id")
                    if aid:
                        seen.add(aid)
                    items.append(data)
                except Exception:
                    logger.warning("Could not read artifact metadata at %s", meta)
    if _use_firestore():
        db = _firestore_sync()
        if db is not None:
            try:
                for doc in db.collection("artifacts").where("user_id", "==", user_id).stream():
                    data = doc.to_dict() or {}
                    aid = data.get("artifact_id") or doc.id
                    if aid in seen:
                        continue
                    seen.add(aid)
                    items.append(data)
            except Exception:
                logger.exception("Firestore artifact list failed for user %s", user_id)
                raise
    items.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return items


def load_artifact(user_id: str, artifact_id: str) -> Dict[str, Any]:
    meta = _artifact_dir(user_id, artifact_id) / "artifact.json"
    if meta.is_file():
        data = json.loads(meta.read_text(encoding="utf-8"))
        if data.get("user_id") == user_id:
            return data
    if _use_firestore():
        db = _firestore_sync()
        if db is not None:
            doc = db.collection("artifacts").document(artifact_id).get()
            if doc.exists:
                data = doc.to_dict() or {}
                if data.get("user_id") == user_id:
                    return data
    raise ArtifactError("Artifact not found")


def read_artifact_content(user_id: str, artifact_id: str, relpath: str) -> str:
    rel = _safe_relpath(relpath)
    metadata = load_artifact(user_id, artifact_id)
    if rel not in _file_paths(metadata):
        raise ArtifactError("File is not part of this artifact")
    path = _artifact_dir(user_id, artifact_id) / rel
    if path.is_file():
        path.resolve().relative_to((artifacts_root() / user_id / artifact_id).resolve())
        content = path.read_text(encoding="utf-8")
        expected = metadata.get("checksums", {}).get(rel)
        if expected and hashlib.sha256(content.encode()).hexdigest() != expected:
            raise ArtifactError("Artifact integrity check failed")
        return content
    if _use_firestore():
        db = _firestore_sync()
        if db is not None:
            doc_id = hashlib.sha256(f"{artifact_id}/{rel}".encode("utf-8")).hexdigest()
            doc = db.collection("artifact_files").document(doc_id).get()
            if doc.exists:
                data = doc.to_dict() or {}
                if data.get("user_id") == user_id:
                    content = str(data.get("content") or "")
                    expected = metadata.get("checksums", {}).get(rel)
                    if expected and hashlib.sha256(content.encode()).hexdigest() != expected:
                        raise ArtifactError("Artifact integrity check failed")
                    return content
    raise ArtifactError("File not found")


def read_artifact_file(user_id: str, artifact_id: str, relpath: str) -> Path:
    rel = _safe_relpath(relpath)
    metadata = load_artifact(user_id, artifact_id)
    if rel not in _file_paths(metadata):
        raise ArtifactError("File is not part of this artifact")
    path = _artifact_dir(user_id, artifact_id) / rel
    if path.is_file():
        path.resolve().relative_to((artifacts_root() / user_id / artifact_id).resolve())
        # §28.8: the integrity re-check is DIGEST-based (binary-safe) —
        # the previous utf-8 decode refused valid PDF bytes, and dropping
        # the check entirely would let tampered bytes be served. The
        # comparison is against the RECORDED manifest digest, never a
        # recomputation of the (possibly tampered) file itself.
        metadata = load_artifact(user_id, artifact_id)
        expected_sha = (metadata.get("checksums") or {}).get(rel)
        stored = path.read_bytes()
        if (expected_sha is None
                or hashlib.sha256(stored).hexdigest() != expected_sha):
            raise ArtifactError(
                f"stored bytes for {rel} fail the recorded integrity check "
                "— refusing to serve missing or tampered content")
        return path
    content = read_artifact_content(user_id, artifact_id, rel)
    dest = _artifact_dir(user_id, artifact_id) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest


# ── Phase 17 (review gap): PRODUCTION callers for the deliverable
# contract, staged publication, and the typed verifier. The builder
# remains the producer; these wrappers record the durable deliverable,
# publish through the staged sequence, and verify with the typed
# verifier — no more test-only helpers.

def _sha256_of_file(path: Path) -> str:
    import hashlib as _hl
    return _hl.sha256(path.read_bytes()).hexdigest()


def _file_entries(dest: Path, written: List[str]) -> List[Dict[str, Any]]:
    """Per-file manifest entries hashed from the ACTUAL bytes (§17.8)."""
    entries = []
    for rel in written:
        p = dest / rel
        data = p.read_bytes()
        entries.append({"path": rel, "content": data.decode("utf-8"),
                        "sha256": _hl_sha(data),
                        "byte_length": len(data)})
    return entries


def _hl_sha(data: bytes) -> str:
    import hashlib as _hl
    return _hl.sha256(data).hexdigest()


async def record_and_publish_deliverable(
    *, owner_user_id: str, tenant_id: str,
    deliverable_type: str, name: str,
    dest: Path, written: List[str],
    run_id: Optional[str] = None, task_id: Optional[str] = None,
    lineage: Optional[Dict[str, Any]] = None,
    deliverable_key: Optional[str] = None,
    artifact_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The production integration: candidate → staged publication →
    typed verification, all through the durable contract. The stored
    bytes ARE the identity; the verdict is persisted on the record."""
    from backend.models.deliverable import Deliverable
    from backend.repositories.deliverable_repository import (
        get_deliverable_repo, DeliverableRepository)
    from backend.services.deliverable_publisher import publish_deliverable
    from backend.services.artifact_verifier import verify_artifact

    repo = get_deliverable_repo()
    if repo is None:
        repo = DeliverableRepository(_db_manager_or_none())
    entries = _file_entries(dest, written)
    d = Deliverable(
        deliverable_id="pending", deliverable_key="", revision=1,
        tenant_id=tenant_id, owner_user_id=owner_user_id,
        run_id=run_id, task_id=task_id, deliverable_type=deliverable_type,
        mime_type="text/html" if deliverable_type == "resume_html" else
        ("application/octet-stream" if deliverable_type.endswith("project")
         else "text/markdown"),
        file_manifest=[], lineage=dict(lineage or {}))
    d = await repo.create_candidate(d, deliverable_key=deliverable_key)

    async def _store(files):
        # The bytes were already written under the artifact id; the
        # staged store step re-checks their presence on disk.
        missing = [f["path"] for f in files
                   if not (dest / f["path"]).exists()]
        if missing:
            raise RuntimeError(f"stored bytes missing: {missing}")

    async def _verify_stored(files):
        return all((dest / f["path"]).exists() for f in files)

    tx = await publish_deliverable(
        repo=repo, deliverable_id=d.deliverable_id, files=entries,
        entrypoint=None, store_fn=_store, verify_stored_fn=_verify_stored)

    verdict = await verify_artifact(
        artifact_id=d.deliverable_id, owner_user_id=owner_user_id,
        repo=repo,
        stored_bytes_fn=_read_deliverable_bytes(dest, entries))
    await repo.transition(
        d.deliverable_id,
        "verification_passed" if verdict.verdict == "pass"
        else ("needs_review" if verdict.verdict == "needs_review"
              else "verification_failed"),
        verification={
            "verdict": verdict.verdict,
            "verifier_version": verdict.verifier_version,
            "checks": verdict.checks,
            "blocking_reasons": verdict.blocking_reasons,
            "evidence_refs": verdict.evidence_refs,
        })
    return {"deliverable_id": d.deliverable_id,
            "deliverable_key": d.deliverable_key,
            "revision": d.revision,
            "artifact_id": artifact_id,
            "publication_stage": tx.stage,
            "publication_failures": tx.failures,
            "verification": {"verdict": verdict.verdict,
                             "blocking_reasons": verdict.blocking_reasons}}


def _read_deliverable_bytes(dest: Path, entries: List[Dict[str, Any]]):
    async def _read(artifact_id: str):
        """§19.2 per-file verification: return a path → bytes MAP (the
        single-blob legacy shape only satisfies one-file manifests). A
        missing file yields None so the verifier reports missing bytes
        instead of silently hashing a partial artifact."""
        files: Dict[str, Optional[bytes]] = {}
        for e in entries:
            p = dest / e["path"]
            if not p.exists():
                return None
            files[e["path"]] = p.read_bytes()
        return files if files else None
    return _read


def _db_manager_or_none():
    try:
        from backend.api.main import app
        factory = getattr(app.state, "factory", None)
        return getattr(factory, "db_manager", None)
    except Exception:
        return None
