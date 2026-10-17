import numpy as np
import pytest

from v2l.bridge import Bridge, BridgeError, Formula


def _env(n=4, **kw):
    base = {"yaw": np.zeros(n), "pitch": np.zeros(n), "roll": np.zeros(n)}
    base.update({k: np.asarray(v, dtype=float) for k, v in kw.items()})
    return base


def test_snenkbridge_syntax():
    env = {"A": np.array([1.0, -2.0]), "B": np.array([0.5, 0.25])}
    assert np.allclose(Formula("math::abs(A) * 2")(env), [2.0, 4.0])
    assert Formula("-2 ^ 2")(env) == -4.0  # power binds tighter than negation
    assert np.allclose(Formula("if(A > 0, B, 1 - B)")(env), [0.5, 0.75])
    assert np.allclose(Formula("if(A > 0 && B > 0.4, 1, 0)")(env), [1.0, 0.0])
    assert np.allclose(Formula("max(A, B, 0.3) // trailing comment")(env), [1.0, 0.3])
    assert np.allclose(Formula("/* note */ A + .5")(env), [1.5, -1.5])


@pytest.mark.parametrize("src", ["__import__('os')", "A.real", "[A]", "A if B else 1"])
def test_anything_but_arithmetic_is_rejected(src):
    with pytest.raises(BridgeError):
        Formula(src)({"A": np.ones(1), "B": np.ones(1)})


def test_missing_inputs_read_zero_and_are_reported():
    b = Bridge([{"name": "Out", "func": "JawOpen + TongueOut", "min": 0, "max": 1, "defaultValue": 0}])
    out, missing = b.evaluate(_env(JawOpen=[0.2, 0.4, 0.6, 0.8]))
    assert missing == ["TongueOut"]
    assert np.allclose(out["Out"], [0.2, 0.4, 0.6, 0.8])


def test_outputs_are_clamped_and_nonfinite_uses_default():
    b = Bridge([{"name": "Out", "func": "X / Y", "min": -1, "max": 1, "defaultValue": 0.25}])
    out, _ = b.evaluate(_env(X=[5.0, -5.0, 0.5, 0.0], Y=[1.0, 1.0, 1.0, 0.0]))
    assert np.allclose(out["Out"], [1.0, -1.0, 0.5, 0.25])


def test_head_inputs_follow_image_space_angles():
    b = Bridge.load("default")
    out, _ = b.evaluate(_env(yaw=[10.0, -10, 0, 0], pitch=[0, 0, 5.0, 0], roll=[0, 0, 0, -7.0]))
    assert np.allclose(out["FaceAngleX"], [10, -10, 0, 0])
    assert np.allclose(out["FaceAngleY"], [0, 0, 5, 0])
    assert np.allclose(out["FaceAngleZ"], [0, 0, 0, -7])


def test_default_bridge_rest_face():
    """All-zero calibrated inputs are the rest face: eyes open at 0.5, neutral smile 0.5, mouth shut."""
    b = Bridge.load("default")
    out, _ = b.evaluate(_env())
    assert np.allclose(out["EyeOpenLeft"], 0.5) and np.allclose(out["EyeOpenRight"], 0.5)
    assert np.allclose(out["MouthSmile"], 0.5)
    assert np.allclose(out["MouthOpen"], 0.0) and np.allclose(out["JawOpen"], 0.0)
    assert np.allclose(out["Brows"], 0.5)


def test_rounding_and_jaw_motion_cannot_force_sealed_lips_open():
    out, _ = Bridge.load("default").evaluate(_env(
        LipOpen=[0, 0, 0, 0], JawOpen=[0, 0.8, 0, 0.8],
        MouthFunnel=[0, 0, 1, 1], LipWidth=[0, 0, -0.8, -0.8],
    ))
    np.testing.assert_allclose(out["MouthOpen"], 0)
    assert out["JawOpen"][3] > 0 and out["MouthFunnel"][3] > 0


def test_puff_seals_lips_and_tongue_suppresses_conflicting_width_and_rounding():
    out, _ = Bridge.load("default").evaluate(_env(
        LipOpen=[0.6, 0.6, 0.6, 0.6], JawOpen=[0.5, 0.5, 0.5, 0.5],
        LipWidth=[-0.5, -0.5, 0.5, 0.5], MouthFunnel=[0.8, 0.8, 0.8, 0.8],
        CheekPuff=[0, 1, 0, 0], TongueOut=[0, 0, 1, 0],
    ))
    assert out["MouthOpen"][1] == 0 and out["JawOpen"][1] == 0
    assert out["MouthPucker"][2] == 0 and out["MouthFunnel"][2] == 0
    assert out["MouthOpen"][2] > 0
    assert out["MouthPucker"][0] < 0 < out["MouthPucker"][3]
