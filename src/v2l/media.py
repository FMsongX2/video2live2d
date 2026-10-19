"""Media files: what an input is (video or audio), what it contains, and its audio as samples.

Standard library at import time (numpy only inside decode), so the laptop side (v2l-remote, Studio,
the MCP server) can classify inputs. ffmpeg is the bundled imageio-ffmpeg binary when installed,
else an ffmpeg on PATH; without either, probe() returns None and the kind follows the extension.
"""

from __future__ import annotations

import functools
import pathlib
import re
import shutil
import subprocess

AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus")
SAMPLE_RATE = 16000  # what Audio2Face-3D consumes


@functools.cache
def ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - not installed or no binary for this platform
        return shutil.which("ffmpeg")


def probe(path: str | pathlib.Path) -> dict | None:
    """{"duration_s", "has_video", "has_audio", "sample_rate", "channels"} read from `ffmpeg -i`'s
    stream listing; None when no ffmpeg is available. Cover art (attached pictures) is not video."""
    exe = ffmpeg()
    if exe is None:
        return None
    r = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace")
    text = r.stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    video = [ln for ln in text.splitlines() if re.search(r"Stream #.*: Video:", ln) and "attached pic" not in ln]
    audio = [ln for ln in text.splitlines() if re.search(r"Stream #.*: Audio:", ln)]
    rate = channels = None
    if audio:
        if m := re.search(r"(\d+) Hz", audio[0]):
            rate = int(m[1])
        a = audio[0]
        channels = (1 if "mono" in a else 2 if "stereo" in a else
                    int(m[1]) if (m := re.search(r"(\d+) channels", a)) else
                    6 if "5.1" in a else 8 if "7.1" in a else None)
    if duration is None and not video and not audio:
        return None if "Invalid data" not in text and "No such file" not in text else \
            {"duration_s": None, "has_video": False, "has_audio": False, "sample_rate": None, "channels": None}
    return {"duration_s": duration, "has_video": bool(video), "has_audio": bool(audio), "sample_rate": rate,
            "channels": channels}


def kind(path: str | pathlib.Path, info: dict | None = None) -> str:
    """"audio" for an audio file (by extension, or a container without a video stream), else "video"."""
    if pathlib.Path(path).suffix.lower() in AUDIO_EXTS:
        return "audio"
    if info is not None and info["has_audio"] and not info["has_video"]:
        return "audio"
    return "video"


def plan(source: str | pathlib.Path, audio: str | pathlib.Path | None, use: str) -> dict:
    """What a run uses: {"kind": video | audio | video+audio, "video", "audio" (separate audio file
    or the audio-only input), "audio_from": file | video | None, "use": off | mouth | face | all,
    "has_audio": bool | None (None: no ffmpeg to look), "duration_s"}. use is audio.use.
    Raises ValueError for a combination that cannot work."""
    src = pathlib.Path(source).expanduser().resolve()
    info = probe(src) if src.is_file() else None
    k = kind(src, info)
    sep = pathlib.Path(audio).expanduser().resolve() if audio else None
    if k == "audio":
        if sep is not None:
            raise ValueError("--audio is for video input; the input is already an audio file")
        return {"kind": "audio", "video": None, "audio": str(src), "audio_from": "file", "use": "all",
                "has_audio": info["has_audio"] if info else None,
                "duration_s": info["duration_s"] if info else None}
    if sep is not None and use == "off":
        raise ValueError("--audio needs audio.use=mouth or audio.use=face (e.g. --audio-use mouth)")
    if use == "off":
        return {"kind": "video", "video": str(src), "audio": None, "audio_from": None, "use": "off",
                "has_audio": info["has_audio"] if info else None,
                "duration_s": info["duration_s"] if info else None}
    ainfo = (probe(sep) if sep.is_file() else None) if sep is not None else info
    return {"kind": "video+audio", "video": str(src), "audio": str(sep) if sep else None,
            "audio_from": "file" if sep else "video", "use": use,
            "has_audio": ainfo["has_audio"] if ainfo else None,
            "duration_s": info["duration_s"] if info else None}


def decode(path: str | pathlib.Path, start: float = 0.0, duration: float | None = None, sr: int = SAMPLE_RATE):
    """Mono float32 samples at sr of [start, start + duration) seconds (to the end without duration).
    All channels are mixed down. Raises ValueError when the file has no audio."""
    import numpy as np

    exe = ffmpeg()
    if exe is None:
        raise RuntimeError("ffmpeg not found (install imageio-ffmpeg or put ffmpeg on PATH)")
    cmd = [exe, "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{max(duration, 0.0):.6f}"]
    cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        msg = r.stderr.decode(errors="replace").strip()
        if "matches no streams" in msg or "does not contain any stream" in msg:
            raise ValueError(f"{pathlib.Path(path).name} has no audio track")
        raise RuntimeError(f"ffmpeg could not decode the audio of {path}: {msg[-300:]}")
    return np.frombuffer(r.stdout, dtype="<f4").astype(np.float32)
