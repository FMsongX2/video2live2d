// Editable motion curves (the `v2l.motion-edit` format, docs/motion-editing.md) for the
// interactive editor: evaluation, drawing geometry, conversion to motion3 for the live preview,
// and the direct-manipulation edits (insert, delete, move, handles). Operations with a defined
// result in the shared vocabulary (interpolation, tangents, refit, scale, trim, ...) run in
// Python (`v2l.motionedit`), which is the source of truth for reading and writing files.
//
// A segment between keys (t0, v0) and (t1, v1):
//   linear           straight line
//   bezier           cubic Bezier through (t0, v0), c1, c2, (t1, v1)
//   stepped          holds v0, jumps to v1 at t1
//   inverse_stepped  jumps to v1 right after t0
// With meta.restricted the control times are t0 + dt/3 and t1 - dt/3, so the value is a cubic
// polynomial in time; the editor changes only control values.

export const TYPES = ["linear", "bezier", "stepped", "inverse_stepped"];
const TYPE_CODE = { linear: 0, bezier: 1, stepped: 2, inverse_stepped: 3 };
const EPS = 1e-6;

export const clone = (v) => JSON.parse(JSON.stringify(v));
export const clampTo = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// Control times of a restricted segment.
export function thirds(t0, t1) { const d = (t1 - t0) / 3; return [t0 + d, t1 - d]; }

// Control points of segment i as [[t, v], [t, v]] (restricted times when the motion is).
export function controls(doc, curve, i) {
  const seg = curve.segments[i];
  const [t0] = curve.keys[i], [t1] = curve.keys[i + 1];
  if (doc.meta.restricted || !seg.c1 || !seg.c2) {
    const [a, b] = thirds(t0, t1);
    return [[a, seg.c1 ? seg.c1[1] : curve.keys[i][1]], [b, seg.c2 ? seg.c2[1] : curve.keys[i + 1][1]]];
  }
  return [seg.c1, seg.c2];
}

function cubic(a, b, c, d, u) {
  const w = 1 - u;
  return w * w * w * a + 3 * w * w * u * b + 3 * w * u * u * c + u * u * u * d;
}

// Curve parameter u at time t on a Bezier segment (bisection: x(u) is monotone for valid files).
function bezierU(p0, c1, c2, p1, t) {
  let lo = 0, hi = 1;
  for (let k = 0; k < 40; k++) {
    const mid = (lo + hi) / 2;
    if (cubic(p0[0], c1[0], c2[0], p1[0], mid) < t) lo = mid; else hi = mid;
  }
  return (lo + hi) / 2;
}

export function segmentValue(doc, curve, i, t) {
  const p0 = curve.keys[i], p1 = curve.keys[i + 1], seg = curve.segments[i];
  switch (seg.type) {
    case "linear": return p0[1] + (p1[1] - p0[1]) * ((t - p0[0]) / ((p1[0] - p0[0]) || 1));
    case "stepped": return t >= p1[0] ? p1[1] : p0[1];
    case "inverse_stepped": return t >= p0[0] ? p1[1] : p0[1];
    case "bezier": {
      const [c1, c2] = controls(doc, curve, i);
      const u = doc.meta.restricted || !seg.c1 ? (t - p0[0]) / ((p1[0] - p0[0]) || 1) : bezierU(p0, c1, c2, p1, t);
      return cubic(p0[1], c1[1], c2[1], p1[1], u);
    }
    default: return p0[1];
  }
}

// Index of the segment with keys[i] <= t < keys[i + 1] (clamped to the curve). At a key time the
// segment that starts there applies, as in the Cubism Framework and `v2l.motionedit eval`.
export function segmentAt(curve, t) {
  const k = curve.keys;
  let lo = 0, hi = k.length - 2;
  if (hi < 0) return -1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (k[mid + 1][0] <= t) lo = mid + 1; else hi = mid;
  }
  return lo;
}

export function valueAt(doc, curve, t) {
  const k = curve.keys;
  if (!k.length) return 0;
  if (k.length === 1 || t < k[0][0]) return k[0][1];
  if (t >= k[k.length - 1][0]) return k[k.length - 1][1];
  return segmentValue(doc, curve, segmentAt(curve, t), t);
}

// ---------------------------------------------------------------- motion3 for the preview

// Motion3 JSON with the same curves (Meta counts are recomputed by Python on save).
export function toMotion3(doc) {
  const curves = doc.curves.map((c) => {
    const s = [c.keys[0][0], c.keys[0][1]];
    c.segments.forEach((seg, i) => {
      const [t1, v1] = c.keys[i + 1];
      s.push(TYPE_CODE[seg.type] ?? 0);
      if (seg.type === "bezier") { const [c1, c2] = controls(doc, c, i); s.push(c1[0], c1[1], c2[0], c2[1]); }
      s.push(t1, v1);
    });
    return { Target: c.target, Id: c.id, Segments: s };
  });
  const m = doc.meta;
  return { Version: 3, Meta: { Duration: m.duration, Fps: m.fps, Loop: !!m.loop, AreBeziersRestricted: !!m.restricted,
                               FadeInTime: m.fade_in, FadeOutTime: m.fade_out }, Curves: curves };
}

// ---------------------------------------------------------------- direct edits (mutate curve)

// Keeps a bezier segment restricted after its keys move, scaling handle offsets so the
// tangents (value per second) stay the same.
function rescaleHandles(doc, curve, i, oldDt) {
  const seg = curve.segments[i];
  if (seg?.type !== "bezier") return;
  const [t0, v0] = curve.keys[i], [t1, v1] = curve.keys[i + 1];
  const k = oldDt > EPS ? (t1 - t0) / oldDt : 1;
  const [a, b] = thirds(t0, t1);
  seg.c1 = [a, v0 + (seg.c1[1] - seg._v0) * k];
  seg.c2 = [b, v1 + (seg.c2[1] - seg._v1) * k];
  delete seg._v0; delete seg._v1;
  if (!doc.meta.restricted) { /* an edited segment becomes restricted-shaped */ }
}

// Moves keys (indices, ascending) by dt/dv with neighbours and range respected. Returns the
// applied [dt, dv]. Handles of neighbouring bezier segments follow (same slopes).
export function moveKeys(doc, curve, indices, dt, dv, range, frame = 0) {
  const keys = curve.keys, sel = new Set(indices), last = keys.length - 1;
  const gap = Math.max(frame, 1e-3);
  // Time: every selected key stays strictly between its nearest unselected neighbours.
  let lo = -Infinity, hi = Infinity;
  for (const i of indices) {
    let a = i - 1; while (a >= 0 && sel.has(a)) a--;
    let b = i + 1; while (b <= last && sel.has(b)) b++;
    const tMin = a >= 0 ? keys[a][0] + gap : 0;
    const tMax = b <= last ? keys[b][0] - gap : doc.meta.duration;
    lo = Math.max(lo, tMin - keys[i][0]);
    hi = Math.min(hi, tMax - keys[i][0]);
  }
  dt = lo > hi ? 0 : clampTo(dt, lo, hi);
  const touched = new Set();
  for (const i of indices) { if (i > 0) touched.add(i - 1); if (i < last) touched.add(i); }
  const old = new Map();
  for (const s of touched) {
    const seg = curve.segments[s];
    old.set(s, keys[s + 1][0] - keys[s][0]);
    if (seg.type === "bezier") {
      const [c1, c2] = controls(doc, curve, s);
      seg.c1 = c1; seg.c2 = c2; seg._v0 = keys[s][1]; seg._v1 = keys[s + 1][1];
    }
  }
  for (const i of indices) {
    keys[i][0] = keys[i][0] + dt;
    keys[i][1] = range ? clampTo(keys[i][1] + dv, range[0], range[1]) : keys[i][1] + dv;
  }
  for (const s of touched) rescaleHandles(doc, curve, s, old.get(s));
  return [dt, dv];
}

// Sets one bezier control value (c = 1 or 2) of segment i.
export function setHandle(doc, curve, i, c, value, range) {
  const seg = curve.segments[i];
  if (seg.type !== "bezier") return;
  const [c1, c2] = controls(doc, curve, i);
  const v = range ? clampTo(value, range[0], range[1]) : value;
  seg.c1 = c === 1 ? [c1[0], v] : c1;
  seg.c2 = c === 2 ? [c2[0], v] : c2;
}

// Inserts a key at t; without a value the split keeps the curve's shape. Returns its index or -1.
export function insertKey(doc, curve, t, value) {
  const keys = curve.keys;
  if (keys.some(([kt]) => Math.abs(kt - t) < 1e-4)) return -1;
  if (t < 0 || t > doc.meta.duration + EPS) return -1;
  if (t < keys[0][0]) {
    keys.unshift([t, value ?? keys[0][1]]);
    curve.segments.unshift({ type: "linear" });
    return 0;
  }
  if (t > keys[keys.length - 1][0]) {
    keys.push([t, value ?? keys[keys.length - 1][1]]);
    curve.segments.push({ type: "linear" });
    return keys.length - 1;
  }
  const i = segmentAt(curve, t);
  const seg = curve.segments[i];
  const p0 = keys[i], p1 = keys[i + 1];
  const v = segmentValue(doc, curve, i, t);
  let left = { type: seg.type }, right = { type: seg.type };
  if (seg.type === "bezier") {
    const [c1, c2] = controls(doc, curve, i);
    const restricted = doc.meta.restricted || !seg.c1;
    const u = restricted ? (t - p0[0]) / (p1[0] - p0[0]) : bezierU(p0, c1, c2, p1, t);
    const lerp = (a, b) => [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];
    const ab = lerp(p0, c1), bc = lerp(c1, c2), cd = lerp(c2, p1);
    const abc = lerp(ab, bc), bcd = lerp(bc, cd);
    left = { type: "bezier", c1: ab, c2: abc };
    right = { type: "bezier", c1: bcd, c2: cd };
  } else if (seg.type === "stepped") {
    left = { type: "linear" };          // flat at v0 up to t
  } else if (seg.type === "inverse_stepped") {
    right = { type: "linear" };         // flat at v1 from t
  }
  keys.splice(i + 1, 0, [t, v]);
  curve.segments.splice(i, 1, left, right);
  if (value != null && Math.abs(value - v) > EPS) moveKeys(doc, curve, [i + 1], 0, value - v, null);
  return i + 1;
}

// Deletes keys (not the first or last). The merged segment takes the type of the segment
// that started at the previous kept key; a merged bezier keeps its outer handles' slopes.
export function deleteKeys(doc, curve, indices) {
  const drop = [...new Set(indices)].filter((i) => i > 0 && i < curve.keys.length - 1).sort((a, b) => b - a);
  for (const i of drop) {
    const before = curve.segments[i - 1], after = curve.segments[i];
    const [t0, v0] = curve.keys[i - 1], [t1, v1] = curve.keys[i + 1];
    let merged = { type: before.type };
    if (before.type === "bezier") {
      const [a] = controls(doc, curve, i - 1);
      const slope0 = (a[1] - v0) / Math.max(a[0] - t0, EPS);
      let slope1 = 0;
      if (after.type === "bezier") { const [, d] = controls(doc, curve, i); slope1 = (v1 - d[1]) / Math.max(curve.keys[i + 1][0] - d[0], EPS); }
      else if (after.type === "linear") slope1 = (v1 - curve.keys[i][1]) / Math.max(t1 - curve.keys[i][0], EPS);
      const [ct1, ct2] = thirds(t0, t1);
      merged = { type: "bezier", c1: [ct1, v0 + slope0 * (ct1 - t0)], c2: [ct2, v1 - slope1 * (t1 - ct2)] };
    }
    curve.keys.splice(i, 1);
    curve.segments.splice(i - 1, 2, merged);
  }
  return drop.length;
}

// Value range of a curve's keys and handles.
export function extent(doc, curve) {
  let lo = Infinity, hi = -Infinity;
  for (const [, v] of curve.keys) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  curve.segments.forEach((seg, i) => {
    if (seg.type !== "bezier") return;
    for (const [, v] of controls(doc, curve, i)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  });
  return [lo, hi];
}

// Indices of segments that start at or end at one of the given keys (the selection's
// segments): a key's outgoing segment, and the incoming one for the curve's last key.
export function segmentsOf(curve, indices) {
  const out = new Set();
  const last = curve.keys.length - 1;
  const sel = new Set(indices);
  for (const i of indices) {
    if (i < last) out.add(i);
    else if (i === last && last > 0 && !sel.has(last - 1)) out.add(last - 1);
  }
  return [...out].sort((a, b) => a - b);
}

export function counts(doc) {
  let keys = 0, segments = 0;
  for (const c of doc.curves) { keys += c.keys.length; segments += c.segments.length; }
  return { curves: doc.curves.length, keys, segments };
}
