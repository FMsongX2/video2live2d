"""Laptop side: run v2l on the GPU/CPU server over SSH and bring the results back.

Standard library only, so it runs from any Python 3.11+ without the heavy dependencies.

  v2l-remote setup                                   install/refresh v2l on the server
  v2l-remote run VIDEO --model DIR --name NAME [--config FILE]... [--set k=v]... [--json]
                                                     track + solve + preview on the server;
                                                     results land in --out/NAME, the motion is
                                                     also copied into the local model folder
                                                     (--no-install: not); --config and --mapping
                                                     files are uploaded; other options go to
                                                     `v2l run`
  v2l-remote probe --model DIR [--details] [--json]  probe a local model on the server
  v2l-remote mapping show --model DIR [--mapping FILE] [--json]
                                                     effective mapping of a local model, computed
                                                     on the server (the model and the mapping file
                                                     are uploaded); `mapping inputs` and `mapping
                                                     validate FILE` without --model run here
  v2l-remote bridge show|check|validate ...          `v2l bridge ...`, run here (no host needed)
  v2l-remote test                                    run the test suite on the server
  v2l-remote regress                                 run regression/videos on the server with the
                                                     model in V2L_REGRESS_MODEL

The server comes from V2L_HOST (user@host, required) and the remote directory from V2L_DIR
(default: v2l, relative to the remote home).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from . import cli
from .cli import UsageError, _Parser
from .config import ConfigError, to_dict
from .mapping import MappingError

HOST = os.environ.get("V2L_HOST", "")
RDIR = os.environ.get("V2L_DIR", "v2l")
ROOT = pathlib.Path(__file__).resolve().parents[2]
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def ssh(command: str, capture: bool = False) -> str:
    r = sh([*SSH, HOST, command], capture_output=capture, text=True)
    return r.stdout if capture else ""


def rsync(src: str, dst: str, *extra: str) -> None:
    sh(["rsync", "-az", "-e", " ".join(SSH), *extra, src, dst])


def push_code() -> None:
    rsync(f"{ROOT}/", f"{HOST}:{RDIR}/", "--delete", "--include=/src/***", "--include=/tests/***",
          "--include=/regression/", "--include=/regression/*.py", "--include=/research/", "--include=/research/*.py",
          "--include=/pyproject.toml", "--include=/README.md", "--include=/LICENSE", "--exclude=*")


def setup() -> None:
    push_code()
    ssh(f"set -e; cd {shlex.quote(RDIR)}; [ -x .venv/bin/python ] || python3 -m venv .venv; "
        ".venv/bin/pip install -q -e '.[test]'; "
        ".venv/bin/python -c 'from v2l.signals import tracker_model as t; from v2l.render import cubism_core as c; "
        "from v2l.assets import web_assets as w; t(\"face_landmarker.task\"); t(\"pose_landmarker_full.task\"); w(); "
        "print(\"core:\", c())'")
    print(f"v2l ready on {HOST}:{RDIR}")


def _say(a: argparse.Namespace):
    """Human lines: stdout normally, stderr when stdout is reserved for the --json object."""
    return lambda *parts: print(*parts, file=sys.stderr if getattr(a, "json", False) else sys.stdout, flush=True)


def _stage(a: argparse.Namespace):
    """With --progress json, the laptop-side stages join the server's event stream on stderr."""
    if getattr(a, "progress", None) != "json":
        return lambda name: None
    return lambda name: print(json.dumps({"event": "stage", "stage": name}), file=sys.stderr, flush=True)


def _remote(command: str, capture: bool, rid: str) -> tuple[int, str, bool]:
    """Runs a shell command on the server (inside RDIR); stderr streams through. A SIGTERM/SIGINT/SIGHUP stops
    the ssh session and the server process whose command line contains outputs/<rid>.
    Returns (exit code, captured stdout, cancelled)."""
    proc = subprocess.Popen([*SSH, HOST, f"cd {shlex.quote(RDIR)} && {command}"],
                            stdout=subprocess.PIPE if capture else None, text=True)
    cancelled = False

    def stop(signum, frame):
        nonlocal cancelled
        cancelled = True
        proc.terminate()

    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        out, _ = proc.communicate()
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
    if cancelled:  # "[o]" keeps pkill from matching the shell that runs it; then remove the run's files
        d = shlex.quote(RDIR)
        # (rm names the folder through $r so this shell's own command line does not match the pattern)
        subprocess.run([*SSH, HOST, f"pkill -f -- {shlex.quote('[o]utputs/' + rid)}; sleep 1; r={rid}; "
                        f"rm -rf {d}/inputs/configs/$r*.json {d}/outputs/$r"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode, out or "", cancelled


def _upload_model(model: pathlib.Path) -> str:
    ssh(f"mkdir -p {shlex.quote(RDIR)}/models {shlex.quote(RDIR)}/inputs/configs")
    rsync(f"{model}/", f"{HOST}:{RDIR}/models/{model.name}/", "--delete", "--exclude=v2l_*.motion3.json")
    return f"models/{model.name}"  # relative to RDIR


def resolve_settings(a: argparse.Namespace, passthrough: list[str]):
    """Settings for `run`, resolved HERE with the same rules as `v2l run` (so --preset finds the
    laptop's presets and every file path is read locally): returns (cli namespace, Config)."""
    ns = cli._parser().parse_args(["run", a.video, "--model", a.model, "--name", a.name, *passthrough])
    return ns, cli.settings(ns)


def run(a: argparse.Namespace, passthrough: list[str]) -> int:
    say = _say(a)
    ns, cfg = resolve_settings(a, passthrough)
    video, model = cli.check_inputs(a.video, a.model, cfg)
    overrides = cli.load_mapping(ns.mapping, cfg.bridge.preset)  # checked here, before any upload
    out = pathlib.Path(a.out).expanduser().resolve() / a.name
    files = cli.planned_files(out, a.name, cfg, ns.dump_signals)
    installed = None if a.no_install else model / f"{a.name}.motion3.json"
    if ns.dry_run:
        res = {"ok": True, "dry_run": True, "name": a.name, "out_dir": str(out), "files": files,
               "installed": str(installed) if installed else None, "settings": to_dict(cfg), "mapping": overrides}
        print(json.dumps(res, ensure_ascii=False, indent=None if a.json else 1), flush=True)
        return 0
    rid = f"{re.sub(r'[^A-Za-z0-9_-]', '_', a.name)}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    stage = _stage(a)
    stage("upload")
    push_code()
    rmodel = _upload_model(model)
    rvideo = f"inputs/{video.name}"
    rsync(str(video), f"{HOST}:{RDIR}/{rvideo}")
    bridge = pathlib.Path(cfg.bridge.preset).expanduser()
    if bridge.is_file():  # a local bridge file travels with the run
        rb = f"inputs/configs/{rid}-bridge.json"
        rsync(str(bridge), f"{HOST}:{RDIR}/{rb}")
        cfg = dataclasses.replace(cfg, bridge=dataclasses.replace(cfg.bridge, preset=rb))
    # One file carries the effective settings (preset, --config files, shortcut flags and --set
    # already applied); it is unique per run and removed after it.
    rconfig = f"inputs/configs/{rid}.json"
    _upload_json(to_dict(cfg), rconfig)
    rout = f"outputs/{rid}"
    cmd = [".venv/bin/v2l", "run", rvideo, "--model", rmodel, "--name", a.name, "--out", rout, "--no-install",
           "--config", rconfig]
    if overrides is not None:  # like the settings: one file per run, removed after it
        rmap = f"inputs/configs/{rid}-mapping.json"
        _upload_json(overrides, rmap)
        cmd += ["--mapping", rmap]
    if ns.dump_signals:
        cmd.append("--dump-signals")
    if a.json:
        cmd.append("--json")
    if a.progress:
        cmd += ["--progress", a.progress]
    code, stdout, cancelled = _remote(f"{shlex.join(cmd)}; code=$?; rm -f inputs/configs/{rid}*.json; exit $code",
                                      capture=a.json, rid=rid)
    if cancelled:
        raise Cancelled("cancelled; the server job was stopped")
    if code == 255:
        raise Unreachable(f"lost the connection to {HOST}")
    result = None
    if a.json:
        try:
            result = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            raise RemoteError(f"the server returned no result (exit {code})") from None
        if not result.get("ok"):
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return code or 1
    elif code != 0:
        return code
    stage("download")
    out.mkdir(parents=True, exist_ok=True)
    rsync(f"{HOST}:{RDIR}/{rout}/", f"{out}/")
    ssh(f"rm -rf {shlex.quote(RDIR)}/{rout}")
    if installed:
        installed.write_bytes(pathlib.Path(files["motion"]).read_bytes())
        say(f"installed {installed}")
    say(f"results in {out}")
    if a.json:
        for k in ("preview", "signals"):
            if not result["files"].get(k):
                files[k] = None
        print(json.dumps({"ok": True, "name": a.name, "out_dir": str(out), "files": files,
                          "installed": str(installed) if installed else None,
                          "summary": result.get("summary")}, ensure_ascii=False), flush=True)
    return 0


def check(a: argparse.Namespace) -> int:
    """Is the compute host reachable, and is v2l set up there?"""
    t0 = time.monotonic()
    r = subprocess.run([*SSH, HOST, f"test -x {shlex.quote(RDIR)}/.venv/bin/v2l && echo ready || echo missing"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Unreachable(f"cannot reach {HOST} over ssh: {r.stderr.strip()[-300:] or f'exit {r.returncode}'}")
    res = {"ok": True, "host": HOST, "dir": RDIR, "latency_ms": round((time.monotonic() - t0) * 1000),
           "installed": r.stdout.strip() == "ready"}
    if a.json:
        print(json.dumps(res), flush=True)
    else:
        print(f"{HOST} reachable ({res['latency_ms']} ms); v2l "
              f"{'installed' if res['installed'] else 'not installed (run v2l-remote setup)'} in {RDIR}")
    return 0


def probe(a: argparse.Namespace, passthrough: list[str]) -> int:
    model = pathlib.Path(a.model).expanduser().resolve()
    if not model.is_dir():
        raise InputError(f"model folder not found: {model}")
    push_code()
    rmodel = _upload_model(model)
    rid = f"probe-{secrets.token_hex(3)}"
    code, _, cancelled = _remote(shlex.join([".venv/bin/v2l", "probe", "--model", rmodel, *passthrough]),
                                 capture=False, rid=rid)
    if cancelled:
        raise Cancelled("cancelled")
    return code


def _upload_json(data, rpath: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(data, f)
    try:
        rsync(f.name, f"{HOST}:{RDIR}/{rpath}")
    finally:
        os.unlink(f.name)


def mapping(a: argparse.Namespace, passthrough: list[str]) -> int:
    """`v2l mapping show/validate` with a model run on the server; the rest runs here."""
    local = ["mapping", a.action, *passthrough]
    ns = cli._parser().parse_args(local)  # usage errors before any upload
    model_arg = getattr(ns, "model", None)
    if ns.action == "inputs" or model_arg is None:
        return cli.main(local)
    model = pathlib.Path(model_arg).expanduser().resolve()
    if not model.is_dir():
        raise InputError(f"model folder not found: {model}")
    mapping_file = ns.mapping if ns.action == "show" else ns.file
    data = cli.load_mapping(mapping_file, ns.bridge) if ns.action == "show" else None
    if ns.action == "validate":
        from .mapping import load_overrides

        data = load_overrides(mapping_file)
    if pathlib.Path(ns.bridge).expanduser().is_file():
        raise InputError("--bridge FILE is not supported here; use a built-in preset name")
    push_code()
    rmodel = _upload_model(model)
    rid = f"mapping-{secrets.token_hex(3)}"
    cmd = [".venv/bin/v2l", "mapping", ns.action]
    if ns.action == "validate":
        cmd.append(f"inputs/configs/{rid}.json")
    cmd += ["--model", rmodel, "--bridge", ns.bridge]
    if data is not None:
        _upload_json(data, f"inputs/configs/{rid}.json")
        if ns.action == "show":
            cmd += ["--mapping", f"inputs/configs/{rid}.json"]
    if ns.json:
        cmd.append("--json")
    code, stdout, cancelled = _remote(f"{shlex.join(cmd)}; code=$?; rm -f inputs/configs/{rid}.json; exit $code",
                                      capture=ns.json, rid=rid)
    if cancelled:
        raise Cancelled("cancelled")
    if code == 255:
        raise Unreachable(f"lost the connection to {HOST}")
    if ns.json:
        try:
            res = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            raise RemoteError(f"the server returned no result (exit {code})") from None
        if res.get("ok") and ns.action == "show":  # local paths for the caller
            res["model_dir"] = str(model)
            res["mapping_file"] = str(pathlib.Path(mapping_file).expanduser().resolve()) if mapping_file else None
        print(json.dumps(res, ensure_ascii=False), flush=True)
    return code


class InputError(Exception):
    pass


class RemoteError(Exception):
    pass


class Unreachable(Exception):
    pass


class Cancelled(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = _Parser(prog="v2l-remote")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("test")
    sub.add_parser("regress")
    sub.add_parser("version", help="version of the local v2l code").add_argument("--json", action="store_true")
    sub.add_parser("check", help="is the compute host reachable and set up?").add_argument("--json", action="store_true")
    r = sub.add_parser("run", help="other `v2l run` options (--preset, --config, --set, shortcut flags, --dry-run) "
                                   "are resolved here and sent as one settings file")
    r.add_argument("video")
    r.add_argument("--model", required=True)
    r.add_argument("--name", required=True)
    r.add_argument("--out", default=os.environ.get("V2L_OUT", "~/Downloads/v2l_out"),
                   help="results land in OUT/NAME/ (default: $V2L_OUT or ~/Downloads/v2l_out)")
    r.add_argument("--no-install", action="store_true", help="do not copy the motion into the local model folder")
    r.add_argument("--json", action="store_true",
                   help="print one JSON result object (local paths) on stdout; other lines go to stderr")
    r.add_argument("--progress", choices=("text", "json"), help="passed to the server; events stream on stderr")
    p = sub.add_parser("probe", help="probe a local model on the server; other options are passed to `v2l probe`")
    p.add_argument("--model", required=True)
    mp = sub.add_parser("mapping", help="show (on the server, with --model) | inputs | validate FILE [--model DIR]; "
                                        "options as for `v2l mapping`")
    mp.add_argument("action", choices=("show", "inputs", "validate"))
    sub.add_parser("bridge", help="show | check --func EXPR | validate FILE, run locally (options as for "
                                  "`v2l bridge`)", add_help=False)
    as_json = "--json" in argv

    def fail(kind: str, message: str, code: int) -> int:
        if as_json:
            print(json.dumps({"ok": False, "error": {"type": kind, "message": message}}, ensure_ascii=False), flush=True)
        else:
            print(f"v2l-remote: {message}", file=sys.stderr)
        return code

    try:
        a, rest = ap.parse_known_args(argv)
        if a.cmd not in ("run", "probe", "mapping", "bridge") and rest:
            ap.error(f"unexpected arguments: {rest}")
        if a.cmd == "bridge":
            return cli.main(["bridge", *rest])  # formulas and presets are read here
        if a.cmd == "mapping" and (a.action == "inputs" or "--model" not in rest):
            return mapping(a, rest)  # local: no compute host needed
        if a.cmd == "version":
            return cli.main(["version", *(["--json"] if a.json else [])])
        if not HOST:
            ap.error("set V2L_HOST to the compute server (user@host); see README")
        if a.cmd == "check":
            return check(a)
        if a.cmd == "setup":
            setup()
            return 0
        if a.cmd == "test":
            push_code()
            return subprocess.run([*SSH, HOST, f"cd {shlex.quote(RDIR)} && .venv/bin/python -m pytest -q"]).returncode
        if a.cmd == "regress":
            if not os.environ.get("V2L_REGRESS_MODEL"):
                ap.error("set V2L_REGRESS_MODEL to the Live2D model folder used for regression runs")
            reg_model = pathlib.Path(os.environ["V2L_REGRESS_MODEL"]).expanduser()
            push_code()
            ssh(f"mkdir -p {shlex.quote(RDIR)}/models")
            rsync(f"{reg_model}/", f"{HOST}:{RDIR}/models/{reg_model.name}/", "--delete", "--exclude=v2l_*.motion3.json")
            rsync(f"{ROOT}/regression/videos/", f"{HOST}:{RDIR}/regression/videos/")
            code = subprocess.run([*SSH, HOST, f"cd {shlex.quote(RDIR)} && .venv/bin/python regression/run.py "
                                   f"--model models/{shlex.quote(reg_model.name)}"]).returncode
            local = ROOT / "regression" / "results"
            local.mkdir(parents=True, exist_ok=True)
            rsync(f"{HOST}:{RDIR}/regression/results/", f"{local}/")
            print(json.dumps(json.loads((local / "summary.json").read_text()), indent=1) if (local / "summary.json").exists() else "")
            return code
        if a.cmd == "probe":
            return probe(a, rest)
        if a.cmd == "mapping":
            return mapping(a, rest)
        return run(a, rest)
    except UsageError as e:
        if not as_json:
            e.parser.print_usage(sys.stderr)
        return fail("usage", f"{e.parser.prog}: {e}", 2)
    except InputError as e:
        return fail("input", str(e), 2)
    except Cancelled as e:
        return fail("cancelled", str(e), 130)
    except RemoteError as e:
        return fail("remote", str(e), 1)
    except Unreachable as e:
        return fail("remote_unreachable", str(e), 4)
    except subprocess.CalledProcessError as e:
        if e.returncode == 255:  # ssh (also under rsync) could not connect or authenticate
            return fail("remote_unreachable", f"cannot reach {HOST} over ssh", 4)
        return fail("remote", f"{' '.join(map(str, e.cmd[:3]))}... failed ({e.returncode})", e.returncode or 1)
    except ConfigError as e:
        return fail("config", str(e), 2)
    except MappingError as e:
        return fail("invalid", str(e), 2)
    except (FileNotFoundError, NotADirectoryError) as e:
        return fail("input", str(e), 2)
    except KeyboardInterrupt:
        return fail("interrupted", "interrupted", 130)


if __name__ == "__main__":
    sys.exit(main())
