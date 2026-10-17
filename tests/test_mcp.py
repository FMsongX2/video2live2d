"""The MCP job layer against a fake CLI (no tracking, no network)."""

import asyncio
import json
import pathlib
import sys

import pytest

from v2l import mcp_server as M

FAKE_CLI = '''
import json, pathlib, sys, time
a = sys.argv[1:]
if a[:2] == ["mapping", "show"]:
    m = a[a.index("--mapping") + 1] if "--mapping" in a else None
    print(json.dumps({"ok": True, "model": "m", "base": "fallback", "entries": [], "parameters": [],
                      "seen": json.loads(pathlib.Path(m).read_text()) if m else None}))
    sys.exit(0)
name, out = a[a.index("--name") + 1], pathlib.Path(a[a.index("--out") + 1])
settings = json.loads(pathlib.Path(a[a.index("--config") + 1]).read_text())
def ev(**e):
    print(json.dumps(e), file=sys.stderr, flush=True)
ev(event="stage", stage="tracking")
print("a plain library line", file=sys.stderr, flush=True)
for i in range(1, 4):
    ev(event="progress", stage="tracking", done=i, total=3)
    time.sleep(5 if name.startswith("slow") else 0.01)
out.mkdir(parents=True, exist_ok=True)
if name.startswith("fail"):
    print(json.dumps({"ok": False, "error": {"type": "unusable_input", "message": "no face found in any frame"}}))
    sys.exit(3)
mapping = json.loads(pathlib.Path(a[a.index("--mapping") + 1]).read_text()) if "--mapping" in a else None
(out / f"{name}.motion3.json").write_text(json.dumps({"smile": settings["intensity"]["smile"]}
                                                     | ({"mapping": mapping} if mapping else {})))
print(json.dumps({"ok": True, "name": name, "out_dir": str(out),
                  "files": {"motion": str(out / f"{name}.motion3.json"), "report": None, "preview": None},
                  "installed": None, "summary": {"face_rate": 1.0}}))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    fake = tmp_path / "fake_cli.py"
    fake.write_text(FAKE_CLI)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    model = tmp_path / "model"
    model.mkdir()
    (model / "m.model3.json").write_text("{}")
    monkeypatch.setenv("V2L_OUT", str(tmp_path / "out"))
    jobs = lambda: M.Jobs(cli=[sys.executable, str(fake)], mode="local", store=tmp_path / "jobs", kill_grace_s=2)
    return {"tmp": tmp_path, "video": video, "model": model, "jobs": jobs}


def start(env, jobs, name, **settings):
    cfg, errors = M.validate(settings or None)
    assert not errors
    base = M.default_out_dir()
    key = M.request_key(env["video"], env["model"], cfg, name, base / name, False)
    return jobs.by_key(key) or jobs.submit(key, env["video"], env["model"], name, cfg, base, False)


def run(coro):
    return asyncio.run(coro)


def test_job_succeeds_with_progress_and_result(env):
    async def go():
        jobs = env["jobs"]()
        job = start(env, jobs, "ok1", intensity={"smile": 1.5})
        seen = []

        async def report(j):
            seen.append((j.status, j.where()))
        await jobs.wait(job, 30, report)
        return job, seen
    job, seen = run(go())
    v = job.view()
    assert v["status"] == "succeeded" and v["percent"] == 100.0
    motion = pathlib.Path(v["result"]["files"]["motion"])
    assert motion.parent == env["tmp"] / "out" / "ok1"
    assert json.loads(motion.read_text()) == {"smile": 1.5}  # settings reached the CLI
    assert ("running", "tracking 3/3") in seen
    assert "a plain library line" in v["log_tail"]


def test_failed_job_reports_the_cli_error_and_removes_partial_output(env):
    async def go():
        jobs = env["jobs"]()
        job = start(env, jobs, "fail1")
        return await jobs.wait(job, 30)
    v = run(go()).view()
    assert v["status"] == "failed"
    assert v["error"] == {"type": "unusable_input", "message": "no face found in any frame"}
    assert not (env["tmp"] / "out" / "fail1").exists()


def test_cancel_stops_a_running_job(env):
    async def go():
        jobs = env["jobs"]()
        job = start(env, jobs, "slow1")
        await jobs.wait(job, 0.5)
        assert job.status == "running"
        await jobs.cancel(job.id)
        return job
    v = run(go()).view()
    assert v["status"] == "cancelled" and v["error"]["type"] == "cancelled"


def test_same_request_resumes_instead_of_running_twice_and_survives_a_restart(env):
    async def go():
        jobs = env["jobs"]()
        first = start(env, jobs, "ok2")
        again = start(env, jobs, "ok2")
        await jobs.wait(first, 30)
        return jobs, first, again
    jobs, first, again = run(go())
    assert again is first and len(jobs.jobs) == 1
    restarted = env["jobs"]()  # a new server process reads the job records
    assert restarted.get(first.id).view()["status"] == "succeeded"
    assert run(_resume(env, restarted, "ok2")).id == first.id


async def _resume(env, jobs, name):
    return start(env, jobs, name)


def test_existing_output_folder_is_never_overwritten(env):
    (env["tmp"] / "out" / "taken").mkdir(parents=True)

    async def go():
        start(env, env["jobs"](), "taken")
    with pytest.raises(M.ToolInputError, match="already exists"):
        run(go())


@pytest.mark.parametrize("value", ["../escape", "", "a b", "x" * 81, "dot.name"])
def test_names_are_restricted(value):
    assert not M.NAME_RE.match(value)


def test_paths_must_be_absolute_and_inside_allowed_dirs(env):
    policy = M.Policy(allow_dirs=(env["tmp"] / "model",))
    with pytest.raises(M.ToolInputError, match="absolute"):
        policy.path("clip.mp4", "video")
    with pytest.raises(M.ToolInputError, match="outside"):
        policy.path(str(env["video"]), "video")
    assert policy.model_dir(str(env["model"])) == env["model"]


def test_settings_validation_lists_every_error():
    cfg, errors = M.validate({"intensity": {"smile": 9, "nope": 1}, "bogus": {}})
    assert cfg is None and len(errors) == 3
    cfg, errors = M.validate({"intensity": {"smile": 2}}, preset="faithful")
    assert not errors and cfg.style.name == "faithful" and cfg.intensity.smile == 2.0


def test_toolsets_and_read_only_through_the_mcp_sdk(env):
    pytest.importorskip("mcp")
    from mcp import Client

    async def names(policy):
        async with Client(M.build_server(env["jobs"](), policy)) as c:
            return {t.name for t in (await c.list_tools()).tools}
    default = run(names(M.Policy()))
    assert {"v2l_generate", "v2l_job_status", "v2l_params"} <= default and "v2l_install_motion" not in default
    assert "v2l_generate" not in run(names(M.Policy(read_only=True, toolsets=M.TOOLSETS)))
    assert "v2l_install_motion" in run(names(M.Policy(toolsets=M.TOOLSETS)))


def test_generate_through_the_mcp_sdk(env):
    pytest.importorskip("mcp")
    from mcp import Client

    async def go():
        progress = []

        async def on_progress(p, total, message):
            progress.append(p)
        async with Client(M.build_server(env["jobs"]())) as c:
            r = await c.call_tool("v2l_generate", {"video": str(env["video"]), "model": str(env["model"]),
                                                   "name": "mcp1", "settings": {"intensity": {"smile": 2}}},
                                  progress_callback=on_progress)
            bad = await c.call_tool("v2l_generate", {"video": str(env["video"]), "model": str(env["model"]),
                                                     "name": "../x"})
            return r, bad, progress
    r, bad, progress = run(go())
    assert not r.is_error and r.structured_content["status"] == "succeeded"
    assert progress and progress == sorted(progress) and progress[-1] == 100.0
    assert bad.is_error and "name must be" in bad.content[0].text


MOTION = {"Version": 3, "Meta": {"Duration": 2.0, "Fps": 30.0, "FadeInTime": 0.5, "FadeOutTime": 0.5, "Loop": False,
                                 "AreBeziersRestricted": True, "CurveCount": 1, "TotalSegmentCount": 2,
                                 "TotalPointCount": 3, "UserDataCount": 0, "TotalUserDataSize": 0},
          "Curves": [{"Target": "Parameter", "Id": "ParamAngleX", "Segments": [0.0, 0.0, 0, 1.0, 10.0, 0, 2.0, 0.0]}]}


def test_motion_tools_through_the_mcp_sdk(env):
    pytest.importorskip("mcp")
    from mcp import Client

    out = env["tmp"] / "out"
    out.mkdir()
    src = out / "a.motion3.json"
    src.write_text(json.dumps(MOTION))
    model_in_out = out / "model"  # a model folder inside the output folder
    model_in_out.mkdir()
    (model_in_out / "m.model3.json").write_text("{}")
    ops = [{"op": "set_interpolation", "curves": "*", "type": "bezier"},
           {"op": "insert_key", "curve": "ParamAngleX", "t": 1.5}]

    async def go():
        async with Client(M.build_server(env["jobs"]())) as c:
            info = await c.call_tool("v2l_motion_info", {"motion": str(src)})
            read = await c.call_tool("v2l_motion_read", {"motion": str(src), "curves": ["ParamAngleX"]})
            edit = await c.call_tool("v2l_motion_edit", {"motion": str(src), "operations": ops,
                                                         "out": str(out / "b.motion3.json")})
            again = await c.call_tool("v2l_motion_edit", {"motion": str(src), "operations": ops,
                                                          "out": str(out / "b.motion3.json")})
            bad = await c.call_tool("v2l_motion_edit", {"motion": str(src), "operations": [{"op": "move_key",
                                    "curve": "ParamAngleX", "t": 1.0, "to_t": 2.5}], "out": str(out / "c.motion3.json")})
            outside = await c.call_tool("v2l_motion_edit", {"motion": str(src), "operations": ops,
                                                            "out": str(env["tmp"] / "x.motion3.json")})
            into_model = await c.call_tool("v2l_motion_edit", {"motion": str(src), "operations": ops,
                                                               "out": str(model_in_out / "x.motion3.json")})
            return info, read, edit, again, bad, outside, into_model
    info, read, edit, again, bad, outside, into_model = run(go())
    assert info.structured_content["counts"]["keys"] == 3 and info.structured_content["problems"] == []
    curve = read.structured_content["motion"]["curves"][0]
    assert curve["keys"] == [[0.0, 0.0], [1.0, 10.0], [2.0, 0.0]] and curve["segments"][0] == {"type": "linear"}
    assert not edit.is_error and edit.structured_content["counts"]["keys"] == 4
    written = json.loads((out / "b.motion3.json").read_text())
    assert written["Meta"]["TotalPointCount"] == 1 + 3 * 3
    assert again.is_error and "overwrite" in again.content[0].text
    assert bad.is_error and "operation 0 (move_key)" in bad.content[0].text and not (out / "c.motion3.json").exists()
    assert outside.is_error and "output folder" in outside.content[0].text
    assert into_model.is_error and "install toolset" in into_model.content[0].text


def test_read_only_keeps_reading_tools_but_hides_editing(env):
    pytest.importorskip("mcp")
    from mcp import Client

    async def names(policy):
        async with Client(M.build_server(env["jobs"](), policy)) as c:
            return {t.name for t in (await c.list_tools()).tools}
    ro = run(names(M.Policy(read_only=True, toolsets=M.TOOLSETS)))
    assert {"v2l_mapping_get", "v2l_motion_read", "v2l_motion_info"} <= ro and "v2l_motion_edit" not in ro
    assert "v2l_motion_edit" in run(names(M.Policy()))


def test_mapping_get_and_generate_with_mapping_through_the_mcp_sdk(env):
    pytest.importorskip("mcp")
    from mcp import Client

    override = {"format": "v2l.mapping", "version": 1, "entries": [{"output": "ParamAngleZ", "invert": True}]}

    async def go():
        async with Client(M.build_server(env["jobs"]())) as c:
            got = await c.call_tool("v2l_mapping_get", {"model": str(env["model"]), "mapping": override})
            bad = await c.call_tool("v2l_mapping_get", {"model": str(env["model"]), "mapping": {
                "entries": [{"output": "ParamAngleZ", "input": "NoSuchInput"}]}})
            gen = await c.call_tool("v2l_generate", {"video": str(env["video"]), "model": str(env["model"]),
                                                     "name": "mapped", "mapping": override})
            return got, bad, gen
    got, bad, gen = run(go())
    res = got.structured_content
    assert res["seen"] == override and any(i["name"] == "FaceAngleX" for i in res["inputs"])
    assert bad.is_error and "unknown input" in bad.content[0].text
    motion = pathlib.Path(gen.structured_content["result"]["files"]["motion"])
    assert json.loads(motion.read_text())["mapping"] == override


def test_bridge_check_through_the_mcp_sdk(env):
    pytest.importorskip("mcp")
    from mcp import Client

    async def go():
        async with Client(M.build_server(env["jobs"](), M.Policy(read_only=True))) as c:
            ok = await c.call_tool("v2l_bridge_check", {"func": "MyEar * 2", "mapping": {
                "bridge": [{"name": "MyEar", "func": "GazeX", "min": -1, "max": 1}]}})
            bad = await c.call_tool("v2l_bridge_check", {"func": "max(JawOpen,"})
            return ok, bad
    ok, bad = run(go())
    assert ok.structured_content["valid"] and ok.structured_content["inputs"] == ["MyEar"]
    assert not bad.structured_content["valid"] and bad.structured_content["problem"]["position"] == len("max(JawOpen,")
