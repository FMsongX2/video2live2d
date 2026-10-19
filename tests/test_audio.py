"""Audio input: input kinds, decoding, mixing rules, time alignment, generated motion, run report.

Audio2Face-3D itself is replaced by a fake run (tests/test_a2f.py covers the model port)."""

import dataclasses
import json
import pathlib
import subprocess
import sys
import wave

import numpy as np
import pytest

import synthetic as S
from v2l import audio as A, media, pipeline
from v2l.config import Config, IdleConfig, merge
from v2l.errors import UnusableInput

SR = 44100


def write_wav(path: pathlib.Path, seconds: float = 4.0, channels: int = 2, bursts=((0.5, 1.5), (2.0, 3.2))) -> pathlib.Path:
    """Tone bursts (speech-like loudness on and off)."""
    t = np.arange(int(seconds * SR)) / SR
    on = np.zeros_like(t)
    for a, b in bursts:
        on[(t >= a) & (t < b)] = 1.0
    x = 0.3 * on * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    pcm = (np.repeat(x[:, None], channels, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def fake_run(track: A.Track, cfg, log=print, progress=None) -> dict:
    """An Audio2Face-3D stand-in: the jaw follows the loudness, one eye-yaw step, model blinks."""
    te, _, loud = A.envelope(track)
    t = track.t0 + np.arange(int((track.t1 - track.t0) * 60)) / 60
    jaw = np.interp(t, te, loud)
    bs = {name: np.zeros(len(t)) for name in S.ARKIT + ["tongueOut"]}
    bs["jawOpen"] = 0.6 * jaw
    bs["mouthClose"] = 0.1 * jaw
    bs["mouthLeft"] = 0.2 * (t > 1.0)
    bs["browInnerUp"] = 0.3 * jaw
    bs["eyeBlinkLeft"] = bs["eyeBlinkRight"] = ((t % 2.0) < 0.1).astype(float)
    bs["eyeLookOutLeft"] = np.full(len(t), 0.4)
    eyes = np.zeros((len(t), 4))
    eyes[:, 0] = eyes[:, 2] = np.where(t > 2.0, 13.0, 0.0)
    return {"fps": 60.0, "t": t, "bs": bs, "eyes": eyes, "meta": {"model": "fake", "cached": False}}


# ---------------------------------------------------------------------------------- input kind

def test_input_kind_by_extension_and_streams(tmp_path):
    for ext in (".wav", ".MP3", ".m4a", ".aac", ".flac", ".ogg", ".opus"):
        assert media.kind(tmp_path / f"take{ext}") == "audio"
    assert media.kind(tmp_path / "clip.mp4") == "video"
    no_video = {"duration_s": 3.0, "has_video": False, "has_audio": True, "sample_rate": 48000, "channels": 2}
    assert media.kind(tmp_path / "voice.mp4", no_video) == "audio"
    assert media.kind(tmp_path / "clip.mp4", {**no_video, "has_video": True}) == "video"


def test_plan_rejects_impossible_combinations(tmp_path):
    wav = write_wav(tmp_path / "v.wav")
    plan = media.plan(wav, None, "off")
    assert plan["kind"] == "audio" and plan["use"] == "all" and plan["audio"] == str(wav) and plan["video"] is None
    with pytest.raises(ValueError, match="already an audio file"):
        media.plan(wav, wav, "mouth")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    with pytest.raises(ValueError, match="audio.use"):
        media.plan(clip, wav, "off")
    p = media.plan(clip, wav, "face")
    assert p["kind"] == "video+audio" and p["audio_from"] == "file" and p["audio"] == str(wav)
    assert media.plan(clip, None, "mouth")["audio_from"] == "video"
    assert media.plan(clip, None, "off")["kind"] == "video"


@pytest.mark.skipif(media.ffmpeg() is None, reason="no ffmpeg")
def test_decode_mono_16k_with_trim_and_probe(tmp_path):
    wav = write_wav(tmp_path / "v.wav", seconds=3.0, channels=2)
    info = media.probe(wav)
    assert info["has_audio"] and not info["has_video"] and info["sample_rate"] == SR and info["channels"] == 2
    assert abs(info["duration_s"] - 3.0) < 0.05
    x = media.decode(wav)
    assert x.dtype == np.float32 and abs(len(x) - 3 * media.SAMPLE_RATE) < 400
    part = media.decode(wav, start=1.0, duration=0.5)
    assert abs(len(part) - 0.5 * media.SAMPLE_RATE) < 200
    opus = tmp_path / "v.opus"
    r = subprocess.run([media.ffmpeg(), "-v", "error", "-i", str(wav), "-c:a", "libopus", str(opus)])
    if r.returncode == 0:
        assert abs(len(media.decode(opus)) - 3 * media.SAMPLE_RATE) < 1600


@pytest.mark.skipif(media.ffmpeg() is None, reason="no ffmpeg")
def test_silent_audio_is_unusable(tmp_path):
    wav = write_wav(tmp_path / "silent.wav", bursts=())
    track = A.load(wav, None, None)
    with pytest.raises(UnusableInput, match="silent"):
        A.diagnostics(track, A.clip_span(track, None, None))


# ------------------------------------------------------------------------------------- mixing

def test_mixing_rules():
    names = ["JawOpen", "LipOpen", "LipWidth", "MouthShift", "MouthClose", "MouthSmileLeft", "TongueOut",
             "CheekPuff", "JawLeft", "BrowInnerUp", "EyeBlinkLeft", "EyeWideRight", "EyeLookOutLeft", "GazeX",
             "GazeY", "NoseSneerLeft", "CheekSquintLeft", "yaw", "pitch", "roll", "shoulderYaw", "shoulderY"]
    mouth = {n for n in names if A.from_audio(n, "mouth")}
    face = {n for n in names if A.from_audio(n, "face")}
    every = {n for n in names if A.from_audio(n, "all")}
    assert mouth == {"JawOpen", "LipOpen", "LipWidth", "MouthShift", "MouthClose", "TongueOut", "CheekPuff", "JawLeft"}
    assert face == mouth | {"MouthSmileLeft", "BrowInnerUp", "EyeBlinkLeft", "EyeWideRight", "NoseSneerLeft",
                            "CheekSquintLeft"}
    assert every == face | {"EyeLookOutLeft", "GazeX", "GazeY"}
    video = {n: np.full(3, 1.0) for n in names}
    aud = {n: np.full(3, 2.0) for n in names}
    out, taken = A.mix(video, aud, "mouth")
    assert taken == sorted(mouth)
    assert all(out[n][0] == (2.0 if n in mouth else 1.0) for n in names)


def test_assembled_inputs_from_blendshapes():
    n = 21
    t = np.arange(n) / 60
    z = np.zeros(n)
    jaw = np.full(n, 0.5)
    jaw[:2] = 0.0  # a closed-mouth stretch sets the LipOpen rest
    run = {"t": t, "eyes": np.tile([[10.0, 5.0, 14.0, 5.0]], (n, 1)),
           "bs": {"jawOpen": jaw, "mouthClose": np.full(n, 0.2), "mouthLeft": np.full(n, 0.3),
                  "mouthRight": z, "mouthPucker": np.full(n, 0.4), "mouthStretchLeft": np.full(n, 0.2),
                  "mouthStretchRight": np.full(n, 0.2), "tongueOut": z},
           "tongue": {"tongueStretch": np.full(n, 0.7), "tongueIn": np.full(n, 2.0)}}
    x = A.inputs(run, IdleConfig())
    assert x["JawOpen"][-1] == 0.5 and x["TongueOut"][-1] == 0.7 and x["CheekPuff"][-1] == 0.0
    assert x["LipOpen"][0] == 0.0 and x["LipOpen"][-1] == pytest.approx(1.2 * 0.3)
    assert x["MouthClose"][-1] == pytest.approx(max(0.2, 0.5 - 0.36))
    assert x["LipWidth"][-1] == pytest.approx(0.2 - 0.4)
    assert x["MouthShift"][-1] == pytest.approx(0.3)  # performer's left = image right = +
    assert np.all(x["GazeX"] == 0)  # rest-centred: a constant gaze is the rest gaze
    run["eyes"][15:, [0, 2]] = 36.0
    x = A.inputs(run, IdleConfig())
    assert x["GazeX"][-1] == pytest.approx((36.0 - 12.0) / A.GAZE_FULL_DEG[0])
    assert np.all(A.inputs(run, IdleConfig(gaze="center"))["GazeX"] == 0)
    quiet = np.zeros(n, bool)
    quiet[2:12] = True  # 10 frames at 60 fps < 0.3 s: too short to measure a rest
    assert A.inputs(run, IdleConfig(), quiet)["JawOpen"][-1] == 0.5
    run["t"] = np.arange(n) / 20.0  # now 0.5 s of pauses: the rest is their median (jaw 0.5)
    assert A.inputs(run, IdleConfig(), quiet)["JawOpen"][-1] == 0.0


def test_offset_and_trim_alignment():
    """An event at file time 2.0 s shows at clip time 2.0 - input.start + audio.offset."""
    t_abs = np.arange(0, 5, 1 / 60)
    x = {"JawOpen": (np.abs(t_abs - 2.0) < 1e-6).astype(float)}
    for start, offset in ((0.0, 0.0), (0.5, 0.0), (0.5, 0.25), (1.0, -0.5)):
        clip_t = np.arange(0, 3, 1 / 60)
        got = A.at(x, t_abs, start + clip_t - offset)["JawOpen"]
        assert clip_t[int(np.argmax(got))] == pytest.approx(2.0 - start + offset, abs=1e-6)
    assert A.at(x, t_abs, np.array([-1.0, 9.0]))["JawOpen"].tolist() == [0.0, 0.0]


# ------------------------------------------------------------------------------ generated motion

def _loud(t):
    return ((t % 3.0) < 1.8).astype(float) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t) ** 2)


def test_idle_is_deterministic_bounded_and_smooth():
    t = np.arange(0, 30, 1 / 60)
    cfg = IdleConfig(head_amplitude=6.0, speech_nods=1.5, seed=4)
    a, stats = A.idle(t, _loud(t), cfg)
    b, _ = A.idle(t, _loud(t), cfg)
    c, _ = A.idle(t, _loud(t), dataclasses.replace(cfg, seed=5))
    assert all(np.array_equal(a[k], b[k]) for k in a)
    assert not np.allclose(a["yaw"], c["yaw"])
    lim = A.bounds(cfg)
    for k, v in a.items():
        assert np.max(np.abs(v)) <= lim[k] + 1e-9, k
    assert np.std(a["yaw"]) > 0.15 * cfg.head_amplitude  # it does move
    assert stats["nods"] > 3 and stats["phrases"] >= 9
    # Not jittery: frame-to-frame acceleration stays small (degrees per frame squared).
    assert np.percentile(np.abs(np.diff(a["yaw"], 2)), 99) < 0.02
    still, _ = A.idle(t, np.zeros_like(t), IdleConfig(head_amplitude=0.0, speech_nods=0.0, body_amplitude=0.0))
    assert all(np.allclose(v, 0) for v in still.values())


def test_procedural_blinks_rate_and_modes():
    t = np.arange(0, 120, 1 / 60)
    v, n = A.blinks(t, IdleConfig(blink_rate=15.0, seed=2))
    assert 20 <= n <= 42 and v.max() == pytest.approx(1.0) and v.min() == 0.0
    x = {"EyeBlinkLeft": np.full(len(t), 0.5), "EyeBlinkRight": np.full(len(t), 0.5)}
    assert A.apply_blinks(dict(x), t, IdleConfig(blinks="model")) is None
    off = dict(x)
    A.apply_blinks(off, t, IdleConfig(blinks="off"))
    assert off["EyeBlinkLeft"].max() == 0.0
    proc = dict(x)
    assert A.apply_blinks(proc, t, IdleConfig(blinks="procedural", seed=2)) == n


# ------------------------------------------------------------------------- video byte identity

# (sum, sum of squares, sin-weighted sum) of every parameter curve of pipeline.solve on
# synthetic.signals() with default settings, recorded before audio support existed.
GOLDEN = {
    "ParamAngleX": (-83.44525277, 6130.63798, -11.00729971),
    "ParamAngleY": (204.239346, 2399.545699, 2.435545736),
    "ParamAngleZ": (16.79866995, 592.436208, 0.2729507577),
    "ParamBodyAngleX": (-12.51001092, 134.0530308, -1.627701237),
    "ParamBodyAngleY": (196.7946243, 322.0758121, 0.2103386129),
    "ParamBodyAngleZ": (2.502156395, 12.28627127, 0.03577813619),
    "ParamBreath": (132.8048105, 103.6998524, -0.2550634101),
    "ParamBrowLY": (6.070290406, 0.3563115582, 0.002029921906),
    "ParamBrowRY": (4.797652247, 0.2832502236, 0.09104112494),
    "ParamCheek": (61.09161868, 15.56534812, 0.01992870673),
    "ParamCheekPuff": (2.254706245, 0.09864059127, -0.3768824143),
    "ParamEyeBallX": (0.0, 0.0, 0.0),
    "ParamEyeBallY": (0.0, 0.0, 0.0),
    "ParamEyeLOpen": (265.5564132, 294.0817271, 0.3761717805),
    "ParamEyeLSmile": (0.3367089059, 0.002378550143, -0.002454706152),
    "ParamEyeROpen": (270.7705661, 305.7898504, 0.1736764951),
    "ParamEyeRSmile": (0.3367089059, 0.002378550143, -0.002454706152),
    "ParamJawOpen": (52.59883234, 33.85296722, -1.288802511),
    "ParamMouthForm": (52.75336393, 15.34244487, 0.003657546888),
    "ParamMouthFunnel": (0.9550163668, 0.008967569163, 0.01280373027),
    "ParamMouthOpenY": (21.72378969, 6.13638269, -0.7453228474),
    "ParamMouthPressLipOpen": (-1.720094361, 0.03967736012, -0.06641358967),
    "ParamMouthPuckerWiden": (-1.286681827, 0.0285874728, 0.07841703561),
    "ParamMouthShrug": (4.868326896, 0.1206340221, 0.128830972),
    "ParamMouthX": (0.5635979826, 0.009873713754, 0.02891099113),
    "ParamTongueOut": (0.0, 0.0, 0.0),
}


def _fingerprint(params):
    return {k: (v.sum(), (v * v).sum(), (v * np.sin(np.arange(len(v)))).sum()) for k, v in params.items()}


def test_video_solve_unchanged_by_audio_code_and_settings(tmp_path):
    """The video path matches its output from before audio support (to rounding across platforms),
    and audio and idle settings do not touch a video-only run."""
    m = S.model(tmp_path)
    p, _ = pipeline.solve(S.signals(), m, Config())
    got = _fingerprint(p)
    assert set(got) == set(GOLDEN)
    for k, ref in GOLDEN.items():
        np.testing.assert_allclose(got[k], ref, rtol=1e-8, atol=1e-9, err_msg=k)
    changed = merge(Config(), {"audio": {"identity": "Mark", "offset": 0.3, "emotion": "joy"},
                               "idle": {"head_amplitude": 9.0, "blinks": "off", "gaze": "center"}})
    p2, _ = pipeline.solve(S.signals(), m, changed)
    assert all(np.array_equal(p[k], p2[k]) for k in p)


# ---------------------------------------------------------------------------- end-to-end report

@pytest.fixture
def stub_pipeline(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    monkeypatch.setattr(pipeline, "prepare_model", lambda d, log=print: S.model(model_dir))
    monkeypatch.setattr(A, "run_a2f", fake_run)
    monkeypatch.setattr(A, "require", lambda: None)
    return model_dir


@pytest.mark.skipif(media.ffmpeg() is None, reason="no ffmpeg")
def test_audio_only_run_report_and_signals(tmp_path, stub_pipeline):
    wav = write_wav(tmp_path / "take.wav", seconds=4.0)
    cfg = merge(Config(), {"preview": {"enabled": False}, "input": {"start": 0.25, "end": 3.75}})
    stages = []
    rep = pipeline.run(wav, stub_pipeline, "talk", tmp_path / "out", cfg, install=False, log=lambda *a: None,
                       event=lambda e: stages.append(e["stage"]) if e["event"] == "stage" else None,
                       dump_signals=True)
    assert stages == ["probe", "audio", "solving", "writing"]
    assert rep["source"] == {"kind": "audio", "video": None, "audio": str(wav.resolve()), "audio_from": "file",
                             "use": "all"}
    a = rep["audio"]
    assert a["sample_rate"] == SR and a["channels"] == 2 and a["window_s"] == [0.25, 3.75]
    assert a["model"]["model"] == "fake" and 0 < a["speech_ratio"] < 1
    assert rep["idle"]["seed"] == 0 and rep["idle"]["blink_mode"] == "procedural" and rep["idle"]["blinks"] >= 0
    assert rep["face_rate"] == 1.0 and rep["pose_rate"] is None and rep["video"] is None
    sig = json.loads((tmp_path / "out" / "talk.signals.json").read_text())
    assert sig["source"]["kind"] == "audio" and "JawOpen" in sig["source"]["audio_inputs"]
    assert {"yaw", "pitch", "roll", "shoulderYaw"} <= set(sig["source"]["generated"])
    n = len(sig["inputs"]["JawOpen"])
    assert abs(n - 3.5 * 30) <= 2 and len(sig["audio"]["loudness"]) == n
    jaw, loud = np.array(sig["inputs"]["JawOpen"]), np.array(sig["audio"]["loudness"])
    assert np.corrcoef(jaw, loud)[0, 1] > 0.9  # the fake jaw follows loudness; alignment kept
    motion = json.loads((tmp_path / "out" / "talk.motion3.json").read_text())
    assert motion["Meta"]["Duration"] == pytest.approx(3.5, abs=0.05)


@pytest.mark.skipif(media.ffmpeg() is None, reason="no ffmpeg")
def test_video_with_audio_mouth_takes_mouth_from_audio(tmp_path, stub_pipeline, monkeypatch):
    wav = write_wav(tmp_path / "voice.wav", seconds=9.0)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not decoded: tracking is stubbed")
    monkeypatch.setattr(media, "probe", lambda p: {"duration_s": 9.0, "has_video": True, "has_audio": True,
                                                  "sample_rate": SR, "channels": 2}
                        if str(p).endswith(".mp4") else {"duration_s": 9.0, "has_video": False, "has_audio": True,
                                                         "sample_rate": SR, "channels": 2})
    monkeypatch.setattr(pipeline, "get_signals", lambda v, cfg, log=print, progress=None: S.signals(n=240))
    cfg = merge(Config(), {"preview": {"enabled": False}, "audio": {"use": "mouth"}})
    rep = pipeline.run(clip, stub_pipeline, "mix", tmp_path / "out", cfg, install=False, log=lambda *a: None,
                       dump_signals=True, audio=wav)
    assert rep["source"]["kind"] == "video+audio" and rep["source"]["audio_from"] == "file"
    assert rep["rare_seconds"] is None and rep["idle"] is None
    sig = json.loads((tmp_path / "out" / "mix.signals.json").read_text())
    assert "JawOpen" in sig["source"]["audio_inputs"] and "BrowInnerUp" not in sig["source"]["audio_inputs"]
    assert sig["source"]["generated"] == []
    jaw, loud = np.array(sig["inputs"]["JawOpen"]), np.array(sig["audio"]["loudness"])
    good = slice(0, 170)  # before the synthetic lost-face gap at 6 s
    assert np.corrcoef(jaw[good], loud[good])[0, 1] > 0.9


# ------------------------------------------------------------------------------------------ CLI

def test_cli_dry_run_reports_the_source(tmp_path):
    model = tmp_path / "m"
    model.mkdir()
    (model / "M.model3.json").write_text("{}")
    wav = write_wav(tmp_path / "take.wav", seconds=1.0)
    r = subprocess.run([sys.executable, "-m", "v2l.cli", "run", str(wav), "--model", str(model), "--name", "n",
                        "--dry-run", "--json"], capture_output=True, text=True)
    res = json.loads(r.stdout)
    assert r.returncode == 0 and res["source"]["kind"] == "audio" and res["source"]["use"] == "all"
    r = subprocess.run([sys.executable, "-m", "v2l.cli", "run", str(wav), "--audio", str(wav), "--model", str(model),
                        "--name", "n", "--dry-run", "--json"], capture_output=True, text=True)
    res = json.loads(r.stdout)
    assert r.returncode == 2 and res["error"]["type"] == "input" and "already an audio file" in res["error"]["message"]


@pytest.mark.skipif(media.ffmpeg() is None, reason="no ffmpeg")
def test_cli_reports_audio_unavailable_before_any_work(tmp_path):
    model = tmp_path / "m"
    model.mkdir()
    (model / "M.model3.json").write_text("{}")
    wav = write_wav(tmp_path / "take.wav", seconds=1.0)
    code = ("import sys; sys.modules['v2l.a2f'] = None; from v2l import cli; "
            f"sys.exit(cli.main(['run', {str(wav)!r}, '--model', {str(model)!r}, '--name', 'n', "
            f"'--out', {str(tmp_path / 'out')!r}, '--json']))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    res = json.loads(r.stdout)
    assert r.returncode == 1 and res["error"]["type"] == "audio_unavailable" and "audio" in res["error"]["message"]
    assert not (tmp_path / "out").exists()
