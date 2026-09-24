import type { AlexisVisualState, CoreVisualSignal } from "../state/AlexisVisualState";
import type { GazeTarget } from "./GazeController";
import type { MorphController } from "../avatar/MorphController";
import type { MorphName } from "../avatar/FaceRig";
import type { GazeController } from "./GazeController";
import type { BlinkController } from "./BlinkController";
import type { HeadMotionController } from "./HeadMotionController";
import type { MicroMotionController } from "./MicroMotionController";
import type { ExpressionController } from "./ExpressionController";
import type { FaceRigResult } from "../avatar/FaceRig";
import type { NeuralMaterialController } from "../rendering/NeuralMaterial";
import type { ParticleSystem } from "../rendering/ParticleSystem";
import { damp } from "../util/damp";

export interface StateVisualParams {
  gaze: GazeTarget;
  head: { yaw: number; pitch: number; roll: number };
  blinkSpeed: number;
  activity: number;
  particleMode: "normal" | "error";
  glow: number;
  microDamping: number;
  expr: Partial<Record<MorphName, number>>;
}

const STATES: Record<AlexisVisualState, StateVisualParams> = {
  idle: {
    gaze: "user",
    head: { yaw: 0, pitch: 0, roll: 0 },
    blinkSpeed: 1,
    activity: 0.15,
    particleMode: "normal",
    glow: 0,
    microDamping: 1,
    expr: {},
  },
  listening: {
    gaze: "user",
    head: { yaw: 0.01, pitch: -0.02, roll: 0 },
    blinkSpeed: 0.9,
    activity: 0.25,
    particleMode: "normal",
    glow: 0.05,
    microDamping: 1.1,
    expr: {},
  },
  thinking: {
    gaze: "thinking",
    head: { yaw: 0.03, pitch: 0.02, roll: 0.01 },
    blinkSpeed: 0.55,
    activity: 0.5,
    particleMode: "normal",
    glow: 0.1,
    microDamping: 0.85,
    expr: { brow_up: 0.22 },
  },
  researching: {
    gaze: "thinking",
    head: { yaw: 0.04, pitch: 0.03, roll: 0.02 },
    blinkSpeed: 0.7,
    activity: 0.85,
    particleMode: "normal",
    glow: 0.18,
    microDamping: 0.8,
    expr: { brow_up: 0.18 },
  },
  focused: {
    gaze: "thinking",
    head: { yaw: 0.01, pitch: 0.02, roll: -0.005 },
    blinkSpeed: 0.8,
    activity: 0.7,
    particleMode: "normal",
    glow: 0.12,
    microDamping: 0.8,
    expr: { brow_up: 0.12, eye_squint: 0.1 },
  },
  speaking: {
    gaze: "user",
    head: { yaw: 0, pitch: -0.01, roll: 0 },
    blinkSpeed: 0.8,
    activity: 0.55,
    particleMode: "normal",
    glow: 0.1,
    microDamping: 1,
    expr: {},
  },
  waiting: {
    gaze: "user",
    head: { yaw: 0, pitch: -0.02, roll: 0 },
    blinkSpeed: 0.8,
    activity: 0.3,
    particleMode: "normal",
    glow: 0.05,
    microDamping: 1,
    expr: { brow_up: 0.1 },
  },
  success: {
    gaze: "user",
    head: { yaw: 0, pitch: 0.01, roll: 0 },
    blinkSpeed: 0.8,
    activity: 0.7,
    particleMode: "normal",
    glow: 0.0,
    microDamping: 1,
    expr: { mouth_smile: 0.3, cheek_raise: 0.14 },
  },
  warning: {
    gaze: "user",
    head: { yaw: 0, pitch: -0.01, roll: 0.02 },
    blinkSpeed: 0.75,
    activity: 0.5,
    particleMode: "normal",
    glow: 0.08,
    microDamping: 0.9,
    expr: { brow_down: 0.16 },
  },
  error: {
    gaze: "away",
    head: { yaw: 0, pitch: 0, roll: 0 },
    blinkSpeed: 0.55,
    activity: 0.2,
    particleMode: "error",
    glow: 0.02,
    microDamping: 0.6,
    expr: { brow_down: 0.28, mouth_frown: 0.18, eye_squint: 0.14 },
  },
};

interface EngineDeps {
  rig: FaceRigResult;
  morph: MorphController;
  gaze: GazeController;
  blink: BlinkController;
  head: HeadMotionController;
  micro: MicroMotionController;
  expression: ExpressionController;
  neural: NeuralMaterialController;
  particles: ParticleSystem;
}

/** Máquina de estados de comportamiento facial (directiva §10). */
export class FaceBehaviorEngine {
  readonly params: StateVisualParams;
  private state: AlexisVisualState = "idle";
  private activityCur = 0.15;
  private glowCur = 0;
  private init = false;

  constructor(private deps: EngineDeps) {
    this.params = STATES.idle;
  }

  getState(): AlexisVisualState {
    return this.state;
  }

  setState(state: AlexisVisualState, signal?: CoreVisualSignal): void {
    this.state = state;
    const p = STATES[state] ?? STATES.idle;
    Object.assign(this.params, p);
    this.deps.gaze.setTarget(p.gaze);
    this.deps.head.setTarget(p.head.yaw, p.head.pitch, p.head.roll);
    this.deps.blink.setSpeed(p.blinkSpeed);
    this.deps.expression.setTargets(p.expr);
    if (state === "success") this.glowCur = 1;
    if (!this.init) {
      this.activityCur = p.activity;
      this.init = true;
    }
  }

  update(dt: number, t: number): void {
    const now = performance.now() / 1000;
    const micro = this.deps.micro;
    micro.update(t, this.params.microDamping);
    this.deps.head.update(dt, micro.output, this.deps.rig);
    this.deps.rig.headRig.position.y = micro.output.breath;
    this.deps.gaze.update(dt, this.deps.rig);
    this.deps.blink.update(dt, now, this.deps.morph);
    this.deps.expression.update(dt);

    const glowTarget = this.params.glow + micro.output.glowFlicker + this.glowCur;
    this.glowCur = Math.max(0, this.glowCur - dt * 1.4);
    this.activityCur = damp(this.activityCur, this.params.activity, 2.2, dt);
    const glow = Math.max(0, glowTarget);

    this.deps.neural.update(t, this.activityCur, glow);
    this.deps.particles.update(t, this.activityCur, this.params.particleMode);
  }
}