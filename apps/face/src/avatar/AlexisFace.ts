import * as THREE from "three";
import type { FaceScene } from "../rendering/SceneManager";
import type { CoreVisualSignal } from "../state/AlexisVisualState";
import { buildFaceRig, type FaceRigResult } from "./FaceRig";
import { MorphController } from "./MorphController";
import { GazeController } from "../behavior/GazeController";
import { BlinkController } from "../behavior/BlinkController";
import { HeadMotionController } from "../behavior/HeadMotionController";
import { MicroMotionController } from "../behavior/MicroMotionController";
import { ExpressionController } from "../behavior/ExpressionController";
import { FaceBehaviorEngine } from "../behavior/FaceBehaviorEngine";
import {
  createFaceMaterial,
  createNetworkLinesMaterial,
  createNetworkPointsMaterial,
  NeuralMaterialController,
} from "../rendering/NeuralMaterial";
import { ParticleSystem } from "../rendering/ParticleSystem";
import { LipSyncEngine } from "../speech/LipSyncEngine";
import { VisemeController } from "../speech/VisemeController";

/** Ensambla el runtime del rostro (directiva §17): rig + behavior + rendering + speech hooks. */
export class AlexisFace {
  readonly rig: FaceRigResult;
  readonly morph: MorphController;
  readonly engine: FaceBehaviorEngine;
  readonly lipSync: LipSyncEngine;
  readonly visemes: VisemeController;

  private gaze = new GazeController();
  private blink = new BlinkController();
  private head = new HeadMotionController();
  private micro = new MicroMotionController();
  private expression: ExpressionController;
  private particles: ParticleSystem;

  constructor(scene: FaceScene) {
    const faceMat = createFaceMaterial();
    const pointMat = createNetworkPointsMaterial();
    const lineMat = createNetworkLinesMaterial();
    const neural = new NeuralMaterialController(faceMat, pointMat, lineMat);
    this.particles = new ParticleSystem();

    this.rig = buildFaceRig(faceMat, pointMat, lineMat);
    this.morph = new MorphController(this.rig.geometry);
    this.expression = new ExpressionController(this.morph);

    this.rig.headRig.add(this.particles.points);
    scene.add(this.rig.faceRoot);

    this.visemes = new VisemeController();
    this.lipSync = new LipSyncEngine();
    this.lipSync.bind(this.visemes);

    this.engine = new FaceBehaviorEngine({
      rig: this.rig,
      morph: this.morph,
      gaze: this.gaze,
      blink: this.blink,
      head: this.head,
      micro: this.micro,
      expression: this.expression,
      neural,
      particles: this.particles,
    });
  }

  /** FASE 15: el adaptador del Core entrega la señal visual y el rostro cambia. */
  applySignal(signal: CoreVisualSignal): void {
    this.engine.setState(signal.state, signal);
  }

  setStateVisual(state: CoreVisualSignal["state"], patch: Partial<CoreVisualSignal> = {}): void {
    this.engine.setState(state, { state, activity: "idle", attention: "user", speaking: false, confidence: 0, ...patch });
  }

  /** FASE 16: la voz está sonando (Web Speech real) → lipSync + boca. */
  setSpeaking(on: boolean): void {
    this.lipSync.setSpeaking(on);
    this.expression.setSpeech(on);
  }

  update(dt: number, t: number): void {
    this.engine.update(dt, t);
  }

  /** Datos para el overlay DEBUG (?debug=1). */
  getGazeTarget(): string {
    return this.gaze.targetName;
  }

  debugInfo(): string {
    const s = this.engine;
    const w = this.morph.weights();
    const weight = Object.entries(w)
      .map(([k, v]) => `${k}=${v}`)
      .join(" ");
    const out = [
      `estado: ${s.getState()}`,
      `gaze: ${this.gaze.targetName}`,
      `parpadeo: activo=${this.blink ? "si" : "no"}`,
      `morphs: ${weight || "(ninguno}"}`,
      `lipSync: speaking=${this.lipSync.speaking ? "true" : "false"} (voz real, fonema reservado)`,
    ];
    return out.join("\n");
  }

  dispose(scene: FaceScene): void {
    scene.scene.remove(this.rig.faceRoot);
    this.rig.geometry.dispose();
    (this.rig.headMesh.material as THREE.Material).dispose();
    this.particles.points.geometry.dispose();
    (this.particles.points.material as THREE.Material).dispose();
  }
}