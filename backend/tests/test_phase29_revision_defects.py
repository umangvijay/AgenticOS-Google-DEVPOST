"""Phase 29 revision (Nov 13) — the three named concrete defects.

1. The browser preview's sanitized display URL must PRESERVE the
   explicit port, and the authoritative normalized origin travels
   separately (the previous rebuild via `hostname` silently moved
   :8443 to :443).
2. The cylinder's cap fans must converge on their OWN center vertices
   (the previous assignment swapped top/bottom center indices, crossing
   every cap through the opposite pole).
3. OBJ index validation is order-independent: a face reference that
   never resolves against the full vertex set is rejected even when the
   face line precedes the vertices.
"""
from __future__ import annotations

import math

import pytest

from backend.services.modeling_service import (
    ModelParameters, _cylinder, export_obj, validate_asset, ModelingError,
)


# ── 1. preview URL/origin ────────────────────────────────────────────

class _FakePage:
    def __init__(self, url):
        self.url = url

    def locator(self, _selector):
        return self

    async def screenshot(self, **kwargs):
        return b"\xff\xd8" + b"0" * 64  # minimal JPEG-ish bytes


@pytest.mark.asyncio
async def test_preview_preserves_explicit_port_and_reports_origin():
    from backend.services.web_agent import WebAgent
    preview = await WebAgent._capture_preview(
        _FakePage("https://changes.example.test:8443/app?token=secret#x"),
        step=3)
    assert ":8443" in preview["url"], \
        "the display URL must not silently move an explicit port to :443"
    assert "token" not in preview["url"], "query is stripped"
    assert preview["origin"] == "https://changes.example.test:8443", \
        "the authoritative normalized origin travels separately"
    assert preview["step"] == 3


@pytest.mark.asyncio
async def test_preview_default_port_still_normalizes():
    from backend.services.web_agent import WebAgent
    preview = await WebAgent._capture_preview(
        _FakePage("https://example.test:443/page?a=1"), step=1)
    assert preview["origin"] == "https://example.test:443"


# ── 2. cylinder cap centers ──────────────────────────────────────────

def test_cylinder_cap_fans_reference_their_own_centers():
    verts, faces = _cylinder(1.0, 2.0, 12)
    top_center = len(verts) - 1   # appended first of the two centers
    bot_center = len(verts)       # appended last
    assert verts[top_center - 1][2] == 1.0, "top center is the +h pole"
    assert verts[bot_center - 1][2] == -1.0, "bottom center is the -h pole"
    for face in faces:
        if len(face) == 3:
            center_z = verts[face[2] - 1][2]
            # A top-ring fan triangle (ring z=+1) must converge on +h;
            # a bottom-ring fan (ring z=-1) on -h.
            ring_z = verts[face[0] - 1][2]
            assert math.copysign(1, center_z) == math.copysign(1, ring_z), \
                "a cap fan crossed to the opposite pole"


def test_cylinder_geometry_imports_with_measured_dimensions():
    obj = export_obj(ModelParameters(
        kind="cylinder", size=(2.0, 2.0, 4.0), segments=24, name="cap-check"))
    report = validate_asset(obj["content"])
    assert report["valid"] is True
    zs = [float(line.split()[3]) for line in
          obj["content"].decode().splitlines() if line.startswith("v ")]
    assert abs((max(zs) - min(zs)) - 4.0) < 1e-6, "measured height matches"
    xs = [float(line.split()[1]) for line in
          obj["content"].decode().splitlines() if line.startswith("v ")]
    assert abs(max(xs) - 1.0) < 1e-6, "measured radius matches"


# ── 3. order-independent index validation ────────────────────────────

def test_unresolved_face_reference_rejected_regardless_of_order():
    face_first = b"f 1 2 9\nv 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
    face_last = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 2 9\n"
    for order, data in (("face-first", face_first), ("face-last", face_last)):
        with pytest.raises(ModelingError, match="undefined vertex"):
            validate_asset(data), order


def test_valid_face_before_vertices_still_accepted():
    data = b"f 1 2 3\nv 0 0 0\nv 1 0 0\nv 0 1 0\n"
    report = validate_asset(data)
    assert report["valid"] is True and report["faces"] == 1


def test_malformed_face_index_is_a_modeled_error():
    with pytest.raises(ModelingError, match="malformed face index"):
        validate_asset(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 xyz\n")
