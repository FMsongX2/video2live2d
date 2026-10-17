"""End-to-end: video + model folder -> motion3.json (+ preview video and QA report)."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import numpy as np

from . import mapping, motion3, temporal
from .assets import CACHE
from .bridge import Bridge
from .calibrate import calibrate, scale
from .config import Config, to_dict
from .features import features
from .model import Model, load
from .signals import Signals, extract
from .style import apply_cute

# Low-pass group (a filtering.* setting) by parameter id pattern; first match wins.
# Breathing is never filtered.
FILTER_GROUPS = [
    (r"Breath", None),
    (r"EyeSparkle", "sparkle"),
    (r"Cheek$", "blush"),
    (r"^ParamBodyAngle", "body"),
    (r"^ParamAngle", "head"),
    (r"EyeBall", "eyeball"),
    (r"Brow", "brows"),
    (r"Eye.?Open", "eye_open"),
    (r"Smile|Squint", "smile"),
    (r"Mouth|Jaw|Tongue|Cheekpuff|CheekPuff", "mouth"),
    (r".", "other"),
]

STAGES = ("probe", "tracking", "expressions", "solving", "writing", "preview")


def text_events(log=print):
    """Default event sink: tracking progress as the familiar '  300/383 frames' log lines."""
    def on(e: dict) -> None:
        if e["event"] == "progress" and e["stage"] == "tracking" and e["done"] % 300 == 0:
            log(f"  {e['done']}/{e['total']} frames")
    return on


def _progress(event, stage: str):
    return lambda done, total: event({"event": "progress", "stage": stage, "done": int(done), "total": int(total)})


def _file_digest(path: pathlib.Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def model_digest(model: Model) -> str:
    h = hashlib.sha1()
    for p in sorted(model.dir.iterdir()):
        if p.suffix in (".moc3", ".json") and not p.name.endswith(".motion3.json"):
            h.update(p.name.encode())
            h.update(_file_digest(p).encode())
    return h.hexdigest()[:16]


def prepare_model(model_dir: str | pathlib.Path, log=print) -> Model:
    """Loads the model and its parameter ranges + screen probe (cached per model content)."""
    from .render import probe_model, read_ranges

    model = load(model_dir)
    cache = CACHE / "models" / f"{model.name}-{model_digest(model)}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        model.ranges, model.probe = data["ranges"], data["probe"]
        return model
    log(f"probing model {model.name} (first use)")
    model.ranges = read_ranges(model)
    model.probe = probe_model(model)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"ranges": model.ranges, "probe": model.probe}, indent=1))
    return model


def _signals_key(video: pathlib.Path, cfg: Config) -> str:
    i = cfg.input
    return f"{_file_digest(video)}-{i.analysis_width}-{i.start}-{i.end}-{int(i.flip)}"


def get_signals(video: pathlib.Path, cfg: Config, log=print, progress=None) -> Signals:
    """Tracker output for cfg.input (cached); progress(done, total) per tracked frame."""
    cache = CACHE / "signals" / f"{_signals_key(video, cfg)}.npz"
    if cache.exists():
        return Signals.load(cache)
    log(f"tracking {video.name}")
    i = cfg.input
    s = extract(video, width=i.analysis_width, start=i.start, end=i.end, flip=i.flip, progress=progress)
    cache.parent.mkdir(parents=True, exist_ok=True)
    s.save(cache)
    return s


def get_rare(video: pathlib.Path, s: Signals, cfg: Config, log=print, progress=None) -> dict[str, np.ndarray] | None:
    """Zero-shot cheek-puff / tongue-out scores (cached); None when disabled or unavailable."""
    from . import rare

    if not cfg.expressions.enabled:
        return None
    if not rare.available():
        log("rare expressions skipped: torch/transformers not installed (pip install -e '.[rare]')")
        return None
    stride = cfg.expressions.stride
    cache = CACHE / "rare" / f"{_signals_key(video, cfg)}-{stride}-{rare.MODEL_ID.replace('/', '_')}.npz"
    if cache.exists():
        d = np.load(cache)
        return {k: d[k] for k in d.files}
    i = cfg.input
    out = rare.scores(video, s, start=i.start, flip=i.flip, width=i.analysis_width, stride=stride, log=log,
                      progress=progress)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, **out)
    return out


def filter_group(pid: str) -> str | None:
    """The filtering.* setting that sets pid's low-pass cutoff; None: not filtered."""
    for pat, group in FILTER_GROUPS:
        if re.search(pat, pid):
            return group
    return "other"


def cutoff(filtering, group: str, fps: float) -> float:
    """Low-pass cutoff (Hz) of a filtering group times filtering.cutoff_scale. A cutoff at or above
    the Nyquist limit switches the filter off, so scaling keeps a filtered group below it (0.45 fps)."""
    base = getattr(filtering, group)
    c = base * filtering.cutoff_scale
    return min(c, 0.45 * fps) if c >= fps / 2 > base else c


def solve(signals: Signals, model: Model, cfg: Config, rare_scores: dict[str, np.ndarray] | None = None,
          overrides: dict | None = None, dump: bool = False) -> tuple[dict[str, np.ndarray], dict]:
    """Returns Live2D parameter curves on the output grid and a record of every decision.
    overrides: a v2l.mapping object (input curves, bridge changes, entries; docs/motion-editing.md).
    dump: also put the tracking inputs and bridge outputs on the grid into info["signals"]."""
    info: dict = {}
    fps = cfg.output.fps
    f = features(signals, rare_scores)
    x, cal = calibrate(f, signals.t, cfg.calibration, cfg.expressions)
    x = scale(x, cfg.intensity)
    raw, t, present = temporal.resample(x, signals.t, fps, max_gap=cfg.gaps.max_gap, ramp=cfg.gaps.ramp)
    n = len(t)
    grid = mapping.apply_input_curves(overrides, raw)
    bridge = Bridge.load(cfg.bridge.preset, (overrides or {}).get("bridge"))
    bridged, missing = bridge.evaluate(grid, fps)
    signals_for_map = {**grid, **bridged}

    entries, notes, map_warnings = mapping.effective(model, overrides, cfg.bridge.preset)
    own_cutoff = {e.output: e.cutoff for e in entries if e.enabled}  # the last entry for a parameter wins
    breath_period = cfg.output.breath_period
    if cfg.output.loop and n > 1:
        cycles = max(1, round((n - 1) / fps / breath_period))
        breath_period = (n - 1) / fps / cycles
    params = mapping.apply(entries, signals_for_map, n, fps, breath_period=breath_period)
    params = {k: v for k, v in params.items() if k in model.ranges and k not in model.physics_outputs}

    if cfg.style.name == "cute":
        apply_cute(params, bridged, model.ranges, fps, cfg.style)
    elif cfg.style.name != "faithful":
        raise ValueError(f"unknown style {cfg.style.name!r}")

    for pid, v in params.items():
        group = filter_group(pid)
        hz = own_cutoff.get(pid)
        if hz is not None:
            v = temporal.denoise(v, hz, fps, despike=cfg.filtering.despike)
        elif group is not None:
            v = temporal.denoise(v, cutoff(cfg.filtering, group, fps), fps, despike=cfg.filtering.despike)
        if cfg.output.loop:
            v = temporal.make_loop(v, fps, blend=cfg.output.loop_blend)
        r = model.ranges[pid]
        params[pid] = np.clip(v, r["min"], r["max"])

    info.update(
        calibration=cal, bridge=bridge.name, bridge_inputs_without_signal=missing, mapping_notes=notes,
        mapping=[mapping.entry_json(e) for e in entries], mapping_warnings=map_warnings,
        frames=n, duration=float(t[-1]) if n else 0.0,
        presence_min=float(present.min()) if n else 0.0,
        # Seconds with the zero-shot cheek puff / tongue out above half (None: stage not run).
        rare_seconds={k: float((raw[k] > 0.5).sum() / fps) for k in ("CheekPuff", "TongueOut")}
        if rare_scores is not None else None,
    )
    if dump:
        info["signals"] = signals_json(raw, grid, bridged, fps, (overrides or {}).get("input_curves") or {})
    return params, info


def signals_json(raw: dict, curved: dict, bridged: dict, fps: float, input_curves: dict) -> dict:
    """The <name>.signals.json object: tracking inputs after input curves ("inputs"), the values
    before the curve for the inputs that have one ("inputs_raw"), and the bridge outputs, one value
    per output frame rounded to 4 decimals (null where nothing was tracked)."""
    def series(v):
        return [round(float(a), 4) if np.isfinite(a) else None for a in v]
    n = len(next(iter(raw.values()))) if raw else 0
    return {"fps": fps, "duration": (n - 1) / fps if n else 0.0,
            "inputs": {k: series(v) for k, v in curved.items()},
            "inputs_raw": {k: series(raw[k]) for k in sorted(set(input_curves) & set(raw))},
            "bridge": {k: series(v) for k, v in bridged.items()}}


def quality_report(signals: Signals, params: dict[str, np.ndarray], model: Model, info: dict, fps: float) -> dict:
    """Numbers a reviewer needs before trusting a motion; warnings flag likely failures."""
    face_rate = float(signals.face_ok.mean())
    pose_rate = float(signals.pose_ok.mean())
    lost = ~signals.face_ok
    longest = 0.0
    for a, b in temporal._runs(lost):
        longest = max(longest, float(signals.t[min(b, len(signals.t) - 1)] - signals.t[a]))
    per_param = {}
    for pid, v in params.items():
        r = model.ranges[pid]
        span = r["max"] - r["min"] or 1.0
        acc = np.diff(v, 2) * fps * fps / span if len(v) > 2 else np.zeros(1)
        per_param[pid] = {
            "p5": float(np.percentile(v, 5)), "p50": float(np.median(v)), "p95": float(np.percentile(v, 95)),
            "use": float((np.percentile(v, 95) - np.percentile(v, 5)) / span),
            "jitter": float(np.median(np.abs(acc))),
        }
    blinks = {}
    for pid in ("ParamEyeLOpen", "ParamEyeROpen"):
        if pid in params:
            closed = params[pid] < 0.3 * model.ranges[pid]["def"]
            blinks[pid] = int(len(temporal._runs(closed)))
    warnings = []
    if face_rate < 0.9:
        warnings.append(f"face found in only {face_rate:.0%} of frames")
    if longest > 1.0:
        warnings.append(f"face lost for {longest:.1f} s; the avatar relaxes to rest there")
    for pid, label in (("ParamMouthOpenY", "mouth"), ("ParamAngleX", "head turn"), ("ParamEyeLOpen", "eyes")):
        if pid in per_param and per_param[pid]["use"] < 0.05:
            warnings.append(f"{label} ({pid}) barely moves (uses {per_param[pid]['use']:.0%} of its range)")
    return {
        "frames_tracked": int(len(signals.t)), "face_rate": face_rate, "pose_rate": pose_rate,
        "longest_face_gap_s": longest, "neutral_window_s": info["calibration"]["neutral_window_s"],
        "blinks": blinks, "params": per_param, "warnings": warnings,
    }


def run(video: str | pathlib.Path, model_dir: str | pathlib.Path, name: str, out_dir: str | pathlib.Path,
        cfg: Config, install: bool = True, log=print, event=None, mapping_overrides: dict | None = None,
        dump_signals: bool = False) -> dict:
    """Writes <out_dir>/<name>.motion3.json, .report.json, and (cfg.preview.enabled) .preview.mp4;
    with install the motion is also copied into the model folder (VTube Studio lists it as an
    animation). mapping_overrides: a v2l.mapping object (checked against the model before tracking).
    dump_signals: also write <name>.signals.json (tracking inputs and bridge outputs per frame).
    event receives {"event": "stage", "stage": s} as each of STAGES begins and
    {"event": "progress", "stage": s, "done": d, "total": t} while tracking, scoring expressions
    and rendering the preview."""
    from .render import render_motion

    event = event or text_events(log)

    def stage(s: str) -> None:
        event({"event": "stage", "stage": s})

    video = pathlib.Path(video).expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")
    Bridge.load(cfg.bridge.preset, (mapping_overrides or {}).get("bridge"))  # fail before tracking
    out_dir = pathlib.Path(out_dir).expanduser().resolve()
    stage("probe")
    model = prepare_model(model_dir, log=log)
    if mapping_overrides:
        mapping.effective(model, mapping_overrides, cfg.bridge.preset)  # fail before tracking
    out_dir.mkdir(parents=True, exist_ok=True)
    stage("tracking")
    signals = get_signals(video, cfg, log=log, progress=_progress(event, "tracking"))
    stage("expressions")
    rare_scores = get_rare(video, signals, cfg, log=log, progress=_progress(event, "expressions"))
    stage("solving")
    log("solving")
    params, info = solve(signals, model, cfg, rare_scores, mapping_overrides, dump=dump_signals)
    if dump_signals:
        (out_dir / f"{name}.signals.json").write_text(json.dumps(info.pop("signals"), separators=(",", ":")))
    stage("writing")
    fps, preview = cfg.output.fps, cfg.preview.enabled
    motion = motion3.build(params, model.ranges, fps, loop=cfg.output.loop, fade_in=cfg.output.fade,
                           fade_out=cfg.output.fade, tolerance=cfg.output.tolerance,
                           interpolation=cfg.output.interpolation)
    errs = motion3.validate(motion, model.ranges)
    if errs:
        raise RuntimeError("generated motion is invalid: " + "; ".join(errs))
    motion_name = f"{name}.motion3.json"
    text = json.dumps(motion, ensure_ascii=False, indent=1)
    (out_dir / motion_name).write_text(text)
    if install or preview:  # the renderer plays motions from the model folder
        (model.dir / motion_name).write_text(text)

    report = quality_report(signals, params, model, info, fps)
    report.update(name=name, video=str(video), model=model.name, style=cfg.style.name, fps=fps, loop=cfg.output.loop,
                  curves=motion["Meta"]["CurveCount"], points=motion["Meta"]["TotalPointCount"],
                  mapping_notes=info["mapping_notes"], bridge=info["bridge"], rare_seconds=info["rare_seconds"],
                  bridge_inputs_without_signal=info["bridge_inputs_without_signal"], calibration=info["calibration"],
                  mapping=info["mapping"], mapping_warnings=info["mapping_warnings"],
                  mapping_overrides=mapping_overrides, config=to_dict(cfg))
    try:
        if preview:
            stage("preview")
            log("rendering preview")
            render_motion(model, motion_name, out_dir / f"{name}.preview.mp4", fps,
                          model.probe.get("framing") if model.probe else None, src_video=str(video),
                          src_start=cfg.input.start or 0.0, width=cfg.preview.width, height=cfg.preview.height,
                          progress=_progress(event, "preview"))
    finally:
        if not install:
            (model.dir / motion_name).unlink(missing_ok=True)
    (out_dir / f"{name}.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return report
