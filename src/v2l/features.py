"""Raw tracker output -> named per-frame features in image space.

Conventions (image space, as a viewer sees the performer):
  yaw   > 0  nose moves to the image right
  pitch > 0  head tilts up
  roll  > 0  head top moves to the image right (clockwise on screen)
Side suffix A = image-left, B = image-right. Missing frames stay NaN; temporal.py fills them.

mouthShift is face-local: positive is image-right in an upright neutral pose. It is measured
along the fitted head's lateral axis, not screen X, so rigid head rotation is not lip motion.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from .signals import Signals

EYE_A = dict(outer=33, inner=133, top=(160, 158), bottom=(144, 153), lid_top=159, lid_bottom=145)
EYE_B = dict(outer=263, inner=362, top=(387, 385), bottom=(373, 380), lid_top=386, lid_bottom=374)
IRIS = (468, 473)
LIP_UP, LIP_LO, MOUTH_A, MOUTH_B, SUBNASALE = 13, 14, 61, 291, 2
FOREHEAD, CHIN = 10, 152
SHOULDER_L, SHOULDER_R = 11, 12  # pose landmarks: the subject's left / right


def features(s: Signals, rare_scores: dict[str, np.ndarray] | None = None) -> dict[str, np.ndarray]:
    """rare_scores: raw zero-shot scores (rare.scores), scored on every n-th frame; they are
    interpolated over the frames in between (only inside stretches where the face is tracked)."""
    h, w = s.size
    n = len(s.t)
    ok = s.face_ok
    f: dict[str, np.ndarray] = {}
    for key, raw in (rare_scores or {}).items():
        good = np.isfinite(raw)
        v = np.full(n, np.nan)
        if good.any():
            v[ok] = np.interp(np.flatnonzero(ok), np.flatnonzero(good), raw[good])
        f[f"rare:{key}"] = v

    yaw = np.full(n, np.nan)
    pitch = np.full(n, np.nan)
    roll = np.full(n, np.nan)
    face_x = np.full((n, 3), np.nan)
    if ok.any():
        # Euler YXZ of MediaPipe's canonical-face -> camera rotation. Measured on footage:
        # +Y yaw moves the nose to the image right, +X pitch looks down, +Z roll lifts the
        # image-right eye (counter-clockwise on screen).
        rotation = Rotation.from_matrix(s.head_mat[ok, :3, :3])
        y_, p_, r_ = rotation.as_euler("YXZ", degrees=True).T
        yaw[ok], pitch[ok], roll[ok] = y_, -p_, -r_
        # The fitted pose uses camera coordinates (Y up, Z toward the viewer); landmark
        # coordinates use image Y down and depth away from the viewer.
        face_x[ok] = rotation.apply([1.0, 0.0, 0.0]) * np.array([1.0, -1.0, -1.0])
    f["yaw"], f["pitch"], f["roll"] = yaw, pitch, roll

    lm2 = s.face_lm[:, :, :2] * np.array([w, h])
    lm3 = s.face_lm * np.array([w, h, w])  # MediaPipe z shares the x scale

    def d2(a, b):
        return np.linalg.norm(lm2[:, a] - lm2[:, b], axis=1)

    def d3(a, b):
        return np.linalg.norm(lm3[:, a] - lm3[:, b], axis=1)

    face_h = d2(FOREHEAD, CHIN)
    # Which iris index belongs to the image-left eye is decided from the data.
    a_center = (lm2[:, EYE_A["outer"]] + lm2[:, EYE_A["inner"]]) / 2
    near0 = np.nanmedian(np.linalg.norm(lm2[:, IRIS[0]] - a_center, axis=1))
    near1 = np.nanmedian(np.linalg.norm(lm2[:, IRIS[1]] - a_center, axis=1))
    iris_of = {"A": IRIS[0], "B": IRIS[1]} if near0 < near1 else {"A": IRIS[1], "B": IRIS[0]}
    for side, e in (("A", EYE_A), ("B", EYE_B)):
        width = d2(e["outer"], e["inner"])
        f[f"ear{side}"] = (d2(e["top"][0], e["bottom"][0]) + d2(e["top"][1], e["bottom"][1])) / (2 * width)
        iris = lm2[:, iris_of[side]]
        cx = (lm2[:, e["outer"], 0] + lm2[:, e["inner"], 0]) / 2
        cy = (lm2[:, e["lid_top"], 1] + lm2[:, e["lid_bottom"], 1]) / 2
        f[f"irisX{side}"] = (iris[:, 0] - cx) / (width / 2)
        f[f"irisY{side}"] = -(iris[:, 1] - cy) / (width / 2)
    f["lipGap"] = d2(LIP_UP, LIP_LO) / face_h
    iod = d3(EYE_A["outer"], EYE_B["outer"])
    f["jawDrop"] = d3(SUBNASALE, CHIN) / iod
    f["mouthWidth"] = d3(MOUTH_A, MOUTH_B) / iod
    # Keep the existing face-midline anchor, but measure the mouth offset in the head's
    # local frame. Projection onto screen X mixed head yaw/roll into lateral lip motion.
    mouth_mid = (lm3[:, MOUTH_A] + lm3[:, MOUTH_B]) / 2
    face_mid = (lm3[:, FOREHEAD] + lm3[:, CHIN]) / 2
    lateral = np.sum((mouth_mid - face_mid) * face_x, axis=1)
    f["mouthShift"] = np.divide(lateral, iod, out=np.full(n, np.nan), where=iod > 0)

    for i, name in enumerate(str(x) for x in s.bs_names):
        if name != "_neutral":
            f["bs:" + name] = s.bs[:, i]

    # Upper body from the pose landmarks, independent of which shoulder is image-left.
    px = s.pose_img[:, :, :2] * np.array([w, h])
    l, r = px[:, SHOULDER_L], px[:, SHOULDER_R]
    l_on_left = np.nanmedian(l[:, 0]) < np.nanmedian(r[:, 0])
    a_sh, b_sh = (l, r) if l_on_left else (r, l)  # a = image-left shoulder
    # Shoulder line angle: > 0 when the image-right shoulder is lower = torso turned clockwise.
    f["shoulderRoll"] = np.degrees(np.arctan2(b_sh[:, 1] - a_sh[:, 1], b_sh[:, 0] - a_sh[:, 0]))
    wl, wr = s.pose_world[:, SHOULDER_L], s.pose_world[:, SHOULDER_R]
    wa, wb = (wl, wr) if l_on_left else (wr, wl)
    # Torso yaw: > 0 when the image-left shoulder is nearer the camera (smaller z), i.e. the
    # chest turns toward the image right.
    f["shoulderYaw"] = np.degrees(np.arctan2(wb[:, 2] - wa[:, 2], np.abs(wb[:, 0] - wa[:, 0])))
    f["shoulderY"] = ((a_sh[:, 1] + b_sh[:, 1]) / 2) / face_h
    f["face_ok"] = ok.astype(float)
    f["pose_ok"] = s.pose_ok.astype(float)
    return f
