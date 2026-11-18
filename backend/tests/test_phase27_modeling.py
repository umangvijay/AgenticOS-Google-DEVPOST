"""Phase 27 §27.15 (27.D10) — real model-asset generation + validation.

A decorative scene is not modeling output; these tests prove a
parameterized asset generates, validates, and round-trips.
"""
import hashlib

import pytest

from backend.services.modeling_service import (
    ModelParameters, ModelingError, build_mesh, export_obj, validate_asset,
)


def test_box_generates_and_validates():
    obj = export_obj(ModelParameters(kind="box", size=(2, 3, 4),
                                     name="crate"))
    assert obj["vertices"] == 8 and obj["faces"] == 6
    check = validate_asset(obj["content"])
    assert check["valid"] is True
    assert check["sha256"] == obj["sha256"]


def test_sphere_resolution_bounded():
    obj = export_obj(ModelParameters(kind="sphere", size=(2, 2, 2),
                                     segments=16, name="orb"))
    assert obj["vertices"] > 100
    assert validate_asset(obj["content"])["valid"]


def test_assembly_combines_parts():
    params = ModelParameters(kind="assembly", name="stack", parts=[
        {"kind": "box", "size": [1, 1, 1], "position": [0, 0, 0.5]},
        {"kind": "cylinder", "size": [1, 1, 2], "segments": 12,
         "position": [0, 0, 2]},
    ])
    obj = export_obj(params)
    assert obj["vertices"] > 20
    assert validate_asset(obj["content"])["valid"]


@pytest.mark.parametrize("bad", [
    {"kind": "torus"},
    {"kind": "box", "size": (0, 1, 1)},
    {"kind": "box", "size": (float("nan"), 1, 1)},
    {"kind": "sphere", "segments": 2},
])
def test_invalid_parameters_refused(bad):
    with pytest.raises(ModelingError):
        export_obj(ModelParameters(**bad))


def test_validator_rejects_corrupt_assets():
    good = export_obj(ModelParameters(kind="box", name="x"))
    with pytest.raises(ModelingError):
        validate_asset(good["content"] + b"v not-a-number\n")
    with pytest.raises(ModelingError):
        validate_asset(b"")
    with pytest.raises(ModelingError):
        validate_asset("vértex".encode("utf-8"))


def test_deterministic_export_is_reproducible():
    a = export_obj(ModelParameters(kind="box", size=(1, 1, 1), name="r"))
    b = export_obj(ModelParameters(kind="box", size=(1, 1, 1), name="r"))
    assert a["sha256"] == b["sha256"], "same parameters, same bytes"
