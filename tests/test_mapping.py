import types

import numpy as np
import pytest

from v2l import mapping as Mp
from v2l.mapping import Entry, apply, resolve


def probe(**params):
    return {"params": params, "min_motion": 0.5}


def test_reversed_when_parameter_moves_the_other_way_on_screen():
    # VTube Studio file maps EyeRightX -1..1 to EyeBallX 1..-1, but +EyeBallX draws the eyes to
    # the image right, so +gaze (image right) must produce +EyeBallX.
    e = Entry("EyeRightX", "ParamEyeBallX", -1, 1, 1, -1)
    fixed, notes = resolve([e], probe(ParamEyeBallX={"visible": True, "dx": 12.0, "dy": 0.0, "rot": 0.0}))
    out = apply(fixed, {"EyeRightX": np.array([1.0])}, 1, 30.0)
    assert out["ParamEyeBallX"][0] == 1.0
    assert notes


def test_kept_when_directions_agree_or_probe_is_unknown():
    e = Entry("FaceAngleX", "ParamAngleX", -30, 30, -60, 60)
    agree, _ = resolve([e], probe(ParamAngleX={"visible": True, "dx": 30.0}))
    unknown, _ = resolve([e], None)
    tiny, _ = resolve([e], probe(ParamAngleX={"visible": True, "dx": -0.1}))
    for fixed in (agree, unknown, tiny):
        assert (fixed[0].out_lo, fixed[0].out_hi) == (-60, 60)


def test_vertical_uses_screen_up_and_rotation_uses_clockwise():
    up = Entry("FaceAngleY", "ParamAngleY", -30, 30, -30, 30)
    rot = Entry("FaceAngleZ", "ParamAngleZ", -30, 30, -30, 30)
    p = probe(ParamAngleY={"visible": True, "dy": 8.0}, ParamAngleZ={"visible": True, "rot": -5.0})
    fixed, _ = resolve([up, rot], p)  # +param moves the drawing down / counter-clockwise
    assert (fixed[0].out_lo, fixed[0].out_hi) == (30, -30)
    assert (fixed[1].out_lo, fixed[1].out_hi) == (30, -30)


def test_eye_input_follows_the_side_the_parameter_draws():
    # EyeOpenRight is the performer's right eye = image left. A rig whose ParamEyeLOpen draws on
    # the image right must be fed from EyeOpenLeft instead.
    e = Entry("EyeOpenRight", "ParamEyeLOpen", 0, 1, 0, 2)
    fixed, _ = resolve([e], probe(ParamEyeLOpen={"visible": True, "side": "right"}))
    assert fixed[0].input == "EyeOpenLeft"
    same, _ = resolve([e], probe(ParamEyeLOpen={"visible": True, "side": "left"}))
    assert same[0].input == "EyeOpenRight"


def test_body_fed_from_face_angle_takes_the_body_signal():
    e = Entry("FaceAngleX", "ParamBodyAngleX", -30, 30, -10, 10)
    fixed, _ = resolve([e], None)
    assert fixed[0].input == "BodyAngleX"


def test_apply_ranges_clamps_and_breathing():
    entries = [
        Entry("MouthSmile", "ParamCheek", 0, 1, 0, 0.3, clamp_in=True, clamp_out=True),
        Entry("", "ParamBreath", 0, 1, 0, 1, breathing=True),
    ]
    out = apply(entries, {"MouthSmile": np.array([0.5, 2.0, -1.0])}, 3, 30.0)
    assert np.allclose(out["ParamCheek"], [0.15, 0.3, 0.0])
    assert out["ParamBreath"].min() >= 0 and out["ParamBreath"].max() <= 1


# ------------------------------------------------------------------------------- overrides

RANGES = {"ParamAngleX": {"min": -30.0, "max": 30.0, "def": 0.0},
          "ParamAngleZ": {"min": -30.0, "max": 30.0, "def": 0.0},
          "ParamMouthOpenY": {"min": 0.0, "max": 1.0, "def": 0.0},
          "ParamBodyAngleX": {"min": -10.0, "max": 10.0, "def": 0.0},
          "ParamHairFront": {"min": -1.0, "max": 1.0, "def": 0.0},
          "ParamHairBack": {"min": -1.0, "max": 1.0, "def": 0.0}}


def model(probe_params=None):
    return types.SimpleNamespace(vtube=None, ranges=RANGES, physics_outputs={"ParamHairBack"},
                                 probe={"params": probe_params or {}, "min_motion": 0.5, "min_rot": 0.01})


def eff(data, probe_params=None):
    entries, notes, warns = Mp.effective(model(probe_params), {"format": "v2l.mapping", "version": 1,
                                                               "entries": data})
    return {e.output: e for e in entries}, notes, warns


def test_override_patches_by_output_and_inherits_omitted_fields():
    base = {e.output: e for e in Mp.entries_for(model())}
    got, _, _ = eff([{"output": "ParamAngleX", "in": [-20, 20], "clamp_out": False}])
    e = got["ParamAngleX"]
    assert (e.in_lo, e.in_hi) == (-20, 20) and e.clamp_out is False
    assert e.input == base["ParamAngleX"].input and (e.out_lo, e.out_hi) == (-30, 30) and e.clamp_in is True
    assert e.source == "override" and e.overridden == ["in", "clamp_out"]
    assert got["ParamAngleZ"].source == "fallback"


def test_added_entry_defaults_to_the_input_and_parameter_ranges():
    got, _, _ = eff([{"output": "ParamHairFront", "input": "FaceAngleX"}])
    e = got["ParamHairFront"]
    assert e.source == "added" and (e.in_lo, e.in_hi) == (-45.0, 45.0) and (e.out_lo, e.out_hi) == (-1.0, 1.0)
    out = apply([e], {"FaceAngleX": np.array([45.0, 0.0, -90.0])}, 3, 30.0)["ParamHairFront"]
    assert np.allclose(out, [1.0, 0.0, -1.0])


def test_disabled_entries_produce_no_curve():
    got, _, _ = eff([{"output": "ParamMouthOpenY", "enabled": False}])
    assert got["ParamMouthOpenY"].enabled is False
    out = apply(list(got.values()), {"MouthOpen": np.array([0.5])}, 1, 30.0)
    assert "ParamMouthOpenY" not in out


def test_invert_mirrors_after_the_probe_correction():
    rot = {"ParamAngleZ": {"visible": True, "rot": -0.2}}  # +param turns the drawing counter-clockwise
    plain, notes, _ = eff([], rot)
    assert (plain["ParamAngleZ"].out_lo, plain["ParamAngleZ"].out_hi) == (30, -30) and notes
    inv, _, _ = eff([{"output": "ParamAngleZ", "invert": True}], rot)
    e = inv["ParamAngleZ"]
    assert (e.out_lo, e.out_hi) == (30, -30) and Mp.applied_out(e) == (-30, 30)
    a = apply([plain["ParamAngleZ"]], {"FaceAngleZ": np.array([15.0])}, 1, 30.0)["ParamAngleZ"]
    b = apply([e], {"FaceAngleZ": np.array([15.0])}, 1, 30.0)["ParamAngleZ"]
    assert a[0] == -b[0] != 0
    # Writing the shown (corrected) range back as an override is stable.
    again, _, _ = eff([{"output": "ParamAngleZ", "out": [30, -30], "invert": True}], rot)
    assert Mp.applied_out(again["ParamAngleZ"]) == (-30, 30)


def test_entry_curve_maps_the_normalized_input():
    curve = {"points": [[0, 0], [0.5, 0.1], [1, 1]], "smooth": False}
    got, _, _ = eff([{"output": "ParamMouthOpenY", "curve": curve, "cutoff": 4.0}])
    e = got["ParamMouthOpenY"]
    assert e.cutoff == 4.0 and Mp.entry_json(e)["curve"] == curve
    out = apply([e], {"MouthOpen": np.array([0.0, 0.25, 0.5, 0.75, 1.0])}, 5, 30.0)["ParamMouthOpenY"]
    assert np.allclose(out, [0.0, 0.05, 0.1, 0.55, 1.0])
    ident, _, _ = eff([{"output": "ParamMouthOpenY", "curve": {"points": [[0, 0], [1, 1]], "smooth": True}}])
    u = np.array([0.0, 0.3, 1.0])
    assert np.allclose(apply([ident["ParamMouthOpenY"]], {"MouthOpen": u}, 3, 30.0)["ParamMouthOpenY"], u)


def test_input_curves_are_normalized_over_the_nominal_range():
    data = {"input_curves": {"yaw": {"points": [[0, 0], [0.5, 0.5], [0.75, 1], [1, 1]], "smooth": False},
                             "EyeWideLeft": {"points": [[0, 0], [0.6, 1], [1, 1]], "smooth": False}}}
    got = Mp.apply_input_curves(data, {"yaw": np.array([-45.0, 0.0, 22.5, 45.0]),
                                       "EyeWideLeft": np.array([0.3, 0.6, 0.9, 0.0]),
                                       "GazeX": np.array([0.1, 0.2, 0.3, 0.4])})
    assert np.allclose(got["yaw"], [-45, 0, 45, 45])           # yaw nominal range -45..45 maps onto 0..1
    assert np.allclose(got["EyeWideLeft"], [0.5, 1.0, 1.0, 0.0])
    assert np.allclose(got["GazeX"], [0.1, 0.2, 0.3, 0.4])     # no curve: unchanged


def test_breathing_override_and_explicit_input_skips_body_substitution():
    got, _, _ = eff([{"output": "ParamHairFront", "breathing": True},
                     {"output": "ParamBodyAngleX", "input": "FaceAngleX"}])
    assert got["ParamHairFront"].breathing and got["ParamHairFront"].source == "added"
    assert got["ParamBodyAngleX"].input == "FaceAngleX"


@pytest.mark.parametrize("entry, message", [
    ({"output": "ParamNope", "input": "MouthOpen"}, "unknown parameter 'ParamNope'"),
    ({"output": "ParamHairFront"}, "must name an input"),
    ({"output": "ParamAngleX", "in": [5, 5]}, "in range is empty"),
    ({"output": "ParamAngleX", "out": [0]}, "out must be [low, high]"),
    ({"output": "ParamAngleX", "input": "FaceAngleQ"}, "unknown input 'FaceAngleQ'"),
    ({"output": "ParamAngleX", "curve": {"points": [[0, 0], [0.5, 1]]}}, "must start at x = 0 and end at x = 1"),
    ({"output": "ParamAngleX", "cutoff": 0}, "cutoff must be null"),
    ({"output": "ParamAngleX", "input": "MyEar"}, "unknown input 'MyEar'"),
    ({"output": "ParamAngleX", "flip": True}, "unknown field flip"),
])
def test_invalid_overrides_are_rejected_with_clear_errors(entry, message):
    with pytest.raises(Mp.MappingError) as e:
        eff([entry])
    assert any(message in p for p in e.value.problems)


def test_physics_outputs_are_flagged():
    _, _, warns = eff([{"output": "ParamHairBack", "input": "FaceAngleX"}])
    assert any("physics" in w for w in warns)


def full(**sections):
    return {"format": "v2l.mapping", "version": 1, **sections}


@pytest.mark.parametrize("sections, message", [
    ({"input_curves": {"FaceAngleX": {"points": [[0, 0], [1, 1]]}}}, "is not a tracking input"),
    ({"input_curves": {"yaw": {"points": [[0, 0], [0.5, 0.2], [0.4, 0.3], [1, 1]]}}}, "strictly increasing"),
    ({"bridge": [{"name": "MouthOpen", "func": "LipOpen *"}]}, "cannot parse formula"),
    ({"bridge": [{"name": "Ear", "func": "GazeX"}]}, "needs func (or delayBuffer), min and max"),
    ({"bridge": [{"name": "Ear", "min": -1, "max": 1, "defaultValue": 0,
                  "delayBuffer": {"refParam": "FaceAngleX", "smoothing": 2}}]}, "delayBuffer: missing delayCount"),
    ({"bridge": [{"name": "MouthOpen", "modifiers": {"steps": [{"trigger": 0.6, "target": 1},
                                                               {"trigger": 0.3, "target": 0.5}]}}]},
     "triggers must be strictly increasing"),
    ({"bridge": [{"name": "A", "min": 0, "max": 1, "defaultValue": 0, "delayBuffer": {
        "refParam": "B", "smoothing": 1, "delayCount": 1, "inMin": 0, "inMax": 1, "outMin": 0, "outMax": 1}},
                 {"name": "B", "min": 0, "max": 1, "defaultValue": 0, "delayBuffer": {
        "refParam": "A", "smoothing": 1, "delayCount": 1, "inMin": 0, "inMax": 1, "outMin": 0, "outMax": 1}}]},
     "cycle"),
])
def test_invalid_input_curves_and_bridge_sections(sections, message):
    errs, _ = Mp.check(full(**sections))
    assert any(message in e for e in errs), errs


def test_entries_may_use_bridge_outputs_the_mapping_adds():
    data = full(bridge=[{"name": "MyEar", "func": "GazeX * 2", "min": -1, "max": 1}],
                entries=[{"output": "ParamHairFront", "input": "MyEar"}])
    got = {e.output: e for e in Mp.effective(model(), data)[0]}
    e = got["ParamHairFront"]
    assert e.source == "added" and (e.in_lo, e.in_hi) == (-1.0, 1.0)
