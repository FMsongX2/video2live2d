"""v2l command line (runs on the compute machine; see remote.py for the laptop side).

  v2l run VIDEO --model DIR --name NAME [--out DIR] [--preset NAME|FILE] [--config FILE]...
          [--set section.name=value]... [shortcut flags] [--mapping FILE] [--dump-signals]
          [--no-install] [--dry-run] [--json] [--progress text|json]
  v2l probe --model DIR [--details] [--json]
  v2l mapping show --model DIR [--mapping FILE] [--bridge NAME|FILE] [--json]
  v2l mapping inputs [--bridge NAME|FILE] [--mapping FILE] [--json]
  v2l mapping validate FILE [--model DIR] [--bridge NAME|FILE] [--json]
  v2l bridge show [--bridge NAME|FILE] [--mapping FILE] [--json]
  v2l bridge check --func EXPR [--bridge NAME|FILE] [--mapping FILE] [--json]
  v2l bridge validate FILE [--bridge NAME|FILE] [--json]
  v2l motion info|read|write|edit|refit|eval|validate ...   motion editing (v2l motion --help)
  v2l presets list [--json] | show NAME | save NAME FILE
  v2l config schema|defaults|presets|validate FILE...
  v2l version [--json]
  v2l mcp [--read-only] [--toolsets ...] [--allow-dir DIR]...   MCP server on stdio (`mcp` extra)

--mapping FILE is a v2l.mapping file: input curves, bridge changes and parameter mappings
(docs/motion-editing.md section 5). --dump-signals also writes <name>.signals.json.

Settings: defaults < --preset < --config files in order < shortcut flags < --set in order.
With --json, stdout carries exactly one JSON object and every other line goes to stderr.
Exit codes: 0 ok, 1 runtime failure, 2 usage or validation error, 3 unusable input (the video
does not open or no face is tracked), 4 compute host unreachable (v2l-remote), 130 interrupted.
Only stdlib modules are imported eagerly, so everything but run/probe/mapping show (and mapping
validate --model) works without the tracking dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
import time
import traceback

from . import config as C
from .errors import UnusableInput

BRIDGES = pathlib.Path(__file__).parent / "bridges"


class UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str):
        super().__init__(message)
        self.parser = parser


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # raised instead of exiting so --json can report it
        raise UsageError(self, message)


def _shortcuts(a: argparse.Namespace) -> dict:
    """The shortcut flags given on the command line as a partial configuration."""
    d: dict = {}

    def put(key: str, value) -> None:
        sec, name = key.split(".")
        d.setdefault(sec, {})[name] = value

    for flag, key in (("style", "style.name"), ("fps", "output.fps"), ("fade", "output.fade"),
                      ("start", "input.start"), ("end", "input.end"), ("bridge", "bridge.preset"),
                      ("rare_stride", "expressions.stride")):
        if getattr(a, flag) is not None:
            put(key, getattr(a, flag))
    if a.loop:
        put("output.loop", True)
    if a.flip:
        put("input.flip", True)
    if a.neutral is not None:
        d.setdefault("calibration", {}).update(
            C.parse_assignment(f"calibration.neutral_window={a.neutral}")["calibration"])
    if a.no_preview:
        put("preview.enabled", False)
    if a.no_rare:
        put("expressions.enabled", False)
    return d


def settings(a: argparse.Namespace) -> C.Config:
    """Defaults < --preset < --config files in order < shortcut flags < --set assignments in order."""
    cfg = C.merge(C.build(a.config, preset=a.preset), _shortcuts(a))
    for s in a.set:
        cfg = C.merge(cfg, C.parse_assignment(s))
    return cfg


def check_inputs(video: str, model: str, cfg: C.Config) -> tuple[pathlib.Path, pathlib.Path]:
    """Cheap checks before any tracking: the video file, the model folder and the bridge preset."""
    v = pathlib.Path(video).expanduser().resolve()
    if not v.is_file():
        raise FileNotFoundError(f"video not found: {v}")
    m = pathlib.Path(model).expanduser().resolve()
    if not m.is_dir():
        raise FileNotFoundError(f"model folder not found: {m}")
    found = [p.name for p in m.glob("*.model3.json") if not p.name.endswith("items_pinned_to_model.json")]
    if len(found) != 1:
        raise FileNotFoundError(f"expected one *.model3.json in {m}, found {found}")
    preset = cfg.bridge.preset
    if not pathlib.Path(preset).is_file() and not (BRIDGES / f"{preset}.json").is_file():
        names = ", ".join(sorted(p.stem for p in BRIDGES.glob("*.json")))
        raise C.ConfigError(f"bridge.preset: {preset!r} is neither a file nor a built-in preset ({names})")
    return v, m


def planned_files(out: pathlib.Path, name: str, cfg: C.Config, signals: bool = False) -> dict:
    return {"motion": str(out / f"{name}.motion3.json"), "report": str(out / f"{name}.report.json"),
            "preview": str(out / f"{name}.preview.mp4") if cfg.preview.enabled else None,
            "signals": str(out / f"{name}.signals.json") if signals else None}


def version() -> str:
    from importlib import metadata

    try:
        return metadata.version("v2l")
    except metadata.PackageNotFoundError:  # a checkout on PYTHONPATH
        import tomllib

        pyproject = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
        return tomllib.loads(pyproject.read_text())["project"]["version"] if pyproject.is_file() else "unknown"


def _parser() -> _Parser:
    ap = _Parser(prog="v2l", description="video of a person -> Live2D motion3.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="track, solve, write motion3.json, render preview, write report")
    r.add_argument("video")
    r.add_argument("--model", required=True, help="Live2D model folder (*.model3.json, optional *.vtube.json)")
    r.add_argument("--name", required=True, help="output name; the motion is <name>.motion3.json")
    r.add_argument("--out", default="v2l_out", help="output folder (default: v2l_out)")
    r.add_argument("--preset", metavar="NAME|FILE", help="settings preset applied before --config files "
                   "(v2l presets list)")
    r.add_argument("--config", action="append", default=[], metavar="FILE",
                   help="JSON configuration file (python -m v2l.config schema); repeatable, later files win")
    r.add_argument("--set", action="append", default=[], metavar="SECTION.NAME=VALUE",
                   help="one setting, e.g. intensity.smile=1.5 or calibration.neutral_window=2:3; "
                        "repeatable, applied last")
    g = r.add_argument_group("shortcuts", "each sets one configuration key")
    g.add_argument("--style", help="style.name: cute | faithful")
    g.add_argument("--fps", type=float, help="output.fps")
    g.add_argument("--fade", type=float, help="output.fade: motion fade in/out seconds")
    g.add_argument("--loop", action="store_true", help="output.loop=true: make the motion loop seamlessly")
    g.add_argument("--flip", action="store_true", help="input.flip=true: input was recorded mirrored (selfie)")
    g.add_argument("--start", type=float, help="input.start, seconds")
    g.add_argument("--end", type=float, help="input.end, seconds")
    g.add_argument("--neutral", metavar="START:END", help="calibration.neutral_window: rest-face window in seconds")
    g.add_argument("--bridge", help="bridge.preset: preset name or SnenkBridge-format JSON file")
    g.add_argument("--no-preview", action="store_true", help="preview.enabled=false")
    g.add_argument("--no-rare", action="store_true", help="expressions.enabled=false: skip cheek puff / tongue out")
    g.add_argument("--rare-stride", type=int, help="expressions.stride: score every n-th frame")
    r.add_argument("--mapping", metavar="FILE", help="v2l.mapping file: input curves, bridge changes and "
                   "parameter mappings for this model (v2l mapping show/inputs, v2l bridge show)")
    r.add_argument("--dump-signals", action="store_true",
                   help="also write <name>.signals.json: tracking inputs and bridge outputs per output frame")
    r.add_argument("--no-install", action="store_true", help="do not copy the motion into the model folder")
    r.add_argument("--dry-run", action="store_true",
                   help="check the inputs and settings and print the planned outputs; no tracking")
    r.add_argument("--json", action="store_true", help="print one JSON result object on stdout, logs on stderr")
    r.add_argument("--progress", choices=("text", "json"), default="text",
                   help="json: stage/progress/log events as JSON lines on stderr")

    p = sub.add_parser("probe", help="read a model's parameter ranges and screen directions (cached)")
    p.add_argument("--model", required=True)
    p.add_argument("--details", action="store_true", help="include the measured effect of every probed parameter")
    p.add_argument("--json", action="store_true", help="print one JSON result object on stdout")

    mp = sub.add_parser("mapping", help="effective input -> parameter mapping, inputs, override validation")
    mps = mp.add_subparsers(dest="action", required=True)
    ms = mps.add_parser("show", help="the model's effective mapping (with --mapping overrides) and its parameters")
    ms.add_argument("--model", required=True)
    ms.add_argument("--mapping", metavar="FILE", help="v2l.mapping override file")
    ms.add_argument("--bridge", default="default", help="bridge preset name or file (default: default)")
    ms.add_argument("--json", action="store_true")
    mi = mps.add_parser("inputs", help="every input name a mapping may use, with meaning and nominal range")
    mi.add_argument("--bridge", default="default", help="bridge preset name or file (default: default)")
    mi.add_argument("--mapping", metavar="FILE", help="include the bridge outputs this mapping file adds")
    mi.add_argument("--json", action="store_true")
    mv = mps.add_parser("validate", help="check a v2l.mapping file (against a model with --model)")
    mv.add_argument("file")
    mv.add_argument("--model", help="also check parameter ids and added entries against this model")
    mv.add_argument("--bridge", default="default", help="bridge preset name or file (default: default)")
    mv.add_argument("--json", action="store_true")

    bp = sub.add_parser("bridge", help="effective bridge outputs, formula check, bridge file validation")
    bps = bp.add_subparsers(dest="action", required=True)
    for action, help_ in (("show", "the bridge preset with a mapping file's bridge changes"),
                          ("check", "parse one formula: names it reads, unknown names, error position"),
                          ("validate", "check a SnenkBridge preset or the bridge section of a v2l.mapping file")):
        p = bps.add_parser(action, help=help_)
        if action == "check":
            p.add_argument("--func", required=True, metavar="EXPR")
        if action == "validate":
            p.add_argument("file")
        else:
            p.add_argument("--mapping", metavar="FILE", help="v2l.mapping file whose bridge section applies")
        p.add_argument("--bridge", default="default", help="bridge preset name or file (default: default)")
        p.add_argument("--json", action="store_true")

    ps = sub.add_parser("presets", help="list, show and save settings presets")
    pss = ps.add_subparsers(dest="action", required=True)
    pl = pss.add_parser("list", help="built-in and user presets")
    pl.add_argument("--json", action="store_true")
    pss.add_parser("show", help="a preset's settings as JSON").add_argument("preset", metavar="NAME|FILE")
    sv = pss.add_parser("save", help="validate FILE and save it as user preset NAME")
    sv.add_argument("preset", metavar="NAME")
    sv.add_argument("file")
    v = sub.add_parser("version", help="v2l version")
    v.add_argument("--json", action="store_true")
    sub.add_parser("config", help="settings schema, defaults and validation (schema | defaults | presets | "
                   "validate FILE...)", add_help=False)
    sub.add_parser("mcp", help="MCP server for AI agents on stdio (pip install 'v2l[mcp]'); v2l mcp --help",
                   add_help=False)
    sub.add_parser("motion", help="read, edit, refit, evaluate and validate motion3.json files (v2l motion --help)",
                   add_help=False)
    return ap


class _Events:
    """--progress json: every stderr line is a JSON event (stage, progress, log). Whatever else
    reaches file descriptor 2 (native tracker logs, warnings, tracebacks) is wrapped into log
    events by a reader thread until close()."""

    def __init__(self):
        import threading

        self.last: dict[str, float] = {}
        sys.stderr.flush()
        self._real = os.dup(2)
        self.out = os.fdopen(os.dup(self._real), "w", encoding="utf-8", buffering=1)
        r, w = os.pipe()
        os.dup2(w, 2)
        os.close(w)
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._wrap, args=(os.fdopen(r, encoding="utf-8", errors="replace"),),
                                        daemon=True)
        self._reader.start()

    def _wrap(self, stream) -> None:
        for line in stream:
            if line.strip():
                self._write({"event": "log", "message": line.rstrip()})

    def _write(self, e: dict) -> None:
        with self._lock:
            self.out.write(json.dumps(e, ensure_ascii=False) + "\n")

    def close(self) -> None:
        sys.stderr.flush()
        os.dup2(self._real, 2)  # closes the pipe's write end; the reader drains and stops
        os.close(self._real)
        self._reader.join(timeout=5)
        self.out.flush()

    def emit(self, e: dict) -> None:
        if e["event"] == "progress":
            now = time.monotonic()
            if e["done"] < e["total"] and now - self.last.get(e["stage"], -1.0) < 0.5:
                return
            self.last[e["stage"]] = now
        self._write(e)

    def log(self, *parts) -> None:
        self.emit({"event": "log", "message": " ".join(map(str, parts))})


def _summary(rep: dict) -> dict:
    return {k: rep[k] for k in ("name", "face_rate", "curves", "points", "blinks", "rare_seconds", "warnings",
                                "mapping_notes", "bridge_inputs_without_signal")}


def load_mapping(path: str | None, bridge: str = "default") -> dict | None:
    """A v2l.mapping file, checked without the model (raises mapping.MappingError)."""
    if not path:
        return None
    from . import mapping

    data = mapping.load_overrides(path)
    errs, _ = mapping.check(data, bridge=bridge)
    if errs:
        raise mapping.MappingError(errs)
    return data


def _run(a: argparse.Namespace, log, event) -> dict:
    cfg = settings(a)  # configuration and input errors before any heavy import
    video, model = check_inputs(a.video, a.model, cfg)
    overrides = load_mapping(a.mapping, cfg.bridge.preset)
    out = pathlib.Path(a.out).expanduser().resolve()
    files = planned_files(out, a.name, cfg, a.dump_signals)
    installed = None if a.no_install else str(model / f"{a.name}.motion3.json")
    if a.dry_run:
        return {"ok": True, "dry_run": True, "name": a.name, "out_dir": str(out), "files": files,
                "installed": installed, "settings": C.to_dict(cfg), "mapping": overrides}
    from .pipeline import run

    rep = run(video, model, a.name, out, cfg, install=not a.no_install, log=log, event=event,
              mapping_overrides=overrides, dump_signals=a.dump_signals)
    if files["preview"] and not pathlib.Path(files["preview"]).exists():
        files["preview"] = None
    return {"ok": True, "name": a.name, "out_dir": str(out), "files": files, "installed": installed,
            "summary": _summary(rep)}


def _presets(a: argparse.Namespace) -> dict | None:
    if a.action == "list":
        found = C.presets()
        if a.json:
            return {"ok": True, "presets": found}
        for p in found:
            tag = "built-in" if p["builtin"] else "user"
            print(f"{p['name']:14s} {tag:8s} {p.get('error') or p.get('description', '')}")
        return None
    if a.action == "show":
        path = C.find_preset(a.preset)
        data = C.load(path)
        C.merge(C.Config(), data)
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return None
    path = C.save_user_preset(a.preset, C.load(a.file))
    print(f"saved {path}")
    return None


def _probe(a: argparse.Namespace, log) -> dict:
    from .pipeline import prepare_model

    m = prepare_model(a.model, log=log)
    out = {"ok": True, "model": m.name, "parameters": len(m.ranges), "physics_outputs": len(m.physics_outputs),
           "vtube_mapping": bool(m.vtube), "framing": m.probe.get("framing")}
    if a.details:
        out["params"] = m.probe["params"]
    return out


def _mapping(a: argparse.Namespace, log) -> dict | None:
    from . import inputs, mapping

    if a.action == "inputs":
        res = {"ok": True, **inputs.listing(a.bridge, (load_mapping(a.mapping, a.bridge) or {}).get("bridge"))}
        if a.json:
            return res
        for i in res["inputs"]:
            print(f"{i['name']:30s} {i['kind']:8s} {i['range'][0]:g}..{i['range'][1]:g}  {i['meaning']}")
        return None
    if a.action == "validate":
        data = mapping.load_overrides(a.file)
        if a.model:
            from .pipeline import prepare_model

            m = prepare_model(a.model, log=log)
            errs, warns = mapping.check(data, m.ranges, mapping.entries_for(m), m.physics_outputs, a.bridge)
        else:
            errs, warns = mapping.check(data, bridge=a.bridge)
        if errs:
            res = {"ok": False, "valid": False, "problems": errs, "warnings": warns,
                   "error": {"type": "invalid", "message": "; ".join(errs), "problems": errs}}
        else:
            res = {"ok": True, "valid": True, "problems": [], "warnings": warns}
        if a.json:
            return res
        for line in errs + [f"warning: {w}" for w in warns]:
            print(line, file=sys.stderr if errs else sys.stdout)
        if not errs:
            print("valid")
        return None if not errs else {"ok": False, "quiet": True}
    from .pipeline import prepare_model

    m = prepare_model(a.model, log=log)
    data = load_mapping(a.mapping, a.bridge)
    res = {"ok": True, **mapping.show(m, data, a.bridge), "mapping_file": str(pathlib.Path(a.mapping).expanduser().resolve())
           if a.mapping else None}
    if a.json:
        return res
    print(f"{res['model']}: {res['base']} mapping, bridge {res['bridge']}")
    for e in res["entries"]:
        src = e["source"] + ("" if e["enabled"] else ", disabled")
        drive = "breathing" if e["breathing"] else f"{e['input']} {e['in'][0]:g}..{e['in'][1]:g}"
        extra = (" invert" if e["invert"] else "") + (f" response={e['response']}" if e["response"] else "")
        print(f"  {e['output']:28s} <- {drive:32s} -> {e['applied_out'][0]:g}..{e['applied_out'][1]:g}"
              f"{extra}  ({src})")
    for line in res["notes"]:
        print(f"  note: {line}")
    for line in res["warnings"]:
        print(f"  warning: {line}")
    unmapped = [p["id"] for p in res["parameters"] if not p["mapped"] and not p["physics"]]
    print(f"  {len(unmapped)} parameters without a mapping: {', '.join(unmapped)}")
    return None


def _bridge(a: argparse.Namespace) -> dict | None:
    """v2l bridge show | check | validate (standard library)."""
    from . import bridge as B, mapping

    data = load_mapping(a.mapping, a.bridge) if getattr(a, "mapping", None) else None
    if a.action == "show":
        res = {"ok": True, **mapping.bridge_show(data, a.bridge)}
        if a.json:
            return res
        print(f"bridge preset {res['bridge']}")
        for p in res["params"]:
            what = (f"delay {p['delayBuffer']['refParam']}" if p["delayBuffer"] else p["func"])
            extra = "  +modifiers" if p["modifiers"] else ""
            print(f"  {p['name']:28s} {p['source']:9s} {p['min']:g}..{p['max']:g}  {what}{extra}")
        return None
    if a.action == "check":
        _, preset = B.read_preset(a.bridge)
        eff = B.effective(preset, (data or {}).get("bridge"))
        res = B.check(a.func, B.known_names(p["name"] for p in eff if B._active(p)))
        res = {"ok": res["valid"], **res}
        if not res["valid"]:
            res["error"] = {"type": "invalid", "message": res["problem"]["message"], **res["problem"]}
        if a.json:
            return res
        if res["valid"]:
            print(f"valid; reads {', '.join(res['inputs']) or 'nothing'}"
                  + (f"; unknown (read as 0): {', '.join(res['unknown'])}" if res["unknown"] else ""))
            return None
        p = res["problem"]
        print(f"{a.func}\n{' ' * (p['position'] or 0)}^ {p['message']}", file=sys.stderr)
        return {"ok": False, "quiet": True}
    raw = json.loads(pathlib.Path(a.file).expanduser().read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict) and raw.get("format") == mapping.FORMAT:
        errs, warns = mapping.check(raw, bridge=a.bridge)
        errs = [e for e in errs if e.startswith("bridge")]
    else:
        _, params = B.read_preset(a.file)
        errs, warns = B.problems(params), []
    res = ({"ok": True, "valid": True, "problems": [], "warnings": warns} if not errs else
           {"ok": False, "valid": False, "problems": errs, "warnings": warns,
            "error": {"type": "invalid", "message": "; ".join(errs), "problems": errs}})
    if a.json:
        return res
    for line in errs + [f"warning: {w}" for w in warns]:
        print(line, file=sys.stderr if errs else sys.stdout)
    if not errs:
        print("valid")
    return None if not errs else {"ok": False, "quiet": True}


def _error(e: BaseException) -> tuple[str, int]:
    """(error type, exit code) for an exception."""
    from .mapping import MappingError

    if isinstance(e, UsageError):
        return "usage", 2
    if isinstance(e, C.ConfigError):
        return "config", 2
    if isinstance(e, MappingError):
        return "invalid", 2
    if isinstance(e, UnusableInput):
        return "unusable_input", 3
    if isinstance(e, (FileNotFoundError, NotADirectoryError, ValueError)):
        return "input", 2
    if isinstance(e, KeyboardInterrupt):
        return "interrupted", 130
    return "runtime", 1


def _interrupt(signum, frame):
    raise KeyboardInterrupt  # SIGTERM cleans up like Ctrl-C (the motion copied for the preview is removed)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["config"]:
        return C.main(argv[1:])
    if argv[:1] == ["mcp"]:
        from .mcp_server import main as mcp_main

        return mcp_main(argv[1:])
    if argv[:1] == ["motion"]:
        from .motionedit import main as motion_main

        return motion_main(argv[1:], prog="v2l motion")

    as_json = "--json" in argv
    events: _Events | None = None
    saved_stdout = None
    if as_json:  # only the result object may reach stdout, whatever libraries print meanwhile
        sys.stdout.flush()
        saved_stdout = os.dup(1)
        os.dup2(2, 1)

    def log(*parts) -> None:
        if events:
            events.log(*parts)
        else:
            print(*parts, file=sys.stderr if as_json else sys.stdout, flush=True)

    result: dict | None = None
    code = 0
    try:
        a = _parser().parse_args(argv)
        if a.cmd in ("run", "probe", "mapping"):
            signal.signal(signal.SIGTERM, _interrupt)
        if a.cmd == "run":
            if a.progress == "json":
                events = _Events()
                if as_json:  # library prints become log events too
                    sys.stdout.flush()
                    os.dup2(2, 1)
            if events:
                sink = events.emit
            else:
                from .pipeline import text_events

                sink = text_events(log) if not a.dry_run else None
            result = _run(a, log, sink)
            if a.dry_run and not as_json:
                print(json.dumps({k: v for k, v in result.items() if k != "ok"}, ensure_ascii=False, indent=1))
                result = None
        elif a.cmd == "presets":
            result = _presets(a)
        elif a.cmd in ("mapping", "bridge"):
            result = _mapping(a, log) if a.cmd == "mapping" else _bridge(a)
            if result is not None and not result["ok"]:
                code = 2
                if result.pop("quiet", False):
                    result = None
            elif result is not None and not as_json:
                result = None
        elif a.cmd == "version":
            v = version()
            if a.json:
                result = {"ok": True, "version": v, "config_version": C.CONFIG_VERSION,
                          "python": sys.version.split()[0]}
            else:
                print(f"v2l {v}")
        else:
            result = _probe(a, log)
            if not as_json:
                print(json.dumps({k: v for k, v in result.items() if k not in ("ok", "params")}, indent=1))
                if a.details:
                    for pid, v in result["params"].items():
                        mv = " ".join(f"{k}={v[k]:+8.2f}" for k in ("dx", "dy", "rot") if k in v)
                        print(f"{pid:28s} {mv:40s} side={v.get('side', '-')}" if v.get("visible")
                              else f"{pid:28s} no visible change")
                result = None
        if result is not None and not as_json:
            print(json.dumps(result["summary"], ensure_ascii=False, indent=1))
            result = None
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - every failure is reported in the chosen format
        kind, code = _error(e)
        if kind == "runtime":
            traceback.print_exc(file=sys.stderr)
        if isinstance(e, UsageError) and not as_json:
            e.parser.print_usage(sys.stderr)
            print(f"{e.parser.prog}: error: {e}", file=sys.stderr)
        elif not as_json:
            print(f"v2l: {e}", file=sys.stderr)
        result = {"ok": False, "error": {"type": kind, "message": str(e)}} if as_json else None
        if result is not None and getattr(e, "problems", None):
            result["error"]["problems"] = e.problems
    finally:
        if saved_stdout is not None:
            sys.stdout.flush()
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        if events:
            events.close()
    if result is not None:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
