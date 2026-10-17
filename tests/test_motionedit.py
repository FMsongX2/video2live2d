"""Editable motions: round trip, exact evaluation, operations, fitting and the CLI contract."""

import bisect
import copy
import json
import math
import subprocess
import sys

import pytest

from v2l import motionedit as M


def motion3(restricted=True):
    """Every segment type, a per-curve fade, an unknown Meta field and user data."""
    third = lambda a, b: (a + (b - a) / 3, b - (b - a) / 3)
    c1t, c2t = third(1.0, 2.0) if restricted else (1.1, 1.9)
    segs = [0.0, 0.0,
            0, 1.0, 10.0,                              # linear 0..1
            1, c1t, 20.0, c2t, -5.0, 2.0, 4.0,          # bezier 1..2
            2, 3.0, 8.0,                                # stepped 2..3
            3, 4.0, -2.0]                               # inverse stepped 3..4
    return {
        "Version": 3,
        "Meta": {"Duration": 4.0, "Fps": 30.0, "FadeInTime": 0.3, "FadeOutTime": 0.7, "Loop": True,
                 "AreBeziersRestricted": restricted, "CurveCount": 2, "TotalSegmentCount": 5,
                 "TotalPointCount": 9, "UserDataCount": 1, "TotalUserDataSize": 5, "Vendor": {"x": 1}},
        "Curves": [
            {"Target": "Parameter", "Id": "ParamA", "FadeInTime": 0.1, "Segments": segs},
            {"Target": "PartOpacity", "Id": "PartB", "Segments": [0.0, 1.0, 0, 4.0, 0.0]},
        ],
        "UserData": [{"Time": 2.5, "Value": "hello"}],
    }


def cubic(a, b, c, d, s):
    return (1 - s) ** 3 * a + 3 * (1 - s) ** 2 * s * b + 3 * (1 - s) * s * s * c + s ** 3 * d


def ops(motion, *operations, ranges=None):
    return M.apply(motion, list(operations), ranges)[0]


def curve(edit, cid="ParamA"):
    return next(c for c in edit["curves"] if c["id"] == cid)


def values(edit, times, cid="ParamA"):
    return M.evaluate(edit, times, [cid])[cid]


def dense(t0, t1, n=400):
    return [t0 + (t1 - t0) * i / n for i in range(n + 1)]


# --------------------------------------------------------------------------------- round trip

@pytest.mark.parametrize("restricted", [True, False])
def test_round_trip_preserves_values_meta_and_passthrough(restricted):
    src = motion3(restricted)
    edit = M.from_motion3(src)
    assert [s["type"] for s in curve(edit)["segments"]] == ["linear", "bezier", "stepped", "inverse_stepped"]
    back = M.to_motion3(json.loads(json.dumps(edit)))
    if not restricted:  # control times are kept as given
        assert back["Curves"] == src["Curves"]
    for a, b in zip(src["Curves"], back["Curves"]):
        assert a["Target"] == b["Target"] and a["Id"] == b["Id"] and a.get("FadeInTime") == b.get("FadeInTime")
        assert b["Segments"] == pytest.approx(a["Segments"], abs=1e-6)
    assert back["Meta"] == src["Meta"]
    assert back["UserData"] == src["UserData"]
    assert M.validate(back) == []


def test_counts_are_recomputed_and_validate_reports_them():
    src = motion3()
    src["Meta"]["TotalPointCount"] = 99
    assert any("TotalPointCount" in p for p in M.validate(src))
    assert M.to_motion3(M.from_motion3(src))["Meta"]["TotalPointCount"] == 9  # 1+1+3+1+1 for ParamA, 2 for PartB


def test_writer_recomputes_restricted_control_times():
    edit = M.from_motion3(motion3())
    curve(edit)["segments"][1]["c1"][0] = 1.9  # ignored: restricted Beziers have fixed control times
    segs = M.to_motion3(edit)["Curves"][0]["Segments"]
    assert segs[6] == pytest.approx(1 + 1 / 3, abs=1e-6) and segs[8] == pytest.approx(2 - 1 / 3, abs=1e-6)


def test_invalid_editable_motions_are_rejected_with_every_problem():
    edit = M.from_motion3(motion3())
    curve(edit)["keys"][2][0] = 0.5  # out of order
    curve(edit, "PartB")["segments"] = []
    with pytest.raises(M.MotionError) as e:
        M.to_motion3(edit)
    assert len(e.value.problems) == 2


# --------------------------------------------------------------------------------- evaluation

def test_linear_stepped_and_inverse_stepped():
    e = M.from_motion3(motion3())
    v = values(e, [0.5, 2.0, 2.999, 3.0, 3.0001, 3.5, 4.0, 9.0])
    assert v[0] == pytest.approx(5.0)
    assert v[1] == 4.0 and v[2] == 4.0          # stepped holds the start value...
    assert v[3] == -2.0                         # ...and at its end the next (inverse stepped) segment applies
    assert v[4] == -2.0 and v[5] == -2.0        # inverse stepped jumps right after its start
    assert v[6] == -2.0 and v[7] == -2.0        # after the last key the last value holds


def test_restricted_bezier_is_its_cubic_polynomial_in_time():
    e = M.from_motion3(motion3())
    # Power-basis coefficients of the Bernstein cubic with values 4.0... here: v0=10, c1=20, c2=-5, v1=4.
    v0, c1, c2, v1 = 10.0, 20.0, -5.0, 4.0
    a = -v0 + 3 * c1 - 3 * c2 + v1
    b = 3 * v0 - 6 * c1 + 3 * c2
    c = -3 * v0 + 3 * c1
    for t in dense(1.0, 2.0, 50)[:-1]:
        s = t - 1.0
        assert values(e, [t])[0] == pytest.approx(((a * s + b) * s + c) * s + v0, abs=1e-9)


def test_unrestricted_bezier_matches_a_dense_parametric_sampling():
    e = M.from_motion3(motion3(restricted=False))
    # Independent reference: sample the parametric curve finely and interpolate in time.
    n = 200000
    xs = [cubic(1.0, 1.1, 1.9, 2.0, i / n) for i in range(n + 1)]
    ys = [cubic(10.0, 20.0, -5.0, 4.0, i / n) for i in range(n + 1)]
    for t in [1.0, 1.01, 1.2, 1.5, 1.77, 1.999]:
        k = min(bisect.bisect_left(xs, t), n)
        k = max(k, 1)
        ref = ys[k - 1] + (ys[k] - ys[k - 1]) * (t - xs[k - 1]) / (xs[k] - xs[k - 1])
        assert values(e, [t])[0] == pytest.approx(ref, abs=1e-6)
    # It differs from the restricted reading of the same numbers.
    assert values(e, [1.2])[0] != pytest.approx(values(M.from_motion3(motion3()), [1.2])[0], abs=1e-3)


# --------------------------------------------------------------------------------- operations

def test_insert_key_keeps_the_shape_of_every_segment_type():
    for restricted in (True, False):
        e = M.from_motion3(motion3(restricted))
        ts = dense(0.0, 4.0, 800)
        before = values(e, ts)
        after = ops(e, *({"op": "insert_key", "curve": "ParamA", "t": t} for t in (0.4, 1.37, 2.5, 3.25)))
        assert len(curve(after)["keys"]) == 9
        assert values(after, ts) == pytest.approx(before, abs=1e-9)


def test_insert_key_with_a_value_and_its_errors():
    e = M.from_motion3(motion3())
    after = ops(e, {"op": "insert_key", "curve": "ParamA", "t": 0.5, "value": 7.0})
    assert values(after, [0.5])[0] == 7.0
    with pytest.raises(M.EditError, match="already has a key"):
        ops(e, {"op": "insert_key", "curve": "ParamA", "t": 1.0})
    with pytest.raises(M.EditError, match="outside"):
        ops(e, {"op": "insert_key", "curve": "ParamA", "t": 4.5})


def test_move_key_stays_between_its_neighbours():
    e = M.from_motion3(motion3())
    moved = ops(e, {"op": "move_key", "curve": "ParamA", "t": 1.0, "to_t": 1.5, "to_value": 3.0})
    assert curve(moved)["keys"][1] == [1.5, 3.0]
    seg = curve(moved)["segments"][1]  # the bezier now spans 1.5..2: control times follow
    assert seg["c1"][0] == pytest.approx(1.5 + 0.5 / 3) and seg["c1"][1] == pytest.approx(20.0 - 7.0)
    with pytest.raises(M.EditError, match="past a neighbouring key") as err:
        ops(e, {"op": "offset_values", "curves": "*", "delta": 1},
            {"op": "move_key", "curve": "ParamA", "t": 1.0, "to_t": 2.0})
    assert err.value.index == 1
    with pytest.raises(M.EditError, match="cannot move in time"):
        ops(e, {"op": "move_key", "curve": "ParamA", "t": 0.0, "to_t": 0.5})
    with pytest.raises(M.EditError, match="no key at"):
        ops(e, {"op": "move_key", "curve": "ParamA", "t": 0.5, "to_value": 1})


def test_delete_keys_never_removes_the_endpoints():
    e = M.from_motion3(motion3())
    with pytest.raises(M.EditError, match="first and last key"):
        ops(e, {"op": "delete_keys", "curve": "ParamA", "times": [0.0]})
    with pytest.raises(M.EditError, match="first and last key"):
        ops(e, {"op": "delete_keys", "curve": "ParamA", "times": [4.0]})
    kept = ops(e, {"op": "delete_keys", "curve": "ParamA", "from": 0.0, "to": 4.0})
    assert [k[0] for k in curve(kept)["keys"]] == [0.0, 4.0]
    one = ops(e, {"op": "delete_keys", "curve": "ParamA", "times": [2.0]})
    assert [k[0] for k in curve(one)["keys"]] == [0.0, 1.0, 3.0, 4.0]
    seg = curve(one)["segments"][1]  # bezier 1..3 keeps the start slope of the old bezier
    assert seg["type"] == "bezier"
    assert (seg["c1"][1] - 10.0) / (seg["c1"][0] - 1.0) == pytest.approx((20.0 - 10.0) / (1 / 3))


def test_trim_splits_cut_segments_exactly():
    for restricted in (True, False):
        e = M.from_motion3(motion3(restricted))
        t = ops(e, {"op": "trim", "from": 0.5, "to": 1.6})
        assert t["meta"]["duration"] == pytest.approx(1.1)
        assert curve(t)["keys"][0][0] == 0.0 and curve(t)["keys"][-1][0] == pytest.approx(1.1)
        ts = dense(0.0, 1.1, 300)[:-1]
        assert values(t, ts) == pytest.approx(values(e, [x + 0.5 for x in ts]), abs=1e-9)
        assert t["passthrough"]["user_data"] == []  # the user data at 2.5 s lies outside
        assert M.validate(M.to_motion3(t)) == []


def test_set_interpolation_changes_only_overlapping_segments():
    e = M.from_motion3(motion3())
    # [1, 2] overlaps only the bezier segment; its neighbours only touch the range at a key.
    lin = ops(e, {"op": "set_interpolation", "curves": ["ParamA"], "type": "linear", "from": 1.0, "to": 2.0})
    assert [s["type"] for s in curve(lin)["segments"]] == ["linear", "linear", "stepped", "inverse_stepped"]
    wide = ops(e, {"op": "set_interpolation", "curves": "*", "type": "bezier", "from": 0.5, "to": 2.5})
    assert [s["type"] for s in curve(wide)["segments"]] == ["bezier", "bezier", "bezier", "inverse_stepped"]
    assert curve(wide)["segments"][1] == curve(e)["segments"][1]  # already bezier: handles kept
    with pytest.raises(M.EditError, match="from must not be after to"):
        ops(e, {"op": "set_interpolation", "curves": "*", "type": "linear", "from": 2, "to": 1})


def test_auto_tangents_do_not_overshoot_extremes_and_flatten_eases():
    keys = [[0.0, 0.0], [1.0, 10.0], [2.0, 0.0], [3.0, 5.0], [4.0, 6.0]]
    e = {"format": M.FORMAT, "version": 1, "meta": {"duration": 4.0, "fps": 30.0, "restricted": True},
         "curves": [{"id": "ParamA", "keys": keys, "segments": [{"type": "linear"}] * 4}]}
    smooth = ops(e, {"op": "set_interpolation", "curves": "*", "type": "bezier"})
    v = values(smooth, dense(0.0, 4.0, 800))
    assert max(v) == pytest.approx(10.0) and min(v) >= -1e-9          # no overshoot at the extremes
    assert all(5.0 - 1e-9 <= x <= 6.0 + 1e-9 for x in values(smooth, dense(3.0, 4.0, 50)))  # monotone piece
    flat = ops(smooth, {"op": "flatten_tangents", "curves": "*", "from": 1.0, "to": 1.0})
    assert curve(flat)["segments"][0]["c2"][1] == 10.0 and curve(flat)["segments"][1]["c1"][1] == 10.0


def test_set_handles_scale_offset_clamp_retime_meta_and_delete_curve():
    e = M.from_motion3(motion3())
    with pytest.raises(M.EditError, match="not a bezier segment"):
        ops(e, {"op": "set_handles", "curve": "ParamA", "segment": 0.0, "c1v": 1})
    h = ops(e, {"op": "set_handles", "curve": "ParamA", "segment": 1.0, "c1v": 1.0})
    assert curve(h)["segments"][1]["c1"][1] == 1.0
    s = ops(e, {"op": "scale_values", "curves": ["ParamA"], "factor": 2, "pivot": "first"},
            {"op": "offset_values", "curves": ["ParamA"], "delta": 1})
    assert values(s, [0.5, 1.5]) == pytest.approx([2 * x + 1 for x in values(e, [0.5, 1.5])])
    c = ops(e, {"op": "clamp", "ranges": {"ParamA": [0, 5]}})
    assert all(0 <= x <= 5 for x in values(c, dense(0, 4, 100)))
    r = ops(e, {"op": "retime", "factor": 2}, {"op": "set_meta", "loop": False, "fade_in": 0.0})
    assert r["meta"]["duration"] == 8.0 and r["meta"]["loop"] is False and r["meta"]["fade_in"] == 0.0
    assert values(r, [3.0]) == pytest.approx(values(e, [1.5]))
    assert r["passthrough"]["user_data"][0]["Time"] == 5.0
    with pytest.raises(M.EditError, match="before the last key"):
        ops(e, {"op": "set_meta", "duration": 2.0})
    d = ops(e, {"op": "delete_curve", "curve": "PartB"})
    assert [c["id"] for c in d["curves"]] == ["ParamA"]


def test_invalid_operations_change_nothing():
    e = M.from_motion3(motion3())
    snapshot = copy.deepcopy(e)
    with pytest.raises(M.EditError, match="unknown curve ParamZ"):
        ops(e, {"op": "offset_values", "curves": ["ParamA"], "delta": 5},
            {"op": "offset_values", "curves": ["ParamZ"], "delta": 1})
    with pytest.raises(M.EditError, match="unknown op"):
        ops(e, {"op": "explode"})
    assert e == snapshot


# --------------------------------------------------------------------------------- fitting

def _signal(n=301, fps=30.0):
    t = [i / fps for i in range(n)]
    v = [8 * math.sin(1.3 * x) + 3 * math.sin(4.1 * x + 0.5) + 0.2 * math.sin(23 * x) for x in t]
    return t, v


def _slopes_at_keys(c):
    k, s = c["keys"], c["segments"]
    return [(M._slope_end(k[i - 1], s[i - 1], k[i]), M._slope_start(k[i], s[i], k[i + 1])) for i in range(1, len(k) - 1)]


def test_fit_bezier_is_within_tolerance_and_c1_at_interior_keys():
    t, v = _signal()
    tol = 0.05
    keys, segs = M.fit_bezier(t, v, tol)
    c = {"id": "x", "keys": keys, "segments": segs}
    assert max(abs(M.value_at(c, x) - y) for x, y in zip(t, v)) <= tol + 1e-9
    assert len(keys) < len(M.fit_linear(t, v, tol)[0])
    for left, right in _slopes_at_keys(c):
        assert left == pytest.approx(right, rel=1e-9, abs=1e-9)


def test_refit_operation_reports_and_respects_tolerance():
    t, v = _signal()
    keys, segs = M.fit_linear(t, v, 0.001)
    e = {"format": M.FORMAT, "version": 1, "meta": {"duration": t[-1], "fps": 30.0, "restricted": True},
         "curves": [{"id": "ParamA", "keys": keys, "segments": segs}]}
    out, reports = M.apply(e, [{"op": "refit", "curves": "*", "tolerance": 0.002, "interpolation": "bezier"}],
                           ranges={"ParamA": [-20, 20]})
    rep = reports[0]["ParamA"]
    assert rep["tolerance"] == pytest.approx(0.08) and rep["max_error"] <= 0.08 + 1e-9
    assert rep["keys_after"] < rep["keys_before"]
    assert max(abs(a - b) for a, b in zip(values(out, t), values(e, t))) <= 0.08 + 1e-9
    for left, right in _slopes_at_keys(curve(out)):
        assert left == pytest.approx(right, rel=1e-9, abs=1e-9)


def test_refit_keeps_stepped_segments():
    e = M.from_motion3(motion3())
    out = ops(e, {"op": "refit", "curves": ["ParamA"], "tolerance": 0.001, "interpolation": "linear"})
    assert [s["type"] for s in curve(out)["segments"]][-2:] == ["stepped", "inverse_stepped"]
    assert values(out, [2.5, 3.5]) == values(e, [2.5, 3.5])


def test_pipeline_bezier_output_reproduces_frames_within_tolerance():
    np = pytest.importorskip("numpy")
    from v2l import motion3 as W

    t, v = _signal()
    ranges = {"ParamA": {"min": -15.0, "max": 15.0, "def": 0.0}}
    tol = 0.004
    lin = W.build({"ParamA": np.array(v)}, ranges, 30.0, tolerance=tol)
    bez = W.build({"ParamA": np.array(v)}, ranges, 30.0, tolerance=tol, interpolation="bezier")
    assert W.validate(bez, ranges) == [] and bez["Meta"]["AreBeziersRestricted"] is True
    back = W.evaluate(bez, np.array(t))["ParamA"]
    assert np.abs(back - np.clip(v, -15, 15)).max() <= tol * 30 + 1e-3
    n_keys = lambda m: len(M.from_motion3(m)["curves"][0]["keys"])
    assert n_keys(bez) < n_keys(lin)


# --------------------------------------------------------------------------------- CLI

def _cli(*args, stdin=None):
    r = subprocess.run([sys.executable, "-m", "v2l.motionedit", *args], capture_output=True, text=True, input=stdin)
    return r.returncode, r.stdout, r.stderr


def test_cli_bad_ops_file_gives_one_json_error(tmp_path):
    src = tmp_path / "m.motion3.json"
    src.write_text(json.dumps(motion3()))
    bad = tmp_path / "ops.json"
    bad.write_text(json.dumps([{"op": "offset_values", "curves": "*", "delta": 1},
                               {"op": "move_key", "curve": "ParamA", "t": 1.0, "to_t": 3.5}]))
    out = tmp_path / "out.motion3.json"
    code, stdout, _ = _cli("edit", str(src), "--ops", str(bad), "--out", str(out), "--json")
    lines = stdout.strip().splitlines()
    assert code == 2 and len(lines) == 1
    err = json.loads(lines[0])
    assert err["ok"] is False and err["error"]["type"] == "invalid" and err["error"]["op_index"] == 1
    assert "past a neighbouring key" in err["error"]["message"] and not out.exists()
    bad.write_text("{not json")
    code, stdout, _ = _cli("edit", str(src), "--ops", str(bad), "--out", str(out), "--json")
    assert code == 2 and json.loads(stdout)["error"]["type"] == "input"


def test_cli_edit_read_eval_and_no_silent_overwrite(tmp_path):
    src = tmp_path / "m.motion3.json"
    src.write_text(json.dumps(motion3()))
    out = tmp_path / "out.motion3.json"
    opsfile = json.dumps({"operations": [{"op": "insert_key", "curve": "ParamA", "t": 0.5, "value": 9.0}]})
    code, stdout, _ = _cli("edit", str(src), "--ops", "-", "--out", str(out), "--json", stdin=opsfile)
    assert code == 0 and json.loads(stdout)["counts"]["keys"] == 8
    code, stdout, _ = _cli("edit", str(src), "--ops", "-", "--out", str(out), "--json", stdin=opsfile)
    assert code == 2 and "--overwrite" in json.loads(stdout)["error"]["message"]
    code, stdout, _ = _cli("edit", str(src), "--ops", "-", "--out", "-", "--json", stdin=opsfile)
    res = json.loads(stdout)
    assert res["out"] is None and res["motion"]["format"] == M.FORMAT
    code, stdout, _ = _cli("eval", str(out), "--times", "0.5,1.5", "--curves", "ParamA", "--json")
    assert json.loads(stdout)["values"]["ParamA"][0] == 9.0
    code, stdout, _ = _cli("read", str(out), "--curves", "PartB")
    assert [c["id"] for c in json.loads(stdout)["curves"]] == ["PartB"]
    code, stdout, _ = _cli("validate", str(out), "--json")
    assert code == 0 and json.loads(stdout)["valid"] is True


def test_editing_needs_only_the_standard_library():
    code = ("import sys; sys.modules['numpy'] = None\n"
            "import v2l.motionedit, v2l.mapping, v2l.inputs, v2l.remote, v2l.bridge, v2l.curves\n"
            "assert v2l.inputs.listing()['inputs']")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
