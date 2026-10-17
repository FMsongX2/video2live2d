"""Live2D model folder: settings, VTube Studio mapping, physics outputs, parameter ranges, probe."""

from __future__ import annotations

import dataclasses
import json
import pathlib


@dataclasses.dataclass
class Model:
    dir: pathlib.Path
    settings_file: pathlib.Path
    settings: dict
    vtube: dict | None
    physics_outputs: set[str]
    ranges: dict[str, dict]          # id -> {min, max, def}; filled by the renderer (moc3)
    probe: dict | None = None        # screen-direction probe, see render.probe_model

    @property
    def name(self) -> str:
        return self.settings_file.name.removesuffix(".model3.json")


def find_settings(model_dir: pathlib.Path) -> pathlib.Path:
    found = [p for p in model_dir.glob("*.model3.json") if not p.name.endswith("items_pinned_to_model.json")]
    if len(found) != 1:
        raise FileNotFoundError(f"expected one *.model3.json in {model_dir}, found {[p.name for p in found]}")
    return found[0]


def load(model_dir: str | pathlib.Path) -> Model:
    d = pathlib.Path(model_dir).expanduser().resolve()
    sf = find_settings(d)
    settings = json.loads(sf.read_text(encoding="utf-8-sig"))
    refs = settings.get("FileReferences", {})
    vt = sorted(d.glob("*.vtube.json"))
    vtube = json.loads(vt[0].read_text(encoding="utf-8-sig")) if vt else None
    phys_out: set[str] = set()
    if refs.get("Physics"):
        phys = json.loads((d / refs["Physics"]).read_text(encoding="utf-8-sig"))
        for s in phys.get("PhysicsSettings", []):
            for o in s.get("Output", []):
                phys_out.add(o["Destination"]["Id"])
    return Model(dir=d, settings_file=sf, settings=settings, vtube=vtube, physics_outputs=phys_out, ranges={})
