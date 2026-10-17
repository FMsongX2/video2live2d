import json
import subprocess
import sys

import pytest

from v2l import cli
from v2l import config as C


def settings(*args):
    return cli.settings(cli._parser().parse_args(["run", "clip.mp4", "--model", "m", "--name", "n", *args]))


def test_precedence_preset_then_files_then_flags_then_set(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"output": {"fps": 24, "fade": 1.0}, "intensity": {"smile": 2.0, "head": 0.5}}))
    b.write_text(json.dumps({"output": {"fade": 1.5}, "intensity": {"smile": 2.5}}))
    cfg = settings("--preset", "faithful", "--config", str(a), "--config", str(b), "--fps", "60",
                   "--set", "output.fps=50", "--set", "intensity.smile=1.5", "--neutral", "1:2")
    assert cfg.style.name == "faithful"         # preset
    assert cfg.output.fade == 1.5                # later file wins over earlier file
    assert cfg.intensity.head == 0.5             # file over defaults
    assert cfg.output.fps == 50.0                # --set over shortcut flag over file
    assert cfg.intensity.smile == 1.5            # --set over files
    assert cfg.calibration.neutral_window == (1.0, 2.0)
    assert settings("--config", str(a), "--fps", "60").output.fps == 60.0  # flag over file
    assert settings("--preset", "faithful", "--style", "cute").style.name == "cute"


def test_defaults_without_options_are_the_schema_defaults():
    assert C.to_dict(settings()) == C.to_dict(C.Config())


@pytest.mark.parametrize("assignment, message", [
    ("intensity.smiles=1", "unknown setting intensity.smiles"),
    ("intensityy.smile=1", "unknown section"),
    ("intensity.smile=9", "outside"),
    ("calibration.neutral_window=3:2", "end must be after the start"),
    ("calibration.blink_deadzone=0.9,0.1", "must not exceed"),
    ("output.loop=maybe", "true or false"),
    ("style.name=sparkly", "must be one of"),
])
def test_strict_validation(assignment, message):
    with pytest.raises(C.ConfigError, match=message):
        settings("--set", assignment)


def _cli(*args):
    r = subprocess.run([sys.executable, "-m", "v2l.cli", *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def test_json_usage_error_is_one_object_on_stdout():
    code, out, _ = _cli("run", "--json")
    assert code == 2
    lines = out.strip().splitlines()
    assert len(lines) == 1
    res = json.loads(lines[0])
    assert res["ok"] is False and res["error"]["type"] == "usage" and res["error"]["message"]


def test_json_config_and_input_errors(tmp_path):
    code, out, _ = _cli("run", str(tmp_path / "missing.mp4"), "--model", str(tmp_path), "--name", "x",
                        "--set", "intensity.smile=9", "--json")
    assert code == 2 and json.loads(out)["error"]["type"] == "config"
    code, out, _ = _cli("run", str(tmp_path / "missing.mp4"), "--model", str(tmp_path), "--name", "x", "--json")
    assert code == 2 and json.loads(out)["error"]["type"] == "input"


def test_dry_run_reports_settings_and_planned_files(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    model = tmp_path / "model"
    model.mkdir()
    (model / "m.model3.json").write_text("{}")
    code, out, _ = _cli("run", str(video), "--model", str(model), "--name", "x", "--out", str(tmp_path / "o"),
                        "--no-preview", "--set", "intensity.gaze=2", "--dry-run", "--json")
    res = json.loads(out)
    assert code == 0 and res["dry_run"] is True
    assert res["files"]["motion"] == str(tmp_path / "o" / "x.motion3.json") and res["files"]["preview"] is None
    assert res["installed"] == str(model / "x.motion3.json")
    assert res["settings"]["intensity"]["gaze"] == 2.0
    assert not (tmp_path / "o").exists()


def test_bridge_check_and_show_work_without_numpy():
    code = ("import sys, json; sys.modules['numpy'] = None\n"
            "from v2l import cli\n"
            "sys.exit(cli.main(['bridge', 'check', '--func', 'JawOpen * (1 - CheekPuff', '--json']))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    res = json.loads(r.stdout)
    assert r.returncode == 2 and res["valid"] is False and res["error"]["type"] == "invalid"
    assert res["problem"]["position"] == len("JawOpen * (1 - CheekPuff") and res["inputs"] == ["CheekPuff", "JawOpen"]
    code, out, _ = _cli("bridge", "show", "--json")
    assert code == 0 and any(p["name"] == "FaceAngleX" and p["source"] == "preset" for p in json.loads(out)["params"])


def test_dry_run_plans_the_signals_file_and_checks_the_mapping(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    model = tmp_path / "model"
    model.mkdir()
    (model / "m.model3.json").write_text("{}")
    good = tmp_path / "map.json"
    good.write_text(json.dumps({"format": "v2l.mapping", "version": 1,
                                "bridge": [{"name": "MouthOpen", "modifiers": {"smoothing": 0.3}}]}))
    code, out, _ = _cli("run", str(video), "--model", str(model), "--name", "x", "--out", str(tmp_path / "o"),
                        "--mapping", str(good), "--dump-signals", "--dry-run", "--json")
    res = json.loads(out)
    assert code == 0 and res["files"]["signals"] == str(tmp_path / "o" / "x.signals.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": "v2l.mapping", "version": 1, "bridge": [{"name": "MouthOpen", "func": "1 +"}]}))
    code, out, _ = _cli("run", str(video), "--model", str(model), "--name", "x", "--mapping", str(bad), "--dry-run",
                        "--json")
    err = json.loads(out)["error"]
    assert code == 2 and err["type"] == "invalid" and any("cannot parse formula" in p for p in err["problems"])


def test_signals_dump_shape():
    np = pytest.importorskip("numpy")
    from v2l.pipeline import signals_json

    raw = {"yaw": np.array([1.23456, np.nan, 3.0]), "EyeWideLeft": np.array([0.1, 0.2, 0.3])}
    curved = {"yaw": raw["yaw"], "EyeWideLeft": np.array([0.2, 0.4, 0.6])}
    d = signals_json(raw, curved, {"MouthOpen": np.array([0.0, 0.5, 1.0])}, 30.0, {"EyeWideLeft": {}})
    assert d == {"fps": 30.0, "duration": 2 / 30,
                 "inputs": {"yaw": [1.2346, None, 3.0], "EyeWideLeft": [0.2, 0.4, 0.6]},
                 "inputs_raw": {"EyeWideLeft": [0.1, 0.2, 0.3]}, "bridge": {"MouthOpen": [0.0, 0.5, 1.0]}}
    json.dumps(d, allow_nan=False)
