"""Every tunable setting of V2L, in one schema shared by the CLI, V2L Studio and the MCP server.

Standard library only, so the laptop side (v2l-remote, Studio, the MCP server) can read the
schema without the tracking dependencies:

  python -m v2l.config schema          field metadata as JSON (types, defaults, ranges, help)
  python -m v2l.config defaults        the default configuration as JSON
  python -m v2l.config validate FILE   check a configuration file
  python -m v2l.config presets         built-in and user presets

A configuration file is JSON with any subset of the sections below, e.g.
{"version": 1, "style": {"name": "faithful"}, "calibration": {"jaw_full": 0.2}}.
Unknown keys, wrong types and out-of-range values are errors. A preset is a configuration file
with an optional "meta": {"name": ..., "description": ...}; built-in presets live in
v2l/presets/, user presets in $XDG_CONFIG_HOME/v2l/presets (default ~/.config/v2l/presets).
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import sys
from typing import Any

CONFIG_VERSION = 1
BUILTIN_PRESETS = pathlib.Path(__file__).parent / "presets"


class ConfigError(ValueError):
    pass


def param(default: Any, *, label: str, help: str, min: float | None = None, max: float | None = None,
          step: float | None = None, unit: str | None = None, choices: tuple[str, ...] | None = None,
          kind: str | None = None, nullable: bool = False, advanced: bool = False,
          affects: str = "solve") -> Any:
    """A setting with the metadata the UI, the CLI and the MCP schema need.

    kind is inferred from the default: boolean, integer, number, string; "enum" with choices;
    "range" is an ordered [low, high] pair, "pair" an [x, y] pair, "window" a [start, end] time span.
    affects says what a change invalidates: "tracking" (the video is tracked again), "solve"
    (cached tracking is reused and only the motion is recomputed) or "output" (only the
    preview rendering changes).
    """
    if kind is None:
        if choices:
            kind = "enum"
        elif isinstance(default, bool):
            kind = "boolean"
        elif isinstance(default, int):
            kind = "integer"
        elif isinstance(default, float) or default is None:
            kind = "number"
        elif isinstance(default, str):
            kind = "string"
        else:
            raise TypeError(f"cannot infer the kind of {default!r}")
    meta = dict(label=label, help=help, kind=kind, min=min, max=max, step=step, unit=unit,
                choices=list(choices) if choices else None, nullable=nullable, advanced=advanced, affects=affects)
    return dataclasses.field(default=default, metadata={k: v for k, v in meta.items() if v is not None})


# ------------------------------------------------------------------------------------- sections

@dataclasses.dataclass
class InputConfig:
    start: float | None = param(None, label="Start", unit="s", min=0.0, step=0.1, nullable=True, affects="tracking",
                                help="Use the clip from this time. Empty: from the beginning.")
    end: float | None = param(None, label="End", unit="s", min=0.0, step=0.1, nullable=True, affects="tracking",
                              help="Use the clip up to this time. Empty: to the end.")
    flip: bool = param(False, label="Mirrored source", affects="tracking",
                       help="The video was recorded mirrored (selfie camera); flip it before tracking.")
    analysis_width: int = param(1280, label="Analysis width", unit="px", min=320, max=3840, step=160, advanced=True,
                                affects="tracking",
                                help="Wider frames are scaled down to this width before tracking. "
                                     "Larger is slower and rarely more accurate.")


@dataclasses.dataclass
class OutputConfig:
    fps: float = param(30.0, label="Frame rate", unit="fps", min=10.0, max=120.0, step=1.0,
                       help="Sampling rate of the motion curves.")
    fade: float = param(0.5, label="Fade in/out", unit="s", min=0.0, max=3.0, step=0.1,
                        help="Fade-in and fade-out time written into the motion3.json.")
    loop: bool = param(False, label="Seamless loop",
                       help="The motion ends where it starts and breathing completes whole cycles. "
                            "Use it for idle animations.")
    loop_blend: float = param(0.5, label="Loop blend", unit="s", min=0.1, max=3.0, step=0.1, advanced=True,
                              help="With a seamless loop: how long the end eases into the first frame.")
    breath_period: float = param(3.4, label="Breathing period", unit="s", min=1.5, max=8.0, step=0.1,
                                 help="Length of one breathing cycle. With a seamless loop it is adjusted "
                                      "to fit whole cycles.")
    interpolation: str = param("linear", label="Curve interpolation", choices=("linear", "bezier"),
                               help="How motion curves are written. linear: straight segments between kept points. "
                                    "bezier: smooth restricted Bezier segments fitted to the motion (about half the "
                                    "keys and easier to edit, but a larger file).")
    tolerance: float = param(0.004, label="Curve simplification", unit="of range", min=0.0005, max=0.05,
                             step=0.0005, advanced=True,
                             help="Largest deviation allowed when simplifying or fitting curves, as a fraction of "
                                  "each parameter's range. Larger gives smaller files and fewer keys.")


@dataclasses.dataclass
class CalibrationConfig:
    neutral_window: tuple[float, float] | None = param(
        None, label="Rest-face window", kind="window", unit="s", min=0.0, step=0.1, nullable=True,
        help="Time span (relative to Start) where the face is neutral. Empty: found automatically.")
    neutral_seconds: float = param(0.6, label="Rest window length", unit="s", min=0.2, max=3.0, step=0.1,
                                   help="Length of the automatically found rest window: the least "
                                        "expressive stretch of the clip.")
    rest_head_range: float = param(20.0, label="Rest head tolerance", unit="deg", min=5.0, max=60.0, step=1.0,
                                   advanced=True,
                                   help="Head turns this far from the clip's usual pose count fully against "
                                        "a window being the rest face.")
    full_percentile: float = param(98.0, label="Full-expression percentile", unit="%", min=80.0, max=100.0,
                                   step=0.5, advanced=True,
                                   help="For brow, eye and other non-mouth channels: the clip percentile "
                                        "that counts as a full expression.")
    min_span: float = param(0.08, label="Minimum span", min=0.01, max=0.5, step=0.01, advanced=True,
                            help="Smallest raw span of a channel that may be stretched to a full expression.")
    max_gain: float = param(3.0, label="Maximum gain", unit="x", min=1.0, max=10.0, step=0.1, advanced=True,
                            help="Upper limit on how much a weak tracker channel is amplified.")
    blink_deadzone: tuple[float, float] = param(
        (0.15, 0.8), label="Blink dead zone", kind="range", min=0.0, max=1.0, step=0.01,
        help="Eye closure below the first value reads as open, above the second as fully closed.")
    blink_landmark_weight: float = param(0.7, label="Blink landmark weight", min=0.0, max=1.0, step=0.05,
                                         advanced=True,
                                         help="Blink = this share from the measured eye aperture plus the rest "
                                              "from MediaPipe's blink blendshape.")
    eye_closed_ratio: float = param(0.45, label="Closed-eye aperture", min=0.1, max=0.9, step=0.05,
                                    advanced=True,
                                    help="Eye aperture, as a fraction of the open aperture, that always reads "
                                         "as closed.")
    eye_wide_range: float = param(0.25, label="Wide-eye range", min=0.05, max=1.0, step=0.05, advanced=True,
                                  help="Aperture above the open level, as a fraction of it, that reads as "
                                       "fully wide.")
    jaw_full: float = param(0.16, label="Jaw fully open at", min=0.03, max=0.5, step=0.01, advanced=True,
                            help="Jaw drop growth over the rest face (fraction) that reads as fully open.")
    lip_gap_full: float = param(0.14, label="Lips fully open at", min=0.03, max=0.5, step=0.01, advanced=True,
                                help="Inner-lip gap, as a fraction of face height, that reads as fully open.")
    mouth_narrow_full: float = param(0.12, label="Mouth fully narrow at", min=0.03, max=0.5, step=0.01,
                                     advanced=True,
                                     help="Mouth width decrease, as a fraction of the rest width, that reads "
                                          "as fully narrow (pucker).")
    mouth_wide_full: float = param(0.15, label="Mouth fully wide at", min=0.03, max=0.5, step=0.01, advanced=True,
                                   help="Mouth width increase, as a fraction of the rest width, that reads as "
                                        "fully wide.")
    mouth_shift_full: float = param(0.08, label="Mouth fully sideways at", min=0.02, max=0.3, step=0.01,
                                    advanced=True,
                                    help="Sideways mouth shift, in eye distances, that reads as fully left or right.")
    gaze_full: tuple[float, float] = param(
        (0.35, 0.30), label="Gaze fully turned at", kind="pair", min=0.05, max=1.0, step=0.01,
        help="Iris offset, in half eye widths, that reads as looking fully sideways (first) and up or "
             "down (second).")


@dataclasses.dataclass
class IntensityConfig:
    head: float = param(1.0, label="Head", unit="x", min=0.0, max=3.0, step=0.05,
                        help="Scales head yaw, pitch and roll.")
    body: float = param(1.0, label="Body", unit="x", min=0.0, max=3.0, step=0.05,
                        help="Scales the shoulder roll, turn and height that drive the body angles.")
    gaze: float = param(1.0, label="Gaze", unit="x", min=0.0, max=3.0, step=0.05,
                        help="Scales eye-ball movement.")
    brows: float = param(1.0, label="Brows", unit="x", min=0.0, max=3.0, step=0.05,
                         help="Scales brow raise and frown.")
    smile: float = param(1.0, label="Smile", unit="x", min=0.0, max=3.0, step=0.05,
                         help="Scales the smile channels.")
    mouth_open: float = param(1.0, label="Mouth opening", unit="x", min=0.0, max=3.0, step=0.05,
                              help="Scales jaw and lip opening.")
    mouth_shape: float = param(1.0, label="Mouth shape", unit="x", min=0.0, max=3.0, step=0.05,
                               help="Scales width, pucker, funnel, press, shrug, roll and sideways shift.")
    cheek_tongue: float = param(1.0, label="Cheek puff and tongue", unit="x", min=0.0, max=3.0, step=0.05,
                                help="Scales cheek puff and tongue out.")


@dataclasses.dataclass
class GapConfig:
    max_gap: float = param(0.4, label="Bridge gaps up to", unit="s", min=0.0, max=3.0, step=0.1,
                           help="Tracking gaps up to this long are interpolated.")
    ramp: float = param(0.3, label="Ease to rest", unit="s", min=0.05, max=2.0, step=0.05, advanced=True,
                        help="Longer gaps ease the avatar to the rest pose over this time on each side.")


@dataclasses.dataclass
class ExpressionConfig:
    enabled: bool = param(True, label="Cheek puff and tongue out", affects="tracking",
                          help="Experimental zero-shot scoring with SigLIP 2. Needs the optional torch and "
                               "transformers install; skipped without them.")
    stride: int = param(3, label="Score every N-th frame", unit="frames", min=1, max=30, step=1, affects="tracking",
                        help="Frames in between are interpolated. Lower is slower.")
    cheek_thresholds: tuple[float, float] = param(
        (0.018, 0.035), label="Cheek puff thresholds", kind="range", min=-0.1, max=0.2, step=0.001, advanced=True,
        help="Raw cheek-puff score mapped to 0..1 between these two values.")
    tongue_thresholds: tuple[float, float] = param(
        (0.008, 0.06), label="Tongue out thresholds", kind="range", min=-0.1, max=0.2, step=0.001, advanced=True,
        help="Raw tongue-out score mapped to 0..1 between these two values.")


@dataclasses.dataclass
class BridgeConfig:
    preset: str = param("default", label="Bridge preset",
                        help="Built-in preset name or the path of a SnenkBridge-format JSON file.")


@dataclasses.dataclass
class StyleConfig:
    name: str = param("cute", label="Style", choices=("cute", "faithful"),
                      help="cute adds a happier, livelier performance; faithful adds nothing. "
                           "The settings below apply to cute only.")
    mouth_form_bias: float = param(0.15, label="Mouth corner lift", min=-1.0, max=1.0, step=0.05,
                                   help="Added to ParamMouthForm, in half-range units.")
    eye_smile_max: float = param(0.7, label="Eye smile", min=0.0, max=1.0, step=0.05,
                                 help="ParamEyeLSmile / ParamEyeRSmile at a full smile.")
    blush_rest: float = param(0.25, label="Blush at rest", min=0.0, max=1.0, step=0.05,
                              help="ParamCheek at rest.")
    blush_smile: float = param(0.75, label="Blush when smiling", min=0.0, max=1.0, step=0.05,
                               help="ParamCheek at a full smile.")
    eye_open_scale: float = param(1.1, label="Resting eye size", unit="x", min=0.5, max=1.5, step=0.05,
                                  help="Resting eye opening is multiplied by this; blinks still close fully.")
    head_gain: float = param(1.2, label="Head liveliness", unit="x", min=0.5, max=3.0, step=0.05,
                             help="Head angles are multiplied by this and soft-limited to the parameter range.")
    sparkle_param: str = param("ParamEyeSparkle", label="Sparkle parameter", advanced=True,
                               help="Parameter switched on during long big smiles, if the model has it.")
    sparkle_smile: float = param(0.75, label="Sparkle smile level", min=0.0, max=1.0, step=0.05, advanced=True,
                                 help="Smile level that must hold to switch the sparkle on.")
    sparkle_hold: float = param(0.5, label="Sparkle hold", unit="s", min=0.0, max=5.0, step=0.1, advanced=True,
                                help="How long the smile must hold.")


@dataclasses.dataclass
class FilterConfig:
    despike: bool = param(True, label="Remove single-frame spikes", advanced=True,
                          help="3-frame median before low-pass filtering, against tracker glitches.")
    cutoff_scale: float = param(1.0, label="Overall smoothing", unit="x", min=0.25, max=4.0, step=0.05,
                                help="Multiplies every cutoff below. Under 1 is smoother and slower, above 1 "
                                     "crisper and noisier. Cutoffs stay below the Nyquist limit of the frame rate.")
    head: float = param(3.0, label="Head angles", unit="Hz", min=0.2, max=15.0, step=0.1,
                        help="Low-pass cutoff for ParamAngle*. Lower is smoother and slower.")
    body: float = param(1.5, label="Body angles", unit="Hz", min=0.2, max=15.0, step=0.1,
                        help="Low-pass cutoff for ParamBodyAngle*.")
    eyeball: float = param(4.0, label="Eye balls", unit="Hz", min=0.2, max=15.0, step=0.1,
                           help="Low-pass cutoff for ParamEyeBall*.")
    eye_open: float = param(8.0, label="Eye opening", unit="Hz", min=0.2, max=15.0, step=0.1,
                            help="Low-pass cutoff for eye opening; keep it high so blinks stay crisp.")
    brows: float = param(3.0, label="Brows", unit="Hz", min=0.2, max=15.0, step=0.1,
                         help="Low-pass cutoff for brow parameters.")
    smile: float = param(3.0, label="Smile and squint", unit="Hz", min=0.2, max=15.0, step=0.1,
                         help="Low-pass cutoff for smile and squint parameters.")
    mouth: float = param(6.0, label="Mouth, jaw, tongue, cheek puff", unit="Hz", min=0.2, max=15.0, step=0.1,
                         help="Low-pass cutoff for mouth-related parameters.")
    blush: float = param(0.8, label="Blush", unit="Hz", min=0.2, max=15.0, step=0.1, advanced=True,
                         help="Low-pass cutoff for ParamCheek.")
    sparkle: float = param(2.0, label="Sparkle", unit="Hz", min=0.2, max=15.0, step=0.1, advanced=True,
                           help="Low-pass cutoff for the sparkle parameter.")
    other: float = param(4.0, label="Other parameters", unit="Hz", min=0.2, max=15.0, step=0.1, advanced=True,
                         help="Low-pass cutoff for everything else (breathing is never filtered).")


EMOTIONS = ("neutral", "amazement", "anger", "cheekiness", "disgust", "fear", "grief", "joy", "outofbreath",
            "pain", "sadness")


@dataclasses.dataclass
class AudioConfig:
    use: str = param("off", label="Use the audio track", choices=("off", "mouth", "face"), affects="tracking",
                     help="For video input: off uses the video only; mouth takes jaw, lips and tongue from the "
                          "audio; face takes the whole face from the audio and keeps head, body and gaze from "
                          "the video. Audio-only input always uses the audio.")
    identity: str = param("Claire", label="Speaking style", choices=("Claire", "James", "Mark"), affects="tracking",
                          help="Which of Audio2Face-3D's trained actors the face imitates.")
    emotion: str = param("neutral", label="Emotion", choices=EMOTIONS, affects="tracking",
                         help="Emotion the face performs while speaking.")
    emotion_strength: float = param(0.6, label="Emotion strength", min=0.0, max=1.0, step=0.05, affects="tracking",
                                    help="How strongly the chosen emotion shows. No effect with neutral.")
    strength: float = param(1.0, label="Audio face strength", unit="x", min=0.0, max=2.0, step=0.05,
                            affects="tracking", help="Overall strength of the audio-driven face.")
    upper_face_strength: float = param(1.0, label="Upper face", unit="x", min=0.0, max=2.0, step=0.05,
                                       affects="tracking", advanced=True,
                                       help="Strength of brow and eye-area motion from the audio.")
    lower_face_strength: float = param(1.0, label="Lower face", unit="x", min=0.0, max=2.0, step=0.05,
                                       affects="tracking", advanced=True,
                                       help="Strength of mouth and jaw motion from the audio.")
    blink_strength: float = param(1.0, label="Blinks", unit="x", min=0.0, max=2.0, step=0.05, affects="tracking",
                                  advanced=True, help="Strength of the blinks the audio model generates.")
    eyeballs_strength: float = param(1.0, label="Eye movement", unit="x", min=0.0, max=2.0, step=0.05,
                                     affects="tracking", advanced=True,
                                     help="Strength of the eye rotation the audio model generates.")
    saccade_strength: float = param(0.6, label="Saccades", unit="x", min=0.0, max=2.0, step=0.05,
                                    affects="tracking", advanced=True,
                                    help="Small quick eye jumps added by the audio model.")
    tongue_strength: float = param(1.3, label="Tongue", unit="x", min=0.0, max=2.0, step=0.05, affects="tracking",
                                   advanced=True, help="Strength of tongue motion from the audio.")
    lip_open_offset: float = param(0.0, label="Lip opening offset", min=-0.2, max=0.2, step=0.01,
                                   affects="tracking", advanced=True,
                                   help="Constant added to the audio model's lip opening.")
    offset: float = param(0.0, label="Audio offset", unit="s", min=-1.0, max=1.0, step=0.01, affects="tracking",
                          help="Shifts the audio-driven face in time; positive moves it later.")
    seed: int = param(0, label="Seed", min=0, max=100000, step=1, affects="tracking", advanced=True,
                      help="Seed for the audio model's random parts (saccades, diffusion).")
    device: str = param("auto", label="Device", choices=("auto", "cuda", "cpu"), affects="tracking", advanced=True,
                        help="Where Audio2Face-3D runs. auto uses an NVIDIA GPU when available.")


@dataclasses.dataclass
class IdleConfig:
    head_amplitude: float = param(4.0, label="Head motion", unit="deg", min=0.0, max=20.0, step=0.5,
                                  help="Size of the generated head sway when there is no video.")
    head_speed: float = param(0.35, label="Head motion speed", unit="Hz", min=0.05, max=2.0, step=0.05,
                              help="Typical rate of the generated head sway.")
    speech_nods: float = param(1.0, label="Speech emphasis", unit="x", min=0.0, max=3.0, step=0.05,
                               help="Small nods and tilts that follow the loudness of the speech.")
    body_amplitude: float = param(1.0, label="Body motion", unit="x", min=0.0, max=3.0, step=0.05,
                                  help="Body sway that follows the generated head motion.")
    blinks: str = param("procedural", label="Blinks", choices=("model", "procedural", "off"),
                        help="procedural adds natural random blinks (Audio2Face-3D v3.0 itself does not "
                             "blink); model uses only the audio model's eye closing; off keeps the eyes open.")
    blink_rate: float = param(15.0, label="Blink rate", unit="per min", min=2.0, max=40.0, step=1.0,
                              help="Average blinks per minute for procedural blinks.")
    gaze: str = param("model", label="Gaze", choices=("model", "center"),
                      help="model uses the audio model's eye movement; center keeps the eyes forward.")
    seed: int = param(0, label="Seed", min=0, max=100000, step=1, advanced=True,
                      help="Seed for the generated head, body and blink motion.")


@dataclasses.dataclass
class PreviewConfig:
    enabled: bool = param(True, label="Render preview video", affects="output",
                          help="Render the source next to the avatar as an MP4 after solving.")
    width: int = param(720, label="Avatar width", unit="px", min=240, max=2160, step=8, advanced=True,
                       affects="output", help="Width of the avatar half of the preview.")
    height: int = param(900, label="Height", unit="px", min=240, max=2160, step=8, advanced=True,
                        affects="output", help="Height of the preview video.")


SECTIONS: dict[str, tuple[type, str, str]] = {
    "input": (InputConfig, "Input", "Which part of the video is used and how it is read."),
    "output": (OutputConfig, "Motion output", "Frame rate, fades, looping and file size of the motion."),
    "calibration": (CalibrationConfig, "Calibration",
                    "How the rest face and the full range of each expression are estimated from the clip."),
    "intensity": (IntensityConfig, "Intensity", "Overall strength of each group of movements."),
    "gaps": (GapConfig, "Tracking gaps", "What happens when the face is lost for a moment."),
    "expressions": (ExpressionConfig, "Cheek puff and tongue", "The optional zero-shot expression stage."),
    "bridge": (BridgeConfig, "Bridge", "VBridger-style formulas from tracking inputs to bridge outputs."),
    "style": (StyleConfig, "Style", "Performance style added on top of the tracked motion."),
    "filtering": (FilterConfig, "Smoothing", "Zero-phase low-pass filtering per parameter group."),
    "preview": (PreviewConfig, "Preview", "The side-by-side preview video."),
    "audio": (AudioConfig, "Audio", "Audio-driven face with NVIDIA Audio2Face-3D."),
    "idle": (IdleConfig, "Generated motion", "Head, body and blinks generated when there is no video."),
}


@dataclasses.dataclass
class Config:
    input: InputConfig = dataclasses.field(default_factory=InputConfig)
    output: OutputConfig = dataclasses.field(default_factory=OutputConfig)
    calibration: CalibrationConfig = dataclasses.field(default_factory=CalibrationConfig)
    intensity: IntensityConfig = dataclasses.field(default_factory=IntensityConfig)
    gaps: GapConfig = dataclasses.field(default_factory=GapConfig)
    expressions: ExpressionConfig = dataclasses.field(default_factory=ExpressionConfig)
    bridge: BridgeConfig = dataclasses.field(default_factory=BridgeConfig)
    style: StyleConfig = dataclasses.field(default_factory=StyleConfig)
    filtering: FilterConfig = dataclasses.field(default_factory=FilterConfig)
    preview: PreviewConfig = dataclasses.field(default_factory=PreviewConfig)
    audio: AudioConfig = dataclasses.field(default_factory=AudioConfig)
    idle: IdleConfig = dataclasses.field(default_factory=IdleConfig)


# --------------------------------------------------------------------------- schema and values

def schema() -> dict:
    """Field metadata for every section, in display order."""
    sections = []
    for sid, (cls, label, help_) in SECTIONS.items():
        fields = []
        for f in dataclasses.fields(cls):
            d = f.default
            fields.append({"key": f"{sid}.{f.name}", "name": f.name,
                           "default": list(d) if isinstance(d, tuple) else d, **f.metadata})
        sections.append({"id": sid, "label": label, "help": help_, "fields": fields})
    return {"version": CONFIG_VERSION, "sections": sections}


def to_dict(cfg: Config) -> dict:
    out: dict[str, Any] = {"version": CONFIG_VERSION}
    for sid in SECTIONS:
        sec = getattr(cfg, sid)
        out[sid] = {f.name: list(v) if isinstance(v := getattr(sec, f.name), tuple) else v
                    for f in dataclasses.fields(sec)}
    return out


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check(key: str, meta: dict, v: Any) -> Any:
    """Validated value for one field (lists become tuples)."""
    kind = meta["kind"]
    if v is None:
        if meta.get("nullable"):
            return None
        raise ConfigError(f"{key} must not be empty")

    def bounded(x: float) -> float:
        lo, hi = meta.get("min"), meta.get("max")
        if (lo is not None and x < lo) or (hi is not None and x > hi):
            raise ConfigError(f"{key}={x} is outside {lo if lo is not None else '-inf'}..{hi if hi is not None else 'inf'}")
        return x

    if kind == "boolean":
        if not isinstance(v, bool):
            raise ConfigError(f"{key} must be true or false")
        return v
    if kind == "integer":
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if not isinstance(v, int) or isinstance(v, bool):
            raise ConfigError(f"{key} must be an integer")
        return int(bounded(v))
    if kind == "number":
        if not _is_number(v):
            raise ConfigError(f"{key} must be a number")
        return float(bounded(v))
    if kind == "string":
        if not isinstance(v, str) or not v:
            raise ConfigError(f"{key} must be a non-empty string")
        return v
    if kind == "enum":
        if v not in meta["choices"]:
            raise ConfigError(f"{key} must be one of {', '.join(meta['choices'])}")
        return v
    if kind in ("range", "pair", "window"):
        if not isinstance(v, (list, tuple)) or len(v) != 2 or not all(_is_number(x) for x in v):
            raise ConfigError(f"{key} must be a pair of numbers")
        a, b = (float(bounded(x)) for x in v)
        if kind == "range" and a > b:
            raise ConfigError(f"{key}: the first value must not exceed the second")
        if kind == "window" and a >= b:
            raise ConfigError(f"{key}: the end must be after the start")
        return (a, b)
    raise ConfigError(f"{key}: unknown kind {kind}")


def merge(cfg: Config, data: dict) -> Config:
    """cfg with the values of a (partial) configuration dict applied; strict."""
    if not isinstance(data, dict):
        raise ConfigError("a configuration must be a JSON object")
    version = data.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigError(f"unsupported configuration version {version!r} (expected {CONFIG_VERSION})")
    out = cfg
    for sid, values in data.items():
        if sid in ("version", "meta"):
            continue
        if sid not in SECTIONS:
            raise ConfigError(f"unknown section {sid!r}")
        if not isinstance(values, dict):
            raise ConfigError(f"section {sid!r} must be a JSON object")
        sec = getattr(out, sid)
        metas = {f.name: f.metadata for f in dataclasses.fields(sec)}
        changes = {}
        for name, v in values.items():
            if name not in metas:
                raise ConfigError(f"unknown setting {sid}.{name}")
            changes[name] = _check(f"{sid}.{name}", metas[name], v)
        out = dataclasses.replace(out, **{sid: dataclasses.replace(sec, **changes)})
    _check_consistency(out)
    return out


def _check_consistency(cfg: Config) -> None:
    i = cfg.input
    if i.start is not None and i.end is not None and i.end <= i.start:
        raise ConfigError("input.end must be after input.start")


def parse_assignment(text: str) -> dict:
    """'section.name=value' -> {"section": {"name": value}}. The value is read as JSON when it
    parses (numbers, true/false, null, [a, b]), as 'a,b' or 'a:b' for pairs, else as a string."""
    key, sep, raw = text.partition("=")
    sid, dot, name = key.strip().partition(".")
    if not sep or not dot or not sid or not name:
        raise ConfigError(f"expected section.name=value, got {text!r}")
    raw = raw.strip()
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError:
        parts = raw.replace(":", ",").split(",")
        if len(parts) == 2:
            try:
                value = [float(p) for p in parts]
            except ValueError:
                value = raw
        else:
            value = raw
    return {sid: {name: value}}


def load(path: str | pathlib.Path) -> dict:
    try:
        return json.loads(pathlib.Path(path).expanduser().read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: not valid JSON ({e})") from None


def build(files: list[str] = (), assignments: list[str] = (), base: Config | None = None,
          preset: str | None = None) -> Config:
    """Defaults <- preset <- configuration files in order <- section.name=value assignments in order."""
    cfg = base or Config()
    if preset:
        cfg = merge(cfg, load(find_preset(preset)))
    for f in files:
        cfg = merge(cfg, load(f))
    for a in assignments:
        cfg = merge(cfg, parse_assignment(a))
    return cfg


# ------------------------------------------------------------------------------------- presets

PRESET_NAME = r"^[A-Za-z0-9_-]{1,64}$"


def user_preset_dir() -> pathlib.Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
    return pathlib.Path(base) / "v2l" / "presets"


def find_preset(name: str) -> pathlib.Path:
    """A preset file: an existing *.json path, else a user preset, else a built-in one."""
    path = pathlib.Path(name).expanduser()
    if path.suffix == ".json" and path.is_file():
        return path
    if re.match(PRESET_NAME, name):
        for d in (user_preset_dir(), BUILTIN_PRESETS):
            if (d / f"{name}.json").is_file():
                return d / f"{name}.json"
    raise ConfigError(f"unknown preset {name!r} (see `python -m v2l.config presets`)")


def presets() -> list[dict]:
    """Built-in and user presets; a user preset with a built-in's name replaces it."""
    found: dict[str, dict] = {}
    for d, builtin in ((BUILTIN_PRESETS, True), (user_preset_dir(), False)):
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                data = load(f)
                merge(Config(), data)
            except ConfigError as e:
                found[f.stem] = {"name": f.stem, "builtin": builtin, "path": str(f), "error": str(e)}
                continue
            meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
            found[f.stem] = {"name": f.stem, "title": meta.get("name", f.stem),
                             "description": meta.get("description", ""), "builtin": builtin, "path": str(f),
                             "settings": {k: v for k, v in data.items() if k not in ("version", "meta")}}
    return list(found.values())


def save_user_preset(name: str, data: dict) -> pathlib.Path:
    """Validates data (a partial configuration, optionally with meta) and writes it as a user preset."""
    if not re.match(PRESET_NAME, name):
        raise ConfigError("preset names use letters, digits, '_' and '-' (up to 64)")
    merge(Config(), data)
    d = user_preset_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps({"version": CONFIG_VERSION, **{k: v for k, v in data.items() if k != "version"}},
                              ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m v2l.config", description="v2l configuration schema and checks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="field metadata as JSON")
    sub.add_parser("defaults", help="the default configuration as JSON")
    sub.add_parser("presets", help="built-in and user presets as JSON")
    v = sub.add_parser("validate", help="check configuration files and print the merged result")
    v.add_argument("files", nargs="+")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "schema":
            out = schema()
        elif a.cmd == "defaults":
            out = to_dict(Config())
        elif a.cmd == "presets":
            out = presets()
        else:
            out = to_dict(build(a.files))
    except ConfigError as e:
        print(f"v2l config: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
