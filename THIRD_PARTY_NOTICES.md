# Third-party notices

video2live2d (V2L; Python package and command `v2l`) is MIT-licensed (see [`LICENSE`](LICENSE)).
Apart from the icons and the ported code listed under "Included in this repository", it does
**not** ship third-party files. The other components below are installed by package managers or
downloaded on first use, under their own licenses. Using them means accepting those licenses.

## Included in this repository

V2L Studio inlines 27 [Lucide](https://lucide.dev) icons as SVG markup (in `app/renderer/`):
`settings`, `ellipsis`, `sliders-horizontal`, `x`, `rotate-ccw`, `lock`, `check`, `undo-2`,
`redo-2`, `plus`, `trash-2`, `magnet`, `scan`, `eye`, `eye-off`, `circle-help`, `spline`, `copy`,
`clipboard-paste`, `timer`, `waves`, `folder-open`, `video`, `mic`, `audio-lines`, `play` and
`pause`. Of these, `ellipsis` (formerly `more-horizontal`), `x`, `lock`, `check`, `plus`, `trash-2`
and `circle-help` (formerly `help-circle`) are derived from [Feather](https://feathericons.com).
The interpolation and step glyphs are V2L's own drawings.

```text
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

```text
The MIT License (MIT) (for the Feather-derived icons listed above)

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

[`src/v2l/a2f.py`](src/v2l/a2f.py) is a Python port of parts of NVIDIA's
[Audio2Face-3D-SDK](https://github.com/NVIDIA/Audio2Face-3D-SDK) (commit `1ca0f02`): the audio
windowing, the diffusion inference loop and the skin, tongue and eye post-processing with the
blendshape solve. The ported files are listed in the module docstring.

```text
MIT License

Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Downloaded on first use

| Component | Used for | Source | License |
|---|---|---|---|
| Live2D Cubism Core for Web (`live2dcubismcore.min.js`) | loading and drawing Cubism models (renderer and Studio) | `https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js` | [Live2D Proprietary Software License](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html) |
| PixiJS 6.5.10 (`pixi.min.js`) | WebGL rendering | npm `pixi.js@6.5.10` via jsDelivr, pinned by SHA-256 in [`src/v2l/web/assets.json`](src/v2l/web/assets.json) | MIT, Copyright (c) 2013-2017 Mathew Groves, Chad Engler |
| pixi-live2d-display 0.4.0 (`cubism4.min.js`) | Live2D models in PixiJS | npm `pixi-live2d-display@0.4.0` via jsDelivr, pinned by SHA-256 | MIT, Copyright (c) 2020 Guan. The bundle includes the Live2D Cubism Framework, which is under the [Live2D Open Software License](https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html) |
| MediaPipe Face Landmarker and Pose Landmarker task models | face blendshapes, head transform, landmarks, shoulders | `storage.googleapis.com/mediapipe-models/` | Apache-2.0 |
| SigLIP 2 (`google/siglip2-so400m-patch14-384`), optional | zero-shot cheek-puff / tongue-out scoring | Hugging Face | Apache-2.0 |
| NVIDIA Audio2Face-3D v3.0 (`nvidia/Audio2Face-3D-v3.0`), audio input only | speech audio -> ARKit blendshapes and eye rotation | Hugging Face, pinned revision, files pinned by SHA-256 in [`src/v2l/a2f.py`](src/v2l/a2f.py) | [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/). Licensed by NVIDIA Corporation under the NVIDIA Open Model License |

Downloads are cached under `~/.cache/v2l` (Python side) and in the Electron user-data folder
(V2L Studio). `V2L_CUBISM_CORE=/path/to/live2dcubismcore.min.js` makes the Python side use a local
copy of Cubism Core instead.

## Installed by package managers

Python (see [`pyproject.toml`](pyproject.toml)): MediaPipe (Apache-2.0), OpenCV
`opencv-python-headless` (Apache-2.0; the wheels bundle further libraries under their own licenses),
NumPy (BSD-3-Clause), SciPy (BSD-3-Clause), Playwright for Python (Apache-2.0), imageio-ffmpeg
(BSD-2-Clause; provides an FFmpeg binary under the FFmpeg licenses), pytest (MIT). Optional: PyTorch
(BSD-3-Clause), Transformers (Apache-2.0), the MCP Python SDK `mcp` (MIT), ONNX Runtime
`onnxruntime` / `onnxruntime-gpu` (MIT; the `[cuda,cudnn]` extra installs NVIDIA CUDA and cuDNN
runtime wheels under NVIDIA's license terms).

Node.js (see [`app/package.json`](app/package.json)): Electron (MIT; bundles Chromium and Node.js
under their own licenses).

Rendering also uses an installed Google Chrome, under Google's terms.

## Compatibility, not copied code

- The bridge formula language follows the syntax documented by
  [SnenkBridge](https://github.com/FaeyUmbrea/SnenkBridge). V2L's interpreter is its own
  implementation; no SnenkBridge code or presets are included. Presets by other authors can be
  loaded with `--bridge FILE` and remain under their authors' terms.
- V2L Studio's neutral colors in both themes and the dark theme's accent follow the gray and
  magenta scales of [Adobe Spectrum 2 design tokens](https://github.com/adobe/spectrum-tokens)
  (Apache-2.0); the light theme's accent is V2L's coral `#FD7688` with shades derived from it. No
  Spectrum files are included.
- The built-in bridge preset ([`src/v2l/bridges/default.json`](src/v2l/bridges/default.json)) was
  written for V2L.
- VBridger is the reference workflow (ARKit blendshapes -> Live2D parameters). No VBridger code
  or data is included.

## Your own material

Live2D models, videos and voice recordings of people and generated motions are not part of this
repository. You need the rights to the models you animate and the consent of the people you
record. Publishing works made with Live2D software may require separate agreements with Live2D
(for example the Live2D Publication License); check Live2D's terms for your use.

Live2D and Cubism are trademarks of Live2D Inc. VBridger, VTube Studio, ARKit, MediaPipe, NVIDIA,
Audio2Face and other names are trademarks of their respective owners. Their use here is
descriptive only.
