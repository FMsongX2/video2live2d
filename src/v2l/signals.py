"""Video -> raw per-frame tracking signals (MediaPipe Face Landmarker + Pose Landmarker).

Nothing is smoothed or calibrated here; every later stage works offline on the whole clip.
Frame times come from the container timestamps, so variable-frame-rate phone video is handled.
"""

from __future__ import annotations

import dataclasses
import pathlib

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mpt
from mediapipe.tasks.python import vision

from .assets import CACHE, download
from .errors import UnusableInput

MODEL_URLS = {
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
    "pose_landmarker_full.task": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task",
}


def tracker_model(name: str) -> pathlib.Path:
    return download(MODEL_URLS[name], CACHE / name)


@dataclasses.dataclass
class Signals:
    t: np.ndarray            # (T,) frame time in seconds from the clip start
    size: np.ndarray         # (2,) analysed frame height, width
    bs_names: np.ndarray     # (52,) blendshape names as MediaPipe reports them
    bs: np.ndarray           # (T, 52) NaN where no face
    head_mat: np.ndarray     # (T, 4, 4) facial transformation matrix
    face_lm: np.ndarray      # (T, 478, 3) normalised landmarks
    face_ok: np.ndarray      # (T,) bool
    pose_img: np.ndarray     # (T, 33, 4) normalised x, y, z, visibility
    pose_world: np.ndarray   # (T, 33, 4) metres, hip centred
    pose_ok: np.ndarray      # (T,) bool

    def save(self, path: str | pathlib.Path) -> None:
        np.savez_compressed(path, **dataclasses.asdict(self))

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "Signals":
        d = np.load(path)
        return cls(**{f.name: d[f.name] for f in dataclasses.fields(cls)})


def extract(
    video: str | pathlib.Path,
    width: int = 1280,
    start: float | None = None,
    end: float | None = None,
    flip: bool = False,
    progress=None,
) -> Signals:
    """Runs the trackers over every frame in [start, end).

    flip mirrors the frames first; use it for selfie footage that was saved mirrored, so that
    image space matches what a viewer of the real person would see.
    progress(done, total) is called after every frame; total is the expected frame count.
    """
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise UnusableInput(f"cannot open video: {video}")
    cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    nominal_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    first = int(round((start or 0.0) * nominal_fps))
    last = n_total if end is None else min(n_total, int(round(end * nominal_fps)))
    expected = max(last - first, 1)
    if start:
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)

    face = vision.FaceLandmarker.create_from_options(
        vision.FaceLandmarkerOptions(
            base_options=mpt.BaseOptions(model_asset_path=str(tracker_model("face_landmarker.task"))),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
    )
    pose = vision.PoseLandmarker.create_from_options(
        vision.PoseLandmarkerOptions(
            base_options=mpt.BaseOptions(model_asset_path=str(tracker_model("pose_landmarker_full.task"))),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
        )
    )

    names: list[str] | None = None
    rows: dict[str, list] = {k: [] for k in ("t", "bs", "mat", "lm", "pimg", "pworld")}
    size = None
    last_ms = -1
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        pos = cap.get(cv2.CAP_PROP_POS_MSEC)
        sec = pos / 1000.0 if pos > 0 or i == 0 else (i / nominal_fps + (start or 0.0))
        if end is not None and sec >= end:
            break
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
        if flip:
            frame = cv2.flip(frame, 1)
        size = frame.shape[:2]
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        ts = max(last_ms + 1, round(sec * 1000.0))  # MediaPipe needs strictly increasing ms
        last_ms = ts
        rows["t"].append(sec - (start or 0.0))

        fr = face.detect_for_video(image, ts)
        if fr.face_blendshapes:
            cats = fr.face_blendshapes[0]
            names = names or [c.category_name for c in cats]
            rows["bs"].append([c.score for c in cats])
            rows["mat"].append(np.asarray(fr.facial_transformation_matrixes[0], dtype=np.float64))
            rows["lm"].append([[p.x, p.y, p.z] for p in fr.face_landmarks[0]])
        else:
            rows["bs"].append(None)
            rows["mat"].append(None)
            rows["lm"].append(None)

        pr = pose.detect_for_video(image, ts)
        if pr.pose_landmarks:
            rows["pimg"].append([[p.x, p.y, p.z, p.visibility] for p in pr.pose_landmarks[0]])
            rows["pworld"].append([[p.x, p.y, p.z, p.visibility] for p in pr.pose_world_landmarks[0]])
        else:
            rows["pimg"].append(None)
            rows["pworld"].append(None)
        i += 1
        if progress:
            progress(i, max(expected, i))
    cap.release()
    face.close()
    pose.close()

    if i == 0:
        raise UnusableInput(f"no frames decoded from {video}")
    if names is None:
        raise UnusableInput("no face found in any frame")

    def stack(seq: list, shape: tuple) -> np.ndarray:
        out = np.full((len(seq), *shape), np.nan)
        for k, r in enumerate(seq):
            if r is not None:
                out[k] = r
        return out

    return Signals(
        t=np.asarray(rows["t"], dtype=np.float64),
        size=np.asarray(size),
        bs_names=np.asarray(names),
        bs=stack(rows["bs"], (len(names),)),
        head_mat=stack(rows["mat"], (4, 4)),
        face_lm=stack(rows["lm"], (478, 3)),
        face_ok=np.asarray([r is not None for r in rows["bs"]]),
        pose_img=stack(rows["pimg"], (33, 4)),
        pose_world=stack(rows["pworld"], (33, 4)),
        pose_ok=np.asarray([r is not None for r in rows["pimg"]]),
    )
