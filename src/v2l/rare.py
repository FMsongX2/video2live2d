"""cheekPuff and tongueOut from video, which MediaPipe's blendshape model barely reports.

Public ARKit regressors are trained on Live Link Face pseudo-labels in which these channels are
almost always zero (SemanticFace, arXiv 2603.14827, reports TongueOut constant and CheekPuff
~0 in its data), and the common AU benchmarks (BP4D, DISFA) do not annotate AU34 (puff). So v2l
reads them zero-shot: SigLIP 2 image-text similarity on the face crop, target prompts against
neutral ones.

Measured on 10 hand-annotated stock clips (3 cheek-puff, 5 tongue-out, 2 talking; not distributed):
pooled frame AUC 0.98 (cheek) and 0.996 (tongue) against 0.07 for MediaPipe's cheekPuff;
leave-one-clip-out thresholding F1 0.86 (cheek) and 0.92 (tongue). The thresholds in
config.ExpressionConfig map the raw score to 0..1: about the 97th percentile of non-target frames
and the median of target frames (research/rare_expr.py reproduces the measurement on your own
annotated clips).

Needs torch + transformers (`pip install -e '.[rare]'`); without them the stage is skipped.
"""

from __future__ import annotations

import cv2
import numpy as np

from .signals import Signals

MODEL_ID = "google/siglip2-so400m-patch14-384"
PROMPTS = {
    "cheek": (["a photo of a person puffing out their cheeks full of air",
               "a close-up of a face with puffed cheeks and closed lips"],
              ["a photo of a person with a neutral face", "a photo of a person talking",
               "a photo of a person smiling", "a photo of a person sticking their tongue out"]),
    "tongue": (["a photo of a person sticking their tongue out",
                "a close-up of a face with the tongue out of the mouth"],
               ["a photo of a person with a neutral face", "a photo of a person talking",
                "a photo of a person smiling with teeth", "a photo of a person puffing out their cheeks"]),
}


def available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def _device(torch, need_gb: float = 3.0) -> str:
    """The GPU is shared with other jobs; use it only when enough memory is free."""
    if torch.cuda.is_available():
        free, _ = torch.cuda.mem_get_info()
        if free / 2**30 >= need_gb:
            return "cuda"
    return "cpu"


def _scored_frames(s: Signals, stride: int) -> int:
    return int(sum(1 for i in range(0, len(s.t), stride) if s.face_ok[i]))


def _face_crops(video, s: Signals, start: float | None, flip: bool, width: int, stride: int):
    """Yields (frame index, RGB face crop) for every stride-th frame that has a face."""
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    if start:
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    h, w = s.size
    for i in range(len(s.t)):
        ok, frame = cap.read()
        if not ok:
            break
        if i % stride or not s.face_ok[i]:
            continue
        fh, fw = frame.shape[:2]
        if fw > width:
            frame = cv2.resize(frame, (width, round(fh * width / fw)), interpolation=cv2.INTER_AREA)
        if flip:
            frame = cv2.flip(frame, 1)
        pts = s.face_lm[i, :, :2] * np.array([w, h])
        (x0, y0), (x1, y1) = pts.min(0), pts.max(0)
        pad = 0.25 * (x1 - x0)
        crop = frame[max(int(y0 - pad), 0):int(y1 + pad), max(int(x0 - pad), 0):int(x1 + pad)]
        if crop.size:
            yield i, cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    cap.release()


def scores(video, s: Signals, start: float | None = None, flip: bool = False, width: int = 1280,
           stride: int = 3, log=print, progress=None) -> dict[str, np.ndarray]:
    """Raw zero-shot scores per frame (NaN on frames not scored); keys 'cheek', 'tongue'.
    progress(done, total) is called after every batch of scored frames."""
    import torch
    from transformers import AutoModel, AutoProcessor

    dev = _device(torch)
    log(f"rare expressions: SigLIP 2 on {dev}, scoring one frame in {stride}")
    model = AutoModel.from_pretrained(MODEL_ID, torch_dtype=torch.float16 if dev == "cuda" else torch.float32).to(dev).eval()
    proc = AutoProcessor.from_pretrained(MODEL_ID)
    with torch.no_grad():
        texts = {k: p + n for k, (p, n) in PROMPTS.items()}
        temb = {}
        for k, t in texts.items():
            tok = proc(text=t, return_tensors="pt", padding="max_length", max_length=64)
            e = model.get_text_features(**{kk: v.to(dev) for kk, v in tok.items()}).float()
            temb[k] = torch.nn.functional.normalize(e, dim=-1).cpu().numpy()
        out = {k: np.full(len(s.t), np.nan) for k in PROMPTS}
        batch: list[tuple[int, np.ndarray]] = []
        total, done = _scored_frames(s, stride), 0

        def flush():
            nonlocal done
            idx = [i for i, _ in batch]
            px = proc(images=[c for _, c in batch], return_tensors="pt")["pixel_values"].to(dev, model.dtype)
            e = torch.nn.functional.normalize(model.get_image_features(pixel_values=px).float(), dim=-1).cpu().numpy()
            for k, (pos, _) in PROMPTS.items():
                sim = e @ temb[k].T
                out[k][idx] = sim[:, :len(pos)].max(1) - sim[:, len(pos):].max(1)
            done += len(batch)
            batch.clear()
            if progress:
                progress(done, total)

        for item in _face_crops(video, s, start, flip, width, stride):
            batch.append(item)
            if len(batch) == 8:
                flush()
        if batch:
            flush()
    return out

