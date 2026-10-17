"""Editable Cubism motions: read, write, evaluate, fit, validate and edit (standard library only).

Reference implementation of docs/motion-editing.md. A motion3.json is turned into the editable
`v2l.motion-edit` form (keys, per-segment interpolation, Bezier handles) and back; every segment
type of the motion3 format is evaluated exactly, including unrestricted Beziers.

  python -m v2l.motionedit info FILE [--json]
  python -m v2l.motionedit read FILE [--curves IDS] [--json]
  python -m v2l.motionedit write EDIT.json --out FILE [--overwrite] [--json]
  python -m v2l.motionedit edit FILE --ops OPS.json --out FILE [--overwrite] [--ranges R.json] [--json]
  python -m v2l.motionedit refit FILE --interpolation bezier|linear --tolerance X [--curves IDS]
                                 [--ranges R.json] --out FILE [--overwrite] [--json]
  python -m v2l.motionedit eval FILE (--times T,... | --fps N) [--curves IDS] [--json]
  python -m v2l.motionedit validate FILE [--ranges R.json] [--json]

The same commands are `v2l motion ...`. FILE is a .motion3.json or an editable JSON file, or `-`
for stdin; `--out -` writes nothing and returns the result as "motion" in the JSON response.
IDS is a comma-separated list of curve ids. R.json maps parameter ids to [min, max].
Exit codes: 0 ok, 1 runtime failure, 2 usage, input or validation error.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import pathlib
import sys

FORMAT = "v2l.motion-edit"
VERSION = 1
TYPES = ("linear", "bezier", "stepped", "inverse_stepped")
CODE = {name: i for i, name in enumerate(TYPES)}
TARGETS = ("Parameter", "PartOpacity", "Model")
TIME_EPS = 1e-4          # a key is addressed by its time within this many seconds
DURATION_SLACK = 1e-3    # keys may end after Duration by at most one frame (Duration is often rounded)
META_KNOWN = ("Duration", "Fps", "FadeInTime", "FadeOutTime", "Loop", "AreBeziersRestricted", "CurveCount",
              "TotalSegmentCount", "TotalPointCount", "UserDataCount", "TotalUserDataSize")
CURVE_KNOWN = ("Target", "Id", "FadeInTime", "FadeOutTime", "Segments")


class MotionError(ValueError):
    """A motion that cannot be read or written; problems lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class EditError(ValueError):
    """An operation that cannot be applied; nothing is changed."""

    def __init__(self, message: str, index: int | None = None, op: str | None = None):
        prefix = f"operation {index} ({op}): " if index is not None else ""
        super().__init__(prefix + message)
        self.index = index


# ------------------------------------------------------------------------------ motion3 <-> editable

def _parse_segments(raw, cid: str) -> tuple[list[list[float]], list[dict]]:
    if not isinstance(raw, list) or len(raw) < 2 or not all(_num(x) for x in raw):
        raise MotionError([f"{cid}: Segments must be a list of numbers starting with a point"])
    keys = [[raw[0], raw[1]]]
    segs: list[dict] = []
    i = 2
    while i < len(raw):
        code = raw[i]
        if code == 1:
            if i + 7 > len(raw):
                raise MotionError([f"{cid}: truncated bezier segment at index {i}"])
            segs.append({"type": "bezier", "c1": [raw[i + 1], raw[i + 2]], "c2": [raw[i + 3], raw[i + 4]]})
            keys.append([raw[i + 5], raw[i + 6]])
            i += 7
        elif code in (0, 2, 3):
            if i + 3 > len(raw):
                raise MotionError([f"{cid}: truncated segment at index {i}"])
            segs.append({"type": TYPES[int(code)]})
            keys.append([raw[i + 1], raw[i + 2]])
            i += 3
        else:
            raise MotionError([f"{cid}: unknown segment type {code!r} at index {i}"])
    return keys, segs


def from_motion3(m: dict, curves: list[str] | None = None) -> dict:
    """The editable form of a motion3 dict (curves: keep only these ids, in file order)."""
    if not isinstance(m, dict) or not isinstance(m.get("Curves"), list):
        raise MotionError(["not a motion3.json: no Curves list"])
    meta = m.get("Meta") or {}
    restricted = bool(meta.get("AreBeziersRestricted", False))
    out_curves = []
    problems = []
    for c in m["Curves"]:
        cid = c.get("Id")
        if curves is not None and cid not in curves:
            continue
        try:
            keys, segs = _parse_segments(c.get("Segments"), str(cid))
        except MotionError as e:
            problems += e.problems
            continue
        curve = {"target": c.get("Target", "Parameter"), "id": cid, "keys": keys, "segments": segs}
        if "FadeInTime" in c:
            curve["fade_in"] = c["FadeInTime"]
        if "FadeOutTime" in c:
            curve["fade_out"] = c["FadeOutTime"]
        extra = {k: v for k, v in c.items() if k not in CURVE_KNOWN}
        if extra:
            curve["extra"] = extra
        if restricted:
            _thirds(curve)
        out_curves.append(curve)
    if problems:
        raise MotionError(problems)
    if curves is not None:
        missing = sorted(set(curves) - {c["id"] for c in out_curves})
        if missing:
            raise MotionError([f"no curve {', '.join(missing)} in the motion"])
    return {
        "format": FORMAT, "version": VERSION,
        "meta": {"duration": meta.get("Duration", 0.0), "fps": meta.get("Fps", 30.0), "loop": bool(meta.get("Loop", False)),
                 "fade_in": meta.get("FadeInTime"), "fade_out": meta.get("FadeOutTime"), "restricted": restricted},
        "curves": out_curves,
        "passthrough": {"meta": {k: v for k, v in meta.items() if k not in META_KNOWN},
                        "user_data": copy.deepcopy(m.get("UserData", []))},
    }


def _user_data_size(user_data: list) -> int:
    return sum(len(str(u.get("Value", "")).encode("utf-8")) for u in user_data)


def counts(edit: dict) -> dict:
    segs = sum(len(c["segments"]) for c in edit["curves"])
    pts = sum(1 + sum(3 if s["type"] == "bezier" else 1 for s in c["segments"]) for c in edit["curves"])
    return {"curves": len(edit["curves"]), "segments": segs, "points": pts,
            "keys": sum(len(c["keys"]) for c in edit["curves"]),
            "user_data": len(edit.get("passthrough", {}).get("user_data") or [])}


def to_motion3(edit: dict, ranges: dict | None = None) -> dict:
    """The motion3 dict of an editable motion. With meta.restricted, Bezier control times are
    recomputed from the key times; Meta counts are always recomputed. Raises MotionError."""
    problems = validate(edit, ranges)
    if problems:
        raise MotionError(problems)
    meta_in = edit["meta"]
    restricted = bool(meta_in.get("restricted", True))
    curves = []
    for c in edit["curves"]:
        k = c["keys"]
        seg = [k[0][0], k[0][1]]
        for i, s in enumerate(c["segments"]):
            (t0, _), (t1, v1) = k[i], k[i + 1]
            if s["type"] == "bezier":
                c1t, c2t = (_r6(t0 + (t1 - t0) / 3), _r6(t1 - (t1 - t0) / 3)) if restricted else (s["c1"][0], s["c2"][0])
                seg += [1, c1t, s["c1"][1], c2t, s["c2"][1], t1, v1]
            else:
                seg += [CODE[s["type"]], t1, v1]
        out = {"Target": c.get("target", "Parameter"), "Id": c["id"]}
        if c.get("fade_in") is not None:
            out["FadeInTime"] = c["fade_in"]
        if c.get("fade_out") is not None:
            out["FadeOutTime"] = c["fade_out"]
        out["Segments"] = seg
        out.update(c.get("extra") or {})
        curves.append(out)
    pas = edit.get("passthrough") or {}
    user_data = pas.get("user_data") or []
    n = counts(edit)
    meta = {"Duration": meta_in["duration"], "Fps": meta_in.get("fps", 30.0)}
    if meta_in.get("fade_in") is not None:
        meta["FadeInTime"] = meta_in["fade_in"]
    if meta_in.get("fade_out") is not None:
        meta["FadeOutTime"] = meta_in["fade_out"]
    meta.update(Loop=bool(meta_in.get("loop", False)), AreBeziersRestricted=restricted, CurveCount=n["curves"],
                TotalSegmentCount=n["segments"], TotalPointCount=n["points"], UserDataCount=len(user_data),
                TotalUserDataSize=_user_data_size(user_data))
    meta.update(pas.get("meta") or {})
    m = {"Version": 3, "Meta": meta, "Curves": curves}
    if user_data:
        m["UserData"] = copy.deepcopy(user_data)
    return m


def _r6(x: float) -> float:
    return round(x, 6)


def _thirds(curve: dict) -> None:
    """Restricted form: control times at a third of the segment from each end."""
    k = curve["keys"]
    for i, s in enumerate(curve["segments"]):
        if s["type"] == "bezier":
            t0, t1 = k[i][0], k[i + 1][0]
            s["c1"] = [t0 + (t1 - t0) / 3, s["c1"][1]]
            s["c2"] = [t1 - (t1 - t0) / 3, s["c2"][1]]


def is_editable(data) -> bool:
    return isinstance(data, dict) and data.get("format") == FORMAT


def as_editable(data: dict, curves: list[str] | None = None) -> dict:
    """An editable copy of data (motion3 or editable)."""
    if is_editable(data):
        e = copy.deepcopy(data)
        if curves is not None:
            missing = sorted(set(curves) - {c.get("id") for c in e.get("curves", [])})
            if missing:
                raise MotionError([f"no curve {', '.join(missing)} in the motion"])
            e["curves"] = [c for c in e["curves"] if c.get("id") in curves]
        return e
    return from_motion3(data, curves)


# ------------------------------------------------------------------------------------- validation

def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pair(x) -> bool:
    return isinstance(x, (list, tuple)) and len(x) == 2 and _num(x[0]) and _num(x[1])


def _range(ranges: dict | None, cid: str):
    if ranges is None or cid not in ranges:
        return None
    r = ranges[cid]
    return (r["min"], r["max"]) if isinstance(r, dict) else (r[0], r[1])


def validate(motion: dict, ranges: dict | None = None) -> list[str]:
    """Problems that would make the motion unreadable or misread; empty when valid. motion is a
    motion3 dict (counts in Meta are checked too) or an editable motion. ranges: {id: {min, max}}
    or {id: [min, max]}; then every Parameter curve must be a model parameter with keys inside."""
    if is_editable(motion):
        return _validate_editable(motion, ranges)
    errs: list[str] = []
    if not isinstance(motion, dict):
        return ["not a JSON object"]
    if motion.get("Version") != 3:
        errs.append("Version must be 3")
    meta = motion.get("Meta")
    if not isinstance(meta, dict):
        return errs + ["Meta is missing"]
    try:
        edit = from_motion3(motion)
    except MotionError as e:
        return errs + e.problems
    n = counts(edit)
    if meta.get("CurveCount") != n["curves"]:
        errs.append("Meta.CurveCount does not match Curves")
    if meta.get("TotalSegmentCount") != n["segments"]:
        errs.append("Meta.TotalSegmentCount does not match")
    if meta.get("TotalPointCount") != n["points"]:
        errs.append("Meta.TotalPointCount does not match")
    if meta.get("UserDataCount", 0) != n["user_data"]:
        errs.append("Meta.UserDataCount does not match UserData")
    return errs + _validate_editable(edit, ranges)


def _validate_editable(e: dict, ranges: dict | None) -> list[str]:
    errs: list[str] = []
    if e.get("format") != FORMAT:
        errs.append(f"format must be {FORMAT!r}")
    if e.get("version") != VERSION:
        errs.append(f"version must be {VERSION}")
    meta = e.get("meta")
    if not isinstance(meta, dict):
        return errs + ["meta is missing"]
    dur = meta.get("duration")
    if not _num(dur) or dur <= 0:
        errs.append("meta.duration must be a positive number")
        dur = None
    fps = meta.get("fps", 30.0)
    if not _num(fps) or fps <= 0:
        errs.append("meta.fps must be a positive number")
        fps = 30.0
    slack = max(DURATION_SLACK, 1.0 / fps)
    for k in ("fade_in", "fade_out"):
        if meta.get(k) is not None and (not _num(meta[k]) or meta[k] < 0):
            errs.append(f"meta.{k} must be a number >= 0 or null")
    for k in ("loop", "restricted"):
        if k in meta and not isinstance(meta[k], bool):
            errs.append(f"meta.{k} must be true or false")
    curves = e.get("curves")
    if not isinstance(curves, list):
        return errs + ["curves must be a list"]
    seen = set()
    restricted = bool(meta.get("restricted", True))
    for c in curves:
        if not isinstance(c, dict):
            errs.append("every curve must be an object")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            errs.append("every curve needs an id")
            continue
        target = c.get("target", "Parameter")
        if target not in TARGETS:
            errs.append(f"{cid}: target must be one of {', '.join(TARGETS)}")
        if (target, cid) in seen:
            errs.append(f"{cid}: duplicate curve")
        seen.add((target, cid))
        for k in ("fade_in", "fade_out"):
            if c.get(k) is not None and (not _num(c[k]) or c[k] < 0):
                errs.append(f"{cid}: {k} must be a number >= 0")
        keys, segs = c.get("keys"), c.get("segments")
        if not isinstance(keys, list) or not keys or not all(_pair(k) for k in keys):
            errs.append(f"{cid}: keys must be a non-empty list of [time, value]")
            continue
        if not isinstance(segs, list) or len(segs) != len(keys) - 1:
            errs.append(f"{cid}: segments must have one entry per pair of neighbouring keys")
            continue
        ts = [k[0] for k in keys]
        if any(b <= a for a, b in zip(ts, ts[1:])):
            errs.append(f"{cid}: key times are not strictly increasing")
        if abs(ts[0]) > 1e-9:
            errs.append(f"{cid}: first point is not at time 0")
        if dur is not None and ts[-1] > dur + slack:
            errs.append(f"{cid}: last point {ts[-1]} is after Duration {dur}")
        for i, s in enumerate(segs):
            if not isinstance(s, dict) or s.get("type") not in TYPES:
                errs.append(f"{cid}: segment {i} type must be one of {', '.join(TYPES)}")
                continue
            if s["type"] == "bezier":
                if not (_pair(s.get("c1")) and _pair(s.get("c2"))):
                    errs.append(f"{cid}: bezier segment {i} needs c1 and c2 as [time, value]")
                elif not restricted:
                    t0, t1 = ts[i], ts[i + 1]
                    if not (t0 - 1e-9 <= s["c1"][0] <= t1 + 1e-9 and t0 - 1e-9 <= s["c2"][0] <= t1 + 1e-9):
                        errs.append(f"{cid}: bezier segment {i} has control times outside its keys")
        if target == "Parameter" and ranges is not None:
            r = _range(ranges, cid)
            if r is None:
                errs.append(f"{cid}: not a parameter of the model")
            else:
                vals = [k[1] for k in keys]
                if min(vals) < r[0] - 1e-6 or max(vals) > r[1] + 1e-6:
                    errs.append(f"{cid}: values outside {r[0]}..{r[1]}")
    ud = (e.get("passthrough") or {}).get("user_data") or []
    if not isinstance(ud, list) or not all(isinstance(u, dict) and _num(u.get("Time")) for u in ud):
        errs.append("passthrough.user_data must be a list of {Time, Value}")
    return errs


# ------------------------------------------------------------------------------------- evaluation

def _cubic(a: float, b: float, c: float, d: float, s: float) -> float:
    u = 1.0 - s
    return u * u * u * a + 3 * u * u * s * b + 3 * u * s * s * c + s * s * s * d


def bezier_parameter(t: float, t0: float, c1t: float, c2t: float, t1: float) -> float:
    """Curve parameter s in [0, 1] whose time coordinate is t (bisection to machine precision)."""
    lo, hi = 0.0, 1.0
    if _cubic(t0, c1t, c2t, t1, 1.0) < _cubic(t0, c1t, c2t, t1, 0.0):  # a reversed segment never occurs in
        lo, hi = hi, lo                                                # valid files; kept for robustness
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        if _cubic(t0, c1t, c2t, t1, mid) < t:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def segment_value(p0, seg: dict, p1, t: float, restricted: bool) -> float:
    """Value of one segment at t (t0 <= t <= t1)."""
    (t0, v0), (t1, v1) = p0, p1
    kind = seg["type"]
    if kind == "linear":
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    if kind == "stepped":
        return v0 if t < t1 else v1
    if kind == "inverse_stepped":
        return v1
    if restricted:
        s = (t - t0) / (t1 - t0)
    else:
        s = bezier_parameter(t, t0, seg["c1"][0], seg["c2"][0], t1)
    return _cubic(v0, seg["c1"][1], seg["c2"][1], v1, s)


def _find(times: list[float], t: float) -> int:
    """Index i of the segment with times[i] <= t < times[i+1] (bisection); -1 before the first key."""
    lo, hi = 0, len(times) - 1
    if t < times[0]:
        return -1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if times[mid] <= t:
            lo = mid
        else:
            hi = mid
    return lo


def _value(keys: list, segs: list, ts: list[float], t: float, restricted: bool) -> float:
    if len(keys) == 1 or t < ts[0]:
        return keys[0][1]
    if t >= ts[-1]:
        return keys[-1][1]
    i = _find(ts, t)
    return segment_value(keys[i], segs[i], keys[i + 1], t, restricted)


def value_at(curve: dict, t: float, restricted: bool = True) -> float:
    """Curve value at time t. At a key time the segment starting there applies (as in Cubism
    runtimes); before the first key the first value holds, after the last key the last value."""
    return _value(curve["keys"], curve["segments"], [k[0] for k in curve["keys"]], t, restricted)


def evaluate(motion: dict, times, curves: list[str] | None = None) -> dict[str, list[float]]:
    """{curve id: [value at each time]} for a motion3 or editable motion."""
    e = motion if is_editable(motion) else from_motion3(motion)
    restricted = bool(e["meta"].get("restricted", True))
    out = {}
    for c in e["curves"]:
        if curves is not None and c["id"] not in curves:
            continue
        ts = [k[0] for k in c["keys"]]
        out[c["id"]] = [_value(c["keys"], c["segments"], ts, float(t), restricted) for t in times]
    return out


def frame_times(duration: float, fps: float) -> list[float]:
    n = int(math.floor(duration * fps + 1e-9))
    return [i / fps for i in range(n + 1)]


# ---------------------------------------------------------------------------------------- fitting

def rdp(times: list[float], values: list[float], eps: float) -> list[int]:
    """Indices kept by Ramer-Douglas-Peucker with a vertical (value) error bound of eps."""
    keep = {0, len(values) - 1}
    stack = [(0, len(values) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ti, tj, vi, vj = times[i], times[j], values[i], values[j]
        worst, k = -1.0, i
        for m in range(i + 1, j):
            err = abs(values[m] - (vi + (vj - vi) * (times[m] - ti) / (tj - ti)))
            if err > worst:
                worst, k = err, m
        if worst > eps:
            keep.add(k)
            stack += [(i, k), (k, j)]
    return sorted(keep)


def fit_linear(times: list[float], values: list[float], tol: float) -> tuple[list[list[float]], list[dict]]:
    idx = rdp(times, values, tol)
    return [[times[i], values[i]] for i in idx], [{"type": "linear"} for _ in idx[1:]]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        p = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / p
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def _basis(s: float) -> tuple[float, float, float, float]:
    u = 1.0 - s
    return u * u * u, 3 * u * u * s, 3 * u * s * s, s * s * s


class _Fitter:
    """Restricted-Bezier least squares after Schneider (1990). Time advances linearly with the curve
    parameter, so each span is a cubic in time and no reparameterisation is needed. The keys are
    samples; the control values follow from one slope per key shared by both neighbouring spans
    (so the curve is C1), solved by least squares over all samples at once (a tridiagonal system).
    Every span whose worst sample misses the tolerance is split there, and the slopes are solved
    again, until all samples are within the tolerance."""

    def __init__(self, t: list[float], v: list[float], tol: float, prefer: list[int] | None = None):
        self.t, self.v, self.tol = t, v, tol
        self.prefer = sorted(prefer or [])
        n = len(t)
        # A faint pull toward the local data slope decides slopes that no sample constrains.
        self.prior = [(v[min(q + 1, n - 1)] - v[max(q - 1, 0)]) / (t[min(q + 1, n - 1)] - t[max(q - 1, 0)])
                      for q in range(n)]
        self.ridge = 1e-12 * max(t[-1] - t[0], 1.0) ** 2

    def slopes(self, knots: list[int]) -> list[float]:
        t, v = self.t, self.v
        m = len(knots)
        diag = [self.ridge] * m
        off = [0.0] * (m - 1)
        rhs = [self.ridge * self.prior[k] for k in knots]
        for a in range(m - 1):
            i, j = knots[a], knots[a + 1]
            h = t[j] - t[i]
            for q in range(i + 1, j):
                b0, b1, b2, b3 = _basis((t[q] - t[i]) / h)
                r = v[q] - (b0 + b1) * v[i] - (b2 + b3) * v[j]
                p, s = b1 * h / 3, -b2 * h / 3  # d(value)/d(slope at i), d(value)/d(slope at j)
                diag[a] += p * p
                diag[a + 1] += s * s
                off[a] += p * s
                rhs[a] += p * r
                rhs[a + 1] += s * r
        for a in range(1, m):  # Thomas algorithm (the matrix is symmetric positive definite)
            f = off[a - 1] / diag[a - 1]
            diag[a] -= f * off[a - 1]
            rhs[a] -= f * rhs[a - 1]
        x = [0.0] * m
        x[-1] = rhs[-1] / diag[-1]
        for a in range(m - 2, -1, -1):
            x[a] = (rhs[a] - off[a] * x[a + 1]) / diag[a]
        return x

    def error(self, i: int, j: int, c1: float, c2: float) -> tuple[float, int]:
        t, v = self.t, self.v
        h = t[j] - t[i]
        worst, at = 0.0, i
        for k in range(i + 1, j):
            e = abs(v[k] - _cubic(v[i], c1, c2, v[j], (t[k] - t[i]) / h))
            if e > worst:
                worst, at = e, k
        return worst, at

    SPLIT_SHARE = 0.8  # per round, split the spans whose error is at least this share of the worst one

    def fit(self) -> list[tuple[int, int, float, float]]:
        t, v = self.t, self.v
        knots = [0, len(t) - 1]
        while True:
            m = self.slopes(knots)
            spans, errors = [], []
            for a in range(len(knots) - 1):
                i, j = knots[a], knots[a + 1]
                h = t[j] - t[i]
                c1, c2 = v[i] + m[a] * h / 3, v[j] - m[a + 1] * h / 3
                spans.append((i, j, c1, c2))
                errors.append(self.error(i, j, c1, c2))
            worst = max(e for e, _ in errors)
            if worst <= self.tol:
                return spans
            split = []
            for (i, j, _, _), (err, k) in zip(spans, errors):
                if err > self.tol and err >= self.SPLIT_SHARE * worst:
                    inside = [p for p in self.prefer if i < p < j]
                    if inside:  # split at a preferred sample (an original key) nearest the worst one
                        k = min(inside, key=lambda p: abs(p - k))
                    split.append(k)
            knots = sorted(set(knots).union(split))


def fit_bezier(times: list[float], values: list[float], tol: float,
               prefer: list[int] | None = None) -> tuple[list[list[float]], list[dict]]:
    """Few restricted Bezier segments within tol of every sample, C1 at interior keys. prefer:
    sample indices where spans are split first (the keys of a curve being refitted)."""
    if len(times) == 1:
        return [[times[0], values[0]]], []
    spans = _Fitter(list(map(float, times)), list(map(float, values)), float(tol), prefer).fit()
    keys = [[times[spans[0][0]], values[spans[0][0]]]]
    segs = []
    for i, j, c1, c2 in spans:
        t0, t1 = times[i], times[j]
        segs.append({"type": "bezier", "c1": [t0 + (t1 - t0) / 3, c1], "c2": [t1 - (t1 - t0) / 3, c2]})
        keys.append([t1, values[j]])
    return keys, segs


def fit(times: list[float], values: list[float], tol: float, interpolation: str, prefer: list[int] | None = None):
    if interpolation == "bezier":
        return fit_bezier(times, values, tol, prefer)
    if interpolation == "linear":
        return fit_linear(times, values, tol)
    raise ValueError(f"interpolation must be bezier or linear, not {interpolation!r}")


# ------------------------------------------------------------------------------- curve primitives

def _slope_start(k0, s: dict, k1) -> float:
    (t0, v0), (t1, v1) = k0, k1
    if s["type"] == "linear":
        return (v1 - v0) / (t1 - t0)
    if s["type"] == "bezier":
        dt = s["c1"][0] - t0
        return (s["c1"][1] - v0) / dt if dt > 1e-12 else 0.0
    return 0.0


def _slope_end(k0, s: dict, k1) -> float:
    (t0, v0), (t1, v1) = k0, k1
    if s["type"] == "linear":
        return (v1 - v0) / (t1 - t0)
    if s["type"] == "bezier":
        dt = t1 - s["c2"][0]
        return (v1 - s["c2"][1]) / dt if dt > 1e-12 else 0.0
    return 0.0


def _handles(k0, k1, m0: float, m1: float) -> dict:
    """A restricted Bezier segment with end slopes m0, m1."""
    (t0, v0), (t1, v1) = k0, k1
    h = t1 - t0
    return {"type": "bezier", "c1": [t0 + h / 3, v0 + m0 * h / 3], "c2": [t1 - h / 3, v1 - m1 * h / 3]}


def auto_slopes(keys: list) -> list[float]:
    """Slopes at the keys that never overshoot: 0 at a local extreme, otherwise a weighted harmonic
    mean of the neighbouring secants (Fritsch-Butland), which keeps each segment monotone."""
    n = len(keys)
    if n < 2:
        return [0.0] * n
    h = [keys[i + 1][0] - keys[i][0] for i in range(n - 1)]
    d = [(keys[i + 1][1] - keys[i][1]) / h[i] for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.0
        else:
            h0, h1 = h[i - 1], h[i]
            m[i] = 3 * (h0 + h1) / ((2 * h1 + h0) / d[i - 1] + (h1 + 2 * h0) / d[i])
    return m


def _split(curve: dict, t: float, restricted: bool) -> int:
    """Inserts a key at t (strictly inside the curve, not on a key) without changing the curve;
    returns the new key's index."""
    keys, segs = curve["keys"], curve["segments"]
    i = _find([k[0] for k in keys], t)
    (t0, v0), (t1, v1) = keys[i], keys[i + 1]
    s = segs[i]
    kind = s["type"]
    if kind == "bezier":
        if restricted:
            u = (t - t0) / (t1 - t0)
            pts_t = [t0, t0 + (t1 - t0) / 3, t1 - (t1 - t0) / 3, t1]
        else:
            u = bezier_parameter(t, t0, s["c1"][0], s["c2"][0], t1)
            pts_t = [t0, s["c1"][0], s["c2"][0], t1]
        pts_v = [v0, s["c1"][1], s["c2"][1], v1]

        def lerp(a, b):
            return a + (b - a) * u

        def casteljau(p):
            a, b, c = lerp(p[0], p[1]), lerp(p[1], p[2]), lerp(p[2], p[3])
            d, e = lerp(a, b), lerp(b, c)
            f = lerp(d, e)
            return [p[0], a, d, f], [f, e, c, p[3]]
        lt, rt = casteljau(pts_t)
        lv, rv = casteljau(pts_v)
        mid = [t, lv[3]]
        left = {"type": "bezier", "c1": [lt[1], lv[1]], "c2": [lt[2], lv[2]]}
        right = {"type": "bezier", "c1": [rt[1], rv[1]], "c2": [rt[2], rv[2]]}
        if restricted:  # exact: the halves of a cubic in time are cubics in time
            left["c1"][0], left["c2"][0] = t0 + (t - t0) / 3, t - (t - t0) / 3
            right["c1"][0], right["c2"][0] = t + (t1 - t) / 3, t1 - (t1 - t) / 3
    else:
        mid = [t, segment_value(keys[i], s, keys[i + 1], t, restricted)]
        left, right = {"type": kind}, {"type": kind}
    keys.insert(i + 1, mid)
    segs[i:i + 1] = [left, right]
    return i + 1


def _key_index(curve: dict, t, what: str = "key") -> int:
    if not _num(t):
        raise EditError(f"{what} time must be a number")
    best = min(range(len(curve["keys"])), key=lambda i: abs(curve["keys"][i][0] - t))
    if abs(curve["keys"][best][0] - t) > TIME_EPS:
        raise EditError(f"{curve['id']} has no key at t={t}")
    return best


def _restrict_segment(keys, i: int, s: dict) -> None:
    if s["type"] == "bezier":
        t0, t1 = keys[i][0], keys[i + 1][0]
        s["c1"][0], s["c2"][0] = t0 + (t1 - t0) / 3, t1 - (t1 - t0) / 3


# ------------------------------------------------------------------------------------- operations

def _curves(edit: dict, sel, field: str = "curves") -> list[dict]:
    by_id = {c["id"]: c for c in edit["curves"]}
    if sel == "*":
        return list(edit["curves"])
    if isinstance(sel, str):
        sel = [sel]
    if not isinstance(sel, list) or not sel or not all(isinstance(x, str) for x in sel):
        raise EditError(f'{field} must be a list of curve ids or "*"')
    missing = [x for x in sel if x not in by_id]
    if missing:
        raise EditError(f"unknown curve {', '.join(missing)}")
    return [by_id[x] for x in sel]


def _one(edit: dict, op: dict) -> dict:
    cid = op.get("curve")
    if not isinstance(cid, str):
        raise EditError("curve must be a curve id")
    return _curves(edit, [cid], "curve")[0]


def _window(op: dict) -> tuple[float, float]:
    lo, hi = op.get("from", -math.inf), op.get("to", math.inf)
    for name, x in (("from", lo), ("to", hi)):
        if x not in (-math.inf, math.inf) and not _num(x):
            raise EditError(f"{name} must be a number of seconds")
    if lo > hi:
        raise EditError("from must not be after to")
    return lo, hi


def _need(op: dict, name: str, check, what: str):
    if name not in op or not check(op[name]):
        raise EditError(f"{name} must be {what}")
    return op[name]


def _span(curve: dict, ranges: dict | None, restricted: bool, fps: float) -> float:
    r = _range(ranges, curve["id"]) if ranges else None
    if r is not None:
        return float(r[1] - r[0])
    vals = [k[1] for k in curve["keys"]]
    for i, s in enumerate(curve["segments"]):
        if s["type"] == "bezier":
            t0, t1 = curve["keys"][i][0], curve["keys"][i + 1][0]
            n = max(2, int((t1 - t0) * fps) + 2)
            vals += [segment_value(curve["keys"][i], s, curve["keys"][i + 1], t0 + (t1 - t0) * q / n, restricted)
                     for q in range(1, n)]
    span = max(vals) - min(vals)
    return span if span > 0 else 1.0


def refit_curve(curve: dict, tol: float, interpolation: str, fps: float, restricted: bool) -> float:
    """Replaces the keys of continuous stretches (stepped segments are kept) by a fitted curve
    sampled at fps plus the original key times. tol is absolute. Returns the largest deviation
    from the original at the samples."""
    keys, segs = curve["keys"], curve["segments"]
    if len(keys) < 2:
        return 0.0
    new_keys, new_segs = [keys[0][:]], []
    worst = 0.0
    a = 0
    while a < len(segs):
        if segs[a]["type"] in ("stepped", "inverse_stepped"):
            new_segs.append(dict(segs[a]))
            new_keys.append(keys[a + 1][:])
            a += 1
            continue
        b = a
        while b < len(segs) and segs[b]["type"] in ("linear", "bezier"):
            b += 1
        # Samples of segments a..b-1 (keys a..b): key times plus the frame grid in between.
        ts, vs, prefer = [], [], []
        for i in range(a, b):
            t0, t1 = keys[i][0], keys[i + 1][0]
            if i > a:  # an original key: spans are split there first
                prefer.append(len(ts))
            ts.append(t0)
            vs.append(keys[i][1])
            q = math.floor(t0 * fps) + 1
            while q / fps < t1 - 1e-9:
                tq = q / fps
                if tq > t0 + 1e-9:
                    ts.append(tq)
                    vs.append(segment_value(keys[i], segs[i], keys[i + 1], tq, restricted))
                q += 1
        ts.append(keys[b][0])
        vs.append(keys[b][1])
        fk, fs = fit(ts, vs, tol, interpolation, prefer)
        piece = {"keys": fk, "segments": fs}
        for t, v in zip(ts, vs):
            worst = max(worst, abs(value_at(piece, t) - v))
        new_keys += [k[:] for k in fk[1:]]
        new_segs += fs
        a = b
    curve["keys"], curve["segments"] = new_keys, new_segs
    if not restricted:
        for i, s in enumerate(new_segs):
            _restrict_segment(new_keys, i, s)
    return worst


def _op_set_interpolation(edit, op, ranges, restricted):
    kind = _need(op, "type", lambda x: x in TYPES, f"one of {', '.join(TYPES)}")
    lo, hi = _window(op)
    for c in _curves(edit, op.get("curves", "*")):
        keys, segs = c["keys"], c["segments"]
        slopes = None
        for i, s in enumerate(segs):
            t0, t1 = keys[i][0], keys[i + 1][0]
            hit = (t0 <= lo < t1) if lo == hi else (t0 < hi and t1 > lo)
            if not hit or s["type"] == kind:
                continue
            if kind == "bezier":
                slopes = slopes or auto_slopes(keys)
                segs[i] = _handles(keys[i], keys[i + 1], slopes[i], slopes[i + 1])
            else:
                segs[i] = {"type": kind}


def _op_insert_key(edit, op, ranges, restricted):
    c = _one(edit, op)
    t = _need(op, "t", _num, "a time in seconds")
    keys = c["keys"]
    if not keys[0][0] < t < keys[-1][0]:
        raise EditError(f"t={t} is outside {c['id']} ({keys[0][0]}..{keys[-1][0]}); keys are inserted between "
                        "the first and the last key")
    if any(abs(k[0] - t) <= TIME_EPS for k in keys):
        raise EditError(f"{c['id']} already has a key at t={t}")
    i = _split(c, t, restricted)
    if "value" in op:
        _set_value(c, i, _need(op, "value", _num, "a number"))


def _set_value(c: dict, i: int, value: float) -> None:
    """Moves key i to value, carrying its Bezier handles along (the tangents stay)."""
    delta = value - c["keys"][i][1]
    c["keys"][i][1] = value
    if i > 0 and c["segments"][i - 1]["type"] == "bezier":
        c["segments"][i - 1]["c2"][1] += delta
    if i < len(c["segments"]) and c["segments"][i]["type"] == "bezier":
        c["segments"][i]["c1"][1] += delta


def _merge(c: dict, a: int, b: int) -> None:
    """Removes the keys strictly between a and b; the joined segment keeps the type of segment a
    and, for Beziers, the slopes at both ends."""
    keys, segs = c["keys"], c["segments"]
    first, last = segs[a], segs[b - 1]
    kind = first["type"]
    if kind == "bezier":
        m0 = _slope_start(keys[a], first, keys[a + 1])
        m1 = _slope_end(keys[b - 1], last, keys[b])
        new = _handles(keys[a], keys[b], m0, m1)
    else:
        new = {"type": kind}
    del keys[a + 1:b]
    segs[a:b] = [new]


def _op_delete_keys(edit, op, ranges, restricted):
    c = _one(edit, op)
    keys = c["keys"]
    n = len(keys)
    if "times" in op:
        times = _need(op, "times", lambda x: isinstance(x, list) and x, "a non-empty list of key times")
        idx = {_key_index(c, t) for t in times}
        if 0 in idx or n - 1 in idx:
            raise EditError(f"the first and last key of {c['id']} cannot be deleted")
    elif "from" in op or "to" in op:
        lo, hi = _window(op)
        idx = {i for i in range(1, n - 1) if lo - TIME_EPS <= keys[i][0] <= hi + TIME_EPS}
    else:
        raise EditError("give times or from/to")
    for i in sorted(idx, reverse=True):
        _merge(c, i - 1, i + 1)


def _op_move_key(edit, op, ranges, restricted):
    c = _one(edit, op)
    i = _key_index(c, op.get("t"))
    keys, segs = c["keys"], c["segments"]
    if "to_t" not in op and "to_value" not in op:
        raise EditError("give to_t and/or to_value")
    if "to_t" in op:
        nt = _need(op, "to_t", _num, "a time in seconds")
        if abs(nt - keys[i][0]) > 1e-12:
            if i == 0 or i == len(keys) - 1:
                raise EditError(f"the first and last key of {c['id']} cannot move in time")
            if not keys[i - 1][0] < nt < keys[i + 1][0]:
                raise EditError(f"to_t={nt} is past a neighbouring key of {c['id']} "
                                f"({keys[i - 1][0]}..{keys[i + 1][0]}, exclusive)")
            old = keys[i][0]
            for j, (a, b) in ((i - 1, (keys[i - 1][0], old)), (i, (old, keys[i + 1][0]))):
                s = segs[j]
                if s["type"] == "bezier":  # control times follow their segment
                    na, nb = (a, nt) if j == i - 1 else (nt, b)
                    for h in ("c1", "c2"):
                        s[h][0] = na + (s[h][0] - a) * (nb - na) / (b - a)
            keys[i][0] = nt
    if "to_value" in op:
        _set_value(c, i, _need(op, "to_value", _num, "a number"))


def _op_set_handles(edit, op, ranges, restricted):
    c = _one(edit, op)
    i = _key_index(c, op.get("segment"), "segment start")
    if i >= len(c["segments"]) or c["segments"][i]["type"] != "bezier":
        raise EditError(f"the segment of {c['id']} starting at t={op.get('segment')} is not a bezier segment")
    if "c1v" not in op and "c2v" not in op:
        raise EditError("give c1v and/or c2v")
    s = c["segments"][i]
    _restrict_segment(c["keys"], i, s)
    if "c1v" in op:
        s["c1"][1] = _need(op, "c1v", _num, "a number")
    if "c2v" in op:
        s["c2"][1] = _need(op, "c2v", _num, "a number")


def _keys_in(c: dict, lo: float, hi: float) -> list[int]:
    return [i for i, k in enumerate(c["keys"]) if lo - TIME_EPS <= k[0] <= hi + TIME_EPS]


def _op_auto_tangents(edit, op, ranges, restricted, flat: bool = False):
    lo, hi = _window(op)
    for c in _curves(edit, op.get("curves", "*")):
        keys, segs = c["keys"], c["segments"]
        slopes = [0.0] * len(keys) if flat else auto_slopes(keys)
        for i in _keys_in(c, lo, hi):
            h_in = keys[i][0] - keys[i - 1][0] if i > 0 else 0.0
            h_out = keys[i + 1][0] - keys[i][0] if i < len(segs) else 0.0
            if i > 0 and segs[i - 1]["type"] == "bezier":
                _restrict_segment(keys, i - 1, segs[i - 1])
                segs[i - 1]["c2"][1] = keys[i][1] - slopes[i] * h_in / 3
            if i < len(segs) and segs[i]["type"] == "bezier":
                _restrict_segment(keys, i, segs[i])
                segs[i]["c1"][1] = keys[i][1] + slopes[i] * h_out / 3


def _op_flatten_tangents(edit, op, ranges, restricted):
    _op_auto_tangents(edit, op, ranges, restricted, flat=True)


def _op_refit(edit, op, ranges, restricted):
    tol = _need(op, "tolerance", lambda x: _num(x) and x > 0, "a positive fraction of the parameter range")
    kind = _need(op, "interpolation", lambda x: x in ("bezier", "linear"), "bezier or linear")
    fps = float(edit["meta"].get("fps") or 30.0)
    report = {}
    for c in _curves(edit, op.get("curves", "*")):
        before = len(c["keys"])
        span = _span(c, ranges, restricted, fps)
        err = refit_curve(c, tol * span, kind, fps, restricted)
        report[c["id"]] = {"keys_before": before, "keys_after": len(c["keys"]), "max_error": err,
                           "tolerance": tol * span}
    return report


def _affine(c: dict, f) -> None:
    for k in c["keys"]:
        k[1] = f(k[1])
    for s in c["segments"]:
        if s["type"] == "bezier":
            s["c1"][1], s["c2"][1] = f(s["c1"][1]), f(s["c2"][1])


def _op_scale_values(edit, op, ranges, restricted):
    factor = _need(op, "factor", _num, "a number")
    pivot = op.get("pivot", "zero")
    if not (_num(pivot) or pivot in ("zero", "first", "mean")):
        raise EditError('pivot must be "zero", "first", "mean" or a number')
    fps = float(edit["meta"].get("fps") or 30.0)
    for c in _curves(edit, op.get("curves", "*")):
        if pivot == "zero":
            p = 0.0
        elif pivot == "first":
            p = c["keys"][0][1]
        elif pivot == "mean":  # time average over the curve's keys span
            ts = frame_times(c["keys"][-1][0], fps) or [0.0]
            vals = [value_at(c, t, restricted) for t in ts]
            p = sum(vals) / len(vals)
        else:
            p = float(pivot)
        _affine(c, lambda v, p=p: p + factor * (v - p))


def _op_offset_values(edit, op, ranges, restricted):
    delta = _need(op, "delta", _num, "a number")
    for c in _curves(edit, op.get("curves", "*")):
        _affine(c, lambda v: v + delta)


def _op_clamp(edit, op, ranges, restricted):
    rng = _need(op, "ranges", lambda x: isinstance(x, dict) and x, "an object {id: [min, max]}")
    for cid, r in rng.items():
        if isinstance(r, dict):
            r = [r.get("min"), r.get("max")]
        if not _pair(r) or r[0] > r[1]:
            raise EditError(f"ranges.{cid} must be [min, max] with min <= max")
    for c in edit["curves"]:
        r = rng.get(c["id"])
        if r is not None:
            lo, hi = (r["min"], r["max"]) if isinstance(r, dict) else r
            _affine(c, lambda v, lo=lo, hi=hi: min(max(v, lo), hi))


def _op_retime(edit, op, ranges, restricted):
    f = _need(op, "factor", lambda x: _num(x) and x > 0, "a positive number")
    for c in edit["curves"]:
        for k in c["keys"]:
            k[0] *= f
        for s in c["segments"]:
            if s["type"] == "bezier":
                s["c1"][0] *= f
                s["c2"][0] *= f
    edit["meta"]["duration"] *= f
    for u in edit.get("passthrough", {}).get("user_data") or []:
        u["Time"] *= f


def _op_trim(edit, op, ranges, restricted):
    lo = _need(op, "from", lambda x: _num(x) and x >= 0, "a time >= 0")
    hi = _need(op, "to", _num, "a time in seconds")
    dur = edit["meta"]["duration"]
    if hi <= lo:
        raise EditError("to must be after from")
    if hi > dur + DURATION_SLACK:
        raise EditError(f"to={hi} is after the end of the motion ({dur})")
    for c in edit["curves"]:
        keys = c["keys"]
        if len(keys) > 1 and keys[0][0] < hi < keys[-1][0] and all(abs(k[0] - hi) > 1e-12 for k in keys):
            _split(c, hi, restricted)
        if len(keys) > 1 and keys[0][0] < lo < keys[-1][0] and all(abs(k[0] - lo) > 1e-12 for k in keys):
            _split(c, lo, restricted)
        if lo >= keys[-1][0]:  # the curve ended before the window: it holds its last value
            c["keys"], c["segments"] = [[0.0, keys[-1][1]]], []
            continue
        a = next(i for i, k in enumerate(keys) if k[0] >= lo - 1e-12)
        b = max(i for i, k in enumerate(keys) if k[0] <= hi + 1e-12)
        c["keys"] = [[k[0] - lo, k[1]] for k in keys[a:b + 1]]
        c["keys"][0][0] = 0.0
        c["segments"] = c["segments"][a:b]
        for s in c["segments"]:
            if s["type"] == "bezier":
                s["c1"][0] -= lo
                s["c2"][0] -= lo
    edit["meta"]["duration"] = hi - lo
    pas = edit.setdefault("passthrough", {})
    pas["user_data"] = [dict(u, Time=u["Time"] - lo) for u in pas.get("user_data") or [] if lo <= u["Time"] <= hi]


def _op_set_meta(edit, op, ranges, restricted):
    meta = edit["meta"]
    known = {"loop", "fade_in", "fade_out", "fps", "duration"}
    unknown = set(op) - known - {"op"}
    if unknown:
        raise EditError(f"unknown field {', '.join(sorted(unknown))}; set_meta takes {', '.join(sorted(known))}")
    if "loop" in op:
        meta["loop"] = _need(op, "loop", lambda x: isinstance(x, bool), "true or false")
    for k in ("fade_in", "fade_out"):
        if k in op:
            meta[k] = _need(op, k, lambda x: _num(x) and x >= 0, "a number of seconds >= 0")
    if "fps" in op:
        meta["fps"] = _need(op, "fps", lambda x: _num(x) and x > 0, "a positive number")
    if "duration" in op:
        d = _need(op, "duration", lambda x: _num(x) and x > 0, "a positive number of seconds")
        last = max((c["keys"][-1][0] for c in edit["curves"]), default=0.0)
        if d < last - DURATION_SLACK:
            raise EditError(f"duration={d} is before the last key ({last}); trim instead")
        meta["duration"] = d


def _op_delete_curve(edit, op, ranges, restricted):
    c = _one(edit, op)
    edit["curves"] = [x for x in edit["curves"] if x is not c]


OPS = {
    "set_interpolation": _op_set_interpolation, "insert_key": _op_insert_key, "delete_keys": _op_delete_keys,
    "move_key": _op_move_key, "set_handles": _op_set_handles, "auto_tangents": _op_auto_tangents,
    "flatten_tangents": _op_flatten_tangents, "refit": _op_refit, "scale_values": _op_scale_values,
    "offset_values": _op_offset_values, "clamp": _op_clamp, "retime": _op_retime, "trim": _op_trim,
    "set_meta": _op_set_meta, "delete_curve": _op_delete_curve,
}


def apply(motion: dict, operations: list, ranges: dict | None = None) -> tuple[dict, list]:
    """Applies operations in order to a copy of motion (motion3 or editable). Returns (editable
    result, per-operation reports: the refit report or None). Raises EditError (with .index) on the
    first invalid operation, and MotionError when the result would be invalid."""
    if isinstance(operations, dict) and "operations" in operations:
        operations = operations["operations"]
    if not isinstance(operations, list):
        raise EditError('operations must be a list (or {"operations": [...]})')
    edit = as_editable(motion)
    problems = _validate_editable(edit, None)
    if problems:
        raise MotionError(problems)
    restricted = bool(edit["meta"].get("restricted", True))
    reports = []
    for n, op in enumerate(operations):
        name = op.get("op") if isinstance(op, dict) else None
        if name not in OPS:
            raise EditError(f"unknown op {name!r}; one of {', '.join(OPS)}", n, str(name))
        try:
            reports.append(OPS[name](edit, op, ranges, restricted))
        except EditError as e:
            raise EditError(str(e), n, name) from None
    if restricted:
        for c in edit["curves"]:
            _thirds(c)
    problems = _validate_editable(edit, None)
    if problems:
        raise MotionError(problems)
    return edit, reports


# ------------------------------------------------------------------------------------------- files

def read_json(path: str):
    if path == "-":
        return json.loads(sys.stdin.read())
    p = pathlib.Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"file not found: {p}")
    return json.loads(p.read_text(encoding="utf-8-sig"))


def load(path: str, curves: list[str] | None = None) -> dict:
    """Editable motion from a motion3.json or editable JSON file (or "-" for stdin)."""
    return as_editable(read_json(path), curves)


def load_ranges(path: str | None) -> dict | None:
    if not path:
        return None
    data = read_json(path)
    if not isinstance(data, dict) or not all(_pair(r) or (isinstance(r, dict) and _num(r.get("min"))
                                                         and _num(r.get("max"))) for r in data.values()):
        raise ValueError("ranges file must map parameter ids to [min, max] or {min, max}")
    return {k: (r if isinstance(r, dict) else {"min": r[0], "max": r[1]}) for k, r in data.items()}


def save(edit: dict, path: str | pathlib.Path, overwrite: bool = False, ranges: dict | None = None) -> pathlib.Path:
    """Writes the editable motion as a motion3.json (validated; never replaces a file unless
    overwrite)."""
    m = to_motion3(edit, ranges)
    p = pathlib.Path(path).expanduser().resolve()
    if p.exists() and not overwrite:
        raise FileExistsError(f"{p} already exists; pass --overwrite to replace it")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1))
    tmp.replace(p)
    return p


def info(edit: dict) -> dict:
    curves = []
    restricted = bool(edit["meta"].get("restricted", True))
    for c in edit["curves"]:
        types = {t: 0 for t in TYPES}
        for s in c["segments"]:
            types[s["type"]] += 1
        vals = [k[1] for k in c["keys"]]
        curves.append({"target": c.get("target", "Parameter"), "id": c["id"], "keys": len(c["keys"]),
                       "types": types, "min": min(vals), "max": max(vals)})
    return {"meta": edit["meta"], "counts": counts(edit), "curves": curves, "restricted": restricted}


# --------------------------------------------------------------------------------------------- CLI

class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Usage(f"{self.prog}: {message}")


def _ids(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def parser(prog: str = "python -m v2l.motionedit") -> argparse.ArgumentParser:
    ap = _Parser(prog=prog, description="read, edit, refit, evaluate and validate Live2D motion3.json files")
    sub = ap.add_subparsers(dest="action", required=True)

    def cmd(name, help_, out=False):
        p = sub.add_parser(name, help=help_)
        p.add_argument("file", help=".motion3.json or editable JSON (v2l.motion-edit); - reads stdin")
        p.add_argument("--json", action="store_true", help="print one JSON result object on stdout")
        if out:
            p.add_argument("--out", required=True, help="motion3.json to write; - writes nothing and returns the "
                                                        "editable result as \"motion\" in the JSON response")
            p.add_argument("--overwrite", action="store_true", help="replace an existing --out file")
        return p

    cmd("info", "curves, segment types and counts")
    p = cmd("read", "the editable JSON (v2l.motion-edit)")
    p.add_argument("--curves", help="comma-separated curve ids (default: all)")
    cmd("write", "editable JSON -> motion3.json (validated)", out=True)
    p = cmd("edit", "apply an operation list (docs/motion-editing.md section 4)", out=True)
    p.add_argument("--ops", required=True, help="JSON file with a list of operations or {\"operations\": [...]}; "
                                                "- reads stdin")
    p.add_argument("--ranges", help="JSON {id: [min, max]}: refit tolerances relative to these ranges")
    p = cmd("refit", "replace keys by a fitted curve", out=True)
    p.add_argument("--interpolation", required=True, choices=("bezier", "linear"))
    p.add_argument("--tolerance", required=True, type=float, help="largest deviation as a fraction of the range")
    p.add_argument("--curves", help="comma-separated curve ids (default: all)")
    p.add_argument("--ranges", help="JSON {id: [min, max]} (default: each curve's own value span)")
    p = cmd("eval", "curve values at given times")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--times", help="comma-separated times in seconds")
    g.add_argument("--fps", type=float, help="every frame from 0 to the duration")
    p.add_argument("--curves", help="comma-separated curve ids (default: all)")
    p = cmd("validate", "check the motion (counts, times, segment layout, optional parameter ranges)")
    p.add_argument("--ranges", help="JSON {id: [min, max]}")
    return ap


def _emit(edit: dict, a, result: dict) -> dict:
    """Writes or returns the result motion; result gets out/counts (and motion with --out -)."""
    if a.out == "-":
        to_motion3(edit)  # validation only
        result.update(out=None, counts=counts(edit), motion=edit)
    else:
        result.update(out=str(save(edit, a.out, a.overwrite)), counts=counts(edit))
    return result


def run(a) -> dict:
    stdin_inputs = [x for x in (a.file, getattr(a, "ops", None)) if x == "-"]
    if len(stdin_inputs) > 1:
        raise _Usage("only one of FILE and --ops can be read from stdin")
    if a.action == "info":
        return {"ok": True, "file": a.file, **info(load(a.file)),
                "problems": validate(read_json(a.file)) if a.file != "-" else []}
    if a.action == "read":
        return {"ok": True, "motion": load(a.file, _ids(a.curves))}
    if a.action == "write":
        data = read_json(a.file)
        return _emit(data if is_editable(data) else as_editable(data), a, {"ok": True})
    if a.action == "edit":
        edit, reports = apply(load(a.file), read_json(a.ops), load_ranges(a.ranges))
        res = {"ok": True, "applied": len(reports)}
        refits = {k: v for r in reports if r for k, v in r.items()}
        if refits:
            res["curves"] = refits
        return _emit(edit, a, res)
    if a.action == "refit":
        if not a.tolerance > 0:
            raise _Usage("--tolerance must be positive")
        op = {"op": "refit", "curves": _ids(a.curves) or "*", "tolerance": a.tolerance,
              "interpolation": a.interpolation}
        edit, reports = apply(load(a.file), [op], load_ranges(a.ranges))
        return _emit(edit, a, {"ok": True, "curves": reports[0]})
    if a.action == "eval":
        edit = load(a.file)
        if a.times:
            try:
                times = [float(x) for x in a.times.split(",") if x.strip()]
            except ValueError:
                raise _Usage("--times must be comma-separated numbers") from None
        else:
            if not a.fps > 0:
                raise _Usage("--fps must be positive")
            times = frame_times(edit["meta"]["duration"], a.fps)
        ids = _ids(a.curves)
        if ids:
            missing = sorted(set(ids) - {c["id"] for c in edit["curves"]})
            if missing:
                raise ValueError(f"no curve {', '.join(missing)} in the motion")
        return {"ok": True, "times": times, "values": evaluate(edit, times, ids)}
    problems = validate(read_json(a.file), load_ranges(a.ranges))
    if problems:
        return {"ok": False, "valid": False, "problems": problems,
                "error": {"type": "invalid", "message": f"{len(problems)} problem(s): " + "; ".join(problems[:5]),
                          "problems": problems}}
    return {"ok": True, "valid": True, "problems": []}


def _error(e: BaseException) -> dict:
    if isinstance(e, _Usage):
        return {"type": "usage", "message": str(e)}
    if isinstance(e, EditError):
        err = {"type": "invalid", "message": str(e)}
        if e.index is not None:
            err["op_index"] = e.index
        return err
    if isinstance(e, MotionError):
        return {"type": "invalid", "message": str(e), "problems": e.problems}
    if isinstance(e, json.JSONDecodeError):
        return {"type": "input", "message": f"not valid JSON: {e}"}
    if isinstance(e, (FileNotFoundError, FileExistsError, NotADirectoryError, IsADirectoryError, ValueError)):
        return {"type": "input", "message": str(e)}
    return {"type": "runtime", "message": f"{type(e).__name__}: {e}"}


def main(argv: list[str] | None = None, prog: str = "python -m v2l.motionedit") -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    try:
        a = parser(prog).parse_args(argv)
        res = run(a)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - reported in the chosen format
        if isinstance(e, KeyboardInterrupt):
            err, code = {"type": "interrupted", "message": "interrupted"}, 130
        else:
            err = _error(e)
            code = 1 if err["type"] == "runtime" else 2
        if as_json:
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False), flush=True)
        else:
            print(f"{prog}: {err['message']}", file=sys.stderr)
        return code
    code = 0 if res.get("ok") else 2
    if as_json:
        print(json.dumps(res, ensure_ascii=False), flush=True)
        return code
    if a.action == "read":
        print(json.dumps(res["motion"], ensure_ascii=False, indent=1))
    elif a.action == "validate":
        print("valid" if res["ok"] else "\n".join(res["problems"]), file=sys.stdout if res["ok"] else sys.stderr)
    elif a.action == "eval":
        ids = list(res["values"])
        print("\t".join(["t", *ids]))
        for n, t in enumerate(res["times"]):
            print("\t".join([f"{t:.4f}", *(f"{res['values'][i][n]:.6g}" for i in ids)]))
    elif a.action == "info":
        m, n = res["meta"], res["counts"]
        print(f"{m['duration']} s at {m['fps']} fps, loop={m['loop']}, restricted={m['restricted']}: "
              f"{n['curves']} curves, {n['segments']} segments, {n['points']} points")
        for c in res["curves"]:
            types = ", ".join(f"{k}={v}" for k, v in c["types"].items() if v)
            print(f"  {c['id']:28s} {c['keys']:4d} keys  {types}")
        for p in res["problems"]:
            print(f"  problem: {p}")
    else:
        motion = res.pop("motion", None)
        if motion is not None:
            print(json.dumps(motion, ensure_ascii=False, indent=1))
        else:
            print(f"wrote {res['out']} ({res['counts']['curves']} curves, {res['counts']['points']} points)")
    return code


if __name__ == "__main__":
    sys.exit(main())
