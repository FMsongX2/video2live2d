# Audio2Live2D: motion from speech

V2L can drive a Live2D model from audio instead of (or together with) a video. The face comes from
NVIDIA [Audio2Face-3D](https://huggingface.co/nvidia/Audio2Face-3D-v3.0) v3.0, which turns speech
into ARKit-named blendshapes; V2L then runs the same chain as for video (intensity, input curves,
resampling, bridge, mapping, style, filtering, motion3.json export).

```
v2l run speech.wav --model DIR --name talk                      # audio only
v2l run clip.mp4 --model DIR --name talk --audio-use mouth      # mouth from the video's own sound
v2l run clip.mp4 --audio voice.wav --audio-use face --model DIR --name talk   # separate track
```

Install the optional `audio` extra (`pip install -e '.[audio]'`; ONNX Runtime with CUDA on
Linux/Windows x86_64, the CPU build elsewhere). `v2l-remote setup` installs it on the compute host
and downloads the models. The model files (about 0.8 GB for the three identities) are downloaded
on first use into `~/.cache/v2l/a2f/`, checked against pinned hashes, and never shipped with V2L.
They are under the NVIDIA Open Model License; the pre/post-processing and blendshape solver are a
Python port of NVIDIA's MIT-licensed Audio2Face-3D SDK (see THIRD_PARTY_NOTICES.md).

## Inputs

* **Audio only**: `.wav .mp3 .m4a .aac .flac .ogg .oga .opus`, or any container without a video
  stream. `input.start` / `input.end` trim it. The preview is the avatar alone with the sound.
* **Video with audio** (`audio.use` = `mouth` or `face`): the video's own sound, or `--audio FILE`
  (a separate track that starts together with the video). `--audio` without `audio.use` set is an
  error on the command line; the MCP tool sets `mouth` for you. The preview shows the video next to
  the avatar with the sound that drove the motion.
* `audio.use=off` (the default) leaves video runs exactly as before: the motion, signals and
  previews are byte-identical to a build without audio support.

All channels are decoded with the bundled ffmpeg to mono 16 kHz. A silent selection (loudest part
under -50 dBFS) or a video without sound when the audio is needed stops the run with
`unusable_input` (exit 3); a missing `audio` extra or model download failure stops it with
`audio_unavailable` (exit 1).

## What comes from where

| channel | audio only | video + `mouth` | video + `face` |
|---|---|---|---|
| jaw, lips, mouth shape, tongue, cheek puff | audio | audio | audio |
| smile and frown | audio | video | audio |
| brows, eye openness and squint, blinks, nose, cheeks | audio (blinks: `idle.blinks`) | video | audio (blinks: `idle.blinks`) |
| gaze (GazeX/Y, EyeLook*) | audio, or centred (`idle.gaze`) | video | video |
| head yaw/pitch/roll | generated (`idle`) | video | video |
| shoulders / body | generated (`idle`) | video | video |
| breathing | mapping (as for video) | mapping | mapping |

With a video, the zero-shot cheek-puff/tongue stage is skipped when the audio is used: both
channels come from the audio model.

Derived inputs (the blendshapes themselves pass through unchanged, 0..1, 0 at rest):

* `JawOpen` = jawOpen - rest
* `LipOpen` = 1.2 max(jawOpen - mouthClose, 0) + 0.25 (lower-lip down + upper-lip up, both sides)
  - 0.25 (press + roll) - rest, clipped to 0..1. The rest of both is their median over the pauses
  (loudness more than about 33 dB under the speech, at least 0.3 s in total): the model's
  resting mouth is slightly open. Without pauses JawOpen keeps 0 and LipOpen uses its
  5th percentile.
* `MouthClose` = max(mouthClose, JawOpen - LipOpen), the same rule as the video path
* `LipWidth` = max(mean dimple, mean stretch) - mouthPucker
* `MouthShift` = mouthLeft - mouthRight (+ = image right)
* `GazeX` / `GazeY` = mean eye yaw / pitch minus its median over 26 / 22 degrees
* `TongueOut` = max(tongueOut, the tongue solve's `tongueStretch`): NVIDIA's skin solve keeps
  tongueOut inactive, and in ordinary speech the tongue stays inside, so it is usually 0

Audio2Face-3D v3.0 does not blink and keeps eyeLook*, jawLeft/Right and mouthLeft/Right at 0;
gaze comes from its eye rotation output.

## Time alignment

The audio-driven face at clip time *t* (seconds after `input.start`) is the model output for file
time `input.start + t - audio.offset`, so a positive `audio.offset` moves the face later. V2L
decodes one extra second on each side of the selection so the offset can reach outside it; outside
the audio the face rests. Audio2Face-3D runs at 60 fps; a video takes the values at its own frame
times, then everything is resampled to `output.fps`.

## Generated motion (`idle`, audio only)

Deterministic for a given `idle.seed`:

* **Head**: band-limited noise around `idle.head_speed` Hz plus a slower drift, soft-limited with
  `tanh` to `idle.head_amplitude` degrees for yaw, 0.6x for pitch and 0.5x for roll.
* **Speech emphasis** (`idle.speech_nods`): per phrase (speech between pauses over 0.25 s) a small
  held turn and tilt (up to 1 degree each), a slight raise of the head while speaking (up to
  1 degree) and a nod (1.5 degrees) at the strongest loudness onsets, at most one per second.
* **Body** (`idle.body_amplitude`): shoulders follow the head 0.25 s later at 0.4x, plus a slow sway
  of up to 1 degree; the bridge turns them into body angles.
* **Blinks** (`idle.blinks`, also with `audio.use=face`): `procedural` (default) adds blinks at
  `idle.blink_rate` per minute (gamma-distributed intervals, at least 0.6 s apart) on top of the
  model's eye closing, `model` keeps only the model's (which has no blinks), `off` removes both.

Bounds, all in degrees: |yaw| <= amplitude + nods, |pitch| <= 0.6 amplitude + 2.5 nods,
|roll| <= 0.5 amplitude + nods (`nods` = `idle.speech_nods`). The usual head and body low-pass
filters apply afterwards.

## Caching

The Audio2Face-3D output is cached in `~/.cache/v2l/a2f/` by audio content, the decoded span and
the model settings (`audio.identity`, `emotion`, `emotion_strength`, the strengths,
`lip_open_offset`, `seed`). Changing `audio.use`, `audio.offset`, `audio.device`, `idle.*` or any
solve setting reuses it.

## Outputs

The usual files. `<name>.report.json` adds

* `source`: `{"kind": "video" | "audio" | "video+audio", "video", "audio" (the audio file, or null
  for a video's own track), "audio_from": "file" | "video" | null, "use": "off" | "mouth" | "face" |
  "all"}`;
* `audio` (null for video only): `path`, `sample_rate`, `channels`, `duration_s` (of the file),
  `window_s` (used span), `decoded_s`, `offset_s`, `rms_dbfs`, `peak_dbfs`, `speech_ratio`, `fps`,
  `frames`, `model` (Audio2Face-3D details: identity, emotion, device, runtime, cache hit);
* `idle` (null unless generated): `seed`, `phrases`, `nods`, `peak_deg`, `bounds_deg`, `blinks`
  (procedural count or null), `blink_mode`, `gaze`.

For audio-only input `video` is null, `face_rate` is 1, `pose_rate` and `neutral_window_s` are null.
`--dump-signals` adds `source` (`kind`, `audio_inputs`: inputs taken from the audio, `generated`:
inputs from the generated motion) and `audio.loudness` (0..1 per output frame, as heard) to
`<name>.signals.json`. Progress events gain the stage `audio` (between `expressions` and
`solving`); audio-only runs skip `tracking` and `expressions`.

## Limitations

* The face follows speech, not the speaker's real expression; the emotion is chosen
  (`audio.emotion`, `audio.emotion_strength`), not detected.
* Head and body motion for audio-only input are generated, not performed.
* Singing, shouting, heavy music or several voices give less reliable mouth shapes.
* On a CPU the model runs slower than real time on long clips; trim while experimenting.
