"""Transfer curves on 0..1 (standard library; numpy arrays accepted when numpy is installed).

A curve is {"points": [[x, y], ...], "smooth": bool}: x runs from 0 to 1, strictly increasing.
smooth=true is a monotone cubic through the points (Fritsch and Carlson, "Monotone Piecewise Cubic
Interpolation", SIAM J. Numer. Anal. 1980): it never overshoots between two points. smooth=false
joins the points with straight lines. Beyond 0..1 the curve continues along its end slopes.
"""

from __future__ import annotations

import bisect
import math


def problems(curve, where: str) -> list[str]:
    """Why a curve object is invalid (empty when valid)."""
    if not isinstance(curve, dict):
        return [f'{where} must be {{"points": [[x, y], ...], "smooth": true|false}}']
    errs = []
    unknown = set(curve) - {"points", "smooth"}
    if unknown:
        errs.append(f"{where}: unknown field {', '.join(sorted(unknown))}")
    if "smooth" in curve and not isinstance(curve["smooth"], bool):
        errs.append(f"{where}.smooth must be true or false")
    pts = curve.get("points")
    ok = isinstance(pts, list) and len(pts) >= 2 and all(
        isinstance(p, (list, tuple)) and len(p) == 2 and all(
            isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) for c in p) for p in pts)
    if not ok:
        return errs + [f"{where}.points must be at least two [x, y] number pairs"]
    xs = [p[0] for p in pts]
    if any(b <= a for a, b in zip(xs, xs[1:])):
        errs.append(f"{where}.points: x must be strictly increasing")
    if xs[0] != 0 or xs[-1] != 1:
        errs.append(f"{where}.points must start at x = 0 and end at x = 1")
    return errs


class Curve:
    def __init__(self, points, smooth: bool = True):
        self.x = [float(p[0]) for p in points]
        self.y = [float(p[1]) for p in points]
        self.smooth = bool(smooth)
        n = len(self.x)
        d = [(self.y[k + 1] - self.y[k]) / (self.x[k + 1] - self.x[k]) for k in range(n - 1)]
        if not self.smooth:
            self.m = None
            self.end_slopes = (d[0], d[-1])
            return
        m = [0.0] * n
        m[0], m[-1] = d[0], d[-1]
        for k in range(1, n - 1):
            m[k] = 0.0 if d[k - 1] * d[k] <= 0 else (d[k - 1] + d[k]) / 2
        for k in range(n - 1):  # Fritsch-Carlson: limit the slopes so each piece stays monotone
            if d[k] == 0:
                m[k] = m[k + 1] = 0.0
                continue
            a, b = m[k] / d[k], m[k + 1] / d[k]
            r = a * a + b * b
            if r > 9:
                tau = 3 / math.sqrt(r)
                m[k], m[k + 1] = tau * a * d[k], tau * b * d[k]
        self.m = m
        self.end_slopes = (m[0], m[-1])

    @classmethod
    def from_json(cls, obj: dict) -> "Curve":
        return cls(obj["points"], obj.get("smooth", True))

    def value(self, u: float) -> float:
        x, y = self.x, self.y
        if u != u:  # NaN stays NaN (no tracking)
            return u
        if u <= x[0]:
            return y[0] + self.end_slopes[0] * (u - x[0])
        if u >= x[-1]:
            return y[-1] + self.end_slopes[1] * (u - x[-1])
        k = bisect.bisect_right(x, u) - 1
        h = x[k + 1] - x[k]
        s = (u - x[k]) / h
        if self.m is None:
            return y[k] + (y[k + 1] - y[k]) * s
        s2, s3 = s * s, s * s * s
        return ((2 * s3 - 3 * s2 + 1) * y[k] + (s3 - 2 * s2 + s) * h * self.m[k]
                + (-2 * s3 + 3 * s2) * y[k + 1] + (s3 - s2) * h * self.m[k + 1])

    def __call__(self, u):
        if isinstance(u, (int, float)):
            return self.value(float(u))
        import numpy as np

        u = np.asarray(u, dtype=float)
        out = np.array([self.value(float(a)) for a in u.ravel()]).reshape(u.shape)
        out[~np.isfinite(u)] = np.nan
        return out
