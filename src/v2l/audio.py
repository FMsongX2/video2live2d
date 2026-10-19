"""Audio-driven inputs: Audio2Face-3D blendshapes -> calibrated, rest-centred V2L inputs, the
generated head/body/blink motion for audio-only input, and the mixing rules with a tracked video.

Time: every function works on absolute times of the audio file in seconds. A tracked video frame
at clip time tv (relative to input.start) shows the audio-driven face of absolute audio time
input.start + tv - audio.offset, so a positive offset moves the face later.

Inputs derived from the blendshapes (each clipped to its nominal range). Rest: with at least 0.3 s
of pauses in the run, the median of JawOpen and LipOpen over the pauses is their zero (the model's
resting mouth is slightly open); without pauses JawOpen keeps 0 and LipOpen uses its 5th percentile.
  JawOpen     = jawOpen - rest
  LipOpen     = 1.2 * max(jawOpen - mouthClose, 0) + 0.25 * (mouthLowerDown{L,R} + mouthUpperUp{L,R})
                - 0.25 * (mouthPress{L,R} + mouthRoll{Upper,Lower}) - rest
  MouthClose  = max(mouthClose, JawOpen - LipOpen)            (same residual rule as the video path)
  LipWidth    = max(mean dimple, mean stretch) - mouthPucker   (signed width, - narrow / + wide)
  MouthShift  = mouthLeft - mouthRight                         (+ = image right, like the video path)
  GazeX/GazeY = mean eye yaw / pitch minus its median, over 26 / 22 degrees (the angles an iris
                offset of calibration.gaze_full's defaults corresponds to), or 0 with idle.gaze=center
  TongueOut   = max(tongueOut, tongue pose tongueStretch); NVIDIA's skin solve keeps tongueOut
                inactive, so in practice it is the protrusion of the tongue solve (rare in speech)
  CheekPuff and every other ARKit channel: the model's value as-is.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re

import numpy as np
from scipy.signal import butter, filtfilt

from . import media
from .assets import CACHE
from .config import AudioConfig, IdleConfig
from .errors import AudioUnavailable, UnusableInput


GAZE_FULL_DEG = (26.0, 22.0)
HOP_S = 0.01                     # loudness envelope hop
SILENCE_DBFS = -50.0             # quieter than this everywhere = no usable speech
QUIET = 0.05                     # loudness below this (about 33 dB under the speech level) is a pause
# Speech channels for audio.use=mouth; smile and frown are expression and stay with the video.
MOUTH = re.compile(r"Jaw\w*|Mouth(?!Smile|Frown)\w*|LipOpen|LipWidth|TongueOut|CheekPuff")
HEAD_BODY = ("yaw", "pitch", "roll", "shoulderRoll", "shoulderYaw", "shoulderY")
# A2F settings that do not change the model run (handled here, not by a2f.infer).
NOT_MODEL = ("use", "offset", "device")
CONTEXT_S = 1.0                  # audio decoded beyond input.start/end on each side (audio.offset range)
GRID_FPS = 60.0                  # time grid of audio-only input before resampling to output.fps


# ------------------------------------------------------------------------------------- tracks

@dataclasses.dataclass
class Track:
    path: pathlib.Path
    samples: np.ndarray          # mono float32 at media.SAMPLE_RATE
    t0: float                    # absolute time of samples[0] in the file
    info: dict                   # media.probe() of the file
    sr: int = media.SAMPLE_RATE

    @property
    def t1(self) -> float:
        return self.t0 + len(self.samples) / self.sr


def load(path: pathlib.Path, start: float | None, end: float | None) -> Track:
    """The audio of [start - 1 s, end + 1 s] (clipped to the file), so audio.offset can shift within it."""
    info = media.probe(path) or {"duration_s": None, "has_video": None, "has_audio": None, "sample_rate": None,
                                 "channels": None}
    if info.get("has_audio") is False:
        raise UnusableInput(f"{path.name} has no audio track")
    t0 = max(0.0, (start or 0.0) - CONTEXT_S)
    dur = None if end is None else end + CONTEXT_S - t0
    try:
        samples = media.decode(path, t0, dur)
    except ValueError as e:
        raise UnusableInput(str(e)) from None
    except RuntimeError as e:
        raise UnusableInput(str(e)) from None
    if len(samples) < media.SAMPLE_RATE // 10:
        raise UnusableInput(f"{path.name}: less than 0.1 s of audio in the selected range")
    return Track(path=path, samples=samples, t0=t0, info=info)


def clip_span(track: Track, start: float | None, end: float | None) -> tuple[float, float]:
    """[start, end] of the used part in absolute file time (audio-only input)."""
    a = start or 0.0
    file_end = track.info.get("duration_s") or track.t1
    b = min(end, file_end) if end is not None else file_end
    b = min(b, track.t1)
    if b - a < 0.1:
        raise UnusableInput("the selected audio range is shorter than 0.1 s")
    return a, b


# ----------------------------------------------------------------------------------- loudness

def envelope(track: Track) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(absolute times, RMS level in dBFS, loudness 0..1) every HOP_S seconds. Loudness is the level
    within the 35 dB below the clip's 95th percentile."""
    hop = int(track.sr * HOP_S)
    win = 2 * hop
    x = track.samples.astype(np.float64)
    n = max(1, (len(x) - win) // hop + 1)
    idx = np.arange(n)[:, None] * hop + np.arange(win)[None, :]
    frames = np.pad(x, (0, max(0, idx.max() + 1 - len(x))))[idx]
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    db = 20 * np.log10(rms + 1e-9)
    hi = float(np.percentile(db, 95))
    loud = np.clip((db - (hi - 35.0)) / 35.0, 0.0, 1.0)
    t = track.t0 + (np.arange(n) * hop + win / 2) / track.sr
    return t, db, loud


def diagnostics(track: Track, span: tuple[float, float]) -> dict:
    """Level numbers of the used span; raises UnusableInput when it is silent."""
    a, b = span
    s = track.samples[int(max(0.0, a - track.t0) * track.sr):int(max(0.0, b - track.t0) * track.sr)]
    if len(s) == 0:
        raise UnusableInput("the selected range contains no audio")
    t, db, _ = envelope(track)
    inside = (t >= a) & (t <= b)
    level = db[inside] if inside.any() else db
    rms = float(np.sqrt(np.mean(s.astype(np.float64) ** 2)))
    peak = float(np.percentile(level, 99))
    if peak < SILENCE_DBFS:
        raise UnusableInput(f"the audio is silent (loudest part {peak:.0f} dBFS); no speech to animate")
    return {"rms_dbfs": round(20 * np.log10(rms + 1e-9), 2), "peak_dbfs": round(peak, 2),
            "speech_ratio": round(float(np.mean(level > max(peak - 25.0, SILENCE_DBFS))), 3)}


# ------------------------------------------------------------------------------------ A2F run

def _digest(path: pathlib.Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def model_settings(cfg: AudioConfig) -> dict:
    return {k: v for k, v in dataclasses.asdict(cfg).items() if k not in NOT_MODEL}


def cache_key(track: Track, cfg: AudioConfig) -> str:
    h = hashlib.sha1(json.dumps([_digest(track.path), round(track.t0, 4), len(track.samples),
                                 model_settings(cfg)], sort_keys=True).encode())
    return h.hexdigest()[:20]


def require():
    """The v2l.a2f module, or AudioUnavailable with the reason (checked before any slow stage)."""
    try:
        from . import a2f
    except ImportError as e:
        raise AudioUnavailable(f"Audio2Face-3D is not installed ({e}); pip install -e '.[audio]'") from None
    ok, reason = a2f.available()
    if not ok:
        raise AudioUnavailable(f"Audio2Face-3D unavailable: {reason}")
    return a2f


def run_a2f(track: Track, cfg: AudioConfig, log=print, progress=None) -> dict:
    """{"fps", "t" (absolute), "bs" {camelCase: (T,)}, "eyes" (T, 4), "meta"}; cached per audio
    content, decoded span and model settings (audio.use, audio.offset and audio.device excluded)."""
    cache = CACHE / "a2f" / f"{cache_key(track, cfg)}.npz"
    if cache.exists():
        d = np.load(cache)
        meta = json.loads(str(d["meta"]))
        return {"fps": float(d["fps"]), "t": d["t"], "eyes": d["eyes"], "meta": {**meta, "cached": True},
                "bs": {str(k): d["bs"][:, i] for i, k in enumerate(d["names"])},
                "tongue": {str(k): d["tongue"][:, i] for i, k in enumerate(d["tongue_names"])}}
    a2f = require()
    log(f"Audio2Face-3D on {track.path.name} ({len(track.samples) / track.sr:.1f} s)")
    kw = {"progress": progress} if progress is not None else {}
    try:
        res = a2f.infer(track.samples, track.sr, cfg, log=log, **kw)
    except (ImportError, OSError) as e:
        raise AudioUnavailable(f"Audio2Face-3D failed to start: {e}") from None
    names = sorted(res.blendshapes)
    tnames = sorted(getattr(res, "tongue", None) or {})
    out = {"fps": float(res.fps), "t": np.asarray(res.t, dtype=np.float64) + track.t0,
           "eyes": np.asarray(res.eyes, dtype=np.float64),
           "bs": {k: np.asarray(res.blendshapes[k], dtype=np.float64) for k in names},
           "tongue": {k: np.asarray(res.tongue[k], dtype=np.float64) for k in tnames}, "meta": dict(res.meta)}
    n = len(out["t"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".part.npz")
    np.savez_compressed(tmp, fps=out["fps"], t=out["t"], eyes=out["eyes"], names=np.array(names),
                        bs=np.stack([out["bs"][k] for k in names], 1) if names else np.zeros((n, 0)),
                        tongue_names=np.array(tnames),
                        tongue=np.stack([out["tongue"][k] for k in tnames], 1) if tnames else np.zeros((n, 0)),
                        meta=json.dumps(out["meta"], default=str))
    tmp.replace(cache)
    return {**out, "meta": {**out["meta"], "cached": False}}


# ---------------------------------------------------------------------------------- assembly

def inputs(run: dict, idle: IdleConfig, quiet: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Calibrated, rest-centred inputs on run["t"] (see the module docstring). quiet: frames
    without speech; with at least 0.3 s of them, JawOpen and LipOpen are measured from their
    median there (the model's resting mouth is slightly open)."""
    bs = {k: np.clip(np.nan_to_num(v), 0.0, 1.0) for k, v in run["bs"].items()}
    n = len(run["t"])
    zero = np.zeros(n)

    def b(name: str) -> np.ndarray:
        return bs.get(name, zero)

    x = {k[0].upper() + k[1:]: v for k, v in bs.items()}
    lip = np.clip(1.2 * np.maximum(b("jawOpen") - b("mouthClose"), 0.0)
                  + 0.25 * (b("mouthLowerDownLeft") + b("mouthLowerDownRight") + b("mouthUpperUpLeft")
                            + b("mouthUpperUpRight"))
                  - 0.25 * (b("mouthPressLeft") + b("mouthPressRight") + b("mouthRollUpper") + b("mouthRollLower")),
                  0.0, None)
    rest_frames = quiet is not None and n > 1 and quiet.sum() * float(np.median(np.diff(run["t"]))) >= 0.3
    jaw_rest = float(np.median(b("jawOpen")[quiet])) if rest_frames else 0.0
    lip_rest = float(np.median(lip[quiet])) if rest_frames else (float(np.percentile(lip, 5)) if n else 0.0)
    jaw = np.clip(b("jawOpen") - jaw_rest, 0.0, 1.0)
    x["JawOpen"] = jaw
    x["LipOpen"] = np.clip(lip - lip_rest, 0.0, 1.0)
    x["MouthClose"] = np.maximum(b("mouthClose"), np.clip(jaw - x["LipOpen"], 0.0, 1.0))
    wide = np.maximum(0.5 * (b("mouthDimpleLeft") + b("mouthDimpleRight")),
                      0.5 * (b("mouthStretchLeft") + b("mouthStretchRight")))
    x["LipWidth"] = np.clip(wide - b("mouthPucker"), -1.0, 1.0)
    x["MouthShift"] = np.clip(b("mouthLeft") - b("mouthRight"), -1.0, 1.0)
    stretch = np.clip(np.nan_to_num((run.get("tongue") or {}).get("tongueStretch", zero)), 0.0, 1.0)
    x["TongueOut"] = np.maximum(x.get("TongueOut", zero), stretch)
    x.setdefault("CheekPuff", zero)
    eyes = np.nan_to_num(run["eyes"]) if len(run["eyes"]) else np.zeros((n, 4))
    if idle.gaze == "center":
        x["GazeX"], x["GazeY"] = zero.copy(), zero.copy()
        for k in [k for k in x if k.startswith("EyeLook")]:
            x[k] = zero.copy()
    else:
        gx, gy = (eyes[:, 0] + eyes[:, 2]) / 2, (eyes[:, 1] + eyes[:, 3]) / 2
        x["GazeX"] = np.clip((gx - np.median(gx)) / GAZE_FULL_DEG[0], -1.0, 1.0)
        x["GazeY"] = np.clip((gy - np.median(gy)) / GAZE_FULL_DEG[1], -1.0, 1.0)
    return x


def at(x: dict[str, np.ndarray], t_src: np.ndarray, t_abs: np.ndarray) -> dict[str, np.ndarray]:
    """Every input linearly interpolated at absolute times t_abs; rest (0) outside the run."""
    return {k: np.interp(t_abs, t_src, v, left=0.0, right=0.0) for k, v in x.items()}


def from_audio(name: str, use: str) -> bool:
    """Does input `name` come from the audio? use: mouth | face (video + audio) or all (audio only).
    mouth: jaw, lips, mouth shape, tongue and cheek puff (not the smile and frown, which are
    expression). face: every face channel except gaze
    (GazeX/GazeY and the EyeLook* blendshapes). Head and body never come from the audio model."""
    if name in HEAD_BODY or name in ("face_ok", "pose_ok"):
        return False
    if use == "all":
        return True
    if name in ("GazeX", "GazeY") or name.startswith("EyeLook"):
        return False
    if use == "face":
        return True
    return bool(MOUTH.fullmatch(name))


def mix(video: dict[str, np.ndarray], audio: dict[str, np.ndarray], use: str) -> tuple[dict[str, np.ndarray], list[str]]:
    """Video inputs with the channels `use` takes from the audio replaced (both on the video frame
    times); returns (inputs, names taken from the audio)."""
    out = dict(video)
    taken = sorted(k for k in audio if from_audio(k, use))
    for k in taken:
        out[k] = audio[k]
    return out, taken


# --------------------------------------------------------------------------- generated motion

def _band(rng: np.random.Generator, n: int, fs: float, hz: float) -> np.ndarray:
    """Unit-variance noise band-limited below hz (zero-phase Butterworth)."""
    pad = int(3 * fs / max(hz, 1e-3)) + 8
    w = rng.standard_normal(n + 2 * pad)
    if hz < fs / 2:
        bb, aa = butter(2, hz / (fs / 2))
        w = filtfilt(bb, aa, w)
    w = w[pad:pad + n]
    sd = float(np.std(w))
    return w / sd if sd > 0 else w


def _hann_smooth(v: np.ndarray, fs: float, seconds: float) -> np.ndarray:
    """Smoothing with a positive kernel: no overshoot, so bounds of v hold."""
    k = max(int(round(seconds * fs)), 1) | 1
    if k < 3 or len(v) < 2:
        return v
    w = np.hanning(k + 2)[1:-1]
    w /= w.sum()
    return np.convolve(np.pad(v, k // 2, mode="edge"), w, mode="valid")


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def bounds(cfg: IdleConfig) -> dict[str, float]:
    """Largest absolute value (degrees, face heights for shoulderY) the generated motion can reach."""
    a, s, bd = cfg.head_amplitude, cfg.speech_nods, cfg.body_amplitude
    head = {"yaw": a + 1.0 * s, "pitch": 0.6 * a + 2.5 * s, "roll": 0.5 * a + 1.0 * s}
    return {**head, "shoulderYaw": bd * (0.4 * head["yaw"] + 1.0), "shoulderRoll": bd * 0.4 * head["roll"],
            "shoulderY": bd * 0.004 * head["pitch"]}


def idle(t: np.ndarray, loud: np.ndarray, cfg: IdleConfig) -> tuple[dict[str, np.ndarray], dict]:
    """Head and body motion for audio-only input on a uniform grid t (seconds), deterministic per
    cfg.seed. loud: speech loudness 0..1 on t.

    head: band-limited noise (head_speed Hz, a slower drift at a third of it), soft-limited with
          tanh to head_amplitude (yaw), 0.6x (pitch) and 0.5x (roll);
    speech: per phrase (speech between pauses over 0.25 s) a small held turn and tilt, a slight
          raise of the head while speaking and a nod at the strongest loudness onsets, at most one
          per second (speech_nods scales all three; nods are at most 2.5 degrees);
    body: shoulders follow the head 0.25 s later at 0.4x (body_amplitude), plus a slow sway.
    Returns (inputs yaw, pitch, roll, shoulderYaw, shoulderRoll, shoulderY; statistics)."""
    n = len(t)
    fs = 1.0 / float(np.median(np.diff(t))) if n > 1 else 60.0
    rng = np.random.default_rng(cfg.seed)
    a, hz, s = cfg.head_amplitude, cfg.head_speed, cfg.speech_nods

    def sway(scale: float, speed: float) -> np.ndarray:
        z = 0.8 * _band(rng, n, fs, speed) + 0.6 * _band(rng, n, fs, speed / 3)
        return scale * np.tanh(0.7 * z)

    yaw, pitch, roll = sway(a, hz), sway(0.6 * a, 0.8 * hz), sway(0.5 * a, 0.6 * hz)

    # Phrases: speech runs separated by pauses longer than 0.25 s.
    speaking = _hann_smooth(loud, fs, 0.1) > 0.35
    for i0, i1 in _runs(~speaking):
        if (i1 - i0) / fs < 0.25 and i0 > 0 and i1 < n:
            speaking[i0:i1] = True
    phrases = _runs(speaking)
    turn, tilt = np.zeros(n), np.zeros(n)
    for i0, i1 in phrases:
        turn[i0:i1] = rng.uniform(-1.0, 1.0)
        tilt[i0:i1] = rng.uniform(-1.0, 1.0)
    turn, tilt = _hann_smooth(turn, fs, 0.6), _hann_smooth(tilt, fs, 0.6)
    engaged = _hann_smooth(loud * speaking, fs, 1.0)

    # Nods: a dip of the head shortly after the strongest loudness onsets (rises of the smoothed
    # envelope), strongest first, at least a second apart: emphasis, not every syllable.
    env = _hann_smooth(loud, fs, 0.15)
    rise = np.gradient(env) * fs
    peaks = [i for i in range(1, n - 1) if rise[i] > 2.0 and rise[i] >= rise[i - 1] and rise[i] > rise[i + 1]]
    onsets: list[int] = []
    for i in sorted(peaks, key=lambda k: -rise[k]):
        if all(abs(i - o) / fs >= 1.0 for o in onsets):
            onsets.append(i)
    onsets.sort()
    nod = np.zeros(n)
    width = max(int(0.6 * fs), 3)
    shape = np.hanning(width)
    for i in onsets:
        j = i + int(0.1 * fs)
        if j >= n:
            continue
        seg = shape[:max(0, min(width, n - j))] * (0.5 + 0.5 * float(env[min(j + width // 2, n - 1)]))
        nod[j:j + len(seg)] += 1.5 * seg
    nod = np.minimum(nod, 2.5)

    yaw = yaw + s * turn
    roll = roll + s * tilt
    pitch = pitch + s * (engaged - nod)

    lag = int(round(0.25 * fs))

    def follow(v: np.ndarray) -> np.ndarray:
        return _hann_smooth(np.concatenate([np.full(lag, v[0]), v[:n - lag]]) if 0 < lag < n else v, fs, 0.5)

    bd = cfg.body_amplitude
    x = {"yaw": yaw, "pitch": pitch, "roll": roll,
         "shoulderYaw": bd * (0.4 * follow(yaw) + np.tanh(0.7 * _band(rng, n, fs, 0.1))),
         "shoulderRoll": bd * 0.4 * follow(roll),
         "shoulderY": -bd * 0.004 * follow(pitch)}
    stats = {"seed": cfg.seed, "phrases": len(phrases), "nods": len(onsets),
             "peak_deg": {k: round(float(np.max(np.abs(x[k]))) if n else 0.0, 3) for k in ("yaw", "pitch", "roll")},
             "bounds_deg": {k: round(v, 3) for k, v in bounds(cfg).items() if k in ("yaw", "pitch", "roll")}}
    return x, stats


def blinks(t: np.ndarray, cfg: IdleConfig) -> tuple[np.ndarray, int]:
    """Procedural blinks on t: 0..1 closure (fast close, short hold, slower open), intervals drawn
    from a gamma distribution with mean 60 / blink_rate seconds (at least 0.6 s apart)."""
    rng = np.random.default_rng(cfg.seed + 7919)
    out = np.zeros(len(t))
    if len(t) == 0:
        return out, 0
    mean = 60.0 / cfg.blink_rate
    at_s = t[0] + rng.uniform(0.3, min(2.0, mean))
    count = 0
    while at_s < t[-1]:
        rel = t - at_s
        close = np.clip(rel / 0.07, 0, 1)
        open_ = np.clip(1 - (rel - 0.10) / 0.15, 0, 1)
        v = np.where(rel < 0, 0.0, np.where(rel < 0.07, close, np.where(rel < 0.10, 1.0, open_)))
        out = np.maximum(out, v * v * (3 - 2 * v))
        count += 1
        at_s += max(0.6, float(rng.gamma(3.0, mean / 3.0)))
    return out, count


def apply_blinks(x: dict[str, np.ndarray], t: np.ndarray, cfg: IdleConfig) -> int | None:
    """idle.blinks on audio-driven inputs: model keeps the model's eye closing (Audio2Face-3D v3.0
    does not blink), procedural adds generated blinks on top of it, off removes it. Returns the
    procedural blink count (None otherwise)."""
    if cfg.blinks == "model":
        return None
    if cfg.blinks == "off":
        for side in ("Left", "Right"):
            x[f"EyeBlink{side}"] = np.zeros(len(t))
        return None
    v, count = blinks(t, cfg)
    for side in ("Left", "Right"):
        x[f"EyeBlink{side}"] = np.maximum(x.get(f"EyeBlink{side}", 0.0), v)
    return count
