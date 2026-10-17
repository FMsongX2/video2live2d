"""Names a mapping may use as its input (standard library only).

Two kinds feed the mapping (pipeline.solve passes both; a bridge output wins over a tracking
input of the same name):
  bridge    outputs of the bridge preset (VBridger-style names such as FaceAngleX, MouthOpen)
  tracking  calibrated, rest-centred tracking inputs (ARKit-style blendshape names plus V2L's own
            geometric measurements), the same values the bridge formulas read
"""

from __future__ import annotations

# Meanings of the outputs of the built-in bridge preset (image space: + is image right / up /
# clockwise). Outputs of other presets are described by their formula.
BRIDGE_MEANING = {
    "FaceAngleX": "head turn, degrees (+ = toward the image right)",
    "FaceAngleY": "head tilt up/down, degrees (+ = up)",
    "FaceAngleZ": "head roll, degrees (+ = clockwise on screen)",
    "BodyAngleX": "upper body turn from head and shoulders, degrees (+ = toward the image right)",
    "BodyAngleY": "upper body lean from head pitch and shoulder height, degrees (+ = up)",
    "BodyAngleZ": "upper body roll from head and shoulder line, degrees (+ = clockwise)",
    "EyeOpenLeft": "performer's left eye openness (0 closed, 0.5 rest, 1 wide)",
    "EyeOpenRight": "performer's right eye openness (0 closed, 0.5 rest, 1 wide)",
    "EyeSquintL": "left eye squint",
    "EyeSquintR": "right eye squint",
    "EyeRightX": "gaze left/right (+ = image right)",
    "EyeRightY": "gaze up/down (+ = up)",
    "EyeLeftX": "gaze left/right (+ = image right)",
    "EyeLeftY": "gaze up/down (+ = up)",
    "Brows": "both brows (0 down, 0.5 rest, 1 raised)",
    "BrowLeftY": "performer's left brow (0 down, 0.5 rest, 1 raised)",
    "BrowRightY": "performer's right brow (0 down, 0.5 rest, 1 raised)",
    "BrowInnerUp": "inner brows raised",
    "MouthOpen": "lip opening",
    "MouthSmile": "smile (0 frown, 0.5 rest, 1 smile)",
    "MouthX": "mouth shifted sideways (+ = image right)",
    "JawOpen": "jaw opening",
    "MouthFunnel": "lips funnelled (as in 'oo')",
    "MouthPucker": "mouth width (- = puckered/narrow, + = wide)",
    "MouthPressLipOpen": "lips curled open (+) or pressed together (-)",
    "MouthShrug": "mouth shrug (chin raised, lips pushed up)",
    "CheekPuff": "cheeks puffed (zero-shot detector)",
    "TongueOut": "tongue out (zero-shot detector)",
    "VoiceVolumePlusMouthOpen": "lip opening (VBridger voice input, from video)",
    "VoiceFrequencyPlusMouthSmile": "smile (VBridger voice input, from video)",
}

# Calibrated tracking inputs: (name, low, high, meaning).
_ARKIT = [
    ("BrowDownLeft", "performer's left brow lowered"), ("BrowDownRight", "performer's right brow lowered"),
    ("BrowInnerUp", "inner brows raised"), ("BrowOuterUpLeft", "performer's left outer brow raised"),
    ("BrowOuterUpRight", "performer's right outer brow raised"), ("CheekPuff", "cheeks puffed"),
    ("CheekSquintLeft", "left cheek raised"), ("CheekSquintRight", "right cheek raised"),
    ("EyeBlinkLeft", "performer's left eye closed"), ("EyeBlinkRight", "performer's right eye closed"),
    ("EyeLookDownLeft", "left eye looks down"), ("EyeLookDownRight", "right eye looks down"),
    ("EyeLookInLeft", "left eye looks toward the nose"), ("EyeLookInRight", "right eye looks toward the nose"),
    ("EyeLookOutLeft", "left eye looks outward"), ("EyeLookOutRight", "right eye looks outward"),
    ("EyeLookUpLeft", "left eye looks up"), ("EyeLookUpRight", "right eye looks up"),
    ("EyeSquintLeft", "left eye squint"), ("EyeSquintRight", "right eye squint"),
    ("EyeWideLeft", "left eye wide open"), ("EyeWideRight", "right eye wide open"),
    ("JawForward", "jaw pushed forward"), ("JawLeft", "jaw to the performer's left"),
    ("JawOpen", "jaw opening"), ("JawRight", "jaw to the performer's right"),
    ("MouthClose", "lips closed while the jaw is open"), ("MouthDimpleLeft", "left mouth corner dimpled"),
    ("MouthDimpleRight", "right mouth corner dimpled"), ("MouthFrownLeft", "left mouth corner down"),
    ("MouthFrownRight", "right mouth corner down"), ("MouthFunnel", "lips funnelled"),
    ("MouthLeft", "mouth to the performer's left"), ("MouthLowerDownLeft", "left lower lip down"),
    ("MouthLowerDownRight", "right lower lip down"), ("MouthPressLeft", "left lips pressed"),
    ("MouthPressRight", "right lips pressed"), ("MouthPucker", "lips puckered"),
    ("MouthRight", "mouth to the performer's right"), ("MouthRollLower", "lower lip rolled in"),
    ("MouthRollUpper", "upper lip rolled in"), ("MouthShrugLower", "lower lip pushed up"),
    ("MouthShrugUpper", "upper lip pushed up"), ("MouthSmileLeft", "left mouth corner up"),
    ("MouthSmileRight", "right mouth corner up"), ("MouthStretchLeft", "left mouth corner stretched"),
    ("MouthStretchRight", "right mouth corner stretched"), ("MouthUpperUpLeft", "left upper lip raised"),
    ("MouthUpperUpRight", "right upper lip raised"), ("NoseSneerLeft", "left nostril raised"),
    ("NoseSneerRight", "right nostril raised"),
]
TRACKING = [
    ("yaw", -45.0, 45.0, "head turn, degrees from the clip's median pose (+ = toward the image right)"),
    ("pitch", -45.0, 45.0, "head tilt, degrees from the clip's median pose (+ = up)"),
    ("roll", -45.0, 45.0, "head roll, degrees from the clip's median pose (+ = clockwise on screen)"),
    ("GazeX", -1.0, 1.0, "gaze left/right (+ = image right)"),
    ("GazeY", -1.0, 1.0, "gaze up/down (+ = up)"),
    ("LipOpen", 0.0, 1.0, "measured lip aperture"),
    ("LipWidth", -1.0, 1.0, "mouth width (- = narrow, + = wide)"),
    ("MouthShift", -1.0, 1.0, "mouth shifted sideways in the face (+ = image right at rest)"),
    ("shoulderRoll", -30.0, 30.0, "shoulder line angle, degrees from the median (+ = clockwise)"),
    ("shoulderYaw", -30.0, 30.0, "torso turn from the shoulders, degrees from the median (+ = toward the image right)"),
    ("shoulderY", -0.5, 0.5, "shoulder height change in face heights (+ = lower on screen)"),
    ("TongueOut", 0.0, 1.0, "tongue out (zero-shot detector; 0 when that stage is off)"),
] + [(name, 0.0, 1.0, meaning + " (ARKit-style, 0 at rest)") for name, meaning in _ARKIT]


def bridge_outputs(bridge: str = "default", overrides: list | None = None) -> tuple[str, list[dict]]:
    """(preset name, active effective bridge parameters) for a preset and a mapping's bridge section."""
    from . import bridge as B

    name, params = B.read_preset(bridge)
    eff = B.effective(params, overrides) if overrides else params
    return name, [p for p in eff if B._active(p) and p.get("name")]


def listing(bridge: str = "default", overrides: list | None = None) -> dict:
    """{"bridge": name, "inputs": [{"name", "kind", "range", "default", "meaning", ("shadowed")}]}:
    the bridge outputs (with a mapping's bridge section applied), then every tracking input; a
    tracking input is "shadowed" when a bridge output of the same name wins for mapping entries."""
    name, entries = bridge_outputs(bridge, overrides)
    out, seen = [], set()
    for e in entries:
        rng = [float(e.get("min", 0.0)), float(e.get("max", 1.0))]
        meaning = BRIDGE_MEANING.get(e["name"]) if e.get("source", "preset") == "preset" else None
        if meaning is None:
            meaning = (f"delayed {e['delayBuffer'].get('refParam')}" if e.get("delayBuffer")
                       else f"bridge formula: {e.get('func', '')}")
        out.append({"name": e["name"], "kind": "bridge", "range": rng, "default": float(e.get("defaultValue", 0.0)),
                    "meaning": meaning})
        seen.add(e["name"])
    for n, lo, hi, meaning in TRACKING:
        out.append({"name": n, "kind": "tracking", "range": [lo, hi], "default": 0.0, "meaning": meaning,
                    "shadowed": n in seen})
    return {"bridge": name, "inputs": out}


def by_name(bridge: str = "default", overrides: list | None = None) -> dict[str, dict]:
    """Inputs a mapping entry can name; a bridge output wins over a tracking input of its name."""
    found: dict[str, dict] = {}
    for i in listing(bridge, overrides)["inputs"]:
        found.setdefault(i["name"], i)
    return found


def tracking_ranges() -> dict[str, tuple[float, float]]:
    """Nominal range of every tracking input (input curves are normalized over it)."""
    return {n: (lo, hi) for n, lo, hi, _ in TRACKING}
