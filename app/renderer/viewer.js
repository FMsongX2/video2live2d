// Live2D viewer: pixi-live2d-display with the update loop driven here, so motion curves can be
// evaluated at any clock (a playing <video>, a scrub bar, or free-running time).
import { valueAt } from "./motion.js";

const { Live2DModel } = PIXI.live2d;

export const FRAMES = {
  face: { top: 0.04, height: 0.3 },
  upper: { top: 0.0, height: 0.5 },
  full: { top: -0.02, height: 1.04 },
};

export class Viewer {
  constructor(container) {
    this.container = container;
    this.app = new PIXI.Application({ resizeTo: container, backgroundAlpha: 0, antialias: true, autoDensity: true,
                                      resolution: window.devicePixelRatio || 1 });
    container.appendChild(this.app.view);
    this.model = null;
    this.motion = null;          // parsed motion3 or null
    this.clock = () => null;     // returns motion time in seconds, or null for idle
    this.physics = true;
    this.preset = "upper";
    this.zoom = 1;
    this.pan = { x: 0, y: 0 };
    this.current = null;         // Map(parameter id -> value) applied this frame
    this.app.ticker.add(() => this.tick(), null, PIXI.UPDATE_PRIORITY.HIGH);
    new ResizeObserver(() => this.layout()).observe(container);
    this.bindPointer();
  }

  async load(url) {
    if (this.model) {
      this.app.stage.removeChild(this.model);
      this.model.destroy({ children: true, texture: true, baseTexture: true });
      this.model = null;
    }
    const model = await Live2DModel.from(url, { autoUpdate: false, autoInteract: false, idleMotionGroup: "__none__" });
    const im = model.internalModel;
    this.saved = { eyeBlink: im.eyeBlink, physics: im.physics };
    const orig = im.motionManager.update.bind(im.motionManager);
    // Applied after the motion stage and before physics: start from defaults, then the curves.
    im.motionManager.update = (core, now) => {
      const r = orig(core, now);
      if (!this.current) return r;
      const p = core._model.parameters;
      for (let i = 0; i < p.count; i++) p.values[i] = p.defaultValues[i];
      for (const [id, v] of this.current.params) core.setParameterValueById(id, v);
      for (const [id, v] of this.current.parts) core.setPartOpacityById(id, v);
      return true;
    };
    model.anchor.set(0, 0);
    this.app.stage.addChild(model);
    this.model = model;
    this.zoom = 1;
    this.pan = { x: 0, y: 0 };
    this.layout();
    return { parameters: im.coreModel._model.parameters.count };
  }

  setPhysics(on) {
    this.physics = on;
    if (this.model) this.model.internalModel.physics = on ? this.saved.physics : undefined;
  }

  // Parameter limits from the Cubism Core: Map(id -> {min, max, default}); parts as ids.
  parameters() {
    if (!this.model) return { params: new Map(), parts: [] };
    const m = this.model.internalModel.coreModel._model;
    const p = m.parameters;
    const params = new Map();
    for (let i = 0; i < p.count; i++) params.set(p.ids[i], { min: p.minimumValues[i], max: p.maximumValues[i], default: p.defaultValues[i] });
    return { params, parts: [...m.parts.ids] };
  }

  setFrame(preset) {
    this.preset = preset;
    this.zoom = 1;
    this.pan = { x: 0, y: 0 };
    this.layout();
  }

  layout() {
    if (!this.model) return;
    const im = this.model.internalModel;
    const W = this.container.clientWidth, H = this.container.clientHeight;
    const f = FRAMES[this.preset];
    const s = Math.min(H / (f.height * im.height), W / im.width * (this.preset === "full" ? 1 : 2.2)) * this.zoom;
    this.model.scale.set(s);
    this.model.position.set(W / 2 - (im.width * s) / 2 + this.pan.x, -f.top * im.height * s + this.pan.y + (H - f.height * im.height * s) / 2);
  }

  bindPointer() {
    const el = this.app.view;
    el.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.zoom = Math.min(6, Math.max(0.3, this.zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1)));
      this.layout();
    }, { passive: false });
    let drag = null;
    el.addEventListener("pointerdown", (e) => { drag = { x: e.clientX - this.pan.x, y: e.clientY - this.pan.y }; el.setPointerCapture(e.pointerId); });
    el.addEventListener("pointermove", (e) => { if (drag) { this.pan = { x: e.clientX - drag.x, y: e.clientY - drag.y }; this.layout(); } });
    el.addEventListener("pointerup", () => { drag = null; });
    el.addEventListener("dblclick", () => this.setFrame(this.preset));
  }

  tick() {
    if (!this.model) return;
    const im = this.model.internalModel;
    const t = this.motion ? this.clock() : null;
    if (t == null) {
      // Idle: the model's own blinking and breathing keep it alive.
      this.current = null;
      im.eyeBlink = this.saved.eyeBlink;
    } else {
      im.eyeBlink = undefined;
      const params = new Map(), parts = new Map();
      for (const c of this.motion.curves) {
        if (c.target === "Parameter") params.set(c.id, valueAt(c, t));
        else if (c.target === "PartOpacity") parts.set(c.id, valueAt(c, t));
      }
      this.current = { params, parts };
    }
    this.model.update(this.app.ticker.deltaMS);
  }
}
