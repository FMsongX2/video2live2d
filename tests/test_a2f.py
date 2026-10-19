"""Audio2Face-3D port: windowing, post-processing, blendshape solve and determinism.

No network, no model files, no onnxruntime: geometry is synthetic and the ONNX session is a fake."""

import numpy as np
import pytest
from scipy.optimize import lsq_linear

from v2l import a2f, inputs
from v2l.config import AudioConfig

# network_info.json of nvidia/Audio2Face-3D-v3.0 (timing part), with tiny geometry sizes.
INFO = {
    "id": {"version": "3.2"},
    "params": {
        "emotions": ["amazement", "anger", "cheekiness", "disgust", "fear", "grief", "joy", "outofbreath",
                     "pain", "sadness"],
        "identities": ["Claire", "James", "Mark"],
        "skin_size": 12, "tongue_size": 6, "jaw_size": 15, "eyes_size": 4,
        "num_diffusion_steps": 2, "num_gru_layers": 2, "gru_latent_dim": 8,
        "num_frames_left_truncate": 15, "num_frames_right_truncate": 15, "num_frames_center": 30,
    },
    "audio_params": {"buffer_len": 16000, "padding_left": 16000, "padding_right": 16000, "samplerate": 16000},
}
MODEL_CONFIG = {
    "input_strength": 1.0, "upper_face_smoothing": 0.001, "lower_face_smoothing": 0.006,
    "upper_face_strength": 1.0, "lower_face_strength": 1.0, "face_mask_level": 0.6, "face_mask_softness": 0.0085,
    "skin_strength": 1.0, "blink_strength": 1.0, "lower_teeth_strength": 1.0, "lower_teeth_height_offset": 0.0,
    "lower_teeth_depth_offset": 0.0, "lip_open_offset": 0.0, "tongue_strength": 1.3, "tongue_height_offset": 0.0,
    "tongue_depth_offset": 0.0, "eyeballs_strength": 1.0, "saccade_strength": 0.6, "right_eye_rot_x_offset": 0.0,
    "right_eye_rot_y_offset": 0.0, "left_eye_rot_x_offset": 0.0, "left_eye_rot_y_offset": 0.0,
    "eyelid_open_offset": 0.0, "eye_saccade_seed": 0,
}


def _params(P, **kw):
    p = dict(strengthL1regularization=0.0, strengthL2regularization=0.0, strengthTemporalSmoothing=0.0,
             strengthSymmetry=0.0, templateBBSize=1.0, bsSolveActivePoses=[1] * P, bsSolveCancelPoses=[-1] * P,
             bsSolveSymmetryPoses=[-1] * P, bsWeightMultipliers=[1.0] * P, bsWeightOffsets=[0.0] * P)
    p.update(kw)
    return p


def _mesh(V, P, seed=0):
    rng = np.random.default_rng(seed)
    neutral = rng.normal(size=V * 3)
    deltas = rng.normal(size=(V * 3, P))
    return neutral, deltas


# ------------------------------------------------------------------------------- windowing

def test_v3_plan_counts_and_timing():
    plan = a2f.Plan.make(INFO, 160000)                     # 10 s at 16 kHz
    assert plan.fps == 60.0
    assert plan.before == 45                               # 1.5 inferences of centre frames precede the audio
    assert plan.n_inferences == 22 and plan.n_frames == 600
    ts = plan.timestamps()
    assert ts[0] == 0 and ts[-1] < 160000 and plan.frames.target(plan.before + plan.n_frames) >= 160000
    assert np.all(np.abs(ts - np.arange(600) * 16000 / 60) < 1)   # f/60 s, quantised to samples
    # window k covers samples [8000 k - 16000, 8000 k); its centre frames start 0.25 s in
    assert plan.windows.start(0) == -16000 and plan.windows.start(3) == 8000
    assert plan.frames_of(0) == []
    assert plan.frames_of(1) == [(15 + i, i - 15) for i in range(15, 30)]
    covered = [f for k in range(plan.n_inferences) for _, f in plan.frames_of(k)]
    assert covered == list(range(600))


@pytest.mark.parametrize("n,frames", [(0, 0), (1, 1), (266, 1), (267, 2), (16000, 60), (16001, 61)])
def test_plan_frame_count_is_frames_with_target_inside_audio(n, frames):
    plan = a2f.Plan.make(INFO, n)
    assert plan.n_frames == frames
    assert all(0 <= t < n for t in plan.timestamps())
    # every reported frame is produced by some inference
    covered = sorted(f for k in range(plan.n_inferences) for _, f in plan.frames_of(k))
    assert covered == list(range(frames))


def test_progress_uses_cpp_integer_semantics():
    p = a2f.Progress(10, 2, -7, 6, 4)                     # reduced to 3/2
    assert (p.num, p.den) == (3, 2)
    assert [p.start(i) for i in range(4)] == [-7, -6, -4, -3]
    assert a2f._tdiv(-7, 2) == -3 and a2f._tdiv(7, -2) == -3 and a2f._tdiv(7, 2) == 3


# ---------------------------------------------------------------------------------- solver

def test_bvls_matches_scipy_bounded_least_squares():
    rng = np.random.default_rng(1)
    for _ in range(25):
        n = int(rng.integers(2, 12))
        A = rng.normal(size=(n, n)) + 3 * np.eye(n)
        b = rng.normal(size=n) * 3
        lo, hi = np.zeros(n), np.ones(n)
        x = a2f.bvls(A, b, lo, hi)
        assert np.all(x >= lo) and np.all(x <= hi)
        ref = lsq_linear(A, b, bounds=(lo, hi), method="bvls", tol=1e-12).x
        assert np.linalg.norm(A @ x - b) <= np.linalg.norm(A @ ref - b) + 1e-9


def test_solve_reconstructs_known_combination_and_respects_bounds():
    neutral, deltas = _mesh(40, 6)
    s = a2f.Solver.from_arrays([f"p{i}" for i in range(6)], neutral, deltas, _params(6))
    w = np.array([[0.0, 0.3, 1.0, 0.5, 0.1, 0.9], [0.2, 0.0, 0.0, 0.7, 0.4, 0.05]])
    got = s.solve(neutral + w @ deltas.T)
    assert np.allclose(got, w, atol=1e-8)
    over = s.solve(neutral + np.array([[2.0, -1.0, 0.5, 0.5, 0.5, 0.5]]) @ deltas.T)
    assert np.all(over >= 0) and np.all(over <= 1)


def test_inactive_poses_are_zero_and_multipliers_offsets_apply_after_solve():
    neutral, deltas = _mesh(30, 4)
    s = a2f.Solver.from_arrays(list("abcd"), neutral, deltas, _params(
        4, bsSolveActivePoses=[1, 0, 1, 1], bsWeightMultipliers=[2.0, 1.0, 1.0, 3.0], bsWeightOffsets=[0, 0, 0.2, 0]))
    w = s.solve(neutral + np.array([[0.4, 0.5, 0.3, 0.2]]) @ deltas.T)
    assert w[0, 1] == 0.0
    assert 0.0 <= w[0, 0] <= 2.0 and 0.2 <= w[0, 2] <= 1.2 and 0.0 <= w[0, 3] <= 3.0


def test_cancel_pair_keeps_only_the_stronger_pose():
    neutral, deltas = _mesh(30, 3, seed=2)
    s = a2f.Solver.from_arrays(list("abc"), neutral, deltas, _params(3, bsSolveCancelPoses=[0, 0, -1]))
    assert s.cancel == [(0, 1)]
    w = s.solve(neutral + np.array([[0.6, 0.4, 0.5]]) @ deltas.T)[0]
    assert w[1] <= 1e-10 and w[0] > 0.3


def test_symmetry_pulls_paired_poses_together():
    neutral, deltas = _mesh(30, 3, seed=3)
    target = neutral + np.array([[0.9, 0.1, 0.5]]) @ deltas.T
    free = a2f.Solver.from_arrays(list("abc"), neutral, deltas, _params(3)).solve(target)[0]
    sym = a2f.Solver.from_arrays(list("abc"), neutral, deltas,
                                 _params(3, bsSolveSymmetryPoses=[7, 7, -1], strengthSymmetry=1e4)).solve(target)[0]
    assert abs(free[0] - free[1]) > 0.7
    assert abs(sym[0] - sym[1]) < 1e-2


def test_minus_one_pairs_follow_sdk_grouping():
    # SDK getPairs groups pair ids including -1: exactly two -1 entries form a pair, more do not.
    neutral, deltas = _mesh(10, 3)
    two = a2f.Solver.from_arrays(list("abc"), neutral, deltas, _params(3, bsSolveCancelPoses=[-1, 5, -1]))
    many = a2f.Solver.from_arrays(list("abc"), neutral, deltas, _params(3))
    assert two.cancel == [(0, 2)] and many.cancel == []


def test_temporal_smoothing_lags_a_step():
    neutral, deltas = _mesh(30, 2, seed=4)
    s = a2f.Solver.from_arrays(["a", "b"], neutral, deltas, _params(2, strengthTemporalSmoothing=0.5))
    step = np.repeat(np.array([[0.0, 0.0], [0.8, 0.8]]), [3, 30], axis=0)
    w = s.solve(neutral + step @ deltas.T)
    assert np.allclose(w[:3], 0.0)
    assert 0.0 < w[3, 0] < 0.8                            # does not jump to the new pose at once
    assert np.all(np.diff(w[3:, 0]) >= -1e-9)              # approaches it monotonically


def test_masked_solve_reads_only_mask_vertices():
    neutral, deltas = _mesh(20, 3, seed=5)
    mask = np.array([1, 4, 7, 9, 12, 15, 18])
    s = a2f.Solver.from_arrays(list("abc"), neutral, deltas, _params(3), mask=mask)
    assert s.D.shape == (len(mask) * 3, 3)
    full = neutral + np.array([[0.2, 0.6, 0.1]]) @ deltas.T
    full_noisy = full.copy()
    keep = np.zeros(60, bool)
    keep[s.mask] = True
    full_noisy[:, ~keep] += 100.0
    assert np.allclose(s.solve(s.masked(full_noisy)), [[0.2, 0.6, 0.1]], atol=1e-8)


def test_arkit_names_are_the_52_mediapipe_names_plus_tongue_out():
    v2l_names = {n[0].lower() + n[1:] for n, _ in inputs._ARKIT}
    assert len(a2f.ARKIT_NAMES) == 52 and len(set(a2f.ARKIT_NAMES)) == 52
    assert set(a2f.ARKIT_NAMES) == v2l_names | {"tongueOut"}
    a2f.check_arkit_names(reversed(a2f.ARKIT_NAMES))
    with pytest.raises(ValueError, match="missing"):
        a2f.check_arkit_names(a2f.ARKIT_NAMES[:-1])


# ------------------------------------------------------------------------- post-processing

def test_skin_post_smoothing_and_face_mask():
    neutral = np.array([0, 0, 0, 0, 1, 0, 0, 2, 0], float)        # 3 vertices at y = 0, 1, 2
    m = a2f.face_mask_lower(neutral, 0.6, 0.0085)
    assert m[0] > 0.999 and m[2] < 0.001                          # low vertex = lower face
    zeros = np.zeros(9)
    p = dict(MODEL_CONFIG, upper_face_smoothing=0.0)
    d = np.zeros((10, 9))
    d[1:] = 1.0                                                   # unit step at frame 1
    out = a2f.skin_post(d, neutral, zeros, zeros, np.repeat(m, 3), p, 1 / 60)
    lower, upper = out[:, 0], out[:, 6]                           # x components (neutral x = 0)
    assert np.allclose(upper[1:], 1.0, atol=1e-3)                 # upper face unsmoothed
    alpha = 1 - 0.5 ** ((1 / 60) / 0.006)
    s1 = s2 = 0.0
    for t in range(1, 10):
        s1 += (1 - s1) * alpha
        s2 += (s1 - s2) * alpha
        assert abs(lower[t] - s2 * m[0]) < 1e-12                  # two-pole exponential, as in the SDK kernel


def test_eyes_saccade_indexing_and_axes():
    sac = np.arange(20, dtype=float).reshape(10, 2)               # 10 entries: X = 2i, Y = 2i + 1
    p = dict(MODEL_CONFIG, saccade_strength=1.0, eyeballs_strength=0.0, eye_saccade_seed=3)
    out = a2f.eyes_post(np.zeros((5, 4)), sac, p, 1 / 60)
    # live time advances by 30 fps * dt = 0.5 per frame: entries 3, 3, 4, 4, 5
    assert out[:, 0].tolist() == [6, 6, 8, 8, 10] and out[:, 3].tolist() == [7, 7, 9, 9, 11]
    yp = a2f.to_yaw_pitch(np.array([[1.0, 2.0, 3.0, 4.0]]))
    assert yp.tolist() == [[2.0, -1.0, 4.0, -3.0]]                # yaw = rotY, pitch up = -rotX


def test_emotion_vector():
    assert not a2f.emotion_vector(INFO, "neutral", 0.6).any()
    v = a2f.emotion_vector(INFO, "joy", 0.6)
    assert v[6] == pytest.approx(0.6) and v.sum() == pytest.approx(0.6)
    with pytest.raises(ValueError):
        a2f.emotion_vector(INFO, "boredom", 1.0)


# ----------------------------------------------------------------------- runtime (fake ONNX)

class FakeSession:
    """Prediction depends on the audio window, the noise and the GRU state; records every call."""

    def __init__(self):
        self.calls = []

    def run(self, names, feeds):
        self.calls.append({k: v.copy() for k, v in feeds.items()})
        noise, window, state = feeds["noise"], feeds["window"], feeds["input_latents"]
        width = noise.shape[-1]
        pred = 0.01 * noise[:, -1] + window[:, :60, None] + state[0, 0, 0, 0]
        assert pred.shape == (1, 60, width)
        return [pred.astype(np.float32), state + 1.0]


def _model():
    S, U = INFO["params"]["skin_size"], INFO["params"]["tongue_size"]
    neutral, deltas = _mesh(S // 3, 52)
    tn, td = _mesh(U // 3, 16, seed=9)
    rng = np.random.default_rng(7)
    return a2f.Model(
        info=INFO, identity="Claire", identity_index=0, config=dict(MODEL_CONFIG),
        neutral_skin=neutral, neutral_tongue=tn, eye_close=np.zeros(S), lip_open=np.zeros(S),
        saccade=rng.normal(size=(50, 2)),
        skin=a2f.Solver.from_arrays(list(a2f.ARKIT_NAMES), neutral, deltas, _params(52, strengthL2regularization=0.1),
                                    mask=np.array([0, 2])),
        tongue=a2f.Solver.from_arrays([f"t{i}" for i in range(16)], tn, td, _params(16)))


def test_run_feeds_the_sdk_windows_and_carries_gru_state():
    model, sess = _model(), FakeSession()
    audio = np.arange(40000, dtype=np.float32) / 40000
    seen = []
    out = a2f.run(audio, model, sess, dict(MODEL_CONFIG), np.zeros(10, np.float32), 0,
                  progress=lambda d, n: seen.append((d, n)))
    plan = out["plan"]
    assert len(sess.calls) == plan.n_inferences == len(seen) and seen[-1] == (plan.n_inferences, plan.n_inferences)
    assert out["skin"].shape == (plan.n_frames, 6) and out["tongue"].shape == (plan.n_frames, 6)
    for k, c in enumerate(sess.calls):
        start = plan.windows.start(k)
        expect = np.zeros(16000, np.float32)
        a, b = max(start, 0), min(start + 16000, len(audio))
        expect[a - start:b - start] = audio[a:b]
        assert np.array_equal(c["window"][0], expect)
        assert np.all(c["input_latents"] == k)                    # state from the previous call
        assert c["identity"].tolist() == [[1.0, 0.0, 0.0]]
        assert c["emotion"].shape == (1, 30, 10)
        assert c["noise"].shape == (1, 3, 60, 12 + 6 + 15 + 4) and c["noise"].dtype == np.float32


def test_seed_determinism():
    model = _model()
    audio = np.random.default_rng(0).normal(size=20000).astype(np.float32)

    def go(seed):
        return a2f.run(audio, model, FakeSession(), dict(MODEL_CONFIG), np.zeros(10, np.float32), seed)

    a, b, c = go(5), go(5), go(6)
    assert np.array_equal(a["skin"], b["skin"]) and np.array_equal(a["eyes"], b["eyes"])
    assert not np.array_equal(a["skin"], c["skin"])


def test_noise_is_independent_of_prefetch_order():
    shape = (1, 3, 60, 37)
    n1 = a2f.DiffusionNoise(3, shape)
    n2 = a2f.DiffusionNoise(3, shape)
    try:
        n2.prefetch(1)
        n2.prefetch(0)
        x0, x1 = n1(0), n1(1)
        assert np.array_equal(x1, n2(1)) and np.array_equal(x0, n2(0))
        assert not np.array_equal(x0, x1)
        assert abs(float(x0.std()) - 1.0) < 0.05
    finally:
        n1.close()
        n2.close()


def test_infer_with_fake_runtime(monkeypatch):
    model = _model()
    monkeypatch.setattr(a2f, "download", lambda *a, **k: a2f.MODEL_DIR)
    monkeypatch.setattr(a2f, "load_model", lambda identity: model)
    monkeypatch.setattr(a2f, "_session", lambda path, device, log: (FakeSession(), "cpu"))
    audio = np.random.default_rng(1).normal(size=44100).astype(np.float32) * 0.1
    r = a2f.infer(audio, 44100, AudioConfig(emotion="joy", blink_strength=0.0), log=lambda *a: None)
    assert r.fps == 60.0 and len(r.t) == 60 and r.t[0] == 0.0
    assert set(r.blendshapes) == set(a2f.ARKIT_NAMES)
    assert all(v.shape == (60,) and np.all((v >= 0) & (v <= 1)) for v in r.blendshapes.values())
    assert not r.blendshapes["eyeBlinkLeft"].any()
    assert r.eyes.shape == (60, 4) and len(r.tongue) == 16
    assert r.meta["emotion_vector"]["joy"] == pytest.approx(0.6) and r.meta["device"] == "cpu"


def test_available_reports_missing_runtime(monkeypatch):
    def missing():
        raise ImportError("onnxruntime is missing (No module named 'onnxruntime'); " + a2f.INSTALL_HINT)
    monkeypatch.setattr(a2f, "_ort", missing)
    ok, why = a2f.available()
    assert not ok and "[audio]" in why
