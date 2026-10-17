"""Experiment: per-frame cues for cheekPuff and tongueOut, which MediaPipe barely reports.

Cues compared against hand-annotated event intervals in your own clips (none are distributed):
regression/expressions/manifest.json is a JSON list of {"id": str, "file": str (relative to
regression/expressions), "action": "cheek_puff" | "tongue_out" | "both" | other, "events":
[[start_s, end_s], ...]}.
  mp_*        MediaPipe's own blendshape (baseline)
  geo_cheek   3D cheek width over eye distance, relative to the clip's lower quartile, gated by
              closed lips
  col_tongue  share of tongue-coloured pixels inside / just below the inner-lip outline
  zs_*        zero-shot image-text similarity (SigLIP 2) on the face crop: target prompt vs
              neutral prompts

usage (compute machine): python research/rare_expr.py [--model google/siglip2-so400m-patch14-384]
writes research/results/rare_expr.json (per-clip ROC AUC, best F1) and per-clip curves.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v2l.config import Config  # noqa: E402
from v2l.pipeline import get_signals  # noqa: E402

CLIPS = ROOT / "regression" / "expressions"
OUT = ROOT / "research" / "results"

# MediaPipe face mesh indices
CHEEK_A, CHEEK_B = 50, 280            # mid-cheek points
EYE_OUT_A, EYE_OUT_B = 33, 263
INNER_LIP = [78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95]
LIP_UP, LIP_LO, CHIN = 13, 14, 152

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


def roc_auc(score: np.ndarray, label: np.ndarray) -> float:
    pos, neg = score[label], score[~label]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def best_f1(score: np.ndarray, label: np.ndarray) -> float:
    best = 0.0
    for th in np.quantile(score, np.linspace(0.05, 0.95, 37)):
        p = score >= th
        tp, fp, fn = (p & label).sum(), (p & ~label).sum(), (~p & label).sum()
        if tp:
            best = max(best, 2 * tp / (2 * tp + fp + fn))
    return float(best)


def frames(video: pathlib.Path, width: int):
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    while True:
        ok, f = cap.read()
        if not ok:
            break
        h, w = f.shape[:2]
        if w > width:
            f = cv2.resize(f, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
        yield f


def cues_for(video: pathlib.Path, zs) -> tuple[dict[str, np.ndarray], np.ndarray]:
    s = get_signals(video, Config(), log=lambda *a: None)
    h, w = s.size
    names = [str(n) for n in s.bs_names]
    lm3 = s.face_lm * np.array([w, h, w])
    lm2 = s.face_lm[:, :, :2] * np.array([w, h])
    iod = np.linalg.norm(lm3[:, EYE_OUT_A] - lm3[:, EYE_OUT_B], axis=1)
    cheek = np.linalg.norm(lm3[:, CHEEK_A] - lm3[:, CHEEK_B], axis=1) / iod
    face_h = np.linalg.norm(lm2[:, 10] - lm2[:, CHIN], axis=1)
    gap = np.linalg.norm(lm2[:, LIP_UP] - lm2[:, LIP_LO], axis=1) / face_h
    closed = np.clip(1 - gap / 0.02, 0, 1)
    out = {
        "mp_cheek": s.bs[:, names.index("cheekPuff")],
        "geo_cheek": (cheek - np.nanpercentile(cheek, 25)) * closed,
        "mp_jaw": s.bs[:, names.index("jawOpen")],
    }
    tongue = np.full(len(s.t), np.nan)
    crops = []
    for i, img in enumerate(frames(video, 1280)):
        if i >= len(s.t):
            break
        if not s.face_ok[i]:
            crops.append(None)
            continue
        pts = lm2[i]
        # Tongue colour inside the inner-lip outline, extended down toward the chin (a protruding
        # tongue covers the lower lip).
        poly = pts[INNER_LIP].astype(np.int32)
        mask = np.zeros(img.shape[:2], np.uint8)
        cv2.fillPoly(mask, [poly], 1)
        mid = ((pts[LIP_LO] + pts[CHIN]) / 2).astype(np.int32)
        mw = int(np.linalg.norm(pts[61] - pts[291]) / 2)
        cv2.ellipse(mask, tuple(int(v) for v in (pts[LIP_LO] + mid) / 2), (max(mw // 2, 2), max(int(np.linalg.norm(mid - pts[LIP_LO]) / 2), 2)), 0, 0, 360, 1, -1)
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
        skin_ref = lab[int(pts[50][1]), int(pts[50][0])]  # cheek skin colour for this frame
        sel = mask.astype(bool)
        if sel.sum() > 10:
            px = lab[sel]
            # Tongue: clearly redder than the cheek skin, not dark (mouth cavity), not white (teeth).
            redder = (px[:, 1] - skin_ref[1] > 8) & (px[:, 0] > 60) & (px[:, 0] < 200)
            tongue[i] = redder.mean() * min(sel.sum() / (mw * mw + 1), 2.0)
        x0, y0 = pts.min(0)
        x1, y1 = pts.max(0)
        pad = 0.25 * (x1 - x0)
        crop = img[max(int(y0 - pad), 0):int(y1 + pad), max(int(x0 - pad), 0):int(x1 + pad)]
        crops.append(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB) if crop.size else None)
    out["col_tongue"] = tongue
    if zs is not None:
        emb = zs.embed(crops)
        for key in ("cheek", "tongue"):
            out[f"zs_{key}"] = zs.score(emb, *PROMPTS[key])
    return {k: np.asarray(v, dtype=float) for k, v in out.items()}, s.t


class ZeroShot:
    """Image-text similarity on face crops: max over target prompts minus max over neutral ones."""

    def __init__(self, model_id: str, device: str):
        import torch
        from transformers import AutoModel, AutoProcessor

        self.torch = torch
        self.dev = device if device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float16 if self.dev == "cuda" else torch.float32
        self.model = AutoModel.from_pretrained(model_id, torch_dtype=dtype).to(self.dev).eval()
        self.proc = AutoProcessor.from_pretrained(model_id)

    def _norm(self, e):
        return self.torch.nn.functional.normalize(e.float(), dim=-1)

    def embed(self, crops):
        """Image embeddings (N, D); rows without a face are NaN."""
        with self.torch.no_grad():
            idx = [i for i, c in enumerate(crops) if c is not None]
            out = None
            for b in range(0, len(idx), 8):  # small batches: the GPU may be shared with other jobs
                chunk = idx[b:b + 8]
                im = self.proc(images=[crops[i] for i in chunk], return_tensors="pt")
                e = self._norm(self.model.get_image_features(pixel_values=im["pixel_values"].to(self.dev, self.model.dtype))).cpu().numpy()
                if out is None:
                    out = np.full((len(crops), e.shape[1]), np.nan, dtype=np.float32)
                out[chunk] = e
            return out

    def score(self, emb, pos, neg):
        with self.torch.no_grad():
            t = self.proc(text=list(pos) + list(neg), return_tensors="pt", padding="max_length", max_length=64)
            te = self._norm(self.model.get_text_features(**{k: v.to(self.dev) for k, v in t.items()})).cpu().numpy()
        sim = emb @ te.T
        return sim[:, :len(pos)].max(axis=1) - sim[:, len(pos):].max(axis=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/siglip2-so400m-patch14-384")
    ap.add_argument("--device", default="auto", help="cuda, cpu or auto")
    ap.add_argument("--no-zero-shot", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    zs = None if a.no_zero_shot else ZeroShot(a.model, a.device)
    manifest = json.loads((CLIPS / "manifest.json").read_text())
    results = []
    for clip in manifest:
        cues, t = cues_for(CLIPS / clip["file"], zs)
        label = np.zeros(len(t), bool)
        for a0, a1 in clip.get("events", []):
            label |= (t >= a0) & (t < a1)
        row = {"id": clip["id"], "action": clip["action"], "frames": len(t), "positive": int(label.sum())}
        for name, v in cues.items():
            ok = np.isfinite(v)
            row[name] = {"auc": roc_auc(v[ok], label[ok]), "f1": best_f1(v[ok], label[ok]),
                         "p95_neg": float(np.nanpercentile(v[~label], 95)) if (~label).any() else None,
                         "median_pos": float(np.nanmedian(v[label])) if label.any() else None}
        np.savez_compressed(OUT / f"{clip['id']}.npz", t=t, label=label, **cues)
        results.append(row)
        print(clip["id"], clip["action"], {k: round(v["auc"], 3) for k, v in row.items() if isinstance(v, dict)}, flush=True)
    # Pooled across clips: target cue on clips of that action plus the other clips as negatives.
    summary = {}
    for key, cue_names in (("cheek", ["mp_cheek", "geo_cheek", "zs_cheek"]), ("tongue", ["col_tongue", "zs_tongue", "mp_jaw"])):
        action = {"cheek": "cheek_puff", "tongue": "tongue_out"}[key]
        for cue in cue_names:
            vs, ls = [], []
            for clip in manifest:
                d = np.load(OUT / f"{clip['id']}.npz")
                if cue not in d:
                    continue
                v = d[cue]
                lab = d["label"] if clip["action"] in (action, "both") else np.zeros(len(v), bool)
                # Per-clip robust standardisation, as the pipeline calibrates per clip.
                med, iqr = np.nanmedian(v), np.nanpercentile(v, 75) - np.nanpercentile(v, 25) + 1e-6
                vs.append((v - med) / iqr)
                ls.append(lab)
            v, lab = np.concatenate(vs), np.concatenate(ls)
            ok = np.isfinite(v)
            summary[f"{key}:{cue}"] = {"auc": roc_auc(v[ok], lab[ok]), "f1": best_f1(v[ok], lab[ok]), "positives": int(lab.sum())}
    (OUT / "rare_expr.json").write_text(json.dumps({"clips": results, "pooled": summary}, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
