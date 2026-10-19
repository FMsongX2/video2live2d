"""NVIDIA Audio2Face-3D v3.0 (diffusion) on onnxruntime: speech audio -> ARKit blendshapes + eye rotations.

Model: Hugging Face nvidia/Audio2Face-3D-v3.0 at a pinned revision, NVIDIA Open Model License,
downloaded on first use into ~/.cache/v2l/a2f/<repo>/<revision> (never shipped with V2L).

The driving code is a Python port of NVIDIA's MIT-licensed Audio2Face-3D-SDK
(github.com/NVIDIA/Audio2Face-3D-SDK, commit 1ca0f02535ed774f5dbcd724a31cd486368dc783,
Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES). Ported files:
  audio2x-common/source/audio2x/audio_accumulator.cpp       WindowProgress (windows, frame timestamps)
  audio2face-sdk/source/audio2face-core/executor_diffusion*.cpp, executor.cpp, model_diffusion.cpp
                                                              inference loop, GRU state, noise, emotion,
                                                              identity, centre-frame selection
  audio2face-sdk/source/audio2face-core/multitrack_animator*.cpp/.cu
                                                              skin / tongue / eyes post-processing
  audio2face-sdk/source/audio2face-core/blendshape_solver_base.cpp, blendshape_solver.cpp, bvls.cpp
                                                              regularised blendshape solve (CPU path)
  audio2face-sdk/source/audio2face-core/parse_helper.cpp      model / solver config reading

Differences from the SDK, all deliberate:
  - diffusion noise comes from numpy's Philox generator seeded with audio.seed (the SDK uses cuRAND
    Philox with its default seed, which numpy cannot reproduce bit for bit);
  - the SDK's TensorRT engine is replaced by onnxruntime on the same network.onnx;
  - the skin post-processing is evaluated only on the vertices the skin solve reads (frontalMask);
  - audio.blink_strength scales the solved eyeBlink weights (in the SDK's diffusion path its
    blink_strength only scales a manual blink offset that is always 0, i.e. it has no effect);
  - input audio is resampled to 16 kHz (the SDK requires 16 kHz input).

Eye rotations: SDK eyes output per eye (rotation about X, rotation about Y) in degrees; the
returned A2FResult.eyes is [right_yaw, right_pitch, left_yaw, left_pitch] in degrees with
yaw + = towards image right (performer's left) and pitch + = up, i.e. yaw = rotY, pitch = -rotX
for the SDK's right-handed, Y-up, +Z-forward head frame.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import pathlib
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable

import numpy as np

from .assets import CACHE

if TYPE_CHECKING:
    from .config import AudioConfig

REPO = "nvidia/Audio2Face-3D-v3.0"
REVISION = "b74132732fd9a9d29b237bec193ded64c9745e91"
MODEL_DIR = CACHE / "a2f" / REPO.replace("/", "--") / REVISION
IDENTITIES = ("Claire", "James", "Mark")
SHARED_FILES = {
    "network.onnx": "db47c2701ca849de443c9e9f25657210f829a74fc458ee6fed603a8a501253a8",
    "network_info.json": "5524cdbe96a6bc89c78f06f32ae959e2302c50c663f407cb2b392c0ecac5975d",
}
IDENTITY_FILES = {
    "Claire": {
        "model_config_Claire.json": "0819530451ad28ef42c1a478398850dc91e32475a49f9899ed37216309107fb4",
        "model_data_Claire.npz": "4f05331263fa609321335e55c20922f4d6709d33160d368c3b537f019429ea4f",
        "bs_skin_Claire.npz": "bcb1fde2c7384fe9ec3cf9932b0fdeeda01fe4a1e42bba3817bba14e7f1716d3",
        "bs_skin_config_Claire.json": "e2b508c5d17f1fb01c3a5b0292072d09e66e8c55bc23fcbe0c9aee8f8eae1713",
        "bs_tongue_Claire.npz": "812f10c34edb6ab6f36aedfe1d59a79d8190a5a8ee0a6071382f6bae9e3413b6",
        "bs_tongue_config_Claire.json": "ace4b0b6b9be280f96a66568bd13ac4ea1fddf9c690464ab450fe339d9752e98",
    },
    "James": {
        "model_config_James.json": "a8f1b3a0868611e4ec22f798877ae83f7aca293194d2d039961185f788a95030",
        "model_data_James.npz": "23f653f88fceddda3ae137a7c2199a59ed36822f7f9681234eaad38ba7d753f3",
        "bs_skin_James.npz": "da119b5291de9d7b711ccd34c99f5a9cec001f5d1e25083c294d3c02f642691c",
        "bs_skin_config_James.json": "51ff9803d97c92cadc4f1bad07331ccde3c431d301e564208f5b2071995aecf2",
        "bs_tongue_James.npz": "831c9d646174d84f628d5516694e00b5077cd5b2bc1dd964d817fabe68497bcc",
        "bs_tongue_config_James.json": "54c4d9d2a17916efdc277693a6b8bd297fd3f3f63e25da4da77cfb35d66b6c1d",
    },
    "Mark": {
        "model_config_Mark.json": "56e2e44b1051813714ab5db70037653a4ff8d907ed45b4d72e6ab96e38ac6421",
        "model_data_Mark.npz": "8843a3a6599b263fd368ff445f4c7a9f9b4454f739271560004d1589693886cf",
        "bs_skin_Mark.npz": "a13234c0fa07e5c3bea3b2f4ad1d5c17fcec5482285607a2c3cd79099ed81297",
        "bs_skin_config_Mark.json": "6a074ba302866485cd6c05805f7414b117b73382b6ec95a4d50b4733809ef1b0",
        "bs_tongue_Mark.npz": "3dfd5dcdeca813df9574536750cf6d2382ec3167482f0b883e1b1eebf84fc022",
        "bs_tongue_config_Mark.json": "24ce53bde029f8318a0a5a6c8fa9975290537107d8917d10e5d6549f9635f6c3",
    },
}
INSTALL_HINT = "install the audio extra: pip install -e '.[audio]'"
ARKIT_NAMES = (
    "eyeBlinkLeft", "eyeLookDownLeft", "eyeLookInLeft", "eyeLookOutLeft", "eyeLookUpLeft", "eyeSquintLeft",
    "eyeWideLeft", "eyeBlinkRight", "eyeLookDownRight", "eyeLookInRight", "eyeLookOutRight", "eyeLookUpRight",
    "eyeSquintRight", "eyeWideRight", "jawForward", "jawLeft", "jawRight", "jawOpen", "mouthClose", "mouthFunnel",
    "mouthPucker", "mouthLeft", "mouthRight", "mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft",
    "mouthFrownRight", "mouthDimpleLeft", "mouthDimpleRight", "mouthStretchLeft", "mouthStretchRight",
    "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper", "mouthPressLeft", "mouthPressRight",
    "mouthLowerDownLeft", "mouthLowerDownRight", "mouthUpperUpLeft", "mouthUpperUpRight", "browDownLeft",
    "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight", "cheekPuff", "cheekSquintLeft",
    "cheekSquintRight", "noseSneerLeft", "noseSneerRight", "tongueOut",
)
GPU_MEM_LIMIT = 3 << 30          # onnxruntime CUDA arena cap; a run measured ~2 GB of GPU memory in total
BVLS_TOLERANCE = 1e-10           # SDK kDefaultTolerance
SACCADE_FPS = 30.0               # SDK: saccade table is sampled at 30 fps whatever the output rate


@dataclasses.dataclass
class A2FResult:
    fps: float                               # native output rate (60 for v3.0)
    t: np.ndarray                            # (T,) seconds from the first audio sample
    blendshapes: dict[str, np.ndarray]       # the 52 ARKit camelCase skin poses, 0..1
    eyes: np.ndarray                         # (T, 4) degrees: right_yaw, right_pitch, left_yaw, left_pitch
    meta: dict                               # JSON-serialisable run description
    tongue: dict[str, np.ndarray] = dataclasses.field(default_factory=dict)  # 16 tongue poses (SDK weights)


# ------------------------------------------------------------------------------- dependencies

def _ort():
    try:
        import onnxruntime
    except ImportError as e:
        raise ImportError(f"onnxruntime is missing ({e}); {INSTALL_HINT}") from None
    return onnxruntime


def _files(identity: str) -> dict[str, str]:
    if identity not in IDENTITY_FILES:
        raise ValueError(f"unknown Audio2Face-3D identity {identity!r}; choose one of {', '.join(IDENTITIES)}")
    return {**SHARED_FILES, **IDENTITY_FILES[identity]}


def available() -> tuple[bool, str]:
    """(True, note) when onnxruntime imports; the note says whether model files still need downloading."""
    try:
        ort = _ort()
    except ImportError as e:
        return False, str(e)
    missing = [n for n in {**SHARED_FILES, **IDENTITY_FILES["Claire"]} if not (MODEL_DIR / n).exists()]
    cuda = "CUDAExecutionProvider" in ort.get_available_providers()
    where = "NVIDIA GPU available" if cuda else "CPU only"
    if missing:
        return True, f"onnxruntime {ort.__version__} ({where}); model files download on first use (~750 MB)"
    return True, f"onnxruntime {ort.__version__} ({where}); model files cached"


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(name: str, sha256: str, log: Callable) -> pathlib.Path:
    """Streams one pinned file into MODEL_DIR (atomic rename); a verified .ok marker skips rehashing."""
    path, ok = MODEL_DIR / name, MODEL_DIR / f".{name}.ok"
    if path.exists() and ok.exists() and ok.read_text() == sha256:
        return path
    if path.exists() and _sha256(path) == sha256:
        ok.write_text(sha256)
        return path
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}"
    tmp = path.with_name(path.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (v2l)"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        if total > 50 << 20:
            log(f"Downloading Audio2Face-3D {name} ({total / 1e6:.0f} MB)")
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != sha256:
        tmp.unlink()
        raise RuntimeError(f"{url} does not match its pinned SHA-256")
    tmp.replace(path)
    ok.write_text(sha256)
    return path


def download(identities=IDENTITIES, log: Callable = print) -> pathlib.Path:
    """Fetches the network and the given identities' data (all three by default); returns MODEL_DIR."""
    if isinstance(identities, str):
        identities = (identities,)
    for ident in identities:
        for name, sha in _files(ident).items():
            _fetch(name, sha, log)
    return MODEL_DIR


# ------------------------------------------------------------------------------ model data

@dataclasses.dataclass
class Solver:
    """BlendshapeSolverBase::Prepare + BlendshapeSolver (CPU, BVLS) for one mesh."""
    names: list[str]                 # all poses, file order
    active: np.ndarray               # (P,) bool
    D: np.ndarray                    # (M, A) deltas: masked vertex components x active poses
    neutral: np.ndarray              # (M,) masked neutral
    mask: np.ndarray | None          # (M,) indices into the flattened pose, or None for all
    A: np.ndarray                    # (A, A) regularised normal matrix
    temporal: float                  # TemporalReg * scaleFactor (weight of the previous solution in b)
    cancel: list[tuple[int, int]]    # active-index pairs
    multipliers: np.ndarray          # (P,)
    offsets: np.ndarray              # (P,)

    @classmethod
    def from_arrays(cls, names, neutral, deltas, params: dict, mask=None) -> "Solver":
        """neutral (V*3,), deltas (V*3, P); params = the "blendshape_params" of a bs_*_config.json."""
        neutral = np.asarray(neutral, dtype=np.float64).ravel()
        deltas = np.asarray(deltas, dtype=np.float64)
        P = deltas.shape[1]
        active = np.asarray(params.get("bsSolveActivePoses", [1] * P), dtype=bool)
        if not active.any():
            raise ValueError("blendshape solve needs at least one active pose")
        rows = None
        if mask is not None and len(mask):
            m = np.asarray(mask, dtype=np.int64)
            rows = (3 * m[:, None] + np.arange(3)).ravel()
        idx = np.flatnonzero(active)
        D = deltas[:, idx] if rows is None else deltas[rows][:, idx]
        nm = neutral if rows is None else neutral[rows]
        nA = len(idx)

        def pairs(key):
            # getPairs: group the active poses by pair id (-1 included, as in the SDK); keep groups of 2.
            ids = np.asarray(params.get(key, [-1] * P))[idx]
            groups: dict[int, list[int]] = {}
            for i, v in enumerate(ids):
                groups.setdefault(int(v), []).append(i)
            return [(g[0], g[1]) for g in groups.values() if len(g) == 2]

        sym = np.zeros((0, nA))
        for i, j in pairs("bsSolveSymmetryPoses"):
            row = np.zeros(nA)
            row[i], row[j] = 1.0, -1.0
            sym = np.vstack([sym, row])
        bb = neutral.reshape(-1, 3)
        scale = (np.linalg.norm(bb.max(0) - bb.min(0)) / params.get("templateBBSize", 45.0)) ** 2
        l1, l2 = params["strengthL1regularization"], params["strengthL2regularization"]
        tr, sr = params["strengthTemporalSmoothing"], params["strengthSymmetry"]
        A = (D.T @ D + l1 * l1 * 0.25 * scale * np.ones((nA, nA)) + l2 * 10.0 * scale * np.eye(nA)
             + tr * 100.0 * scale * np.eye(nA) + sr * 10.0 * scale * sym.T @ sym)
        return cls(names=list(names), active=active, D=D, neutral=nm, mask=rows, A=A, temporal=tr * scale,
                   cancel=pairs("bsSolveCancelPoses"),
                   multipliers=np.asarray(params.get("bsWeightMultipliers", [1.0] * P), dtype=np.float64),
                   offsets=np.asarray(params.get("bsWeightOffsets", [0.0] * P), dtype=np.float64))

    def masked(self, poses: np.ndarray) -> np.ndarray:
        return poses if self.mask is None else poses[..., self.mask]

    def solve(self, targets: np.ndarray) -> np.ndarray:
        """targets (T, M) masked absolute poses in frame order -> weights (T, P) (temporal state from zero)."""
        rhs = (np.asarray(targets, dtype=np.float64) - self.neutral) @ self.D
        nA = self.A.shape[0]
        lo, prev = np.zeros(nA), np.zeros(nA)
        out = np.zeros((len(rhs), len(self.names)))
        for t, atb in enumerate(rhs):
            b = atb + self.temporal * prev
            hi = np.ones(nA)
            x = bvls(self.A, b, lo, hi)
            if self.cancel:
                for i, j in self.cancel:
                    hi[j if x[i] >= x[j] else i] = 1e-10
                x = bvls(self.A, b, lo, hi)
            prev = x
            out[t, self.active] = x
        return out * self.multipliers + self.offsets


def _lstsq(A, b):
    return np.linalg.lstsq(A, b, rcond=None)[0]


def bvls(A: np.ndarray, b: np.ndarray, lo: np.ndarray, hi: np.ndarray, tol: float = BVLS_TOLERANCE) -> np.ndarray:
    """min |A x - b|^2 subject to lo <= x <= hi; a line-by-line port of the SDK's bvls::solveSystem."""
    n = len(b)

    def cmin(x, on):
        free = np.flatnonzero(on == 0)
        if free.size == 0:
            return x
        if free.size == n:
            return _lstsq(A, b)
        x0 = x.copy()
        x0[free] = 0.0
        x = x.copy()
        x[free] = _lstsq(A[:, free], b - A @ x0)
        return x

    def cost_grad(x):
        r = A @ x - b
        return 0.5 * float(r @ r), A.T @ r

    def update_on(x):
        return np.where(x == lo, -1, np.where(x == hi, 1, 0))

    def kkt(g, on):
        return float(np.max(np.where(on == 0, np.abs(g), g * on)))

    # initialFeasiblePoint
    x, on = np.zeros(n), np.zeros(n, dtype=np.int64)
    while True:
        x = cmin(x, on)
        below = (on == 0) & (x < lo)
        above = (on == 0) & ~below & (x > hi)
        if not (below.any() or above.any()):
            break
        x[below], on[below] = lo[below], -1
        x[above], on[above] = hi[above], 1
    cost, g = cost_grad(x)
    on = update_on(x)
    opt = prev_opt = kkt(g, on)
    for _ in range(n):
        if opt < tol:
            break
        # varToDetach: the bound variable whose gradient pulls it inside the most
        _, g = cost_grad(x)
        pull = np.where(x == lo, np.where(g < 0, -g, 0.0), np.where(x == hi, np.where(g > 0, g, 0.0), 0.0))
        k = int(np.argmax(pull))
        if pull[k] <= 0.0:
            break
        on[k] = 0
        while True:
            s = cmin(x, on)
            # makePointFeasible: walk towards s while feasible; snap the blocking variable to its bound
            alpha, idx, bound = 1.0, -1, 0.0
            for i in range(n):
                if x[i] == s[i]:
                    continue
                if s[i] < lo[i]:
                    a = (x[i] - lo[i]) / (x[i] - s[i])
                    if a < alpha:
                        alpha, idx, bound = a, i, lo[i]
                elif s[i] > hi[i]:
                    a = (hi[i] - x[i]) / (s[i] - x[i])
                    if a < alpha:
                        alpha, idx, bound = a, i, hi[i]
            x = x + alpha * (s - x)
            if idx != -1:
                x[idx] = bound
            if alpha == 1.0:
                break
            on = update_on(x)
        new_cost, g = cost_grad(x)
        if new_cost > cost:
            break
        cost = new_cost
        opt = kkt(g, on)
        if abs(prev_opt - opt) < 1e-6:
            break
        prev_opt = opt
    return x


@dataclasses.dataclass
class Model:
    """One identity's data plus the shared network description."""
    info: dict                       # network_info.json
    identity: str
    identity_index: int
    config: dict                     # model_config_<identity>.json "config"
    neutral_skin: np.ndarray         # (S,) flattened
    neutral_tongue: np.ndarray       # (U,)
    eye_close: np.ndarray            # (S,)
    lip_open: np.ndarray             # (S,)
    saccade: np.ndarray              # (K, 2)
    skin: Solver
    tongue: Solver

    @property
    def dims(self) -> tuple[int, int, int, int]:
        p = self.info["params"]
        return p["skin_size"], p["tongue_size"], p["jaw_size"], p["eyes_size"]


def _solver_from_files(npz: pathlib.Path, cfg: pathlib.Path) -> Solver:
    z = np.load(npz)
    names = [n.decode() if isinstance(n, bytes) else str(n) for n in z["poseNames"]]
    if names[0] != "neutral":
        raise ValueError(f"{npz.name}: poseNames[0] must be 'neutral'")
    names = names[1:]
    deltas = np.stack([z[n].ravel() for n in names], 1)
    mask = z["frontalMask"] if "frontalMask" in z.files else None
    params = json.loads(cfg.read_text())["blendshape_params"]
    return Solver.from_arrays(names, z["neutral"].ravel(), deltas, params, mask)


def load_model(identity: str, model_dir: pathlib.Path | None = None) -> Model:
    d = pathlib.Path(model_dir or MODEL_DIR)
    info = json.loads((d / "network_info.json").read_text())
    ids = info["params"]["identities"]
    if identity not in ids:
        raise ValueError(f"unknown Audio2Face-3D identity {identity!r}; choose one of {', '.join(ids)}")
    z = np.load(d / f"model_data_{identity}.npz")
    skin = _solver_from_files(d / f"bs_skin_{identity}.npz", d / f"bs_skin_config_{identity}.json")
    check_arkit_names(skin.names)
    return Model(
        info=info, identity=identity, identity_index=ids.index(identity),
        config=json.loads((d / f"model_config_{identity}.json").read_text())["config"],
        neutral_skin=z["neutral_skin"].astype(np.float64).ravel(),
        neutral_tongue=z["neutral_tongue"].astype(np.float64).ravel(),
        eye_close=z["eye_close_pose_delta"].astype(np.float64).ravel(),
        lip_open=z["lip_open_pose_delta"].astype(np.float64).ravel(),
        saccade=z["saccade_rot_matrix"].astype(np.float64),
        skin=skin,
        tongue=_solver_from_files(d / f"bs_tongue_{identity}.npz", d / f"bs_tongue_config_{identity}.json"))


def check_arkit_names(names) -> None:
    """The skin solve's poses must be exactly the 52 ARKit blendshapes (MediaPipe's camelCase names)."""
    names = list(names)
    if len(names) != len(set(names)) or set(names) != set(ARKIT_NAMES):
        extra, missing = sorted(set(names) - set(ARKIT_NAMES)), sorted(set(ARKIT_NAMES) - set(names))
        raise ValueError(f"Audio2Face-3D skin poses are not the 52 ARKit blendshapes (extra {extra}, missing {missing})")


# ------------------------------------------------------------------------------- windowing

def _tdiv(a: int, b: int) -> int:
    """C++ integer division (truncates towards zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


@dataclasses.dataclass(frozen=True)
class Progress:
    """nva2x::WindowProgress: window i covers samples [start(i), start(i) + size), target = start + target_offset."""
    size: int
    target_offset: int
    start_offset: int
    num: int
    den: int

    def __post_init__(self):
        g = math.gcd(self.num, self.den)
        object.__setattr__(self, "num", self.num // g)
        object.__setattr__(self, "den", self.den // g)

    def start(self, i: int) -> int:
        return i * self.num // self.den + self.start_offset

    def target(self, i: int) -> int:
        return self.start(i) + self.target_offset

    def available(self, end: int) -> int:
        """GetNbAvailableWindows(end, isClosed=True) from read count 0."""
        offset = self.start_offset + self.target_offset
        n = _tdiv(end * self.den - offset * self.den + self.num - 1, self.num)
        if offset + _tdiv(n * self.num, self.den) >= end:
            n -= 1
        return max(0, n + 1)

    def per_frame(self, frames: int) -> "Progress":
        return Progress(self.size, self.target_offset, self.start_offset, self.num, self.den * frames)


@dataclasses.dataclass(frozen=True)
class Plan:
    """Which inferences run and which of their centre frames are reported (offline executor)."""
    windows: Progress
    frames: Progress
    center: int                      # centre frames per inference
    left: int                        # left-truncated frames (offset of the first centre frame)
    before: int                      # frames before the audio (target < 0), never reported
    n_inferences: int
    n_frames: int
    sr: int
    fps: float

    @classmethod
    def make(cls, info: dict, n_samples: int) -> "Plan":
        p, a = info["params"], info["audio_params"]
        left, right, center = p["num_frames_left_truncate"], p["num_frames_right_truncate"], p["num_frames_center"]
        total = left + right + center
        buf = a["buffer_len"]
        windows = Progress(buf, buf * left // total, -a["padding_left"], buf * center, total)
        frames = windows.per_frame(center)
        before = frames.available(0)
        return cls(windows=windows, frames=frames, center=center, left=left, before=before,
                   n_inferences=windows.available(n_samples),
                   n_frames=max(0, frames.available(n_samples) - before),
                   sr=a["samplerate"], fps=total * a["samplerate"] / buf)

    def timestamps(self) -> np.ndarray:
        """Target sample of each reported frame."""
        return np.array([self.frames.target(f + self.before) for f in range(self.n_frames)], dtype=np.int64)

    def frames_of(self, k: int) -> list[tuple[int, int]]:
        """(prediction row, output frame) pairs that inference k reports."""
        out = []
        for i in range(self.center):
            f = k * self.center + i - self.before
            if 0 <= f < self.n_frames:
                out.append((self.left + i, f))
        return out


# --------------------------------------------------------------------------- post-processing

def face_mask_lower(neutral_skin: np.ndarray, level: float, softness: float) -> np.ndarray:
    """CalculateFaceMaskLowerFromPackedNeutralPose: per-vertex 0 (upper face) .. 1 (lower face)."""
    y = neutral_skin.reshape(-1, 3)[:, 1]
    lo, hi = y.min(), y.max()
    return 1.0 / (1.0 + np.exp(-(level - (y - lo) / (hi - lo)) / softness))


def skin_post(deltas: np.ndarray, neutral: np.ndarray, eye_close: np.ndarray, lip_open: np.ndarray,
              mask_lower: np.ndarray, p: dict, dt: float) -> np.ndarray:
    """ComputeSkinPostProcessing over frames (T, E) of element-aligned arrays (E = any subset of components)."""
    blink_offset = 0.0                                            # not in model configs; SDK default
    delta = (p["skin_strength"] * deltas
             + eye_close * (-p["eyelid_open_offset"] + blink_offset * p["blink_strength"])
             + lip_open * p["lip_open_offset"])

    def alpha(s):
        return 1.0 - 0.5 ** (dt / s) if s > 0 else -1.0

    def smooth(a):
        out = np.empty_like(delta)
        s1 = s2 = None
        for t, d in enumerate(delta):
            if s1 is None or a <= 0:
                s1, s2 = d.copy(), d.copy()
            else:
                s1 += (d - s1) * a
                s2 += (s1 - s2) * a
            out[t] = s2
        return out

    lower = smooth(alpha(p["lower_face_smoothing"]))
    upper = smooth(alpha(p["upper_face_smoothing"]))
    return (neutral + upper * p["upper_face_strength"] * (1.0 - mask_lower)
            + lower * p["lower_face_strength"] * mask_lower)


def tongue_post(deltas: np.ndarray, neutral: np.ndarray, p: dict) -> np.ndarray:
    out = neutral + deltas * p["tongue_strength"]
    out[..., 1::3] += p["tongue_height_offset"]
    out[..., 2::3] += p["tongue_depth_offset"]
    return out


def eyes_post(raw: np.ndarray, saccade: np.ndarray, p: dict, dt: float) -> np.ndarray:
    """ComputeEyesRotation: raw (T, 4) [rX, rY, lX, lY] -> (T, 4) same layout, degrees."""
    n = len(saccade)
    live = (np.arange(len(raw)) * (dt * SACCADE_FPS)) % n          # live time before each frame's increment
    idx = np.floor((p["eye_saccade_seed"] + live) % n).astype(np.int64)
    sac = saccade[idx]                                               # (T, 2) [X, Y]
    off = np.array([p["right_eye_rot_x_offset"], p["right_eye_rot_y_offset"],
                    p["left_eye_rot_x_offset"], p["left_eye_rot_y_offset"]])
    return off + raw * p["eyeballs_strength"] + p["saccade_strength"] * np.tile(sac, 2)


def to_yaw_pitch(eyes_xy: np.ndarray) -> np.ndarray:
    """[rX, rY, lX, lY] -> [right_yaw, right_pitch, left_yaw, left_pitch] (yaw + image right, pitch + up)."""
    return np.stack([eyes_xy[:, 1], -eyes_xy[:, 0], eyes_xy[:, 3], -eyes_xy[:, 2]], 1)


# ------------------------------------------------------------------------------------ runtime

def _session(path: pathlib.Path, device: str, log: Callable):
    """(onnxruntime session, device actually used)."""
    ort = _ort()
    so = ort.SessionOptions()
    so.log_severity_level = 3
    want_cuda = device in ("auto", "cuda")
    if want_cuda and "CUDAExecutionProvider" in ort.get_available_providers():
        if hasattr(ort, "preload_dlls"):
            try:
                ort.preload_dlls()
            except Exception:  # noqa: BLE001 - system CUDA libraries may still work
                pass
        cuda = ("CUDAExecutionProvider", {"arena_extend_strategy": "kSameAsRequested",
                                          "gpu_mem_limit": GPU_MEM_LIMIT, "cudnn_conv_algo_search": "HEURISTIC"})
        try:
            s = ort.InferenceSession(str(path), so, providers=[cuda, "CPUExecutionProvider"])
            if "CUDAExecutionProvider" in s.get_providers():
                return s, "cuda"
        except Exception as e:  # noqa: BLE001
            if device == "cuda":
                raise RuntimeError(f"audio.device=cuda but onnxruntime cannot use the GPU: {e}") from None
            log(f"Audio2Face-3D: GPU unavailable ({str(e).splitlines()[0]}), using the CPU")
        if device == "cuda":
            raise RuntimeError("audio.device=cuda but onnxruntime cannot use the GPU (CUDA/cuDNN libraries "
                               "missing?); " + INSTALL_HINT)
    elif device == "cuda":
        raise RuntimeError("audio.device=cuda but this onnxruntime has no CUDA support; on Linux/Windows "
                           "with an NVIDIA GPU " + INSTALL_HINT)
    return ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"]), "cpu"


def emotion_vector(info: dict, emotion: str, strength: float) -> np.ndarray:
    names = info["params"]["emotions"]
    v = np.zeros(len(names), dtype=np.float32)
    if emotion != "neutral":
        if emotion not in names:
            raise ValueError(f"unknown Audio2Face-3D emotion {emotion!r}; choose neutral or one of {', '.join(names)}")
        v[names.index(emotion)] = strength
    return v


def resample(audio: np.ndarray, sr: int, target: int) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1:
        raise ValueError("audio must be mono (1-D)")
    if sr == target:
        return audio
    from scipy.signal import resample_poly
    g = math.gcd(int(sr), int(target))
    return resample_poly(audio, target // g, int(sr) // g).astype(np.float32)


def model_params(model: Model, cfg: "AudioConfig") -> dict:
    """The identity's model_config with V2L's audio.* settings applied (absolute values, SDK names)."""
    p = dict(model.config)
    p.update(skin_strength=cfg.strength, upper_face_strength=cfg.upper_face_strength,
             lower_face_strength=cfg.lower_face_strength, tongue_strength=cfg.tongue_strength,
             eyeballs_strength=cfg.eyeballs_strength, saccade_strength=cfg.saccade_strength,
             lip_open_offset=cfg.lip_open_offset,
             eye_saccade_seed=float(model.config.get("eye_saccade_seed", 0.0)) + cfg.seed)
    return p


NOISE_CHUNKS = 6


class DiffusionNoise:
    """Standard normal float32 diffusion noise, deterministic in (seed, inference index).

    The rows are split into NOISE_CHUNKS fixed slices, each drawn from its own Philox stream keyed
    by (seed, k, slice) and filled on a thread (numpy releases the GIL), so the result does not
    depend on thread timing. prefetch(k) starts inference k's noise while the network runs."""

    def __init__(self, seed: int, shape: tuple):
        self.seed, self.shape = seed, shape
        self.pool = ThreadPoolExecutor(NOISE_CHUNKS)
        self.pending: dict[int, tuple[np.ndarray, list]] = {}

    def _fill(self, k: int, out: np.ndarray, c: int):
        rng = np.random.Generator(np.random.Philox(np.random.SeedSequence([self.seed, k, c])))
        rng.standard_normal(out=out, dtype=np.float32)

    def prefetch(self, k: int):
        if k in self.pending:
            return
        out = np.empty(self.shape, dtype=np.float32)
        rows = out.reshape(-1, self.shape[-1])
        edges = np.linspace(0, len(rows), NOISE_CHUNKS + 1).astype(int)
        jobs = [self.pool.submit(self._fill, k, rows[a:b], c) for c, (a, b) in enumerate(zip(edges[:-1], edges[1:]))]
        self.pending[k] = (out, jobs)

    def __call__(self, k: int, shape: tuple | None = None) -> np.ndarray:
        self.prefetch(k)
        out, jobs = self.pending.pop(k)
        for j in jobs:
            j.result()
        return out

    def close(self):
        self.pool.shutdown(cancel_futures=True)


def run(audio16k: np.ndarray, model: Model, session, p: dict, emotion: np.ndarray, seed: int, *,
        noise: Callable[[int, tuple], np.ndarray] | None = None, full_geometry: bool = False,
        progress: Callable | None = None) -> dict:
    """The offline diffusion executor plus post-processing. Returns frame timestamps (samples),
    masked/full skin geometry, tongue geometry, eyes [rX, rY, lX, lY] and the raw network rows.

    noise(k, shape) may supply the diffusion noise of inference k (validation against the SDK)."""
    S, U, J, E = model.dims
    width = S + U + J + E
    plan = Plan.make(model.info, len(audio16k))
    if plan.n_frames == 0:
        raise ValueError("audio is too short for Audio2Face-3D (no output frame)")
    pinfo = model.info["params"]
    steps, layers, latent = pinfo["num_diffusion_steps"], pinfo["num_gru_layers"], pinfo["gru_latent_dim"]
    total = plan.left + plan.center + pinfo["num_frames_right_truncate"]
    shape = (1, steps + 1, total, width)
    own = noise is None
    if own:
        noise = DiffusionNoise(seed, shape)
    identity = np.zeros((1, len(pinfo["identities"])), dtype=np.float32)
    identity[0, model.identity_index] = 1.0
    emo = np.broadcast_to(emotion.astype(np.float32), (1, plan.center, len(emotion))).copy()
    state = np.zeros((layers, 2, 1, latent), dtype=np.float32)
    buf = plan.windows.size
    sel = model.skin.mask if not full_geometry else None
    skin_cols = np.arange(S) if sel is None else sel
    raw = np.zeros((plan.n_frames, width), dtype=np.float32) if full_geometry else None
    skin_d = np.zeros((plan.n_frames, len(skin_cols)), dtype=np.float32)
    tongue_d = np.zeros((plan.n_frames, U), dtype=np.float32)
    eyes_raw = np.zeros((plan.n_frames, E), dtype=np.float32)
    strength = float(model.config.get("input_strength", 1.0))
    try:
        for k in range(plan.n_inferences):
            start = plan.windows.start(k)
            window = np.zeros((1, buf), dtype=np.float32)
            a, b = max(start, 0), min(start + buf, len(audio16k))
            if b > a:
                window[0, a - start:b - start] = audio16k[a:b]
            if strength != 1.0:
                window *= strength
            nz = noise(k, shape)
            if own and k + 1 < plan.n_inferences:
                noise.prefetch(k + 1)                                 # drawn while the network runs
            pred, state = session.run(["prediction", "output_latents"], {
                "window": window, "identity": identity, "emotion": emo, "input_latents": state,
                "noise": np.ascontiguousarray(nz, dtype=np.float32)})
            for row, f in plan.frames_of(k):
                r = pred[0, row]
                skin_d[f] = r[:S][skin_cols]
                tongue_d[f] = r[S:S + U]
                eyes_raw[f] = r[S + U + J:S + U + J + E]
                if raw is not None:
                    raw[f] = r
            if progress is not None:
                progress(k + 1, plan.n_inferences)
    finally:
        if own:
            noise.close()
    dt = 1.0 / plan.fps
    mask_lower = face_mask_lower(model.neutral_skin, p["face_mask_level"], p["face_mask_softness"])
    vert = skin_cols // 3
    skin = skin_post(skin_d.astype(np.float64), model.neutral_skin[skin_cols], model.eye_close[skin_cols],
                     model.lip_open[skin_cols], mask_lower[vert], p, dt)
    return dict(plan=plan, samples=plan.timestamps(), skin=skin, skin_cols=skin_cols,
                tongue=tongue_post(tongue_d.astype(np.float64), model.neutral_tongue, p),
                eyes=eyes_post(eyes_raw.astype(np.float64), model.saccade, p, dt), raw=raw)


def infer(audio: np.ndarray, sr: int, cfg: "AudioConfig", *, log: Callable = print,
          progress: Callable | None = None) -> A2FResult:
    """Mono float audio at sr -> A2FResult at the model's native 60 fps (t[0] = 0 = first sample)."""
    t0 = time.perf_counter()
    download(cfg.identity, log=log)
    model = load_model(cfg.identity)
    session, device = _session(MODEL_DIR / "network.onnx", cfg.device, log)
    sr_model = model.info["audio_params"]["samplerate"]
    audio16k = resample(audio, sr, sr_model)
    p = model_params(model, cfg)
    emotion = emotion_vector(model.info, cfg.emotion, cfg.emotion_strength)
    log(f"Audio2Face-3D v3.0 ({cfg.identity}, {cfg.emotion}, {device}) on {len(audio16k) / sr_model:.1f} s of audio")
    out = run(audio16k, model, session, p, emotion, cfg.seed, progress=progress)
    skin_w = model.skin.solve(out["skin"])
    tongue_w = model.tongue.solve(model.tongue.masked(out["tongue"]))
    bs = {n: skin_w[:, i].astype(np.float32) for i, n in enumerate(model.skin.names)}
    for n in ("eyeBlinkLeft", "eyeBlinkRight"):
        bs[n] = np.clip(bs[n] * cfg.blink_strength, 0.0, 1.0).astype(np.float32)
    plan: Plan = out["plan"]
    runtime = time.perf_counter() - t0
    meta = dict(model=REPO, revision=REVISION, network_version=model.info["id"].get("version"),
                identity=cfg.identity, emotion=cfg.emotion, emotion_strength=cfg.emotion_strength,
                emotion_vector=dict(zip(model.info["params"]["emotions"], map(float, emotion))),
                device=device, seed=cfg.seed, inferences=plan.n_inferences, frames=plan.n_frames,
                audio_seconds=len(audio16k) / sr_model, runtime_s=round(runtime, 3))
    return A2FResult(fps=plan.fps, t=out["samples"] / sr_model, blendshapes=bs,
                     eyes=to_yaw_pitch(out["eyes"]).astype(np.float32), meta=meta,
                     tongue={n: tongue_w[:, i].astype(np.float32) for i, n in enumerate(model.tongue.names)})
