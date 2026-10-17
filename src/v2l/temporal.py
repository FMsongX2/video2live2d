"""Time handling: resampling to the output grid, tracking gaps, zero-phase filtering, looping."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, filtfilt, medfilt


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index runs where mask is True."""
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def resample(x: dict[str, np.ndarray], t_src: np.ndarray, fps: float, max_gap: float = 0.4,
             ramp: float = 0.3) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """Puts every rest-centred signal on a uniform grid and handles tracking loss.

    Gaps up to max_gap seconds are bridged by linear interpolation. Longer gaps fade to the rest
    value 0 over `ramp` seconds on each side, so a performer leaving the frame relaxes the avatar
    instead of freezing it. Returns (signals on the grid, grid times, presence weight 0..1).
    """
    duration = float(t_src[-1] - t_src[0]) if len(t_src) > 1 else 0.0
    n = max(int(round(duration * fps)) + 1, 1)
    t = t_src[0] + np.arange(n) / fps
    ok_src = x["face_ok"] > 0

    # Presence on the grid: 0 inside long gaps, ramps at their edges.
    present = np.ones(n)
    lost = np.interp(t, t_src, (~ok_src).astype(float)) > 0.5
    for a, b in _runs(lost):
        if (b - a) / fps > max_gap:
            present[a:b] = 0.0
    k = max(int(round(ramp * fps)), 1)
    kernel = np.ones(2 * k + 1) / (2 * k + 1)
    present = np.clip(np.convolve(np.pad(present, k, mode="edge"), kernel, mode="valid"), 0.0, 1.0)

    out: dict[str, np.ndarray] = {}
    for name, v in x.items():
        if name in ("face_ok", "pose_ok"):
            continue
        good = np.isfinite(v)
        if not good.any():
            out[name] = np.zeros(n)
            continue
        g = np.interp(t, t_src[good], v[good])
        out[name] = g * present if not name.startswith("shoulder") else g
    # Shoulders follow pose presence separately; without pose the body follows the head only.
    pose_present = np.interp(t, t_src, (x["pose_ok"] > 0).astype(float))
    for name in ("shoulderRoll", "shoulderYaw", "shoulderY"):
        if name in out:
            out[name] = out[name] * pose_present
    return out, t - t[0], present


def lowpass(v: np.ndarray, cutoff: float, fps: float, order: int = 2) -> np.ndarray:
    """Zero-phase Butterworth low-pass (forward-backward), no lag."""
    if cutoff >= fps / 2 or len(v) < 3 * (order + 1) + 1:
        return v
    b, a = butter(order, cutoff / (fps / 2))
    return filtfilt(b, a, v, padlen=min(3 * max(len(a), len(b)), len(v) - 1))


def denoise(v: np.ndarray, cutoff: float, fps: float, despike: bool = True) -> np.ndarray:
    """3-sample median against single-frame tracker glitches (with despike), then zero-phase low-pass."""
    if despike and len(v) >= 3:
        v = medfilt(v, 3)
    return lowpass(v, cutoff, fps)


def make_loop(v: np.ndarray, fps: float, blend: float = 0.5) -> np.ndarray:
    """Eases the last `blend` seconds into the first value so playback loops without a jump."""
    n = len(v)
    k = min(int(round(blend * fps)), n - 1)
    if k < 2:
        return v
    s = np.linspace(0.0, 1.0, k)
    s = s * s * (3 - 2 * s)
    out = v.copy()
    out[n - k:] = (1 - s) * v[n - k:] + s * v[0]
    return out
