"""Cubism .motion3.json writing, reading back and validation (linear segments only)."""

from __future__ import annotations

import numpy as np


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


def build(params: dict[str, np.ndarray], ranges: dict, fps: float, *, loop: bool = False,
          fade_in: float = 0.5, fade_out: float = 0.5, tolerance: float = 0.004) -> dict:
    """tolerance is the largest allowed deviation as a fraction of each parameter's range."""
    n = len(next(iter(params.values())))
    if n < 2:
        raise ValueError("a motion needs at least two frames")
    t = np.arange(n) / fps
    curves, seg_total, pt_total = [], 0, 0
    for pid in sorted(params):
        r = ranges[pid]
        v = np.clip(np.asarray(params[pid], dtype=float), r["min"], r["max"])
        idx = rdp(t, v, eps=tolerance * (r["max"] - r["min"]))
        segs = [round(float(t[idx[0]]), 4), round(float(v[idx[0]]), 5)]
        for k in idx[1:]:
            segs += [0, round(float(t[k]), 4), round(float(v[k]), 5)]
        curves.append({"Target": "Parameter", "Id": pid, "Segments": segs})
        seg_total += len(idx) - 1
        pt_total += len(idx)
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
    """Parameter values at the given times (linear segments; other types raise)."""
    out = {}
    for c in motion["Curves"]:
        s = c["Segments"]
        pts_t, pts_v = [s[0]], [s[1]]
        i = 2
        while i < len(s):
            if s[i] != 0:
                raise ValueError(f"{c['Id']}: only linear segments are supported, got type {s[i]}")
            pts_t.append(s[i + 1])
            pts_v.append(s[i + 2])
            i += 3
        out[c["Id"]] = np.interp(times, pts_t, pts_v)
    return out


def validate(motion: dict, ranges: dict | None = None) -> list[str]:
    """Problems that would make Cubism runtimes misread the file; empty when valid."""
    errs: list[str] = []
    meta = motion.get("Meta", {})
    curves = motion.get("Curves", [])
    if motion.get("Version") != 3:
        errs.append("Version must be 3")
    if meta.get("CurveCount") != len(curves):
        errs.append("Meta.CurveCount does not match Curves")
    segs = pts = 0
    for c in curves:
        s = c.get("Segments", [])
        if len(s) < 2 or (len(s) - 2) % 3:
            errs.append(f"{c.get('Id')}: malformed linear segment list")
            continue
        ts = [s[0]] + s[3::3]
        if any(b <= a for a, b in zip(ts, ts[1:])):
            errs.append(f"{c.get('Id')}: segment times are not strictly increasing")
        if ts[0] != 0:
            errs.append(f"{c.get('Id')}: first point is not at time 0")
        if abs(ts[-1] - meta.get("Duration", -1)) > 1e-3:
            errs.append(f"{c.get('Id')}: last point {ts[-1]} != Duration")
        if ranges is not None:
            r = ranges.get(c.get("Id"))
            vals = [s[1]] + s[4::3]
            if r is None:
                errs.append(f"{c.get('Id')}: not a parameter of the model")
            elif min(vals) < r["min"] - 1e-6 or max(vals) > r["max"] + 1e-6:
                errs.append(f"{c.get('Id')}: values outside {r['min']}..{r['max']}")
        segs += (len(s) - 2) // 3
        pts += 1 + (len(s) - 2) // 3
    if meta.get("TotalSegmentCount") != segs:
        errs.append("Meta.TotalSegmentCount does not match")
    if meta.get("TotalPointCount") != pts:
        errs.append("Meta.TotalPointCount does not match")
    return errs
