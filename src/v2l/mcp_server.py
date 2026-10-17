"""MCP server: lets AI agents turn videos into Live2D motions with v2l (stdio transport).

  v2l mcp [--read-only] [--toolsets inspect,generate,install] [--allow-dir DIR]...
  bin/v2l-mcp [same flags]      from a checkout; needs only python3 and `mcp` (or uv)

Two modes with the same tools. Local: jobs run `python -m v2l.cli run` on this machine.
Remote: when V2L_HOST is set (and V2L_DIR), jobs run `python -m v2l.remote run` (v2l-remote),
which uploads the inputs to the compute server and brings the results back here; this side then
needs only the standard library and `mcp`. The host comes from the environment only, never from
a tool argument. Each job is a CLI subprocess with `--json --progress json`, so the server stays
light and a job can be cancelled.

Long jobs follow the bounded-wait pattern: v2l_generate starts (or resumes) a job and waits at
most wait_seconds (<= 45, below the 60 s request timeout of common clients) while sending progress
notifications, then returns either the result or a job handle for v2l_job_status. A retry with
the same request (same video and model content, settings, name and output folder) resumes the
existing job instead of starting a second run. Job records persist in ~/.cache/v2l/jobs/.

Everything except build_server() and main() is standard library, so the job layer can be tested
without the MCP SDK.
"""

import argparse
import asyncio
import collections
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from typing import Any

from . import config as C

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_WAIT_S = 45.0
DEFAULT_WAIT_S = 40.0
SRC = pathlib.Path(__file__).resolve().parents[1]  # makes `python -m v2l...` work from a checkout
TOOLSETS = ("inspect", "generate", "install")
DEFAULT_TOOLSETS = ("inspect", "generate")
FINAL = ("succeeded", "failed", "cancelled")
# Share of the whole job by stage, for one monotonic progress number (percent).
STAGE_SPAN = {"upload": (0, 4), "probe": (4, 7), "tracking": (7, 60), "expressions": (60, 72), "solving": (72, 78),
              "writing": (78, 80), "preview": (80, 96), "download": (96, 99)}


class ToolInputError(ValueError):
    """A tool argument the agent must fix."""


def default_out_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("V2L_OUT", "~/Downloads/v2l_out")).expanduser().resolve()


def default_store() -> pathlib.Path:
    return pathlib.Path.home() / ".cache" / "v2l" / "jobs"


def env_mode() -> str:
    return "remote" if os.environ.get("V2L_HOST") else "local"


def cli_prefix(mode: str) -> list[str]:
    return [sys.executable, "-m", "v2l.remote" if mode == "remote" else "v2l.cli"]


def _digest_file(path: pathlib.Path, h) -> None:
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)


def model_digest(model_dir: pathlib.Path) -> str:
    """Content of the model's settings, physics, VTube Studio and moc3 files (not its motions)."""
    h = hashlib.sha256()
    for p in sorted(model_dir.iterdir()):
        if p.is_file() and p.suffix in (".moc3", ".json") and not p.name.endswith(".motion3.json"):
            h.update(p.name.encode())
            _digest_file(p, h)
    return h.hexdigest()


def request_key(video: pathlib.Path, model_dir: pathlib.Path, cfg: C.Config, name: str, target: pathlib.Path,
                install: bool, mapping: dict | None = None) -> str:
    h = hashlib.sha256()
    _digest_file(video, h)
    h.update(model_digest(model_dir).encode())
    h.update(json.dumps([C.to_dict(cfg), name, str(target), install], sort_keys=True).encode())
    if mapping:
        h.update(json.dumps(mapping, sort_keys=True).encode())
    return h.hexdigest()


def mapping_arg(policy: "Policy", value: Any, bridge: str) -> dict | None:
    """A v2l.mapping override given as an object or as an absolute path, checked without the model."""
    from . import mapping

    if value is None:
        return None
    if isinstance(value, str):
        p = policy.path(value, "mapping")
        if not p.is_file():
            raise ToolInputError(f"mapping file not found: {p}")
        try:
            value = mapping.load_overrides(p)
        except mapping.MappingError as e:
            raise ToolInputError(str(e)) from None
    errs, _ = mapping.check(value, bridge=bridge)
    if errs:
        raise ToolInputError("invalid mapping: " + "; ".join(errs))
    return value


def in_model_folder(path: pathlib.Path) -> bool:
    """Whether path lies in a Live2D model folder (a *.model3.json up to three levels above it)."""
    for d in list(path.parents)[:3]:
        if d.is_dir() and any(d.glob("*.model3.json")):
            return True
    return False


def motion_edit(policy: "Policy", motion: Any, operations: Any, out: Any, overwrite: bool = False,
                ranges: dict | None = None) -> dict:
    """Applies operations to a motion and writes the result (see v2l_motion_edit)."""
    from . import motionedit as ME

    src = policy.path(motion, "motion", (default_out_dir(),))
    if not src.is_file():
        raise ToolInputError(f"motion not found: {src}")
    dst = policy.path(out, "out", (default_out_dir(),))
    roots = (*policy.allow_dirs, default_out_dir())
    if not any(dst == d or dst.is_relative_to(d) for d in roots):
        raise ToolInputError(f"out {dst} must lie in the output folder ({default_out_dir()})"
                             + (f" or an allowed folder ({', '.join(map(str, policy.allow_dirs))})"
                                if policy.allow_dirs else ""))
    if not dst.name.endswith(".motion3.json"):
        raise ToolInputError("out must be a *.motion3.json path")
    if in_model_folder(dst) and not policy.enabled("install"):
        raise ToolInputError("writing into a model folder needs the install toolset (only the user can enable "
                             "it); write under the output folder and install with v2l_install_motion")
    if dst.exists() and not overwrite:
        raise ToolInputError(f"{dst} already exists; pass overwrite=true to replace it")
    if ranges is not None:
        ranges = {k: ({"min": r[0], "max": r[1]} if isinstance(r, (list, tuple)) else r) for k, r in ranges.items()}
    try:
        edit, reports = ME.apply(ME.load(str(src)), operations, ranges)
        ME.save(edit, dst, overwrite=True)
    except ME.EditError as e:
        raise ToolInputError(str(e)) from None
    except (ME.MotionError, ValueError) as e:
        raise ToolInputError(f"invalid motion: {e}") from None
    res = {"out": str(dst), "applied": len(reports), "counts": ME.counts(edit)}
    refits = {k: v for r in reports if r for k, v in r.items()}
    if refits:
        res["curves"] = refits
    return res


@dataclasses.dataclass
class Policy:
    """What the server allows: toolsets and, with allow_dirs, the folders paths must lie in."""
    toolsets: tuple[str, ...] = DEFAULT_TOOLSETS
    read_only: bool = False
    allow_dirs: tuple[pathlib.Path, ...] = ()

    def enabled(self, toolset: str) -> bool:
        if self.read_only and toolset in ("generate", "install"):
            return False
        return toolset in self.toolsets

    def path(self, value: Any, what: str, extra_allowed: tuple[pathlib.Path, ...] = ()) -> pathlib.Path:
        if not isinstance(value, str) or not value.strip():
            raise ToolInputError(f"{what} must be a non-empty path")
        p = pathlib.Path(value).expanduser()
        if not p.is_absolute():
            raise ToolInputError(f"{what} must be an absolute path (or start with ~): {value!r}")
        p = p.resolve()
        allowed = (*self.allow_dirs, *extra_allowed)
        if self.allow_dirs and not any(p == d or p.is_relative_to(d) for d in allowed):
            raise ToolInputError(f"{what} {p} is outside the folders this server may use "
                                 f"({', '.join(map(str, allowed))}); only the user can change --allow-dir")
        return p

    def model_dir(self, value: Any) -> pathlib.Path:
        d = self.path(value, "model")
        if not d.is_dir():
            raise ToolInputError(f"model is not a folder: {d}")
        found = [p.name for p in d.glob("*.model3.json") if not p.name.endswith("items_pinned_to_model.json")]
        if len(found) != 1:
            raise ToolInputError(f"model must be a folder with exactly one *.model3.json, found {found}")
        return d


def validate(settings: Any, preset: str | None = None) -> tuple[C.Config | None, list[str]]:
    """The effective configuration (defaults < preset < settings), or every problem found."""
    try:
        base = C.build(preset=preset) if preset else C.Config()
    except C.ConfigError as e:
        return None, [str(e)]
    if settings is None:
        return base, []
    if not isinstance(settings, dict):
        return None, ['settings must be a JSON object such as {"intensity": {"smile": 1.5}}']
    errors = []
    for sid, values in settings.items():
        try:  # the section itself (or the version)
            C.merge(base, {sid: {} if isinstance(values, dict) else values})
        except C.ConfigError as e:
            errors.append(str(e))
            continue
        if sid in ("version", "meta"):
            continue
        for name, v in values.items():
            try:
                C.merge(base, {sid: {name: v}})
            except C.ConfigError as e:
                errors.append(str(e))
    if errors:
        return None, errors
    try:
        return C.merge(base, settings), []
    except C.ConfigError as e:
        return None, [str(e)]


# ------------------------------------------------------------------------------------------ jobs

@dataclasses.dataclass
class Job:
    id: str
    key: str
    name: str
    video: str
    model_dir: str
    out_dir: str                      # the job's own folder: <output folder>/<name>
    install: bool
    mode: str
    argv: list[str] = dataclasses.field(default_factory=list)
    created: float = dataclasses.field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    status: str = "queued"            # queued | running | succeeded | failed | cancelled
    stage: str | None = None
    progress: dict | None = None
    log: collections.deque = dataclasses.field(default_factory=lambda: collections.deque(maxlen=30))
    result: dict | None = None
    error: dict | None = None
    cancel_requested: bool = False
    workdir: str | None = None
    proc: Any = None
    task: Any = None
    changed: Any = None
    saved_at: float = 0.0

    RECORD = ("id", "key", "name", "video", "model_dir", "out_dir", "install", "mode", "created", "started",
              "finished", "status", "stage", "progress", "result", "error")

    @property
    def done(self) -> bool:
        return self.status in FINAL

    def percent(self) -> float:
        if self.status == "succeeded":
            return 100.0
        if self.stage not in STAGE_SPAN:
            return 0.0
        lo, hi = STAGE_SPAN[self.stage]
        frac = self.progress["done"] / max(self.progress["total"], 1) if self.progress else 0.0
        return round(lo + (hi - lo) * min(frac, 1.0), 1)

    def where(self) -> str:
        w = self.stage or self.status
        return w + (f" {self.progress['done']}/{self.progress['total']}" if self.progress else "")

    def message(self) -> str:
        if self.status == "succeeded":
            return f"Done. The motion and report are in {self.out_dir}."
        if self.status == "failed":
            return f"Failed: {(self.error or {}).get('message', 'see log_tail')}"
        if self.status == "cancelled":
            return "Cancelled."
        return (f"Still {self.status} ({self.where()}, {self.percent():.0f}%). Call v2l_job_status(job=\"{self.id}\") to "
                "keep waiting; do not start a new run.")

    def view(self) -> dict:
        end = self.finished or time.time()
        return {"job": self.id, "name": self.name, "status": self.status, "stage": self.stage,
                "progress": self.progress, "percent": self.percent(),
                "elapsed_s": round(end - self.started, 1) if self.started else 0.0,
                "out_dir": self.out_dir, "message": self.message(), "log_tail": list(self.log),
                "result": self.result, "error": self.error}

    def record(self) -> dict:
        return {**{k: getattr(self, k) for k in self.RECORD}, "log": list(self.log)}


class Jobs:
    """Runs motion jobs as CLI subprocesses, one at a time (later ones wait as "queued")."""

    def __init__(self, cli: list[str] | None = None, mode: str | None = None, store: pathlib.Path | None = None,
                 max_parallel: int = 1, kill_grace_s: float = 10.0):
        self.mode = mode or env_mode()
        self.cli = cli or cli_prefix(self.mode)
        self.store = store or default_store()
        self.kill_grace_s = kill_grace_s
        self.max_parallel = max_parallel
        self._slots: asyncio.Semaphore | None = None
        self.jobs: dict[str, Job] = {}
        self._load()

    # ---- persistence
    def _load(self) -> None:
        if not self.store.is_dir():
            return
        files = sorted(self.store.glob("*.json"), key=lambda p: p.stat().st_mtime)[-200:]
        for f in files:
            try:
                rec = json.loads(f.read_text())
                job = Job(**{k: rec[k] for k in Job.RECORD})
            except (OSError, ValueError, KeyError, TypeError):
                continue
            job.log.extend(rec.get("log", []))
            if not job.done:  # its server went away
                job.status = "failed"
                job.error = {"type": "interrupted", "message": "the MCP server stopped while this job ran"}
                job.finished = job.finished or time.time()
                self._save(job, force=True)
            self.jobs[job.id] = job

    def _save(self, job: Job, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - job.saved_at < 1.0:
            return
        job.saved_at = now
        self.store.mkdir(parents=True, exist_ok=True)
        tmp = self.store / f".{job.id}.part"
        tmp.write_text(json.dumps(job.record(), ensure_ascii=False))
        tmp.replace(self.store / f"{job.id}.json")

    def _touch(self, job: Job, force: bool = False) -> None:
        if job.changed is not None:
            job.changed.set()
        job.changed = asyncio.Event()
        self._save(job, force)

    # ---- lookup
    def env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def get(self, job_id: Any) -> Job:
        if not isinstance(job_id, str) or job_id not in self.jobs:
            raise ToolInputError(f"unknown job {job_id!r}; v2l_jobs lists the known jobs")
        return self.jobs[job_id]

    def by_key(self, key: str) -> Job | None:
        for job in sorted(self.jobs.values(), key=lambda j: -j.created):
            if job.key == key and job.status in ("queued", "running", "succeeded"):
                if job.status == "succeeded" and not pathlib.Path(job.result["files"]["motion"]).exists():
                    continue  # the files were deleted: run again
                return job
        return None

    # ---- lifecycle
    def submit(self, key: str, video: pathlib.Path, model: pathlib.Path, name: str, cfg: C.Config,
               base: pathlib.Path, install: bool, mapping: dict | None = None) -> Job:
        """Starts a job (the caller has validated the arguments and checked by_key)."""
        target = base / name
        if target.exists():
            raise ToolInputError(f"{target} already exists; choose another name (results are never overwritten)")
        if any(j.out_dir == str(target) and not j.done for j in self.jobs.values()):
            raise ToolInputError(f"another job is already writing to {target}")
        if install and (model / f"{name}.motion3.json").exists():
            raise ToolInputError(f"{model / (name + '.motion3.json')} already exists; choose another name")
        job_id = secrets.token_hex(4)
        workdir = tempfile.mkdtemp(prefix=f"v2l-mcp-{job_id}-")
        cfg_file = pathlib.Path(workdir) / "settings.json"
        cfg_file.write_text(json.dumps(C.to_dict(cfg), indent=1))
        # v2l-remote puts results in --out/NAME; the local CLI writes straight into --out.
        out_arg = base if self.mode == "remote" else target
        argv = [*self.cli, "run", str(video), "--model", str(model), "--name", name, "--out", str(out_arg),
                "--config", str(cfg_file), "--json", "--progress", "json"]
        if not install:
            argv.append("--no-install")
        if mapping:
            map_file = pathlib.Path(workdir) / "mapping.json"
            map_file.write_text(json.dumps(mapping, indent=1))
            argv += ["--mapping", str(map_file)]
        job = Job(id=job_id, key=key, name=name, video=str(video), model_dir=str(model), out_dir=str(target),
                  install=install, mode=self.mode, argv=argv, workdir=workdir)
        self.jobs[job_id] = job
        self._touch(job, force=True)
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        return job

    async def _run(self, job: Job) -> None:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.max_parallel)
        try:
            async with self._slots:
                if job.cancel_requested:
                    return
                job.status, job.started = "running", time.time()
                self._touch(job, force=True)
                job.proc = await asyncio.create_subprocess_exec(
                    *job.argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env(),
                    stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
                out, _ = await asyncio.gather(job.proc.stdout.read(), self._events(job))
                code = await job.proc.wait()
                self._finish(job, code, out.decode("utf-8", "replace"))
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - the job reports it instead of the server crashing
            job.status, job.error = "failed", {"type": "runtime", "message": f"{type(e).__name__}: {e}"}
        finally:
            if job.cancel_requested:
                job.status = "cancelled"
                job.error = {"type": "cancelled", "message": "cancelled by request"}
            if job.status != "succeeded":
                shutil.rmtree(job.out_dir, ignore_errors=True)  # it did not exist before this job
            job.finished = time.time()
            if job.workdir:
                shutil.rmtree(job.workdir, ignore_errors=True)
            self._touch(job, force=True)

    async def _events(self, job: Job) -> None:
        async for raw in job.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                e = None
            force = False
            if not isinstance(e, dict) or "event" not in e:
                job.log.append(line[:500])
            elif e["event"] == "stage":
                job.stage, job.progress, force = e.get("stage"), None, True
            elif e["event"] == "progress":
                job.stage = e.get("stage", job.stage)
                job.progress = {"done": e.get("done"), "total": e.get("total")}
            elif e["event"] == "log":
                job.log.append(str(e.get("message", ""))[:500])
            self._touch(job, force)

    def _finish(self, job: Job, code: int, stdout: str) -> None:
        res = None
        for line in reversed(stdout.strip().splitlines()):
            try:
                res = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if job.cancel_requested:
            return
        if code == 0 and isinstance(res, dict) and res.get("ok"):
            job.status, job.result = "succeeded", res
            job.stage, job.progress = None, None
        else:
            job.status = "failed"
            job.error = res["error"] if isinstance(res, dict) and isinstance(res.get("error"), dict) else {
                "type": "runtime", "message": f"exit code {code}; see log_tail"}

    async def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.done:
            return job
        job.cancel_requested = True
        if job.proc is None or job.proc.returncode is not None:
            if job.task:
                job.task.cancel()
                try:
                    await job.task
                except asyncio.CancelledError:
                    pass
            return job
        self._signal(job, signal.SIGTERM)  # v2l-remote also stops the server-side process on SIGTERM
        try:
            await asyncio.wait_for(asyncio.shield(job.task), self.kill_grace_s)
        except asyncio.TimeoutError:
            self._signal(job, signal.SIGKILL)
            await job.task
        return job

    @staticmethod
    def _signal(job: Job, sig: int) -> None:
        try:
            os.killpg(job.proc.pid, sig)
        except ProcessLookupError:
            pass

    async def wait(self, job: Job, wait_s: float, report=None, heartbeat_s: float = 5.0) -> Job:
        """Waits until the job is final or wait_s passes; report(job) on every change and at least
        every heartbeat_s (progress notifications keep the client's request alive)."""
        deadline = time.monotonic() + max(0.0, min(float(wait_s), MAX_WAIT_S))
        while not job.done:
            if report:
                await report(job)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if job.changed is None:
                job.changed = asyncio.Event()
            try:
                await asyncio.wait_for(job.changed.wait(), min(remaining, heartbeat_s))
            except asyncio.TimeoutError:
                pass
        if report:
            await report(job)
        return job

    def stop_all(self) -> None:
        """SIGTERM to every running job (synchronous, for server exit)."""
        for job in self.jobs.values():
            if job.proc is not None and job.proc.returncode is None:
                self._signal(job, signal.SIGTERM)


# ------------------------------------------------------------------------------- one-shot calls

async def run_cli(argv: list[str], env: dict, timeout_s: float = 900.0) -> dict:
    """Runs a short CLI command with --json and returns its result object."""
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                stdin=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        raise ToolInputError(f"timed out after {timeout_s:.0f} s") from None
    lines = out.decode("utf-8", "replace").strip().splitlines()
    try:
        res = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        raise ToolInputError(f"no result (exit {proc.returncode}): {err.decode('utf-8', 'replace')[-800:]}") from None
    if not res.get("ok"):
        err = res.get("error", {})
        raise ToolInputError(f"{err.get('type', 'error')}: {err.get('message')}")
    return res


def motions_in(model_dir: pathlib.Path) -> dict:
    motions = []
    for p in sorted(model_dir.rglob("*.motion3.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8-sig")).get("Meta", {})
        except (OSError, ValueError):
            meta = {}
        motions.append({"file": str(p), "name": p.name.removesuffix(".motion3.json"),
                        "duration_s": meta.get("Duration"), "loop": meta.get("Loop"),
                        "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime))})
    return {"model": str(model_dir), "motions": motions}


# ---------------------------------------------------------------------------------- MCP binding

INSTRUCTIONS = """V2L turns a recorded video of one person into a Live2D Cubism motion (.motion3.json) for a
Live2D model folder, plus a QA report and an optional side-by-side preview MP4.
Flow: v2l_params (settings schema and presets) -> optionally v2l_validate_settings -> v2l_generate, which
waits up to ~40 s; while the result says status "queued"/"running", call v2l_job_status(job) (never start a
second run for the same request). Jobs take roughly 0.5-2x the clip length on a GPU server and much longer on
a laptop CPU; trim with settings {"input": {"start": s, "end": s}} while experimenting. Paths are on the machine
running this server and must be absolute (or start with ~). Each job writes a new folder <output>/<name>/;
existing results are never overwritten.
Adjusting results: v2l_mapping_get shows which tracked input drives which model parameter; pass changes as
v2l_generate(mapping=...) (a new name; cached tracking is reused). v2l_motion_read / v2l_motion_edit edit the
curves of a finished motion (keys, interpolation, Bezier handles, refit, trim, ...)."""

JOB_SHAPE = """Returns {"job", "name", "status": queued | running | succeeded | failed | cancelled, "stage": upload |
probe | tracking | expressions | solving | writing | preview | download | null, "progress": {"done", "total"}
(frames of the current stage) or null, "percent": 0-100 estimate, "elapsed_s", "out_dir", "message" (what to
do next), "log_tail", "result", "error"}. On success result is {"ok": true, "name", "out_dir", "files":
{"motion", "report", "preview" (null when not rendered)}, "installed": path or null, "summary": {"face_rate",
"curves", "points", "blinks", "warnings", ...}}; files.report holds per-parameter detail and the effective
settings. On failure error is {"type": usage | config | input | invalid (bad mapping) | unusable_input (no face
tracked) | runtime | remote | remote_unreachable | interrupted | cancelled, "message"}."""

MOTION_EDIT_SHAPE = """The editable motion is {"format": "v2l.motion-edit", "version": 1, "meta": {"duration",
"fps", "loop", "fade_in", "fade_out", "restricted"}, "curves": [{"target", "id", "keys": [[t, v], ...], "segments":
[{"type": "linear" | "stepped" | "inverse_stepped"} | {"type": "bezier", "c1": [t, v], "c2": [t, v]}]}],
"passthrough": {...}}; segments[i] joins keys[i] and keys[i+1]. With meta.restricted (always true for v2l output)
Bezier control times sit at 1/3 and 2/3 of the segment and only the control values matter."""


def build_server(jobs: Jobs | None = None, policy: Policy | None = None):
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    jobs = jobs or Jobs()
    policy = policy or Policy()
    server = MCPServer("v2l", instructions=INSTRUCTIONS)
    ro = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    def tool(toolset: str, annotations: ToolAnnotations, description: str | None = None):
        def register(fn):
            if policy.enabled(toolset):
                server.tool(annotations=annotations, description=description)(fn)
            return fn
        return register

    def fail(e: Exception):
        raise ToolError(str(e)) from None

    async def reporter(ctx: Context):
        last: list[Any] = [None, 0.0]

        async def report(job: Job) -> None:
            key = (job.status, job.stage, job.percent())
            now = time.monotonic()
            if key != last[0] or now - last[1] >= 5.0:
                last[0], last[1] = key, now
                await ctx.report_progress(job.percent(), 100.0, job.status if job.done else job.where())
        return report

    @tool("inspect", ro)
    async def v2l_params(section: str | None = None) -> dict[str, Any]:
        """The v2l settings schema, the defaults and the presets. Returns {"schema": {"version", "sections":
        [{"id", "label", "help", "fields": [{"key": "section.name", "name", "default", "kind", "label", "help",
        "min", "max", "step", "unit", "choices", "nullable", "advanced", "affects"}]}]}, "defaults": {section:
        {name: value}}, "presets": [{"name", "title", "description", "builtin", "settings"}]}. section (e.g.
        "intensity") limits schema and defaults to one section. kind: boolean | integer | number | string | enum
        (one of choices) | range ([low, high], low <= high) | pair ([x, y]) | window ([start, end] seconds).
        affects: "tracking" (changing it re-tracks the video), "solve" (fast: cached tracking is reused) or
        "output" (preview only). Settings are passed as a partial object {section: {name: value}}; omitted keys
        keep their defaults. Common knobs: style.name ("cute" adds smile/blush appeal, "faithful" adds
        nothing), intensity.* (0-3x per movement group), input.start/end (trim, seconds), output.loop (seamless
        idle loop), filtering.cutoff_scale (smoothing), calibration.neutral_window (rest face)."""
        sch = C.schema()
        defaults = C.to_dict(C.Config())
        if section is not None:
            if section not in C.SECTIONS:
                fail(ToolInputError(f"unknown section {section!r}; one of {', '.join(C.SECTIONS)}"))
            sch = {**sch, "sections": [s for s in sch["sections"] if s["id"] == section]}
            defaults = {"version": defaults["version"], section: defaults[section]}
        return {"schema": sch, "defaults": defaults,
                "presets": [{k: p.get(k) for k in ("name", "title", "description", "builtin", "settings", "error")}
                            for p in C.presets()]}

    @tool("inspect", ro)
    async def v2l_validate_settings(settings: dict[str, Any] | None = None, preset: str | None = None) -> dict[str, Any]:
        """Dry run of the settings only: applies preset (a name from v2l_params) and then the partial settings
        object {section: {name: value}} to the defaults, without touching any file. Returns {"ok": true,
        "settings": <complete effective settings>} or {"ok": false, "errors": ["intensity.smile=9 is outside
        0.0..3.0", ...]} listing every problem (unknown preset, section or key, wrong type, out of range, bad
        window)."""
        cfg, errors = validate(settings, preset)
        return {"ok": True, "settings": C.to_dict(cfg)} if cfg else {"ok": False, "errors": errors}

    @tool("inspect", ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
    async def v2l_probe_model(model: str, details: bool = False) -> dict[str, Any]:
        """Loads a Live2D model folder (absolute path, exactly one *.model3.json) and reports what v2l will
        drive: {"model": name, "parameters": count, "physics_outputs": count, "vtube_mapping": true when the
        model's *.vtube.json mapping is used, "framing": {...}} plus, with details, "params": {parameter id:
        measured screen effect}. The first call per model renders it headlessly to measure parameter
        directions (up to a few minutes; cached afterwards). In remote mode the folder is uploaded to the
        compute server first."""
        try:
            d = policy.model_dir(model)
            return await run_cli([*jobs.cli, "probe", "--model", str(d), "--json", *(["--details"] if details else [])],
                                 jobs.env())
        except ToolInputError as e:
            fail(e)

    @tool("inspect", ro)
    async def v2l_list_motions(model: str) -> dict[str, Any]:
        """Motions (*.motion3.json) inside a Live2D model folder (absolute path), including ones installed by v2l:
        {"model", "motions": [{"file", "name", "duration_s", "loop", "modified"}]}."""
        try:
            return motions_in(policy.model_dir(model))
        except ToolInputError as e:
            fail(e)

    @tool("inspect", ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True))
    async def v2l_mapping_get(model: str, mapping: dict[str, Any] | str | None = None) -> dict[str, Any]:
        """The effective mapping of a Live2D model in its three layers, and the inputs a mapping may use.
        model: absolute path of the model folder. mapping: optional v2l.mapping file contents or the absolute
        path of such a file: {"format": "v2l.mapping", "version": 1, "input_curves"?: {tracking input name:
        {"points": [[x, y], ...] (x and y 0..1 over the input's nominal range, x from 0 to 1 strictly
        increasing), "smooth": true (monotone cubic) | false (linear)}}, "bridge"?: [{"name", "func"?, "min"?,
        "max"?, "defaultValue"?, "delayBuffer"?: {"refParam", "smoothing", "delayCount", "inMin", "inMax",
        "outMin", "outMax"} | null, "modifiers"?: {"delayMs"?, "smoothing"? (0..1), "steps"?: [{"trigger",
        "target", "holdMs"?, "threshold"?}]} | null, "enabled"?}] (same name as a preset output patches it,
        a new name adds one, enabled false drops it), "entries"?: [{"output": parameter id, "input"?, "in"?: [lo,
        hi], "out"?: [lo, hi], "clamp_in"?, "clamp_out"?, "invert"?, "curve"?: {"points", "smooth"} | null
        (normalized input -> output), "cutoff"?: Hz | null (low-pass override), "breathing"?, "enabled"?}]};
        entries patch the base entry with the same output (omitted fields inherited), add one for an unmapped
        parameter (needs input), or remove one (enabled false). Returns {"model", "model_dir", "base": "vtube" |
        "fallback", "bridge": preset name, "input_curves", "bridge_params": [{"name", "func", "min", "max",
        "defaultValue", "delayBuffer", "modifiers", "source": preset | override | added | disabled,
        "overridden", "inputs", "unknown"}], "entries": [{"output", "input", "in", "out" (before invert),
        "clamp_in", "clamp_out", "invert", "curve", "cutoff", "breathing", "enabled", "source": vtube | fallback
        | override | added, "applied_out" (range actually used), "overridden", "corrections": [body | direction
        | side], "filter_group", "default_cutoff"}], "notes" (probe corrections), "warnings", "parameters":
        [{"id", "name", "min", "max", "default", "mapped", "physics"}], "inputs": [{"name", "kind": bridge |
        tracking, "range", "default", "meaning", "shadowed"?}]}. To change a mapping, pass only the fields you
        want to change (v2l_generate mapping). Check a formula first with v2l_bridge_check. The first call per
        model renders it to measure parameter directions (cached afterwards)."""
        from . import inputs

        try:
            d = policy.model_dir(model)
            data = mapping_arg(policy, mapping, "default")
            argv = [*jobs.cli, "mapping", "show", "--model", str(d), "--json"]
            with tempfile.TemporaryDirectory(prefix="v2l-mcp-map-") as tmp:
                if data is not None:
                    f = pathlib.Path(tmp) / "mapping.json"
                    f.write_text(json.dumps(data))
                    argv += ["--mapping", str(f)]
                res = await run_cli(argv, jobs.env())
            res.pop("ok", None)
            res["mapping_file"] = mapping if isinstance(mapping, str) else None
            res["inputs"] = inputs.listing("default", (data or {}).get("bridge"))["inputs"]
            return res
        except ToolInputError as e:
            fail(e)

    @tool("inspect", ro)
    async def v2l_bridge_check(func: str, mapping: dict[str, Any] | str | None = None) -> dict[str, Any]:
        """Checks one bridge formula (SnenkBridge/VBridger syntax: numbers, input names, + - * / % ^,
        comparisons, && || !, if(c, a, b), min/max and math::* functions, // and /* */ comments) without running
        anything. mapping: optional v2l.mapping (object or absolute path) whose added bridge outputs count as
        known names. Returns {"valid", "inputs": [names it reads], "unknown": [names nothing provides; they read
        as 0], "functions", "tokens": [{"start", "end", "kind": number | name | function | operator | paren |
        comma}], "problem": null | {"message", "position", "end"}} with 0-based character offsets into func."""
        from . import bridge as B

        try:
            data = mapping_arg(policy, mapping, "default")
        except ToolInputError as e:
            fail(e)
        _, preset = B.read_preset("default")
        eff = B.effective(preset, (data or {}).get("bridge"))
        return B.check(func, B.known_names(p["name"] for p in eff if B._active(p)))

    @tool("inspect", ro)
    async def v2l_motion_info(motion: str) -> dict[str, Any]:
        """Summary of a *.motion3.json (absolute path): {"meta": {"duration", "fps", "loop", "fade_in",
        "fade_out", "restricted"}, "counts": {"curves", "segments", "points", "keys", "user_data"}, "curves":
        [{"target", "id", "keys", "types": {"linear", "bezier", "stepped", "inverse_stepped"}, "min", "max"}],
        "problems": [validation problems; empty when valid]}."""
        from . import motionedit as ME

        try:
            p = policy.path(motion, "motion", (default_out_dir(),))
            if not p.is_file():
                raise ToolInputError(f"motion not found: {p}")
            try:
                data = ME.read_json(str(p))
                return {**ME.info(ME.as_editable(data)), "problems": ME.validate(data)}
            except (ME.MotionError, ValueError) as e:
                raise ToolInputError(f"cannot read {p}: {e}") from None
        except ToolInputError as e:
            fail(e)

    @tool("inspect", ro, """Reads a *.motion3.json (absolute path) as an editable motion: keys, per-segment
        interpolation and Bezier handles. curves: optional list of curve ids (default: all; a motion has one curve
        per parameter). Returns {"motion": <editable motion>}. """ + MOTION_EDIT_SHAPE)
    async def v2l_motion_read(motion: str, curves: list[str] | None = None) -> dict[str, Any]:
        from . import motionedit as ME

        try:
            p = policy.path(motion, "motion", (default_out_dir(),))
            if not p.is_file():
                raise ToolInputError(f"motion not found: {p}")
            try:
                return {"motion": ME.load(str(p), curves)}
            except (ME.MotionError, ValueError) as e:
                raise ToolInputError(f"cannot read {p}: {e}") from None
        except ToolInputError as e:
            fail(e)

    @tool("generate", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                      openWorldHint=False),
          """Edits a motion: applies operations in order to the *.motion3.json at motion (absolute path) and writes
        the result to out (absolute *.motion3.json path inside the output folder or an allowed folder; inside a
        Live2D model folder only when the server enables the install toolset). An existing out is replaced only
        with overwrite=true; writing back onto the source is allowed with overwrite=true. Nothing is written
        when any operation is invalid. Operations (times in seconds; curves: list of ids or "*"; a key is
        addressed by its time within 0.0001 s): {"op": "set_interpolation", "curves", "type": linear | bezier |
        stepped | inverse_stepped, "from"?, "to"?} (segments overlapping [from, to]; bezier starts from smooth
        handles); {"op": "insert_key", "curve", "t", "value"?} (keeps the shape; value moves the new key with its
        handles); {"op": "delete_keys", "curve", "times" | "from"/"to"} (first and last key stay; naming them is
        an error); {"op": "move_key", "curve", "t", "to_t"?, "to_value"?} (to_t strictly between the
        neighbours; first/last key cannot move in time); {"op": "set_handles", "curve", "segment": start time,
        "c1v"?, "c2v"?} (bezier segments only); {"op": "auto_tangents" | "flatten_tangents", "curves", "from"?,
        "to"?}; {"op": "refit", "curves", "tolerance": fraction of the range, "interpolation": bezier |
        linear}; {"op": "scale_values", "curves", "factor", "pivot": zero | first | mean | number}; {"op":
        "offset_values", "curves", "delta"}; {"op": "clamp", "ranges": {id: [min, max]}}; {"op": "retime",
        "factor"}; {"op": "trim", "from", "to"}; {"op": "set_meta", "loop"?, "fade_in"?, "fade_out"?, "fps"?,
        "duration"?}; {"op": "delete_curve", "curve"}. ranges: optional {id: [min, max]} used as the range for
        refit tolerances (default: each curve's own value span). Returns {"out", "applied", "counts": {"curves",
        "segments", "points", "keys", "user_data"}, "curves"?: refit report {id: {"keys_before", "keys_after",
        "max_error", "tolerance"}}}.""")
    async def v2l_motion_edit(motion: str, operations: list[dict[str, Any]], out: str, overwrite: bool = False,
                              ranges: dict[str, list[float]] | None = None) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(motion_edit, policy, motion, operations, out, overwrite, ranges)
        except ToolInputError as e:
            fail(e)

    @tool("inspect", ro, "Lists the jobs this server knows (also from earlier sessions), newest first: {\"mode\": "
                         "\"local\" | \"remote\", \"jobs\": [{\"job\", \"name\", \"status\", \"stage\", \"percent\", "
                         "\"elapsed_s\", \"out_dir\"}]}.")
    async def v2l_jobs() -> dict[str, Any]:
        views = [j.view() for j in sorted(jobs.jobs.values(), key=lambda j: -j.created)][:50]
        return {"mode": jobs.mode, "jobs": [{k: v[k] for k in ("job", "name", "status", "stage", "percent",
                                                               "elapsed_s", "out_dir")} for v in views]}

    @tool("inspect", ro, "State of a job. Waits up to wait_seconds (0-45, default 40) for it to finish, sending "
                         "progress notifications meanwhile; call it again while status is queued or running. "
                         + JOB_SHAPE)
    async def v2l_job_status(job: str, wait_seconds: float = DEFAULT_WAIT_S, ctx: Context | None = None) -> dict[str, Any]:
        try:
            j = jobs.get(job)
        except ToolInputError as e:
            fail(e)
        return (await jobs.wait(j, wait_seconds, await reporter(ctx) if ctx else None)).view()

    @tool("generate", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                      openWorldHint=False),
          """Converts a video of one person into a Live2D motion for a model. Starts a job (or resumes the
        identical earlier request: same video and model content, settings, name and output folder), waits up to
        wait_seconds (0-45, default 40) while sending progress notifications, then returns the job state. If
        status is still "queued"/"running", call v2l_job_status(job) to keep waiting; never start a new run for
        the same request. Arguments: video: absolute path of the clip (mp4/mov/...; one face, roughly
        frontal). model: absolute path of the Live2D model folder (exactly one *.model3.json). name: 1-80 of
        A-Z a-z 0-9 _ -; files are <name>.motion3.json, <name>.report.json, <name>.preview.mp4. preset: a
        preset name from v2l_params, applied before settings. settings: partial settings {section: {name:
        value}} (invalid ones are rejected with every error listed). preview: render the preview MP4
        (overrides settings.preview.enabled; default true; takes about as long as tracking). out_dir: absolute
        output folder (default: the configured output folder, $V2L_OUT or ~/Downloads/v2l_out); results go to
        <out_dir>/<name>/, which must not exist yet. install: also copy the motion into the model folder (only
        when the server enables the install toolset; refused if the model has <name>.motion3.json). mapping:
        optional v2l.mapping overrides (object or absolute path; see v2l_mapping_get), applied after tracking.
        job: an existing job id to resume instead (other arguments are then ignored). """ + JOB_SHAPE)
    async def v2l_generate(video: str | None = None, model: str | None = None, name: str | None = None,
                           preset: str | None = None, settings: dict[str, Any] | None = None,
                           preview: bool | None = None, out_dir: str | None = None, install: bool = False,
                           job: str | None = None, wait_seconds: float = DEFAULT_WAIT_S,
                           mapping: dict[str, Any] | str | None = None,
                           ctx: Context | None = None) -> dict[str, Any]:
        try:
            if job is not None:
                j = jobs.get(job)
            else:
                if video is None or model is None or name is None:
                    raise ToolInputError("video, model and name are required (or pass job to resume a job)")
                if install and not policy.enabled("install"):
                    raise ToolInputError("installing into the model folder is disabled on this server; leave "
                                         "install false (only the user can enable the install toolset)")
                v = policy.path(video, "video")
                if not v.is_file():
                    raise ToolInputError(f"video not found: {v}")
                m = policy.model_dir(model)
                if not isinstance(name, str) or not NAME_RE.match(name):
                    raise ToolInputError("name must be 1-80 characters of A-Z, a-z, 0-9, '_' or '-'")
                base = policy.path(out_dir, "out_dir", (default_out_dir(),)) if out_dir else default_out_dir()
                cfg, errors = validate(settings, preset)
                if errors:
                    raise ToolInputError("invalid settings: " + "; ".join(errors))
                if preview is not None:
                    cfg = dataclasses.replace(cfg, preview=dataclasses.replace(cfg.preview, enabled=bool(preview)))
                overrides = mapping_arg(policy, mapping, cfg.bridge.preset)
                key = await asyncio.to_thread(request_key, v, m, cfg, name, base / name, bool(install), overrides)
                j = jobs.by_key(key) or jobs.submit(key, v, m, name, cfg, base, bool(install), overrides)
        except ToolInputError as e:
            fail(e)
        return (await jobs.wait(j, wait_seconds, await reporter(ctx) if ctx else None)).view()

    @tool("generate", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                      openWorldHint=False))
    async def v2l_job_cancel(job: str) -> dict[str, Any]:
        """Stops a queued or running job (in remote mode the process on the compute server too) and returns
        its final state (status "cancelled"); its partial output folder is removed. Finished jobs are returned
        unchanged."""
        try:
            return (await jobs.cancel(job)).view()
        except ToolInputError as e:
            fail(e)

    @tool("install", ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True,
                                     openWorldHint=False))
    async def v2l_install_motion(motion: str, model: str, overwrite: bool = False) -> dict[str, Any]:
        """Copies a *.motion3.json (absolute path, e.g. result.files.motion of a job) into a Live2D model folder
        so VTube Studio and the model list it. Refuses to replace an existing file of the same name unless
        overwrite is true. Returns {"installed": path, "replaced": bool}."""
        try:
            src = policy.path(motion, "motion", (default_out_dir(),))
            if not src.is_file() or not src.name.endswith(".motion3.json"):
                raise ToolInputError(f"motion must be an existing *.motion3.json file: {src}")
            d = policy.model_dir(model)
            dst = d / src.name
            replaced = dst.exists()
            if replaced and not overwrite:
                raise ToolInputError(f"{dst} already exists; pass overwrite=true to replace it")
            tmp = dst.with_name(dst.name + ".part")
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
            return {"installed": str(dst), "replaced": replaced}
        except ToolInputError as e:
            fail(e)

    server.v2l_jobs = jobs
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="v2l mcp", description="V2L MCP server on stdio. Local mode, or remote through v2l-remote when "
        "V2L_HOST is set; results go to V2L_OUT (default ~/Downloads/v2l_out).")
    ap.add_argument("--read-only", action="store_true", help="hide the tools that generate or install motions")
    ap.add_argument("--toolsets", default=",".join(DEFAULT_TOOLSETS),
                    help=f"comma-separated subset of {','.join(TOOLSETS)} (default: {','.join(DEFAULT_TOOLSETS)})")
    ap.add_argument("--allow-dir", action="append", default=[], metavar="DIR",
                    help="every path argument must lie inside one of these folders (repeatable; the output "
                         "folder is always allowed for outputs)")
    a = ap.parse_args(argv)
    toolsets = tuple(t.strip() for t in a.toolsets.split(",") if t.strip())
    unknown = set(toolsets) - set(TOOLSETS)
    if unknown:
        ap.error(f"unknown toolsets {sorted(unknown)}; choose from {', '.join(TOOLSETS)}")
    policy = Policy(toolsets=toolsets, read_only=a.read_only,
                    allow_dirs=tuple(pathlib.Path(d).expanduser().resolve() for d in a.allow_dir))
    try:
        server = build_server(policy=policy)
    except ImportError:
        print("v2l mcp needs the MCP SDK: pip install 'v2l[mcp]' (or run bin/v2l-mcp)", file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        print(f"V2L MCP server ({server.v2l_jobs.mode} mode) is waiting for an MCP client on stdin; this is not "
              "a hang. Register it in your client instead, e.g. `claude mcp add v2l -- v2l mcp`.", file=sys.stderr)
    try:
        server.run("stdio")
    finally:
        server.v2l_jobs.stop_all()  # the client went away: no orphaned jobs
    return 0


if __name__ == "__main__":
    sys.exit(main())
