"""VBridger-compatible bridge: calibrated tracking inputs -> named bridge outputs.

Bridge files use the SnenkBridge preset format: a JSON list of parameters {name, func, min, max,
defaultValue, delayBuffer?}, or the wrapper {"format": "snek", "version": 1, "title", "params":
[...]}, so presets written for SnenkBridge/VBridger-style tools load as they are. Unknown keys are
ignored by those tools, which lets V2L store its optional `modifiers` in the same objects.

Formulas are parsed here (standard library) into a small expression tree: numbers, input names,
+ - * / % ^ (power, left to right), comparisons, && || !, if(cond, a, b), min/max and math::*
functions, math::pi(), // and /* */ comments; nothing else is executable. They are evaluated
vectorised over all frames with numpy, which is imported only for evaluation.

Inputs available to formulas:
  - ARKit-named video estimates, rest-centred; mouth/jaw retain unit-scale headroom
  - HeadRotX = yaw, HeadRotY = pitch, HeadRotZ = roll in degrees, image space
    (yaw + = turn to image right, pitch + = up, roll + = head top to image right)
  - GazeX / GazeY (-1..1, image right / up)
  - MouthShift (-1..1, face-local lateral displacement; + is image-right at neutral)
  - LipOpen (0..1, measured lip aperture), LipWidth (-1..1, narrow..wide)
  - HeadPosX/Y/Z = 0 (not tracked)
  - WaveN / PingPongN: N-millisecond time waves (triangle 0..1..0 / ramp 0..1) from the clip start
Inputs a formula names but the video cannot provide (TongueOut, ...) read as 0.

Each output frame: the formula (or the delay buffer) gives a value, non-finite values fall back to
defaultValue, the value is clamped to [min, max], then the optional modifiers run in this order:
delayMs, smoothing, steps (see docs/motion-editing.md section 5).
"""

from __future__ import annotations

import ast
import json
import math
import pathlib
import re

BRIDGES = pathlib.Path(__file__).parent / "bridges"

# Formula functions -> numpy function names, and their argument counts (None: one or more).
FUNCTIONS = {
    "abs": ("abs", 1), "min": ("minimum", None), "max": ("maximum", None), "sqrt": ("sqrt", 1),
    "cbrt": ("cbrt", 1), "exp": ("exp", 1), "ln": ("log", 1), "log10": ("log10", 1), "log2": ("log2", 1),
    "pow": ("power", 2), "sin": ("sin", 1), "cos": ("cos", 1), "tan": ("tan", 1), "asin": ("arcsin", 1),
    "acos": ("arccos", 1), "atan": ("arctan", 1), "atan2": ("arctan2", 2), "sinh": ("sinh", 1),
    "cosh": ("cosh", 1), "tanh": ("tanh", 1), "hypot": ("hypot", 2), "floor": ("floor", 1),
    "ceil": ("ceil", 1), "round": ("round", 1),
}
HEAD = ("HeadRotX", "HeadRotY", "HeadRotZ", "HeadPosX", "HeadPosY", "HeadPosZ")
TIME_RE = re.compile(r"^(Wave|PingPong)([1-9][0-9]*)$")
DELAY_KEYS = ("refParam", "smoothing", "delayCount", "inMin", "inMax", "outMin", "outMax")
MAX_DELAY = 1_000_000
PARAM_KEYS = ("name", "func", "min", "max", "defaultValue", "delayBuffer", "modifiers", "enabled")


class BridgeError(ValueError):
    """A formula or bridge parameter that cannot be used; position/end locate a formula error."""

    def __init__(self, message: str, position: int | None = None, end: int | None = None):
        super().__init__(message)
        self.position, self.end = position, end


# ----------------------------------------------------------------------------------- formulas

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<line>//[^\n]*)
  | (?P<block>/\*.*?\*/)
  | (?P<open_block>/\*)
  | (?P<number>0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?)
  | (?P<operator>&&|\|\||==|!=|<=|>=|[-+*/%^<>!])
  | (?P<paren>[()])
  | (?P<comma>,)
""", re.S | re.X)


def tokenize(src: str) -> list[tuple[str, str, int, int]]:
    """(kind, text, start, end) tokens; whitespace and comments are dropped."""
    out, i = [], 0
    while i < len(src):
        m = _TOKEN.match(src, i)
        if not m:
            raise BridgeError(f"unexpected character {src[i]!r}", i, i + 1)
        kind = m.lastgroup
        if kind == "open_block":
            raise BridgeError("unclosed /* comment", i, len(src))
        if kind not in ("ws", "line", "block"):
            out.append((kind, m.group(), m.start(), m.end()))
        i = m.end()
    return out


_BIN = {"+": ast.Add, "-": ast.Sub, "*": ast.Mult, "/": ast.Div, "%": ast.Mod, "^": ast.Pow}
_CMP = {"==": ast.Eq, "!=": ast.NotEq, "<": ast.Lt, "<=": ast.LtE, ">": ast.Gt, ">=": ast.GtE}


class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.tokens = tokenize(src)
        self.i = 0
        self.names: dict[str, list[tuple[int, int]]] = {}
        self.functions: list[str] = []
        self.calls: set[int] = set()  # token indexes of function names

    def peek(self, *texts: str):
        if self.i < len(self.tokens) and (not texts or self.tokens[self.i][1] in texts):
            return self.tokens[self.i]
        return None

    def take(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def fail(self, message: str, tok=None):
        if tok is None:
            tok = self.tokens[self.i] if self.i < len(self.tokens) else ("eof", "", len(self.src), len(self.src))
        raise BridgeError(message, tok[2], tok[3])

    def parse(self):
        if not self.tokens:
            raise BridgeError("the formula is empty", 0, len(self.src))
        node = self.expr()
        if self.i < len(self.tokens):
            self.fail(f"unexpected {self.tokens[self.i][1]!r}")
        return node

    def expr(self):
        return self.boolean("||", ast.Or, self.conjunction)

    def conjunction(self):
        return self.boolean("&&", ast.And, self.comparison)

    def boolean(self, text, op, sub):
        values = [sub()]
        while self.peek(text):
            self.take()
            values.append(sub())
        return values[0] if len(values) == 1 else ast.BoolOp(op=op(), values=values)

    def comparison(self):
        left = self.additive()
        if self.peek(*_CMP):
            op = _CMP[self.take()[1]]()
            left = ast.Compare(left=left, ops=[op], comparators=[self.additive()])
            if self.peek(*_CMP):
                self.fail("comparisons cannot be chained; use &&")
        return left

    def additive(self):
        left = self.multiplicative()
        while self.peek("+", "-"):
            left = ast.BinOp(left=left, op=_BIN[self.take()[1]](), right=self.multiplicative())
        return left

    def multiplicative(self):
        left = self.unary()
        while self.peek("*", "/", "%"):
            left = ast.BinOp(left=left, op=_BIN[self.take()[1]](), right=self.unary())
        return left

    def unary(self):
        if self.peek("-", "+", "!"):
            op = {"-": ast.USub, "+": ast.UAdd, "!": ast.Not}[self.take()[1]]()
            return ast.UnaryOp(op=op, operand=self.unary())
        return self.power()

    def power(self):  # binds tighter than a sign and groups left to right, as in SnenkBridge
        left = self.primary()
        while self.peek("^"):
            self.take()
            right = self.unary() if self.peek("-", "+") else self.primary()
            left = ast.BinOp(left=left, op=ast.Pow(), right=right)
        return left

    def primary(self):
        if self.i >= len(self.tokens):
            self.fail("the formula ends too early")
        kind, text, start, end = tok = self.take()
        if kind == "number":
            return ast.Constant(value=float(int(text, 0)) if text[:2].lower() in ("0x", "0b", "0o") else float(text))
        if text == "(":
            node = self.expr()
            if not self.peek(")"):
                self.fail("missing )")
            self.take()
            return node
        if kind == "name":
            if self.peek("("):
                return self.call(tok)
            if "::" in text:
                self.fail(f"{text} is not a value; did you mean {text}(...)?", tok)
            self.names.setdefault(text, []).append((start, end))
            return ast.Name(id=text)
        self.fail(f"unexpected {text!r}", tok)

    def call(self, tok):
        name = tok[1].removeprefix("math::")
        self.calls.add(self.i - 1)
        self.take()  # (
        args = []
        if not self.peek(")"):
            args.append(self.expr())
            while self.peek(","):
                self.take()
                args.append(self.expr())
        if not self.peek(")"):
            self.fail("missing ) after the arguments")
        self.take()
        if name == "pi":
            if args:
                self.fail("math::pi() takes no arguments", tok)
            return ast.Constant(value=math.pi)
        if name == "if":
            if len(args) != 3:
                self.fail("if(condition, then, else) takes three arguments", tok)
            self.functions.append("if")
            return ast.Call(func=ast.Name(id="_if"), args=args, keywords=[])
        if name not in FUNCTIONS:
            self.fail(f"unknown function {tok[1]!r}", tok)
        arity = FUNCTIONS[name][1]
        if (arity is None and not args) or (arity is not None and len(args) != arity):
            self.fail(f"{name} takes {'one or more' if arity is None else arity} argument(s)", tok)
        self.functions.append(name)
        return ast.Call(func=ast.Name(id=name), args=args, keywords=[])


def known_names(extra=()) -> set[str]:
    """Names a formula can read: the calibrated tracking inputs and the head channels."""
    from .inputs import TRACKING

    return {n for n, *_ in TRACKING} | set(HEAD) | set(extra)


def check(func: str, known: set[str] | None = None) -> dict:
    """Parse one formula for an editor: {"valid", "inputs", "unknown", "functions", "tokens",
    "problem": null | {"message", "position", "end"}} (0-based character offsets)."""
    known = known_names() if known is None else known
    try:
        tokens = tokenize(func)
    except BridgeError as e:
        return {"valid": False, "inputs": [], "unknown": [], "functions": [], "tokens": [],
                "problem": {"message": str(e), "position": e.position, "end": e.end}}
    p = _Parser(func)
    problem = None
    try:
        p.parse()
    except BridgeError as e:
        problem = {"message": str(e), "position": e.position, "end": e.end}
    kinds = []
    for n, (kind, text, start, end) in enumerate(tokens):
        if kind == "name":
            kind = "function" if (n + 1 < len(tokens) and tokens[n + 1][1] == "(") else "name"
        kinds.append({"start": start, "end": end, "kind": kind})
    names = sorted(p.names)
    readable = [n for n in names if n in known or TIME_RE.match(n)]
    return {"valid": problem is None, "inputs": readable, "unknown": [n for n in names if n not in readable],
            "functions": sorted(set(p.functions)), "tokens": kinds, "problem": problem}


class Formula:
    def __init__(self, src: str):
        self.src = src
        p = _Parser(src)
        try:
            self.tree = p.parse()
        except BridgeError as e:
            raise BridgeError(f"cannot parse formula {src!r}: {e}", e.position, e.end) from None
        self.names = set(p.names)

    def __call__(self, env: dict):
        return self._eval(self.tree, env)

    def _eval(self, node, env):
        import numpy as np

        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand, env)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return v
            return np.logical_not(v)
        if isinstance(node, ast.BinOp):
            a, b = self._eval(node.left, env), self._eval(node.right, env)
            ops = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply,
                   ast.Div: np.divide, ast.Pow: np.power, ast.Mod: np.mod}
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                return ops[type(node.op)](a, b)
        if isinstance(node, ast.BoolOp):
            vals = [self._eval(v, env) for v in node.values]
            fn = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
            out = vals[0]
            for v in vals[1:]:
                out = fn(out, v)
            return out
        if isinstance(node, ast.Compare):
            a, b = self._eval(node.left, env), self._eval(node.comparators[0], env)
            ops = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater,
                   ast.GtE: np.greater_equal, ast.Eq: np.equal, ast.NotEq: np.not_equal}
            return ops[type(node.ops[0])](a, b)
        args = [self._eval(a, env) for a in node.args]
        if node.func.id == "_if":
            return np.where(args[0], args[1], args[2])
        fn = getattr(np, FUNCTIONS[node.func.id][0])
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            out = args[0]
            if node.func.id in ("min", "max"):
                for v in args[1:]:
                    out = fn(out, v)
                return out
            return fn(*args)


# ------------------------------------------------------------------------------ presets

def read_preset(path_or_name: str) -> tuple[str, list[dict]]:
    """(name, parameters) of a built-in preset name or a SnenkBridge-format file: a list of
    parameters, the {"format": "snek", "version": 1, "params": [...]} wrapper, or an older settings
    file with a "parameters"/"customParam" list."""
    p = pathlib.Path(path_or_name).expanduser()
    if not p.is_file():
        p = BRIDGES / f"{path_or_name}.json"
    if not p.is_file():
        names = ", ".join(sorted(x.stem for x in BRIDGES.glob("*.json")))
        raise FileNotFoundError(f"bridge preset {path_or_name!r} is neither a file nor a built-in preset ({names})")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise BridgeError(f"{p.name} is not valid JSON: {e}") from None
    if isinstance(data, dict):
        if "format" in data:
            if data.get("format") != "snek":
                raise BridgeError(f"{p.name}: unsupported preset format {data.get('format')!r} (expected \"snek\")")
            if data.get("version", 1) != 1:
                raise BridgeError(f"{p.name}: unsupported preset version {data.get('version')!r}")
            data = data.get("params", [])
        else:
            data = data.get("parameters") or data.get("customParam") or []
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        raise BridgeError(f"{p.name}: expected a list of bridge parameters")
    return p.stem, [dict(e) for e in data]


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _active(p: dict) -> bool:
    return p.get("enabled", True) is not False and (bool(str(p.get("func", "")).strip()) or bool(p.get("delayBuffer")))


def delay_problems(d, where: str) -> list[str]:
    if not isinstance(d, dict):
        return [f"{where}.delayBuffer must be an object with {', '.join(DELAY_KEYS)}"]
    errs = [f"{where}.delayBuffer: missing {k}" for k in DELAY_KEYS if k not in d]
    errs += [f"{where}.delayBuffer: unknown field {k}" for k in d if k not in DELAY_KEYS]
    if errs:
        return errs
    if not isinstance(d["refParam"], str) or not d["refParam"]:
        errs.append(f"{where}.delayBuffer.refParam must be an input or bridge output name")
    if not all(_num(d[k]) for k in ("smoothing", "inMin", "inMax", "outMin", "outMax")):
        errs.append(f"{where}.delayBuffer: smoothing and the in/out ranges must be numbers")
    elif d["inMin"] > d["inMax"] or d["outMin"] > d["outMax"]:
        errs.append(f"{where}.delayBuffer: inMin/outMin must not exceed inMax/outMax")
    if not (isinstance(d["delayCount"], int) and not isinstance(d["delayCount"], bool)
            and 0 <= d["delayCount"] <= MAX_DELAY):
        errs.append(f"{where}.delayBuffer.delayCount must be a whole number 0..{MAX_DELAY}")
    return errs


def modifier_problems(m, where: str) -> list[str]:
    if not isinstance(m, dict):
        return [f"{where}.modifiers must be an object {{delayMs?, smoothing?, steps?}}"]
    errs = [f"{where}.modifiers: unknown field {k}" for k in m if k not in ("delayMs", "smoothing", "steps")]
    if "delayMs" in m and not (_num(m["delayMs"]) and m["delayMs"] >= 0):
        errs.append(f"{where}.modifiers.delayMs must be a number >= 0")
    if "smoothing" in m and not (_num(m["smoothing"]) and 0 <= m["smoothing"] < 1):
        errs.append(f"{where}.modifiers.smoothing must be a number in 0..1 (1 excluded)")
    if "steps" in m:
        steps = m["steps"]
        if not isinstance(steps, list):
            return errs + [f"{where}.modifiers.steps must be a list"]
        for n, s in enumerate(steps):
            w = f"{where}.modifiers.steps[{n}]"
            if not isinstance(s, dict) or not (_num(s.get("trigger")) and _num(s.get("target"))):
                errs.append(f"{w} needs numbers trigger and target")
                continue
            errs += [f"{w}: unknown field {k}" for k in s if k not in ("trigger", "target", "holdMs", "threshold")]
            for k in ("holdMs", "threshold"):
                if k in s and not (_num(s[k]) and s[k] >= 0):
                    errs.append(f"{w}.{k} must be a number >= 0")
        trig = [s.get("trigger") for s in steps if isinstance(s, dict) and _num(s.get("trigger"))]
        if any(b <= a for a, b in zip(trig, trig[1:])):
            errs.append(f"{where}.modifiers.steps: triggers must be strictly increasing")
    return errs


def problems(params: list[dict]) -> list[str]:
    """Every problem of an effective parameter list (names, ranges, formulas, delay buffers,
    modifiers, delayed-dependency cycles)."""
    errs: list[str] = []
    seen = set()
    for p in params:
        name = p.get("name")
        where = f"bridge {name!r}"
        if not isinstance(name, str) or not name.strip() or name in seen:
            errs.append(f"{where}: empty or duplicate name")
        seen.add(name)
        if p.get("enabled", True) is False:
            continue
        if not all(_num(p.get(k)) for k in ("min", "max", "defaultValue")):
            errs.append(f"{where}: min, max and defaultValue must be numbers")
        elif not p["min"] <= p["defaultValue"] <= p["max"]:
            errs.append(f"{where}: needs min <= defaultValue <= max")
        if p.get("delayBuffer"):
            errs += delay_problems(p["delayBuffer"], where)
        else:
            func = p.get("func", "")
            if not isinstance(func, str) or not func.strip():
                errs.append(f"{where}: needs a func (or a delayBuffer)")
            else:
                try:
                    Formula(func)
                except BridgeError as e:
                    errs.append(f"{where}: {e}")
        if p.get("modifiers") is not None:
            errs += modifier_problems(p["modifiers"], where)
    try:
        dependency_order([p for p in params if _active(p)])
    except BridgeError as e:
        errs.append(str(e))
    return errs


def dependency_order(params: list[dict]) -> list[int]:
    """Evaluation order: a delay buffer after the output it reads. Raises on cycles."""
    index = {p["name"]: i for i, p in enumerate(params)}
    state = [0] * len(params)
    order: list[int] = []

    def visit(i: int) -> None:
        if state[i] == 2:
            return
        if state[i] == 1:
            raise BridgeError(f"bridge {params[i]['name']!r}: delayed dependency cycle")
        state[i] = 1
        d = params[i].get("delayBuffer")
        if d and d.get("refParam") in index:
            visit(index[d["refParam"]])
        state[i] = 2
        order.append(i)

    for i in range(len(params)):
        visit(i)
    return order


def override_problems(overrides, base_names: set[str]) -> tuple[list[str], list[str]]:
    """(errors, warnings) of the "bridge" section of a v2l.mapping file (structure only)."""
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(overrides, list):
        return ["bridge must be a list of bridge parameters"], []
    seen = set()
    for n, o in enumerate(overrides):
        if not isinstance(o, dict) or not isinstance(o.get("name"), str) or not o["name"].strip():
            errs.append(f"bridge[{n}] must be an object with a name")
            continue
        where = f"bridge[{n}] ({o['name']})"
        if o["name"] in seen:
            errs.append(f"{where}: {o['name']} appears twice; use one object per output")
        seen.add(o["name"])
        unknown = set(o) - set(PARAM_KEYS)
        if unknown:
            errs.append(f"{where}: unknown field {', '.join(sorted(unknown))}; fields are {', '.join(PARAM_KEYS)}")
        if "enabled" in o and not isinstance(o["enabled"], bool):
            errs.append(f"{where}: enabled must be true or false")
        if o["name"] not in base_names:
            if o.get("enabled", True) is False:
                warns.append(f"{where}: the preset has no output {o['name']}, so there is nothing to disable")
            elif not (str(o.get("func", "")).strip() or o.get("delayBuffer")) or not ("min" in o and "max" in o):
                errs.append(f"{where}: a new bridge output needs func (or delayBuffer), min and max")
    return errs, warns


def effective(preset_params: list[dict], overrides: list[dict] | None) -> list[dict]:
    """Preset parameters with the overrides applied, each with "source" (preset | override |
    added | disabled) and "overridden" (fields changed). Disabled ones stay in the list."""
    out = []
    by_name = {o["name"]: o for o in overrides or []}
    for p in preset_params:
        p = dict(p, source="preset", overridden=[])
        o = by_name.get(p.get("name"))
        if o is not None:
            _patch(p, o)
            p["source"] = "disabled" if p.get("enabled", True) is False else "override"
        out.append(p)
    names = {p.get("name") for p in preset_params}
    for o in overrides or []:
        if o["name"] in names or o.get("enabled", True) is False:
            continue
        p = {"name": o["name"], "func": "", "min": 0.0, "max": 1.0, "source": "added", "overridden": []}
        _patch(p, o)
        p.setdefault("defaultValue", min(max(0.0, float(p["min"])), float(p["max"])) if _num(p["min"]) and _num(p["max"]) else 0.0)
        out.append(p)
    return out


def _patch(p: dict, o: dict) -> None:
    for k in ("func", "min", "max", "defaultValue", "enabled"):
        if k in o:
            p[k] = o[k]
            p["overridden"].append(k)
    if "delayBuffer" in o:
        if o["delayBuffer"] is None:
            p.pop("delayBuffer", None)
        else:
            p["delayBuffer"] = {**(p.get("delayBuffer") or {}), **o["delayBuffer"]} if isinstance(o["delayBuffer"], dict) else o["delayBuffer"]
        p["overridden"].append("delayBuffer")
    if "modifiers" in o:
        if o["modifiers"] is None:
            p.pop("modifiers", None)
        else:
            p["modifiers"] = o["modifiers"]
        p["overridden"].append("modifiers")


def describe(params: list[dict], known: set[str] | None = None) -> list[dict]:
    """Effective parameters for display: fields plus the names each one reads."""
    known = known_names() if known is None else known
    outputs = {p.get("name") for p in params if _active(p)}
    rows = []
    for p in params:
        row = {"name": p.get("name"), "func": p.get("func", ""), "min": p.get("min"), "max": p.get("max"),
               "defaultValue": p.get("defaultValue"), "delayBuffer": p.get("delayBuffer"),
               "modifiers": p.get("modifiers"), "source": p.get("source", "preset"),
               "overridden": list(p.get("overridden", []))}
        if p.get("delayBuffer"):
            ref = p["delayBuffer"].get("refParam")
            ok = ref in known or ref in outputs
            row["inputs"], row["unknown"] = ([ref], []) if ok else ([], [ref])
        else:
            c = check(str(p.get("func", "")), known)
            row["inputs"], row["unknown"] = c["inputs"], c["unknown"]
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------- evaluation

def delay_buffer(ref, d: dict):
    """SnenkBridge-style delay buffer over frames: each frame the reference value is clamped to
    inMin..inMax and mapped onto outMin..outMax, then written into a ring of max(delayCount, 1)
    slots that starts filled with 0; the slot read back is the oldest one (the value from
    delayCount - 1 frames earlier), and the output moves toward it by 1/max(smoothing, 1) of the
    remaining distance per frame, starting from 0."""
    import numpy as np

    n_slots = max(int(d["delayCount"]), 1)
    ring = [0.0] * n_slots
    cursor, current = 0, 0.0
    lo, hi, olo, ohi = float(d["inMin"]), float(d["inMax"]), float(d["outMin"]), float(d["outMax"])
    rate = 1.0 / max(float(d["smoothing"]), 1.0)
    out = np.empty(len(ref))
    for k, x in enumerate(np.asarray(ref, dtype=float)):
        x = x if math.isfinite(x) else 0.0
        u = 0.0 if hi == lo else (min(max(x, lo), hi) - lo) / (hi - lo)
        ring[cursor] = olo + u * (ohi - olo)
        cursor = (cursor + 1) % n_slots
        current += (ring[cursor] - current) * rate
        out[k] = current
    return out


def apply_steps(v, steps: list[dict], lo: float, hi: float, fps: float):
    """VBridger-style steps: the output sits on the target of the highest step whose trigger the
    value has reached; it drops one step when the value falls below that step's trigger by more
    than its threshold. Entering a step by rising holds it for at least holdMs. Below the first
    trigger the output is the range minimum; a step at the range maximum is implied unless a
    trigger already reaches it."""
    import numpy as np

    levels = [(-math.inf, lo, 0.0, 0.0)] + [(float(s["trigger"]), float(s["target"]), float(s.get("holdMs", 0.0)),
                                             float(s.get("threshold", 0.0))) for s in steps]
    if not any(s[0] >= hi for s in levels[1:]):
        levels.append((hi, hi, 0.0, 0.0))
    state, hold_until = 0, -math.inf
    out = np.empty(len(v))
    for i, x in enumerate(np.asarray(v, dtype=float)):
        now = i * 1000.0 / fps
        if now >= hold_until and math.isfinite(x):
            up = max(j for j, s in enumerate(levels) if s[0] <= x)
            if up > state:
                state, hold_until = up, now + levels[up][2]
            else:
                while state > 0 and x < levels[state][0] - levels[state][3]:
                    state -= 1
        out[i] = levels[state][1]
    return out


def apply_modifiers(v, mods: dict, lo: float, hi: float, fps: float):
    """delayMs, then smoothing, then steps (VBridger's modifier order)."""
    import numpy as np

    v = np.asarray(v, dtype=float)
    k = int(round(float(mods.get("delayMs", 0.0)) / 1000.0 * fps))
    if k > 0 and len(v):
        v = np.concatenate([np.full(min(k, len(v)), v[0]), v[:max(len(v) - k, 0)]])
    s = float(mods.get("smoothing", 0.0))
    if s > 0 and len(v):
        out = np.empty_like(v)
        out[0] = v[0]
        for i in range(1, len(v)):
            out[i] = out[i - 1] + (1.0 - s) * (v[i] - out[i - 1])
        v = out
    if mods.get("steps"):
        v = apply_steps(v, mods["steps"], lo, hi, fps)
    return v


class Bridge:
    def __init__(self, entries: list[dict], name: str = "bridge"):
        self.name = name
        active = [dict(e) for e in entries if _active(e)]
        for e in active:
            e["formula"] = None if e.get("delayBuffer") else Formula(e["func"])
        self.entries = [active[i] for i in dependency_order(active)]

    @classmethod
    def load(cls, path_or_name: str, overrides: list[dict] | None = None) -> "Bridge":
        name, params = read_preset(path_or_name)
        return cls(effective(params, overrides) if overrides else params, name=name)

    def inputs(self) -> set[str]:
        outputs = {e["name"] for e in self.entries}
        names = set()
        for e in self.entries:
            if e["formula"] is not None:
                names |= e["formula"].names
            elif e["delayBuffer"]["refParam"] not in outputs:
                names.add(e["delayBuffer"]["refParam"])
        return names

    def evaluate(self, x: dict, fps: float = 30.0) -> tuple[dict, list[str]]:
        import numpy as np

        n = len(x["yaw"])
        zeros = np.zeros(n)
        env = dict(x)
        env.update(HeadRotX=x["yaw"], HeadRotY=x["pitch"], HeadRotZ=x["roll"],
                   HeadPosX=zeros, HeadPosY=zeros, HeadPosZ=zeros)
        ms = np.arange(n) * 1000.0 / fps
        for name in self.inputs():
            m = TIME_RE.match(name)
            if m:
                phase = (ms % int(m.group(2))) / int(m.group(2))
                env[name] = 1.0 - np.abs(2.0 * phase - 1.0) if m.group(1) == "Wave" else phase
        missing = sorted(self.inputs() - set(env))
        for m in missing:
            env[m] = zeros
        out: dict = {}
        for e in self.entries:
            if e["formula"] is None:
                ref = e["delayBuffer"]["refParam"]
                v = delay_buffer(out[ref] if ref in out else env[ref], e["delayBuffer"])
            else:
                v = np.broadcast_to(np.asarray(e["formula"](env), dtype=float), (n,)).copy()
                v[~np.isfinite(v)] = float(e.get("defaultValue", 0.0))
            v = np.clip(v, float(e.get("min", -1e6)), float(e.get("max", 1e6)))
            if e.get("modifiers"):
                v = apply_modifiers(v, e["modifiers"], float(e.get("min", -1e6)), float(e.get("max", 1e6)), fps)
            out[e["name"]] = v
        return out, missing
