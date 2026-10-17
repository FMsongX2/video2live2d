"""Whole-clip calibration: features -> ARKit-named inputs at rest = 0.

The least-active window estimates rest. Eye/brow channels use a high-percentile response;
mouth/jaw coefficients retain their unit-scale headroom instead of stretching each clip's
speech to its full range. Jaw displacement, lip aperture and signed lip width stay separate.
These are video-derived controls, not a claim of numerical equivalence to iPhone ARKit.

ARKit side naming: "Left" is the performer's left, which is the image right for un-mirrored
footage (signals.extract(flip=True) fixes mirrored selfie footage first).
Missing frames stay NaN. scale() applies the user's per-group intensity afterwards.
"""

from __future__ import annotations

import re

import numpy as np

from .config import CalibrationConfig, ExpressionConfig, IntensityConfig
from .errors import UnusableInput


def smoothstep(e0: float, e1: float, v: np.ndarray) -> np.ndarray:
    t = np.clip((v - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _pct(v: np.ndarray, q: float) -> float:
    return float(np.nanpercentile(v, q))


def neutral_window(f: dict[str, np.ndarray], t: np.ndarray, seconds: float,
                   head_range: float = 20.0) -> tuple[int, int]:
    """Index range of the window with the least summed, range-normalised expression activity."""
    cues = [f["bs:mouthSmileLeft"], f["bs:mouthSmileRight"], f["jawDrop"], f["lipGap"],
            f["bs:eyeBlinkLeft"], f["bs:eyeBlinkRight"], f["bs:browInnerUp"],
            f["bs:browOuterUpLeft"], f["bs:browOuterUpRight"], f["bs:browDownLeft"], f["bs:browDownRight"]]
    score = np.zeros(len(t))
    for c in cues:
        lo, hi = _pct(c, 2), _pct(c, 98)
        score += np.clip((c - lo) / max(hi - lo, 1e-6), 0, 1)
    # Head turned far from its usual pose is not a rest pose either.
    for k in ("yaw", "pitch"):
        dev = np.abs(f[k] - np.nanmedian(f[k]))
        score += np.clip(dev / head_range, 0, 1)
    score[~np.isfinite(score)] = np.inf  # frames without a face never qualify
    dt = np.median(np.diff(t)) if len(t) > 1 else 1 / 30
    n = int(np.clip(round(seconds / dt), 1, len(t)))
    run = np.convolve(score, np.ones(n) / n, mode="valid")
    if not np.isfinite(run).any():
        raise UnusableInput("no stretch of frames with a face long enough for a rest window")
    i = int(np.argmin(run))
    return i, i + n


def calibrate(f: dict[str, np.ndarray], t: np.ndarray, cfg: CalibrationConfig,
              expressions: ExpressionConfig | None = None) -> tuple[dict[str, np.ndarray], dict]:
    expressions = expressions or ExpressionConfig()
    cal: dict = {}
    x: dict[str, np.ndarray] = {}
    if cfg.neutral_window:
        a, b = cfg.neutral_window
        idx = np.flatnonzero((t >= a) & (t < b) & (f["face_ok"] > 0))
        if len(idx) == 0:
            raise UnusableInput(f"neutral window {a}-{b} s has no frames with a face")
        w0, w1 = int(idx[0]), int(idx[-1]) + 1
    else:
        w0, w1 = neutral_window(f, t, cfg.neutral_seconds, cfg.rest_head_range)
    cal["neutral_window_s"] = [float(t[w0]), float(t[w1 - 1])]

    def rest(v):
        return float(np.nanmedian(v[w0:w1]))

    # Head: zero at the clip's median pose. The performer may face a ring light or a second
    # screen rather than the lens; the avatar should look at the viewer in that pose.
    for k in ("yaw", "pitch", "roll"):
        c = float(np.nanmedian(f[k]))
        cal[f"{k}_center"] = c
        x[k] = f[k] - c

    def onesided(v, name):
        lo = rest(v)
        unit_scale = name.startswith(("mouth", "jaw"))
        hi = 1.0 if unit_scale else _pct(v, cfg.full_percentile)
        gain = min(1.0 / max(hi - lo, cfg.min_span), cfg.max_gain)
        cal[name] = dict(rest=lo, full=hi, gain=gain,
                         normalization="unit_headroom" if unit_scale else "clip_percentile")
        return np.clip((v - lo) * gain, 0.0, 1.0)

    for key in [k for k in f if k.startswith("bs:")]:
        name = key[3:]
        x[name[0].upper() + name[1:]] = onesided(f[key], name)

    # Eyes: landmark aperture (EAR) per image side fused with the blendshape, then a deadzone
    # like a VBridger input curve so tracker noise around the open eye reads as open.
    for side, arkit in (("A", "Right"), ("B", "Left")):
        ear = f[f"ear{side}"]
        open_lvl = max(rest(ear), _pct(ear, 60))
        closed_lvl = min(_pct(ear, 1), open_lvl * cfg.eye_closed_ratio)
        closure = np.clip((open_lvl - ear) / max(open_lvl - closed_lvl, 1e-6), 0.0, 1.0)
        wide = np.clip((ear - open_lvl) / (cfg.eye_wide_range * open_lvl), 0.0, 1.0)
        cal[f"eye{arkit}"] = dict(open=open_lvl, closed=closed_lvl)
        w = cfg.blink_landmark_weight
        blink = np.clip(w * closure + (1.0 - w) * x[f"EyeBlink{arkit}"], 0.0, 1.0)
        x[f"EyeBlink{arkit}"] = smoothstep(*cfg.blink_deadzone, blink)
        x[f"EyeWide{arkit}"] = np.fmax(wide, x[f"EyeWide{arkit}"])

    # Lip aperture must not open the jaw control, and jaw motion must not force parted lips.
    # Average the two jaw observations rather than promoting whichever one is largest.
    jaw0 = rest(f["jawDrop"])
    jaw = np.clip((f["jawDrop"] - jaw0) / (cfg.jaw_full * jaw0), 0.0, 1.0)
    g0 = rest(f["lipGap"])
    gap = np.clip((f["lipGap"] - g0) / cfg.lip_gap_full, 0.0, 1.0)
    cal["jaw"] = dict(rest=jaw0)
    cal["lipGap"] = dict(rest=g0)
    x["JawOpen"] = 0.5 * (jaw + x["JawOpen"])
    x["LipOpen"] = gap
    # Preserve the tracker's independent lip-closure cue. The residual also expresses a
    # lowered jaw with sealed lips to third-party JawOpen - MouthClose bridge formulas.
    x["MouthClose"] = np.maximum(x["MouthClose"], np.clip(x["JawOpen"] - gap, 0.0, 1.0))

    # Cheek puff and tongue out: MediaPipe barely reports them; the zero-shot scores from
    # rare.py (when computed) carry them, on fixed thresholds calibrated across clips.
    if "rare:cheek" in f:
        x["CheekPuff"] = np.fmax(_to_unit(f["rare:cheek"], expressions.cheek_thresholds), x.get("CheekPuff", 0.0))
        x["TongueOut"] = _to_unit(f["rare:tongue"], expressions.tongue_thresholds)

    # Width is one signed control, not synthetic Dimple/Stretch activations that a bridge
    # later sums again. Geometry and tracker coefficients each contribute one bounded vote.
    w0r = rest(f["mouthWidth"])
    narrow = np.clip((w0r - f["mouthWidth"]) / (cfg.mouth_narrow_full * w0r), 0.0, 1.0)
    wide_m = np.clip((f["mouthWidth"] - w0r) / (cfg.mouth_wide_full * w0r), 0.0, 1.0)
    cal["mouthWidth"] = dict(rest=w0r)
    tracked_wide = np.maximum(
        0.5 * (x["MouthDimpleLeft"] + x["MouthDimpleRight"]),
        0.5 * (x["MouthStretchLeft"] + x["MouthStretchRight"]),
    )
    x["LipWidth"] = 0.5 * (wide_m - narrow + tracked_wide - x["MouthPucker"])
    # A narrow, visibly open mouth still supplies rounding when the tracker is weak.
    # Do not erase the original pucker/dimple coefficients or force rounding to its maximum.
    x["MouthFunnel"] = 0.5 * (x["MouthFunnel"] + narrow * smoothstep(0.05, 0.25, gap))

    # Gaze is image-relative; mouth shift is face-local (right in the neutral head pose).
    gx = (f["irisXA"] + f["irisXB"]) / 2
    gy = (f["irisYA"] + f["irisYB"]) / 2
    cal["gaze_rest"] = [rest(gx), rest(gy)]
    x["GazeX"] = np.clip((gx - rest(gx)) / cfg.gaze_full[0], -1, 1)
    x["GazeY"] = np.clip((gy - rest(gy)) / cfg.gaze_full[1], -1, 1)
    x["MouthShift"] = np.clip((f["mouthShift"] - rest(f["mouthShift"])) / cfg.mouth_shift_full, -1, 1)

    for k in ("shoulderRoll", "shoulderYaw", "shoulderY"):
        c = float(np.nanmedian(f[k])) if np.isfinite(f[k]).any() else 0.0
        cal[f"{k}_center"] = c
        x[k] = f[k] - c
    x["face_ok"] = f["face_ok"]
    x["pose_ok"] = f["pose_ok"]
    return x, cal


def _to_unit(raw: np.ndarray, thresholds: tuple[float, float]) -> np.ndarray:
    """Raw zero-shot score -> 0..1 activation (smoothstep between the two thresholds)."""
    lo, hi = thresholds
    return smoothstep(lo, hi, raw)


# Calibrated inputs per intensity group: (group, name pattern, clip after scaling or None).
INTENSITY_GROUPS = [
    ("head", r"yaw|pitch|roll", None),
    ("body", r"shoulderRoll|shoulderYaw|shoulderY", None),
    ("gaze", r"GazeX|GazeY", (-1.0, 1.0)),
    ("brows", r"Brow\w*", (0.0, 1.0)),
    ("smile", r"MouthSmileLeft|MouthSmileRight", (0.0, 1.0)),
    ("mouth_open", r"JawOpen|LipOpen", (0.0, 1.0)),
    ("mouth_shape", r"LipWidth|MouthShift", (-1.0, 1.0)),
    ("mouth_shape", r"Mouth(Funnel|Pucker|Left|Right|(Press|Shrug|Roll|Dimple|Stretch|UpperUp|LowerDown|Frown)\w*)",
     (0.0, 1.0)),
    ("cheek_tongue", r"CheekPuff|TongueOut", (0.0, 1.0)),
]


def intensity_group(name: str) -> tuple[str, tuple[float, float] | None] | None:
    for group, pat, clip in INTENSITY_GROUPS:
        if re.fullmatch(pat, name):
            return group, clip
    return None


def scale(x: dict[str, np.ndarray], intensity: IntensityConfig) -> dict[str, np.ndarray]:
    """Calibrated, rest-centred inputs with each group multiplied by its intensity (a factor of 1
    leaves a channel untouched); bounded channels are clipped back to their range."""
    out = dict(x)
    for name, v in x.items():
        g = intensity_group(name)
        if g is None or getattr(intensity, g[0]) == 1.0:
            continue
        v = v * getattr(intensity, g[0])
        out[name] = np.clip(v, *g[1]) if g[1] else v
    return out
