"""Deterministic synthetic tracking signals and a model stub for pipeline tests (no tracker, no renderer)."""

from __future__ import annotations

import pathlib

import numpy as np

from test_features import face_sequence
from v2l.model import Model
from v2l.signals import Signals

ARKIT = ["browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight", "cheekPuff",
         "cheekSquintLeft", "cheekSquintRight", "eyeBlinkLeft", "eyeBlinkRight", "eyeLookDownLeft",
         "eyeLookDownRight", "eyeLookInLeft", "eyeLookInRight", "eyeLookOutLeft", "eyeLookOutRight",
         "eyeLookUpLeft", "eyeLookUpRight", "eyeSquintLeft", "eyeSquintRight", "eyeWideLeft", "eyeWideRight",
         "jawForward", "jawLeft", "jawOpen", "jawRight", "mouthClose", "mouthDimpleLeft", "mouthDimpleRight",
         "mouthFrownLeft", "mouthFrownRight", "mouthFunnel", "mouthLeft", "mouthLowerDownLeft",
         "mouthLowerDownRight", "mouthPressLeft", "mouthPressRight", "mouthPucker", "mouthRight",
         "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper", "mouthSmileLeft",
         "mouthSmileRight", "mouthStretchLeft", "mouthStretchRight", "mouthUpperUpLeft", "mouthUpperUpRight",
         "noseSneerLeft", "noseSneerRight"]

PARAMS = {
    "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0), "ParamAngleZ": (-30, 30, 0),
    "ParamBodyAngleX": (-10, 10, 0), "ParamBodyAngleY": (-10, 10, 0), "ParamBodyAngleZ": (-10, 10, 0),
    "ParamBreath": (0, 1, 0), "ParamBrowLY": (-1, 1, 0), "ParamBrowRY": (-1, 1, 0),
    "ParamCheekPuff": (0, 1, 0), "ParamEyeBallX": (-1, 1, 0), "ParamEyeBallY": (-1, 1, 0),
    "ParamEyeLOpen": (0, 1.2, 1), "ParamEyeROpen": (0, 1.2, 1), "ParamEyeLSmile": (0, 1, 0),
    "ParamEyeRSmile": (0, 1, 0), "ParamJawOpen": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
    "ParamMouthFunnel": (0, 1, 0), "ParamMouthOpenY": (0, 1, 0), "ParamMouthPressLipOpen": (-1, 1, 0),
    "ParamMouthPuckerWiden": (-1, 1, 0), "ParamMouthShrug": (0, 1, 0), "ParamMouthX": (-1, 1, 0),
    "ParamTongueOut": (0, 1, 0), "ParamCheek": (0, 1, 0),
}


def signals(n: int = 240, seed: int = 3) -> Signals:
    """A talking, blinking, nodding face at 30 fps with a lost-face gap."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / 30
    angles = np.stack([4 * np.sin(2 * np.pi * 0.3 * t), 6 * np.sin(2 * np.pi * 0.2 * t + 1),
                       2 * np.sin(2 * np.pi * 0.5 * t)], 1)
    jaw = 30 * np.clip(np.sin(2 * np.pi * 2.1 * t), 0, None) * (t > 2)
    s = face_sequence(angles, jaw_drop=jaw)
    bs = np.clip(rng.normal(0.05, 0.03, (n, len(ARKIT))), 0, 1)
    k = {name: i for i, name in enumerate(ARKIT)}
    bs[:, k["jawOpen"]] = jaw / 40
    for side in ("Left", "Right"):
        bs[:, k["eyeBlink" + side]] = np.where((t % 3.0) < 0.15, 0.9, 0.05)
        bs[:, k["mouthSmile" + side]] = 0.3 * (t > 5)
    s.bs_names = np.array(ARKIT)
    s.bs = bs
    gap = (t > 6.0) & (t < 6.8)
    s.face_ok = ~gap
    for a in ("bs", "head_mat", "face_lm"):
        v = getattr(s, a)
        v[gap] = np.nan
    return s


def model(tmp: pathlib.Path) -> Model:
    sf = tmp / "Stub.model3.json"
    sf.write_text("{}")
    ranges = {k: {"min": float(a), "max": float(b), "def": float(d)} for k, (a, b, d) in PARAMS.items()}
    return Model(dir=tmp, settings_file=sf, settings={}, vtube=None, physics_outputs=set(), ranges=ranges, probe=None)
