# Motion and mapping editing

V2L motions are ordinary Cubism `.motion3.json` files. This document defines the two editable
representations that V2L Studio, the `v2l` CLI and the MCP server share:

1. **Editable motion** (`v2l.motion-edit`): keyframes, per-segment interpolation and Bezier handles,
   plus a list of edit operations.
2. **Mapping file** (`v2l.mapping`): per-model changes in three layers: curves on tracking inputs,
   bridge formulas, and which input drives which Live2D parameter, over which ranges and with
   which response curve.

Both are plain JSON. The Python module `v2l.motionedit` (standard library only) is the reference
implementation for reading, writing, fitting, validating and applying operations.

## 1. Curve semantics

A motion3 curve is a start point followed by segments, each continuing from the previous end point
(Live2D motion3.json format):

| Type | Segment | Value between `p0` and `p1` |
|---|---|---|
| `0` linear | `[0, t1, v1]` | straight line |
| `1` bezier | `[1, c1t, c1v, c2t, c2v, t1, v1]` | cubic Bezier through `p0, c1, c2, p1` |
| `2` stepped | `[2, t1, v1]` | holds `v0`, jumps to `v1` at `t1` |
| `3` inverse stepped | `[3, t1, v1]` | jumps to `v1` right after `t0`, holds it |

**Restricted Bezier.** When `Meta.AreBeziersRestricted` is true, the control times are
`c1t = t0 + (t1 - t0)/3` and `c2t = t1 - (t1 - t0)/3`. Time then advances linearly with the curve
parameter, so the value is a cubic polynomial in time and every Cubism runtime evaluates it the same
way. V2L always writes restricted Beziers; editors change only the control values `c1v`, `c2v`.
Unrestricted Beziers from other tools are read and evaluated exactly (the time equation is solved
for the curve parameter) and are converted to restricted form only by an explicit edit of that
segment.

**Time lookup.** At a key time the segment that starts there applies; after the last key the curve
holds its last value. So a stepped segment shows `v0` on `[t0, t1)`, and an inverse stepped segment
shows `v1` on `[t0, t1)`.

## 2. Editable motion format

```json
{
  "format": "v2l.motion-edit",
  "version": 1,
  "meta": {"duration": 4.0, "fps": 30.0, "loop": false, "fade_in": 0.5, "fade_out": 0.5, "restricted": true},
  "curves": [
    {
      "target": "Parameter",
      "id": "ParamAngleX",
      "keys": [[0.0, 0.0], [0.5, 12.0], [1.2, -3.5]],
      "segments": [
        {"type": "bezier", "c1": [0.1667, 4.0], "c2": [0.3333, 11.0]},
        {"type": "linear"}
      ]
    }
  ],
  "passthrough": {"meta": {}, "user_data": []}
}
```

- `keys` are `[time, value]`, strictly increasing in time. `segments` has one entry per pair of
  neighbouring keys (`len(segments) == len(keys) - 1`). The first key is at time 0; the last key
  may end before `meta.duration` (the curve then holds its value) but not more than one frame after
  it (Cubism files often round `Duration`).
- Segment types: `linear`, `bezier` (with `c1`, `c2` as `[time, value]`), `stepped`,
  `inverse_stepped`.
- `meta.restricted` mirrors `AreBeziersRestricted`. With `true`, the writer recomputes control times
  from the key times, whatever the file says. `meta.fade_in` / `fade_out` are `null` when the file
  has no `FadeInTime` / `FadeOutTime`.
- `target` is `Parameter`, `PartOpacity` or `Model`, as in motion3. A curve may also carry
  `fade_in` / `fade_out` (the motion3 per-curve `FadeInTime` / `FadeOutTime`) and `extra` (curve
  fields V2L does not know); editors keep them as they are.
- `passthrough` keeps Meta fields and `UserData` that V2L does not edit, so a read and write round
  trip preserves them. Counts in Meta (`CurveCount`, `TotalSegmentCount`, `TotalPointCount`,
  `UserDataCount`, `TotalUserDataSize`) are always recomputed on write: a Bezier segment counts three
  points, the other types one, plus one start point per curve; `TotalUserDataSize` is the UTF-8 size
  of the user data values.

## 3. Fitting

`refit` turns a curve (or a densely sampled motion) into few editable keys:

- **bezier**: a least-squares fit in the restricted parameterisation, after Schneider, "An Algorithm
  for Automatically Fitting Digitized Curves" (Graphics Gems, 1990). Time advances linearly with the
  curve parameter, so no reparameterisation is needed. Keys are samples; each key has one slope shared
  by both neighbouring spans, so the curve is smooth (C1), and all slopes are solved together by
  least squares over the samples. Spans whose worst sample misses the tolerance are split at that
  sample and the slopes are solved again, until every sample is within the tolerance.
- **linear**: Ramer-Douglas-Peucker on the value axis.

The tolerance is a fraction of the parameter's range (the same unit as `output.tolerance`); without
known ranges the curve's own value span is used. Refitting an existing curve samples it at
`meta.fps` plus its key times, splits at its original keys first, and keeps stepped and inverse
stepped segments as they are.

## 4. Edit operations

An operation list is applied in order; each operation is an object with `op`. Curve selectors are a
list of ids or `"*"`. Times are seconds. A key is addressed by its time (within 0.0001 s).

| `op` | Fields | Effect |
|---|---|---|
| `set_interpolation` | `curves`, `type`, `from?`, `to?` | Set the type of every segment that overlaps `[from, to]` (default: all; a segment that only touches the range at a key is not included; with `from == to`, the segment containing that time). Switching to `bezier` starts from smooth automatic handles; segments that already are Beziers keep theirs. |
| `insert_key` | `curve`, `t`, `value?` | Insert a key strictly between the first and last key, not on an existing key; the default value is the curve's value at `t`, and the split keeps the shape exactly. With `value`, the new key moves there with its handles. |
| `delete_keys` | `curve`, `times?` or `from`/`to` | Delete keys. Naming the first or last key in `times` is an error; a `from`/`to` range keeps them. The joined segment takes the type of the first removed one, and a Bezier keeps the slopes at both ends. |
| `move_key` | `curve`, `t`, `to_t?`, `to_value?` | Move a key in time (strictly between its neighbours; the first and last key cannot move in time) and/or value (its handles move with it). |
| `set_handles` | `curve`, `segment` (start time), `c1v?`, `c2v?` | Set Bezier control values of one segment (an error for other types). |
| `auto_tangents` | `curves`, `from?`, `to?` | Smooth handles at the keys in the range (Bezier segments only), without overshooting a key that is a local extreme. |
| `flatten_tangents` | `curves`, `from?`, `to?` | Horizontal handles at the keys in the range (ease in and out). |
| `refit` | `curves`, `tolerance`, `interpolation` | Replace keys by a fitted curve (section 3). |
| `scale_values` | `curves`, `factor`, `pivot` (`zero`, `first`, `mean` or a number) | Scale values around a pivot (`mean`: the curve's time average). |
| `offset_values` | `curves`, `delta` | Add a constant. |
| `clamp` | `ranges` (`{id: [min, max]}`) | Clamp keys and control values to parameter ranges. |
| `retime` | `factor` | Scale every time (and the duration). |
| `trim` | `from`, `to` | Keep `[from, to]`, shifted to start at 0; cut segments are split exactly. |
| `set_meta` | `loop?`, `fade_in?`, `fade_out?`, `fps?`, `duration?` | Change meta values. |
| `delete_curve` | `curve` | Remove a curve. |

Invalid operations (unknown op or curve, a missing or wrong field, a move past a neighbour, deleting an
end key) are errors that name the operation's index; nothing is written.

## 5. Mapping file

One file per model holds three layers, applied in this order after tracking:

```
tracking (calibrated, intensity applied) -> input_curves -> bridge (+ modifiers) -> entries -> filtering
```

```json
{
  "format": "v2l.mapping",
  "version": 1,
  "input_curves": {
    "EyeWideLeft": {"points": [[0, 0], [0.6, 1], [1, 1]], "smooth": true}
  },
  "bridge": [
    {"name": "MouthSmile", "func": "0.5 + 0.5 * (MouthSmileLeft + MouthSmileRight)"},
    {"name": "EarSwing", "func": "", "min": -1, "max": 1, "defaultValue": 0,
     "delayBuffer": {"refParam": "FaceAngleX", "smoothing": 3, "delayCount": 8,
                     "inMin": -20, "inMax": 20, "outMin": -1, "outMax": 1}},
    {"name": "MouthOpen", "modifiers": {"smoothing": 0.2,
      "steps": [{"trigger": 0.1, "target": 0.5, "threshold": 0.05},
                {"trigger": 0.35, "target": 1, "holdMs": 100}]}},
    {"name": "VoiceVolumePlusMouthOpen", "enabled": false}
  ],
  "entries": [
    {"output": "ParamAngleX", "in": [-25, 25], "out": [-30, 30], "cutoff": 2.0},
    {"output": "ParamMouthOpenY", "curve": {"points": [[0, 0], [0.5, 0.2], [1, 1]], "smooth": true}},
    {"output": "ParamEarL", "input": "EarSwing"},
    {"output": "ParamBodyAngleZ", "enabled": false}
  ]
}
```

Every section is optional. Unknown fields are errors, so typing mistakes do not pass silently.

### Curves

Input curves and entry curves share one shape: `{"points": [[x, y], ...], "smooth": true|false}`.
The points start at `x = 0`, end at `x = 1` and have strictly increasing `x`; `y` is any number.
With `smooth: true` the curve is a monotone cubic through the points (Fritsch and Carlson's
method): between two points it never goes above or below them, so a flat stretch stays flat and a
rising stretch never dips. With `smooth: false` the points are joined by straight lines. Outside
`0..1` the curve continues along its end slopes.

### Layer 1: input curves

`input_curves` maps a tracking input name (kind `tracking` in `v2l mapping inputs`) to a curve.
Both axes are normalized over the input's nominal range (`range` in the listing): `0..1` for
blendshape-style inputs, `-45..45` degrees mapped onto `0..1` for the head angles, and so on. The
curve reshapes the input before the bridge reads it, like VBridger's input panel: an eye that
never opens fully can reach 1 at 0.6 with the curve in the example.

### Layer 2: bridge

The bridge turns tracking inputs into named outputs with SnenkBridge-format parameters
`{name, func, min, max, defaultValue, delayBuffer?}`. The preset (`bridge.preset` setting or
`--bridge`) is the base: a plain list of parameters, or the `{"format": "snek", "version": 1,
"title", "params": [...]}` wrapper. The `bridge` section of the mapping file is a list, one object
per output name:

- a name the preset has patches that output; omitted fields are inherited, a `delayBuffer` object
  is merged into the preset's, and `null` removes `delayBuffer` or `modifiers`;
- a new name adds an output (it needs `func` or `delayBuffer`, plus `min` and `max`);
- `"enabled": false` drops a preset output.

Formulas use SnenkBridge/VBridger syntax: numbers, input names, `+ - * / %`, `^` (power; it binds
tighter than a sign and groups left to right, so `-2 ^ 2` is -4 and `2 ^ 3 ^ 2` is 64),
comparisons, `&& || !`, `if(condition, then, else)`, `min`/`max` with any number of arguments, the
`math::` functions (`abs`, `sqrt`, `pow`, `sin`, `floor`, ...), `math::pi()`, and `//` and `/* */`
comments. Arithmetic is floating point. `WaveN` and `PingPongN` are time waves with an N ms period:
a triangle 0..1..0 and a ramp 0..1, counted from the start of the clip. A name that no tracking
input or bridge output provides reads as 0 (reported as `unknown` by `v2l bridge check`). Every
frame a formula that gives a non-finite value falls back to `defaultValue`, and the value is
clamped to `min..max`.

**Delay buffer** (`delayBuffer`, as in SnenkBridge). Instead of a formula the output follows
another value with a delay: `refParam` names a bridge output (evaluated first; cycles are errors)
or a tracking input. Every output frame, the reference value is clamped to `inMin..inMax` and
mapped linearly onto `outMin..outMax` (`outMin` when `inMin == inMax`). That value goes into a ring
of `max(delayCount, 1)` slots that starts out filled with 0, and the oldest slot is read back, so
the read value is the one from `delayCount - 1` frames earlier (zero for the first frames). The
output starts at 0 and moves toward the read value by `1 / max(smoothing, 1)` of the remaining
distance per frame. V2L counts frames at the output frame rate (`output.fps`).

**Modifiers** (V2L's VBridger-style post-processing, stored in an extra `modifiers` key that
SnenkBridge ignores) run after the formula or delay buffer, in this order:

| Modifier | Effect |
|---|---|
| `delayMs` | Shift the output later by this many milliseconds (rounded to whole frames; the first value fills the start). |
| `smoothing` | `0..1` (1 excluded): each frame the output moves `1 - smoothing` of the way to the new value. |
| `steps` | `[{trigger, target, holdMs?, threshold?}]` with increasing triggers. The output sits on the `target` of the highest step whose `trigger` the value has reached; below the first trigger it is `min`, and a step at `max` is implied unless a trigger reaches it. It drops one step when the value falls below that step's trigger by more than its `threshold`. A step entered by rising holds for at least `holdMs`. |

Steps produce flat plateaus; the parameter filtering afterwards softens them unless the entry's
`cutoff` is at least half the output frame rate, which switches the low-pass off.

### Layer 3: entries

The base entries come from the model's VTube Studio parameter settings (`*.vtube.json`) or the
standard fallback. An entry patches the base entry with the same `output`; omitted fields are
inherited. An entry for a parameter that has no base mapping adds one (it needs `input` unless it
sets `breathing`); `in` defaults to the input's nominal range, `out` to the parameter's range, and
both clamps are on. Entries may name bridge outputs that the `bridge` section adds.

Entry values use the same terms as the base mapping: the screen-direction probe's corrections
(direction, eye/brow side, body angle fed from the head) are applied to the patched entry, then
`invert`. The corrections are idempotent, so an entry shown by `v2l mapping show` (where `out` is
the range before `invert`, and `applied_out` the range used) can be written back unchanged. A body
parameter whose `input` the entry names explicitly keeps that input.

| Field | Meaning |
|---|---|
| `output` | Live2D parameter id (required). |
| `input` | Bridge output or tracking input that drives it (see `v2l mapping inputs`). |
| `in`, `out` | Input range mapped onto the output range. |
| `clamp_in`, `clamp_out` | Clamp before / after mapping. |
| `invert` | Mirror the output range, after the probe's direction correction. |
| `curve` | Curve from the normalized input (`in` mapped onto 0..1) to the normalized output (0..1 onto `out`). `null` is linear. |
| `cutoff` | Low-pass cutoff in Hz for this parameter; `null` uses its filtering group's setting. At or above half the output frame rate the low-pass is off. |
| `breathing` | Drive the parameter with the breathing cycle instead of an input. |
| `enabled` | `false` removes the mapping. |

All three layers apply after tracking, so changing them reuses cached tracking. Every report
records the effective entries (`mapping`), the mapping file (`mapping_overrides`) and warnings.

### Signals dump

`v2l run --dump-signals` also writes `<name>.signals.json`, so an editor can plot what the mapping
sees:

```json
{"fps": 30.0, "duration": 14.567,
 "inputs": {"yaw": [0.1234, ...], "EyeWideLeft": [...]},
 "inputs_raw": {"EyeWideLeft": [...]},
 "bridge": {"FaceAngleX": [...], "MouthOpen": [...]}}
```

There is one value per output frame (frame `i` is at `i / fps` seconds), rounded to 4 decimals,
`null` where nothing was tracked. `inputs` are the tracking inputs after the input curves,
`inputs_raw` the values before the curve for the inputs that have one, and `bridge` the bridge
outputs after their modifiers. Runs driven by audio add `"source": {"kind", "audio_inputs",
"generated"}` (which inputs came from the audio model and which were generated) and
`"audio": {"loudness": [...]}` (0..1 per output frame); see [audio.md](audio.md).

## 6. Commands and tools

```
v2l motion info FILE                         curves, segment types, counts, problems
v2l motion read FILE [--curves IDS]          editable JSON
v2l motion write EDIT.json --out FILE        editable JSON -> motion3.json
v2l motion edit FILE --ops OPS.json --out FILE [--ranges R.json]
v2l motion refit FILE --interpolation bezier|linear --tolerance X [--curves IDS] [--ranges R.json] --out FILE
v2l motion eval FILE (--times T,... | --fps N) [--curves IDS]
v2l motion validate FILE [--ranges R.json]
v2l mapping show --model DIR [--mapping FILE]     all three layers, effective, with sources; parameters
v2l mapping inputs [--mapping FILE]              input names with meaning and nominal range
v2l mapping validate FILE [--model DIR]
v2l bridge show [--bridge B] [--mapping FILE]    effective bridge outputs (preset | override | added | disabled)
v2l bridge check --func EXPR [--mapping FILE]    parse one formula: names read, unknown names, error position
v2l bridge validate FILE                         a SnenkBridge preset or a mapping file's bridge section
v2l run VIDEO --model DIR --name NAME --mapping FILE [--dump-signals] ...
```

`v2l motion` is also `python -m v2l.motionedit`. The motion commands, `v2l bridge`, `v2l mapping
inputs` and `v2l mapping validate` without `--model` need only the standard library. FILE may be a
motion3.json or an editable JSON file, or `-` for stdin; `--out -` writes nothing and returns the
result as `motion`. An existing `--out` file is replaced only with `--overwrite`. With `--json`
every command prints one object; errors are `{"ok": false, "error": {"type", "message"}}` (type
`invalid` adds `problems`, the failing `op_index`, or a formula's `position`) with exit code 2.
`v2l-remote mapping show --model DIR [--mapping FILE]` computes the mapping on the compute host,
`v2l-remote bridge ...` runs locally, and `v2l-remote run --mapping FILE --dump-signals` uploads the
file and brings the signals dump back with the results.

The MCP server offers `v2l_mapping_get(model, mapping?)` (all three layers and the inputs),
`v2l_bridge_check(func, mapping?)`, `v2l_motion_info(motion)`, `v2l_motion_read(motion, curves?)` and
`v2l_motion_edit(motion, operations, out, overwrite?, ranges?)`, and `v2l_generate` accepts `mapping`
(an object or a path). `v2l_motion_edit` writes only inside the output folder or `--allow-dir`
folders, into a model folder only with the `install` toolset, and is hidden in read-only mode.
