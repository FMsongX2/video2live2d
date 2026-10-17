import numpy as np
import pytest

from v2l.bridge import Bridge
from v2l.calibrate import calibrate, neutral_window
from v2l.config import CalibrationConfig

BS = ["mouthSmileLeft", "mouthSmileRight", "eyeBlinkLeft", "eyeBlinkRight", "browInnerUp", "browOuterUpLeft",
      "browOuterUpRight", "browDownLeft", "browDownRight", "jawOpen", "eyeWideLeft", "eyeWideRight",
      "mouthPucker", "mouthFunnel", "mouthDimpleLeft", "mouthDimpleRight", "mouthClose",
      "mouthStretchLeft", "mouthStretchRight"]


def synthetic(n=300, fps=30.0, rest=(120, 150)):
    """Performer smiling and talking everywhere except a quiet stretch at frames rest[0]:rest[1]."""
    t = np.arange(n) / fps
    rng = np.random.default_rng(1)
    active = np.ones(n)
    active[rest[0]:rest[1]] = 0.0
    f = {f"bs:{k}": np.full(n, 0.05) for k in BS}
    f["bs:mouthSmileLeft"] = f["bs:mouthSmileRight"] = 0.1 + 0.7 * active * (0.5 + 0.5 * np.sin(t * 3)) + 0.02 * rng.random(n)
    f["bs:browOuterUpLeft"] = f["bs:browOuterUpRight"] = 0.4 + 0.3 * active * rng.random(n)
    f["jawDrop"] = 0.78 * (1 + 0.12 * active * np.abs(np.sin(t * 9)))
    f["lipGap"] = 0.06 * active * np.abs(np.sin(t * 9))
    f["yaw"] = -30 + 5 * np.sin(t)  # turned toward a ring light
    f["pitch"] = np.zeros(n)
    f["roll"] = np.zeros(n)
    for side in ("A", "B"):
        f[f"ear{side}"] = np.full(n, 0.3)
        f[f"irisX{side}"] = np.zeros(n)
        f[f"irisY{side}"] = np.zeros(n)
    f["mouthWidth"] = np.full(n, 0.63)
    f["mouthShift"] = np.zeros(n)
    for k in ("shoulderRoll", "shoulderYaw", "shoulderY"):
        f[k] = np.zeros(n)
    f["face_ok"] = np.ones(n)
    f["pose_ok"] = np.ones(n)
    return f, t


def test_rest_window_lands_on_the_quiet_stretch():
    f, t = synthetic()
    a, b = neutral_window(f, t, 0.6)
    assert 120 <= a and b <= 150


def test_rest_mouth_stays_neutral_and_head_is_centered():
    f, t = synthetic()
    x, _ = calibrate(f, t, CalibrationConfig())
    assert np.all(x["MouthSmileLeft"][125:145] < 0.05)
    for key in ("JawOpen", "LipOpen", "LipWidth", "MouthClose"):
        assert np.allclose(x[key][125:145], 0)
    assert abs(np.median(x["yaw"])) < 1e-9


def test_dead_channel_is_not_inflated():
    f, t = synthetic()
    f["bs:cheekPuff"] = 0.001 + 0.004 * np.random.default_rng(2).random(len(t))
    x, _ = calibrate(f, t, CalibrationConfig())
    assert x["CheekPuff"].max() < 0.02


def test_frames_without_face_never_form_the_rest_window():
    f, t = synthetic()
    for k in list(f):
        if k not in ("face_ok", "pose_ok"):
            f[k] = f[k].copy()
            f[k][120:150] = np.nan
    f["face_ok"][120:150] = 0
    a, b = neutral_window(f, t, 0.6)
    assert b <= 120 or a >= 150
    x, _ = calibrate(f, t, CalibrationConfig())
    for key in ("JawOpen", "LipOpen", "LipWidth", "MouthClose", "MouthFunnel"):
        assert np.isnan(x[key][120:150]).all()


def test_explicit_rest_window_overrides_search():
    f, t = synthetic()
    x, cal = calibrate(f, t, CalibrationConfig(neutral_window=(0.0, 1.0)))
    assert cal["neutral_window_s"][0] == 0.0
    with pytest.raises(ValueError):
        f["face_ok"][:60] = 0
        calibrate(f, t, CalibrationConfig(neutral_window=(0.0, 1.0)))


def test_zero_shot_scores_drive_cheek_puff_and_tongue_out():
    f, t = synthetic()
    n = len(t)
    cheek = np.full(n, -0.03)
    cheek[40:80] = 0.05            # clearly above the calibrated "full" threshold
    tongue = np.full(n, -0.04)
    tongue[200:230] = 0.09
    f["rare:cheek"], f["rare:tongue"] = cheek, tongue
    f["bs:cheekPuff"] = np.full(n, 0.001)
    x, _ = calibrate(f, t, CalibrationConfig())
    assert np.all(x["CheekPuff"][45:75] > 0.95) and np.all(x["CheekPuff"][100:200] < 0.05)
    assert np.all(x["TongueOut"][205:225] > 0.95) and x["TongueOut"][:190].max() < 0.05


def test_without_zero_shot_scores_tongue_out_is_not_invented():
    f, t = synthetic()
    x, _ = calibrate(f, t, CalibrationConfig())
    assert "TongueOut" not in x


def neutral_mouth_case():
    f, t = synthetic(n=120, rest=(0, 120))
    for key in f:
        if key.startswith("bs:"):
            f[key] = np.zeros(len(t))
    return f, t, CalibrationConfig(neutral_window=(0, 1))


def test_lips_can_part_without_opening_the_jaw():
    f, t, cfg = neutral_mouth_case()
    f["lipGap"][40:70] = cfg.lip_gap_full * 0.5
    x, _ = calibrate(f, t, cfg)
    outputs, _ = Bridge.load("default").evaluate(x)
    assert np.allclose(outputs["JawOpen"][40:70], 0)
    assert np.allclose(outputs["MouthOpen"][40:70], 0.5)


def test_lowered_jaw_can_keep_lips_sealed():
    f, t, cfg = neutral_mouth_case()
    f["jawDrop"][40:70] *= 1 + cfg.jaw_full
    f["bs:jawOpen"][40:70] = 0.8
    x, _ = calibrate(f, t, cfg)
    outputs, _ = Bridge.load("default").evaluate(x)
    assert np.all(outputs["JawOpen"][40:70] > 0.5)
    assert np.allclose(outputs["MouthOpen"][40:70], 0)
    assert np.all(x["MouthClose"][40:70] >= x["JawOpen"][40:70])


def test_tracker_lip_closure_is_preserved_independently_of_jaw():
    f, t, cfg = neutral_mouth_case()
    f["bs:mouthClose"][40:70] = 0.7
    x, _ = calibrate(f, t, cfg)
    assert np.all(x["MouthClose"][40:70] > 0.5)
    assert np.allclose(x["JawOpen"][40:70], 0)


def test_later_extreme_expression_does_not_rescale_earlier_mouth_motion():
    f, t, cfg = neutral_mouth_case()
    channels = ("jawOpen", "mouthPucker", "mouthFunnel", "mouthSmileLeft",
                "mouthSmileRight", "mouthStretchLeft", "mouthStretchRight")
    for key in channels:
        f["bs:" + key][40:60] = 0.2
    first, _ = calibrate(f, t, cfg)
    for key in channels:
        f["bs:" + key][80:100] = 0.95
    second, _ = calibrate(f, t, cfg)
    for key in ("JawOpen", "MouthClose", "LipWidth", "MouthPucker", "MouthFunnel",
                "MouthSmileLeft", "MouthSmileRight"):
        np.testing.assert_allclose(first[key][40:60], second[key][40:60])


@pytest.mark.parametrize("width_ratio, direction", [(0.88, -1), (1.15, 1)])
def test_width_geometry_does_not_fabricate_other_blendshapes(width_ratio, direction):
    f, t, cfg = neutral_mouth_case()
    f["mouthWidth"][40:70] *= width_ratio
    x, _ = calibrate(f, t, cfg)
    assert np.all(direction * x["LipWidth"][40:70] > 0)
    assert np.all(np.abs(x["LipWidth"][40:70]) < 1)
    for key in ("MouthDimpleLeft", "MouthDimpleRight", "MouthStretchLeft",
                "MouthStretchRight", "MouthPucker"):
        assert np.allclose(x[key][40:70], 0)


@pytest.mark.parametrize("direction", [-1, 1])
def test_agreeing_full_strength_cues_can_still_reach_extreme_poses(direction):
    f, t, cfg = neutral_mouth_case()
    f["jawDrop"][40:70] *= 1 + cfg.jaw_full
    f["bs:jawOpen"][40:70] = 1
    f["lipGap"][40:70] = cfg.lip_gap_full
    f["mouthWidth"][40:70] *= 0.88 if direction < 0 else 1.15
    if direction < 0:
        f["bs:mouthPucker"][40:70] = 1
    else:
        f["bs:mouthStretchLeft"][40:70] = 1
        f["bs:mouthStretchRight"][40:70] = 1
    x, _ = calibrate(f, t, cfg)
    outputs, _ = Bridge.load("default").evaluate(x)
    assert np.allclose(outputs["MouthPucker"][40:70], direction)
    assert np.allclose(outputs["MouthOpen"][40:70], 1)
    assert np.allclose(outputs["JawOpen"][40:70], 1)


def test_rounding_geometry_requires_parted_lips_and_survives_a_weak_tracker():
    f, t, cfg = neutral_mouth_case()
    f["mouthWidth"][40:90] *= 0.88
    f["lipGap"][65:90] = cfg.lip_gap_full * 0.5
    x, _ = calibrate(f, t, cfg)
    assert np.allclose(x["MouthFunnel"][40:65], 0)
    assert np.all((x["MouthFunnel"][65:90] > 0) & (x["MouthFunnel"][65:90] < 1))
