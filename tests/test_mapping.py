import numpy as np

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
