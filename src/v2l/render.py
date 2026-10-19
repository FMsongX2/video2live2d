"""Headless Live2D rendering: parameter ranges, screen-direction probe, motion playback video.

Chrome (system install, driven by Playwright) runs pixi-live2d-display with the official Cubism
Core. Rendering uses SwiftShader: ANGLE/Vulkan on an NVIDIA GPU lost the WebGL context with
several 8K textures, the CPU rasteriser does not.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import http.server
import os
import pathlib
import subprocess
import threading
import urllib.parse

import cv2
import imageio_ffmpeg
import numpy as np
from playwright.sync_api import sync_playwright

from .assets import CACHE, download, web_assets
from .model import Model

WEB = pathlib.Path(__file__).parent / "web"
CORE_URL = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"
CHROME_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def cubism_core() -> pathlib.Path:
    """Live2D Cubism Core for Web (Live2D Proprietary Software License): fetched, not shipped."""
    env = os.environ.get("V2L_CUBISM_CORE")
    if env:
        return pathlib.Path(env)
    return download(CORE_URL, CACHE / "live2dcubismcore.min.js")


class _Handler(http.server.SimpleHTTPRequestHandler):
    model_dir: pathlib.Path
    core: pathlib.Path
    libs: dict[str, pathlib.Path]

    def log_message(self, *args) -> None:
        pass

    def translate_path(self, path: str) -> str:
        path = urllib.parse.unquote(path.split("?", 1)[0])
        if path == "/core.js":
            return str(self.core)
        if path.startswith("/lib/"):
            lib = self.libs.get(path[len("/lib/"):])
            return str(lib) if lib else str(WEB / "missing")
        if path.startswith("/model/"):
            return str(self.model_dir / path[len("/model/"):])
        return str(WEB / path.lstrip("/"))


@contextlib.contextmanager
def page_for(model: Model, width: int, height: int, motion_file: str | None = None):
    handler = type("H", (_Handler,), {"model_dir": model.dir, "core": cubism_core(), "libs": web_assets()})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(handler, directory=str(WEB)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True, args=CHROME_ARGS)
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{httpd.server_address[1]}/index.html")
                page.wait_for_function("window.ready === true")
                size = page.evaluate("o => init(o)", {
                    "modelUrl": f"/model/{model.settings_file.name}", "motionFile": motion_file,
                    "width": width, "height": height})
                if errors:
                    raise RuntimeError("renderer page error: " + "; ".join(errors))
                page.model_size = size
                yield page
            finally:
                browser.close()
    finally:
        httpd.shutdown()


def _decode(data_url: str, flags=cv2.IMREAD_COLOR) -> np.ndarray:
    buf = np.frombuffer(base64.b64decode(data_url.split(",", 1)[1]), np.uint8)
    return cv2.imdecode(buf, flags)


def read_ranges(model: Model) -> dict[str, dict]:
    with page_for(model, 64, 64) as page:
        return {p["id"]: {"min": p["min"], "max": p["max"], "def": p["def"]} for p in page.evaluate("inspect()")}


def _change_centroid(a: np.ndarray, b: np.ndarray, near: tuple[float, float] | None = None,
                     reach: float = 100.0) -> tuple[float, float] | None:
    """Centre of the pixels that differ between two renders; None when nothing visibly changed.

    With near, one connected patch of change is kept: the one with the most change, discounted by
    distance from `near` (scale `reach` pixels). Closing an eye can also flick physics-linked
    ears or hair clips; the eyelid is the strongest change close to the eye.
    """
    diff = cv2.absdiff(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)).astype(np.float32)
    changed = diff > 8
    if changed.sum() < 20:
        return None
    if near is not None:
        n, labels, stats, cents = cv2.connectedComponentsWithStats(cv2.dilate(changed.astype(np.uint8), np.ones((9, 9), np.uint8)))
        if n > 1:
            mass = np.bincount(labels.ravel(), weights=(diff * changed).ravel(), minlength=n)
            score = [mass[k] * np.exp(-np.hypot(cents[k][0] - near[0], cents[k][1] - near[1]) / reach) for k in range(1, n)]
            changed &= labels == 1 + int(np.argmax(score))
    yy, xx = np.nonzero(changed)
    return float(xx.mean()), float(yy.mean())


def _dark_shift(a: np.ndarray, b: np.ndarray, roi: tuple) -> tuple[float, float]:
    """Motion of the dark mass (iris, mouth line) inside roi from render a to render b.

    Pixels that got darker mark where the feature arrived, pixels that got lighter where it left.
    """
    x0, y0, x1, y1 = (max(int(v), 0) for v in roi)
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)[y0:y1, x0:x1].astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)[y0:y1, x0:x1].astype(np.float32)
    darker, lighter = np.clip(ga - gb, 0, None), np.clip(gb - ga, 0, None)
    darker[darker < 8] = 0
    lighter[lighter < 8] = 0
    if darker.sum() == 0 or lighter.sum() == 0:
        return 0.0, 0.0
    ys, xs = np.mgrid[0:ga.shape[0], 0:ga.shape[1]]

    def cen(wgt):
        return (xs * wgt).sum() / wgt.sum(), (ys * wgt).sum() / wgt.sum()

    (ax, ay), (bx, by) = cen(darker), cen(lighter)
    return float(ax - bx), float(ay - by)


# Parameters whose screen direction the mapping needs.
HEAD_BODY = ["ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ"]
FEATURE = {"ParamEyeBallX": "eyes", "ParamEyeBallY": "eyes", "ParamMouthX": "mouth"}


class _Prober:
    def __init__(self, page, model: Model, settle: int):
        self.page, self.model, self.settle = page, model, settle
        self.eye_ids = [p for p in ("ParamEyeLOpen", "ParamEyeROpen") if p in model.ranges]
        self.expect: dict[str, tuple[float, float]] = {}  # where each eye should be in the view
        self.reach = 100.0  # pixels; set to the eye spacing once the view is zoomed
        self.debug = os.environ.get("V2L_PROBE_DEBUG")

    def render(self, pose: dict, physics: bool, tag: str | None = None, steps: int | None = None) -> np.ndarray:
        self.page.evaluate(f"setPhysics({'true' if physics else 'false'})")
        n = steps if steps is not None else (self.settle if physics else 2)
        img = _decode(self.page.evaluate("([p, n]) => poseImage(p, n)", [pose, n]))
        if self.debug and tag:
            pathlib.Path(self.debug).mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(pathlib.Path(self.debug) / f"{tag}.jpg"), img)
        return img

    def eyes(self, pose: dict, physics: bool) -> dict[str, tuple[float, float]] | None:
        """Image position of each eye-open parameter's eye in a pose, found by closing it.

        Both the open and the closed picture are rendered at the physics steady state (damped
        physics converges to the same rest whatever the start), so they differ only by what the
        eyelid moves; closing an eye can also flick physics-linked ears, so the patch of change
        nearest to where the eye is expected is the one kept.
        """
        base = self.render(pose, physics)
        found = {}
        tag = "_".join(f"{k}{v:+.0f}" for k, v in pose.items()) or "default"
        for pid in self.eye_ids:
            closed = self.render({**pose, pid: self.model.ranges[pid]["min"]}, physics)
            c = _change_centroid(base, closed, near=self.expect.get(pid), reach=self.reach)
            if self.debug:
                dbg = np.hstack([base, closed])
                if c is not None:
                    cv2.circle(dbg, (int(c[0]), int(c[1])), 6, (0, 0, 255), 2)
                cv2.imwrite(str(pathlib.Path(self.debug) / f"eyes_{tag}_{pid}.jpg"), dbg)
            if c is None:
                return None
            found[pid] = c
        return found if len(found) == 2 else None

    def head(self, pid: str, lo: float, hi: float) -> dict:
        """Face motion for a head/body parameter, read from the eye anchors with physics settled."""
        a, b = self.eyes({pid: lo}, True), self.eyes({pid: hi}, True)
        if not a or not b:
            return {}
        a, b = sorted(a.values()), sorted(b.values())

        def mid(e):
            return (e[0][0] + e[1][0]) / 2, (e[0][1] + e[1][1]) / 2

        def ang(e):  # image y points down, so a positive change is clockwise on screen
            return float(np.arctan2(e[1][1] - e[0][1], e[1][0] - e[0][0]))

        (ax, ay), (bx, by) = mid(a), mid(b)
        rot = ang(b) - ang(a)
        if pid.endswith("Z") and abs(rot) < 0.01:
            # A tilt about a pivot below the face (hips, neck) shows up as a sideways shift of the
            # face more than as an eye-line tilt; a face moving right means clockwise.
            rot = 0.011 * float(np.sign(bx - ax))
        return {"dx": bx - ax, "dy": by - ay, "rot": rot}


def probe_model(model: Model, size: int = 640, settle: int = 300) -> dict:
    """Measures how each mapped parameter changes the drawing on screen.

    1. Eyes on the whole model (closing each eye-open parameter, physics off) give the face
       position, the eye spacing and the preview framing; the view then zooms onto the face.
    2. Head and body parameters: the eyes are found again in the lo and hi pose with physics
       settled (many rigs turn the head through physics-driven copies of the angle parameters);
       the eye midpoint shift is dx/dy and the eye-line angle change is rot. Dense optical flow
       was tried first and followed the back hair, which moves against the face.
    3. Eyeball and mouth X/Y: dark-mass shift inside the eye or mouth boxes.
    4. Every parameter: the image side of its change relative to the eye midpoint, measured with
       physics off, or settled physics on when the parameter only acts through physics.
    dx, dy are pixels (y down); rot is radians (+ = clockwise on screen).
    """
    from .mapping import entries_for  # local import: mapping does not depend on rendering

    targets = list(dict.fromkeys(HEAD_BODY + list(FEATURE) + [e.output for e in entries_for(model)]))
    targets = [p for p in targets if p in model.ranges]
    with page_for(model, size, size) as page:
        pr = _Prober(page, model, settle)
        w, h = page.model_size["width"], page.model_size["height"]
        whole = {"cx": 0.5, "cy": 0.5, "h": max(1.0, w / h)}
        page.evaluate("f => frame(f)", whole)
        scale = size / (whole["h"] * h)
        x0, y0 = size / 2 - 0.5 * w * scale, size / 2 - 0.5 * h * scale
        found = pr.eyes({}, False)
        eyes = sorted(((x - x0) / (w * scale), (y - y0) / (h * scale)) for x, y in found.values()) if found else []
        zoom = framing = None
        spacing = 0.0
        if eyes:
            spacing = max(abs(eyes[1][0] - eyes[0][0]) * w / h, 0.01)  # in fractions of the model height
            mid_x, eye_y = (eyes[0][0] + eyes[1][0]) / 2, (eyes[0][1] + eyes[1][1]) / 2
            zoom = {"cx": mid_x, "cy": eye_y + 0.8 * spacing, "h": 4.0 * spacing}
            framing = {"cx": mid_x, "cy": eye_y + 1.3 * spacing, "h": 8.0 * spacing}
            # Default eye positions in the zoomed view: anchors for finding the eyes again.
            s2 = size / (zoom["h"] * h)
            for pid, (px, py) in found.items():
                fx, fy = (px - x0) / (w * scale), (py - y0) / (h * scale)
                pr.expect[pid] = (size / 2 + (fx - zoom["cx"]) * w * s2, size / 2 + (fy - zoom["cy"]) * h * s2)
            pr.reach = size * spacing / zoom["h"]
        page.evaluate("f => frame(f)", zoom or whole)
        sp = size * spacing / zoom["h"] if zoom else 0.1 * size
        mx, ey = size / 2, size / 2 - 0.8 * sp
        boxes = {
            "eyes": [(mx - 0.85 * sp, ey - 0.35 * sp, mx - 0.15 * sp, ey + 0.35 * sp),
                     (mx + 0.15 * sp, ey - 0.35 * sp, mx + 0.85 * sp, ey + 0.35 * sp)],
            "mouth": [(mx - 0.5 * sp, ey + 0.6 * sp, mx + 0.5 * sp, ey + 1.3 * sp)],
        }
        out: dict[str, dict] = {}
        for pid in targets:
            r = model.ranges[pid]
            span = r["max"] - r["min"]
            lo, hi = max(r["min"], r["def"] - 0.3 * span), min(r["max"], r["def"] + 0.3 * span)
            a = pr.render({pid: lo}, False, f"{pid}_lo")
            b = pr.render({pid: hi}, False, f"{pid}_hi")
            c = _change_centroid(a, b)
            if c is None:  # some rigs move a feature only through physics
                a, b = pr.render({pid: lo}, True), pr.render({pid: hi}, True)
                c = _change_centroid(a, b)
            d: dict = {"visible": c is not None}
            if c is not None:
                d["cx"], d["cy"] = c
                off = c[0] - mx
                d["side"] = "left" if off < -0.25 * sp else "right" if off > 0.25 * sp else "center"
            if pid in HEAD_BODY:
                d.update(pr.head(pid, lo, hi))
                d["visible"] = "dx" in d
            elif pid in FEATURE and c is not None:
                shifts = [_dark_shift(a, b, box) for box in boxes[FEATURE[pid]]]
                d["dx"] = float(np.mean([s[0] for s in shifts]))
                d["dy"] = float(np.mean([s[1] for s in shifts]))
                d["rot"] = 0.0
            out[pid] = d
    # Directions smaller than these are treated as unknown (no correction applied).
    return {"params": out, "min_motion": 0.5, "min_rot": 0.01, "eyes": eyes, "zoom": zoom, "framing": framing,
            "probe_size": size}


def render_motion(model: Model, motion_file: str, out_mp4: pathlib.Path, fps: float, framing: dict | None,
                  src_video: str | None = None, src_start: float = 0.0, width: int = 720, height: int = 900,
                  progress=None, audio: str | None = None) -> int:
    """Plays motion_file (inside the model folder) with physics and encodes an mp4.

    With src_video the source clip is placed on the left at the same height, with its own sound.
    With audio that file's sound (from src_start) is muxed instead (audio-only input, or a video
    driven by a separate audio track). progress(done, total) is called after every rendered frame.
    """
    import json

    motion = json.loads((model.dir / motion_file).read_text())
    duration = motion["Meta"]["Duration"]
    n = int(round(duration * fps)) + 1
    frames = out_mp4.with_suffix(".frames")
    frames.mkdir(parents=True, exist_ok=True)
    with page_for(model, width, height, motion_file) as page:
        page.evaluate("f => frame(f)", framing or {"cx": 0.5, "cy": 0.3, "h": 0.6})
        if not page.evaluate("play()"):
            raise RuntimeError("motion did not start")
        for i in range(n):
            (frames / f"{i:05d}.jpg").write_bytes(base64.b64decode(page.evaluate(f"step({1000.0 / fps})").split(",", 1)[1]))
            if progress:
                progress(i + 1, n)
    rendered = ["-framerate", str(fps), "-i", str(frames / "%05d.jpg")]
    sound = ["-ss", f"{src_start:.3f}", "-i", audio] if audio else []
    encode = ["-t", f"{duration:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "21", "-c:a", "aac",
              str(out_mp4)]
    if src_video:
        cmd = [FFMPEG, "-y", "-v", "error", "-ss", f"{src_start:.3f}", "-i", src_video, *rendered, *sound,
               "-filter_complex",
               f"[0:v]fps={fps},scale=-2:{height}[a];[a][1:v]hstack=inputs=2,scale=trunc(iw/2)*2:trunc(ih/2)*2[v]",
               "-map", "[v]", "-map", "2:a:0" if audio else "0:a?", *encode]
    elif audio:
        cmd = [FFMPEG, "-y", "-v", "error", *rendered, *sound, "-map", "0:v", "-map", "1:a:0", *encode]
    else:
        cmd = [FFMPEG, "-y", "-v", "error", *rendered, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "21", str(out_mp4)]
    subprocess.run(cmd, check=True)
    for f in frames.iterdir():
        f.unlink()
    frames.rmdir()
    return n
