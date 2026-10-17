"""Bridge outputs -> Live2D parameters.

The model's own VTube Studio mapping (*.vtube.json ParameterSettings) is used when present, so
the rigger's ranges and choices apply exactly as in VTube Studio. Without it a standard VBridger
mapping onto Cubism standard parameter ids is used.

Two corrections make the result independent of tracker and rig conventions:
  * direction: head angles use image-space directions. Local facial controls such as MouthX
    use the corresponding direction at a neutral head pose. The renderer measures each
    parameter in that pose; if input slope x probe direction disagrees, the mapping is reflected.
  * side: per-eye/brow inputs are tied to an image side (ARKit Left = image right); if the probe
    shows the parameter drawing on the other side, the mirrored input is used.
Body parameters fed by FaceAngle in VTube Studio take the bridge's BodyAngle (head + shoulders).
"""

from __future__ import annotations

import dataclasses

import numpy as np

# Image-space meaning of bridge outputs: h = + is image right, v = + is up, r = + is clockwise.
DIRECTION = {
    "FaceAngleX": "h", "BodyAngleX": "h", "EyeRightX": "h", "EyeLeftX": "h", "MouthX": "h",
    "FaceAngleY": "v", "BodyAngleY": "v", "EyeRightY": "v", "EyeLeftY": "v",
    "FaceAngleZ": "r", "BodyAngleZ": "r",
}
# Image side of side-specific inputs (performer's left = image right for un-mirrored footage).
SIDE_PAIRS = [
    ("EyeOpenLeft", "EyeOpenRight"), ("EyeSquintL", "EyeSquintR"), ("EyeSquintLeft", "EyeSquintRight"),
    ("EyeBlinkLeft", "EyeBlinkRight"), ("EyeWideLeft", "EyeWideRight"), ("BrowLeftY", "BrowRightY"),
    ("BrowOuterUpLeft", "BrowOuterUpRight"), ("BrowDownLeft", "BrowDownRight"),
    ("MouthSmileLeft", "MouthSmileRight"),
]
SIDE = {a: "right" for a, _ in SIDE_PAIRS} | {b: "left" for _, b in SIDE_PAIRS}
MIRROR = {a: b for a, b in SIDE_PAIRS} | {b: a for a, b in SIDE_PAIRS}
BODY_FROM_FACE = {"ParamBodyAngleX": ("FaceAngleX", "BodyAngleX"), "ParamBodyAngleY": ("FaceAngleY", "BodyAngleY"),
                  "ParamBodyAngleZ": ("FaceAngleZ", "BodyAngleZ")}


@dataclasses.dataclass
class Entry:
    input: str
    output: str
    in_lo: float
    in_hi: float
    out_lo: float
    out_hi: float
    clamp_in: bool = False
    clamp_out: bool = False
    breathing: bool = False


# Fallback for models without a VTube Studio file: standard VBridger inputs onto Cubism ids.
# Output ranges come from the model: "range" = the parameter's min..max, "open" = min..default
# (eye-open parameters rest at their default; VBridger's EyeOpen rests at 0.5).
DEFAULT_MAP = [
    ("FaceAngleX", "ParamAngleX", -30, 30, "range"), ("FaceAngleY", "ParamAngleY", -30, 30, "range"),
    ("FaceAngleZ", "ParamAngleZ", -30, 30, "range"),
    ("BodyAngleX", "ParamBodyAngleX", -20, 20, "range"), ("BodyAngleY", "ParamBodyAngleY", -20, 20, "range"),
    ("BodyAngleZ", "ParamBodyAngleZ", -20, 20, "range"),
    ("EyeOpenLeft", "ParamEyeLOpen", 0, 0.5, "open"), ("EyeOpenRight", "ParamEyeROpen", 0, 0.5, "open"),
    ("MouthSmile", "ParamEyeLSmile", 0.5, 1, "range"), ("MouthSmile", "ParamEyeRSmile", 0.5, 1, "range"),
    ("EyeRightX", "ParamEyeBallX", -1, 1, "range"), ("EyeRightY", "ParamEyeBallY", -1, 1, "range"),
    ("BrowLeftY", "ParamBrowLY", 0, 1, "range"), ("BrowRightY", "ParamBrowRY", 0, 1, "range"),
    ("MouthSmile", "ParamMouthForm", 0, 1, "range"), ("MouthOpen", "ParamMouthOpenY", 0, 1, "range"),
    ("MouthX", "ParamMouthX", -1, 1, "range"), ("JawOpen", "ParamJawOpen", 0, 1, "range"),
    ("MouthFunnel", "ParamMouthFunnel", 0, 1, "range"), ("MouthPucker", "ParamMouthPuckerWiden", -1, 1, "range"),
    ("MouthPressLipOpen", "ParamMouthPressLipOpen", -1.3, 1.3, "range"), ("MouthShrug", "ParamMouthShrug", 0, 1, "range"),
    ("CheekPuff", "ParamCheekPuff", 0, 1, "range"), ("TongueOut", "ParamTongueOut", 0, 1, "range"),
]


def entries_for(model) -> list[Entry]:
    out: list[Entry] = []
    if model.vtube:
        for s in model.vtube.get("ParameterSettings", []):
            pid = s.get("OutputLive2D", "")
            if pid not in model.ranges:
                continue
            out.append(Entry(
                input=s.get("Input", ""), output=pid,
                in_lo=float(s.get("InputRangeLower", 0)), in_hi=float(s.get("InputRangeUpper", 1)),
                out_lo=float(s.get("OutputRangeLower", 0)), out_hi=float(s.get("OutputRangeUpper", 1)),
                clamp_in=bool(s.get("ClampInput")), clamp_out=bool(s.get("ClampOutput")),
                breathing=bool(s.get("UseBreathing")),
            ))
        return out
    for inp, pid, lo, hi, kind in DEFAULT_MAP:
        if pid in model.ranges:
            r = model.ranges[pid]
            top = r["def"] if kind == "open" else r["max"]
            out.append(Entry(inp, pid, lo, hi, r["min"], top, clamp_in=kind == "range", clamp_out=kind == "range"))
    if "ParamBreath" in model.ranges:
        out.append(Entry("", "ParamBreath", 0, 1, 0, 1, breathing=True))
    return out


def _probe_sign(probe: dict | None, pid: str, kind: str) -> float:
    if not probe or pid not in probe.get("params", {}):
        return 0.0
    p = probe["params"][pid]
    v = {"h": p.get("dx", 0.0), "v": -p.get("dy", 0.0), "r": p.get("rot", 0.0)}[kind]
    floor = probe.get("min_rot", 0.01) if kind == "r" else probe.get("min_motion", 0.5)
    return float(np.sign(v)) if abs(v) >= floor else 0.0


def resolve(entries: list[Entry], probe: dict | None) -> tuple[list[Entry], list[str]]:
    """Applies the body substitution and the probe-based direction/side corrections."""
    notes: list[str] = []
    fixed: list[Entry] = []
    for e in entries:
        e = dataclasses.replace(e)
        if e.output in BODY_FROM_FACE and e.input == BODY_FROM_FACE[e.output][0]:
            e.input = BODY_FROM_FACE[e.output][1]
        kind = DIRECTION.get(e.input)
        if kind:
            screen = _probe_sign(probe, e.output, kind)
            slope = np.sign((e.out_hi - e.out_lo) / (e.in_hi - e.in_lo)) if e.in_hi != e.in_lo else 0.0
            if screen and slope and screen * slope < 0:
                e.out_lo, e.out_hi = e.out_hi, e.out_lo
                notes.append(f"{e.output}: reversed so +{e.input} moves the drawing {'right' if kind == 'h' else 'up' if kind == 'v' else 'clockwise'}")
        side = SIDE.get(e.input)
        if side and probe and e.output in probe.get("params", {}):
            drawn = probe["params"][e.output].get("side")
            if drawn in ("left", "right") and drawn != side:
                notes.append(f"{e.output}: input {e.input} -> {MIRROR[e.input]} (parameter draws on image {drawn})")
                e.input = MIRROR[e.input]
        fixed.append(e)
    return fixed, notes


def apply(entries: list[Entry], signals: dict[str, np.ndarray], n: int, fps: float,
          breath_period: float = 3.4) -> dict[str, np.ndarray]:
    t = np.arange(n) / fps
    params: dict[str, np.ndarray] = {}
    for e in entries:
        if e.breathing:
            params[e.output] = e.out_lo + (e.out_hi - e.out_lo) * (0.5 + 0.5 * np.sin(2 * np.pi * t / breath_period))
            continue
        v = signals.get(e.input)
        if v is None or e.in_hi == e.in_lo:
            continue
        if e.clamp_in:
            v = np.clip(v, min(e.in_lo, e.in_hi), max(e.in_lo, e.in_hi))
        y = e.out_lo + (v - e.in_lo) * (e.out_hi - e.out_lo) / (e.in_hi - e.in_lo)
        if e.clamp_out:
            y = np.clip(y, min(e.out_lo, e.out_hi), max(e.out_lo, e.out_hi))
        # When several settings target one parameter the last one is kept.
        params[e.output] = y
    return params
