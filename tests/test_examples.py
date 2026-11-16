#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Every example here, built by PartCAD.

Each part, sketch and assembly of every package is built, and the ones whose
size says whether they came out right are measured. What needs more than
PartCAD and a network skips where it is missing: the examples in 'container'
need a container runtime (they run in the plugin's image), and the board in
'pcb' needs KiCad to be built -- its hyperdrc checks do not, and run anyway.

Run it from the root of this repository, with a PartCAD that has sketchTypes
(0.10.25 or newer) installed: `pytest`. The first run downloads the plugin, its
programs, a font and two KiCad models, and builds PartCAD's sandbox.
"""

import asyncio
import math
import os
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PACKAGES = ["gears", "lattices", "airfoils", "text", "raster", "models", "csg", "sketches", "container", "pcb"]

# What cannot be built without more than PartCAD: a KiCad board needs KiCad.
NEEDS_KICAD = {("pcb", "part", "board")}


def _declared():
    """(package, kind, name) of every object every package declares."""
    found = []
    for package in PACKAGES:
        with open(os.path.join(ROOT, package, "partcad.yaml"), encoding="utf-8") as f:
            config = yaml.safe_load(f)
        for section, kind in (("parts", "part"), ("sketches", "sketch"), ("assemblies", "assembly")):
            for name in config.get(section) or {}:
                found.append((package, kind, name))
    return found


OBJECTS = _declared()


def _partcad():
    pc = pytest.importorskip("partcad")
    if not hasattr(pc, "Context"):
        pytest.skip("this PartCAD has no Context")
    return pc


def _docker():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def ctx():
    pc = _partcad()
    context = pc.Context(ROOT)
    if not hasattr(context.get_project("//pub/feature/cad/csgrs"), "get_sketch_type_config"):
        pytest.skip("this PartCAD has no sketchTypes; these examples need 0.10.25 or newer")
    return context


def _get(ctx, kind, path):
    return {"part": ctx.get_part, "sketch": ctx.get_sketch, "assembly": ctx.get_assembly}[kind](path)


def _measured(ctx, kind, path):
    """How big the object is, built: '{"bbox": [x0, y0, z0, x1, y1, z1], "volume": ...}'.

    A part or a sketch measures itself as it is built. An assembly records no
    measurements of its own: its box is its children's boxes where it puts them.
    """
    shape = _get(ctx, kind, path)
    assert shape is not None, "%s %s is not declared" % (kind, path)
    if kind == "assembly":
        assert asyncio.run(shape.get_wrapped(ctx)) is not None, shape.errors
        box = asyncio.run(shape.get_bounding_box_async(ctx))
        assert box, shape.errors
        return {"bbox": list(box)}
    measured = asyncio.run(shape.get_measurements_async(ctx))
    assert measured is not None and measured.get("bbox"), shape.errors
    return measured


def test_every_package_is_listed():
    """A package added to the repository is a package this test builds."""
    on_disk = sorted(
        d
        for d in os.listdir(ROOT)
        if os.path.isfile(os.path.join(ROOT, d, "partcad.yaml")) and d not in ("deps", "tests")
    )
    assert on_disk == sorted(PACKAGES)


@pytest.mark.parametrize("package, kind, name", OBJECTS, ids=["%s:%s" % (p, n) for p, _k, n in OBJECTS])
def test_every_example_builds(ctx, package, kind, name):
    if (package, kind, name) in NEEDS_KICAD:
        pytest.skip("a KiCad board is built by KiCad; its hyperdrc checks are tested below")
    if package == "container" and not _docker():
        pytest.skip("the examples in 'container' run in the plugin's image, and there is no container runtime here")
    measured = _measured(ctx, kind, "//%s:%s" % (package, name))
    lo, hi = measured["bbox"][:3], measured["bbox"][3:]
    assert all(b > a for a, b in zip(lo[:2], hi[:2])), measured["bbox"]
    if kind == "part" and (package, name) != ("models", "header"):
        assert measured["volume"] > 0


def _overlaps(ctx, path):
    """Which parts of the assembly share space: PartCAD's own check, run in its sandbox."""
    assembly = ctx.get_assembly(path)
    found = asyncio.run(assembly.get_interference_async(ctx))
    assert found is not None, assembly.errors
    # Both gears checked: a solid PartCAD could not ask about would be left out
    # of 'overlaps' and named here instead.
    assert found["parts"] == 2 and not found["unchecked"], found
    return found["overlaps"]


def test_the_gears_mesh(ctx):
    # The gear's tips on a 39 mm circle, the pinion's on a 21 mm one, each with
    # a tooth along +X as it is made.
    gear = _measured(ctx, "part", "//pub/feature/cad/csgrs:spur_gear;module=1.5,teeth=24,thickness=6.0,bore=6.0")
    pinion = _measured(ctx, "part", "//gears:pinion")
    assert gear["bbox"][3] == pytest.approx(19.5) and pinion["bbox"][3] == pytest.approx(10.5)
    # 'pair.assy' puts the pinion 27 mm along X -- closer than 19.5 + 10.5, so
    # the tip circles cross -- turned half a tooth: the box around the gear's
    # box and the pinion's box turned 15 degrees there.
    bbox = _measured(ctx, "assembly", "//gears:pair")["bbox"]
    turned = 10.5 * (math.cos(math.radians(15)) + math.sin(math.radians(15)))
    assert bbox[0] == pytest.approx(-19.5) and bbox[3] == pytest.approx(27 + turned)
    # There the teeth of each sit in the gaps of the other, and nothing overlaps;
    # not turned, a tooth of each is in the same place.
    assert _overlaps(ctx, "//gears:pair") == []
    clash = _overlaps(ctx, "//gears:pair;turn=0.0")
    assert len(clash) == 1 and clash[0]["volume"] > 1, clash


def test_a_parameter_sets_the_example(ctx):
    assert _measured(ctx, "part", "//gears:wheel;teeth=36")["bbox"][3] == pytest.approx(1.5 * 38 / 2)
    assert _measured(ctx, "part", "//csg:bracket;width=60.0")["bbox"][3] == pytest.approx(60)
    assert _measured(ctx, "sketch", "//sketches:plate;width=80.0")["bbox"][3] == pytest.approx(80)


def test_a_kicad_model_is_in_millimetres(ctx):
    # An 0805 resistor: 2.0 x 1.25 mm.
    bbox = _measured(ctx, "part", "//models:r0805")["bbox"]
    assert bbox[3] - bbox[0] == pytest.approx(2.0, abs=0.01)
    assert bbox[4] - bbox[1] == pytest.approx(1.2, abs=0.06)


def test_a_sketch_is_extruded_by_partcad(ctx):
    sketch = _measured(ctx, "sketch", "//sketches:plate")["bbox"]
    plate = _measured(ctx, "part", "//sketches:mounting_plate")
    assert plate["bbox"][:2] + plate["bbox"][3:5] == pytest.approx(sketch[:2] + sketch[3:5])
    assert plate["bbox"][5] - plate["bbox"][2] == pytest.approx(3)


def _pc():
    pc = os.path.join(os.path.dirname(sys.executable), "pc")
    if not os.path.isfile(pc):
        pytest.skip("no 'pc' command beside this interpreter")
    return pc


def test_the_board_passes_hyperdrc(ctx):
    result = subprocess.run(
        [_pc(), "--no-ansi", "qual", "test", "--other", "//pcb:board"], cwd=ROOT, capture_output=True, text=True
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0, log
    assert "Qualification failed" not in log
