"""Phase 27 §27.15 Part B — real 3D model-asset generation (bounded scope).

Parameterized scenes (boxes/spheres/prism assemblies) exported as
Wavefront OBJ — a plain-text, universally readable format requiring no
native dependencies. A job is: parameter set → mesh construction →
validation → artifact bytes. The decorative CyanotypeScene is NOT part
of this pipeline; a rotating decorative object is not modeling output.

Jobs run in-process with hard bounds (finite geometry, vertex/face
caps, byte cap). Cancellation and durable progress ride the artifact
store's publication path.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

MAX_VERTICES = 200_000
MAX_FACES = 200_000
MAX_OUTPUT_BYTES = 32 * 1024 * 1024

ModelingError = ValueError


KNOWN_UNITS = ("mm", "cm", "m", "in")
KNOWN_MATERIALS = ("generic", "plastic", "aluminium", "steel", "wood", "resin")


@dataclass
class ModelParameters:
    """Bounded, versioned parameter set for one asset. §28.9: the spec
    carries DIMENSIONS WITH UNITS and a material declaration — the
    exported metadata records both so a downloaded asset can be measured
    against the requested dimensions."""
    kind: str                       # "box" | "sphere" | "cylinder" | "assembly"
    size: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    segments: int = 16              # sphere/cylinder resolution
    parts: List[Dict[str, Any]] = field(default_factory=list)  # assembly
    name: str = "asset"
    units: str = "mm"               # KNOWN_UNITS — declared, recorded
    material: str = "generic"       # KNOWN_MATERIALS — declared, recorded

    def validate(self) -> None:
        if self.kind not in ("box", "sphere", "cylinder", "assembly"):
            raise ModelingError(f"unsupported model kind: {self.kind}")
        for v in self.size:
            if not math.isfinite(v) or v <= 0 or v > 100:
                raise ModelingError("sizes must be finite and within (0, 100]")
        if not (3 <= self.segments <= 256):
            raise ModelingError("segments must be within [3, 256]")
        if self.units not in KNOWN_UNITS:
            raise ModelingError(
                f"units must be one of {KNOWN_UNITS}, got {self.units!r}")
        if self.material not in KNOWN_MATERIALS:
            raise ModelingError(
                f"material must be one of {KNOWN_MATERIALS}, "
                f"got {self.material!r}")


def _box(size) -> Tuple[List, List]:
    w, h, d = size
    hx, hy, hz = w / 2, h / 2, d / 2
    verts = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
             (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    faces = [[1, 4, 3, 2], [5, 6, 7, 8], [1, 2, 6, 5], [2, 3, 7, 6],
             [3, 4, 8, 7], [4, 1, 5, 8]]
    return verts, faces


def _sphere(radius: float, segments: int) -> Tuple[List, List]:
    verts, faces = [], []
    rings = max(4, segments)
    cols = max(4, segments * 2)
    for i in range(rings + 1):
        theta = math.pi * i / rings
        for j in range(cols):
            phi = 2 * math.pi * j / cols
            verts.append((radius * math.sin(theta) * math.cos(phi),
                          radius * math.sin(theta) * math.sin(phi),
                          radius * math.cos(theta)))
    for i in range(rings):
        for j in range(cols):
            a = i * cols + j + 1
            b = i * cols + (j + 1) % cols + 1
            c = (i + 1) * cols + (j + 1) % cols + 1
            d = (i + 1) * cols + j + 1
            if i != 0:
                faces.append([a, b, c])
            if i != rings - 1:
                faces.append([a, c, d])
    return verts, faces


def _cylinder(radius: float, height: float, segments: int) -> Tuple[List, List]:
    verts, faces = [], []
    h = height / 2
    for sign in (1, -1):
        for j in range(segments):
            phi = 2 * math.pi * j / segments
            verts.append((radius * math.cos(phi), radius * math.sin(phi),
                          sign * h))
    top_start = 1
    bot_start = segments + 1
    verts.append((0, 0, h))
    verts.append((0, 0, -h))
    # §29.33 revision: the centers were appended TOP then BOTTOM, so the
    # LAST vertex (1-based len(verts)) is the BOTTOM center and the
    # second-to-last is the TOP center. The previous assignment reversed
    # them, crossing every cap fan through the opposite pole.
    top_center = len(verts) - 1
    bot_center = len(verts)
    for j in range(segments):
        a, b = top_start + j, top_start + (j + 1) % segments
        c, d = bot_start + j, bot_start + (j + 1) % segments
        faces.append([a, b, top_center])
        faces.append([d, c, bot_center])
        faces.append([a, c, d, b])
    return verts, faces


def build_mesh(params: ModelParameters) -> Tuple[List, List]:
    params.validate()
    if params.kind == "box":
        return _box(params.size)
    if params.kind == "sphere":
        v, f = _sphere(min(params.size) / 2, params.segments)
        if len(v) > MAX_VERTICES or len(f) > MAX_FACES:
            raise ModelingError("mesh exceeds the resource cap")
        return v, f
    if params.kind == "cylinder":
        v, f = _cylinder(min(params.size[:2]) / 2, params.size[2],
                         params.segments)
        if len(v) > MAX_VERTICES or len(f) > MAX_FACES:
            raise ModelingError("mesh exceeds the resource cap")
        return v, f
    # assembly: transform part meshes by declared position/scale
    verts: List = []
    faces: List = []
    if not params.parts or len(params.parts) > 16:
        raise ModelingError("assembly requires 1–16 parts")
    for part in params.parts:
        sub = ModelParameters(
            kind=part.get("kind", "box"),
            size=tuple(part.get("size", params.size)),
            segments=int(part.get("segments", params.segments)),
        )
        sub.validate()
        pv, pf = build_mesh(sub)
        ox, oy, oz = part.get("position", [0, 0, 0])
        base = len(verts)
        verts.extend((x + ox, y + oy, z + oz) for x, y, z in pv)
        faces.extend([[i + base for i in face] for face in pf])
        if len(verts) > MAX_VERTICES or len(faces) > MAX_FACES:
            raise ModelingError("assembly exceeds the resource cap")
    return verts, faces


def export_obj(params: ModelParameters) -> Dict[str, Any]:
    """Parameters → validated OBJ bytes + metadata (the job product)."""
    started = time.time()
    verts, faces = build_mesh(params)
    for x, y, z in verts:
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            raise ModelingError("non-finite geometry produced")
    # §J24/§29.33 deterministic re-export: the OBJ BYTES are a pure
    # function of the parameters — no wall-clock, uuid or other volatile
    # value may enter the hashed content (a `# generated <unix-time>`
    # comment previously made identical parameters hash differently
    # across second boundaries; generation_ms stays in METADATA, never
    # in the bytes).
    lines = [f"# AgentOS model asset: {params.name}",
             f"# deterministic export {params.kind} units={params.units}"]
    lines.extend(f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in verts)
    lines.extend("f " + " ".join(str(i) for i in face) for face in faces)
    data = "\n".join(lines).encode()
    if len(data) > MAX_OUTPUT_BYTES:
        raise ModelingError("export exceeds the byte cap")
    obj = {
        "name": f"{params.name}.obj",
        "content": data,
        "format": "obj",
        "vertices": len(verts),
        "faces": len(faces),
        "parameters": {"kind": params.kind, "size": list(params.size),
                       "segments": params.segments,
                       "parts": params.parts,
                       "units": params.units,
                       "material": params.material},
        "generation_ms": int((time.time() - started) * 1000),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return obj


def validate_asset(data: bytes) -> Dict[str, Any]:
    """Independent validation of exported bytes (readable format, finite
    geometry, bounded size) — the model_asset_verifier contract.
    §29.33 revision: index validation is TWO-PASS — every face reference
    is checked against the FULL vertex count regardless of line order,
    so an early face reference to an unresolved vertex is rejected (the
    previous guard was conditional on vertices already seen)."""
    if not data or len(data) > MAX_OUTPUT_BYTES:
        raise ModelingError("asset missing or exceeds the byte cap")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ModelingError("asset is not readable ASCII OBJ") from exc
    verts = faces = 0
    face_refs: List[int] = []
    for line in text.splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) != 4:
                raise ModelingError("malformed vertex line")
            for raw in parts[1:]:
                if not math.isfinite(float(raw)):
                    raise ModelingError("non-finite vertex")
            verts += 1
        elif line.startswith("f "):
            idx = line.split()[1:]
            if len(idx) < 3:
                raise ModelingError("degenerate face")
            for raw in idx:
                try:
                    face_refs.append(int(raw))
                except ValueError:
                    raise ModelingError("malformed face index")
            faces += 1
    # Second pass: EVERY reference must resolve against the complete
    # vertex set — order-independent.
    for i in face_refs:
        if not 1 <= i <= verts:
            raise ModelingError("face references an undefined vertex")
    if verts < 1 or faces < 1:
        raise ModelingError("asset has no geometry")
    return {"vertices": verts, "faces": faces,
            "sha256": hashlib.sha256(data).hexdigest(), "valid": True}


def new_job_id() -> str:
    return f"model-{uuid.uuid4().hex[:12]}"


def serialize_metadata(obj: Dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in obj.items() if k != "content"},
                      indent=2)
