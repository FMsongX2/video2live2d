"""Cubism .motion3.json writing from dense parameter curves; reading back and validation are in
motionedit (every segment type)."""

from __future__ import annotations

import numpy as np

from . import motionedit


def rdp(t: np.ndarray, v: np.ndarray, eps: float) -> list[int]:
    """Indices kept by Ramer-Douglas-Peucker with a vertical (value) error bound of eps."""
    keep = {0, len(v) - 1}
    stack = [(0, len(v) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg_t, seg_v = t[i:j + 1], v[i:j + 1]
        line = v[i] + (v[j] - v[i]) * (seg_t - t[i]) / (t[j] - t[i])
        err = np.abs(seg_v - line)
        k = int(np.argmax(err))
        if err[k] > eps:
            keep.add(i + k)
            stack += [(i, i + k), (i + k, j)]
    return sorted(keep)


def _bezier_segments(t: np.ndarray, v: np.ndarray, eps: float) -> tuple[list, int, int]:
    """Fitted restricted Bezier curve: (Segments list, segment count, point count)."""
    keys, segs = motionedit.fit_bezier(t.tolist(), v.tolist(), eps)
    times = [round(float(k[0]), 4) for k in keys]
    out = [times[0], round(float(keys[0][1]), 5)]
    for i, s in enumerate(segs):
        t0, t1 = times[i], times[i + 1]
        out += [1, round(t0 + (t1 - t0) / 3, 4), round(float(s["c1"][1]), 5),
                round(t1 - (t1 - t0) / 3, 4), round(float(s["c2"][1]), 5), t1, round(float(keys[i + 1][1]), 5)]
    return out, len(segs), 1 + 3 * len(segs)


def build(params: dict[str, np.ndarray], ranges: dict, fps: float, *, loop: bool = False,
          fade_in: float = 0.5, fade_out: float = 0.5, tolerance: float = 0.004,
          interpolation: str = "linear") -> dict:
    """tolerance is the largest allowed deviation as a fraction of each parameter's range.
    interpolation: "linear" (Ramer-Douglas-Peucker points joined by straight segments) or "bezier"
    (restricted Bezier segments fitted by motionedit.fit_bezier, smooth at every key)."""
    if interpolation not in ("linear", "bezier"):
        raise ValueError(f"interpolation must be linear or bezier, not {interpolation!r}")
    n = len(next(iter(params.values())))
    if n < 2:
        raise ValueError("a motion needs at least two frames")
    t = np.arange(n) / fps
    curves, seg_total, pt_total = [], 0, 0
    for pid in sorted(params):
        r = ranges[pid]
        v = np.clip(np.asarray(params[pid], dtype=float), r["min"], r["max"])
        eps = tolerance * (r["max"] - r["min"])
        if interpolation == "bezier":
            segs, n_seg, n_pt = _bezier_segments(t, v, eps)
        else:
            idx = rdp(t, v, eps=eps)
            segs = [round(float(t[idx[0]]), 4), round(float(v[idx[0]]), 5)]
            for k in idx[1:]:
                segs += [0, round(float(t[k]), 4), round(float(v[k]), 5)]
            n_seg, n_pt = len(idx) - 1, len(idx)
        curves.append({"Target": "Parameter", "Id": pid, "Segments": segs})
        seg_total += n_seg
        pt_total += n_pt
    return {
        "Version": 3,
        "Meta": {
            "Duration": round(float(t[-1]), 4),
            "Fps": float(fps),
            "FadeInTime": fade_in,
            "FadeOutTime": fade_out,
            "Loop": loop,
            "AreBeziersRestricted": True,
            "CurveCount": len(curves),
            "TotalSegmentCount": seg_total,
            "TotalPointCount": pt_total,
            "UserDataCount": 0,
            "TotalUserDataSize": 0,
        },
        "Curves": curves,
    }


def evaluate(motion: dict, times: np.ndarray) -> dict[str, np.ndarray]:
    """Parameter values at the given times (every segment type)."""
    return {k: np.asarray(v) for k, v in motionedit.evaluate(motion, np.asarray(times, dtype=float).tolist()).items()}


validate = motionedit.validate
