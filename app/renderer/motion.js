// Cubism .motion3.json evaluation at arbitrary times, so playback can follow a <video> clock
// (scrubbing, pausing) instead of the runtime's own timer.
//
// Segment types: 0 linear, 1 bezier, 2 stepped, 3 inverse stepped. Bezier x(u) is inverted by
// bisection, which is exact for both restricted and unrestricted control points. At a key time
// the segment that starts there applies (as in the Cubism Framework and `v2l.motionedit eval`),
// so an inverse-stepped segment already gives its end value at its start time.

export function parseMotion(json) {
  const meta = json.Meta || {};
  const curves = [];
  for (const c of json.Curves || []) {
    const s = c.Segments;
    const segs = [];
    let p0 = [s[0], s[1]];
    let i = 2;
    while (i < s.length) {
      const type = s[i];
      if (type === 1) {
        const seg = { type, p0, c1: [s[i + 1], s[i + 2]], c2: [s[i + 3], s[i + 4]], p1: [s[i + 5], s[i + 6]] };
        segs.push(seg);
        p0 = seg.p1;
        i += 7;
      } else {
        const seg = { type, p0, p1: [s[i + 1], s[i + 2]] };
        segs.push(seg);
        p0 = seg.p1;
        i += 3;
      }
    }
    curves.push({ target: c.Target, id: c.Id, first: [s[0], s[1]], segs });
  }
  return { duration: meta.Duration || 0, fps: meta.Fps || 30, loop: !!meta.Loop, curves };
}

function bezier(seg, t) {
  const x = (u) => {
    const v = 1 - u;
    return v * v * v * seg.p0[0] + 3 * v * v * u * seg.c1[0] + 3 * v * u * u * seg.c2[0] + u * u * u * seg.p1[0];
  };
  let lo = 0, hi = 1;
  for (let k = 0; k < 30; k++) {
    const mid = (lo + hi) / 2;
    if (x(mid) < t) lo = mid; else hi = mid;
  }
  const u = (lo + hi) / 2, v = 1 - u;
  return v * v * v * seg.p0[1] + 3 * v * v * u * seg.c1[1] + 3 * v * u * u * seg.c2[1] + u * u * u * seg.p1[1];
}

export function valueAt(curve, t) {
  const segs = curve.segs;
  if (!segs.length || t < curve.first[0]) return curve.first[1];
  let lo = 0, hi = segs.length - 1;
  if (t >= segs[hi].p1[0]) return segs[hi].p1[1];
  // Binary search for the segment with p0 <= t < p1.
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid].p1[0] <= t) lo = mid + 1; else hi = mid;
  }
  const seg = segs[lo];
  switch (seg.type) {
    case 0: {
      const k = (t - seg.p0[0]) / (seg.p1[0] - seg.p0[0] || 1);
      return seg.p0[1] + (seg.p1[1] - seg.p0[1]) * k;
    }
    case 1: return bezier(seg, t);
    case 2: return seg.p0[1];
    case 3: return seg.p1[1];
    default: return seg.p0[1];
  }
}
