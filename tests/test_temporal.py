import numpy as np

from v2l import temporal


def _signals(t, v, ok):
    return {"yaw": v.copy(), "shoulderYaw": v.copy(), "face_ok": ok.astype(float), "pose_ok": np.ones_like(v)}


def test_short_gap_is_bridged_long_gap_relaxes_to_rest():
    fps = 30.0
    t = np.arange(300) / fps
    v = np.full(300, 10.0)
    ok = np.ones(300, bool)
    ok[50:56] = False      # 0.2 s: tracker hiccup
    ok[150:240] = False    # 3 s: performer left the frame
    v[~ok] = np.nan
    out, grid, present = temporal.resample(_signals(t, v, ok), t, fps)
    assert np.allclose(out["yaw"][45:60], 10.0)
    assert np.allclose(out["yaw"][170:220], 0.0)
    assert abs(present[100] - 1.0) < 1e-9 and present[195] == 0.0
    assert np.all(np.abs(np.diff(out["yaw"])) < 10.0 / (0.25 * fps))  # ramps, no jumps


def test_variable_frame_rate_lands_on_uniform_grid():
    rng = np.random.default_rng(0)
    t = np.cumsum(rng.uniform(1 / 40, 1 / 20, 200))
    t -= t[0]
    v = np.sin(t)
    out, grid, _ = temporal.resample(_signals(t, v, np.ones(200, bool)), t, 30.0)
    assert np.allclose(np.diff(grid), 1 / 30.0)
    assert np.allclose(out["yaw"], np.sin(grid), atol=2e-3)


def test_zero_phase_filter_adds_no_lag():
    fps = 30.0
    t = np.arange(600) / fps
    clean = np.sin(2 * np.pi * 0.5 * t)
    noisy = clean + np.random.default_rng(0).normal(0, 0.2, len(t))
    f = temporal.lowpass(noisy, 3.0, fps)
    lags = np.arange(-10, 11)
    corr = [np.dot(np.roll(f, k)[50:-50], clean[50:-50]) for k in lags]
    assert lags[int(np.argmax(corr))] == 0


def test_loop_end_meets_start():
    v = np.linspace(0, 1, 120)
    out = temporal.make_loop(v, 30.0)
    assert out[-1] == v[0]
    assert np.array_equal(out[:100], v[:100])
