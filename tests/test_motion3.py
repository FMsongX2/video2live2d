import numpy as np

from v2l import motion3

RANGES = {"ParamA": {"min": -30.0, "max": 30.0, "def": 0.0}, "ParamB": {"min": 0.0, "max": 1.0, "def": 0.0}}


def _params(n=300, fps=30.0):
    t = np.arange(n) / fps
    rng = np.random.default_rng(0)
    return {
        "ParamA": 25 * np.sin(2 * np.pi * 0.7 * t) + rng.normal(0, 0.3, n),
        "ParamB": np.clip(0.5 + 0.6 * np.sin(2 * np.pi * 2.1 * t), 0, 1),
    }


def test_written_curves_reproduce_frames_within_tolerance():
    fps, tol = 30.0, 0.004
    params = _params(fps=fps)
    m = motion3.build(params, RANGES, fps, tolerance=tol)
    assert motion3.validate(m, RANGES) == []
    t = np.arange(300) / fps
    back = motion3.evaluate(m, t)
    for pid, v in params.items():
        r = RANGES[pid]
        err = np.abs(back[pid] - np.clip(v, r["min"], r["max"]))
        assert err.max() <= tol * (r["max"] - r["min"]) + 1e-4


def test_values_outside_the_model_range_are_clamped():
    m = motion3.build({"ParamB": np.linspace(-1, 2, 50)}, RANGES, 30.0)
    vals = motion3.evaluate(m, np.arange(50) / 30.0)["ParamB"]
    assert vals.min() >= 0.0 and vals.max() <= 1.0
    assert motion3.validate(m, RANGES) == []


def test_validate_reports_broken_files():
    m = motion3.build(_params(), RANGES, 30.0)
    m["Meta"]["TotalPointCount"] += 1
    m["Curves"][0]["Segments"][1] = 99.0
    errs = motion3.validate(m, RANGES)
    assert any("TotalPointCount" in e for e in errs)
    assert any("outside" in e for e in errs)


def test_duration_matches_last_point_and_loop_flag():
    m = motion3.build(_params(n=91), RANGES, 30.0, loop=True)
    assert m["Meta"]["Duration"] == 3.0
    assert m["Meta"]["Loop"] is True
    for c in m["Curves"]:
        assert c["Segments"][0] == 0 and c["Segments"][-2] == 3.0
