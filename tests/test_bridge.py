import json

import numpy as np
import pytest

from v2l import bridge as B
from v2l.bridge import Bridge, BridgeError, Formula
from v2l.curves import Curve, problems as curve_problems


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


# ------------------------------------------------------------- SnenkBridge compatibility, modifiers

DELAY = {"refParam": "Src", "smoothing": 2, "delayCount": 3, "inMin": 0, "inMax": 10, "outMin": 0, "outMax": 1}


def test_delay_buffer_follows_snenkbridge_semantics():
    # Ring of 3 slots starting at 0: the slot read back holds the value from two frames earlier;
    # the output moves halfway (1 / smoothing) toward it each frame. Worked by hand:
    # frame:     0    1    2     3     4      5
    # mapped:    1    1    1     0     0      0      (10 -> 1 on the 0..10 -> 0..1 map)
    # read back: 0    0    1     1     1      0
    # output:    0    0    0.5   0.75  0.875  0.4375
    out = B.delay_buffer(np.array([10.0, 10, 10, 0, 0, 0]), DELAY)
    assert np.allclose(out, [0, 0, 0.5, 0.75, 0.875, 0.4375])
    # One slot and smoothing 1: no delay, only the range map (inputs outside are clamped first).
    plain = B.delay_buffer(np.array([-5.0, 5.0, 20.0]), dict(DELAY, delayCount=1, smoothing=1))
    assert np.allclose(plain, [0.0, 0.5, 1.0])


def test_delay_buffer_reads_another_output_and_is_evaluated_after_it():
    b = B.Bridge([
        {"name": "Late", "func": "", "min": 0, "max": 1, "defaultValue": 0, "delayBuffer": dict(DELAY, refParam="Src")},
        {"name": "Src", "func": "JawOpen * 10", "min": 0, "max": 10, "defaultValue": 0},
    ])
    out, missing = b.evaluate(_env(n=6, JawOpen=[1, 1, 1, 0, 0, 0]))
    assert missing == [] and np.allclose(out["Late"], [0, 0, 0.5, 0.75, 0.875, 0.4375])


def test_modifiers_delay_then_smoothing():
    b = B.Bridge([{"name": "Out", "func": "JawOpen", "min": 0, "max": 1, "defaultValue": 0,
                   "modifiers": {"delayMs": 200, "smoothing": 0.5}}])
    out, _ = b.evaluate(_env(n=5, JawOpen=[0, 1, 1, 1, 1]), fps=10.0)
    # Delayed by two frames (200 ms at 10 fps): 0 0 0 1 1; then each frame moves half the way.
    assert np.allclose(out["Out"], [0, 0, 0, 0.5, 0.75])


def test_steps_hysteresis_and_hold():
    steps = [{"trigger": 0.3, "target": 0.5, "threshold": 0.1}, {"trigger": 0.6, "target": 1.0, "holdMs": 300}]
    v = np.array([0, 0.35, 0.25, 0.15, 0.7, 0.1, 0.1, 0.1, 0.1, 0.65])
    out = B.apply_steps(v, steps, 0.0, 1.0, fps=10.0)
    # 0.25 stays on the 0.3 step (it falls back only below 0.3 - 0.1); 0.7 enters the top step,
    # which holds for 300 ms (frames 4..6) before it may drop all the way down.
    assert list(out) == [0, 0.5, 0.5, 0, 1, 1, 1, 0, 0, 1]


def test_bridge_patch_add_and_disable_on_top_of_a_preset():
    _, preset = B.read_preset("default")
    eff = B.effective(preset, [
        {"name": "MouthOpen", "func": "LipOpen * 2"},
        {"name": "Ear", "func": "", "min": -1, "max": 1, "delayBuffer": dict(DELAY, refParam="FaceAngleX")},
        {"name": "VoiceVolumePlusMouthOpen", "enabled": False},
    ])
    src = {p["name"]: p["source"] for p in eff}
    assert src["MouthOpen"] == "override" and src["Ear"] == "added" and src["VoiceVolumePlusMouthOpen"] == "disabled"
    assert src["FaceAngleX"] == "preset"
    mouth = next(p for p in eff if p["name"] == "MouthOpen")
    assert mouth["max"] == 1.0 and mouth["overridden"] == ["func"]  # omitted fields inherited
    b = B.Bridge(eff)
    out, _ = b.evaluate(_env(n=2, LipOpen=[0.2, 0.7]))
    assert np.allclose(out["MouthOpen"], [0.4, 1.0]) and "VoiceVolumePlusMouthOpen" not in out and "Ear" in out
    assert B.problems(eff) == []


def test_snek_wrapper_and_plain_list_load_with_extra_keys(tmp_path):
    params = [{"name": "Out", "func": "JawOpen", "min": 0, "max": 1, "defaultValue": 0,
               "modifiers": {"smoothing": 0.2}},
              {"name": "Lag", "func": "", "min": 0, "max": 1, "defaultValue": 0,
               "delayBuffer": dict(DELAY, refParam="Out", inMax=1)}]
    wrapped = tmp_path / "w.snek"
    wrapped.write_text(json.dumps({"format": "snek", "version": 1, "title": "t", "params": params}))
    plain = tmp_path / "p.json"
    plain.write_text(json.dumps(params))
    for f in (wrapped, plain):
        name, got = B.read_preset(str(f))
        assert [p["name"] for p in got] == ["Out", "Lag"] and B.problems(got) == []
        assert set(B.Bridge.load(str(f)).evaluate(_env(n=3, JawOpen=[0, 1, 1]))[0]) == {"Out", "Lag"}
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"format": "other", "params": []}))
    with pytest.raises(BridgeError, match="unsupported preset format"):
        B.read_preset(str(bad))


def test_power_groups_left_to_right_and_binds_tighter_than_a_sign():
    env = {"A": np.array([2.0])}
    assert Formula("2 ^ 3 ^ 2")(env) == 64.0
    assert Formula("-A ^ 2")(env) == -4.0
    assert Formula("math::pow(A, 3) + math::pi() * 0")(env) == 8.0


def test_check_reports_names_and_the_error_position():
    ok = B.check("if(JawOpen > 0.5, Wave1000, MyThing) // note")
    assert ok["valid"] and ok["inputs"] == ["JawOpen", "Wave1000"] and ok["unknown"] == ["MyThing"]
    assert [t["kind"] for t in ok["tokens"][:3]] == ["function", "paren", "name"]
    bad = B.check("0.5 + (MouthSmileLeft * ")
    assert not bad["valid"] and bad["problem"]["position"] == len("0.5 + (MouthSmileLeft * ")
    unknown_fn = B.check("1 + foo(2)")
    assert unknown_fn["problem"]["message"].startswith("unknown function") and unknown_fn["problem"]["position"] == 4


def test_monotone_curve_never_overshoots():
    c = Curve([[0, 0], [0.2, 0.0], [0.4, 0.9], [0.5, 1.0], [1, 1]], smooth=True)
    xs = np.linspace(0, 1, 2001)
    ys = c(xs)
    assert ys.min() >= -1e-12 and ys.max() <= 1 + 1e-12          # flat stretches stay flat
    assert np.all(np.diff(ys) >= -1e-12)                          # monotone data, monotone curve
    for x, y in [[0, 0], [0.2, 0.0], [0.4, 0.9], [0.5, 1.0], [1, 1]]:
        assert c.value(x) == pytest.approx(y)
    lin = Curve([[0, 0], [0.5, 0.8], [1, 1]], smooth=False)
    assert lin.value(0.25) == pytest.approx(0.4) and lin.value(1.5) == pytest.approx(1.2)  # end slope beyond 1
    assert curve_problems({"points": [[0, 0], [1, 1]], "smooth": "yes"}, "c")
