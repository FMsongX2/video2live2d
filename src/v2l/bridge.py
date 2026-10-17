"""VBridger-compatible bridge: calibrated tracking inputs -> named bridge outputs.

Bridge files use the SnenkBridge preset format (a JSON list of {name, func, min, max,
defaultValue}), so presets written for SnenkBridge/VBridger-style tools can be loaded as-is.
Formulas are evaluated vectorised over all frames by a small AST interpreter (arithmetic,
comparisons, &&/||/!, if(), min/max and math::* functions); nothing else is executable.

Inputs available to formulas:
  - ARKit-named video estimates, rest-centred; mouth/jaw retain unit-scale headroom
  - HeadRotX = yaw, HeadRotY = pitch, HeadRotZ = roll in degrees, image space
    (yaw + = turn to image right, pitch + = up, roll + = head top to image right)
  - GazeX / GazeY (-1..1, image right / up)
  - MouthShift (-1..1, face-local lateral displacement; + is image-right at neutral)
  - LipOpen (0..1, measured lip aperture), LipWidth (-1..1, narrow..wide)
  - HeadPosX/Y/Z = 0 (not tracked)
Inputs a formula names but the video cannot provide (TongueOut, ...) read as 0.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import numpy as np

BRIDGES = pathlib.Path(__file__).parent / "bridges"

_FUNCS = {
    "abs": np.abs, "min": np.minimum, "max": np.maximum, "sqrt": np.sqrt, "cbrt": np.cbrt,
    "exp": np.exp, "ln": np.log, "log10": np.log10, "log2": np.log2, "pow": np.power,
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "asin": np.arcsin, "acos": np.arccos,
    "atan": np.arctan, "atan2": np.arctan2, "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
    "hypot": np.hypot, "floor": np.floor, "ceil": np.ceil, "round": np.round,
}


class BridgeError(ValueError):
    pass


def _to_python(src: str) -> str:
    s = re.sub(r"//[^\n]*", "", src)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = s.replace("math::pi()", repr(np.pi)).replace("math::", "")
    s = s.replace("&&", " and ").replace("||", " or ")
    s = re.sub(r"!(?!=)", " not ", s)
    s = s.replace("^", "**")
    s = re.sub(r"\bif\s*\(", "_if(", s)
    return s


class Formula:
    def __init__(self, src: str):
        self.src = src
        try:
            self.tree = ast.parse(_to_python(src).strip(), mode="eval")
        except SyntaxError as e:
            raise BridgeError(f"cannot parse formula {src!r}: {e}") from e
        self.names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)} - set(_FUNCS) - {"_if"}

    def __call__(self, env: dict[str, np.ndarray]) -> np.ndarray:
        return self._eval(self.tree.body, env)

    def _eval(self, node, env):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand, env)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return v
            if isinstance(node.op, ast.Not):
                return np.logical_not(v)
        if isinstance(node, ast.BinOp):
            a, b = self._eval(node.left, env), self._eval(node.right, env)
            ops = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply,
                   ast.Div: np.divide, ast.Pow: np.power, ast.Mod: np.mod}
            for k, fn in ops.items():
                if isinstance(node.op, k):
                    with np.errstate(divide="ignore", invalid="ignore"):
                        return fn(a, b)
        if isinstance(node, ast.BoolOp):
            vals = [self._eval(v, env) for v in node.values]
            fn = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
            out = vals[0]
            for v in vals[1:]:
                out = fn(out, v)
            return out
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            a, b = self._eval(node.left, env), self._eval(node.comparators[0], env)
            ops = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater,
                   ast.GtE: np.greater_equal, ast.Eq: np.equal, ast.NotEq: np.not_equal}
            for k, fn in ops.items():
                if isinstance(node.ops[0], k):
                    return fn(a, b)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            args = [self._eval(a, env) for a in node.args]
            if node.func.id == "_if" and len(args) == 3:
                return np.where(args[0], args[1], args[2])
            fn = _FUNCS.get(node.func.id)
            if fn is not None:
                if fn in (np.minimum, np.maximum) and len(args) > 2:
                    out = args[0]
                    for v in args[1:]:
                        out = fn(out, v)
                    return out
                return fn(*args)
        raise BridgeError(f"unsupported expression in {self.src!r}: {ast.dump(node)[:80]}")


class Bridge:
    def __init__(self, entries: list[dict], name: str = "bridge"):
        self.name = name
        self.entries = [dict(e, formula=Formula(e["func"])) for e in entries if str(e.get("func", "")).strip()]

    @classmethod
    def load(cls, path_or_name: str) -> "Bridge":
        p = pathlib.Path(path_or_name)
        if not p.exists():
            p = BRIDGES / f"{path_or_name}.json"
        data = json.loads(p.read_text())
        if isinstance(data, dict):  # SnenkBridge settings file with a parameter list inside
            data = data.get("parameters") or data.get("customParam") or []
        return cls(data, name=p.stem)

    def inputs(self) -> set[str]:
        return set().union(*(e["formula"].names for e in self.entries))

    def evaluate(self, x: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], list[str]]:
        n = len(x["yaw"])
        zeros = np.zeros(n)
        env = dict(x)
        env.update(HeadRotX=x["yaw"], HeadRotY=x["pitch"], HeadRotZ=x["roll"],
                   HeadPosX=zeros, HeadPosY=zeros, HeadPosZ=zeros)
        missing = sorted(self.inputs() - set(env))
        for m in missing:
            env[m] = zeros
        out: dict[str, np.ndarray] = {}
        for e in self.entries:
            v = np.broadcast_to(np.asarray(e["formula"](env), dtype=float), (n,)).copy()
            v[~np.isfinite(v)] = float(e.get("defaultValue", 0.0))
            out[e["name"]] = np.clip(v, float(e.get("min", -1e6)), float(e.get("max", 1e6)))
        return out, missing
