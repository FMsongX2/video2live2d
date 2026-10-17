"""Optional appeal layer applied to Live2D parameters after mapping.

faithful  nothing added; the avatar does what the bridge and the model's mapping say.
cute      a happier rest face, eye smile and blush that follow the smile, slightly larger
          resting eyes, a little more head motion, and an optional sparkle parameter during
          sustained big smiles. Only parameters the model has are touched.
"""

from __future__ import annotations

import numpy as np

from .calibrate import smoothstep
from .config import StyleConfig


def _hold(mask: np.ndarray, min_len: int) -> np.ndarray:
    out = np.zeros_like(mask)
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        if b - a >= min_len:
            out[a:b] = True
    return out


def apply_cute(params: dict[str, np.ndarray], bridge: dict[str, np.ndarray], ranges: dict, fps: float,
               cfg: StyleConfig) -> None:
    """The cute layer with the cute settings of cfg (cfg.name is not consulted)."""
    smile = np.clip((bridge.get("MouthSmile", 0.5) - 0.5) * 2, 0, 1)  # 0 rest .. 1 full smile
    if "ParamMouthForm" in params:
        r = ranges["ParamMouthForm"]
        params["ParamMouthForm"] = np.clip(params["ParamMouthForm"] + cfg.mouth_form_bias * (r["max"] - r["min"]) / 2, r["min"], r["max"])
    for k in ("ParamEyeLSmile", "ParamEyeRSmile"):
        if k in ranges:
            params[k] = ranges[k]["min"] + cfg.eye_smile_max * (ranges[k]["max"] - ranges[k]["min"]) * smoothstep(0.25, 0.9, smile)
    if "ParamCheek" in ranges:
        r = ranges["ParamCheek"]
        params["ParamCheek"] = r["min"] + (r["max"] - r["min"]) * (cfg.blush_rest + (cfg.blush_smile - cfg.blush_rest) * smoothstep(0.2, 0.9, smile))
    for k in ("ParamEyeLOpen", "ParamEyeROpen"):
        if k in params:
            rest = ranges[k]["def"]
            v = params[k]
            params[k] = np.where(v >= rest, rest * cfg.eye_open_scale + (v - rest) * 0.8, v * cfg.eye_open_scale)
    for k in ("ParamAngleX", "ParamAngleY", "ParamAngleZ"):
        if k in params:
            lim = max(abs(ranges[k]["min"]), abs(ranges[k]["max"]))
            params[k] = lim * np.tanh(cfg.head_gain * params[k] / lim)
    if cfg.sparkle_param in ranges:
        r = ranges[cfg.sparkle_param]
        on = _hold(smile > cfg.sparkle_smile, max(int(cfg.sparkle_hold * fps), 1))
        params[cfg.sparkle_param] = r["min"] + (r["max"] - r["min"]) * on.astype(float)
