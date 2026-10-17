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

A mapping file (`v2l.mapping`, docs/motion-editing.md section 5) has three layers: input curves on
tracking inputs, bridge parameters patched or added on top of the bridge preset, and entries that
patch, add or disable parameter mappings before the corrections; `invert` mirrors the output
range after them. Everything but apply()/apply_input_curves() is standard library.
"""

from __future__ import annotations

import dataclasses
import json
import math
import pathlib

from . import inputs as I
from .curves import Curve, problems as curve_problems

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
FORMAT = "v2l.mapping"
VERSION = 1
FIELDS = ("output", "input", "in", "out", "clamp_in", "clamp_out", "invert", "curve", "cutoff", "breathing",
          "enabled")


class MappingError(ValueError):
    """Invalid mapping overrides; problems lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


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
    invert: bool = False                      # mirror the output range after the probe corrections
    curve: dict | None = None                 # {"points", "smooth"} on normalized input -> output; None: linear
    cutoff: float | None = None               # low-pass cutoff in Hz; None: the parameter group's default
    enabled: bool = True
    source: str = "fallback"                  # vtube | fallback | override | added
    overridden: list[str] = dataclasses.field(default_factory=list)  # override fields that changed it
    corrections: list[str] = dataclasses.field(default_factory=list)  # probe corrections applied


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
                breathing=bool(s.get("UseBreathing")), source="vtube",
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


def _sign(x: float) -> float:
    return 1.0 if x > 0 else -1.0 if x < 0 else 0.0


def _probe_sign(probe: dict | None, pid: str, kind: str) -> float:
    if not probe or pid not in probe.get("params", {}):
        return 0.0
    p = probe["params"][pid]
    v = {"h": p.get("dx", 0.0), "v": -p.get("dy", 0.0), "r": p.get("rot", 0.0)}[kind]
    floor = probe.get("min_rot", 0.01) if kind == "r" else probe.get("min_motion", 0.5)
    return _sign(v) if abs(v) >= floor else 0.0


def resolve(entries: list[Entry], probe: dict | None) -> tuple[list[Entry], list[str]]:
    """Applies the body substitution (unless an override named the input) and the probe-based
    direction/side corrections. `invert` is applied later, by apply() (see applied_out)."""
    notes: list[str] = []
    fixed: list[Entry] = []
    for e in entries:
        e = dataclasses.replace(e, corrections=list(e.corrections), overridden=list(e.overridden))
        if (e.output in BODY_FROM_FACE and e.input == BODY_FROM_FACE[e.output][0]
                and "input" not in e.overridden):
            e.input = BODY_FROM_FACE[e.output][1]
            e.corrections.append("body")
        kind = DIRECTION.get(e.input)
        if kind:
            screen = _probe_sign(probe, e.output, kind)
            slope = _sign((e.out_hi - e.out_lo) / (e.in_hi - e.in_lo)) if e.in_hi != e.in_lo else 0.0
            if screen and slope and screen * slope < 0:
                e.out_lo, e.out_hi = e.out_hi, e.out_lo
                e.corrections.append("direction")
                notes.append(f"{e.output}: reversed so +{e.input} moves the drawing {'right' if kind == 'h' else 'up' if kind == 'v' else 'clockwise'}")
        side = SIDE.get(e.input)
        if side and probe and e.output in probe.get("params", {}):
            drawn = probe["params"][e.output].get("side")
            if drawn in ("left", "right") and drawn != side:
                notes.append(f"{e.output}: input {e.input} -> {MIRROR[e.input]} (parameter draws on image {drawn})")
                e.input = MIRROR[e.input]
                e.corrections.append("side")
        fixed.append(e)
    return fixed, notes


def applied_out(e: Entry) -> tuple[float, float]:
    """The output range actually used: out reversed when invert."""
    return (e.out_hi, e.out_lo) if e.invert else (e.out_lo, e.out_hi)


def apply(entries: list[Entry], signals: dict, n: int, fps: float, breath_period: float = 3.4) -> dict:
    import numpy as np

    t = np.arange(n) / fps
    params: dict[str, np.ndarray] = {}
    for e in entries:
        if not e.enabled:
            continue
        out_lo, out_hi = applied_out(e)
        if e.breathing:
            params[e.output] = out_lo + (out_hi - out_lo) * (0.5 + 0.5 * np.sin(2 * np.pi * t / breath_period))
            continue
        v = signals.get(e.input)
        if v is None or e.in_hi == e.in_lo:
            continue
        if e.clamp_in:
            v = np.clip(v, min(e.in_lo, e.in_hi), max(e.in_lo, e.in_hi))
        if e.curve is None:
            y = out_lo + (v - e.in_lo) * (out_hi - out_lo) / (e.in_hi - e.in_lo)
        else:
            y = out_lo + Curve.from_json(e.curve)((v - e.in_lo) / (e.in_hi - e.in_lo)) * (out_hi - out_lo)
        if e.clamp_out:
            y = np.clip(y, min(out_lo, out_hi), max(out_lo, out_hi))
        # When several settings target one parameter the last one is kept.
        params[e.output] = y
    return params


def apply_input_curves(data: dict | None, signals: dict) -> dict:
    """Tracking inputs with the mapping's input curves applied (x and y normalized over each
    input's nominal range); inputs without a curve are returned unchanged."""
    curves = (data or {}).get("input_curves") or {}
    if not curves:
        return signals
    ranges = I.tracking_ranges()
    out = dict(signals)
    for name, c in curves.items():
        if name in out:
            lo, hi = ranges[name]
            out[name] = lo + Curve.from_json(c)((out[name] - lo) / (hi - lo)) * (hi - lo)
    return out


# ------------------------------------------------------------------------------------- overrides

def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pair(x) -> bool:
    return isinstance(x, (list, tuple)) and len(x) == 2 and _num(x[0]) and _num(x[1])


def load_overrides(path: str | pathlib.Path) -> dict:
    p = pathlib.Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"mapping file not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise MappingError([f"{p.name} is not valid JSON: {e}"]) from None


def check(data, ranges: dict | None = None, base: list[Entry] | None = None, physics: set | None = None,
          bridge: str = "default") -> tuple[list[str], list[str]]:
    """(errors, warnings) of a v2l.mapping object: input curves, the bridge section on top of the
    bridge preset, and entries. ranges (model parameters) and base (the model's base entries)
    enable the checks that need the model."""
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(data, dict):
        return ['a mapping file is a JSON object {"format": "v2l.mapping", "version": 1, ...}'], []
    if data.get("format", FORMAT) != FORMAT:
        errs.append(f"format must be {FORMAT!r}")
    if data.get("version", VERSION) != VERSION:
        errs.append(f"version must be {VERSION}")
    unknown_top = set(data) - {"format", "version", "description", "input_curves", "bridge", "entries"}
    if unknown_top:
        errs.append(f"unknown top-level field {', '.join(sorted(unknown_top))}")

    tracking = I.tracking_ranges()
    curves = data.get("input_curves", {})
    if not isinstance(curves, dict):
        errs.append('input_curves must be an object {"<input name>": {"points": [...], "smooth": true}}')
    else:
        for name, c in curves.items():
            if name not in tracking:
                errs.append(f"input_curves: {name!r} is not a tracking input (v2l mapping inputs lists them)")
            errs += curve_problems(c, f"input_curves.{name}")

    from . import bridge as B

    overrides = data.get("bridge")
    try:
        _, preset = B.read_preset(bridge)
    except (FileNotFoundError, B.BridgeError) as e:
        return errs + [str(e)], warns
    if overrides is not None:
        e1, w1 = B.override_problems(overrides, {p.get("name") for p in preset})
        errs += e1
        warns += w1
        if not e1:
            eff = B.effective(preset, overrides)
            known = B.known_names(p["name"] for p in eff if B._active(p))
            changed = [p for p in eff if p["source"] in ("override", "added")]
            errs += [e for e in B.problems(eff) if any(f"bridge {p['name']!r}" in e for p in changed)
                     or "cycle" in e]
            for row in B.describe(changed, known):
                if row["unknown"]:
                    warns.append(f"bridge {row['name']!r} reads {', '.join(row['unknown'])}, which no tracking "
                                 "input or bridge output provides (read as 0)")
    bad_bridge = any(e.startswith("bridge") for e in errs)
    known_inputs = I.by_name(bridge, None if bad_bridge else overrides)

    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return errs + ["entries must be a list"], warns
    base_ids = {e.output for e in base} if base is not None else None
    seen = set()
    for n, o in enumerate(entries):
        where = f"entries[{n}]"
        if not isinstance(o, dict):
            errs.append(f"{where} must be an object")
            continue
        pid = o.get("output")
        if not isinstance(pid, str) or not pid:
            errs.append(f"{where}: output (a Live2D parameter id) is required")
            continue
        where = f"{where} ({pid})"
        if pid in seen:
            errs.append(f"{where}: {pid} appears twice; use one entry per parameter")
        seen.add(pid)
        unknown = set(o) - set(FIELDS)
        if unknown:
            errs.append(f"{where}: unknown field {', '.join(sorted(unknown))}; fields are {', '.join(FIELDS)}")
        if "input" in o and not (isinstance(o["input"], str) and o["input"]):
            errs.append(f"{where}: input must be an input name (v2l mapping inputs lists them)")
        elif "input" in o and o["input"] not in known_inputs:
            errs.append(f"{where}: unknown input {o['input']!r} (v2l mapping inputs lists them)")
        for k in ("in", "out"):
            if k in o and not _pair(o[k]):
                errs.append(f"{where}: {k} must be [low, high] numbers")
        if _pair(o.get("in")) and o["in"][0] == o["in"][1]:
            errs.append(f"{where}: in range is empty ({o['in'][0]} to {o['in'][1]})")
        if _pair(o.get("out")) and o["out"][0] == o["out"][1]:
            warns.append(f"{where}: out range is a single value; the parameter stays constant")
        for k in ("clamp_in", "clamp_out", "invert", "breathing", "enabled"):
            if k in o and not isinstance(o[k], bool):
                errs.append(f"{where}: {k} must be true or false")
        if o.get("curve") is not None:
            errs += curve_problems(o["curve"], f"{where}.curve")
        if o.get("cutoff") is not None and not (_num(o["cutoff"]) and o["cutoff"] > 0):
            errs.append(f"{where}: cutoff must be null (the group default) or a frequency in Hz above 0")
        if ranges is not None:
            if pid not in ranges:
                errs.append(f"{where}: unknown parameter {pid!r}; the model has no such parameter")
                continue
            r = ranges[pid]
            if _pair(o.get("out")) and (min(o["out"]) < r["min"] - 1e-6 or max(o["out"]) > r["max"] + 1e-6):
                warns.append(f"{where}: out {o['out']} reaches beyond the parameter range {r['min']}..{r['max']} "
                             "(values are clamped to it)")
        if physics and pid in physics and o.get("enabled", True):
            warns.append(f"{where}: {pid} is driven by the model's physics; v2l does not write it")
        if base_ids is not None and pid not in base_ids:
            if o.get("enabled", True) is False:
                warns.append(f"{where}: {pid} has no base mapping, so there is nothing to disable")
            elif not o.get("breathing") and "input" not in o:
                errs.append(f"{where}: {pid} has no base mapping, so the entry must name an input (or set breathing)")
    return errs, warns


def _patch(e: Entry, o: dict) -> None:
    changed = []
    if "input" in o:
        e.input = o["input"]
        changed.append("input")
    if "in" in o:
        e.in_lo, e.in_hi = float(o["in"][0]), float(o["in"][1])
        changed.append("in")
    if "out" in o:
        e.out_lo, e.out_hi = float(o["out"][0]), float(o["out"][1])
        changed.append("out")
    for k in ("clamp_in", "clamp_out", "invert", "breathing", "enabled", "curve"):
        if k in o:
            setattr(e, k, o[k])
            changed.append(k)
    if "cutoff" in o:
        e.cutoff = float(o["cutoff"]) if o["cutoff"] is not None else None
        changed.append("cutoff")
    e.overridden = changed


def override(base: list[Entry], data: dict | None, ranges: dict, known_inputs: dict | None = None) -> list[Entry]:
    """Base entries patched, added or disabled by a (checked) v2l.mapping object."""
    if not data or not data.get("entries"):
        return [dataclasses.replace(e) for e in base]
    by_out = {o["output"]: o for o in data["entries"]}
    out = []
    for e in base:
        e = dataclasses.replace(e)
        o = by_out.get(e.output)
        if o is not None:
            _patch(e, o)
            e.source = "override"
        out.append(e)
    base_ids = {e.output for e in base}
    for o in data["entries"]:
        if o["output"] in base_ids or o.get("enabled", True) is False:
            continue
        r = ranges.get(o["output"], {"min": 0.0, "max": 1.0})
        nominal = (known_inputs or {}).get(o.get("input", ""), {}).get("range", [0.0, 1.0])
        e = Entry(o.get("input", ""), o["output"], float(nominal[0]), float(nominal[1]), float(r["min"]),
                  float(r["max"]), clamp_in=True, clamp_out=True, source="added")
        _patch(e, o)
        out.append(e)
    return out


def effective(model, data: dict | None = None, bridge: str = "default") -> tuple[list[Entry], list[str], list[str]]:
    """(entries, probe notes, warnings): the model's base mapping with the overrides applied and
    the probe corrections made. Disabled entries are included with enabled=False. Raises
    MappingError when the mapping file is invalid for this model."""
    base = entries_for(model)
    warns: list[str] = []
    if data:
        errs, warns = check(data, model.ranges, base, model.physics_outputs, bridge)
        if errs:
            raise MappingError(errs)
    known = I.by_name(bridge, (data or {}).get("bridge"))
    entries, notes = resolve(override(base, data, model.ranges, known), model.probe)
    return entries, notes, warns


def entry_json(e: Entry) -> dict:
    """An effective entry in override terms (in/out before invert) plus read-only details."""
    return {"output": e.output, "input": e.input, "in": [e.in_lo, e.in_hi], "out": [e.out_lo, e.out_hi],
            "clamp_in": e.clamp_in, "clamp_out": e.clamp_out, "invert": e.invert, "curve": e.curve,
            "cutoff": e.cutoff, "breathing": e.breathing, "enabled": e.enabled, "source": e.source,
            "applied_out": list(applied_out(e)), "overridden": list(e.overridden),
            "corrections": list(e.corrections)}


def display_names(model) -> dict[str, str]:
    """Parameter display names from the model's cdi3.json (empty when there is none)."""
    ref = model.settings.get("FileReferences", {}).get("DisplayInfo")
    if not ref or not (model.dir / ref).is_file():
        return {}
    try:
        cdi = json.loads((model.dir / ref).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {p.get("Id"): p.get("Name") for p in cdi.get("Parameters", []) if p.get("Id")}


def bridge_show(data: dict | None = None, bridge: str = "default") -> dict:
    """The effective bridge (`v2l bridge show --json`): preset name and parameters with source."""
    from . import bridge as B

    name, preset = B.read_preset(bridge)
    eff = B.effective(preset, (data or {}).get("bridge"))
    known = B.known_names(p["name"] for p in eff if B._active(p))
    return {"bridge": name, "params": B.describe(eff, known)}


def show(model, data: dict | None = None, bridge: str = "default") -> dict:
    """The effective mapping and the model's parameters (the `v2l mapping show --json` object)."""
    from .config import Config
    from .pipeline import cutoff as group_cutoff, filter_group

    entries, notes, warns = effective(model, data, bridge)
    names = display_names(model)
    mapped = {e.output for e in entries if e.enabled}
    params = [{"id": pid, "name": names.get(pid), "min": r["min"], "max": r["max"], "default": r["def"],
               "mapped": pid in mapped, "physics": pid in model.physics_outputs} for pid, r in model.ranges.items()]
    cfg = Config()
    rows = []
    for e in entries:
        group = filter_group(e.output)
        rows.append({**entry_json(e), "filter_group": group,
                     "default_cutoff": group_cutoff(cfg.filtering, group, cfg.output.fps) if group else None})
    br = bridge_show(data, bridge)
    return {"model": model.name, "model_dir": str(model.dir), "base": "vtube" if model.vtube else "fallback",
            "bridge": br["bridge"], "input_curves": (data or {}).get("input_curves") or {},
            "bridge_params": br["params"], "entries": rows, "notes": notes, "warnings": warns,
            "parameters": params}
