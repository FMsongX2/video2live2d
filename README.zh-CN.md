<div align="center">

<img src="docs/assets/banner.svg" alt="video2live2d：视频帧、面部关键点和 Live2D 参数曲线" width="100%">

<p><strong>离线将真人录制视频或语音录音转换为 Live2D Cubism 动作。</strong></p>

<p>
<a href="https://github.com/FMsongX2/video2live2d/actions/workflows/ci.yml"><img src="https://github.com/FMsongX2/video2live2d/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
<img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" alt="Python 3.11+">
<img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey.svg" alt="Platform: Linux | macOS">
</p>

<p><a href="README.md">English</a> · <a href="README.ko.md">한국어</a> · <a href="README.ja.md">日本語</a> · <strong>简体中文</strong></p>

</div>

video2live2d（V2L）读取一段拍到真人面部和双肩的视频，为兼容 VBridger 的 Live2D 模型生成 Cubism `.motion3.json`。不需要 iPhone，也不需要实时追踪：录一段视频，运行一条命令，即可得到可在 VTube Studio 或任何 Cubism 运行时中播放的动作。借助可选的 Audio2Live2D 模式，也可以用语音音频代替视频（或与视频一起）驱动面部。产品名称为 V2L；Python 包和命令的名称为 `v2l`。

## 亮点

- **输入视频，输出动作。** 从普通录制的视频中生成头部和身体角度、眼睛与视线、眉毛、VBridger 嘴部参数组、肩膀和呼吸。
- **Audio2Live2D。** 语音音频通过 NVIDIA Audio2Face-3D v3.0 驱动面部（可选 `audio` extra）：仅有音频时可得到口型同步的面部，并生成头部、身体动作和眨眼；视频也可以从自身的声音或单独的音频文件获取嘴部或整个面部。
- **自动校准。** 以视频中活动最少的片段作为静止表情，替代 VBridger 的手动 Calibrate 按钮；也可以自行指定该片段。
- **沿用模型自身的设置。** 如果模型有 `*.vtube.json`（VTube Studio 设置），参数范围、限幅和呼吸设置取自该文件；否则使用标准 Cubism 参数 ID。
- **与绑定无关的方向检查。** 无头渲染探测会测量每个参数在屏幕上的实际效果，修正方向相反和左右眼对调的问题。结果按模型缓存。
- **SnenkBridge 格式的 bridge。** 内置的 VBridger 风格 bridge 由 SnenkBridge 公式语言的安全解释器求值，`--bridge FILE` 可加载同一格式的其他预设。
- **两种风格。** `cute`（默认）加入稍显开朗、生动的表演；`faithful` 不做任何添加。
- **一切皆可调。** 输入、校准、强度、平滑、风格、音频等 12 个分区共 90 项设置，来自 CLI、V2L Studio 和 MCP 服务器共用的同一模式；可通过内置预设和用户预设组合使用。
- **面向智能体。** `--json` 输出单个 JSON 结果对象，`--progress json` 输出 JSON Lines 进度事件，退出码有文档说明，并提供面向 AI 智能体的 MCP 服务器。
- **动作编辑。** 每个区段可选线性、贝塞尔、阶梯或反向阶梯的关键帧、贝塞尔控制柄、重新拟合和撤销，可在 V2L Studio 的曲线编辑器中或通过 `v2l motion` 操作完成。
- **三层映射编辑。** 参照 VBridger、SnenkBridge 和 VTube Studio 的参数设置：输入曲线；带公式、SnenkBridge 风格延迟缓冲和 VBridger 风格修饰器（延迟、平滑、阶梯）的 bridge 输出；以及按参数设置范围、反转、限幅、响应曲线和平滑截止频率的映射。
- **可选：鼓腮和吐舌。** 零样本 SigLIP 2 阶段为 MediaPipe 难以识别的表情打分。
- **预览与报告。** 原视频与模型并排的预览视频，以及包含检测率、眨眼、范围使用率和警告的 `report.json`。
- **笔记本 + 计算主机工作流。** 仅依赖标准库的 `bin/v2l-remote` 通过 SSH 在另一台机器上运行流水线；V2L Studio 将其封装为带有 Live2D 查看器、摄像头录制和语音录制功能的桌面应用。

## 工作原理

```mermaid
flowchart LR
    subgraph S1["1 · 追踪"]
        direction TB
        A["视频"] --> B["MediaPipe Face + Pose"]
        B --> C["图像空间特征"]
        A2["音频"] --> A3["Audio2Face-3D<br/>(可选)"]
    end
    subgraph S2["2 · 解算"]
        direction TB
        E["鼓腮 / 吐舌<br/>(SigLIP 2，可选)"] --> D["整段视频校准"]
        D --> P["强度"]
        P --> F["缺失片段处理"]
        F --> G["VBridger 风格 bridge"]
    end
    subgraph S3["3 · 重定向"]
        direction TB
        H["模型映射<br/>(*.vtube.json)"] --> I["渲染探测<br/>(方向与左右修正)"]
        I --> J["风格：cute / faithful"]
        J --> K["零相位滤波"]
    end
    subgraph S4["4 · 导出"]
        direction TB
        L["motion3.json"]
        M["preview.mp4"]
        N["report.json"]
    end
    S1 --> S2 --> S3 --> S4
```

| 阶段 | 源文件 | 作用 |
|---|---|---|
| 追踪 | `src/v2l/signals.py` | MediaPipe Face Landmarker（52 个 ARKit 命名的 blendshape、头部变换、478 个关键点）和 Pose Landmarker（肩膀）。 |
| 音频 | `src/v2l/media.py`, `src/v2l/a2f.py`, `src/v2l/audio.py` | 可选。将音频解码为 16 kHz 单声道，在 ONNX Runtime 上运行 NVIDIA Audio2Face-3D v3.0（60 fps 的 ARKit 命名 blendshape 和眼球旋转）；仅输入音频时，还会生成头部、身体动作和眨眼（`idle.*`）。见 [docs/audio.md](docs/audio.md)。 |
| 特征 | `src/v2l/features.py` | 头部 yaw/pitch/roll、单眼开合度、虹膜视线、下颌张开、内唇间隙、嘴宽、以面部为基准的嘴部横向位移、肩膀 roll/yaw/高度。 |
| 鼓腮 / 吐舌 | `src/v2l/rare.py` | 可选。每 N 帧用 SigLIP 2 进行零样本打分。 |
| 校准 | `src/v2l/calibrate.py` | 从整段视频中找出静止表情（活动最少的片段）以及各通道的响应幅度。 |
| 强度 | `src/v2l/calibrate.py` | 按 `intensity.*` 设置缩放各组动作（头部、身体、视线、眉毛、微笑、嘴部、鼓腮/吐舌）。 |
| 缺失片段 | `src/v2l/temporal.py` | 重采样到输出帧率；0.4 秒（`gaps.max_gap`）以内的缺失会插值，更长的缺失平滑回到静止姿态。 |
| Bridge | `src/v2l/bridge.py`, `src/v2l/bridges/default.json` | 用 SnenkBridge 公式语言的安全 AST 解释器计算 VBridger 风格的输出。映射文件可以修改或新增输出，并支持延迟缓冲和修饰器。 |
| 映射 | `src/v2l/mapping.py`, `src/v2l/model.py` | 复用模型的 VTube Studio 参数设置；没有时回退到标准 Cubism ID。映射文件（`--mapping`）可以修改任意条目或新增条目。 |
| 探测 | `src/v2l/render.py`, `src/v2l/web/` | 无头渲染模型，修正方向相反和左右眼对调。 |
| 风格 | `src/v2l/style.py` | `cute` 或 `faithful`。 |
| 导出 | `src/v2l/motion3.py`, `src/v2l/pipeline.py` | 按参数组进行零相位 Butterworth 滤波，用 Ramer-Douglas-Peucker 在各参数范围 0.4 % 以内简化的线性分段（`output.interpolation=bezier` 时为受限贝塞尔），校验，生成预览和报告。 |
| 设置 | `src/v2l/config.py`, `src/v2l/presets/` | 各阶段共用的设置模式、校验和预设。 |
| 编辑 | `src/v2l/motionedit.py`, `src/v2l/inputs.py` | 可编辑动作格式、编辑操作、拟合和校验，以及映射可使用的输入列表。 |

物理输出参数交由运行时处理。探测和预览的渲染由 Playwright 驱动已安装的 Google Chrome，并使用 SwiftShader（CPU 渲染），因为 GPU WebGL 后端在超大（8K）纹理下会丢失上下文。

## 环境要求

**计算主机**（运行 `v2l`）

- Linux（x86_64 或 arm64）或 Apple silicon 的 macOS，Python 3.11 及以上。MediaPipe 未发布 Intel macOS 的 wheel。
- 已安装 Google Chrome（Playwright 以 `channel="chrome"` 启动）。
- 无需另装 ffmpeg：`imageio-ffmpeg` 自带可执行文件。
- 可选：用于鼓腮/吐舌阶段的 NVIDIA GPU。空闲显存不少于 3 GB 时使用 GPU，否则在 CPU 上运行，每个打分帧约 0.8 秒。
- 可选，用于音频输入（`audio` extra）：ONNX Runtime 1.31。在 Linux 和 Windows x86_64 上，该 extra 安装 CUDA 13 版本，需要 NVIDIA 驱动 580 或更高版本，不可用时回退到 CPU；macOS 和 ARM 安装 CPU 版本。约 10–14 秒的语音在 CUDA GPU 上约需 2.5–3 秒，在 Apple silicon CPU 上约需 3–5 秒。

**笔记本**（用于远程工作流和 V2L Studio）

- V2L Studio：Node.js 和 npm，以及 `python3`。`PATH` 中的 `ffmpeg` 为可选项，Studio 用它对摄像头录制进行重新封装，以便正确拖动定位。
- 远程模式（`bin/v2l-remote`、Studio 的 **远程服务器**、`bin/v2l-mcp`）：`python3`、`rsync`，以及可通过密钥登录计算主机的 `ssh`。
- 本地模式（Studio 的 **本机**）：在同一台机器上满足上述计算主机的要求。

计算主机和笔记本可以是同一台机器。

## 快速开始

### A. 在计算主机上运行

```bash
git clone https://github.com/FMsongX2/video2live2d.git
cd video2live2d
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
# optional: cheek puff / tongue out (installs torch + transformers)
pip install -e '.[rare]'
# optional: audio input (Audio2Live2D; installs onnxruntime)
pip install -e '.[audio]'

v2l run clip.mp4 --model ~/Models/MyModel --name hello
v2l run speech.wav --model ~/Models/MyModel --name talk     # needs the audio extra
```

结果保存在 `v2l_out/`（`hello.motion3.json`、`hello.preview.mp4`、`hello.report.json`）。除非传入 `--no-install`，动作还会复制到模型文件夹中。追踪模型和 Live2D Cubism Core 在首次使用时下载。首次处理音频时，会将 Audio2Face-3D 模型（约 725 MB，每个 identity 另需约 17 MB）下载到 `~/.cache/v2l/a2f/`。

### B. 笔记本 + 计算主机

```bash
export V2L_HOST=user@gpu-box          # required, no default
export V2L_DIR=v2l                    # optional, relative to the remote home

bin/v2l-remote setup                  # push code, create .venv, install (with the audio extra), pre-download tracker models, Audio2Face-3D models, Cubism Core and renderer libraries
bin/v2l-remote run clip.mp4 --model ~/Models/MyModel --name hello --out ~/Downloads/v2l_out
bin/v2l-remote run speech.wav --model ~/Models/MyModel --name talk
```

`run` 会上传输入（视频或音频）和模型文件夹，在主机上运行 `v2l run`，再把结果取回到 `~/Downloads/v2l_out/hello/`。除非传入 `--no-install`，动作还会复制到本地模型文件夹。也可使用其他 `v2l run` 选项（`--preset`、`--config`、`--set`、`--audio`、快捷参数）。可用 `bin/v2l-remote check` 检查连接。更新代码后请重新运行 `setup`。如需在主机上启用鼓腮/吐舌阶段，请在主机的 `V2L_DIR` 中运行 `.venv/bin/pip install -e '.[rare]'`。

### C. V2L Studio

```bash
bin/v2l-studio
# optional: values from the environment (shown locked in Settings)
V2L_MODEL=~/Models/MyModel V2L_HOST=user@gpu-box bin/v2l-studio
```

首次启动时会在 `app/` 中运行 `npm install`。生成动作需要计算环境，在 **设置 → 计算** 中选择：

- **远程主机：** 输入 `user@gpu-box`，点击 **测试连接**，然后点击 **设置服务器**（运行 `bin/v2l-remote setup`）。
- **本机：** 点击 **设置本机**（在仓库中创建 `.venv` 并安装 V2L），或像 A 中那样在 `.venv` 中用 `pip install -e .` 自行安装。

`V2L_HOST`、`V2L_MODEL` 等环境变量仍然有效，并会锁定对应的设置。

## 使用参考

### `v2l run`

```text
v2l run INPUT --model DIR --name NAME [--audio FILE] [--out DIR] [--preset NAME|FILE] [--config FILE]...
        [--set SECTION.NAME=VALUE]... [shortcut flags] [--mapping FILE] [--dump-signals]
        [--no-install] [--dry-run] [--json] [--progress text|json]
```

`INPUT` 是视频或音频文件（`.wav .mp3 .m4a .aac .flac .ogg .oga .opus`，或不含视频流的容器）。音频需要 `audio` extra；见[音频输入](#音频输入)。

设置按以下顺序应用，后者优先：

**默认值 < `--preset` < `--config` 文件（按顺序） < 快捷参数 < `--set`（按顺序）**

<details>
<summary>全部选项</summary>

| 选项 | 默认值 | 含义 |
|---|---|---|
| `--audio FILE` | 无 | 为视频输入指定单独的音轨，与视频同时开始。需要 `audio.use=mouth` 或 `face`（`--audio-use`）。不指定时使用视频自身的声音。 |
| `--model DIR` | 必填 | Live2D 模型文件夹（`*.model3.json`，可选 `*.vtube.json`）。 |
| `--name NAME` | 必填 | 输出名称；动作文件为 `NAME.motion3.json`。 |
| `--out DIR` | `v2l_out` | 输出文件夹；文件直接写入其中。 |
| `--preset NAME\|FILE` | 无 | 内置预设、用户预设或预设文件（见[设置与预设](#设置与预设)）。 |
| `--config FILE` | 无 | JSON 配置文件。可重复指定，后面的文件优先。 |
| `--set SECTION.NAME=VALUE` | 无 | 单个设置，例如 `intensity.smile=1.5` 或 `calibration.neutral_window=2:3`。可重复指定，最后应用。 |
| `--mapping FILE` | 无 | 映射文件（见[编辑动作与映射](#编辑动作与映射)）。 |
| `--dump-signals` | 关 | 同时写出 `NAME.signals.json`：逐帧的追踪输入和 bridge 输出。 |
| `--no-install` | 关 | 不把动作复制到模型文件夹。 |
| `--dry-run` | 关 | 检查输入和设置，输出计划生成的文件和最终设置。不进行追踪。 |
| `--json` | 关 | stdout 只输出一个 JSON 结果对象，其余内容全部输出到 stderr。 |
| `--progress text\|json` | `text` | `json`：stderr 的每一行都是 JSON 事件（见下文）。 |

</details>

<details>
<summary>快捷参数（每个对应一个设置）</summary>

| 参数 | 设置 | 含义 |
|---|---|---|
| `--style cute\|faithful` | `style.name` | `cute`（默认）加入更开朗的静止表情、笑时的眯眼笑和脸红、稍大的平时眼睛、稍多的头部动作；若模型有 `ParamEyeSparkle`，长时间大笑时会出现闪亮的眼睛。`faithful` 不做任何添加。 |
| `--fps N` | `output.fps` | 输出帧率（默认 30）。 |
| `--fade S` | `output.fade` | 淡入/淡出时间（秒，默认 0.5）。 |
| `--loop` | `output.loop=true` | 让动作无缝循环（用于待机动画）。 |
| `--flip` | `input.flip=true` | 输入是镜像录制的（自拍摄像头）。 |
| `--start S`, `--end S` | `input.start`, `input.end` | 只使用视频的一部分（秒）。 |
| `--neutral A:B` | `calibration.neutral_window` | 静止表情片段（秒），相对于 `--start`。默认自动查找。 |
| `--bridge NAME\|FILE` | `bridge.preset` | 内置 bridge 预设名称（默认 `default`），或 SnenkBridge 格式的 JSON 文件：普通参数列表或 `snek` 预设，支持 `delayBuffer` 输出。 |
| `--no-preview` | `preview.enabled=false` | 不生成预览视频。 |
| `--no-rare` | `expressions.enabled=false` | 跳过鼓腮 / 吐舌阶段。 |
| `--rare-stride N` | `expressions.stride` | 每 N 帧为鼓腮 / 吐舌打分一次（默认 3）。 |
| `--audio-use off\|mouth\|face` | `audio.use` | 针对视频：`mouth` 从音频获取下颌、嘴唇和舌头，`face` 获取整个面部（头部、身体和视线仍来自视频）。默认 `off`。 |

</details>

**曲线插值。** `output.interpolation` 默认为 `linear`。`--set output.interpolation=bezier` 写出受限贝塞尔分段：关键帧约减半，更便于编辑，但每个贝塞尔分段计为三个点，因此文件更大。

**JSON 结果**（`--json`）：

```text
{"ok": true, "name": "hello", "out_dir": "...", "files": {"motion": "...", "report": "...", "preview": "..." | null},
 "installed": "..." | null, "summary": {...}}
{"ok": false, "error": {"type": "...", "message": "..."}}
```

使用 `--dry-run` 时，结果中以 `"dry_run": true` 和 `"settings"` 代替 `"summary"`。

**进度事件**（`--progress json`，stderr 每行一个 JSON 对象）：

| 事件 | 字段 |
|---|---|
| `stage` | `stage`：`probe`、`tracking`、`expressions`、`audio`、`solving`、`writing`、`preview` 之一（`v2l-remote` 会在最前面加 `upload`，最后加 `download`）。仅音频的运行会跳过 `tracking` 和 `expressions`；只有使用音频时才会运行 `audio`。 |
| `progress` | `stage`、`done`、`total`；约每 0.5 秒一次，最终计数总会发送。 |
| `log` | `message`；追踪器和库的输出也会包装成这种事件。 |

**退出码**（`v2l` 和 `v2l-remote` 相同）：

| 退出码 | 错误类型 |
|---|---|
| `0` | 成功 |
| `1` | `runtime`, `remote`, `audio_unavailable`（缺少 `audio` extra 或 Audio2Face-3D 模型） |
| `2` | `usage`, `config`, `input`, `invalid`（未通过校验的动作、编辑操作、映射或公式） |
| `3` | `unusable_input`：视频无法打开、未追踪到面部，或音频缺失或无声 |
| `4` | `remote_unreachable`：无法连接计算主机 |
| `130` | `interrupted`, `cancelled` |

#### 音频输入

```bash
v2l run speech.wav --model ~/Models/MyModel --name talk                                # audio only
v2l run clip.mp4 --model ~/Models/MyModel --name talk --audio-use mouth                # mouth from the video's own sound
v2l run clip.mp4 --audio voice.wav --audio-use face --model ~/Models/MyModel --name talk  # separate audio track
```

- **仅音频。** NVIDIA Audio2Face-3D v3.0 将语音转换为 60 fps 的 ARKit 命名 blendshape，V2L 再执行与视频相同的处理链（强度、输入曲线、bridge、映射、风格、滤波）。头部和身体动作由程序生成（`idle.*`），眨眼也是生成的，因为该模型不会眨眼。`input.start` 和 `input.end` 用于裁剪音频。
- **视频加音频。** `audio.use=mouth` 从音频获取下颌、嘴唇、嘴型、舌头和鼓腮，其余全部来自视频。`audio.use=face` 从音频获取整个面部，头部、身体和视线保留视频的结果。使用音频时会跳过鼓腮/吐舌阶段。
- **默认值。** `audio.use` 为 `off`，因此除非手动开启，视频运行的结果不会改变。未设置 `audio.use` 却传入 `--audio` 属于用法错误。
- **可选项。** 情绪是选择的，而不是检测出来的：`audio.emotion`（默认 `neutral`）和 `audio.emotion_strength`。`audio.identity` 从模型训练所用的演员（`Claire`、`James`、`Mark`）中选择一位。`audio.offset` 在时间上平移面部动作。

所选片段无声，或需要音频但视频没有声音时，以 `unusable_input`（退出码 3）失败。缺少 `audio` extra 或模型下载失败时，以 `audio_unavailable`（退出码 1）失败。详细说明、通道表和派生输入：[docs/audio.md](docs/audio.md)。

### 其他 `v2l` 命令

| 命令 | 作用 |
|---|---|
| `v2l probe --model DIR [--details] [--json]` | 读取模型的参数范围并测量屏幕方向（按模型缓存）。`--details` 会附加每个被探测参数的测量结果。 |
| `v2l presets list [--json]` | 内置预设和用户预设列表。 |
| `v2l presets show NAME\|FILE` | 以 JSON 显示预设的设置。 |
| `v2l presets save NAME FILE` | 校验 `FILE` 并保存为用户预设 `NAME`。 |
| `v2l config schema` | 以 JSON 输出每个设置的类型、默认值、范围、单位、说明和 `affects`。 |
| `v2l config defaults` | 以 JSON 输出默认配置。 |
| `v2l config presets` | 以 JSON 输出内置预设和用户预设。 |
| `v2l config validate FILE...` | 检查配置文件并输出合并结果。 |
| `v2l version [--json]` | V2L 版本。 |
| `v2l motion info\|read\|write\|edit\|refit\|eval\|validate ...` | 查看、转换、编辑、重新拟合、采样和校验动作文件。 |
| `v2l mapping show\|inputs\|validate ...` | 模型的最终映射（含每个值的来源）、映射可用的输入，以及映射文件校验。 |
| `v2l bridge show\|check\|validate ...` | 最终的 bridge 输出、单个公式检查，以及 SnenkBridge 预设或映射中 bridge 部分的校验。 |
| `v2l mcp [...]` | MCP 服务器（见[智能体](#智能体-mcp--cli)）。 |

`v2l config`、`v2l presets`、`v2l version`、`v2l motion`、`v2l bridge` 以及不带 `--model` 的 `v2l mapping inputs|validate` 只需要标准库。格式和全部选项：[docs/motion-editing.md](docs/motion-editing.md)。

### `bin/v2l-remote`

| 命令 | 作用 |
|---|---|
| `setup` | 将代码推送到 `V2L_HOST`，创建 `.venv`，执行 `pip install -e '.[test,audio]'`，并预先下载追踪模型、Audio2Face-3D 模型（全部三个 identity）、Cubism Core 和渲染库。 |
| `run INPUT --model DIR --name NAME [--audio FILE] [--out DIR] [--no-install] [--json] [--progress text\|json] [v2l run options...]` | 在主机上运行 `v2l run`，并把结果取回到 `OUT/NAME/`（`--out` 默认为 `V2L_OUT`，未设置时为 `~/Downloads/v2l_out`）。`INPUT` 为视频或音频文件。 |
| `probe --model DIR [--details] [--json]` | 在主机上探测本地模型。 |
| `mapping show --model DIR [--mapping FILE] [--json]` | 在主机上计算本地模型的最终映射。 |
| `mapping inputs\|validate ...`、`bridge ...` | 在笔记本上运行，不需要 `V2L_HOST`。 |
| `check [--json]` | 检查主机是否可连接、是否已安装 V2L。 |
| `version [--json]` | 本地 V2L 代码的版本。 |
| `test` | 在主机上运行单元测试。 |
| `regress` | 使用 `V2L_REGRESS_MODEL` 中的模型在主机上运行回归测试。 |

`v2l-remote run` 如何处理设置和任务：

- `--preset`、`--config` 文件、快捷参数和 `--set` 都在**笔记本上**解析，因此会使用你的用户预设和本地文件。每次运行上传一个包含最终设置的文件（如有本地 bridge 文件也一并上传），结束后删除。无效设置会在上传前报错。
- `--dry-run` 在笔记本上运行，不建立连接。
- `--mapping FILE` 会像设置文件一样上传，运行后删除；使用 `--dump-signals` 时，信号文件会随结果一起取回。
- `--audio FILE` 会像输入一样上传到 `V2L_DIR` 中的 `inputs/audio/`，并传给主机上的 `v2l run`。
- 停止 `v2l-remote`（Ctrl-C、SIGTERM 或 SIGHUP）时，主机上的任务也会停止，并删除该次运行的文件，退出码为 130。

### 环境变量

| 变量 | 使用者 | 含义 |
|---|---|---|
| `V2L_HOST` | `v2l-remote`、Studio、MCP 服务器 | 计算主机的 SSH 目标，例如 `user@gpu-box`。`v2l-remote` 必填，无默认值。对 MCP 服务器而言，设置后启用远程模式。 |
| `V2L_DIR` | `v2l-remote`、Studio、MCP 服务器（远程模式） | 主机上的工作目录。默认 `v2l`，相对于远程主目录。 |
| `V2L_OUT` | `v2l-remote`、Studio、MCP 服务器 | 输出文件夹（`v2l-remote run --out` 的默认值）。默认 `~/Downloads/v2l_out`。 |
| `V2L_MODEL` | Studio | 启动时打开的模型文件夹。可选。 |
| `V2L_CUBISM_CORE` | `v2l` | 本地 `live2dcubismcore.min.js` 的路径，用于代替下载。 |
| `V2L_REGRESS_MODEL` | `v2l-remote regress` | 回归运行所用的模型文件夹。`regress` 必填。 |
| `XDG_CONFIG_HOME` | `v2l`、`v2l-remote`、MCP 服务器 | 用户预设位于 `$XDG_CONFIG_HOME/v2l/presets`（默认 `~/.config/v2l/presets`）。 |

## 设置与预设

所有可调参数都在同一个模式文件 `src/v2l/config.py` 中：12 个分区，共 90 项设置。CLI、V2L Studio 和 MCP 服务器读取同一模式，因此每项设置在各处的名称、范围和含义都相同。未知的键、错误的类型和超出范围的值都会报错。

| 分区 | 控制内容 | 示例键 |
|---|---|---|
| `input` | 使用视频的哪一部分以及如何读取。 | `start`, `end`, `flip` |
| `output` | 动作的帧率、淡入淡出、循环和文件大小。 | `fps`, `loop`, `interpolation` |
| `calibration` | 如何估计静止表情和各表情的最大幅度。 | `neutral_window`, `jaw_full`, `gaze_full` |
| `intensity` | 各组动作的整体强度。 | `head`, `smile`, `mouth_open` |
| `gaps` | 面部短暂丢失时的处理方式。 | `max_gap`, `ramp` |
| `expressions` | 可选的鼓腮 / 吐舌阶段。 | `enabled`, `stride`, `cheek_thresholds` |
| `bridge` | 从追踪输入到 bridge 输出的 VBridger 风格公式。 | `preset` |
| `style` | 叠加在追踪动作之上的表演风格。 | `name`, `blush_smile`, `head_gain` |
| `filtering` | 按参数组的零相位低通滤波。 | `cutoff_scale`, `head`, `mouth` |
| `preview` | 并排显示的预览视频。 | `enabled`, `width`, `height` |
| `audio` | 由音频驱动的面部（NVIDIA Audio2Face-3D）。 | `use`, `identity`, `emotion`, `emotion_strength` |
| `idle` | 没有视频时生成的头部、身体动作和眨眼。 | `head_amplitude`, `speech_nods`, `blinks` |

<details>
<summary>各分区的全部设置名</summary>

| 分区 | 设置 |
|---|---|
| `input` | `start`, `end`, `flip`, `analysis_width` |
| `output` | `fps`, `fade`, `loop`, `loop_blend`, `breath_period`, `interpolation`, `tolerance` |
| `calibration` | `neutral_window`, `neutral_seconds`, `rest_head_range`, `full_percentile`, `min_span`, `max_gain`, `blink_deadzone`, `blink_landmark_weight`, `eye_closed_ratio`, `eye_wide_range`, `jaw_full`, `lip_gap_full`, `mouth_narrow_full`, `mouth_wide_full`, `mouth_shift_full`, `gaze_full` |
| `intensity` | `head`, `body`, `gaze`, `brows`, `smile`, `mouth_open`, `mouth_shape`, `cheek_tongue` |
| `gaps` | `max_gap`, `ramp` |
| `expressions` | `enabled`, `stride`, `cheek_thresholds`, `tongue_thresholds` |
| `bridge` | `preset` |
| `style` | `name`, `mouth_form_bias`, `eye_smile_max`, `blush_rest`, `blush_smile`, `eye_open_scale`, `head_gain`, `sparkle_param`, `sparkle_smile`, `sparkle_hold` |
| `filtering` | `despike`, `cutoff_scale`, `head`, `body`, `eyeball`, `eye_open`, `brows`, `smile`, `mouth`, `blush`, `sparkle`, `other` |
| `preview` | `enabled`, `width`, `height` |
| `audio` | `use`, `identity`, `emotion`, `emotion_strength`, `strength`, `upper_face_strength`, `lower_face_strength`, `blink_strength`, `eyeballs_strength`, `saccade_strength`, `tongue_strength`, `lip_open_offset`, `offset`, `seed`, `device` |
| `idle` | `head_amplitude`, `head_speed`, `speech_nods`, `body_amplitude`, `blinks`, `blink_rate`, `gaze`, `seed` |

</details>

类型、默认值、范围、单位和说明请用 `v2l config schema` 查看（或使用只需标准库的 `python -m v2l.config schema`）。

<details>
<summary><code>audio</code> 和 <code>idle</code> 的主要设置</summary>

| 设置 | 默认值 | 含义 |
|---|---|---|
| `audio.use` | `off` | 针对视频输入：`off`、`mouth`（下颌、嘴唇、舌头来自音频）或 `face`（整个面部）。仅音频输入时始终使用音频。 |
| `audio.identity` | `Claire` | 面部模仿 Audio2Face-3D 训练所用的哪位演员：`Claire`、`James`、`Mark`。 |
| `audio.emotion` | `neutral` | `neutral`、`amazement`、`anger`、`cheekiness`、`disgust`、`fear`、`grief`、`joy`、`outofbreath`、`pain`、`sadness`。 |
| `audio.emotion_strength` | `0.6` | 情绪表现的强度（0 到 1）。 |
| `audio.strength` | `1.0` | 音频驱动面部的整体强度（0 到 2）。 |
| `audio.offset` | `0` 秒 | 在时间上平移音频驱动的面部（-1 到 1 秒）；正值使其延后。 |
| `audio.device` | `auto` | `auto`（有 NVIDIA GPU 时使用 GPU）、`cuda` 或 `cpu`。 |
| `idle.head_amplitude` | `4` 度 | 生成的头部摆动幅度。 |
| `idle.speech_nods` | `1.0` | 随语音响度出现的轻微点头和歪头（0 到 3）。 |
| `idle.body_amplitude` | `1.0` | 跟随生成的头部动作的身体摆动（0 到 3）。 |
| `idle.blinks` | `procedural` | `procedural` 添加随机眨眼（Audio2Face-3D v3.0 不会眨眼），`model` 只保留模型自身的闭眼动作，`off` 保持睁眼。`audio.use=face` 时也会使用。 |
| `idle.blink_rate` | 每分钟 `15` 次 | 程序化眨眼的平均频率。 |
| `idle.gaze` | `model` | `model` 使用音频模型的眼球运动，`center` 让眼睛保持直视前方。 |

</details>

**修改的影响范围。** 每项设置都有一个 `affects` 值：

- `tracking`：重新分析输入（`input.*`、`expressions.enabled`、`expressions.stride`、`audio.*`）。Audio2Face-3D 的输出单独缓存在 `~/.cache/v2l/a2f/`，因此 `audio.use`、`audio.offset` 和 `audio.device` 会复用该缓存。
- `solve`：复用缓存的追踪结果，只重新计算动作（大多数设置）。追踪缓存位于运行 `v2l` 的机器上的 `~/.cache/v2l`，因此只改 solve 设置重新运行同一段视频时会跳过追踪。
- `output`：只改变预览渲染（`preview.*`）。

**内置预设**

| 预设 | 说明 |
|---|---|
| `default` | V2L 默认值：cute 风格、自动静止表情、标准平滑。 |
| `faithful` | 只呈现摄像头看到的内容：不添加微笑、脸红、眼睛或头部风格化。 |
| `calm` | 更小、更平滑的头部和身体动作，适合说话、朗读或 ASMR 风格的视频。 |
| `lively` | 更大的头部、身体和面部动作，适合活泼的视频。 |
| `idle_loop` | 无缝待机动画：循环、完整的呼吸周期和柔和的平滑。 |

用户预设位于 `$XDG_CONFIG_HOME/v2l/presets`（默认 `~/.config/v2l/presets`）；与内置预设同名的用户预设会替换内置预设。预设就是带有可选 `meta` 块的配置文件：

```json
{
  "version": 1,
  "meta": {"name": "Talk show", "description": "Calmer head, stronger smile."},
  "intensity": {"head": 0.8, "smile": 1.3},
  "filtering": {"cutoff_scale": 0.8}
}
```

```bash
v2l config validate talk_show.json
v2l presets save talk_show talk_show.json
v2l run clip.mp4 --model ~/Models/MyModel --name hello --preset talk_show --set intensity.smile=1.5
```

## 编辑动作与映射

动作和映射在生成之后仍可通过 CLI、MCP 服务器和 V2L Studio 编辑。文件格式、全部操作、公式语法和所有命令选项见完整规范 [docs/motion-editing.md](docs/motion-editing.md)。

### 动作

动作就是普通的 `.motion3.json` 文件。两个关键帧之间的每个区段为线性、贝塞尔、阶梯或反向阶梯之一。V2L 写出**受限贝塞尔**（`AreBeziersRestricted`）：两个控制点位于区段时间的 1/3 和 2/3 处，因此数值是时间的三次多项式，所有 Cubism 运行时的计算结果都相同。编辑器只修改控制值。其他工具生成的贝塞尔会被精确读取，只在编辑该区段时才转换。

`v2l motion read` 返回可编辑的 JSON（关键帧、每个区段的类型、控制柄）。`v2l motion edit` 按顺序应用操作列表：插入、移动和删除关键帧，设置插值和控制柄，自动或平直切线，重新拟合、缩放、偏移、限幅、调整时间、裁剪和时间设置。无效操作会连同其序号一起报告，且不写入任何内容；已有的输出文件只有在使用 `--overwrite` 时才会被替换。

```json
[
  {"op": "insert_key", "curve": "ParamAngleX", "t": 1.0, "value": 12},
  {"op": "set_interpolation", "curves": ["ParamAngleX"], "type": "bezier", "from": 0.5, "to": 2.0},
  {"op": "refit", "curves": "*", "tolerance": 0.004, "interpolation": "bezier"},
  {"op": "trim", "from": 1.0, "to": 13.0}
]
```

```bash
v2l motion read hello.motion3.json --json                # editable JSON: keys, segment types, handles
v2l motion edit hello.motion3.json --ops ops.json --out hello_edit.motion3.json --json
v2l motion refit hello.motion3.json --interpolation bezier --tolerance 0.004 --out hello_bezier.motion3.json
```

### 映射

映射文件（`--mapping FILE`）按模型改变追踪结果作用到 Live2D 参数的方式。它分三层，在追踪之后按此顺序应用：

| 层 | 参照 | 可修改的内容 |
|---|---|---|
| `input_curves` | VBridger 的输入曲线 | 重塑追踪输入，例如让无法完全睁开的眼睛也能达到 1。 |
| `bridge` | SnenkBridge 预设、VBridger 修饰器 | 修改、新增或禁用 bridge 输出：公式、SnenkBridge 风格的 `delayBuffer`，以及修饰器 `delayMs`、`smoothing` 和 `steps`。 |
| `entries` | VTube Studio 参数设置 | 按 Live2D 参数设置输入、输入和输出范围、反转、限幅、响应曲线、平滑截止频率、呼吸、启用或停用。 |

```json
{
  "format": "v2l.mapping",
  "version": 1,
  "input_curves": {"EyeWideLeft": {"points": [[0, 0], [0.6, 1], [1, 1]], "smooth": true}},
  "bridge": [
    {"name": "MouthOpen", "modifiers": {"smoothing": 0.2}},
    {"name": "EarSwing", "func": "", "min": -1, "max": 1, "defaultValue": 0,
     "delayBuffer": {"refParam": "FaceAngleX", "smoothing": 3, "delayCount": 8,
                     "inMin": -20, "inMax": 20, "outMin": -1, "outMax": 1}}
  ],
  "entries": [
    {"output": "ParamAngleX", "in": [-25, 25], "out": [-30, 30], "cutoff": 2.0},
    {"output": "ParamEarL", "input": "EarSwing"},
    {"output": "ParamBodyAngleZ", "enabled": false}
  ]
}
```

```bash
v2l mapping inputs --json                               # input names, meaning, nominal range
v2l mapping show --model ~/Models/MyModel --mapping MyModel.json
v2l bridge check --func "0.5 + 0.5 * (MouthSmileLeft + MouthSmileRight)"
v2l run clip.mp4 --model ~/Models/MyModel --name hello --mapping MyModel.json --dump-signals
```

只保存你修改的值，其余来自模型的 `*.vtube.json` 或标准映射，并经过渲染探测的方向修正。公式遵循 SnenkBridge 的语法，差异见规范。三层都在追踪之后应用，因此修改映射会复用追踪缓存。`--dump-signals` 写出包含逐帧输入和 bridge 输出的 `NAME.signals.json`，便于绘制映射所看到的数值。

### 在 V2L Studio 中

动作编辑器和映射编辑器（见 [V2L Studio](#v2l-studio)）使用相同的格式并调用相同的操作。

<p align="center"><img src="docs/assets/motion-editor-light.png" alt="浅色主题下的 V2L Studio 动作编辑器：参数列表、带关键帧和贝塞尔控制柄的曲线图以及检查器" width="80%"></p>

<p align="center">
<img src="docs/assets/mapping-bridge-light.png" alt="浅色主题下的 V2L Studio 映射编辑器：带公式和修饰器的桥接输出标签页" width="49%">
<img src="docs/assets/mapping-inputs-dark.png" alt="深色主题下的 V2L Studio 映射编辑器：带输入曲线的输入标签页" width="49%">
</p>

## 智能体 (MCP & CLI)

AI 智能体可以通过两种方式使用 V2L：带 `--json` 和 `--progress json` 的 CLI，或通过 stdio 运行的 [MCP](https://modelcontextprotocol.io/) 服务器。对已经能执行 shell 命令的编程智能体来说，CLI 更轻量；MCP 服务器适合聊天和桌面客户端。

**安装**

- 本地模式（在本机追踪）：`pip install -e '.[mcp]'`（音频输入使用 `'.[mcp,audio]'`），然后运行 `v2l mcp`。
- 远程模式（在笔记本上运行，任务通过 `v2l-remote` 在 `V2L_HOST` 上执行）：使用 `bin/v2l-mcp`。只需要 `python3`、`rsync`、`ssh` 和 `mcp` 包；缺少 `mcp` 时由 [uv](https://docs.astral.sh/uv/) 提供。

只要设置了 `V2L_HOST`，服务器就以远程模式运行。

**在客户端中注册**（将 `/abs/path` 和 `user@gpu-box` 替换为你自己的值）：

```bash
# local mode
claude mcp add v2l -- /abs/path/video2live2d/.venv/bin/v2l mcp
# remote mode
claude mcp add v2l -e V2L_HOST=user@gpu-box -e V2L_DIR=v2l -- /abs/path/video2live2d/bin/v2l-mcp
codex mcp add v2l --env V2L_HOST=user@gpu-box --env V2L_DIR=v2l -- /abs/path/video2live2d/bin/v2l-mcp
```

使用 JSON 配置的客户端：

```json
{
  "mcpServers": {
    "v2l": {
      "command": "/abs/path/video2live2d/bin/v2l-mcp",
      "env": {"V2L_HOST": "user@gpu-box", "V2L_DIR": "v2l", "V2L_OUT": "/Users/me/Downloads/v2l_out"}
    }
  }
}
```

**工具**

| 工具 | 作用 |
|---|---|
| `v2l_params(section?)` | 设置模式、默认值和预设，可只看某个分区。 |
| `v2l_validate_settings(settings?, preset?)` | 仅对设置做试运行：应用预设和部分设置并进行检查。 |
| `v2l_probe_model(model, details?)` | 探测模型的参数和屏幕方向。 |
| `v2l_list_motions(model)` | 模型文件夹中的 `*.motion3.json` 文件。 |
| `v2l_mapping_get(model, mapping?)` | 模型的最终映射（全部三层）和可用的输入。 |
| `v2l_bridge_check(func, mapping?)` | 检查单个 bridge 公式：读取的名称、未知名称、错误位置。 |
| `v2l_motion_info(motion)` | 动作的曲线、分段类型、计数和问题。 |
| `v2l_motion_read(motion, curves?)` | 以可编辑 JSON 返回动作。 |
| `v2l_motion_edit(motion, operations, out, overwrite?, ranges?)` | 应用编辑操作，并将结果写为新动作。 |
| `v2l_generate(video?, model, name, preset?, settings?, mapping?, preview?, out_dir?, install?, job?, wait_seconds?, audio?)` | 从视频、音频文件或两者启动或恢复生成任务。 |
| `v2l_job_status(job, wait_seconds?)` | 等待任务并返回进度或结果。 |
| `v2l_jobs()` | 所有已知任务。 |
| `v2l_job_cancel(job)` | 停止排队或运行中的任务（远程模式下也会停止主机上的进程）。 |
| `v2l_install_motion(motion, model, overwrite?)` | 将动作复制到模型文件夹（仅在启用 `install` 工具集时可用）。 |

**长任务。** `v2l_generate` 在发送进度通知的同时最多等待 45 秒（默认 40 秒，低于常见客户端 60 秒的请求超时），然后返回结果或任务句柄。用该句柄调用 `v2l_job_status` 即可继续等待。再次发送相同的请求（相同的视频/音频和模型内容、设置、名称和输出文件夹）会恢复同一个任务，而不会启动第二次运行。任务逐个执行，后续任务显示为 `queued`。任务记录保存在 `~/.cache/v2l/jobs/`。

**音频。** 只传 `audio`（或把音频文件作为 `video` 传入）时，由语音驱动面部，并生成头部、身体动作和眨眼。同时传入 `video` 和 `audio` 时，对于 `settings.audio.use` 从音频获取的通道，用该音频替代视频自身的声音；若设置中 `audio.use` 保持为 `off`，服务器会将其设为 `mouth`。只传视频并将 `settings.audio.use` 设为 `mouth` 或 `face` 时，使用视频自身的声音。运行任务的主机需要安装 `audio` extra（`bin/v2l-remote setup` 会安装）。

**服务器参数**

| 参数 | 含义 |
|---|---|
| `--read-only` | 隐藏生成、取消或安装动作的工具。 |
| `--toolsets inspect,generate,install` | 启用的工具组（默认 `inspect,generate`）。 |
| `--allow-dir DIR` | 所有路径参数都必须位于这些文件夹之一内（可重复指定）。输出文件夹始终允许用于输出。 |

**安全**

- 计算主机只由 `V2L_HOST` / `V2L_DIR` 决定，不会来自工具参数。
- 路径必须是绝对路径或以 `~` 开头；名称必须匹配 `^[A-Za-z0-9_-]{1,80}$`。
- 安装到模型文件夹默认关闭：未启用 `install` 工具集时，`install=true` 会被拒绝。
- 已存在的 `<out>/<name>/` 永远不会被覆盖。失败或取消的任务会删除自己的输出文件夹，因此重试时可以复用同一名称。
- `v2l_motion_edit` 只写入输出文件夹或 `--allow-dir` 文件夹，只有启用 `install` 工具集时才写入模型文件夹，并在 `--read-only` 下隐藏。

**GUI 客户端与 `PATH`。** 桌面应用通常以精简的 `PATH` 启动，可能找不到 `v2l`、`uv`、`rsync` 或 `ssh`。请在客户端配置中使用绝对路径，并确保 `bin/v2l-mcp` 所需的工具位于标准位置。从终端启动时，服务器会提示正在等待 MCP 客户端；这不是卡住。

## V2L Studio

<p align="center">
<img src="docs/assets/studio-light.png" alt="浅色主题下的 V2L Studio 主窗口：左侧为 Live2D 查看器，右侧为创建动作面板" width="49%">
<img src="docs/assets/settings-tuning-dark.png" alt="深色主题下的 V2L Studio 设置：带有强度滑块的调校分区" width="49%">
</p>

V2L Studio（`bin/v2l-studio`）是一个 Electron 桌面应用，一侧是 Live2D 查看器，另一侧是创建面板。

- **查看器。** 取景预设、拖动与缩放、物理开关和进度条。按 **Space** 切换播放。纹理通过上限为 4K 的查看副本显示，不会修改模型文件夹。
- **创建面板。** 选择或拖入视频或音频文件，或用摄像头录制一段、录制一段语音（**录制语音**）。音频来源会显示可播放、可拖动定位的波形。设置动作名称、调校预设和风格，并可选择设置裁剪、静止表情片段、鼓腮/吐舌识别和镜像。涉及音频时会出现 **音频** 分组：对于带声音的视频，**使用音轨** 设置 `audio.use`（关闭、嘴部来自音频、面部来自音频），**使用单独的音频文件** 会传入 `--audio`；使用音频时，可以设置情绪、情绪强度和说话风格（`audio.identity`）。⋯ 菜单可将这些值保存为默认值、重置，或把本次运行复制为 `v2l run` 命令。
- **生成。** 显示带编号的阶段列表（upload、probe、tracking、expressions、audio、solving、writing、preview、download），以及追踪和表情打分的帧进度；原始日志仍可查看。失败会注明原因（没有可用的面部、没有可用的语音、音频模型不可用、无法连接主机、设置无效、已取消），取消时会请求确认。完成的结果可以与原视频或音频同步对比、保存到模型文件夹（覆盖前确认）、用 **使用这些设置** 重新载入其设置，或用 **用当前设置重新运行** 以带编号的名称重新生成。结果卡片会显示来源类型；对于音频，还会显示语音占比、电平、情绪、说话风格和音频模型所用的设备。
- **动作编辑器。** 点击 **编辑动作**（在动作列表旁或结果上）会在查看器下方打开曲线编辑器，查看器和原视频跟随播放头。可以选择、拖动和插入关键帧，编辑贝塞尔控制柄，在线性、贝塞尔、阶梯和反向阶梯之间切换区段，应用自动或平直切线，并在检查器中重新拟合、缩放、裁剪或调整时间。所有更改都支持撤销和重做，弹出面板列出键盘和鼠标快捷键。可另存为新动作、确认后覆盖，或保存到模型文件夹。
- **映射编辑器。** 点击模型标题栏的 **映射**，或设置 → 生成默认值中的 **参数映射**，三层会以标签页（输入、桥接输出、参数）打开。**预览更改** 用未保存的映射复用追踪结果重新生成上一个结果并播放，再用 **保留** 或 **丢弃** 决定是否留下该结果。保存的映射会传给之后的每次运行。
- **摄像头录制。** 3-2-1 倒计时和无表情提示，该片段即成为静止表情片段。录制时长 2.3–60 秒，麦克风需手动开启，带声音的录制也可以用音频驱动嘴部。录制文件保存在 `<V2L_OUT>/recordings/`。
- **语音录制。** **录制语音** 可选择麦克风、显示输入电平、倒计时并录制 1–60 秒。录音以 `.m4a` 或 `.opus`（没有 `ffmpeg` 时为 WAV）保存在 `<V2L_OUT>/recordings/`。
- **语言与主题。** 英语、韩语、日语和简体中文；跟随系统、浅色或深色主题。中性色取自 Adobe Spectrum 2，强调色在浅色主题中为珊瑚色（`#FD7688`），在深色主题中为 Spectrum 品红。

### 设置窗口

通过齿轮按钮、应用菜单或 ⌘,（Linux 上为 Ctrl+,）打开。搜索框可按标签、说明和键名筛选所有设置。

| 分区 | 内容 |
|---|---|
| 通用 | 语言、主题（跟随系统 / 浅色 / 深色）、默认模型文件夹、启动时打开上次的模型。 |
| 计算 | **远程服务器**（主机、远程目录、**测试连接**、**设置服务器**）或 **本机**（`v2l` 可执行文件路径、**测试**、**设置本机**）。 |
| 输出 | 输出文件夹、结果名称已存在时的处理方式、自动保存到模型文件夹（默认关闭）、预览视频（Studio 中默认关闭）。 |
| 生成默认值 | 默认预设、风格、循环、镜像、鼓腮/吐舌及间隔、帧率、淡入淡出、bridge 预设、曲线插值、参数映射、音轨使用方式（`audio.use`）、情绪、情绪强度、说话风格。 |
| 调校 | 按 [设置与预设](#设置与预设) 中的分组显示模式中的全部设置，包括 **音频**（`audio.*`）和 **生成动作**（`idle.*`）。高级项隐藏在开关之后，每个设置和分组都可以重置，会改变追踪的设置带有“需重新分析源”标记。 |
| 预设 | 与 CLI 共用的内置预设和用户预设：应用、保存当前调校、重命名、删除、导入、导出。 |
| 查看器 | 物理、4K 纹理、取景、循环和同步的默认值。 |
| 摄像头 | 默认摄像头以及摄像头录制时是否录制音频。 |
| 智能体与 CLI | 可复制的 `claude mcp add`、`codex mcp add` 命令和 MCP 服务器的客户端 JSON，以及把当前设置合成一条 `v2l run` 命令；已填入你的可执行文件、主机和输出文件夹。 |
| 高级 | 日志筛选、清除模型缓存、导出和导入全部设置、全部重置。 |
| 关于 | 版本、许可证、第三方声明、可复制的诊断信息（不含路径和主机名）。 |

<p align="center"><img src="docs/assets/settings-agents-light.png" alt="V2L Studio 设置中的智能体与 CLI 分区：可复制的 claude mcp add 和 codex mcp add 命令" width="70%"></p>

**保存。** 修改会立即保存到应用数据文件夹（`v2l-studio`）中的 `settings.json`，只记录与默认值不同的值。优先级：模式默认值 < Studio 设置 < 环境变量 < 创建面板中的单次运行值。由环境变量（`V2L_HOST`、`V2L_DIR`、`V2L_OUT`、`V2L_MODEL`）设定的值会显示为锁定状态。

**运行位置。** 查看器、录制和文件处理在笔记本上运行。追踪、Audio2Face-3D、解算和探测按设置 → 计算中的选择，在远程主机（通过 `bin/v2l-remote`）或本机（通过本地 `v2l`）上运行。计算环境设置好之前，连接按钮显示为 **设置生成**，点击后打开设置 → 计算。

## 输出文件

对于 `--name hello`，`v2l run` 将以下文件直接写入 `--out` 文件夹（默认 `v2l_out/`）；`bin/v2l-remote run` 和 Studio 则写入 `<out>/hello/`（默认 `~/Downloads/v2l_out/hello/`）：

| 文件 | 内容 |
|---|---|
| `hello.motion3.json` | Cubism 动作：默认为线性分段（`output.interpolation=bezier` 时为受限贝塞尔），经过简化和校验。 |
| `hello.preview.mp4` | 原视频与开启物理的模型渲染并排显示（使用 `--no-preview` 时跳过）。仅音频输入时只显示模型；由音频驱动动作时，预览带有该声音。 |
| `hello.report.json` | 面部和姿态检测率、最长追踪缺失、所用的静止表情片段、眨眼、各参数的范围使用率、最终映射、映射说明和警告。还包含 `source`（`kind`：`video`、`audio` 或 `video+audio`，以及 `use`）、`audio`（文件、时长、电平、语音占比、Audio2Face-3D identity、情绪和设备；仅视频时为 null）和 `idle`（生成的动作；未生成时为 null）。 |
| `hello.signals.json` | 逐帧的追踪输入和 bridge 输出（仅在使用 `--dump-signals` 时）；使用音频时还包括每个输入的来源和逐帧响度。 |

默认情况下动作还会复制到模型文件夹，VTube Studio 随后会将其列为该模型的动画。

## 录制建议

- 保证面部光线充足、完整可见，并让双肩入镜。
- 开头保持约 1 秒的无表情，然后传入 `--neutral 0:1`。
- 表演可以稍微夸张一些。虚拟形象只能表现摄像头看到的内容。
- 如果素材是镜像录制的（自拍摄像头），请传入 `--flip`。
- 用 `--start` 和 `--end` 剪掉开头和结尾的准备时间。
- 音频方面：只录一个人的声音，靠近麦克风，不要有音乐或背景人声。试验时用 `--start` 和 `--end` 裁剪较长的录音。

## 局限性

- V2L 并非 iPhone ARKit 或 VBridger 的数值复现。校准是从视频中估计的，使用兼容的 bridge 预设也不会使输出与 VBridger 等同。
- MediaPipe 的部分 blendshape 通道较弱或噪声较大；V2L 将其与关键点几何结合，但细微的嘴部和眉毛形状仍可能被低估或高估。
- 鼓腮和吐舌是实验性功能，仅在一个不公开的小型内部数据集（10 段带标注的视频）上测量过（帧级 AUC：鼓腮 0.98，吐舌 0.996，而 MediaPipe 的 `cheekPuff` 为 0.07）。
- 手臂和手只有在模型绑定会驱动它们时才会动，通常通过物理实现，不会复制表演者的手臂姿势。
- 仅支持离线处理，没有实时追踪模式。
- 每段视频仅支持一名表演者。
- 映射文件新增的参数不会被渲染探测测量，因此其方向不会自动修正。
- 音频输入：面部跟随语音，而不是说话者真实的表情。情绪是选择的（`audio.emotion`），不是检测出来的；不使用 NVIDIA 的 Audio2Emotion。
- 音频输入：Audio2Face-3D v3.0 从不眨眼（`idle.blinks` 默认添加程序化眨眼），并且 eyeLook*、jawLeft/Right、mouthLeft/Right 和 tongueOut 始终不活动；视线来自其眼球旋转。
- 音频输入不提供头部姿态。仅音频输入时，头部和身体动作是生成的（`idle.*`），而非真实表演。
- 唱歌、喊叫、响亮的音乐或多人说话会让嘴型不够可靠。在 CPU 上，较长片段的处理速度慢于实时。

## 开发

```bash
pip install -e '.[test,mcp]'
pytest
```

测试涵盖校准、bridge 语言、映射、缺失片段、motion3 导出、CLI 和 MCP 服务器；MCP 测试使用模拟的 CLI，不访问网络。未安装 `mcp` extra 时，需要 MCP SDK 的测试会被跳过。

**回归测试。** `regression/run.py` 在你自己的视频上运行流水线，写出 `regression/results/summary.json` 以及每段视频的联系表（contact sheet）。视频不随仓库分发。请在 `regression/videos/manifest.json` 中列出：

```json
[
  {"id": "talk_front", "file": "talk_front.mp4", "conditions": ["frontal", "speech"]},
  {"id": "turn_dim", "file": "turn_dim.mp4", "conditions": ["head turn", "low light"]}
]
```

`file` 是相对于 `regression/videos/` 的路径。运行方式：

```bash
python regression/run.py --model /path/to/model [--style cute] [--config FILE]
# or from the laptop:
V2L_REGRESS_MODEL=/path/to/model bin/v2l-remote regress
```

**研究。** `research/rare_expr.py` 在 `regression/expressions/manifest.json` 中列出的、你自己标注的视频上复现鼓腮 / 吐舌的测量。

**CI。** [CI 工作流](.github/workflows/ci.yml) 安装 `.[test,mcp]`，在 Python 3.12 上运行 `pytest`，并检查 JavaScript 语法。

已测试环境：计算主机为 Ubuntu 24.04 x86_64 + Python 3.12（鼓腮/吐舌阶段在 GPU 和 CPU 上均已测试）；本地 `v2l run` 和 Studio 为 macOS（Apple silicon）+ Python 3.14。

## 第三方组件与许可证

V2L 自身代码采用 MIT 许可证（[LICENSE](LICENSE)）。除内联在 V2L Studio 中的 27 个 Lucide 图标，以及 [`src/v2l/a2f.py`](src/v2l/a2f.py) 中对 NVIDIA 采用 MIT 许可证的 Audio2Face-3D SDK 代码的 Python 移植外，第三方文件由包管理器安装或在首次使用时下载，不包含在本仓库中：

| 组件 | 许可证 |
|---|---|
| Live2D Cubism Core for Web | Live2D Proprietary Software License（从 Live2D 官网下载） |
| pixi.js 6.5.10 | MIT |
| pixi-live2d-display 0.4.0 (`cubism4.min.js`) | MIT；内含采用 Live2D Open Software License 的 Cubism Framework |
| MediaPipe Face / Pose Landmarker 模型 | Apache-2.0 |
| SigLIP 2 (`google/siglip2-so400m-patch14-384`) | Apache-2.0 |
| NVIDIA Audio2Face-3D v3.0 模型（`nvidia/Audio2Face-3D-v3.0`，仅用于音频输入） | NVIDIA Open Model License（首次使用时从 Hugging Face 下载，以 SHA-256 固定） |
| Audio2Face-3D SDK（`src/v2l/a2f.py` 中移植的部分） | MIT |
| ONNX Runtime（`onnxruntime` / `onnxruntime-gpu`，`audio` extra） | MIT；`[cuda,cudnn]` extra 会按 NVIDIA 的条款安装 NVIDIA CUDA 和 cuDNN 运行时 wheel |
| [Lucide](https://lucide.dev) 图标（27 个，内联在 V2L Studio 中） | ISC；其中 7 个源自 Feather（MIT） |

pixi 文件在 `src/v2l/web/assets.json` 中以 SHA-256 固定。详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，音频模式见 [docs/audio.md](docs/audio.md)。不包含第三方 bridge 预设。

**你的责任。**

- 发布使用 Live2D 模型制作的作品可能需要 Live2D 自己的许可（例如 Live2D Publication License）。请根据自身情况查阅 Live2D 的条款。
- 你需要拥有所驱动模型的相应权利。
- 你需要获得所处理视频和语音录音中人员的同意。
- 下载 Audio2Face-3D 模型即表示接受 NVIDIA Open Model License。模型文件从不随 V2L 分发；借助它生成的动作只包含参数曲线，不包含模型数据。

## 致谢

- 面部和姿态追踪：[MediaPipe](https://github.com/google-ai-edge/mediapipe)。
- 渲染：[pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) 和 [PixiJS](https://github.com/pixijs/pixijs)。
- 语音驱动的面部动画：[NVIDIA Audio2Face-3D](https://huggingface.co/nvidia/Audio2Face-3D-v3.0) 及其 [SDK](https://github.com/NVIDIA/Audio2Face-3D-SDK)；运行它所用的 [ONNX Runtime](https://onnxruntime.ai/)。
- 模型格式与运行时：[Live2D Cubism](https://www.live2d.com/)。
- V2L Studio 的图标：[Lucide](https://lucide.dev)；主题的设计令牌：[Adobe Spectrum 2](https://spectrum.adobe.com/)。
- bridge 解释器所实现的 [SnenkBridge](https://github.com/FaeyUmbrea/SnenkBridge) 公式语言及其延迟缓冲，也是映射编辑器的参考。
- [VBridger](https://store.steampowered.com/app/1898830/VBridger/)，V2L 以其工作流为参照，映射编辑器沿用了它的输入曲线和修饰器。
- [SandoitchiBridge](https://github.com/an1by/SandoitchiBridge)，其变换配置为映射文件的设计提供了参考。

## 许可证

[MIT](LICENSE)
