"""Regression run over regression/videos/manifest.json (on the compute machine).

The clips are your own; none are distributed. manifest.json is a JSON list of
{"id": str, "file": str (relative to regression/videos), "conditions": [str, ...]}.
For every clip: v2l run with the given model, a contact sheet of the preview, and one row in
results/summary.json (detection rates, blinks, range use of key parameters, warnings).
usage: python regression/run.py --model DIR [--style cute] [--config FILE]...
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v2l.config import ConfigError, build, merge  # noqa: E402
from v2l.pipeline import run  # noqa: E402
from v2l.render import FFMPEG  # noqa: E402

KEY_PARAMS = ["ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamEyeLOpen", "ParamMouthOpenY", "ParamMouthForm",
              "ParamEyeBallX", "ParamBodyAngleX"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Live2D model folder")
    ap.add_argument("--style", default="cute")
    ap.add_argument("--config", action="append", default=[], help="v2l configuration file (repeatable)")
    a = ap.parse_args()
    try:
        cfg = merge(build(a.config), {"style": {"name": a.style}, "preview": {"enabled": True}})
    except ConfigError as e:
        ap.error(str(e))
    videos = ROOT / "regression" / "videos"
    out = ROOT / "regression" / "results"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    failed = 0
    for clip in json.loads((videos / "manifest.json").read_text()):
        name = f"reg_{clip['id']}"
        t0 = time.time()
        try:
            rep = run(videos / clip["file"], a.model, name, out, cfg, install=False)
        except Exception as e:  # one bad clip must not hide the others
            failed += 1
            rows.append({"id": clip["id"], "error": f"{type(e).__name__}: {e}"})
            print(f"FAIL {clip['id']}: {e}", flush=True)
            continue
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(out / f"{name}.preview.mp4"), "-vf",
                        "fps=1/2,scale=-2:300,tile=4x2", "-frames:v", "1", str(out / f"{name}.sheet.jpg")], check=False)
        rows.append({
            "id": clip["id"], "conditions": clip.get("conditions", []), "seconds": round(time.time() - t0, 1),
            "face_rate": round(rep["face_rate"], 3), "pose_rate": round(rep["pose_rate"], 3),
            "blinks": rep["blinks"], "neutral_window_s": rep["neutral_window_s"],
            "use": {p: round(rep["params"][p]["use"], 3) for p in KEY_PARAMS if p in rep["params"]},
            "warnings": rep["warnings"],
        })
        print(f"ok   {clip['id']}  {rows[-1]['seconds']} s  warnings={len(rep['warnings'])}", flush=True)
    (out / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
