"""Downloaded third-party assets (tracker models, Cubism Core, renderer libraries), cached under ~/.cache/v2l."""

from __future__ import annotations

import hashlib
import json
import pathlib
import urllib.request

CACHE = pathlib.Path.home() / ".cache" / "v2l"
WEB_ASSETS = pathlib.Path(__file__).parent / "web" / "assets.json"


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url: str, path: pathlib.Path, sha256: str | None = None) -> pathlib.Path:
    """Fetches url to path once (atomic rename). Some hosts reject urllib's default user agent.

    With sha256, a cached file that does not match is fetched again, and a download that does
    not match is rejected.
    """
    if path.exists() and (sha256 is None or _sha256(path) == sha256):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (v2l)"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        f.write(r.read())
    if sha256 is not None and _sha256(tmp) != sha256:
        tmp.unlink()
        raise RuntimeError(f"{url} does not match its pinned SHA-256")
    tmp.rename(path)
    return path


def web_assets() -> dict[str, pathlib.Path]:
    """Renderer libraries pinned by version and SHA-256 in web/assets.json (pixi.js, pixi-live2d-display)."""
    manifest = json.loads(WEB_ASSETS.read_text())
    return {name: download(e["url"], CACHE / "web" / name, e["sha256"]) for name, e in manifest.items()}
